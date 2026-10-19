"""E4 de RFC-EXP-00018: la invariante S bajo cortes (H2.2).

``run(B1); snapshot; restore en otra máquina; run(B2)`` debe dar lo mismo que ``run(B1 + B2)`` en registros,
``pc``, ``instret``, memoria, gas consumido, UART (salida y cola), ``mtimecmp``, ``Stop`` y ``state_hash()``, para
cualquier corte B1 (P1.7). Este arnés lo comprueba sobre CoreMark, los 14 binarios de Embench y programas aleatorios
con código automodificable, con el intérprete y con los bloques compilados (``warmup`` 1 y 2), con cortes uniformes y
cortes **sesgados** hacia lo que suele perderse al restaurar: stores sobre líneas de código (sube la suma de las
generaciones), MMIO (cambia la longitud de la salida del UART o la cola de entrada) y salidas lentas de los bloques
(``block_instructions`` avanza menos que las instrucciones ejecutadas) y cambios de ``mtimecmp``. Para sesgar
sin instrumentar el hart, una pasada previa con bloques, ``warmup`` 1 y **sin horizonte** (así el intérprete solo
ejecuta lo que un bloque no puede: salidas lentas, instrucciones no traducibles y sus colas, nunca la cola de un
trozo de gas) en trozos de ``run(k)`` pequeño localiza las ventanas de ``instret`` en que ocurren esos eventos, y
los cortes se concentran ahí.

Por programa y modo se ejecutan: (A) el intérprete sin cortes (estado final de referencia); (B) los bloques con
calentamiento 1 y con calentamiento 2 cortados en los mismos puntos sin restaurar (hash de estado en cada corte:
localiza la primera divergencia; cobertura y compilaciones de referencia de cada modo); (C) la cadena con snapshot
y restauración en cada corte, alternando el destino entre una máquina
nueva y la **máquina usada** de la iteración anterior (con las cachés, los bloques y el mapa de código del tramo
anterior del mismo programa), pasando una de cada diez veces por ``snapshot_bytes``/``restore_bytes``;
además, una **sonda que ejecutó el programa entero** (cachés, bloques, mapa y páginas escritas del final: lo que
una restauración sin ``reset`` o con cachés vivas dejaría ver) recibe el mismo snapshot en cada corte (uno de
cada cincuenta en los programas de más de 100 000 instrucciones), se compara con el origen y vuelve a ejecutar
hasta el final para el corte siguiente. En una
muestra de cortes (todos, en los programas de hasta 100 000 instrucciones) se comprueban además los invariantes de
H2.0 (toda palabra cacheada tiene su línea marcada y coincide con la memoria; todo bloque vigente recompila a la
misma fuente; byte ≠ 0 ⇒ página escrita) y la ida y vuelta de las páginas del snapshot. El origen de cada programa
corre con un divisor de tiempo propio (1, 3, 7, 1 000 y, en las anclas de tiempo, 2⁶⁴ − 1, 2³² + 5 y 70 001) y las
máquinas nuevas de destino (cortes pares) nacen con el predeterminado y adoptan el del snapshot; la máquina usada y
la sonda entera ya lo llevan. El corpus del CLI incluye los catorce programas ancla (parches sobre el propio
código, tope de recompilaciones, MMIO con cola de entrada del UART, `mtimecmp` de 64 bits y `mtime` por MMIO, una
palabra por grupo de snapshot, salida larga del UART, CSR de tiempo con divisores extremos), además de CoreMark,
Embench y los aleatorios con registros iniciales al azar.

Criterio del RFC (§5, E4): mata P1 (y P3) una divergencia que solo se cure reintroduciendo el checkpoint por
instrucción o un coste que dependa de la partición; una divergencia por un campo olvidado en el snapshot es un
hallazgo de H2.1 que se corrige y se ancla. Los recuentos son deterministas; los tiempos son informativos:
``ms_snapshot_restore`` cronometra solo ``snapshot()`` + ``restore()`` (y ``ms_snapshot_restore_bytes`` la ruta por
bytes); ``ms_por_corte`` es todo el trabajo del arnés por corte (hashes, comparaciones y comprobaciones incluidas).
Una restauración vacía el calentamiento: con cortes muy densos, el modo con calentamiento 2 apenas compila (los
bloques necesitan dos llegadas al mismo pc dentro de un tramo), lo que el informe muestra como cobertura baja.

Uso (desde la raíz del repositorio):
    python -m benchmarks.benchmark_rv32_invariante [--cortes 1000] [--conjunto todos|coremark|embench|aleatorios|anclas]
        [--aleatorios 200] [--semilla 20261019] [--json informe.json]
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import statistics
import sys
import time
from pathlib import Path

from cpu_digital.rv32_blocks import compile_block
from cpu_digital.rv32_decode import decode
from cpu_digital.rv32_hart import Stop
from cpu_digital.rv32_mem import LINE_SHIFT, Rv32Memory
from cpu_digital.rv32_platform import Machine

ROOT = Path(__file__).resolve().parents[1]
BIN_DIR = ROOT / "benchmarks" / "rv32_arranque" / "bin"
COREMARK = BIN_DIR / "coremark_rv32i_zicsr_it10.bin"
MODES = (("intérprete", False, 2), ("bloques w1", True, 1), ("bloques w2", True, 2))
BIG_RAM = 1 << 20
SMALL_RAM = 1 << 16
BYTES_EVERY = 10          # uno de cada diez cortes pasa por snapshot_bytes / restore_bytes
FINISHED_EVERY = 50       # en programas largos, uno de cada cincuenta cortes pasa por la sonda que ejecutó el programa entero
LONG_PROGRAM = 100_000    # instrucciones a partir de las cuales la sonda entera no se usa en cada corte
CHECK_EVERY = 25          # uno de cada veinticinco cortes comprueba los invariantes derivados y la ida y vuelta


# ── Estado observable y comprobaciones ──

def observable(machine: Machine) -> tuple:
    hart = machine.hart
    return (list(hart.regs), hart.pc, hart.instret, bytes(machine.memory.lanes), bytes(machine.uart.output),
            list(machine.uart.input), machine.clint.mtimecmp, hart.time_divisor)


def derived_errors(machine: Machine) -> list[str]:
    """Invariantes I y W de H2.0 sobre el estado actual: las decodificaciones vigentes tienen su línea marcada y
    coinciden con la memoria; los bloques vigentes tienen sus líneas marcadas y recompilan a la misma fuente (exige
    ``keep_block_sources``; ``compile_block`` marca líneas, así que el mapa de código se deja como estaba); byte ≠ 0
    ⇒ página escrita."""
    hart, memory = machine.hart, machine.memory
    errors: list[str] = []
    gens, code, words = memory.generations, memory.code, memory.words_u
    saved_code = bytes(code)
    for page, (generation, entries) in hart.decode_cache.items():
        if gens[page] != generation:
            continue
        for index, decoded in enumerate(entries):
            if decoded is None:
                continue
            offset = (page << 12) + (index << 2)
            if not code[offset >> LINE_SHIFT]:
                errors.append(f"decodificación sin marca en {offset:#x}")
            if decode(words[offset >> 2]) != decoded:
                errors.append(f"decodificación obsoleta en {offset:#x}")
    for pc, block in hart.blocks.items():
        if block is None or any(gens[p] != g for p, g in zip(block.pages, block.generations)):
            continue
        offset = pc - hart.base
        if any(not code[(offset + 4 * i) >> LINE_SHIFT] for i in range(block.length)):
            errors.append(f"bloque sin marca en {pc:#x}")
        fresh = compile_block(hart, pc, 1, True)
        if fresh is None or fresh.source != block.source:
            errors.append(f"bloque obsoleto en {pc:#x}")
    code[:] = saved_code                                  # recompilar marcó líneas: el mapa vuelve a lo que era
    lanes, written = memory.lanes, memory.written
    for page in range(len(written)):
        if not written[page] and any(lanes[page << 12:(page + 1) << 12]):
            errors.append(f"bytes distintos de cero en la página {page} sin marcar como escrita")
    return errors


def pages_round_trip(memory: Rv32Memory) -> bool:
    scratch = Rv32Memory(memory.size, memory.base)
    try:
        scratch.restore_pages(memory.snapshot_pages())
        return bytes(scratch.lanes) == bytes(memory.lanes)
    finally:
        scratch.close()


def new_machine(image: bytes, ram: int, accelerate: bool, warmup: int, regs: dict[int, int] | None,
                feed: bytes = b"", time_divisor: int = 1) -> Machine:
    machine = Machine(ram_bytes=ram, time_divisor=time_divisor)
    if not machine.hart.horizon:                       # no es un assert: con -O también debe fallar en voz alta
        raise RuntimeError("la invariante S exige gas exacto: horizon=True")
    machine.load_flat(image)
    machine.uart.feed(feed)
    machine.hart.accelerate = accelerate
    machine.hart.warmup = warmup
    machine.hart.keep_block_sources = True   # para recompilar y comparar la fuente en la comprobación derivada
    for register, value in (regs or {}).items():
        machine.hart.regs[register] = value
    return machine


# ── Pasada de perfil y planificación de cortes ──

def profile(image: bytes, ram: int, gas: int, regs: dict[int, int] | None, step: int, feed: bytes = b"",
            time_divisor: int = 1) -> dict[str, object]:
    """Ejecuta con bloques (``warmup`` 1, sin horizonte) en trozos de ``step`` gas y anota las ventanas de ``instret``
    en que la suma de generaciones sube (store sobre código), la salida o la cola del UART o ``mtimecmp`` cambian
    (MMIO) o el intérprete ejecuta algo (salida lenta de un bloque, instrucción no traducible o su cola). Sin
    horizonte el trozo puede rebasar el gas: las ventanas se miden en ``instret``. Devuelve el total de
    instrucciones, la parada, las ventanas, la cobertura de bloques y las compilaciones."""
    machine = new_machine(image, ram, True, 1, regs, feed, time_divisor)
    machine.hart.horizon = False
    try:
        hart, memory, uart, clint = machine.hart, machine.memory, machine.uart, machine.clint
        windows: list[tuple[int, int, str]] = []
        generations = sum(memory.generations)
        output, pending, mtimecmp = len(uart.output), len(uart.input), clint.mtimecmp
        remaining = gas
        stop = Stop("gas")
        while remaining > 0 and stop.reason == "gas":
            before = hart.instret
            blocks_before = hart.block_instructions
            chunk = min(step, remaining)
            stop = hart.run(chunk)
            executed = hart.instret - before
            remaining -= chunk - hart.gas
            kinds = []
            total = sum(memory.generations)
            if total != generations:
                kinds.append("código")
                generations = total
            if (len(uart.output), len(uart.input), clint.mtimecmp) != (output, pending, mtimecmp):
                kinds.append("mmio")
                output, pending, mtimecmp = len(uart.output), len(uart.input), clint.mtimecmp
            if hart.block_instructions - blocks_before < executed:
                kinds.append("lento")
            if kinds:
                windows.append((before, hart.instret, "+".join(kinds)))
        return {"instrucciones": hart.instret, "parada": stop.reason, "ventanas": windows,
                "bloques": hart.block_instructions / hart.instret if hart.instret else 0.0, "compilaciones": hart.compilations}
    finally:
        machine.close()


def plan_cuts(total: int, count: int, rng: random.Random, windows: list[tuple[int, int, str]] | None = None) -> list[int]:
    """``count`` cortes distintos en [1, total): uniformes, o sesgados a las ventanas de eventos (el final y el inicio
    de cada ventana, dos puntos interiores al azar, de 1 a 4 instrucciones después del final y de 1 a 4 antes del
    inicio; lo que no cubran los eventos se reparte uniformemente). Nunca 0 ni ``total``."""
    if total <= 1:
        return []
    count = min(count, total - 1)
    cuts: set[int] = set()
    if windows:
        candidates: list[int] = []
        for start, end, _ in windows:
            candidates.append(end)
            candidates.append(max(1, start))
            candidates.extend(rng.randrange(start, end + 1) for _ in range(2))
            candidates.extend(end + delta for delta in (1, 2, 3, 4))
            candidates.extend(start - delta for delta in (1, 2, 3, 4))
        rng.shuffle(candidates)
        for value in candidates:
            if 1 <= value < total:
                cuts.add(value)
            if len(cuts) >= count:
                break
    while len(cuts) < count:
        cuts.add(rng.randrange(1, total))
    return sorted(cuts)


# ── Ejecuciones ──

def run_uninterrupted(image: bytes, ram: int, gas: int, regs: dict[int, int] | None, feed: bytes = b"",
                      time_divisor: int = 1) -> dict[str, object]:
    machine = new_machine(image, ram, False, 2, regs, feed, time_divisor)
    try:
        started = time.perf_counter()
        stop = machine.run(gas)
        return {"estado": observable(machine), "hash": machine.state_hash(), "parada": stop, "consumido": gas - machine.hart.gas,
                "segundos": time.perf_counter() - started}
    finally:
        machine.close()


def run_chunked(image: bytes, ram: int, gas: int, regs: dict[int, int] | None, cuts: list[int], accelerate: bool,
                warmup: int, feed: bytes = b"", time_divisor: int = 1) -> dict[str, object]:
    """Cortes de gas sin restaurar: hash de estado en cada corte alcanzado."""
    machine = new_machine(image, ram, accelerate, warmup, regs, feed, time_divisor)
    try:
        hashes: list[str] = []
        consumed = 0
        last = 0
        stop = Stop("gas")
        started = time.perf_counter()
        for cut in cuts:
            stop = machine.run(cut - last)
            consumed += (cut - last) - machine.hart.gas
            if stop.reason != "gas":
                break
            last = cut
            hashes.append(machine.state_hash())
        if stop.reason == "gas":
            stop = machine.run(gas - last)
            consumed += (gas - last) - machine.hart.gas
        return {"estado": observable(machine), "hash": machine.state_hash(), "parada": stop, "consumido": consumed,
                "hashes": hashes, "segundos": time.perf_counter() - started,
                "cobertura": machine.hart.block_instructions / machine.hart.instret if machine.hart.instret else 0.0,
                "compilaciones": machine.hart.compilations}
    finally:
        machine.close()


def run_chained(image: bytes, ram: int, gas: int, regs: dict[int, int] | None, cuts: list[int], accelerate: bool,
                warmup: int, expected_hashes: list[str], bytes_every: int = BYTES_EVERY, check_every: int = CHECK_EVERY,
                feed: bytes = b"", finished_every: int = 1, time_divisor: int = 1) -> dict[str, object]:
    """La cadena: en cada corte, snapshot, restauración en otra máquina (nueva en los cortes pares, la máquina usada
    de la iteración anterior en los impares) y continuación; y, uno de cada ``finished_every`` cortes, el mismo
    snapshot en una sonda que ejecutó el programa entero y que vuelve a ejecutarlo hasta el final después. Compara
    el hash de cada corte con ``expected_hashes``. El origen corre con ``time_divisor``; las máquinas nuevas de
    destino (cortes pares) nacen con el predeterminado y lo adoptan al restaurar; la máquina usada y la sonda entera
    ya lo llevan. Si una restauración lanza, el destino se cierra igual."""
    current = new_machine(image, ram, accelerate, warmup, regs, feed, time_divisor)
    spare: Machine | None = None
    finished: Machine | None = None
    target: Machine | None = None
    divergences: list[dict[str, object]] = []
    cut_seconds: list[float] = []
    restore_seconds: list[float] = []        # solo snapshot() + restore() (diccionario)
    bytes_seconds: list[float] = []          # solo snapshot_bytes() + restore_bytes()
    consumed = 0
    last = 0
    reached = 0
    # Las estadísticas no se restauran ni se reinician: la cadena suma los incrementos de cada tramo.
    compilations = 0
    in_blocks = 0
    segment_compilations, segment_blocks = current.hart.compilations, current.hart.block_instructions
    stop = Stop("gas")
    started = time.perf_counter()
    try:
        for index, cut in enumerate(cuts):
            stop = current.run(cut - last)
            consumed += (cut - last) - current.hart.gas
            compilations += current.hart.compilations - segment_compilations
            in_blocks += current.hart.block_instructions - segment_blocks
            segment_compilations, segment_blocks = current.hart.compilations, current.hart.block_instructions
            if stop.reason != "gas":
                break
            last = cut
            reached += 1
            cut_started = time.perf_counter()
            digest = current.state_hash()
            if index < len(expected_hashes) and digest != expected_hashes[index]:
                divergences.append({"corte": cut, "instret": current.hart.instret, "motivo": "hash distinto del de la ejecución sin restaurar"})
            if index % check_every == 0:
                errors = derived_errors(current)
                if errors:
                    divergences.append({"corte": cut, "instret": current.hart.instret, "motivo": "invariantes derivados: " + "; ".join(errors[:3])})
                if not pages_round_trip(current.memory):
                    divergences.append({"corte": cut, "instret": current.hart.instret, "motivo": "las páginas del snapshot no reproducen la memoria"})
            if index % 2 == 0 or spare is None:
                target = Machine(ram_bytes=ram)                     # divisor predeterminado: adopta el del snapshot
                target.hart.accelerate = accelerate
                target.hart.warmup = warmup
                target.hart.keep_block_sources = True
            else:
                target = spare      # la máquina usada: cachés y mapa del tramo anterior del mismo programa
                spare = None
            restore_started = time.perf_counter()
            if index % bytes_every == 0:
                target.restore_bytes(current.snapshot_bytes())
                bytes_seconds.append(time.perf_counter() - restore_started)
            else:
                target.restore(current.snapshot())
                restore_seconds.append(time.perf_counter() - restore_started)
            segment_compilations, segment_blocks = target.hart.compilations, target.hart.block_instructions
            if (target.hart.accelerate, target.hart.warmup, target.hart.horizon) != (accelerate, warmup, True):
                divergences.append({"corte": cut, "instret": current.hart.instret,
                                    "motivo": "la restauración cambió la configuración del destino"})
            if target.state_hash() != digest:
                divergences.append({"corte": cut, "instret": current.hart.instret, "motivo": "la restauración no reproduce el hash"})
            if observable(target) != observable(current):
                divergences.append({"corte": cut, "instret": current.hart.instret, "motivo": "la restauración no reproduce el estado observable"})
            cut_seconds.append(time.perf_counter() - cut_started)   # la sonda entera queda fuera del tiempo por corte
            if index % finished_every == 0:
                if finished is None:
                    finished = new_machine(image, ram, True, 1, regs, feed, time_divisor)   # ejecutó el programa entero:
                    finished.run(gas)                                        # cachés, bloques, mapa y páginas del final
                if index % bytes_every == 0:
                    finished.restore_bytes(current.snapshot_bytes())
                else:
                    finished.restore(current.snapshot())
                if finished.state_hash() != digest or observable(finished) != observable(current):
                    divergences.append({"corte": cut, "instret": current.hart.instret,
                                        "motivo": "la restauración en una máquina que ejecutó el programa entero no reproduce el origen"})
                finished.run(gas - cut)                                      # vuelve al final para el corte siguiente
            if spare is not None:
                spare.close()
            spare = current
            current = target
        if stop.reason == "gas":
            stop = current.run(gas - last)
            consumed += (gas - last) - current.hart.gas
        compilations += current.hart.compilations - segment_compilations
        in_blocks += current.hart.block_instructions - segment_blocks
        return {"estado": observable(current), "hash": current.state_hash(), "parada": stop, "consumido": consumed,
                "cortes_alcanzados": reached, "divergencias": divergences, "segundos": time.perf_counter() - started,
                "ms_por_corte": 1000 * statistics.mean(cut_seconds) if cut_seconds else None,
                "ms_snapshot_restore": 1000 * statistics.mean(restore_seconds) if restore_seconds else None,
                "ms_snapshot_restore_bytes": 1000 * statistics.mean(bytes_seconds) if bytes_seconds else None,
                "compilaciones": compilations,
                "cobertura": in_blocks / current.hart.instret if current.hart.instret else 0.0}
    finally:
        current.close()
        if spare is not None:
            spare.close()
        if finished is not None:
            finished.close()
        if target is not None and target is not current and target is not spare:   # restore lanzó a medias
            target.close()


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def _fmt(value: float | None, digits: int) -> str:
    return "n/d" if value is None else f"{value:.{digits}f}"


def verify_program(name: str, image: bytes, ram: int, gas: int, cuts: int, rng: random.Random,
                   regs: dict[int, int] | None = None, step: int | None = None, modes=MODES,
                   bytes_every: int = BYTES_EVERY, check_every: int | None = None, feed: bytes = b"",
                   time_divisor: int = 1) -> dict[str, object]:
    """Perfil, plan de cortes (uniformes y sesgados), referencia sin cortes, referencias troceadas (una por calentamiento
    con bloques) y cadenas por modo. ``check_every`` por defecto: todos los cortes en programas de hasta
    ``LONG_PROGRAM`` instrucciones, ``CHECK_EVERY`` en los largos."""
    reference = run_uninterrupted(image, ram, gas, regs, feed, time_divisor)
    total = reference["estado"][2]
    if step is None:
        step = 1 if total <= LONG_PROGRAM else 64
    if check_every is None:
        check_every = 1 if total <= LONG_PROGRAM else CHECK_EVERY
    profiled = profile(image, ram, gas, regs, step, feed, time_divisor)
    plans = {"uniformes": plan_cuts(total, cuts, rng), "sesgados": plan_cuts(total, cuts, rng, profiled["ventanas"])}
    result: dict[str, object] = {"programa": name, "instrucciones": total, "parada": reference["parada"].reason,
                                 "ventanas": len(profiled["ventanas"]),
                                 "clases_de_ventana": sorted({kind for _, _, kind in profiled["ventanas"]}),
                                 "segundos_interprete": reference["segundos"], "planes": {}}
    divergences_total = 0
    for plan_name, plan in plans.items():
        # Referencia troceada sin restaurar por calentamiento con bloques (la de calentamiento 1 da los hashes por corte).
        chunked_by_warmup = {warmup: run_chunked(image, ram, gas, regs, plan, True, warmup, feed, time_divisor)
                             for warmup in sorted({warmup for _, accelerate, warmup in modes if accelerate} or {1})}
        chunked = chunked_by_warmup[min(chunked_by_warmup)]
        plan_result: dict[str, object] = {"cortes": len(plan), "cortes_en_ventana": sum(
            1 for cut in plan if any(start - 4 <= cut <= end + 4 for start, end, _ in profiled["ventanas"])), "modos": {}}
        chunked_detail: dict[str, object] | None = None
        for warmup, c in chunked_by_warmup.items():
            if c["hashes"] != chunked["hashes"]:
                first = next((i for i, (a, b) in enumerate(zip(c["hashes"], chunked["hashes"])) if a != b),
                             min(len(c["hashes"]), len(chunked["hashes"])))
                chunked_detail = {"corte": plan[first] if first < len(plan) else None, "calentamiento": warmup,
                                  "motivo": "los hashes de la ejecución troceada sin restaurar difieren entre calentamientos"}
                break
            if c["estado"] != reference["estado"] or c["parada"] != reference["parada"] or c["consumido"] != reference["consumido"]:
                chunked_detail = {"corte": None, "calentamiento": warmup,
                                  "motivo": "la ejecución troceada sin restaurar difiere de la referencia sin cortes (estado final, parada o gas)"}
                break
        chunked_divergence = chunked_detail is not None
        if chunked_divergence:
            divergences_total += 1
        plan_result["troceado_sin_restaurar_igual"] = not chunked_divergence
        plan_result["divergencia_troceada"] = chunked_detail
        for mode_name, accelerate, warmup in modes:
            baseline = chunked_by_warmup.get(warmup, chunked) if accelerate else None
            chained = run_chained(image, ram, gas, regs, plan, accelerate, warmup, chunked["hashes"], bytes_every, check_every, feed,
                                  1 if total <= LONG_PROGRAM else FINISHED_EVERY, time_divisor)
            final_ok = chained["estado"] == reference["estado"] and chained["parada"] == reference["parada"] \
                and chained["consumido"] == reference["consumido"] and chained["hash"] == reference["hash"]
            divergences = list(chained["divergencias"])
            if not final_ok:
                divergences.append({"corte": None, "instret": chained["estado"][2], "motivo": "estado final distinto del de la ejecución sin cortes"})
            divergences_total += len(divergences)
            plan_result["modos"][mode_name] = {
                "cortes_alcanzados": chained["cortes_alcanzados"], "divergencias": divergences[:5], "n_divergencias": len(divergences),
                "ms_por_corte": _round(chained["ms_por_corte"], 2), "segundos": round(chained["segundos"], 3),
                "ms_snapshot_restore": _round(chained["ms_snapshot_restore"], 3),
                "ms_snapshot_restore_bytes": _round(chained["ms_snapshot_restore_bytes"], 3),
                "cobertura_cadena": round(chained["cobertura"], 4),
                "cobertura_sin_restaurar": round(baseline["cobertura"], 4) if baseline else None,
                "compilaciones_cadena": chained["compilaciones"],
                "compilaciones_sin_restaurar": baseline["compilaciones"] if baseline else None,
                "compilaciones_por_restauracion": round((chained["compilaciones"] - baseline["compilaciones"]) / chained["cortes_alcanzados"], 2)
                if baseline and chained["cortes_alcanzados"] else None,
            }
        result["planes"][plan_name] = plan_result
    result["divergencias"] = divergences_total
    result["time_divisor"] = time_divisor
    return result


# ── Conjuntos de programas: (nombre, imagen, RAM, gas, registros iniciales, cola de entrada del UART, divisor) ──

Program = tuple[str, bytes, int, int, dict[int, int] | None, bytes, int]
DIVISORS = (1, 3, 7, 1000)
FEED = b"abcdefgh"   # cola de entrada del UART de los programas ancla (el de MMIO la consume)


def anchor_corpus() -> list[Program]:
    """Catorce programas cortos: los de las revisiones ciegas de H2.0 (parches sobre el propio código, tope de
    recompilaciones, datos en la página del código), uno de MMIO (UART con cola de entrada, ``mtimecmp``), uno que lee
    los CSR de tiempo y, de la segunda ciega del arnés, ``mtimecmp`` de 64 bits con ``mtime`` por MMIO, una sola
    palabra en cada grupo de snapshot, salida larga del UART y los CSR ``time``/``timeh`` con divisores extremos
    (2⁶⁴ − 1, 2³² + 5, 70 001). Divisores distintos en cada uno."""
    from cpu_digital.rv32_isa import CSR_CYCLE, CSR_TIME, CSR_TIMEH, Kind, encode
    from cpu_digital.rv32_platform import CLINT_BASE, CLINT_MTIME, CLINT_MTIMECMP, UART_BASE
    from tests.test_rv32_blocks import SelfModifyingCode
    from tests.test_rv32_hart import CODE, DATA, assemble
    ebreak = encode(Kind.EBREAK)
    loop_with_store = SelfModifyingCode.LOOP_WITH_STORE
    programs = [
        ("sb sobre instrucción ejecutada", [encode(Kind.JAL, 0, imm=0x20)] + [ebreak] * 7 + [
            encode(Kind.ADDI, 1, 1, imm=1), encode(Kind.SB, 0, 10, 5, imm=2), encode(Kind.ADDI, 20, 20, imm=-1),
            encode(Kind.BNE, 0, 20, 0, imm=-12), ebreak], {10: CODE + 0x20, 5: 0x30, 20: 4}),
        ("sw que se pisa a sí mismo", [encode(Kind.JAL, 0, imm=0x20)] + [ebreak] * 7 + [
            encode(Kind.SW, 0, 10, 5, imm=0), encode(Kind.ADDI, 20, 20, imm=-1), encode(Kind.BNE, 0, 20, 0, imm=-8), ebreak],
         {10: CODE + 0x20, 5: encode(Kind.ADDI, 1, 1, imm=5), 20: 4}),
        ("sb en el último byte de la línea del bne", [encode(Kind.JAL, 0, imm=0x20)] + [ebreak] * 7 + [
            encode(Kind.ADDI, 20, 20, imm=-1), encode(Kind.XORI, 5, 5, imm=1), encode(Kind.SB, 0, 11, 5, imm=3),
            encode(Kind.BNE, 0, 20, 0, imm=-12), ebreak], {11: CODE + 0x2C, 5: 0xFE, 16: 2, 20: 6}),
        ("marcas añadidas tras compilar", [encode(Kind.JAL, 0, imm=0x20)] + [ebreak] * 7 + [
            encode(Kind.SW, 0, 11, 5, imm=0), encode(Kind.ADD, 5, 5, 6), encode(Kind.JAL, 0, imm=0x18),
            ebreak, ebreak, ebreak, ebreak, ebreak, encode(Kind.ADDI, 1, 1, imm=0), encode(Kind.ADDI, 20, 20, imm=-1),
            encode(Kind.BNE, 0, 20, 0, imm=-0x28), ebreak],
         {11: CODE + 0x40, 5: encode(Kind.ADDI, 1, 1, imm=1), 6: 0x00100000, 20: 5}),
        ("bucle que escribe sobre su línea (tope de recompilaciones)", loop_with_store, {9: CODE + 0x18, 8: 14}),
        ("bucle con datos en su página", loop_with_store, {9: CODE + 0x100, 8: 14}),
        ("MMIO: UART, cola de entrada y mtimecmp", [
            encode(Kind.LBU, 1, 10, imm=5), encode(Kind.LBU, 2, 10, imm=0), encode(Kind.SB, 0, 10, 2, imm=0),
            encode(Kind.SW, 0, 11, 2, imm=0), encode(Kind.LW, 3, 11, imm=0), encode(Kind.SW, 0, 12, 3, imm=0),
            encode(Kind.ADDI, 20, 20, imm=-1), encode(Kind.BNE, 0, 20, 0, imm=-28), ebreak],
         {10: UART_BASE, 11: CLINT_BASE + CLINT_MTIMECMP, 12: DATA, 20: 6}),
        ("CSR de tiempo con divisor", [encode(Kind.CSRRS, 1, 0, imm=CSR_TIME), encode(Kind.CSRRS, 2, 0, imm=CSR_CYCLE),
            encode(Kind.ADD, 3, 3, 1), encode(Kind.ADDI, 20, 20, imm=-1), encode(Kind.BNE, 0, 20, 0, imm=-16), ebreak],
         {20: 9}),
    ]
    rotated = [(name, words, regs, DIVISORS[index % len(DIVISORS)]) for index, (name, words, regs) in enumerate(programs)]
    mtime_mtimecmp = [encode(Kind.SW, 0, 10, 21, imm=4), encode(Kind.SW, 0, 10, 20, imm=0),   # mtimecmp = (x21 << 32) | x20
                      encode(Kind.LW, 1, 11, imm=0), encode(Kind.LW, 2, 11, imm=4),           # mtime (bajo y alto) por MMIO
                      encode(Kind.LW, 3, 10, imm=0), encode(Kind.LW, 4, 10, imm=4),           # mtimecmp releído
                      encode(Kind.ADD, 5, 5, 1), encode(Kind.ADD, 6, 6, 4), encode(Kind.ADDI, 21, 21, imm=0x111),
                      encode(Kind.ADDI, 20, 20, imm=-1), encode(Kind.BNE, 0, 20, 0, imm=-40), ebreak]
    sparse = [encode(Kind.SW, 0, 10, 5, imm=0), encode(Kind.ADDI, 5, 5, imm=1), encode(Kind.SW, 0, 11, 5, imm=4),
              encode(Kind.ADDI, 5, 5, imm=1), encode(Kind.SW, 0, 12, 5, imm=8), encode(Kind.SW, 0, 10, 0, imm=0),
              encode(Kind.ADDI, 5, 5, imm=1), encode(Kind.SW, 0, 10, 5, imm=16), ebreak]
    long_output = [encode(Kind.SB, 0, 10, 20, imm=0), encode(Kind.ADDI, 20, 20, imm=-1), encode(Kind.BNE, 0, 20, 0, imm=-8), ebreak]
    time_reads = [encode(Kind.CSRRS, 1, 0, imm=CSR_TIME), encode(Kind.CSRRS, 2, 0, imm=CSR_TIMEH), encode(Kind.ADD, 3, 3, 1),
                  encode(Kind.ADDI, 20, 20, imm=-1), encode(Kind.BNE, 0, 20, 0, imm=-16), ebreak]
    extra = [
        ("mtimecmp de 64 bits y mtime por MMIO", mtime_mtimecmp,
         {10: CLINT_BASE + CLINT_MTIMECMP, 11: CLINT_BASE + CLINT_MTIME, 20: 5, 21: 0xA5A50000}, 7),
        ("una sola palabra en cada grupo de snapshot", sparse, {10: CODE + 0x4000, 11: CODE + 0x8100, 12: CODE + 0xC200, 5: 0xDEADBEEF}, 1),
        ("salida larga del UART (80 bytes)", long_output, {10: UART_BASE, 20: 80}, 3),
        ("CSR time y timeh con divisor 2⁶⁴ − 1", time_reads, {20: 9}, (1 << 64) - 1),
        ("CSR time y timeh con divisor 2³² + 5", time_reads, {20: 9}, (1 << 32) + 5),
        ("CSR time y timeh con divisor 70 001", time_reads, {20: 9}, 70_001),
    ]
    return [(f"ancla-{index}: {name}", assemble(words), SMALL_RAM, 2000, regs, FEED, divisor)
            for index, (name, words, regs, divisor) in enumerate(rotated + extra)]


def random_corpus(count: int, rng: random.Random) -> list[Program]:
    """Programas aleatorios de las pruebas de H1/H2.0: lineales, con stores sobre su propio código y los hostiles;
    registros iniciales al azar (salvo las bases, que fija el prólogo) y divisor de tiempo rotatorio."""
    from tests.test_rv32_blocks import HostileProgramsAgainstReference, hostile_layout, loop_program
    from tests.test_rv32_hart import assemble, random_program
    corpus: list[Program] = []
    for index in range(count):
        kind = index % 4
        divisor = DIVISORS[index % len(DIVISORS)]
        if kind == 3:
            image, regs = HostileProgramsAgainstReference.generate(rng)
            corpus.append((f"hostil-{index}", image, SMALL_RAM, 3000, regs, b"", divisor))
            continue
        words = loop_program(rng, rng.randrange(12, 60), rng.randrange(2, 9)) if kind == 0 else random_program(rng, rng.randrange(20, 200))
        if kind == 2:
            words = hostile_layout(words, rng, 0x40, max(64, 4 * len(words) - 0x40))
        regs = {register: rng.getrandbits(32) for register in range(1, 32) if register not in (10, 11, 20)}
        corpus.append((f"aleatorio-{index}", assemble(words), SMALL_RAM, 2000, regs, b"", divisor))
    return corpus


def embench_corpus() -> list[Program]:
    from benchmarks.benchmark_rv32_embench import versioned
    return [(name, path.read_bytes(), BIG_RAM, 400_000_000, None, b"", DIVISORS[index % len(DIVISORS)])
            for index, (name, path) in enumerate(sorted(versioned().items()))]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(description="E4 de RFC-EXP-00018: invariante S bajo cortes")
    parser.add_argument("--cortes", type=int, default=1000)
    parser.add_argument("--conjunto", choices=("todos", "coremark", "embench", "aleatorios", "anclas"), default="todos")
    parser.add_argument("--aleatorios", type=int, default=200)
    parser.add_argument("--semilla", type=int, default=20261019)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    if args.cortes < 1 or args.aleatorios < 0:
        parser.error("cortes debe ser positivo y aleatorios no negativo")
    rng = random.Random(args.semilla)
    corpus: list[Program] = []
    if args.conjunto in ("todos", "coremark"):
        corpus.append(("coremark", COREMARK.read_bytes(), BIG_RAM, 20_000_000, None, b"", 3))
    if args.conjunto in ("todos", "embench"):
        corpus.extend(embench_corpus())
    if args.conjunto in ("todos", "anclas", "aleatorios"):
        corpus.extend(anchor_corpus())
    if args.conjunto in ("todos", "aleatorios"):
        corpus.extend(random_corpus(args.aleatorios, rng))
    print(f"Python {platform.python_version()} ({platform.system()} {platform.machine()}); cortes por plan: {args.cortes}; "
          f"programas: {len(corpus)}; semilla {args.semilla}")
    report: dict[str, object] = {"python": platform.python_version(), "cortes": args.cortes, "semilla": args.semilla, "programas": []}
    divergences = 0
    without_cuts: list[str] = []
    started = time.perf_counter()
    for name, image, ram, gas, regs, feed, time_divisor in corpus:
        result = verify_program(name, image, ram, gas, args.cortes, rng, regs, feed=feed, time_divisor=time_divisor)
        report["programas"].append(result)
        divergences += int(result["divergencias"])
        if not result["planes"]["uniformes"]["cortes"]:
            without_cuts.append(name)
        if name.startswith(("aleatorio", "hostil", "ancla")) and not result["divergencias"]:
            continue
        uniform, biased = result["planes"]["uniformes"], result["planes"]["sesgados"]
        line = (f"{name:16s} {result['instrucciones']:>11,} instr  ventanas {result['ventanas']:>6}  "
                f"cortes {uniform['cortes']}/{biased['cortes']} (sesgados en ventana {biased['cortes_en_ventana']})  ")
        for mode_name, _, _ in MODES:
            mode = biased["modos"][mode_name]
            line += (f"{mode_name}: {mode['n_divergencias']} div, {_fmt(mode['ms_snapshot_restore'], 2)} ms snapshot+restore "
                     f"({_fmt(mode['ms_por_corte'], 1)} ms de arnés por corte)  ")
        for plan_name in ("uniformes", "sesgados"):
            detail = result["planes"][plan_name]["divergencia_troceada"]
            if detail:
                line += f"troceado {plan_name} DIVERGE (corte {detail['corte']}, w{detail['calentamiento']}: {detail['motivo']})  "
        print(line + ("DIVERGE" if result["divergencias"] else "ok"))
    small = [r for r in report["programas"] if str(r["programa"]).startswith(("aleatorio", "hostil", "ancla"))]
    if small:
        cuts_in_windows = sum(int(r["planes"]["sesgados"]["cortes_en_ventana"]) for r in small)
        cuts_total = sum(int(r["planes"]["sesgados"]["cortes"]) for r in small)
        print(f"\n{len(small)} programas ancla/aleatorios/hostiles: {sum(int(r['divergencias']) for r in small)} divergencias; "
              f"cortes sesgados en ventana {cuts_in_windows}/{cuts_total}")
    if without_cuts:
        print(f"Programas sin cortes (una o ninguna instrucción): {len(without_cuts)}: {', '.join(without_cuts[:8])}")
    report["programas_sin_cortes"] = without_cuts
    report["divergencias"] = divergences
    report["segundos"] = time.perf_counter() - started
    report["veredicto"] = "la invariante S se cumple en todos los cortes" if not divergences else f"{divergences} divergencias: triaje"
    print(f"\nDivergencias totales: {divergences} → {report['veredicto']} "
          f"({'P1 sobrevive a E4' if not divergences else 'TRIAJE (P1 en duda)'}); {report['segundos']:.0f} s")
    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
