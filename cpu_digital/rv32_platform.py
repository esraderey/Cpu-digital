"""Plataforma mínima del hart RV32 (RFC-EXP-00018, P1.11): bus, UART, SYSCON, CLINT y máquina.

El mapa es el de mini-rv32ima, que es el que arranca Linux nommu y el que usan los binarios de
CoreMark versionados: RAM desde 0x80000000, UART 8250 en 0x10000000 (registro de datos en +0,
LSR en +5 con el transmisor siempre libre y el bit de dato recibido), SYSCON en 0x11100000
(0x5555 apaga, 0x7777 reinicia) y CLINT en 0x11000000 (``mtimecmp`` en +0x4000, ``mtime`` en
+0xBFF8, derivado de ``instret`` del hart). Toda dirección sin dispositivo es un fallo de acceso.
Las capacidades como ventanas MMIO (P1.9) y las interrupciones del CLINT (H3) se añaden sobre
este bus sin cambiar el hart.
"""

from __future__ import annotations

from collections import deque

from .rv32_hart import HostStop, Rv32Hart, Stop
from .rv32_mem import DEFAULT_BASE, AccessFault, Rv32Memory

UART_BASE = 0x10000000
UART_SIZE = 0x100
SYSCON_BASE = 0x11100000
SYSCON_SIZE = 0x4
CLINT_BASE = 0x11000000
CLINT_SIZE = 0x10000
CLINT_MTIMECMP = 0x4000
CLINT_MTIME = 0xBFF8
SYSCON_POWEROFF = 0x5555
SYSCON_REBOOT = 0x7777
MASK32 = 0xFFFFFFFF
_WIDTH_MASK = {1: 0xFF, 2: 0xFFFF, 4: MASK32}


class Uart:
    """8250 mínimo: lo que escriba el invitado sale por ``output``; ``feed`` encola entrada."""

    __slots__ = ("output", "input")

    def __init__(self) -> None:
        self.output = bytearray()
        self.input: deque[int] = deque()

    def load(self, offset: int, width: int) -> int:
        if offset == 0:
            return self.input.popleft() if self.input else 0
        if offset == 5:
            return 0x60 | (1 if self.input else 0)  # THRE y TEMT siempre; DR si hay entrada
        return 0

    def store(self, offset: int, width: int, value: int) -> None:
        if offset == 0:
            self.output.append(value & 0xFF)

    def feed(self, data: bytes | str) -> None:
        if isinstance(data, str):
            data = data.encode("latin-1")
        self.input.extend(data)

    def reset(self) -> None:
        self.output.clear()
        self.input.clear()

    @property
    def text(self) -> str:
        return self.output.decode("latin-1")


class Syscon:
    __slots__ = ()

    def load(self, offset: int, width: int) -> int:
        return 0

    def store(self, offset: int, width: int, value: int) -> None:
        if offset != 0:
            return
        if (value & MASK32) == SYSCON_POWEROFF:
            raise HostStop("apagado")
        if (value & MASK32) == SYSCON_REBOOT:
            raise HostStop("reinicio")


class Clint:
    """``mtime`` = ``instret`` del hart // el divisor **del hart** (un solo reloj para ``rdtime`` y el CLINT);
    ``mtimecmp`` se guarda (las interrupciones llegan en H3). Los dos registros de 64 bits admiten accesos
    de 1, 2 y 4 bytes en cualquier desplazamiento interior; fuera de ellos se lee 0 y se ignora."""

    __slots__ = ("hart", "mtimecmp")

    def __init__(self, hart: Rv32Hart) -> None:
        self.hart = hart
        self.mtimecmp = 0

    @property
    def time_divisor(self) -> int:
        return self.hart.time_divisor

    @property
    def mtime(self) -> int:
        return self.hart.instret // self.hart.time_divisor

    def reset(self) -> None:
        self.mtimecmp = 0

    def load(self, offset: int, width: int) -> int:
        if CLINT_MTIME <= offset < CLINT_MTIME + 8:
            return (self.mtime >> (8 * (offset - CLINT_MTIME))) & _WIDTH_MASK[width]
        if CLINT_MTIMECMP <= offset < CLINT_MTIMECMP + 8:
            return (self.mtimecmp >> (8 * (offset - CLINT_MTIMECMP))) & _WIDTH_MASK[width]
        return 0

    def store(self, offset: int, width: int, value: int) -> None:
        if CLINT_MTIMECMP <= offset < CLINT_MTIMECMP + 8:
            shift = 8 * (offset - CLINT_MTIMECMP)
            mask = _WIDTH_MASK[width] << shift
            self.mtimecmp = (self.mtimecmp & ~mask | (value & _WIDTH_MASK[width]) << shift) & 0xFFFFFFFFFFFFFFFF


