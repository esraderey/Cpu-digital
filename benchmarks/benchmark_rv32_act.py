"""E5 (ACT) de RFC-EXP-00018: riscv-arch-test 2.7.4, rv32i_m/I, enteros y fragmentados, contra las firmas de referencia
del repositorio (H2.4).

ACT4 (ramas ``main`` y ``act4`` de riscv-non-isa/riscv-arch-test) ya no trae firmas de referencia: las calcula con el
modelo Sail, que no está en la máquina del proyecto. La última versión que sí las trae es la 2.7.4
(``ACT_TAG``, zip con sha256 ``ACT_ZIP_SHA256``): ``riscv-test-suite/rv32i_m/I/src/*.S`` con sus
``references/*.reference_output``. Esos tests se ensamblan con clang y un objetivo propio
(``benchmarks/rv32_arranque/act/model_test.h`` y ``link.ld``: sin trap handler, ``RVMODEL_HALT`` apaga por SYSCON y
la firma queda entre ``begin_signature`` y ``end_signature``, cuyas direcciones se leen del ELF con ``llvm-nm`` y se
guardan junto al binario). El hart ejecuta cada test entero, y su firma (una palabra de 32 bits por línea, en
hexadecimal) debe coincidir con la referencia; fragmentado con K cortes uniformes y K sesgados (arnés de E4) debe dar
el mismo estado final. Se versionan los binarios (``act_<test>_rv32i_zicsr.bin``), las direcciones de la firma y el
sha256 de cada firma de referencia (``benchmarks/rv32_arranque/act/SIGNATURES.sha256``); las fuentes y las
referencias no se versionan. Lo que clang no ensamble o no coincida se documenta.

Uso (desde la raíz del repositorio):
    python -m benchmarks.benchmark_rv32_act [--cortes 64] [--semilla 5] [--json informe.json]
    python -m benchmarks.benchmark_rv32_act --construir <riscv-arch-test-2.7.4> [--clang clang] [--sin-instalar]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import random
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
PORT_DIR = ROOT / "benchmarks" / "rv32_arranque" / "act"
BIN_DIR = ROOT / "benchmarks" / "rv32_arranque" / "bin"
SUMS = BIN_DIR / "SHA256SUMS"
SIGNATURES = PORT_DIR / "SIGNATURES.sha256"   # «<sha256 de la referencia canónica> <test> <begin_signature> <end_signature>»
ARCH = "rv32i_zicsr"
PREFIX = "act_"
ACT_TAG = "2.7.4"
ACT_COMMIT = "2.7.4 (etiqueta de riscv-non-isa/riscv-arch-test)"
ACT_ZIP_SHA256 = "c10cb4db50bdf6aa0e9c1aa2368b590ece7433b4cf631ff666d12c06d0ec61cd"   # zip de codeload.github.com
MIN_RAM = 1 << 20      # los tests de saltos ocupan cientos de KB (jal-01: 1,75 MB): RAM por test, potencia de dos
GAS = 1_000_000


def ram_for(image: bytes) -> int:
    """La menor potencia de dos, desde 1 MiB, que deja 64 KiB libres tras la imagen."""
    ram = MIN_RAM
    while ram < len(image) + (1 << 16):
        ram *= 2
    return ram


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True, capture_output=True, **kwargs)


def signature_symbols(elf: Path) -> tuple[int, int]:
    symbols: dict[str, int] = {}
    for line in run(["llvm-nm", str(elf)]).stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] in ("begin_signature", "end_signature"):
            symbols[parts[2]] = int(parts[0], 16)
    return symbols["begin_signature"], symbols["end_signature"]


def canonical_reference(text: str) -> list[str]:
    return [line.strip().lower() for line in text.splitlines() if line.strip()]


def reference_digest(lines: list[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode("ascii")).hexdigest()


def build(clang: str, suite: Path, build_dir: Path) -> dict[str, tuple[Path, int, int, str] | str]:
    """Ensambla cada test de rv32i_m/I; devuelve (bin, begin, end, sha256 de su referencia) o el primer error literal."""
    build_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, tuple[Path, int, int, str] | str] = {}
    sources = sorted((suite / "rv32i_m" / "I" / "src").glob("*.S"))
    for source in sources:
        name = source.stem
        reference = suite / "rv32i_m" / "I" / "references" / f"{name}.reference_output"
        if not reference.is_file():
            results[name] = "sin firma de referencia en el repositorio"
            continue
        elf = build_dir / f"{PREFIX}{name}_{ARCH}.elf"
        binary = build_dir / f"{PREFIX}{name}_{ARCH}.bin"
        command = [clang, "--target=riscv32-unknown-elf", f"-march={ARCH}", "-mabi=ilp32", "-nostdlib", "-ffreestanding", "-static",
                   "-DXLEN=32", f"-I{PORT_DIR}", f"-I{suite / 'env'}", "-fuse-ld=lld", "-T", str(PORT_DIR / "link.ld"), "-o", str(elf),
                   str(source)]
        try:
            run(command)
            run(["llvm-objcopy", "-O", "binary", str(elf), str(binary)])
            begin, end = signature_symbols(elf)
            results[name] = (binary, begin, end, reference_digest(canonical_reference(reference.read_text(encoding="utf-8"))))
        except (subprocess.CalledProcessError, KeyError) as error:
            stderr = getattr(error, "stderr", "") or repr(error)
            first = next((line for line in stderr.splitlines() if "error:" in line), (stderr.strip().splitlines() or ["?"])[0])
            results[name] = f"error: {first[:200]}"
    return results


def install(results: dict[str, tuple[Path, int, int, str] | str]) -> list[str]:
    installed = []
    lines = [line for line in SUMS.read_text(encoding="utf-8").splitlines() if line and PREFIX not in line] if SUMS.exists() else []
    signatures = []
    for name, result in sorted(results.items()):
        if isinstance(result, tuple):
            binary, begin, end, digest = result
            target = BIN_DIR / binary.name
            shutil.copyfile(binary, target)
            lines.append(f"{hashlib.sha256(target.read_bytes()).hexdigest()} *{target.name}")
            signatures.append(f"{digest} {name} {begin:#x} {end:#x}")
            installed.append(name)
    SUMS.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    SIGNATURES.write_text("\n".join(signatures) + "\n", encoding="utf-8", newline="\n")
    return installed


def versioned() -> dict[str, tuple[Path, int, int, str]]:
    """Binarios de ACT versionados con su sha256 verificado, las direcciones de la firma y el sha256 de la referencia."""
    expected = {}
    for line in SUMS.read_text(encoding="utf-8").splitlines():
        if PREFIX in line:
            digest, _, filename = line.partition(" *")
            expected[filename] = digest
    signatures = {}
    for line in SIGNATURES.read_text(encoding="utf-8").splitlines():
        digest, name, begin, end = line.split()
        signatures[name] = (digest, int(begin, 16), int(end, 16))
    binaries = {}
    for filename, digest in expected.items():
        path = BIN_DIR / filename
        if not path.is_file():
            raise SystemExit(f"falta {path}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise SystemExit(f"sha256 distinto en {path}")
        name = filename[len(PREFIX):-len(f"_{ARCH}.bin")]
        reference, begin, end = signatures[name]
        binaries[name] = (path, begin, end, reference)
    return binaries


def signature_of(machine: Machine, begin: int, end: int) -> list[str]:
    raw = machine.memory.read_bytes(begin, end - begin)
    return [f"{int.from_bytes(raw[i:i + 4], 'little'):08x}" for i in range(0, len(raw), 4)]


def run_whole(image: bytes, begin: int, end: int, reference: str, ram: int | None = None, gas: int = GAS) -> dict[str, object]:
    machine = Machine(ram_bytes=ram or ram_for(image))
    try:
        machine.load_flat(image)
        started = time.perf_counter()
        stop = machine.run(gas)
        elapsed = time.perf_counter() - started
        signature = signature_of(machine, begin, end)
        digest = reference_digest(signature)
        halted = stop == Stop("apagado") and not machine.uart.output
        return {"parada": stop.reason, "apagado": halted, "firma_igual": digest == reference, "palabras": len(signature),
                "instrucciones": machine.hart.instret, "segundos": elapsed, "hash": machine.state_hash()}
    finally:
        machine.close()


def run_fragmented(name: str, image: bytes, cuts: int, rng: random.Random, ram: int | None = None, gas: int = GAS) -> dict[str, object]:
    result = verify_program(name, image, ram or ram_for(image), gas, cuts, rng, None, step=1, bytes_every=8, check_every=8)
    return {"divergencias": result["divergencias"], "instrucciones": result["instrucciones"], "parada": result["parada"],
            "ventanas": result["ventanas"],
            "planes": {plan: {"cortes": p["cortes"], "cortes_en_ventana": p["cortes_en_ventana"],
                              "modos": {m: {"divergencias": v["n_divergencias"], "cortes_alcanzados": v["cortes_alcanzados"],
                                            "ms_por_corte": v["ms_por_corte"]} for m, v in p["modos"].items()}}
                       for plan, p in result["planes"].items()}}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(description="riscv-arch-test 2.7.4 rv32i_m/I sobre el hart RV32, enteros y fragmentados")
    parser.add_argument("--construir", type=Path, default=None, help="raíz de riscv-arch-test-2.7.4 ya extraída (contiene riscv-test-suite/)")
    parser.add_argument("--clang", default="clang")
    parser.add_argument("--sin-instalar", action="store_true")
    parser.add_argument("--cortes", type=int, default=64)
    parser.add_argument("--semilla", type=int, default=5)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    if args.cortes < 1:
        parser.error("cortes debe ser positivo")
    print(f"Python {platform.python_version()} ({platform.system()} {platform.machine()}); riscv-arch-test {ACT_TAG} rv32i_m/I; {ARCH}; "
          f"cortes por plan: {args.cortes}")
    report: dict[str, object] = {"python": platform.python_version(), "act": ACT_TAG, "zip_sha256": ACT_ZIP_SHA256, "arch": ARCH,
                                 "cortes": args.cortes, "semilla": args.semilla}
    binaries: dict[str, tuple[Path, int, int, str]] | None = None
    if args.construir is not None:
        clang = shutil.which(args.clang)
        if clang is None:
            print(f"No se encuentra {args.clang}", file=sys.stderr)
            return 2
        results = build(clang, args.construir / "riscv-test-suite", Path(tempfile.gettempdir()) / "rv32_act_build")
        excluded = {name: reason for name, reason in results.items() if not isinstance(reason, tuple)}
        if args.sin_instalar:
            binaries = {name: value for name, value in results.items() if isinstance(value, tuple)}
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
    matches = divergences = 0
    for name, (path, begin, end, reference) in sorted(binaries.items()):
        image = path.read_bytes()
        whole = run_whole(image, begin, end, reference)
        fragmented = run_fragmented(name, image, args.cortes, rng)
        rows[name] = {"sha256": hashlib.sha256(image).hexdigest()[:16], "bytes": len(image), "entero": whole, "fragmentado": fragmented}
        ok = whole["apagado"] and whole["firma_igual"]
        matches += ok
        divergences += int(fragmented["divergencias"])
        biased = fragmented["planes"]["sesgados"]
        print(f"{name:16s} {'firma igual' if ok else 'FIRMA DISTINTA o sin apagar':28s} {whole['instrucciones']:>7} instr  "
              f"{whole['palabras']:>4} palabras  ventanas {fragmented['ventanas']:>3}  cortes {fragmented['planes']['uniformes']['cortes']}/"
              f"{biased['cortes']} (sesgados en ventana {biased['cortes_en_ventana']})  divergencias {fragmented['divergencias']}")
    report["tests"] = rows
    report["resumen"] = {"firmas_iguales": matches, "total": len(rows), "divergencias": divergences}
    print(f"\n{len(rows)} tests: firmas iguales {matches}; divergencias fragmentado/entero: {divergences}")
    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return 0 if matches == len(rows) and divergences == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
