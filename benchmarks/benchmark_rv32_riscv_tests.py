"""E5 (primera parte) de RFC-EXP-00018: riscv-tests rv32ui sobre el hart, enteros y fragmentados (H2.3).

Compila ``isa/rv32ui/*.S`` de riscv-software-src/riscv-tests (commit ``RISCV_TESTS_COMMIT``, zip con sha256
``RISCV_TESTS_ZIP_SHA256``; los ``rv32ui/*.S`` son envoltorios de los cuerpos de ``isa/rv64ui``, con los que forman
los 42 tests de la extensión I sin privilegios) con el ensamblador integrado de clang
(``--target=riscv32-unknown-elf -march=rv32i_zicsr -mabi=ilp32``) y un entorno propio que sustituye a ``env/p``
(``benchmarks/rv32_arranque/riscv_tests/riscv_test.h`` y ``link.ld``): sin trap handler (los traps llegan en H3),
``RVTEST_PASS`` escribe 0x5555 en SYSCON (apagado) y ``RVTEST_FAIL`` escribe el número de test (``TESTNUM`` = ``gp``)
por el UART y 0x7777 en SYSCON (reinicio). Los binarios se versionan en ``benchmarks/rv32_arranque/bin/`` con su
sha256 (``riscv_tests_<nombre>_rv32i_zicsr.bin``); las fuentes de riscv-tests no se versionan. Lo que clang no
ensamble se documenta con el primer error literal.

Cada test se ejecuta entero (parada ``apagado`` sin salida por el UART = paso; ``reinicio`` con el número de test en
el UART = fallo; cualquier otra parada se declara) y fragmentado con K cortes uniformes y K sesgados, con snapshot y
restauración entre cortes en una máquina nueva o usada (arnés de E4, ``benchmarks/benchmark_rv32_invariante.py``):
el veredicto y el estado final deben ser los de la ejecución entera. El experimento de mutantes de restauración
errónea (P2.2) vive en ``benchmarks/benchmark_rv32_mutantes.py``.

Uso (desde la raíz del repositorio):
    python -m benchmarks.benchmark_rv32_riscv_tests [--cortes 64] [--semilla 5] [--json informe.json]
    python -m benchmarks.benchmark_rv32_riscv_tests --construir <riscv-tests> [--clang clang] [--sin-instalar]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from benchmarks.benchmark_rv32_invariante import verify_program
from cpu_digital.rv32_hart import Stop
from cpu_digital.rv32_platform import Machine

ROOT = Path(__file__).resolve().parents[1]
PORT_DIR = ROOT / "benchmarks" / "rv32_arranque" / "riscv_tests"
BIN_DIR = ROOT / "benchmarks" / "rv32_arranque" / "bin"
SUMS = BIN_DIR / "SHA256SUMS"
ARCH = "rv32i_zicsr"
RISCV_TESTS_COMMIT = "d7e86f701b929b5d801f8cda1a4871ab05f452bb"   # master de riscv-software-src/riscv-tests, 2026-10-19
RISCV_TESTS_ZIP_SHA256 = "efbca84cea4cfd0ce8e2e1af143bae246df6f7982ac894ac84e537d371081634"   # zip de codeload.github.com
PREFIX = "riscv_tests_"
# isa/rv32ui de riscv-tests (los 42 de la extensión I sin privilegios; fence_i, ma_data, ld_st y st_ld incluidos).
TESTS = ("add", "addi", "and", "andi", "auipc", "beq", "bge", "bgeu", "blt", "bltu", "bne", "fence_i", "jal", "jalr", "ld_st",
         "lb", "lbu", "lh", "lhu", "lui", "lw", "ma_data", "or", "ori", "sb", "sh", "simple", "sll", "slli", "slt", "slti",
         "sltiu", "sltu", "sra", "srai", "srl", "srli", "st_ld", "sub", "sw", "xor", "xori")
RAM = 1 << 16
GAS = 200_000


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True, capture_output=True, **kwargs)


def build(clang: str, repo: Path, build_dir: Path) -> dict[str, Path | str]:
    """Ensambla cada test; devuelve la ruta del .bin o el primer error literal de clang."""
    build_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, Path | str] = {}
    for name in TESTS:
        source = repo / "isa" / "rv32ui" / f"{name}.S"
        if not source.is_file():
            results[name] = f"no existe {source}"
            continue
        elf = build_dir / f"{PREFIX}{name}_{ARCH}.elf"
        binary = build_dir / f"{PREFIX}{name}_{ARCH}.bin"
        command = [clang, "--target=riscv32-unknown-elf", f"-march={ARCH}", "-mabi=ilp32", "-nostdlib", "-ffreestanding",
                   "-static", f"-I{PORT_DIR}", f"-I{repo / 'isa' / 'macros' / 'scalar'}", f"-I{repo / 'env'}",
                   "-fuse-ld=lld", "-T", str(PORT_DIR / "link.ld"), "-o", str(elf), str(source)]
        try:
            run(command)
            run(["llvm-objcopy", "-O", "binary", str(elf), str(binary)])
            results[name] = binary
        except subprocess.CalledProcessError as error:
            first = next((line for line in error.stderr.splitlines() if "error:" in line), (error.stderr.strip().splitlines() or ["?"])[0])
            results[name] = f"error: {first[:200]}"
    return results


def install(results: dict[str, Path | str]) -> list[str]:
    installed = []
    lines = [line for line in SUMS.read_text(encoding="utf-8").splitlines() if line and PREFIX not in line] if SUMS.exists() else []
    for name, result in results.items():
        if isinstance(result, Path):
            target = BIN_DIR / result.name
            shutil.copyfile(result, target)
            lines.append(f"{hashlib.sha256(target.read_bytes()).hexdigest()} *{target.name}")
            installed.append(name)
    SUMS.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return installed


def versioned() -> dict[str, Path]:
    """Binarios de riscv-tests versionados cuyo sha256 coincide con SHA256SUMS."""
    expected = {}
    for line in SUMS.read_text(encoding="utf-8").splitlines():
        if PREFIX in line:
            digest, _, filename = line.partition(" *")
            expected[filename] = digest
    binaries = {}
    for filename, digest in expected.items():
        path = BIN_DIR / filename
        if not path.is_file():
            raise SystemExit(f"falta {path}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise SystemExit(f"sha256 distinto en {path}")
        binaries[re.sub(rf"^{PREFIX}(.*)_{ARCH}\.bin$", r"\1", filename)] = path
    return binaries


def verdict(stop: Stop, output: bytes) -> tuple[str, int | None]:
    """(``pass`` | ``fail`` | otra parada, número de test fallado)."""
    if stop == Stop("apagado") and not output:
        return "pass", None
    if stop == Stop("reinicio") and output:
        return "fail", output[-1]
    return f"parada {stop.reason}" + (f" causa {stop.cause} valor {stop.value:#x}" if stop.cause >= 0 else ""), None


def run_whole(image: bytes, ram: int = RAM, gas: int = GAS) -> dict[str, object]:
    machine = Machine(ram_bytes=ram)
    try:
        machine.load_flat(image)
        started = time.perf_counter()
        stop = machine.run(gas)
        elapsed = time.perf_counter() - started
        outcome, number = verdict(stop, bytes(machine.uart.output))
        return {"veredicto": outcome, "test_fallado": number, "instrucciones": machine.hart.instret, "segundos": elapsed,
                "hash": machine.state_hash()}
    finally:
        machine.close()


def run_fragmented(name: str, image: bytes, cuts: int, rng: random.Random, ram: int = RAM, gas: int = GAS) -> dict[str, object]:
    """Entero frente a fragmentado con K cortes uniformes y K sesgados (arnés de E4): divergencias y veredictos."""
    result = verify_program(name, image, ram, gas, cuts, rng, None, step=1, bytes_every=4, check_every=4)
    return {"divergencias": result["divergencias"], "instrucciones": result["instrucciones"], "parada": result["parada"],
            "ventanas": result["ventanas"], "planes": {plan: {"cortes": p["cortes"], "cortes_en_ventana": p["cortes_en_ventana"],
                                                                "modos": {m: {"divergencias": v["n_divergencias"],
                                                                              "cortes_alcanzados": v["cortes_alcanzados"],
                                                                              "ms_por_corte": v["ms_por_corte"]}
                                                                          for m, v in p["modos"].items()}}
                                                       for plan, p in result["planes"].items()}}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(description="riscv-tests rv32ui sobre el hart RV32, enteros y fragmentados (E5)")
    parser.add_argument("--construir", type=Path, default=None, help="raíz de riscv-tests ya extraída")
    parser.add_argument("--clang", default="clang")
    parser.add_argument("--sin-instalar", action="store_true", help="ejecuta lo recién ensamblado sin tocar bin/")
    parser.add_argument("--cortes", type=int, default=64)
    parser.add_argument("--semilla", type=int, default=5)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    if args.cortes < 1:
        parser.error("cortes debe ser positivo")
    print(f"Python {platform.python_version()} ({platform.system()} {platform.machine()}); riscv-tests rv32ui; {ARCH}; "
          f"cortes por plan: {args.cortes}")
    report: dict[str, object] = {"python": platform.python_version(), "arch": ARCH, "cortes": args.cortes, "semilla": args.semilla}
    binaries: dict[str, Path] | None = None
    if args.construir is not None:
        clang = shutil.which(args.clang)
        if clang is None:
            print(f"No se encuentra {args.clang}", file=sys.stderr)
            return 2
        build_dir = Path(tempfile.gettempdir()) / "rv32_riscv_tests_build"
        results = build(clang, args.construir, build_dir)
        excluded = {name: reason for name, reason in results.items() if not isinstance(reason, Path)}
        if args.sin_instalar:
            binaries = {name: path for name, path in results.items() if isinstance(path, Path)}
            print(f"ensamblados (sin instalar): {len(binaries)}")
        else:
            installed = install(results)
            print(f"ensamblados e instalados en bin/: {len(installed)}")
        for name, reason in excluded.items():
            print(f"  fuera: {name}: {reason}")
        report["excluidos"] = excluded
    if binaries is None:
        binaries = versioned()
    rng = random.Random(args.semilla)
    rows: dict[str, dict[str, object]] = {}
    passes = fails = others = divergences = 0
    for name, path in sorted(binaries.items()):
        image = path.read_bytes()
        whole = run_whole(image)
        fragmented = run_fragmented(name, image, args.cortes, rng)
        rows[name] = {"sha256": hashlib.sha256(image).hexdigest()[:16], "bytes": len(image), "entero": whole, "fragmentado": fragmented}
        passes += whole["veredicto"] == "pass"
        fails += whole["veredicto"] == "fail"
        others += whole["veredicto"] not in ("pass", "fail")
        divergences += int(fragmented["divergencias"])
        biased = fragmented["planes"]["sesgados"]
        print(f"{name:10s} {whole['veredicto']:18s} {whole['instrucciones']:>7} instr  ventanas {fragmented['ventanas']:>4}  "
              f"cortes {fragmented['planes']['uniformes']['cortes']}/{biased['cortes']} (sesgados en ventana {biased['cortes_en_ventana']})  "
              f"divergencias {fragmented['divergencias']}")
    report["tests"] = rows
    report["resumen"] = {"pasan": passes, "fallan": fails, "otras_paradas": others, "divergencias": divergences, "total": len(rows)}
    print(f"\n{len(rows)} tests: pasan {passes}, fallan {fails}, otras paradas {others}; divergencias fragmentado/entero: {divergences}")
    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return 0 if fails == 0 and others == 0 and divergences == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
