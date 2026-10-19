"""Plataforma mínima del hart RV32 (RFC-EXP-00018, P1.11): bus, UART, SYSCON, CLINT y máquina.

El mapa es el de mini-rv32ima, que es el que arranca Linux nommu y el que usan los binarios de
CoreMark versionados: RAM desde 0x80000000, UART 8250 en 0x10000000 (registro de datos en +0,
LSR en +5 con el transmisor siempre libre y el bit de dato recibido), SYSCON en 0x11100000
(0x5555 apaga, 0x7777 reinicia) y CLINT en 0x11000000 (``mtimecmp`` en +0x4000, ``mtime`` en
+0xBFF8, derivado de ``instret`` del hart). Toda dirección sin dispositivo es un fallo de acceso.
Las capacidades como ventanas MMIO (P1.9) y las interrupciones del CLINT (H3) se añaden sobre
este bus sin cambiar el hart.

Snapshot (H2.1): ``Machine.snapshot()`` es el del hart más ``"uart": {"output": [bytes], "input":
[bytes]}`` y ``"clint": {"mtimecmp": entero de 64 bits}``; ``restore`` valida los dispositivos sin
tocarlos, restaura el hart (atómico) y solo entonces confirma UART y CLINT con pasos que no pueden
fallar (la salida del UART se sustituye por un ``bytearray`` nuevo: una vista que el host tuviera sobre
el anterior se queda con el anterior); ``from_snapshot_bytes`` crea una máquina con la geometría de RAM
del snapshot (lo único de configuración que adopta de él; el divisor de tiempo es estado y se adopta en
cualquier restauración) y la configuración predeterminada. ``load_flat`` devuelve el divisor al valor con
que se construyó la máquina. Cada clase declara su inventario de estado (P2.5); un dispositivo nuevo con
estado tendría que entrar en ``Machine.snapshot`` y en el inventario, porque el bus no entra en el snapshot.
"""

from __future__ import annotations

import hashlib
from collections import deque

from .rv32_hart import (
    MASK64,
    HostStop,
    Rv32Hart,
    Stop,
    canonical_json,
    decode_snapshot,
    encode_snapshot,
    expect_keys,
)
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
_MACHINE_KEYS = frozenset({"version", "hart", "ram", "uart", "clint"})
_UART_KEYS = frozenset({"output", "input"})
_CLINT_KEYS = frozenset({"mtimecmp"})
DEFAULT_MAX_SNAPSHOT_RAM = 64 << 20   # from_snapshot_bytes no reserva más RAM que esto sin permiso explícito


def _byte_list(value: object, name: str) -> list[int]:
    items = list(value) if isinstance(value, list) else None   # copia antes de validar: lo validado es lo que se usa
    if items is None or not all(type(item) is int and 0 <= item <= 255 for item in items):
        raise ValueError(f"{name} inválida en el snapshot")
    return items


class Uart:
    """8250 mínimo: lo que escriba el invitado sale por ``output``; ``feed`` encola entrada."""

    __slots__ = ("output", "input")
    STATE_FIELDS = ("output", "input")   # inventario (P2.5): snapshot()["uart"]
    CONFIG_FIELDS = ()
    DERIVED_FIELDS: dict[str, str] = {}
    STATS_FIELDS = ()
    HARNESS_FIELDS = ()

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
        self.output = bytearray()   # objeto nuevo: una vista del host sobre el anterior no impide reiniciar (ni deja la máquina a medias)
        self.input.clear()

    @property
    def text(self) -> str:
        return self.output.decode("latin-1")


