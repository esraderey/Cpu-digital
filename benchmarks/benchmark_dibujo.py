"""Qué le haría falta al raycaster de referencia para ir más rápido (RFC-EXP-00017).

Mide ``vm_programs/juego_raycaster.tasm`` con el acelerador de bucles y responde a cinco
preguntas:

1. ¿Qué ejecuta? La mezcla de instrucciones.
2. ¿Dónde se va el tiempo? Lo que tarda cada bucle compilado y lo que tarda el intérprete
   con el resto de instrucciones, y el tope de la ley de Amdahl si la coma flotante no
   costara nada, que es lo más que daría un «núcleo matemático» escalar.
3. ¿Cuánto costarían en el host las dos operaciones de la unidad de dibujo (P2)? Un relleno
   con paso (techo, muro y suelo) y una columna de textura escalada en 16.16 con téxeles
   transparentes (sprites), prototipadas en Python sobre las páginas de ``PagedMemory`` y
   comprobadas contra una implementación palabra a palabra. Con esos costes estima el tiempo
   del juego si las dos operaciones sustituyeran a sus bucles.
4. ¿Qué dejaría en el intérprete un acelerador con varios saltos de vuelta, bucles anidados
   y cuerpos largos (P1)? Lo simula por fase sobre la traza del intérprete con las reglas de
   ``benchmark_cobertura.py``, y estima el tiempo.
5. ¿Y las dos propuestas juntas? Estimación gruesa, con los mismos costes.

Uso (desde la raíz del repositorio):
    python benchmarks/benchmark_dibujo.py [--frames 20] [--rondas 3]
"""

from __future__ import annotations

import argparse
import importlib.util
import operator
import platform
import statistics
import sys
import time
from array import array
from collections import Counter, defaultdict
from itertools import compress
from pathlib import Path
from types import ModuleType

from cpu_digital.memory import PagedMemory
from cpu_digital.vm32 import TramoyaVM32, VMConfig
from cpu_digital.vm32_assembler import Program32, VM32Assembler
from cpu_digital.vm32_isa import VMOpcode as Op

ROOT = Path(__file__).resolve().parents[1]
GAME = ROOT / "vm_programs" / "juego_raycaster.tasm"
GAS = 4 * 10**9
FB, WIDTH = 0x10000, 160
FILLS = ("RM_CEIL", "RM_WL", "RM_FL")  # techo, muro y suelo: 4 filas por vuelta
TEXELS = "RS_PIX"                       # sprites: 2 téxeles por vuelta
GROUPS = {
    "coma flotante": {Op.FADD, Op.FSUB, Op.FMUL, Op.FDIV, Op.FSQRT, Op.FABS, Op.FCMP, Op.FTOI, Op.ITOF},
    "aritmética y lógica entera": {Op.ADD, Op.ADDI, Op.SUB, Op.SUBI, Op.MUL, Op.MULI, Op.DIV, Op.MOD, Op.AND,
                                   Op.OR, Op.XOR, Op.NOT, Op.SHL, Op.SHR, Op.CMP, Op.CMPI, Op.TEST},
    "memoria (LOAD, STORE)": {Op.LOAD, Op.STORE},
    "saltos, llamadas y retornos": {Op.JMP, Op.JZ, Op.JNZ, Op.JNEG, Op.JPOS, Op.JC, Op.JNC, Op.JLT, Op.JGT,
                                    Op.CALL, Op.CALLR, Op.RET},
    "movimientos (MOV, MOVI, LEA)": {Op.MOV, Op.MOVI, Op.LEA},
}


class Tracing(TramoyaVM32):
    """Intérprete que anota el PC de cada instrucción."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.pcs = array("l")

    def _execute_one(self) -> None:
        self.pcs.append(self._pc)
        super()._execute_one()


class Timed(TramoyaVM32):
    """Acelerador que mide el tiempo pasado dentro de cada bucle compilado."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.loop_seconds: defaultdict[int, float] = defaultdict(float)
        self.loop_steps: Counter[int] = Counter()

    def _run_loop(self, loop, budget: int) -> bool:
        before = self._accelerated_instructions
        started = time.perf_counter()
        try:
            return super()._run_loop(loop, budget)
        finally:
            self.loop_seconds[loop.header] += time.perf_counter() - started
            self.loop_steps[loop.header] += self._accelerated_instructions - before


