"""H3.5 de RFC-EXP-00018: FreeRTOS en el hart con el puerto propio de ``benchmarks/rv32_arranque/freertos``.

El puerto (``FreeRTOSConfig.h``, ``main.c``, ``support.c``/``.h``, ``start.S``, ``link.ld`` y las cabeceras mínimas de
``libc/``) se versiona; las fuentes de FreeRTOS-Kernel no (externas: etiqueta V11.1.0 de github.com/FreeRTOS/FreeRTOS-Kernel,
sha256 del zip ``d5875aca…a298a2``). ``--construir DIR`` compila con clang (``--target=riscv32-unknown-elf
-march=rv32ima_zicsr -mabi=ilp32 -O2``, lld) el núcleo (``tasks.c``, ``queue.c``, ``list.c``), el puerto oficial
``portable/GCC/RISC-V`` con ``RISCV_MTIME_CLINT_no_extensions`` y el puerto propio, y deja el binario plano versionado en
``rv32_arranque/bin/freertos_demo_rv32ima_zicsr.bin`` (con ``--instalar``; su sha256 en ``SHA256SUMS``). Lo que clang no
compile se informa con el error literal.

La demo: un productor manda 1..20 por una cola cada 2 tics (``vTaskDelay``: interrupción del temporizador del CLINT y
cambio de contexto por ``ECALL``), un consumidor los imprime con el tic y, al acabar, escribe ``FreeRTOS ok suma 210`` y
apaga por SYSCON; el gancho ocioso duerme con ``WFI``. Sin ``--construir`` ejecuta el binario versionado en cada modo
(intérprete, bloques con calentamiento 1 y 2): gas e ``instret`` al primer byte del UART y al apagado, tiempo del host,
cobertura de bloques; con ``--cortes K``, E4 (``verify_program`` con K cortes uniformes y K sesgados). No optimiza nada.

Uso (desde la raíz del repositorio):
    python -m benchmarks.benchmark_rv32_freertos [--construir FreeRTOS-Kernel] [--instalar] [--cortes 0] [--json informe.json]
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

from benchmarks.benchmark_rv32_linux import attach_probe
from cpu_digital.rv32_platform import Machine

ROOT = Path(__file__).resolve().parents[1]
PORT = ROOT / "benchmarks" / "rv32_arranque" / "freertos"
BIN_DIR = ROOT / "benchmarks" / "rv32_arranque" / "bin"
BINARY = BIN_DIR / "freertos_demo_rv32ima_zicsr.bin"
SUMS = BIN_DIR / "SHA256SUMS"
KERNEL_ZIP_SHA256 = "d5875acad2479817b35b2031dc1c7c22d100f45c7f3c9f6039cb217437a298a2"   # FreeRTOS-Kernel-V11.1.0.zip
EXPECTED_END = "FreeRTOS ok suma 210\n"
RAM = 1 << 20
GAS = 5_000_000
MODES = {"interprete": (False, 2), "w1": (True, 1), "w2": (True, 2)}


def build(clang: str, kernel: Path, build_dir: Path) -> Path | str:
    """Compila la demo; devuelve el binario plano o el primer error literal de clang/lld."""
    port_asm = kernel / "portable" / "GCC" / "RISC-V"
    sources = [PORT / "start.S", PORT / "main.c", PORT / "support.c", kernel / "tasks.c", kernel / "queue.c", kernel / "list.c",
               port_asm / "port.c", port_asm / "portASM.S"]
    includes = [PORT, PORT / "libc", kernel / "include", port_asm, port_asm / "chip_specific_extensions" / "RISCV_MTIME_CLINT_no_extensions"]
    elf, binary = build_dir / "freertos.elf", build_dir / "freertos.bin"
    command = [clang, "--target=riscv32-unknown-elf", "-march=rv32ima_zicsr", "-mabi=ilp32", "-O2", "-ffreestanding", "-nostdlib",
               *(f"-I{path}" for path in includes), *map(str, sources), "-fuse-ld=lld", "-T", str(PORT / "link.ld"), "-o", str(elf)]
    done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode != 0:
        return next((line for line in done.stderr.splitlines() if "error" in line), done.stderr.strip()[:400])
    objcopy = shutil.which("llvm-objcopy") or "llvm-objcopy"
    subprocess.run([objcopy, "-O", "binary", str(elf), str(binary)], check=True, capture_output=True)
    return binary


def run_demo(image: bytes, accelerate: bool, warmup: int, gas: int = GAS) -> dict[str, object]:
    machine = Machine(ram_bytes=RAM)
    try:
        machine.load_flat(image)
        machine.hart.accelerate, machine.hart.warmup = accelerate, warmup
        probe = attach_probe(machine, stop_at="nunca")
        started = time.perf_counter()
        stop = machine.run(gas)
        seconds = time.perf_counter() - started
        hart = machine.hart
        used = gas - hart.gas
        return {"parada": stop.reason, "gas": used, "instret": hart.instret, "segundos": seconds,
                "primer_byte_instret": probe.marks.get("primer_byte"), "cobertura": hart.block_instructions / max(1, used),
                "compilaciones": hart.compilations, "uart": machine.uart.text}
    finally:
        machine.close()


def versioned_ok() -> bool:
    sums = dict(line.split(" *")[::-1] for line in SUMS.read_text(encoding="utf-8").splitlines() if line.strip())
    return BINARY.is_file() and hashlib.sha256(BINARY.read_bytes()).hexdigest() == sums.get(BINARY.name)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(description="H3.5: FreeRTOS en el hart RV32")
    parser.add_argument("--construir", type=Path, default=None, help="directorio de FreeRTOS-Kernel (V11.1.0)")
    parser.add_argument("--instalar", action="store_true", help="copia el binario construido a rv32_arranque/bin")
    parser.add_argument("--clang", default="clang")
    parser.add_argument("--cortes", type=int, default=0)
    parser.add_argument("--semilla", type=int, default=20261020)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    report: dict[str, object] = {"python": platform.python_version(), "modos": {}}
    if args.construir:
        clang = shutil.which(args.clang)
        if clang is None:
            print(f"No se encuentra {args.clang}", file=sys.stderr)
            return 2
        with tempfile.TemporaryDirectory(prefix="rv32_freertos_") as temporary:
            result = build(clang, args.construir, Path(temporary))
            if isinstance(result, str):
                print(f"clang no compila la demo: {result}")
                report["error"] = result
                image = None
            else:
                image = result.read_bytes()
                print(f"construido: {len(image)} bytes, sha256 {hashlib.sha256(image).hexdigest()}")
                if args.instalar:
                    shutil.copyfile(result, BINARY)
    else:
        if not versioned_ok():
            print(f"{BINARY.name} falta o su sha256 no coincide con SHA256SUMS", file=sys.stderr)
            return 2
        image = BINARY.read_bytes()
    if image is not None:
        print(f"Python {platform.python_version()}; {len(image)} bytes, sha256 {hashlib.sha256(image).hexdigest()[:16]}…")
        for name, (accelerate, warmup) in MODES.items():
            result = run_demo(image, accelerate, warmup)
            report["modos"][name] = result
            print(f"{name:10s} parada {result['parada']}; gas {result['gas']:,}; instret {result['instret']:,}; "
                  f"{result['segundos']:.2f} s; primer byte en instret {result['primer_byte_instret']}; cobertura "
                  f"{result['cobertura']:.1%}; {'salida esperada' if result['uart'].endswith(EXPECTED_END) else 'SALIDA DISTINTA'}")
        if args.cortes:
            from benchmarks.benchmark_rv32_invariante import verify_program
            gas = report["modos"]["interprete"]["gas"] + 1000
            result = verify_program("freertos", image, RAM, gas, args.cortes, random.Random(args.semilla), None, step=1)
            report["e4"] = result
            print(f"E4: {result['divergencias']} divergencias; {result['planes']['sesgados']['cortes']} cortes sesgados "
                  f"({result['planes']['sesgados']['cortes_en_ventana']} en ventana); clases {result['clases_de_ventana']}")
    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