class Syscon:
    __slots__ = ()
    STATE_FIELDS = ()
    CONFIG_FIELDS = ()
    DERIVED_FIELDS: dict[str, str] = {}
    STATS_FIELDS = ()
    HARNESS_FIELDS = ()

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
    STATE_FIELDS = ("mtimecmp",)   # inventario (P2.5): snapshot()["clint"]; mtime se deriva del hart
    CONFIG_FIELDS = ("hart",)
    DERIVED_FIELDS: dict[str, str] = {}
    STATS_FIELDS = ()
    HARNESS_FIELDS = ()

    def __init__(self, hart: Rv32Hart) -> None:
        self.hart = hart
        self.mtimecmp = 0

    @property
    def time_divisor(self) -> int:
        return self.hart.time_divisor

    @property
    def mtime(self) -> int:
        return (self.hart.instret & MASK64) // self.hart.time_divisor   # el mismo reloj que rdtime, envuelto a 64 bits

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
    STATE_FIELDS = ()   # inventario (P2.5): las ventanas las decide el host; no entran en el snapshot
    CONFIG_FIELDS = ("windows",)
    DERIVED_FIELDS: dict[str, str] = {}
    STATS_FIELDS = ()
    HARNESS_FIELDS = ()

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

    __slots__ = ("memory", "bus", "hart", "uart", "syscon", "clint", "time_divisor")
    STATE_FIELDS = ()   # inventario (P2.5): la máquina solo cablea; el estado vive en el hart, la RAM, el UART y el CLINT
    CONFIG_FIELDS = ("memory", "bus", "hart", "uart", "syscon", "clint", "time_divisor")   # el divisor de construcción
    DERIVED_FIELDS: dict[str, str] = {}
    STATS_FIELDS = ()
    HARNESS_FIELDS = ()

    def __init__(self, ram_bytes: int = 64 << 20, base: int = DEFAULT_BASE, time_divisor: int = 1) -> None:
        self.memory = Rv32Memory(ram_bytes, base)
        self.bus = Bus()
        try:
            self.hart = Rv32Hart(self.memory, self.bus, time_divisor)   # el CLINT toma el reloj del hart
        except BaseException:
            self.memory.close()   # divisor rechazado con la RAM ya reservada: no dejar el mmap vivo hasta el recolector (ciega 4)
            raise
        self.time_divisor = time_divisor                            # load_flat lo repone tras un restore
        self.uart = Uart()
        self.syscon = Syscon()
        self.clint = Clint(self.hart)
        self.bus.attach(UART_BASE, UART_SIZE, self.uart)
        self.bus.attach(SYSCON_BASE, SYSCON_SIZE, self.syscon)
        self.bus.attach(CLINT_BASE, CLINT_SIZE, self.clint)

    def load_flat(self, image: bytes | bytearray | memoryview, address: int | None = None) -> None:
        """Deja la máquina limpia (RAM a cero, UART y ``mtimecmp`` vacíos, hart a cero, divisor de tiempo el de la
        construcción aunque un ``restore`` lo cambiara), carga un binario plano (``llvm-objcopy -O binary``) y apunta
        el pc a su inicio. Si no cabe, ``AccessFault`` sin cargar."""
        start = self.memory.base if address is None else address
        if not 0 <= start - self.memory.base <= self.memory.size - memoryview(image).nbytes:
            raise AccessFault(start)   # bytes, no elementos: un array('I') cuenta 4 por elemento
        self.memory.reset()
        self.uart.reset()
        self.clint.reset()
        self.memory.load_bytes(start, image)
        self.hart.reset(start)
        self.hart.time_divisor = self.time_divisor

    def run(self, gas: int) -> Stop:
        return self.hart.run(gas)

    @property
    def uart_text(self) -> str:
        return self.uart.text

    # ── Snapshot y restauración (H2.1) ──

    def snapshot(self) -> dict[str, object]:
        """El snapshot del hart más el UART (salida y cola de entrada) y el CLINT (``mtimecmp``)."""
        state = self.hart.snapshot()
        state["uart"] = {"output": list(self.uart.output), "input": list(self.uart.input)}
        state["clint"] = {"mtimecmp": self.clint.mtimecmp}
        return state

    def state_hash(self) -> str:
        return hashlib.sha256(canonical_json(self.snapshot())).hexdigest()

    def snapshot_bytes(self) -> bytes:
        return encode_snapshot(self.snapshot())

    def restore(self, state: object) -> None:
        """Restaura hart, RAM, UART y CLINT de forma atómica: valida los dispositivos sin tocarlos, restaura el hart
        (que valida todo lo suyo antes de escribir) y solo entonces confirma UART y ``mtimecmp`` con pasos que no
        pueden fallar: la salida del UART pasa a ser un ``bytearray`` nuevo (redimensionar el anterior fallaría si el
        host tuviera una vista sobre él). Un snapshot de ``Rv32Hart`` suelto no vale aquí (le faltan secciones):
        ``ValueError``."""
        top = expect_keys(state, _MACHINE_KEYS, "raíz")
        uart = expect_keys(top["uart"], _UART_KEYS, "uart")
        clint = expect_keys(top["clint"], _CLINT_KEYS, "clint")
        output = _byte_list(uart["output"], "La salida del UART")
        pending = _byte_list(uart["input"], "La cola de entrada del UART")
        mtimecmp = clint["mtimecmp"]
        if type(mtimecmp) is not int or not 0 <= mtimecmp < (1 << 64):
            raise ValueError("mtimecmp inválido en el snapshot")
        self.hart.restore({key: top[key] for key in ("version", "hart", "ram")})   # atómica
        self.uart.output = bytearray(output)
        self.uart.input.clear()
        self.uart.input.extend(pending)
        self.clint.mtimecmp = mtimecmp

    def restore_bytes(self, data: bytes) -> None:
        self.restore(decode_snapshot(data))

    @classmethod
    def from_snapshot_bytes(cls, data: bytes, *, max_ram_bytes: int = DEFAULT_MAX_SNAPSHOT_RAM) -> "Machine":
        """Crea una máquina con la geometría de RAM que pide el snapshot (lo único de configuración que adopta de él;
        el resto es la predeterminada, también el divisor de construcción, aunque el hart adopte el del snapshot) y
        lo restaura. Rechaza una RAM mayor que ``max_ram_bytes`` antes de reservarla; si la restauración falla,
        cierra la máquina y relanza."""
        if type(max_ram_bytes) is not int or max_ram_bytes < 0:
            raise ValueError("max_ram_bytes debe ser un entero no negativo")
        payload = decode_snapshot(data)
        ram = payload.get("ram")
        if not isinstance(ram, dict) or type(ram.get("base")) is not int or type(ram.get("size")) is not int:
            raise ValueError("Snapshot sin geometría de RAM válida")
        if ram["size"] > max_ram_bytes:
            raise ValueError(f"El snapshot pide {ram['size']} bytes de RAM y el tope es {max_ram_bytes}")
        machine = cls(ram_bytes=ram["size"], base=ram["base"])   # Rv32Memory valida la geometría (ValueError)
        try:
            machine.restore(payload)
        except BaseException:
            machine.close()
            raise
        return machine

    def close(self) -> None:
        self.memory.close()


__all__ = ["Bus", "CLINT_BASE", "CLINT_MTIME", "CLINT_MTIMECMP", "CLINT_SIZE", "Clint", "DEFAULT_MAX_SNAPSHOT_RAM", "Machine",
           "SYSCON_BASE", "SYSCON_SIZE", "Syscon", "UART_BASE", "UART_SIZE", "Uart"]