def new_vm(program: Program32, frames: int, accelerate: bool, cls: type[TramoyaVM32] = TramoyaVM32) -> TramoyaVM32:
    vm = cls(VMConfig(gas_limit=GAS, trace_size=0, accelerate_loops=accelerate))
    vm.load_program(program, inputs=(frames,))
    return vm


def label_of(labels: dict[int, str], pc: int) -> str:
    best = max((address for address in labels if address <= pc), default=None)
    if best is None:
        return str(pc)
    return labels[best] if best == pc else f"{labels[best]}+{(pc - best) // 4}"


def coverage_benchmark() -> ModuleType:
    # benchmarks/ no es un paquete instalado: el script hermano se carga por su ruta.
    path = Path(__file__).resolve().with_name("benchmark_cobertura.py")
    spec = importlib.util.spec_from_file_location("benchmark_cobertura", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # las dataclasses del script buscan aquí su módulo
    spec.loader.exec_module(module)
    return module


# ── Las dos operaciones de la unidad de dibujo, en el host ──────────────


def fill_column(memory: PagedMemory, start: int, count: int, stride: int, value: int) -> None:
    """Escribe ``value`` en ``count`` palabras desde ``start`` con paso ``stride``, página a página."""
    pages, size = memory._pages, memory.page_words
    address, left = start, count
    while left:
        index, offset = divmod(address, size)
        page = pages.get(index)
        if page is None:
            page = pages[index] = memory._new_page(index)
        here = min(left, (size - offset + stride - 1) // stride)
        page[offset:offset + here * stride:stride] = array("i", [value]) * here
        address += here * stride
        left -= here


def blit_column(memory: PagedMemory, destination: int, accumulator: int, step: int, count: int, stride: int) -> None:
    """Columna de textura escalada: el téxel k es memory[(accumulator + k·step) >> 16] y el 0 es
    transparente. Lee los téxeles de una vez y mezcla cada página de destino con un slice."""
    pages, size = memory._pages, memory.page_words
    first, last = accumulator >> 16, (accumulator + step * (count - 1)) >> 16
    source = memory.read_block(first, last - first + 1)
    texels = [source[((accumulator + k * step) >> 16) - first] for k in range(count)]
    row, address = 0, destination
    while row < count:
        index, offset = divmod(address, size)
        page = pages.get(index)
        if page is None:
            page = pages[index] = memory._new_page(index)
        here = min(count - row, (size - offset + stride - 1) // stride)
        span = slice(offset, offset + here * stride, stride)
        page[span] = array("i", [t if t else o for t, o in zip(texels[row:row + here], page[span])])
        row += here
        address += here * stride


def check_prototypes() -> None:
    """Las dos operaciones escriben lo mismo que una implementación palabra a palabra."""
    texture = 200_000
    for rows in (1, 4, 37, 120):
        fast, slow = PagedMemory(1 << 20), PagedMemory(1 << 20)
        fill_column(fast, FB + 37, rows, WIDTH, 5)
        for k in range(rows):
            slow[FB + 37 + k * WIDTH] = 5
        for k in range(17):
            fast[texture + k] = slow[texture + k] = (k % 3) * 7  # con téxeles transparentes
        step = (16 << 16) // rows
        blit_column(fast, FB + 50, texture << 16, step, rows, WIDTH)
        accumulator = texture << 16
        for k in range(rows):
            texel = slow[accumulator >> 16]
            if texel:
                slow[FB + 50 + k * WIDTH] = texel
            accumulator += step
        if fast.read_block(FB, WIDTH * 121) != slow.read_block(FB, WIDTH * 121):
            raise SystemExit(f"Los prototipos no coinciden con la referencia ({rows} filas)")


def cost_model(function, sizes: tuple[int, int], make_args, repeats: int = 2000) -> tuple[float, float]:
    """Coste fijo y coste por fila (segundos) de ``function``, ajustados con dos tamaños."""
    costs = []
    for rows in sizes:
        args = make_args(rows)
        samples = []
        for _ in range(5):
            started = time.perf_counter()
            for _ in range(repeats):
                function(*args)
            samples.append((time.perf_counter() - started) / repeats)
        costs.append(statistics.median(samples))
    per_row = (costs[1] - costs[0]) / (sizes[1] - sizes[0])
    return costs[0] - per_row * sizes[0], per_row


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(description="Lo que le haría falta al raycaster de referencia (RFC-EXP-00017)")
    parser.add_argument("--frames", type=int, default=20, help="Frames del juego")
    parser.add_argument("--rondas", type=int, default=3, help="Ejecuciones medidas; se toma la mediana")
    args = parser.parse_args()
    if args.frames < 1 or args.rondas < 1:
        parser.error("frames y rondas deben ser positivos")

    program = VM32Assembler().assemble(GAME.read_text(encoding="utf-8"), GAME.name).program
    labels = {value: name for name, value in program.symbols.items()
              if 0 <= value < program.code_size and value % 4 == 0}
    by_name = {name: value for value, name in labels.items()}
    print(f"Python {platform.python_version()} · {platform.system()} {platform.machine()} · "
          f"{args.frames} frames · rondas={args.rondas}\n")

    # 1. Mezcla de instrucciones y recuentos de los bucles de dibujo (deterministas).
    traced = new_vm(program, args.frames, accelerate=False, cls=Tracing)
    traced.run()
    pcs = traced.pcs
    counts = Counter(pcs)
    total = len(pcs)
    print(f"1. Mezcla de instrucciones ({total:,})")
    weights: Counter[Op] = Counter()
    for pc, n in counts.items():
        weights[traced._fetch_decoded(pc)[0].opcode] += n
    for name, ops in GROUPS.items():
        n = sum(weights[op] for op in ops)
        print(f"   {name:<30}{n:>10,}{100 * n / total:>8.1f} %")

    # Vueltas y entradas de cada bucle de dibujo: una vuelta por llegada a su cabecera, y una
    # entrada por llegada que no viene de su propio salto de vuelta (que está detrás).
    headers = {by_name[name]: name for name in (*FILLS, TEXELS)}
    iterations, back = Counter(), Counter()
    previous = -1
    for pc in pcs:
        name = headers.get(pc)
        if name is not None:
            iterations[name] += 1
            back[name] += previous > pc
        previous = pc
    entries = {name: iterations[name] - back[name] for name in headers.values()}
    fill_rows = sum(4 * iterations[name] for name in FILLS)
    fill_calls = sum(entries[name] for name in FILLS)
    texels, blit_calls = 2 * iterations[TEXELS], entries[TEXELS]

    # 2. Reparto del tiempo con el acelerador: total sin instrumentar y reparto instrumentado.
    plain = []
    for _ in range(args.rondas):
        vm = new_vm(program, args.frames, accelerate=True)
        started = time.perf_counter()
        vm.run()
        plain.append(time.perf_counter() - started)
    seconds = statistics.median(plain)
    timed_runs = []
    for _ in range(args.rondas):
        vm = new_vm(program, args.frames, accelerate=True, cls=Timed)
        started = time.perf_counter()
        vm.run()
        timed_runs.append((time.perf_counter() - started, vm))
    timed_total, timed = sorted(timed_runs, key=lambda item: item[0])[len(timed_runs) // 2]
    compiled = sum(timed.loop_seconds.values())
    interpreted = timed.result().instructions - timed.accelerated_instructions
    per_interpreted = (timed_total - compiled) / interpreted
    print(f"\n2. Con acelerador: {seconds:.3f} s ({1000 * seconds / args.frames:.1f} ms por frame, "
          f"{args.frames / seconds:.1f} frames/s). Reparto de la ejecución instrumentada ({timed_total:.3f} s):")
    print(f"   bucles compilados {100 * compiled / timed_total:5.1f} % del tiempo, "
          f"{100 * timed.accelerated_instructions / total:5.1f} % de las instrucciones, "
          f"{1e6 * compiled / timed.accelerated_instructions:.2f} µs cada una")
    print(f"   intérprete        {100 * (timed_total - compiled) / timed_total:5.1f} % del tiempo, "
          f"{100 * interpreted / total:5.1f} % de las instrucciones, {1e6 * per_interpreted:.2f} µs cada una")
    print(f"   {'bucle':<12}{'% tiempo':>10}{'instr.':>11}{'µs/instr':>10}")
    for header, spent in sorted(timed.loop_seconds.items(), key=lambda item: -item[1])[:8]:
        print(f"   {label_of(labels, header):<12}{100 * spent / timed_total:>9.1f}%{timed.loop_steps[header]:>11,}"
              f"{1e6 * spent / max(1, timed.loop_steps[header]):>10.3f}")
    drawing = sum(timed.loop_seconds[by_name[name]] for name in (*FILLS, TEXELS))
    print(f"   los cuatro bucles de dibujo: {100 * drawing / timed_total:.1f} % del tiempo")
    # Qué instrucciones acelera hoy el acelerador: su simulación sobre la traza, que es exacta.
    cobertura = coverage_benchmark()
    analysis = cobertura.LoopAnalysis(program, pcs)
    now = analysis.simulate(None)
    if sum(now) != timed.accelerated_instructions:
        raise SystemExit("La simulación del acelerador no coincide con la medición")
    per_compiled = compiled / timed.accelerated_instructions
    floats = {pc for pc in counts if traced._fetch_decoded(pc)[0].opcode in GROUPS["coma flotante"]}
    float_fast = sum(1 for pc in compress(pcs, now) if pc in floats)
    float_slow = sum(counts[pc] for pc in floats) - float_fast
    float_seconds = float_slow * per_interpreted + float_fast * per_compiled
    print(f"   coma flotante: {100 * (float_slow + float_fast) / total:.1f} % de las instrucciones y "
          f"~{100 * float_seconds / timed_total:.1f} % del tiempo; si no costara nada, como mucho "
          f"{timed_total / (timed_total - float_seconds):.2f}× (ley de Amdahl)")

    # 3. Las dos operaciones en el host y la estimación.
    check_prototypes()
    memory = PagedMemory(1 << 20)
    texture = 200_000
    for k in range(17):
        memory[texture + k] = (k % 3) * 7
    fill_fixed, fill_row = cost_model(fill_column, (8, 120), lambda rows: (memory, FB + 37, rows, WIDTH, 5))
    blit_fixed, blit_row = cost_model(
        blit_column, (8, 120), lambda rows: (memory, FB + 50, texture << 16, (16 << 16) // rows, rows, WIDTH))
    loop_fill = sum(timed.loop_seconds[by_name[name]] for name in FILLS)
    loop_blit = timed.loop_seconds[by_name[TEXELS]]
    print("\n3. Unidad de dibujo en el host (prototipos comprobados contra la referencia palabra a palabra)")
    print(f"   relleno con paso:  {1e6 * fill_fixed:.2f} µs + {1e9 * fill_row:.0f} ns por fila; "
          f"hoy, {1e6 * loop_fill / fill_calls:.1f} µs por relleno ({fill_calls:,} rellenos, {fill_rows:,} filas)")
    print(f"   columna de sprite: {1e6 * blit_fixed:.2f} µs + {1e9 * blit_row:.0f} ns por téxel; "
          f"hoy, {1e9 * loop_blit / texels:.0f} ns por téxel ({blit_calls:,} columnas, {texels:,} téxeles)")
    # Cada instrucción nueva cuesta un despacho del intérprete más su trabajo en el host.
    new_fill = fill_calls * (per_interpreted + fill_fixed) + fill_rows * fill_row
    new_blit = blit_calls * (per_interpreted + blit_fixed) + texels * blit_row
    estimate = seconds * (timed_total - loop_fill - loop_blit + new_fill + new_blit) / timed_total
    print(f"   dibujo: {loop_fill + loop_blit:.3f} s en la ejecución instrumentada -> {new_fill + new_blit:.3f} s")
    print(f"   juego: {seconds:.3f} s -> ~{estimate:.3f} s ({seconds / estimate:.2f}×, "
          f"~{args.frames / estimate:.1f} frames/s); tope si el dibujo fuera gratis: "
          f"{timed_total / (timed_total - loop_fill - loop_blit):.2f}×")

    # 4. P1: las extensiones de benchmark_cobertura salvo las llamadas (en este juego no aportan),
    # en dos pasos. Las instrucciones que pasan a compilarse se suponen al coste medio de una
    # compilada hoy.
    p1 = cobertura.EXTENSIONS[-1][1] - {"llamada"}
    print("\n4. P1 simulada sobre la traza del intérprete")
    print(f"   {'acelerador':<52}{'cobertura':>10}{'interpretadas':>15}{'juego':>10}")
    print(f"   {'actual':<52}{100 * sum(now) / total:>9.2f}%{total - sum(now):>15,}{seconds:>9.3f}s")
    first = None
    for name, allowed in (("+ varios saltos de vuelta (P1a)", p1 - {"anidado", "largo"}),
                          ("  + bucles anidados, con cuerpos de 64 como hoy", p1 - {"largo"}),
                          ("  + cuerpos de más de 64, sin bucles anidados", p1 - {"anidado"}),
                          ("  + bucles anidados y cuerpos de más de 64 (P1b)", p1)):
        extended = analysis.simulate(allowed)
        first = extended if first is None else first
        moved = sum(extended) - sum(now)
        p1_total = timed_total - moved * (per_interpreted - per_compiled)
        print(f"   {name:<52}{100 * sum(extended) / total:>9.2f}%{total - sum(extended):>15,}"
              f"{seconds * p1_total / timed_total:>9.3f}s  ({timed_total / p1_total:.2f}×, "
              f"~{args.frames * timed_total / (seconds * p1_total):.1f} frames/s)")
    owner = {pc: label_of(labels, pc).split("+")[0] for pc in counts}
    gained = Counter(owner[pc] for pc, before, after in zip(pcs, now, first) if after and not before)
    print("   P1a compila además: " + ", ".join(f"{name} {n:,}" for name, n in gained.most_common()))
    phase_of = {pc: name for name, start, end in cobertura.phase_ranges(program) for pc in range(start, end, 4)}
    executed = Counter(map(phase_of.__getitem__, pcs))
    slow_now = Counter(map(phase_of.__getitem__, compress(pcs, map(operator.not_, now))))
    slow_p1 = Counter(map(phase_of.__getitem__, compress(pcs, map(operator.not_, extended))))
    print(f"\n   {'fase':<18}{'instr.':>11}{'interpretadas hoy':>19}{'con P1':>10}")
    for name, _, _ in cobertura.phase_ranges(program):
        print(f"   {name:<18}{executed[name]:>11,}{slow_now[name]:>19,}{slow_p1[name]:>10,}")

    # 5. Las dos juntas: P2 dentro de las regiones de P1. Los rellenos están en RM_COL, que P1
    # compila, así que su despacho costaría el de una instrucción compilada; las columnas de
    # sprite siguen en el intérprete (el camino visible está fuera de línea).
    both = (p1_total - loop_fill - loop_blit + fill_calls * (per_compiled + fill_fixed) + fill_rows * fill_row
            + blit_calls * (per_interpreted + blit_fixed) + texels * blit_row)
    print(f"\n5. P1 y P2 juntas: juego {seconds:.3f} s -> ~{seconds * both / timed_total:.3f} s "
          f"({timed_total / both:.2f}×, ~{args.frames * timed_total / (seconds * both):.1f} frames/s), "
          f"con los mismos supuestos")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
