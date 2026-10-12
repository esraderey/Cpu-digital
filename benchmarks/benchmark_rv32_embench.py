"""Embench IoT 1.0 sobre el hart RV32 (RFC-EXP-00018, H1; complemento informativo de E3).

Compila los benchmarks de Embench 1.0 (etiqueta ``embench-1.0``, commit 0466a18e4f6b; zip con sha256
1a28cf0133f0afb4ffa59a3e6f168b53c27e4b946b27cbd1144dc685a022cb21) con clang para rv32i_zicsr, bare
metal, con el soporte de placa de ``benchmarks/rv32_arranque/embench/`` (disparador por el CSR cycle,
UART en 0x10000000, apagado por SYSCON) y los ejecuta en el hart en los tres modos de E3: intérprete,
bloques sin horizonte y bloques con horizonte. Cada binario publica «ticks=N» (instrucciones entre
los disparadores) y «V0» si ``verify_benchmark`` acepta el resultado. Los binarios se versionan en
``benchmarks/rv32_arranque/bin/`` con su sha256; los benchmarks de coma flotante (cubic, minver,
nbody, st, ud, wikisort y los que arrastran libm) no se enlazan en rv32i, porque no hay compiler-rt
ni libm para riscv32, y se declaran fuera.

Los umbrales de E3 (§5 del RFC) son de CoreMark; aquí se aplican como lectura informativa: intérprete
≥ 0,4 M instr/s y bloques con horizonte ≥ 2× en la media geométrica. Antes de dar por buena una cifra,
comprobar la carga de la máquina y repetir (los tiempos son el mínimo de ``--rondas``).

Disposición de memoria: los binarios versionados enlazan con ``embench/link.ld`` (datos alineados a
página, como los deja cualquier cargador con MMU); con ``--ld empaquetado`` se enlazan con el
``link.ld`` de CoreMark (datos pegados al código) para medir el coste de la invalidación por página
de 4 KiB (P1.6): cada store a datos que comparten página con el código saca al bloque, sube la
generación y lo invalida, hasta el tope de recompilaciones. ``--sin-instalar`` ejecuta lo recién
compilado desde el directorio temporal del sistema sin tocar ``bin/``.

Uso (desde la raíz del repositorio):
    python benchmarks/benchmark_rv32_embench.py [--rondas 2] [--gas 400000000] [--json informe.json]
    python benchmarks/benchmark_rv32_embench.py --construir <embench-iot-embench-1.0> [--clang clang] [--ld alineado|empaquetado] [--sin-instalar]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from cpu_digital.rv32_platform import Machine

ROOT = Path(__file__).resolve().parents[1]
E1_DIR = ROOT / "benchmarks" / "rv32_arranque"
PORT_DIR = E1_DIR / "embench"
COREMARK_PORT = E1_DIR / "coremark"
BIN_DIR = E1_DIR / "bin"
SUMS = BIN_DIR / "SHA256SUMS"
ARCH = "rv32i_zicsr"
EMBENCH_TAG = "embench-1.0"
EMBENCH_COMMIT = "0466a18e4f6b"
BENCHMARKS = ("aha-mont64", "crc32", "cubic", "edn", "huffbench", "matmult-int", "minver", "nbody", "nettle-aes",
              "nettle-sha256", "nsichneu", "picojpeg", "qrduino", "sglib-combined", "slre", "st", "statemate", "ud",
              "wikisort")
MODES = (("intérprete", False, True), ("bloques sin horizonte", True, False), ("bloques con horizonte", True, True))
INTERPRETER_MINIMUM = 400_000
BLOCKS_MINIMUM_RATIO = 2.0
TICKS = re.compile(r"ticks=(\d+)")


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True, capture_output=True, **kwargs)


def build(clang: str, embench: Path, build_dir: Path, linker_script: Path) -> dict[str, Path | str]:
    """Compila cada benchmark; devuelve ruta del .bin o el motivo por el que no enlaza."""
    build_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, Path | str] = {}
    common = [str(PORT_DIR / "start.S"), str(PORT_DIR / "boardsupport.c"), str(PORT_DIR / "libc_mini.c"),
              str(PORT_DIR / "builtins64_rv32i.c"), str(COREMARK_PORT / "support.c"), str(COREMARK_PORT / "builtins_rv32i.c"),
              str(embench / "support" / "main.c"), str(embench / "support" / "beebsc.c")]
    for name in BENCHMARKS:
        sources = sorted(str(p) for p in (embench / "src" / name).glob("*.c"))
        elf = build_dir / f"embench_{name}_{ARCH}.elf"
        binary = build_dir / f"embench_{name}_{ARCH}.bin"
        command = [clang, "--target=riscv32-unknown-elf", f"-march={ARCH}", "-mabi=ilp32", "-O2", "-ffreestanding",
                   "-nostdlib", "-fno-builtin-printf", "-Wno-unused-value", "-Wno-implicit-function-declaration",
                   f"-isystem{PORT_DIR / 'include'}", f"-I{embench / 'support'}", f"-I{embench / 'src' / name}",
                   "-DCPU_MHZ=1", "-DWARMUP_HEAT=1", "-fuse-ld=lld", "-T", str(linker_script), "-o", str(elf),
                   *common, *sources]
        try:
            run(command)
            run(["llvm-objcopy", "-O", "binary", str(elf), str(binary)])
            results[name] = binary
        except subprocess.CalledProcessError as error:
            undefined = sorted(set(re.findall(r"undefined symbol: (\w+)", error.stderr)))
            first = (error.stderr.strip().splitlines() or ["?"])[0]
            results[name] = f"sin enlazar ({', '.join(undefined[:6])})" if undefined else f"error: {first[:160]}"
    return results


def install(results: dict[str, Path | str]) -> list[str]:
    """Copia los .bin compilados a bin/ y reescribe sus líneas en SHA256SUMS; devuelve los instalados."""
    installed = []
    lines = [line for line in SUMS.read_text(encoding="utf-8").splitlines() if line and "embench_" not in line] if SUMS.exists() else []
    for name, result in results.items():
        if isinstance(result, Path):
            target = BIN_DIR / result.name
            shutil.copyfile(result, target)
            lines.append(f"{hashlib.sha256(target.read_bytes()).hexdigest()} *{target.name}")
            installed.append(name)
    SUMS.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return installed


def versioned() -> dict[str, Path]:
    """Binarios de Embench versionados cuyo sha256 coincide con SHA256SUMS."""
    expected = {}
    for line in SUMS.read_text(encoding="utf-8").splitlines():
        if "embench_" in line:
            digest, _, name = line.partition(" *")
            expected[name] = digest
    binaries = {}
    for filename, digest in expected.items():
        path = BIN_DIR / filename
        if not path.is_file():
            raise SystemExit(f"falta {path}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise SystemExit(f"sha256 distinto en {path}")
        binaries[filename.split("_")[1]] = path
    return binaries


def run_mode(image: bytes, accelerate: bool, horizon: bool, gas: int) -> dict[str, object]:
    machine = Machine(ram_bytes=1 << 20)
    try:
        machine.load_flat(image)
        machine.hart.accelerate = accelerate
        machine.hart.horizon = horizon
        started = time.perf_counter()
        stop = machine.run(gas)
        elapsed = time.perf_counter() - started
        text = machine.uart_text
        match = TICKS.search(text)
        hart = machine.hart
        return {"parada": stop.reason, "instrucciones": hart.instret, "segundos": elapsed,
                "instr_por_segundo": hart.instret / elapsed if elapsed else 0.0,
                "ticks": int(match.group(1)) if match else None, "verificado": "V0" in text, "salida": text[-80:],
                "cobertura": hart.block_instructions / hart.instret if hart.instret else 0.0,
                "compilaciones": hart.compilations, "gas_restante": hart.gas}
    finally:
        machine.close()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(description="Embench 1.0 sobre el hart RV32")
    parser.add_argument("--rondas", type=int, default=2)
    parser.add_argument("--gas", type=int, default=400_000_000)
    parser.add_argument("--construir", type=Path, default=None, help="raíz de embench-iot-embench-1.0 ya extraída")
    parser.add_argument("--clang", default="clang")
    parser.add_argument("--ld", choices=("alineado", "empaquetado"), default="alineado",
                        help="alineado: embench/link.ld (datos en página aparte); empaquetado: coremark/link.ld")
    parser.add_argument("--sin-instalar", action="store_true", help="ejecuta lo compilado desde build_embench/ sin tocar bin/")
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    if args.rondas < 1 or args.gas < 1:
        parser.error("rondas y gas deben ser positivos")
    print(f"Python {platform.python_version()} ({platform.system()} {platform.machine()}); Embench {EMBENCH_TAG} "
          f"({EMBENCH_COMMIT}); {ARCH}; rondas: {args.rondas}")
    report: dict[str, object] = {"python": platform.python_version(), "embench": EMBENCH_TAG, "commit": EMBENCH_COMMIT,
                                 "arch": ARCH, "rondas": args.rondas, "ld": args.ld}
    binaries: dict[str, Path] | None = None
    if args.construir is not None:
        clang = shutil.which(args.clang)
        if clang is None:
            print(f"No se encuentra {args.clang}", file=sys.stderr)
            return 2
        linker_script = PORT_DIR / "link.ld" if args.ld == "alineado" else COREMARK_PORT / "link.ld"
        build_dir = Path(tempfile.gettempdir()) / "rv32_embench_build" / args.ld   # fuera del repositorio
        results = build(clang, args.construir, build_dir, linker_script)
        excluded = {name: reason for name, reason in results.items() if not isinstance(reason, Path)}
        if args.sin_instalar:
            binaries = {name: path for name, path in results.items() if isinstance(path, Path)}
            print(f"compilados ({args.ld}, sin instalar): {', '.join(sorted(binaries))}")
        else:
            installed = install(results)
            print(f"compilados ({args.ld}) e instalados en bin/: {', '.join(installed)}")
        for name, reason in excluded.items():
            print(f"  fuera: {name}: {reason}")
        report["excluidos"] = excluded
    if binaries is None:
        binaries = versioned()
    rows: dict[str, dict[str, object]] = {}
    for name, path in sorted(binaries.items()):
        image = path.read_bytes()
        row: dict[str, object] = {"sha256": hashlib.sha256(image).hexdigest()[:16], "bytes": len(image)}
        for mode, accelerate, horizon in MODES:
            rounds = [run_mode(image, accelerate, horizon, args.gas) for _ in range(args.rondas)]
            best = dict(min(rounds, key=lambda r: float(r["segundos"])))
            best["segundos_mediana"] = statistics.median(float(r["segundos"]) for r in rounds)
            row[mode] = best
        rows[name] = row
        interp, blocks = row["intérprete"], row["bloques con horizonte"]
        finished = all(row[m]["parada"] == "apagado" for m, _, _ in MODES)
        same = not finished or len({(r["instrucciones"], r["ticks"], r["verificado"]) for r in (row[m] for m, _, _ in MODES)}) == 1
        print(f"{name:16s} {interp['instrucciones']:>11,} instr  ticks {interp['ticks'] if interp['ticks'] is not None else '—':>10}  "
              f"{'V' if interp['verificado'] else 'X'}  intérprete {interp['instr_por_segundo'] / 1e6:5.2f} M/s  "
              f"bloques {blocks['instr_por_segundo'] / 1e6:5.2f} M/s  {blocks['instr_por_segundo'] / interp['instr_por_segundo']:4.2f}×  "
              f"cobertura {blocks['cobertura']:.1%}  parada {blocks['parada']}  {'' if same else 'RECUENTO DISTINTO'}")
    ratios = [float(r["bloques con horizonte"]["instr_por_segundo"]) / float(r["intérprete"]["instr_por_segundo"]) for r in rows.values()]
    horizon_costs = [float(r["bloques con horizonte"]["segundos"]) / float(r["bloques sin horizonte"]["segundos"]) - 1 for r in rows.values()]
    verdict = {
        "benchmarks": len(rows),
        "todos_verificados": all(r["intérprete"]["verificado"] for r in rows.values()),
        "todos_terminan": all(r[m]["parada"] == "apagado" for r in rows.values() for m, _, _ in MODES),
        "mismo_recuento_en_los_tres_modos": all(len({(r[m]["instrucciones"], r[m]["ticks"]) for m, _, _ in MODES}) == 1 for r in rows.values()),
        "interprete_min_instr_s": min(float(r["intérprete"]["instr_por_segundo"]) for r in rows.values()),
        "interprete_sobrevive": all(float(r["intérprete"]["instr_por_segundo"]) >= INTERPRETER_MINIMUM for r in rows.values()),
        "media_geometrica_bloques": math.exp(sum(math.log(x) for x in ratios) / len(ratios)),
        "ratio_minimo": min(ratios), "ratio_maximo": max(ratios),
        "coste_horizonte_mediano": statistics.median(horizon_costs),
    }
    verdict["bloques_sobreviven"] = verdict["media_geometrica_bloques"] >= BLOCKS_MINIMUM_RATIO
    print(f"\n{len(rows)} benchmarks ({args.ld}); todos terminan: {verdict['todos_terminan']}; todos verificados: "
          f"{verdict['todos_verificados']}; mismo recuento en los tres modos: {verdict['mismo_recuento_en_los_tres_modos']}")
    print(f"Intérprete mínimo: {verdict['interprete_min_instr_s'] / 1e6:.2f} M instr/s → "
          f"{'sobrevive' if verdict['interprete_sobrevive'] else 'MUERE'} (umbral 0,4 M)")
    print(f"Bloques con horizonte / intérprete: media geométrica {verdict['media_geometrica_bloques']:.2f}× "
          f"(mín {verdict['ratio_minimo']:.2f}×, máx {verdict['ratio_maximo']:.2f}×) → "
          f"{'sobreviven' if verdict['bloques_sobreviven'] else 'MUEREN'} (umbral 2×)")
    print(f"Coste mediano del horizonte: {verdict['coste_horizonte_mediano'] * 100:+.1f} %")
    report["benchmarks"] = rows
    report["veredictos"] = verdict
    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
