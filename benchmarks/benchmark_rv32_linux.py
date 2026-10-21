"""E1 en el hart (H3.5 de RFC-EXP-00018): arrancar la imagen de Linux nommu de E1 con el DTB de la plataforma.

Carga la imagen ``linux-6.1.14-rv32nommu-cnl-1`` (``Image`` descomprimido del zip de cnlohr/mini-rv32ima-images, sha256
``benchmark_rv32_arranque.IMAGE_SHA256``; fuente externa, no versionada, por argumento) en una ``Machine`` de 64 MiB con el
DTB de ``rv32_dtb`` (``a0`` = 0, ``a1`` = DTB) y la ejecuta hasta el prompt del shell: una sonda en la ventana del UART
teclea ``root`` en el instante en que el invitado escribe ``login:`` (como el arnés de E1 con mini-rv32ima) y para la
máquina con ``HostStop("prompt")`` en la escritura que completa ``# `` tras el login, así que las cifras son exactas e
independientes del tamaño de los trozos de gas. La sonda es del arnés (el bus no entra en el snapshot): no cambia el hart.

Imprime, para cada modo (intérprete, bloques con calentamiento 1 y 2): ``instret`` y gas al primer ``printk`` (primer byte
del UART), al ``login:`` y al prompt; el tiempo del host; la cobertura de bloques; las compilaciones; y las cifras frente a
E1 (64 929 979 instrucciones hasta el shell en mini-rv32ima) y frente a la hoja de ruta (15–60 s). ``instret`` incluye el
reloj que ``WFI`` adelanta sin ejecutar (P1.3: el tiempo ocioso no cobra gas); el gas es lo que el hart ejecutó (las
entradas en trap cobran 1). Con ``--cortes K`` pasa además E4 sobre el arranque hasta ``login:`` (``verify_program`` de
``benchmark_rv32_invariante`` con K cortes uniformes y K sesgados). **No optimiza nada**: mide.

Uso (desde la raíz del repositorio):
    python -m benchmarks.benchmark_rv32_linux --imagen Image [--modos interprete,w1,w2] [--cortes 0] [--json informe.json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import random
import sys
import time
from pathlib import Path

from benchmarks.benchmark_rv32_arranque import IMAGE_SHA256
from cpu_digital.rv32_dtb import boot, machine_dtb
from cpu_digital.rv32_hart import HostStop, Stop
from cpu_digital.rv32_platform import UART_BASE, Machine

RAM = 64 << 20
E1_INSTRUCTIONS = 64_929_979          # mini-rv32ima instrumentado, hasta «~ #» (H0)
H3_LINUX_MAX = 1_000_000_000          # E1: por encima, la meta de H3.5 baja a Zephyr y FreeRTOS
CHUNK = 5_000_000
CAP = 2_000_000_000
MODES = {"interprete": (False, 2), "w1": (True, 1), "w2": (True, 2)}


class UartProbe:
    """Envoltorio de la ventana del UART: reenvía todo al UART real y anota ``instret`` (de la instrucción que escribe) al
    primer byte, al ``login:`` (y teclea ``root``) y al prompt ``# `` tras el login, donde para con ``HostStop``."""

    __slots__ = ("uart", "hart", "marks", "logged_in", "stop_at")

    def __init__(self, uart, hart, stop_at: str = "prompt") -> None:
        self.uart, self.hart, self.stop_at = uart, hart, stop_at
        self.marks: dict[str, int] = {}
        self.logged_in = False

    def load(self, offset: int, width: int) -> int:
        return self.uart.load(offset, width)

    def store(self, offset: int, width: int, value: int) -> None:
        self.uart.store(offset, width, value)
        if offset != 0:
            return
        output = self.uart.output
        now = self.hart.instret + 1                       # la escritura se confirma: cuenta la propia instrucción
        self.marks.setdefault("primer_byte", now)
        if not self.logged_in and output.endswith(b"login: "):
            self.logged_in = True
            self.marks["login"] = now
            self.uart.feed(b"root\n")
            if self.stop_at == "login":
                raise HostStop("login")
        elif self.logged_in and output.endswith(b"# "):
            self.marks["prompt"] = now
            raise HostStop("prompt")


def attach_probe(machine: Machine, stop_at: str = "prompt") -> UartProbe:
    probe = UartProbe(machine.uart, machine.hart, stop_at)
    machine.bus.windows[:] = [(base, size, probe if base == UART_BASE else device) for base, size, device in machine.bus.windows]
    return probe


