"""E5 (segunda parte) de RFC-EXP-00018: mutantes de restauración errónea frente al fragmentador (P2.2, H2.3).

Cada mutante es una o varias sustituciones textuales sobre una copia del árbol (``cpu_digital/``, ``benchmarks/``,
``tests/``) en un directorio temporal, de uno en uno: un campo del esquema presente pero mal restaurado (registros,
``pc``, ``instret``, ``time_divisor``, RAM, salida y cola del UART, ``mtimecmp``), cachés no vaciadas (decodificación,
bloques, mapa de código), generaciones que no suben al restaurar (``reset``, escritura del host y ``clear_code``), y
sus combinaciones (un bloque compilado solo sobrevive a una restauración si no se vacía **y** las generaciones no
suben), ``written`` no recalculado, generaciones que bajan y un snapshot que omite el último grupo de páginas. Dos
pasos por mutante, cada uno en un subproceso con el árbol mutado en ``PYTHONPATH``:

1. **Vivacidad** (``--sonda``): ¿cambia el mutante algo observable? Se mide de dos formas. *General*: una batería
   dirigida (programas ancla con parches sobre el propio código, tope de recompilaciones y MMIO con cola de entrada,
   más programas aleatorios) con todos los cortes, restauración en máquina nueva y usada, aceleración distinta en
   origen y destino, comparando ``snapshot()``, ``state_hash()`` y el estado observable tras seguir ejecutando frente
   a la ejecución entera. *En la batería*: lo mismo sobre los programas del detector (los riscv-tests versionados, o
   los aleatorios) con cortes densos. Un mutante equivalente en la batería no puede detectarlo ningún fragmentador
   de esa batería: queda fuera del denominador de E5, y se informa aparte si está vivo en general (es un límite de
   cobertura de los programas, no del fragmentador).
2. **Detección** por el fragmentador con K cortes uniformes y K sesgados: ``benchmark_rv32_riscv_tests`` o, con
   ``--detector aleatorios``, ``benchmark_rv32_invariante`` sobre programas aleatorios. Se anota por separado qué plan
   detectó.

Criterio del RFC (§5, E5): mata P2 si el fragmentador detecta menos del 90 % de los mutantes vivos (en la batería) o
si los cortes sesgados no superan a los uniformes.

Uso (desde la raíz del repositorio):
    python -m benchmarks.benchmark_rv32_mutantes [--detector riscv|aleatorios] [--cortes 64] [--json informe.json]
    python -m benchmarks.benchmark_rv32_mutantes --sonda --detector riscv --json sonda.json   (uso interno)
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SMALL_RAM = 1 << 16
Edit = tuple[str, str, str]   # (archivo relativo, texto original (una sola aparición), texto mutado)

HART = "cpu_digital/rv32_hart.py"
MEM = "cpu_digital/rv32_mem.py"
PLATFORM = "cpu_digital/rv32_platform.py"
CLEARS = ("        self.decode_cache = {}\n        self.blocks = {}\n        self.block_hits = {}\n        self.recompilations = {}\n"
          "        self.memory.clear_code()\n")
NO_BUMPS: list[Edit] = [
    (MEM, "            generations[page] += 1\n            written[page] = 1\n", "            written[page] = 1   # MUTANTE: el host no sube\n"),
    (MEM, "                generations[page] += 1\n                written[page] = 0\n", "                written[page] = 0   # MUTANTE: reset no sube\n"),
    (MEM, "            generations[page] += 1\n            start = page * lines_per_page\n", "            start = page * lines_per_page   # MUTANTE: clear_code no sube\n"),
]
MUTANTS: dict[str, list[Edit]] = {
    "regs_sin_restaurar": [(HART, "        self.regs[:] = regs                # la misma lista, como reset\n",
                            "        pass                               # MUTANTE: registros sin restaurar\n")],
    "regs_desplazados": [(HART, "        self.regs[:] = regs                # la misma lista, como reset\n",
                          "        self.regs[:] = [0] + regs[2:] + regs[1:2]   # MUTANTE: registros desplazados\n")],
    "pc_sin_restaurar": [(HART, "        self.pc = pc\n        self.instret = instret\n", "        self.instret = instret   # MUTANTE: pc sin restaurar\n")],
    "instret_sin_restaurar": [(HART, "        self.pc = pc\n        self.instret = instret\n", "        self.pc = pc   # MUTANTE: instret sin restaurar\n")],
    "instret_mas_uno": [(HART, "        self.pc = pc\n        self.instret = instret\n", "        self.pc = pc\n        self.instret = instret + 1   # MUTANTE\n")],
    "time_divisor_no_adoptado": [(HART, "        self.time_divisor = time_divisor\n        self.decode_cache = {}\n",
                                  "        self.decode_cache = {}   # MUTANTE: time_divisor no adoptado\n")],
    "ram_sin_restaurar": [(MEM, "            self.words_i[start:start + len(values)] = array(\"i\", values)\n",
                           "            pass   # MUTANTE: contenido de la RAM sin restaurar\n")],
    "uart_salida_sin_restaurar": [(PLATFORM, "        self.uart.output = bytearray(output)\n", "        pass   # MUTANTE: salida del UART sin restaurar\n")],
    "uart_cola_sin_restaurar": [(PLATFORM, "        self.uart.input.clear()\n        self.uart.input.extend(pending)\n",
                                 "        pass   # MUTANTE: cola de entrada del UART sin restaurar\n")],
    "mtimecmp_sin_restaurar": [(PLATFORM, "        self.clint.mtimecmp = mtimecmp\n", "        pass   # MUTANTE: mtimecmp sin restaurar\n")],
    "decodificacion_viva": [(HART, CLEARS, CLEARS.replace("        self.decode_cache = {}\n", "        pass   # MUTANTE: decode_cache viva\n"))],
    "bloques_vivos": [(HART, CLEARS, CLEARS.replace("        self.blocks = {}\n", "        pass   # MUTANTE: bloques vivos\n"))],
    "mapa_de_codigo_sin_vaciar": [(HART, CLEARS, CLEARS.replace("        self.memory.clear_code()\n", "        pass   # MUTANTE: mapa sin vaciar\n"))],
    "generaciones_quietas": list(NO_BUMPS),
    "written_sin_recalcular": [(MEM, "        self.reset()\n        for index, values in validated:\n",
                                "        for index, values in validated:   # MUTANTE: sin reset previo (written y contenido viejos)\n")],
    "generaciones_que_bajan": [(MEM, "            generations[page] += 1\n            written[page] = 1\n",
                                "            generations[page] = 0   # MUTANTE: la generación baja\n            written[page] = 1\n")],
    "snapshot_omite_ultimo_grupo": [(MEM, "        for first in range(0, total, pages_per_snapshot_page):\n",
                                     "        for first in range(0, total - pages_per_snapshot_page, pages_per_snapshot_page):   # MUTANTE\n")],
}
MUTANTS["bloques_vivos+generaciones_quietas"] = MUTANTS["bloques_vivos"] + NO_BUMPS
MUTANTS["decodificacion_viva+generaciones_quietas"] = MUTANTS["decodificacion_viva"] + NO_BUMPS
MUTANTS["mapa_sin_vaciar+generaciones_quietas"] = MUTANTS["mapa_de_codigo_sin_vaciar"] + NO_BUMPS


def make_tree(destination: Path) -> None:
    for folder in ("cpu_digital", "benchmarks", "tests"):
        shutil.copytree(ROOT / folder, destination / folder, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def apply(tree: Path, edits: list[Edit]) -> None:
    for relative, old, new in edits:
        path = tree / relative
        text = path.read_text(encoding="utf-8")
        if text.count(old) != 1:
            raise SystemExit(f"el ancla del mutante aparece {text.count(old)} veces en {relative}: {old[:60]!r}")
        path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")


def subprocess_json(tree: Path, module: str, arguments: list[str], output: Path, timeout: int) -> dict[str, object]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(tree)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONUTF8"] = "1"
    completed = subprocess.run([sys.executable, "-m", module, *arguments, "--json", str(output)], cwd=str(tree), env=environment,
                               capture_output=True, text=True, timeout=timeout)
    if not output.is_file():
        raise SystemExit(f"{module} no produjo {output.name} (código {completed.returncode}): {completed.stderr[-800:]}")
    return json.loads(output.read_text(encoding="utf-8"))


# ── Sonda de vivacidad (se ejecuta dentro del árbol mutado) ──

def probe(programs: list[tuple[str, bytes, int, int, dict[int, int] | None, bytes, int]], rng: random.Random,
          dense: int = 200, limit: int = 5) -> dict[str, object]:
    """Todos los cortes (o ``dense`` al azar si el programa es largo), restauración en máquina nueva y usada con
    aceleración distinta en origen y destino; vivo si algo observable cambia frente a la ejecución entera."""
    from benchmarks.benchmark_rv32_invariante import new_machine, observable
    from cpu_digital.rv32_hart import Stop
    from cpu_digital.rv32_platform import Machine

    checks = 0
    alive: list[str] = []
    for name, image, ram, gas, regs, feed, divisor in programs:
        whole = new_machine(image, ram, False, 2, regs, feed, divisor)
        try:
            stop = whole.run(gas)
            total = whole.hart.instret
            expected = (observable(whole), stop, whole.state_hash())
        finally:
            whole.close()
        cuts = range(1, total) if total <= dense else sorted({rng.randrange(1, total) for _ in range(dense)})
        for cut in cuts:
            for accelerate_source, accelerate_target, warmup in ((False, True, 1), (True, False, 1), (True, True, 2)):
                source = new_machine(image, ram, accelerate_source, warmup, regs, feed, divisor)
                used = new_machine(image, ram, True, 1, regs, feed, divisor)
                fresh = Machine(ram_bytes=ram)
                try:
                    if source.run(cut) != Stop("gas"):
                        continue
                    used.run(gas)
                    state = source.snapshot()
                    digest = source.state_hash()
                    for label, target in (("nueva", fresh), ("usada", used)):
                        target.restore(state)
                        checks += 1
                        if target.snapshot() != state or target.state_hash() != digest or observable(target) != observable(source):
                            alive.append(f"{name} corte {cut} destino {label}: la restauración no reproduce el origen")
                            continue
                        target.hart.accelerate, target.hart.warmup = accelerate_target, warmup
                        stop = target.run(gas - cut)
                        if (observable(target), stop, target.state_hash()) != expected:
                            alive.append(f"{name} corte {cut} destino {label} ({accelerate_source}->{accelerate_target}, w{warmup}): estado final distinto")
                finally:
                    source.close()
                    used.close()
                    fresh.close()
                if len(alive) >= limit:
                    return {"vivo": True, "comprobaciones": checks, "motivos": alive[:limit]}
    return {"vivo": bool(alive), "comprobaciones": checks, "motivos": alive[:limit]}


def probe_both(detector: str, random_programs: int, seed: int) -> dict[str, object]:
    from benchmarks.benchmark_rv32_invariante import anchor_corpus, random_corpus

    rng = random.Random(seed)
    general = anchor_corpus() + random_corpus(12, rng)
    if detector == "riscv":
        from benchmarks.benchmark_rv32_riscv_tests import GAS, RAM, versioned
        battery = [(name, path.read_bytes(), RAM, GAS, None, b"", 1) for name, path in sorted(versioned().items())]
    else:
        battery = random_corpus(random_programs, rng)
    return {"general": probe(general, rng), "bateria": probe(battery, rng, dense=120)}


def detector_result(report: dict[str, object], detector: str) -> dict[str, object]:
    """Divergencias por plan (uniformes / sesgados) a partir del JSON del fragmentador elegido."""
    uniform = biased = 0
    entries = report["tests"].values() if detector == "riscv" else report["programas"]
    for entry in entries:
        plans = entry["fragmentado"]["planes"] if detector == "riscv" else entry["planes"]
        key = "divergencias" if detector == "riscv" else "n_divergencias"
        uniform += sum(int(m[key]) for m in plans["uniformes"]["modos"].values())
        biased += sum(int(m[key]) for m in plans["sesgados"]["modos"].values())
        if detector == "riscv" and entry["entero"]["veredicto"] != "pass":
            uniform += 1
            biased += 1
        if detector != "riscv" and not all(p["troceado_sin_restaurar_igual"] for p in plans.values()):
            uniform += 1
            biased += 1
    return {"uniformes": uniform, "sesgados": biased}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(description="E5: mutantes de restauración errónea frente al fragmentador")
    parser.add_argument("--sonda", action="store_true", help="uso interno: sonda de vivacidad en el árbol actual")
    parser.add_argument("--detector", choices=("riscv", "aleatorios"), default="riscv")
    parser.add_argument("--cortes", type=int, default=64)
    parser.add_argument("--aleatorios", type=int, default=30, help="programas del detector aleatorio")
    parser.add_argument("--semilla", type=int, default=5)
    parser.add_argument("--solo", default=None, help="nombres de mutantes separados por coma (por defecto, todos)")
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    if args.sonda:
        result = probe_both(args.detector, args.aleatorios, args.semilla)
        if args.json:
            args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
        return 0
    names = list(MUTANTS) if args.solo is None else [name.strip() for name in args.solo.split(",")]
    unknown = [name for name in names if name not in MUTANTS]
    if unknown:
        parser.error(f"mutantes desconocidos: {unknown}")
    print(f"Python {platform.python_version()} ({platform.system()} {platform.machine()}); detector {args.detector}; cortes {args.cortes}; "
          f"mutantes {len(names)}")
    probe_arguments = ["--sonda", "--detector", args.detector, "--aleatorios", str(args.aleatorios), "--semilla", str(args.semilla)]
    detector_arguments = (["--cortes", str(args.cortes), "--semilla", str(args.semilla)] if args.detector == "riscv"
                          else ["--conjunto", "aleatorios", "--aleatorios", str(args.aleatorios), "--cortes", str(args.cortes),
                                "--semilla", str(args.semilla)])
    detector_module = "benchmarks.benchmark_rv32_riscv_tests" if args.detector == "riscv" else "benchmarks.benchmark_rv32_invariante"
    report: dict[str, object] = {"python": platform.python_version(), "detector": args.detector, "cortes": args.cortes, "mutantes": {}}
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="rv32_mutantes_") as temporary:
        base = Path(temporary) / "base"
        make_tree(base)
        baseline_probe = subprocess_json(base, "benchmarks.benchmark_rv32_mutantes", probe_arguments, base / "sonda.json", 1800)
        baseline = detector_result(subprocess_json(base, detector_module, detector_arguments, base / "detector.json", 3600), args.detector)
        clean = not baseline_probe["general"]["vivo"] and not baseline_probe["bateria"]["vivo"] and not baseline["uniformes"] and not baseline["sesgados"]
        print(f"base: sonda general {baseline_probe['general']['comprobaciones']} comprobaciones, batería "
              f"{baseline_probe['bateria']['comprobaciones']}; {'limpia' if clean else 'NO LIMPIA'}; detector uniformes "
              f"{baseline['uniformes']} sesgados {baseline['sesgados']}")
        report["base"] = {"sonda": baseline_probe, "detector": baseline}
        if not clean:
            print("La base no está limpia: no se evalúan mutantes")
            return 2
        for name in names:
            tree = Path(temporary) / name
            make_tree(tree)
            apply(tree, MUTANTS[name])
            probed = subprocess_json(tree, "benchmarks.benchmark_rv32_mutantes", probe_arguments, tree / "sonda.json", 1800)
            entry: dict[str, object] = {"vivo_general": probed["general"]["vivo"], "vivo_bateria": probed["bateria"]["vivo"],
                                        "motivos": probed["general"]["motivos"] or probed["bateria"]["motivos"]}
            if probed["bateria"]["vivo"]:
                detected = detector_result(subprocess_json(tree, detector_module, detector_arguments, tree / "detector.json", 3600), args.detector)
                entry["detector"] = detected
                entry["detectado_uniformes"] = detected["uniformes"] > 0
                entry["detectado_sesgados"] = detected["sesgados"] > 0
            report["mutantes"][name] = entry
            if probed["bateria"]["vivo"]:
                status = (f"vivo; uniformes {'DETECTA' if entry['detectado_uniformes'] else 'no'} ({entry['detector']['uniformes']}), "
                          f"sesgados {'DETECTA' if entry['detectado_sesgados'] else 'no'} ({entry['detector']['sesgados']})")
            elif probed["general"]["vivo"]:
                status = "vivo en general pero no observable con la batería (límite de cobertura de los programas)"
            else:
                status = "equivalente (ninguna sonda lo distingue)"
            print(f"{name:44s} {status}")
            shutil.rmtree(tree, ignore_errors=True)
    alive = [name for name, entry in report["mutantes"].items() if entry["vivo_bateria"]]
    only_general = [name for name, entry in report["mutantes"].items() if entry["vivo_general"] and not entry["vivo_bateria"]]
    detected_uniform = sum(1 for name in alive if report["mutantes"][name]["detectado_uniformes"])
    detected_biased = sum(1 for name in alive if report["mutantes"][name]["detectado_sesgados"])
    detected_any = sum(1 for name in alive if report["mutantes"][name]["detectado_uniformes"] or report["mutantes"][name]["detectado_sesgados"])
    rate = detected_any / len(alive) if alive else 0.0
    report["resumen"] = {"mutantes": len(names), "vivos_en_bateria": len(alive), "vivos_solo_en_general": only_general,
                         "detectados": detected_any, "detectados_uniformes": detected_uniform, "detectados_sesgados": detected_biased,
                         "tasa": rate, "p2_sobrevive": bool(alive) and rate >= 0.9 and detected_biased >= detected_uniform,
                         "segundos": time.perf_counter() - started}
    print(f"\n{len(names)} mutantes: {len(alive)} vivos en la batería (y {len(only_general)} vivos solo en general: {', '.join(only_general) or '—'}); "
          f"el fragmentador detecta {detected_any} ({rate:.0%}; umbral 90 %): uniformes {detected_uniform}, sesgados {detected_biased} → "
          f"{'P2 sobrevive' if report['resumen']['p2_sobrevive'] else 'MATA P2 (o sin mutantes vivos)'}; {report['resumen']['segundos']:.0f} s")
    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