class Bus:
    """Despacha los accesos que no caen en la RAM a los dispositivos por ventana de direcciones.

    El bus recorta al ancho del acceso en los dos sentidos: lo que devuelve un dispositivo se
    enmascara a 8, 16 o 32 bits antes de que el hart extienda el signo, y lo que escribe el
    invitado llega al dispositivo ya recortado. Un acceso debe caber entero en una ventana: si
    sobresale por el final, es un fallo de acceso con la dirección inicial. Una ventana que solape
    la RAM queda a la sombra de esta (el hart resuelve la RAM antes que el bus).
    """

    __slots__ = ("windows",)

    def __init__(self) -> None:
        self.windows: list[tuple[int, int, object]] = []

    def attach(self, base: int, size: int, device: object) -> None:
        if size <= 0 or base < 0 or base + size > (1 << 32):
            raise ValueError("La ventana MMIO debe tener tamaño positivo y caber en 32 bits")
        for other_base, other_size, _ in self.windows:
            if base < other_base + other_size and other_base < base + size:
                raise ValueError("Las ventanas MMIO no pueden solaparse")
        self.windows.append((base, size, device))

    def load(self, address: int, width: int) -> int:
        for base, size, device in self.windows:
            if base <= address and address + width <= base + size:
                return device.load(address - base, width) & _WIDTH_MASK[width]
        raise AccessFault(address)

    def store(self, address: int, width: int, value: int) -> None:
        for base, size, device in self.windows:
            if base <= address and address + width <= base + size:
                device.store(address - base, width, value & _WIDTH_MASK[width])
                return
        raise AccessFault(address)


class Machine:
    """RAM, bus con UART/SYSCON/CLINT y un hart; carga binarios planos y ejecuta con gas."""

    __slots__ = ("memory", "bus", "hart", "uart", "syscon", "clint")

    def __init__(self, ram_bytes: int = 64 << 20, base: int = DEFAULT_BASE, time_divisor: int = 1) -> None:
        self.memory = Rv32Memory(ram_bytes, base)
        self.bus = Bus()
        self.hart = Rv32Hart(self.memory, self.bus, time_divisor)   # el CLINT toma el reloj del hart
        self.uart = Uart()
        self.syscon = Syscon()
        self.clint = Clint(self.hart)
        self.bus.attach(UART_BASE, UART_SIZE, self.uart)
        self.bus.attach(SYSCON_BASE, SYSCON_SIZE, self.syscon)
        self.bus.attach(CLINT_BASE, CLINT_SIZE, self.clint)

    def load_flat(self, image: bytes | bytearray | memoryview, address: int | None = None) -> None:
        """Deja la máquina limpia (RAM a cero, UART y ``mtimecmp`` vacíos, hart a cero), carga un binario
        plano (``llvm-objcopy -O binary``) y apunta el pc a su inicio. Si no cabe, ``AccessFault`` sin cargar."""
        start = self.memory.base if address is None else address
        if not 0 <= start - self.memory.base <= self.memory.size - memoryview(image).nbytes:
            raise AccessFault(start)   # bytes, no elementos: un array('I') cuenta 4 por elemento
        self.memory.reset()
        self.uart.reset()
        self.clint.reset()
        self.memory.load_bytes(start, image)
        self.hart.reset(start)

    def run(self, gas: int) -> Stop:
        return self.hart.run(gas)

    @property
    def uart_text(self) -> str:
        return self.uart.text

    def close(self) -> None:
        self.memory.close()


__all__ = ["Bus", "CLINT_BASE", "CLINT_MTIME", "CLINT_MTIMECMP", "CLINT_SIZE", "Clint", "Machine", "SYSCON_BASE",
           "SYSCON_SIZE", "Syscon", "UART_BASE", "UART_SIZE", "Uart"]