def boot_linux(image: bytes, accelerate: bool, warmup: int, stop_at: str = "prompt", cap: int = CAP) -> dict[str, object]:
    machine = Machine(ram_bytes=RAM)
    try:
        dtb_address = boot(machine, image)
        machine.hart.accelerate, machine.hart.warmup = accelerate, warmup
        probe = attach_probe(machine, stop_at)
        gas_used = 0
        stop = Stop("gas")
        started = time.perf_counter()
        while gas_used < cap and stop.reason == "gas":
            stop = machine.run(CHUNK)
            gas_used += CHUNK - machine.hart.gas
        seconds = time.perf_counter() - started
        hart = machine.hart
        # Cobertura sobre el gas (lo ejecutado más las entradas en trap, una fracción ínfima); instret incluye el sueño de WFI.
        return {"parada": stop.reason, "instret": hart.instret, "gas": gas_used, "segundos": seconds,
                "marcas_instret": dict(probe.marks), "cobertura": hart.block_instructions / max(1, gas_used),
                "compilaciones": hart.compilations, "dtb": dtb_address, "uart_bytes": len(machine.uart.output),
                "uart_cola": machine.uart.text[-400:]}
    finally:
        machine.close()


def boot_image(image: bytes) -> tuple[bytes, dict[int, int]]:
    """Imagen plana con el DTB en su sitio (para ``verify_program``, que carga con ``load_flat``) y los registros a0/a1."""
    machine = Machine(ram_bytes=RAM)
    try:
        address = boot(machine, image)
        dtb = machine_dtb(machine)
    finally:
        machine.close()
    flat = bytearray(address - 0x80000000 + len(dtb))
    flat[:len(image)] = image
    flat[address - 0x80000000:] = dtb
    return bytes(flat), {10: 0, 11: address}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(description="E1 en el hart: Linux nommu con el DTB de la plataforma")
    parser.add_argument("--imagen", type=Path, required=True, help="Image de linux-6.1.14-rv32nommu-cnl-1")
    parser.add_argument("--modos", default="interprete,w1,w2")
    parser.add_argument("--cortes", type=int, default=0, help="K cortes por plan para E4 sobre el arranque hasta login:")
    parser.add_argument("--semilla", type=int, default=20261020)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    image = args.imagen.read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    print(f"Python {platform.python_version()} ({platform.system()} {platform.machine()}); imagen {args.imagen.name} "
          f"sha256 {digest[:16]}… ({'la de E1' if digest == IMAGE_SHA256 else 'DISTINTA de la de E1'}); RAM {RAM >> 20} MiB")
    report: dict[str, object] = {"python": platform.python_version(), "imagen_sha256": digest, "imagen_e1": digest == IMAGE_SHA256,
                                 "modos": {}}
    for name in args.modos.split(","):
        accelerate, warmup = MODES[name]
        result = boot_linux(image, accelerate, warmup)
        report["modos"][name] = result
        marks = result["marcas_instret"]
        print(f"{name:10s} parada {result['parada']}; instret {result['instret']:,}; gas {result['gas']:,}; "
              f"{result['segundos']:.1f} s; primer byte en {marks.get('primer_byte', 'n/d')}, login: en {marks.get('login', 'n/d')}, "
              f"prompt en {marks.get('prompt', 'n/d')}; cobertura {result['cobertura']:.1%}; compilaciones {result['compilaciones']}")
    first = next(iter(report["modos"].values()))
    prompt = first["marcas_instret"].get("prompt")
    if prompt is not None:
        print(f"Frente a E1: {first['gas']:,} de gas y {prompt:,} de instret hasta el shell frente a {E1_INSTRUCTIONS:,} "
              f"({first['gas'] / E1_INSTRUCTIONS:.2f}× en gas); umbral de H3 {H3_LINUX_MAX:,}: "
              f"{'por debajo' if first['gas'] <= H3_LINUX_MAX else 'POR ENCIMA: la meta baja a Zephyr y FreeRTOS'}")
    if args.cortes:
        from benchmarks.benchmark_rv32_invariante import verify_program
        login = boot_linux(image, False, 2, stop_at="login")
        flat, regs = boot_image(image)
        gas = login["gas"]                                       # hasta la escritura que completa «login: »
        result = verify_program("linux-hasta-login", flat, RAM, gas, args.cortes, random.Random(args.semilla), regs, step=4096)
        report["e4"] = result
        print(f"E4 sobre el arranque hasta login: ({gas:,} de gas): {result['divergencias']} divergencias; "
              f"ventanas {result['ventanas']}; clases {result['clases_de_ventana']}")
    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
