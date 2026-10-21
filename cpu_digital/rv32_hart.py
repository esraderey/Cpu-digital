"""Intérprete comprobar-y-confirmar de RV32I con Zicsr y modos M y U para el hart de RFC-EXP-00018 (H1; H3.1).

Cada manejador calcula, comprueba (rango de direcciones, alineación de destinos, existencia, privilegio y
escritura del CSR) y solo entonces escribe: una excepción no confirma nada de la instrucción que la produjo
(registros, memoria y CSR intactos; P1.2). No hay checkpoint ni journal. La excepción entra en el manejador de
``mtvec`` con la única escritura múltiple de la ISA (``mepc``, ``mcause``, ``mtval``, ``mstatus``, privilegio y
``pc``, en ese orden, sin nada que pueda fallar en medio), y cobra la tarifa de una instrucción sin avanzar
``instret``; si el destino del trap no está en la RAM (``mtvec`` = 0 tras el reinicio), el hart se detiene como en
H1/H2 con la causa de ``mcause`` y el ``mtval`` en el ``Stop``, sin escribir nada ni cobrar gas (modo compatible).
``MRET`` vuelve al privilegio de ``MPP``. El mapa de CSR de la plataforma (perfil WARL fijo, P1.8) está en
``CSR_READERS``/``CSR_WRITERS`` y en ``.forja/h3/spec_zicsr_traps.md``.

Interrupciones (H3.4; ``.forja/h3/spec_interrupciones_wfi.md``): la fuente de ``mip`` es ``interrupts`` (el CLINT en
``Machine``). Una interrupción pendiente, habilitada en ``mie`` y tomable (modo U, o M con ``mstatus.MIE``) entra como trap
en la frontera de instrucción (MEI > MSI > MTI; ``mepc`` = ``pc``, ``mtval`` = 0; 1 de gas; ``Stop("interrupcion")`` en modo
compatible). El bucle caliente no la busca: se reevalúa al empezar ``run``, tras cada trap, tras las instrucciones que
pueden cambiar qué es tomable (escritura de ``mstatus`` o ``mie``, ``MRET``, store a un dispositivo, ``WFI``, que avisan con
``_Recheck`` tras confirmarse) y al llegar al plazo del temporizador: mientras haya plazo, el gas que lo rebasa se aparta y
vuelve al reevaluar o al salir, así que ni el intérprete ni un bloque lo cruzan. ``WFI`` adelanta ``instret`` hasta el plazo
sin cobrar más gas o, sin evento posible, para con ``Stop("wfi")`` sin confirmar nada. No hay estado nuevo: todo lo que
decide la entrega ya está en el snapshot y el plazo se recalcula en cada ``run`` (invariante S).

Dos contadores (P1.3): ``gas`` mide el coste del host (tarifa provisional de 1 por instrucción)
e ``instret`` las instrucciones confirmadas del invitado; ``cycle`` = ``instret`` hasta que P3
aporte el modelo de tiempo y ``time`` = ``instret`` // ``time_divisor`` (estado del hart, que el snapshot
lleva; ``TIME_DIVISOR`` es solo su valor por defecto).

La decodificación se cachea por página de 4 KiB en una lista de 1 024 entradas asociada a la
generación de la página (P1.6): una escritura que sube la generación, propia o ajena, la descarta
entera. Al decodificar una palabra el hart marca su línea de 16 bytes en el mapa de código de la
memoria (``Rv32Memory.code``), y sus stores solo suben la generación cuando tocan una línea
marcada: un store a datos que comparten página con el código ya no invalida nada (refinamiento de
P1.6 del 2026-10-11; antes todo store invalidaba). Caché y mapa son derivados y no entran en el
snapshot; ``attach_memory`` y ``reset`` los vacían juntos. Todo depende de que las generaciones de
``Rv32Memory`` nunca bajen (también en ``reset`` y ``restore_pages``): el hart no se entera de
esas operaciones y una generación repetida resucitaría una decodificación vieja.

Fuente: RISC-V ISA Manual no privilegiado v20260120 §2.4–§2.8 y Zicsr; privilegiado, tabla de
``mcause``. Los atributos ``base``, ``size``, ``lanes``, ``words_u``, ``gens``, ``written`` y
``code`` son espejos de la memoria para el camino caliente: si se sustituye la memoria hay que
llamar a ``attach_memory``.

Snapshot y restauración (H2.1; P1.7, P2.3 y P2.5 del RFC; esquema v2 desde H3.0). ``snapshot()`` devuelve el
estado arquitectónico y el contenido de la RAM en un diccionario serializable en JSON::

    {"version": 2,
     "hart": {"regs": [32 enteros en [0, 2³²)], "pc": entero en [0, 2³²), "instret": entero en [0, 2⁶⁴)
              (run lo envuelve a 64 bits al terminar y time/timeh/mtime se calculan sobre el contador
              envuelto, como los contadores de Zicntr),
              "csrs": {"0x300": mstatus, "0x304": mie, "0x305": mtvec, "0x340": mscratch, "0x341": mepc,
                       "0x342": mcause, "0x343": mtval},     # los CSR de modo M con estado propio (STATEFUL_CSRS)
              "privilege": "M" | "U",
              "reservation": null | dirección de palabra en la RAM (reserva de LR/SC),
              "time_divisor": entero en [1, 2⁶⁴)},
     "ram": {"base": ..., "size": ..., "page_words": 4096, "pages": [[índice, [enteros con signo]], ...]}}

Cada CSR guarda solo valores legales (WARL fijo de la plataforma, P1.8: ``legalize_csr``); ``mip`` no tiene bits con
estado propio (``MSIP`` es el registro ``msip`` del CLINT y ``MTIP`` se deriva de ``mtime`` y ``mtimecmp``), así que no
entra. ``restore`` lee también la versión 1 de H2 (``csrs`` vacío, sin ``privilege``, reserva nula) y pone el estado
nuevo a su valor de reinicio (CSR a 0, modo M, sin reserva); ``snapshot()`` emite siempre la versión 2.

No lleva el gas (lo fija el arnés en cada ``run``), ni la configuración (``accelerate``, ``horizon``,
``warmup``, ``max_blocks``, ``keep_block_sources``, el bus), ni las cachés ni las estadísticas: todo eso
está clasificado en el inventario de la clase (``STATE_FIELDS``, ``DERIVED_FIELDS``, ``CONFIG_FIELDS``,
``STATS_FIELDS``, ``HARNESS_FIELDS``), que una prueba recorre por introspección. ``restore`` valida todo
antes de tocar nada (atómico), restaura la RAM por ``restore_pages`` y vacía las cachés y el mapa de
código; ``state_hash`` es el sha256 de la serialización canónica (P2.3) y ``snapshot_bytes`` /
``restore_bytes`` la envuelven con firma, zlib y tope de tamaño como los snapshots de VM32. La invariante S
(``run(B1); snapshot; restore; run(B2)`` ≡ ``run(B1 + B2)``) se enuncia sobre ``run`` con gas exacto, es decir,
con ``horizon=True`` (el valor por defecto): sin horizonte, cuánto rebasa cada ``run`` depende de qué bloques
estén compilados, y eso no es estado del invitado.
"""

from __future__ import annotations

import hashlib
import json
import sys
import zlib
from typing import Callable, NamedTuple

from . import rv32_alu as alu
from .rv32_decode import decode
from .rv32_isa import (
    CAUSE_BREAKPOINT,
    CAUSE_ECALL_FROM_M,
    CAUSE_ECALL_FROM_U,
    CAUSE_ILLEGAL_INSTRUCTION,
    CAUSE_INSTRUCTION_ACCESS_FAULT,
    CAUSE_INSTRUCTION_MISALIGNED,
    CAUSE_LOAD_ACCESS_FAULT,
    CAUSE_LOAD_MISALIGNED,
    CAUSE_STORE_ACCESS_FAULT,
    CAUSE_STORE_MISALIGNED,
    CSR_CYCLE,
    CSR_CYCLEH,
    CSR_INSTRET,
    CSR_INSTRETH,
    CSR_MARCHID,
    CSR_MCAUSE,
    CSR_MCONFIGPTR,
    CSR_MCOUNTEREN,
    CSR_MCYCLE,
    CSR_MCYCLEH,
    CSR_MEPC,
    CSR_MHARTID,
    CSR_MIE,
    CSR_MIMPID,
    CSR_MINSTRET,
    CSR_MINSTRETH,
    CSR_MIP,
    CSR_MISA,
    CSR_MSCRATCH,
    CSR_MSTATUS,
    CSR_MSTATUSH,
    CSR_MTVAL,
    CSR_MTVEC,
    CSR_MVENDORID,
    CSR_TIME,
    CSR_TIMEH,
    INTERRUPT_BIT,
    INTERRUPT_MACHINE_EXTERNAL,
    INTERRUPT_MACHINE_SOFTWARE,
    INTERRUPT_MACHINE_TIMER,
    MIE_MEIE,
    MIE_MSIE,
    MIE_MTIE,
    MIP_MEIP,
    MIP_MSIP,
    MSTATUS_MIE,
    MSTATUS_MPIE,
    MSTATUS_MPP,
    MSTATUS_MPRV,
    MSTATUS_TW,
    PRIV_M,
    PRIV_U,
    Kind,
)
from .rv32_mem import LINE_MASK, LINE_SHIFT, SNAPSHOT_PAGE_WORDS, AccessFault, Rv32Memory

MASK32 = 0xFFFFFFFF
MASK64 = (1 << 64) - 1
GAS_PER_INSTRUCTION = 1
TIME_DIVISOR = 1
PAGE_SHIFT = 12
ENTRIES_PER_PAGE = 1024
DEFAULT_WARMUP = 2  # veces que un punto de control se alcanza antes de compilar su bloque
# Recompilaciones por pc antes de dejar ese punto al intérprete: un bucle con datos en la página de
# su propio código invalida su bloque en cada vuelta y recompilar cuesta ~26× interpretarlo.
MAX_RECOMPILATIONS = 8
# Entradas de la caché de bloques (y de calentamiento): al llenarse se vacía entera, como la caché de
# bloques de QEMU. Un bloque sin fuente retiene ~4,4 KB: 4 096 entradas acotan la caché en ~18 MB.
MAX_BLOCKS = 4096
# Snapshot (H2.1; esquema v2 desde H3.0): versión que emite snapshot(), versiones que restore lee, firma de la
# serialización y tope del JSON descomprimido (una RAM de 64 MiB entera cabe). CSR_SLOTS enlaza cada CSR de modo M con
# estado propio con su slot del hart; STATEFUL_CSRS son sus números, que entran en snapshot()["hart"]["csrs"] con
# claves hexadecimales en texto ("0x300"). Los contadores de Zicntr se derivan de instret y del divisor; los CSR
# constantes (misa, mvendorid, marchid, mimpid, mhartid) no tienen estado; mip tampoco (MSIP vive en el CLINT y MTIP
# se deriva de mtime y mtimecmp).
SNAPSHOT_VERSION = 2
SNAPSHOT_VERSIONS = frozenset({1, 2})
SNAPSHOT_MAGIC = b"RV32S\x01"
MAX_SNAPSHOT_RAW_BYTES = 256 * 1024 * 1024
CSR_SLOTS: tuple[tuple[int, str], ...] = (
    (CSR_MSTATUS, "mstatus"), (CSR_MIE, "mie"), (CSR_MTVEC, "mtvec"), (CSR_MSCRATCH, "mscratch"), (CSR_MEPC, "mepc"),
    (CSR_MCAUSE, "mcause"), (CSR_MTVAL, "mtval"),
)
STATEFUL_CSRS: frozenset[int] = frozenset(number for number, _ in CSR_SLOTS)
_TOP_KEYS = frozenset({"version", "hart", "ram"})
_HART_KEYS_V1 = frozenset({"regs", "pc", "instret", "csrs", "reservation", "time_divisor"})
_HART_KEYS = _HART_KEYS_V1 | {"privilege"}
_RAM_KEYS = frozenset({"base", "size", "page_words", "pages"})
_CSR_KEYS = frozenset(f"{number:#05x}" for number in STATEFUL_CSRS)   # una sola grafía: "0x" y tres dígitos en minúscula
_PRIVILEGES = {"M": PRIV_M, "U": PRIV_U}
_PRIVILEGE_NAMES = {PRIV_M: "M", PRIV_U: "U"}
# Perfil WARL de la plataforma (P1.8; H3): bits escribibles de mstatus y mie. MPP solo admite M y U.
MSTATUS_WRITABLE = MSTATUS_MIE | MSTATUS_MPIE | MSTATUS_MPP | MSTATUS_MPRV | MSTATUS_TW
MIE_WRITABLE = MIE_MSIE | MIE_MTIE | MIE_MEIE


def legalize_csr(csr: int, value: int) -> int:
    """Lo que guarda el CSR con estado ``csr`` (de ``STATEFUL_CSRS``) al escribirle ``value`` (en [0, 2³²)): la
    legalización WARL fija de la plataforma (P1.8). ``mstatus`` solo conserva MIE, MPIE, MPP, MPRV y TW, y un MPP
    ilegal (1, modo S, o 2, reservado) pasa a U, como en Spike y Sail; ``mie`` solo MSIE, MTIE y MEIE; ``mtvec`` borra
    el bit 1 (MODE ∈ {0 directo, 1 vectorizado}); ``mepc`` borra los dos bits bajos (IALIGN = 32, sin C);
    ``mscratch``, ``mcause`` (WLRL) y ``mtval`` guardan los 32 bits. Un snapshot solo es válido si cada CSR ya es su
    propia legalización."""
    if csr == CSR_MSTATUS:
        value &= MSTATUS_WRITABLE
        if (value & MSTATUS_MPP) not in (0, MSTATUS_MPP):
            value &= ~MSTATUS_MPP
        return value
    if csr == CSR_MIE:
        return value & MIE_WRITABLE
    if csr == CSR_MTVEC:
        return value & 0xFFFFFFFD
    if csr == CSR_MEPC:
        return value & 0xFFFFFFFC
    if csr in STATEFUL_CSRS:
        return value & MASK32
    raise ValueError(f"CSR sin estado propio: {csr:#x}")


def canonical_json(payload: object) -> bytes:
    """Serialización canónica de un snapshot (claves ordenadas, sin espacios, ASCII): lo que firma el hash de
    estado y lo que comprime ``encode_snapshot``; dos snapshots iguales dan los mismos bytes en cualquier proceso."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def encode_snapshot(payload: object) -> bytes:
    """Firma + zlib (nivel 9) de la serialización canónica: determinista para una misma zlib (entre versiones de
    zlib lo estable es el JSON canónico y el hash, no los bytes comprimidos)."""
    return SNAPSHOT_MAGIC + zlib.compress(canonical_json(payload), 9)


def decode_snapshot(data: bytes, maximum: int | None = None) -> dict[str, object]:
    """Inverso de ``encode_snapshot`` con la receta de VM32: firma, descompresión acotada a ``maximum`` bytes
    (``MAX_SNAPSHOT_RAW_BYTES`` si no se da; un snapshot truncado, corrupto o con bytes de más se rechaza), JSON y
    diccionario. Solo valida la envoltura; el contenido lo valida ``restore``. Toda anomalía de ``data`` es
    ``ValueError``; el tope acota los bytes descomprimidos, no la memoria que ocupa el JSON cargado (unas diez
    veces más en el peor caso medido)."""
    if maximum is None:
        maximum = MAX_SNAPSHOT_RAW_BYTES
    if type(maximum) is not int or maximum < 0:   # un tope negativo haría descomprimir sin límite antes de rechazar
        raise ValueError("Tope de tamaño del snapshot inválido")
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ValueError("El snapshot RV32 debe ser bytes")
    data = bytes(data)
    if not data.startswith(SNAPSHOT_MAGIC):
        raise ValueError("Firma de snapshot RV32 inválida")
    decompressor = zlib.decompressobj()
    try:
        raw = decompressor.decompress(data[len(SNAPSHOT_MAGIC):], min(maximum, sys.maxsize - 1) + 1)
    except zlib.error as exc:
        raise ValueError("Snapshot RV32 corrupto") from exc
    if len(raw) > maximum or decompressor.unconsumed_tail or decompressor.unused_data or not decompressor.eof:
        raise ValueError("Snapshot RV32 excede el tamaño permitido o está truncado")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("JSON de snapshot RV32 inválido") from exc
    if not isinstance(payload, dict):
        raise ValueError("Estructura de snapshot RV32 inválida")
    return payload


def expect_keys(mapping: object, expected: frozenset[str], name: str) -> dict[str, object]:
    """``mapping`` debe ser un diccionario con exactamente las claves ``expected``; si no, ``ValueError``. Devuelve
    una copia plana con cada clave leída una sola vez: el mapeo original no se vuelve a consultar (un objeto hostil
    no puede cambiar entre validar y restaurar) y una clave que sus claves prometen pero su lectura no tiene es
    ``ValueError``, no ``KeyError`` (ciega 4)."""
    if not isinstance(mapping, dict):
        raise ValueError(f"La sección {name} del snapshot debe ser un diccionario")
    if set(mapping) != expected:
        raise ValueError(f"Claves inválidas en la sección {name} del snapshot: faltan "
                         f"{sorted(map(repr, expected - set(mapping)))}, sobran {sorted(map(repr, set(mapping) - expected))}")
    try:
        return {key: mapping[key] for key in expected}
    except KeyError as exc:
        raise ValueError(f"La sección {name} del snapshot anuncia una clave que no tiene: {exc.args[0]!r}") from None


def _is_uint(value: object, bits: int) -> bool:
    return type(value) is int and 0 <= value < (1 << bits)


class Stop(NamedTuple):
    """Por qué se detuvo ``run``: ``gas``, ``apagado``, ``reinicio``, ``fallo``, ``ebreak``, ``ecall``, ``interrupcion``
    o ``wfi`` (H3.4).

    ``cause`` es el valor de ``mcause`` cuando la parada es un trap en modo compatible (``fallo``, ``ebreak``, ``ecall``,
    ``interrupcion``, con el bit 31 puesto) y −1 cuando no lo es; ``value`` es el ``mtval`` correspondiente (dirección,
    palabra o 0). ``wfi``: una ``WFI`` sin evento posible, sin confirmar (el ``pc`` sigue en ella).
    """

    reason: str
    cause: int = -1
    value: int = 0


class HostStop(Exception):
    """Un dispositivo pide parar tras confirmar la instrucción (SYSCON).

    Solo puede lanzarla una **escritura**: el efecto ya se produjo y la instrucción se confirma
    (``pc + 4``, ``instret`` y gas cobrados). Una carga que la lanzara confirmaría la instrucción
    sin escribir ``rd``; los dispositivos no deben hacerlo.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Halt(Exception):
    """Parada antes de confirmar la instrucción en curso."""

    def __init__(self, reason: str, cause: int = -1, value: int = 0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.cause = cause
        self.value = value


class _Recheck(Exception):
    """La instrucción en curso ya confirmó sus efectos y puede haber cambiado qué interrupción es tomable (escritura de
    ``mstatus`` o ``mie``, ``MRET``, store a un dispositivo, ``WFI``; H3.4): el bucle la retira (``pc``, ``instret`` y
    gas) y reevalúa las interrupciones antes de la siguiente. ``next_pc`` es el destino (``None``: ``pc`` + 4) e
    ``instret``, si no es ``None``, el contador tras retirarla (``WFI`` adelanta el reloj del invitado)."""

    def __init__(self, next_pc: int | None = None, instret: int | None = None) -> None:
        super().__init__()
        self.next_pc = next_pc
        self.instret = instret


class _Stall(Exception):
    """``WFI`` sin ningún evento posible (H3.4): parada ``Stop("wfi")`` sin confirmar nada (el ``pc`` sigue en ella)."""


class Rv32Hart:
    __slots__ = ("memory", "bus", "interrupts", "regs", "pc", "instret", "gas", "decode_cache", "time_divisor",
                 "reservation", "privilege", "mstatus", "mie", "mtvec", "mscratch", "mepc", "mcause", "mtval",
                 "base", "size", "lanes", "words_u", "gens", "written", "code",
                 "accelerate", "horizon", "warmup", "blocks", "block_hits", "block_instructions", "compilations",
                 "recompilations", "keep_block_sources", "max_blocks", "cache_flushes")
    # Inventario de estado (P2.5): cada slot está en exactamente una de estas listas; tests/test_rv32_snapshot.py lo
    # comprueba por introspección sobre toda clase con __slots__ de los módulos rv32_* y fija la clasificación exacta
    # (un slot nuevo sin clasificar, o clasificado de otra forma, pone la suite en rojo). Los slots de CSR_SLOTS viajan
    # en snapshot()["hart"]["csrs"]; los demás, con su nombre en snapshot()["hart"].
    STATE_FIELDS = ("regs", "pc", "instret", "reservation", "time_divisor", "privilege",
                    "mstatus", "mie", "mtvec", "mscratch", "mepc", "mcause", "mtval")
    CSR_FIELDS = CSR_SLOTS
    DERIVED_FIELDS = {
        "decode_cache": "diccionario página → (generación con la que se llenó, lista de decodificaciones); vacío en restore, "
                        "reset y attach_memory",
        "blocks": "bloques compilados con la generación de sus páginas; vacíos en restore, reset y attach_memory",
        "block_hits": "calentamiento por pc: decide cuándo compilar, no qué se ejecuta; vacío en restore, reset y attach_memory",
        "recompilations": "recompilaciones por pc: deciden cuándo dejar de compilar, no qué se ejecuta; vacías en restore, "
                          "reset y attach_memory",
        "base": "espejo de memory.base; attach_memory lo refresca",
        "size": "espejo de memory.size; attach_memory lo refresca",
        "lanes": "espejo de memory.lanes; attach_memory lo refresca",
        "words_u": "espejo de memory.words_u; attach_memory lo refresca",
        "gens": "espejo de memory.generations; attach_memory lo refresca",
        "written": "espejo de memory.written; attach_memory lo refresca",
        "code": "espejo de memory.code; attach_memory lo refresca",
    }
    # interrupts: la fuente de los bits pendientes de mip (el CLINT en Machine; None en un hart suelto), como el bus.
    CONFIG_FIELDS = ("memory", "bus", "interrupts", "accelerate", "horizon", "warmup", "keep_block_sources", "max_blocks")
    STATS_FIELDS = ("block_instructions", "compilations", "cache_flushes")
    HARNESS_FIELDS = ("gas",)   # presupuesto que run recibe y devuelve: lo fija el arnés, nunca el snapshot

    def __init__(self, memory: Rv32Memory, bus: object | None = None, time_divisor: int = TIME_DIVISOR) -> None:
        if type(time_divisor) is not int or not 1 <= time_divisor < (1 << 64):   # el dominio del snapshot
            raise ValueError("time_divisor debe ser un entero en [1, 2⁶⁴)")
        self.bus = bus
        self.interrupts = None   # objeto con pending_bits() -> bits de mip (Machine pone el CLINT)
        self.regs: list[int] = [0] * 32
        self.instret = 0
        self.gas = 0
        self.reservation = None   # reserva de LR/SC: dirección de palabra en la RAM o None (entra en el snapshot)
        self.privilege = PRIV_M
        self.mstatus = self.mie = self.mtvec = self.mscratch = self.mepc = self.mcause = self.mtval = 0
        self.time_divisor = time_divisor
        # Bloques compilados (rv32_blocks): desactivados por defecto en H1 (E3 los midió en 3,21× el
        # intérprete; se activan por defecto cuando H2 los someta a la invariante S).
        self.accelerate = False
        self.horizon = True
        self.warmup = DEFAULT_WARMUP
        self.keep_block_sources = False   # True conserva el texto generado en cada Block (depuración)
        self.max_blocks = MAX_BLOCKS
        self.blocks: dict[int, object] = {}
        self.block_hits: dict[int, int] = {}
        self.recompilations: dict[int, int] = {}
        self.block_instructions = 0
        self.compilations = 0
        self.cache_flushes = 0
        self.attach_memory(memory)
        self.pc = memory.base

    def attach_memory(self, memory: Rv32Memory) -> None:
        """Sustituye la memoria: espejos del camino caliente nuevos; caché de decodificación, bloques,
        calentamiento, recompilaciones y mapa de código de la memoria vacíos; la reserva de LR/SC se pierde (era
        sobre la memoria sustituida). No toca registros, ``pc``, ``instret``, CSR, privilegio, gas ni las
        estadísticas acumuladas (``compilations``, ``block_instructions``, ``cache_flushes``): eso es ``reset``."""
        self.reservation = None
        self.memory = memory
        self.base = memory.base
        self.size = memory.size
        self.lanes = memory.lanes
        self.words_u = memory.words_u
        self.gens = memory.generations
        self.written = memory.written
        self.code = memory.code
        # Las marcas justificaban las cachés de este hart, que se vacían aquí mismo; clear_code sube además la
        # generación de las páginas marcadas, por si otro hart con cachés vivas comparte la memoria.
        memory.clear_code()
        self.decode_cache: dict[int, tuple[int, list[object]]] = {}
        self.blocks = {}
        self.block_hits = {}
        self.recompilations = {}

    def reset(self, pc: int | None = None) -> None:
        """Registros a cero (la misma lista: quien la tenga referenciada sigue viéndola), contadores a cero, CSR de
        modo M a su reinicio (0), modo M, sin reserva, cachés y mapa de código de la memoria vacíos."""
        self.regs[:] = [0] * 32
        self.pc = self.base if pc is None else pc
        self.instret = 0
        self.gas = 0
        self.reservation = None
        self.privilege = PRIV_M
        self.mstatus = self.mie = self.mtvec = self.mscratch = self.mepc = self.mcause = self.mtval = 0
        self.decode_cache = {}
        self.blocks = {}
        self.block_hits = {}
        self.recompilations = {}
        self.block_instructions = 0
        self.compilations = 0
        self.cache_flushes = 0
        self.memory.clear_code()   # junto con las cachés que lo justificaban

    # ── Vías lentas: fuera de la RAM alineada ──

    def _load_slow(self, address: int, width: int) -> int:
        offset = address - self.base
        if 0 <= offset <= self.size - width:
            memory = self.memory
            if width == 4:
                return memory.load32(address)
            return memory.load16u(address) if width == 2 else memory.load8u(address)
        if self.bus is None:
            raise _Halt("fallo", CAUSE_LOAD_ACCESS_FAULT, address)
        try:
            return self.bus.load(address, width)
        except AccessFault:
            raise _Halt("fallo", CAUSE_LOAD_ACCESS_FAULT, address) from None

    def _store_slow(self, address: int, width: int, value: int) -> None:
        offset = address - self.base
        if 0 <= offset <= self.size - width:
            memory = self.memory
            if self.reservation is not None:
                _lose_reservation(self, address, width)   # política R-9: el store ya no puede fallar
            if width == 4:
                memory.store32(address, value)
            elif width == 2:
                memory.store16(address, value)
            else:
                memory.store8(address, value)
            return
        if self.bus is None:
            raise _Halt("fallo", CAUSE_STORE_ACCESS_FAULT, address)
        try:
            self.bus.store(address, width, value)
        except AccessFault:
            raise _Halt("fallo", CAUSE_STORE_ACCESS_FAULT, address) from None
        raise _Recheck()   # un dispositivo puede haber cambiado msip o mtimecmp (H3.4): el store ya se confirmó

    def read_csr(self, csr: int, raw: int = 0) -> int:
        """Lee ``csr`` en el privilegio actual según el mapa de la plataforma (``CSR_READERS``); un CSR que no existe
        o que el privilegio actual no alcanza es instrucción ilegal (``_Halt`` con ``raw`` como ``mtval``)."""
        reader = CSR_READERS.get(csr)
        if reader is None or (csr >> 8) & 3 > self.privilege or (self.privilege == PRIV_U and csr in _HPM_USER):
            raise _Halt("fallo", CAUSE_ILLEGAL_INSTRUCTION, raw)
        return reader(self)

    def trap_target(self, cause: int) -> int | None:
        """Dirección a la que entra un trap de causa ``cause`` según ``mtvec`` (las excepciones van a la base; en modo
        vectorizado las interrupciones van a base + 4 × código), o ``None`` si no está en la RAM: modo compatible."""
        mtvec = self.mtvec
        target = mtvec & 0xFFFFFFFC
        if mtvec & 1 and cause & INTERRUPT_BIT:
            target = (target + 4 * (cause & 0x7FFFFFFF)) & MASK32
        return target if 0 <= target - self.base <= self.size - 4 else None

    def enter_trap(self, cause: int, tval: int, pc: int) -> None:
        """Entrada en trap (P1.2): escribe, en este orden y sin nada que pueda fallar, ``mepc`` (el ``pc`` de la
        instrucción), ``mcause``, ``mtval``, ``mstatus`` (MPIE ← MIE, MIE ← 0, MPP ← privilegio) y el privilegio (M);
        pierde la reserva de LR/SC. El ``pc`` nuevo (``trap_target``) lo pone quien llama."""
        mstatus = self.mstatus
        self.mepc = pc & 0xFFFFFFFC
        self.mcause = cause & MASK32
        self.mtval = tval & MASK32
        self.mstatus = ((mstatus & ~(MSTATUS_MIE | MSTATUS_MPIE | MSTATUS_MPP)) | (MSTATUS_MPIE if mstatus & MSTATUS_MIE else 0)
                        | (self.privilege << 11))
        self.privilege = PRIV_M
        self.reservation = None

    # ── Snapshot y restauración (H2.1) ──

    def snapshot(self) -> dict[str, object]:
        """Estado arquitectónico del hart y contenido de la RAM, serializable en JSON (esquema en la cabecera del
        módulo). No lleva el gas, la configuración, las cachés ni las estadísticas. No modifica nada."""
        memory = self.memory
        return {
            "version": SNAPSHOT_VERSION,
            "hart": {"regs": list(self.regs), "pc": self.pc & MASK32, "instret": self.instret & MASK64,
                     "csrs": {f"{number:#05x}": getattr(self, slot) for number, slot in CSR_SLOTS},
                     "privilege": _PRIVILEGE_NAMES.get(self.privilege, self.privilege),   # fuera de dominio: falla al restaurar
                     "reservation": self.reservation, "time_divisor": self.time_divisor},
            "ram": {"base": memory.base, "size": memory.size, "page_words": SNAPSHOT_PAGE_WORDS,
                    "pages": memory.snapshot_pages()},
        }

    def state_hash(self) -> str:
        """sha256 (hexadecimal) de la serialización canónica de ``snapshot()`` (P2.3): igual en dos harts con el
        mismo estado arquitectónico, con o sin bloques, en cualquier proceso."""
        return hashlib.sha256(canonical_json(self.snapshot())).hexdigest()

    def snapshot_bytes(self) -> bytes:
        return encode_snapshot(self.snapshot())

    def restore(self, state: object) -> None:
        """Deja el hart y su RAM exactamente como describe ``state`` (lo que devolvió ``snapshot``, o su JSON).
        Valida todo antes de tocar nada: si lanza ``ValueError``, el hart, la memoria, las cachés, las estadísticas
        y el gas quedan como estaban. Al confirmar vacía la caché de decodificación, los bloques, el calentamiento,
        las recompilaciones y el mapa de código (``clear_code`` sube la generación de las páginas marcadas y
        ``restore_pages`` la de las restauradas y borradas: ninguna caché anterior, de este hart o de otro sobre la
        misma memoria, puede coincidir). No adopta configuración ni toca el gas ni las estadísticas; sí adopta
        ``time_divisor``, que es estado del invitado. Un snapshot de la versión 1 deja los CSR de modo M, el privilegio
        y la reserva en su valor de reinicio."""
        regs, pc, instret, csrs, privilege, reservation, time_divisor, pages = self._parse_snapshot(state)
        self.memory.restore_pages(pages)   # valida su entrada antes de escribir: si lanza, nada ha cambiado
        self.regs[:] = regs                # la misma lista, como reset
        self.pc = pc
        self.instret = instret
        self.mstatus, self.mie, self.mtvec, self.mscratch, self.mepc, self.mcause, self.mtval = csrs
        self.privilege = privilege
        self.reservation = reservation
        self.time_divisor = time_divisor
        self.decode_cache = {}
        self.blocks = {}
        self.block_hits = {}
        self.recompilations = {}
        self.memory.clear_code()
        if reservation is not None:   # H3.3: la línea de la reserva vuelve a marcarse (los store de bloque salen)
            self.memory.mark_code(reservation - self.base, 4)

    def restore_bytes(self, data: bytes) -> None:
        self.restore(decode_snapshot(data))

    def _parse_snapshot(self, state: object) -> tuple[list[int], int, int, tuple[int, ...], int, int | None, int, object]:
        """Valida un snapshot (versión 1 o 2) contra este hart sin modificarlo; devuelve los campos normalizados:
        ``regs`` copiada antes de validarse y cada clave leída una sola vez (``expect_keys`` copia cada sección, también
        ``csrs``), de modo que lo validado es lo que se restaura aunque el objeto cambie al releerlo; los CSR en el
        orden de ``CSR_SLOTS``. Las páginas las copia y valida ``Rv32Memory.restore_pages`` registro a registro, cada
        una copiada antes de medirla y validarla (también antes de escribir)."""
        if self.memory.buffer.closed:
            raise ValueError("La RAM del hart está cerrada")
        top = expect_keys(state, _TOP_KEYS, "raíz")
        version = top["version"]
        if type(version) is not int or version not in SNAPSHOT_VERSIONS:
            raise ValueError("Versión de snapshot RV32 no compatible")
        hart = expect_keys(top["hart"], _HART_KEYS if version == 2 else _HART_KEYS_V1, "hart")
        ram = expect_keys(top["ram"], _RAM_KEYS, "ram")
        given = hart["regs"]
        regs = list(given) if isinstance(given, list) else None
        if regs is None or len(regs) != 32 or not all(_is_uint(value, 32) for value in regs):
            raise ValueError("Registros inválidos en el snapshot")
        if regs[0] != 0:
            raise ValueError("x0 debe valer cero en el snapshot")
        pc = hart["pc"]
        if not _is_uint(pc, 32):
            raise ValueError("pc inválido en el snapshot")
        instret = hart["instret"]
        if not _is_uint(instret, 64):   # el contador de Zicntr es de 64 bits; y así siempre se serializa
            raise ValueError("instret inválido en el snapshot")
        given_csrs = hart["csrs"]
        if not isinstance(given_csrs, dict):
            raise ValueError("CSR inválidos en el snapshot")
        if version == 1:
            # H2 no tenía CSR con estado: csrs vacío; los de modo M quedan en su reinicio.
            if given_csrs:
                raise ValueError("Un snapshot de la versión 1 no lleva CSR: csrs debe estar vacío")
            csrs: tuple[int, ...] = (0,) * len(CSR_SLOTS)
        else:
            # Exactamente los CSR con estado, con una sola grafía cada uno ("0x" y tres dígitos en minúscula).
            values = expect_keys(given_csrs, _CSR_KEYS, "csrs")
            csrs = tuple(values[f"{number:#05x}"] for number, _ in CSR_SLOTS)
            for (number, _), value in zip(CSR_SLOTS, csrs):
                if not _is_uint(value, 32) or legalize_csr(number, value) != value:
                    raise ValueError(f"Valor ilegal del CSR {number:#05x} en el snapshot: {value!r}")
        if version == 1:
            privilege = PRIV_M
        else:
            name = hart["privilege"]
            if type(name) is not str or name not in _PRIVILEGES:
                raise ValueError("privilege inválido en el snapshot: debe ser \"M\" o \"U\"")
            privilege = _PRIVILEGES[name]
        memory = self.memory
        base, size, page_words = ram["base"], ram["size"], ram["page_words"]
        if type(base) is not int or type(size) is not int or (base, size) != (memory.base, memory.size):
            raise ValueError("El snapshot requiere otra RAM (base o tamaño distintos)")
        if type(page_words) is not int or page_words != SNAPSHOT_PAGE_WORDS:
            raise ValueError("Tamaño de página de snapshot inválido")
        reservation = hart["reservation"]
        if reservation is not None and (version == 1 or type(reservation) is not int or reservation & 3
                                        or not base <= reservation <= base + size - 4):
            raise ValueError("reservation inválida en el snapshot: null o una dirección de palabra en la RAM")
        time_divisor = hart["time_divisor"]
        if type(time_divisor) is not int or not 1 <= time_divisor < (1 << 64):
            raise ValueError("time_divisor inválido en el snapshot")
        return regs, pc, instret, csrs, privilege, reservation, time_divisor, ram["pages"]

    # ── Bloques compilados ──

    def _block_for(self, pc: int):
        """Bloque válido para ``pc``: lo compila si el punto está caliente y lo recompila si una de sus
        páginas cambió; ``None`` si toca interpretar (frío, no traducible o demasiado volátil)."""
        blocks = self.blocks
        block = blocks.get(pc)
        if block is None:
            if pc in blocks:  # no traducible o volátil: no se reintenta hasta reset/attach_memory
                return None
            hits = self.block_hits.get(pc, 0) + 1
            if hits < self.warmup:
                if len(self.block_hits) >= self.max_blocks:
                    self.block_hits.clear()
                self.block_hits[pc] = hits
                return None
        else:
            gens = self.gens
            if all(gens[page] == generation for page, generation in zip(block.pages, block.generations)):
                return block
            recompiled = self.recompilations.get(pc, 0) + 1
            if recompiled > MAX_RECOMPILATIONS:
                blocks[pc] = None   # demasiado volátil: el intérprete se queda con este punto
                return None
            self.recompilations[pc] = recompiled
        from .rv32_blocks import compile_block
        block = compile_block(self, pc, GAS_PER_INSTRUCTION, self.keep_block_sources)
        if len(blocks) >= self.max_blocks and pc not in blocks:
            blocks.clear()   # caché llena: se vacía entera (determinista; los pcs calientes se recompilan)
            self.block_hits.clear()
            self.recompilations.clear()
            self.cache_flushes += 1
        blocks[pc] = block   # None queda cacheado: «no traducible» no se reintenta
        if block is not None:
            self.compilations += 1
        return block

    # ── Bucle del intérprete ──

    def run(self, gas: int) -> Stop:
        """Ejecuta hasta agotar ``gas`` (1 por instrucción) o hasta una parada; devuelve el motivo."""
        if gas < 0:
            raise ValueError("El gas no puede ser negativo")
        regs = self.regs
        base, size, gens, words = self.base, self.size, self.gens, self.words_u
        code = self.code
        pages = self.decode_cache
        handlers = HANDLERS
        tariff = GAS_PER_INSTRUCTION
        blocks = self.blocks
        block_instructions = self.block_instructions
        pc = self.pc & MASK32
        instret = self.instret
        # Centinela de la página en curso: ninguna página calculada (|página| < 2²⁰) coincide con él;
        # −1 sería la página justo debajo de la base y saltaría la comprobación de rango.
        cur_page = -(1 << 32)
        cur_gen = -1
        cur_list: list[object] = []
        stop: Stop | None = None
        accelerate = self.accelerate
        horizon = self.horizon
        lookup = accelerate  # solo se busca un bloque en un punto de control
        if pc & 3:
            # Solo puede venir de fuera (los saltos comprueban el destino): parada sin cobrar gas.
            self.pc = pc
            self.instret = instret & MASK64   # también aquí envuelve: run nunca deja el contador fuera de 64 bits (ciega 4)
            self.gas = gas
            return Stop("fallo", CAUSE_INSTRUCTION_MISALIGNED, pc) if gas else Stop("gas")
        source = self.interrupts
        reserve = 0   # gas apartado por el plazo del temporizador: vuelve al presupuesto al reevaluar y al salir
        try:
            while stop is None:
                # Frontera de reevaluación (H3.4): al empezar, tras un trap, tras una instrucción que avisa con _Recheck
                # y al llegar al plazo del temporizador. Sin interrupciones habilitadas en mie no hace nada más.
                gas += reserve
                reserve = 0
                horizon = self.horizon
                enabled = self.mie
                if enabled and source is not None and (self.privilege == PRIV_U or self.mstatus & MSTATUS_MIE):
                    self.instret = instret
                    pending = source.pending_bits() & enabled
                    if pending:
                        if gas < tariff:
                            stop = Stop("gas")
                            break
                        # Prioridad MEI > MSI > MTI (privilegiado §3.1.9); mepc = pc: la instrucción no se ejecutó.
                        cause = INTERRUPT_BIT | (INTERRUPT_MACHINE_EXTERNAL if pending & MIP_MEIP else
                                                 INTERRUPT_MACHINE_SOFTWARE if pending & MIP_MSIP else INTERRUPT_MACHINE_TIMER)
                        target = self.trap_target(cause)
                        if target is None:
                            stop = Stop("interrupcion", cause, 0)   # modo compatible: sin escribir nada ni cobrar gas
                            break
                        self.enter_trap(cause, 0, pc)
                        pc = target
                        gas -= tariff   # como la entrada en trap síncrona
                        lookup = accelerate
                        continue
                    if enabled & MIE_MTIE:
                        deadline = source.timer_deadline()
                        if deadline is not None:
                            # Ni el intérprete ni un bloque cruzan el plazo: el presupuesto se acota y el bloque que no
                            # cabe entero no entra (horizonte impuesto mientras haya plazo, P1.4).
                            budget = (deadline - instret) * tariff
                            if gas > budget:
                                reserve = gas - budget
                                gas = budget
                            horizon = True
                try:
                    while gas >= tariff:
                        if lookup:
                            lookup = False
                            # Camino caliente en línea: bloque cacheado y sus páginas con la misma generación.
                            block = blocks.get(pc)
                            if block is None or gens[block.page0] != block.gen0 or (block.page1 >= 0 and gens[block.page1] != block.gen1):
                                block = self._block_for(pc)
                            if block is not None and (not horizon or gas >= block.cost):
                                next_pc, executed, exit_index = block.run(regs)
                                instret += executed
                                gas -= executed * tariff
                                block_instructions += executed
                                pc = next_pc
                                lookup = exit_index == 0  # tras una salida lenta, el intérprete sigue la cola
                                continue
                        offset = pc - base
                        page = offset >> PAGE_SHIFT
                        if page != cur_page:
                            if pc > MASK32:  # pc + 4 desbordó 2³²: en RV32 el pc envuelve (siempre cambia de página)
                                pc &= MASK32
                                offset = pc - base
                                page = offset >> PAGE_SHIFT
                            if not 0 <= offset < size:
                                raise _Halt("fallo", CAUSE_INSTRUCTION_ACCESS_FAULT, pc)
                            cur_page = page
                            cur_gen = gens[page]
                            entry = pages.get(page)
                            if entry is None or entry[0] != cur_gen:
                                entry = (cur_gen, [None] * ENTRIES_PER_PAGE)
                                pages[page] = entry
                            cur_list = entry[1]
                        elif gens[page] != cur_gen:
                            cur_gen = gens[page]
                            entry = (cur_gen, [None] * ENTRIES_PER_PAGE)
                            pages[page] = entry
                            cur_list = entry[1]
                        index = (offset & 0xFFF) >> 2
                        decoded = cur_list[index]
                        if decoded is None:
                            decoded = decode(words[offset >> 2])
                            cur_list[index] = decoded
                            code[offset >> LINE_SHIFT] = 1   # la línea contiene una palabra decodificada (P1.6)
                        self.instret = instret
                        next_pc = handlers[decoded[0]](self, regs, decoded[1], decoded[2], decoded[3], decoded[4], decoded[5], pc)
                        instret += 1
                        gas -= tariff
                        if next_pc is None:
                            pc += 4
                        else:
                            pc = next_pc
                            lookup = accelerate
                    else:
                        if not reserve:   # con gas apartado, el bucle ha llegado al plazo: se reevalúa
                            stop = Stop("gas")
                except _Recheck as recheck:
                    # La instrucción ya se confirmó: se retira como en el camino normal y se reevalúa.
                    instret = instret + 1 if recheck.instret is None else recheck.instret
                    gas -= tariff
                    if recheck.next_pc is None:
                        pc += 4
                    else:
                        pc = recheck.next_pc
                        lookup = accelerate
                except _Stall:
                    stop = Stop("wfi")   # WFI sin evento posible: nada confirmado, el pc sigue en ella
                except _Halt as halt:
                    # La instrucción en pc no se confirmó. Con manejador en la RAM, entrada en trap (P1.2) y se sigue;
                    # sin él (mtvec = 0 tras el reinicio), parada como en H1/H2, sin escribir nada ni cobrar gas.
                    target = self.trap_target(halt.cause)
                    if target is None:
                        stop = Stop(halt.reason, halt.cause, halt.value)
                    else:
                        self.instret = instret
                        self.enter_trap(halt.cause, halt.value, pc)
                        pc = target
                        gas -= tariff   # la entrada en trap cobra la tarifa de una instrucción y no avanza instret
                        lookup = accelerate
        except HostStop as host:
            # El dispositivo ya ejecutó el efecto: la instrucción se confirma y luego se para.
            instret += 1
            gas -= tariff
            pc += 4
            stop = Stop(host.reason)
        finally:
            self.pc = pc & MASK32   # pc + 4 en la última palabra del espacio envuelve a 0 (RV32)
            self.instret = instret & MASK64   # el contador de Zicntr envuelve a 64 bits (snapshot y time también)
            self.gas = gas + reserve
            self.block_instructions = block_instructions
        return stop


# ── Manejadores: (hart, regs, rd, rs1, rs2, imm, raw, pc) → siguiente pc o None ──
# Sin anotaciones en cada manejador: la firma es la de ``Handler`` para todos y se comprueba en
# ``HANDLERS``; así cada definición cabe en una línea.

Handler = Callable[[Rv32Hart, list[int], int, int, int, int, int, int], "int | None"]


def _illegal(hart, regs, rd, rs1, rs2, imm, raw, pc):
    raise _Halt("fallo", CAUSE_ILLEGAL_INSTRUCTION, raw)


def _lui(hart, regs, rd, rs1, rs2, imm, raw, pc):
    if rd:
        regs[rd] = imm & MASK32
    return None


def _auipc(hart, regs, rd, rs1, rs2, imm, raw, pc):
    if rd:
        regs[rd] = (pc + imm) & MASK32
    return None


def _jal(hart, regs, rd, rs1, rs2, imm, raw, pc):
    target = (pc + imm) & MASK32
    if target & 3:
        raise _Halt("fallo", CAUSE_INSTRUCTION_MISALIGNED, target)
    if rd:
        regs[rd] = (pc + 4) & MASK32
    return target


def _jalr(hart, regs, rd, rs1, rs2, imm, raw, pc):
    target = (regs[rs1] + imm) & 0xFFFFFFFE
    if target & 3:
        raise _Halt("fallo", CAUSE_INSTRUCTION_MISALIGNED, target)
    if rd:
        regs[rd] = (pc + 4) & MASK32
    return target


def _branch(condition: Callable[[int, int], bool]) -> Handler:
    def handler(hart, regs, rd, rs1, rs2, imm, raw, pc):
        if condition(regs[rs1], regs[rs2]):
            target = (pc + imm) & MASK32
            if target & 3:
                raise _Halt("fallo", CAUSE_INSTRUCTION_MISALIGNED, target)
            return target
        return pc + 4   # la caída también es punto de control: el bucle vuelve a buscar un bloque
    return handler


def _signed_lt(a: int, b: int) -> bool:
    return (a - 0x100000000 if a & 0x80000000 else a) < (b - 0x100000000 if b & 0x80000000 else b)


def _lb(hart, regs, rd, rs1, rs2, imm, raw, pc):
    address = (regs[rs1] + imm) & MASK32
    offset = address - hart.base
    if 0 <= offset < hart.size:
        value = hart.lanes[offset]
    else:
        value = hart._load_slow(address, 1)
    if rd:
        regs[rd] = value + 0xFFFFFF00 if value > 0x7F else value  # extensión de signo a 32 bits sin signo
    return None


def _lbu(hart, regs, rd, rs1, rs2, imm, raw, pc):
    address = (regs[rs1] + imm) & MASK32
    offset = address - hart.base
    if 0 <= offset < hart.size:
        value = hart.lanes[offset]
    else:
        value = hart._load_slow(address, 1)
    if rd:
        regs[rd] = value
    return None


def _lh(hart, regs, rd, rs1, rs2, imm, raw, pc):
    address = (regs[rs1] + imm) & MASK32
    offset = address - hart.base
    if 0 <= offset <= hart.size - 2:
        lanes = hart.lanes
        value = lanes[offset] | (lanes[offset + 1] << 8)
    else:
        value = hart._load_slow(address, 2)
    if rd:
        regs[rd] = value + 0xFFFF0000 if value > 0x7FFF else value
    return None


def _lhu(hart, regs, rd, rs1, rs2, imm, raw, pc):
    address = (regs[rs1] + imm) & MASK32
    offset = address - hart.base
    if 0 <= offset <= hart.size - 2:
        lanes = hart.lanes
        value = lanes[offset] | (lanes[offset + 1] << 8)
    else:
        value = hart._load_slow(address, 2)
    if rd:
        regs[rd] = value
    return None


def _lw(hart, regs, rd, rs1, rs2, imm, raw, pc):
    address = (regs[rs1] + imm) & MASK32
    offset = address - hart.base
    if 0 <= offset <= hart.size - 4 and not offset & 3:
        value = hart.words_u[offset >> 2]
    else:
        value = hart._load_slow(address, 4)
    if rd:
        regs[rd] = value
    return None


def _lose_reservation(hart: Rv32Hart, address: int, width: int) -> None:
    """Política de la reserva (R-9, H3.3): un store del hart que toca la palabra reservada la pierde. Los store de la vía
    rápida solo la consultan en la rama de «línea marcada»: ``LR`` marca la línea de su palabra en el mapa de código, así
    que un store que no cae en una línea marcada no puede tocarla (y los de los bloques salen al intérprete)."""
    reservation = hart.reservation
    if reservation is not None and address < reservation + 4 and reservation < address + width:
        hart.reservation = None


def _sb(hart, regs, rd, rs1, rs2, imm, raw, pc):
    address = (regs[rs1] + imm) & MASK32
    offset = address - hart.base
    if 0 <= offset < hart.size:
        hart.written[offset >> PAGE_SHIFT] = 1
        if hart.code[offset >> LINE_SHIFT]:
            hart.gens[offset >> PAGE_SHIFT] += 1
            if hart.reservation is not None:
                _lose_reservation(hart, address, 1)
        hart.lanes[offset] = regs[rs2] & 0xFF
    else:
        hart._store_slow(address, 1, regs[rs2])
    return None


def _sh(hart, regs, rd, rs1, rs2, imm, raw, pc):
    address = (regs[rs1] + imm) & MASK32
    offset = address - hart.base
    # Vía rápida solo si la media palabra no cruza línea (y por tanto tampoco página).
    if 0 <= offset <= hart.size - 2 and (offset & LINE_MASK) != LINE_MASK:
        hart.written[offset >> PAGE_SHIFT] = 1
        if hart.code[offset >> LINE_SHIFT]:
            hart.gens[offset >> PAGE_SHIFT] += 1
            if hart.reservation is not None:
                _lose_reservation(hart, address, 2)
        lanes = hart.lanes
        value = regs[rs2]
        lanes[offset] = value & 0xFF
        lanes[offset + 1] = (value >> 8) & 0xFF
    else:
        hart._store_slow(address, 2, regs[rs2])
    return None


def _sw(hart, regs, rd, rs1, rs2, imm, raw, pc):
    address = (regs[rs1] + imm) & MASK32
    offset = address - hart.base
    if 0 <= offset <= hart.size - 4 and not offset & 3:
        hart.written[offset >> PAGE_SHIFT] = 1
        if hart.code[offset >> LINE_SHIFT]:
            hart.gens[offset >> PAGE_SHIFT] += 1
            if hart.reservation is not None:
                _lose_reservation(hart, address, 4)
        hart.words_u[offset >> 2] = regs[rs2]
    else:
        hart._store_slow(address, 4, regs[rs2])
    return None


# ── Extensión A (H3.3; spec .forja/h3/spec_extension_a.md): solo sobre la RAM, palabras alineadas ──

def _atomic_offset(hart, address: int, load: bool) -> int:
    """Comprobaciones de LR/SC/AMO antes de confirmar nada: alineación a 4 (desalineado de carga o de escritura/AMO) y
    palabra entera en la RAM (fallo de acceso; los atómicos no existen en las ventanas de los dispositivos)."""
    if address & 3:
        raise _Halt("fallo", CAUSE_LOAD_MISALIGNED if load else CAUSE_STORE_MISALIGNED, address)
    offset = address - hart.base
    if not 0 <= offset <= hart.size - 4:
        raise _Halt("fallo", CAUSE_LOAD_ACCESS_FAULT if load else CAUSE_STORE_ACCESS_FAULT, address)
    return offset


def _lr_w(hart, regs, rd, rs1, rs2, imm, raw, pc):
    address = regs[rs1]
    offset = _atomic_offset(hart, address, True)
    value = hart.words_u[offset >> 2]
    hart.code[offset >> LINE_SHIFT] = 1   # la línea de la reserva: los store de los bloques sobre ella salen al intérprete
    hart.reservation = address
    if rd:
        regs[rd] = value
    return None


def _sc_w(hart, regs, rd, rs1, rs2, imm, raw, pc):
    address = regs[rs1]
    _atomic_offset(hart, address, False)
    success = hart.reservation == address   # sin fallos espurios (P1.8): solo falla sin reserva o con otra palabra
    hart.reservation = None                 # todo SC pierde la reserva, con éxito o sin él
    if success:
        hart.memory.store32(address, regs[rs2])
    if rd:
        regs[rd] = 0 if success else 1
    return None


def _amo(operation: Callable[[int, int], int]) -> Handler:
    """AMO*.W: lectura-modificación-escritura confirmada en un solo paso (memoria y después rd, P1.2); devuelve el viejo."""
    def handler(hart, regs, rd, rs1, rs2, imm, raw, pc):
        address = regs[rs1]
        offset = _atomic_offset(hart, address, False)
        old = hart.words_u[offset >> 2]
        hart.memory.store32(address, operation(old, regs[rs2]) & MASK32)   # rs2 leído antes de escribir rd
        if hart.reservation is not None:
            _lose_reservation(hart, address, 4)
        if rd:
            regs[rd] = old
        return None
    return handler


def _addi(hart, regs, rd, rs1, rs2, imm, raw, pc):
    if rd:
        regs[rd] = (regs[rs1] + imm) & MASK32
    return None


def _slti(hart, regs, rd, rs1, rs2, imm, raw, pc):
    if rd:
        a = regs[rs1]
        regs[rd] = 1 if (a - 0x100000000 if a & 0x80000000 else a) < imm else 0
    return None


def _sltiu(hart, regs, rd, rs1, rs2, imm, raw, pc):
    if rd:
        regs[rd] = 1 if regs[rs1] < (imm & MASK32) else 0
    return None


def _xori(hart, regs, rd, rs1, rs2, imm, raw, pc):
    if rd:
        regs[rd] = regs[rs1] ^ (imm & MASK32)
    return None


def _ori(hart, regs, rd, rs1, rs2, imm, raw, pc):
    if rd:
        regs[rd] = regs[rs1] | (imm & MASK32)
    return None


def _andi(hart, regs, rd, rs1, rs2, imm, raw, pc):
    if rd:
        regs[rd] = regs[rs1] & (imm & MASK32)
    return None


def _slli(hart, regs, rd, rs1, rs2, imm, raw, pc):
    if rd:
        regs[rd] = (regs[rs1] << imm) & MASK32
    return None


def _srli(hart, regs, rd, rs1, rs2, imm, raw, pc):
    if rd:
        regs[rd] = regs[rs1] >> imm
    return None


def _srai(hart, regs, rd, rs1, rs2, imm, raw, pc):
    if rd:
        a = regs[rs1]
        regs[rd] = ((a - 0x100000000 if a & 0x80000000 else a) >> imm) & MASK32
    return None


def _add(hart, regs, rd, rs1, rs2, imm, raw, pc):
    if rd:
        regs[rd] = (regs[rs1] + regs[rs2]) & MASK32
    return None


def _sub(hart, regs, rd, rs1, rs2, imm, raw, pc):
    if rd:
        regs[rd] = (regs[rs1] - regs[rs2]) & MASK32
    return None


def _sll(hart, regs, rd, rs1, rs2, imm, raw, pc):
    if rd:
        regs[rd] = (regs[rs1] << (regs[rs2] & 31)) & MASK32
    return None


def _slt(hart, regs, rd, rs1, rs2, imm, raw, pc):
    if rd:
        regs[rd] = 1 if _signed_lt(regs[rs1], regs[rs2]) else 0
    return None


def _sltu(hart, regs, rd, rs1, rs2, imm, raw, pc):
    if rd:
        regs[rd] = 1 if regs[rs1] < regs[rs2] else 0
    return None


def _xor(hart, regs, rd, rs1, rs2, imm, raw, pc):
    if rd:
        regs[rd] = regs[rs1] ^ regs[rs2]
    return None


def _srl(hart, regs, rd, rs1, rs2, imm, raw, pc):
    if rd:
        regs[rd] = regs[rs1] >> (regs[rs2] & 31)
    return None


def _sra(hart, regs, rd, rs1, rs2, imm, raw, pc):
    if rd:
        a = regs[rs1]
        regs[rd] = ((a - 0x100000000 if a & 0x80000000 else a) >> (regs[rs2] & 31)) & MASK32
    return None


def _or(hart, regs, rd, rs1, rs2, imm, raw, pc):
    if rd:
        regs[rd] = regs[rs1] | regs[rs2]
    return None


def _and(hart, regs, rd, rs1, rs2, imm, raw, pc):
    if rd:
        regs[rd] = regs[rs1] & regs[rs2]
    return None


def _m_extension(function: Callable[[int, int], int]) -> Handler:
    """Extensión M (H3.2): la función de ``rv32_alu`` sobre los dos registros; sin excepciones posibles (la división
    entre cero y el desbordamiento tienen resultado definido en la ISA)."""
    def handler(hart, regs, rd, rs1, rs2, imm, raw, pc):
        if rd:
            regs[rd] = function(regs[rs1], regs[rs2])
        return None
    return handler


def _fence(hart, regs, rd, rs1, rs2, imm, raw, pc):
    return None


def _ecall(hart, regs, rd, rs1, rs2, imm, raw, pc):
    raise _Halt("ecall", CAUSE_ECALL_FROM_U if hart.privilege == PRIV_U else CAUSE_ECALL_FROM_M, 0)


def _ebreak(hart, regs, rd, rs1, rs2, imm, raw, pc):
    # mtval = 0: la especificación solo exige la dirección si se escribe un valor no nulo (perfil fijo, P1.8).
    raise _Halt("ebreak", CAUSE_BREAKPOINT, 0)


def _mret(hart, regs, rd, rs1, rs2, imm, raw, pc):
    """MRET (privilegiado §3.3.2): solo en M. MIE ← MPIE, MPIE ← 1, MPP ← U (el menos privilegiado), MPRV ← 0 si se
    sale de M; privilegio ← MPP; la reserva de LR/SC se pierde; pc ← mepc (siempre alineado por la legalización)."""
    if hart.privilege != PRIV_M:
        raise _Halt("fallo", CAUSE_ILLEGAL_INSTRUCTION, raw)
    mstatus = hart.mstatus
    previous = (mstatus & MSTATUS_MPP) >> 11
    mstatus = (mstatus & ~(MSTATUS_MIE | MSTATUS_MPP)) | MSTATUS_MPIE | (MSTATUS_MIE if mstatus & MSTATUS_MPIE else 0)
    if previous != PRIV_M:
        mstatus &= ~MSTATUS_MPRV
    hart.mstatus = mstatus
    hart.privilege = previous
    hart.reservation = None
    raise _Recheck(hart.mepc)   # cambian MIE y el privilegio: el bucle reevalúa las interrupciones (H3.4)


def _wfi(hart, regs, rd, rs1, rs2, imm, raw, pc):
    """WFI (privilegiado §3.3.3; H3.4): ilegal en U con ``mstatus.TW`` = 1. Con una interrupción pendiente y habilitada
    en ``mie`` (aunque ``mstatus.MIE`` = 0) se retira como un NOP; si no, y el temporizador puede despertarla (MTIE en
    ``mie`` y el plazo cabe en la vuelta de 2⁶⁴ en curso), se retira adelantando ``instret`` hasta el plazo sin cobrar
    más gas que el de la propia instrucción; sin evento posible, ``Stop("wfi")`` sin confirmar nada."""
    if hart.privilege == PRIV_U and hart.mstatus & MSTATUS_TW:
        raise _Halt("fallo", CAUSE_ILLEGAL_INSTRUCTION, raw)
    source = hart.interrupts
    if source is None:
        raise _Stall()
    enabled = hart.mie
    if source.pending_bits() & enabled:
        return None
    if enabled & MIE_MTIE:
        deadline = source.timer_deadline()
        if deadline is not None:
            raise _Recheck(None, max(hart.instret + 1, deadline))
    raise _Stall()


def _zicsr(op: int, immediate: bool) -> Handler:
    """CSRRW (op 0), CSRRS (1), CSRRC (2) y sus formas inmediatas. Comprobar y confirmar (P1.2): existencia,
    privilegio y escritura de un CSR de solo lectura se comprueban antes de escribir nada; luego el CSR y por último
    ``rd``. CSRRW con ``rd`` = x0 no lee; CSRRS/CSRRC (y CSRRSI/CSRRCI) solo escriben si el índice ``rs1`` (o el
    ``uimm``) no es cero."""
    def handler(hart, regs, rd, rs1, rs2, imm, raw, pc):
        operand = rs1 if immediate else regs[rs1]   # leído antes que nada: rd puede ser rs1
        writes = op == 0 or rs1 != 0
        reader = CSR_READERS.get(imm)
        privilege = hart.privilege
        if (reader is None or (imm >> 8) & 3 > privilege or (writes and imm >> 10 == 3)
                or (privilege == PRIV_U and imm in _HPM_USER)):
            raise _Halt("fallo", CAUSE_ILLEGAL_INSTRUCTION, raw)
        old = reader(hart) if rd or op else 0
        recheck = False
        if writes:
            writer = CSR_WRITERS.get(imm)
            if writer is not None:   # los CSR sin estado del mapa ignoran la escritura (WARL de solo lectura)
                writer(hart, operand if op == 0 else (old | operand if op == 1 else old & ~operand & MASK32))
                recheck = imm == CSR_MSTATUS or imm == CSR_MIE
        if rd:
            regs[rd] = old
        if recheck:
            raise _Recheck()   # MIE de mstatus o mie pueden hacer tomable una interrupción (H3.4)
        return None
    return handler


# ── Mapa de CSR de la plataforma (H3.1; spec .forja/h3/spec_zicsr_traps.md) ──
# misa: MXL = 1 (RV32) y las extensiones implementadas (A desde H3.3, I, M desde H3.2 y U). mcounteren fijo a 0b111:
# cycle, time e instret legibles en U; los hpmcounter no.
MISA = ((1 << 30) | (1 << (ord("A") - ord("A"))) | (1 << (ord("I") - ord("A"))) | (1 << (ord("M") - ord("A")))
        | (1 << (ord("U") - ord("A"))))
MCOUNTEREN = 0b111


def _time(hart: Rv32Hart) -> int:
    return (hart.instret & MASK64) // hart.time_divisor   # sobre el contador envuelto: igual antes y después de restaurar


def _zero(hart: Rv32Hart) -> int:
    return 0


def _mip(hart: Rv32Hart) -> int:
    source = hart.interrupts
    return source.pending_bits() if source is not None else 0


CSR_READERS: dict[int, Callable[[Rv32Hart], int]] = {
    CSR_CYCLE: lambda h: h.instret & MASK32, CSR_INSTRET: lambda h: h.instret & MASK32,
    CSR_CYCLEH: lambda h: (h.instret >> 32) & MASK32, CSR_INSTRETH: lambda h: (h.instret >> 32) & MASK32,
    CSR_TIME: lambda h: _time(h) & MASK32, CSR_TIMEH: lambda h: (_time(h) >> 32) & MASK32,
    CSR_MCYCLE: lambda h: h.instret & MASK32, CSR_MINSTRET: lambda h: h.instret & MASK32,
    CSR_MCYCLEH: lambda h: (h.instret >> 32) & MASK32, CSR_MINSTRETH: lambda h: (h.instret >> 32) & MASK32,
    CSR_MSTATUS: lambda h: h.mstatus, CSR_MIE: lambda h: h.mie, CSR_MTVEC: lambda h: h.mtvec, CSR_MSCRATCH: lambda h: h.mscratch,
    CSR_MEPC: lambda h: h.mepc, CSR_MCAUSE: lambda h: h.mcause, CSR_MTVAL: lambda h: h.mtval, CSR_MIP: _mip,
    CSR_MISA: lambda h: MISA, CSR_MCOUNTEREN: lambda h: MCOUNTEREN, CSR_MSTATUSH: _zero,
    CSR_MVENDORID: _zero, CSR_MARCHID: _zero, CSR_MIMPID: _zero, CSR_MHARTID: _zero, CSR_MCONFIGPTR: _zero,
}
_HPM_USER = frozenset(range(0xC03, 0xC20)) | frozenset(range(0xC83, 0xCA0))   # ilegales en U: mcounteren[31:3] = 0
for _number in (*range(0xB03, 0xB20), *range(0xB83, 0xBA0), *range(0x323, 0x340), *_HPM_USER):
    CSR_READERS[_number] = _zero   # mhpmcounter3–31(h), mhpmevent3–31 y hpmcounter3–31(h): leen 0
# Solo los CSR con estado se escriben (con su legalización WARL); el resto del mapa ignora la escritura o, si su número
# es de solo lectura, la escritura es ilegal (lo decide el manejador).
CSR_WRITERS: dict[int, Callable[[Rv32Hart, int], None]] = {
    number: (lambda h, value, number=number, slot=slot: setattr(h, slot, legalize_csr(number, value)))
    for number, slot in CSR_SLOTS
}


HANDLERS: list[Handler] = [_illegal] * (max(int(kind) for kind in Kind) + 1)
for _kind, _handler in {
    Kind.LUI: _lui, Kind.AUIPC: _auipc, Kind.JAL: _jal, Kind.JALR: _jalr,
    Kind.BEQ: _branch(lambda a, b: a == b), Kind.BNE: _branch(lambda a, b: a != b),
    Kind.BLT: _branch(_signed_lt), Kind.BGE: _branch(lambda a, b: not _signed_lt(a, b)),
    Kind.BLTU: _branch(lambda a, b: a < b), Kind.BGEU: _branch(lambda a, b: a >= b),
    Kind.LB: _lb, Kind.LH: _lh, Kind.LW: _lw, Kind.LBU: _lbu, Kind.LHU: _lhu,
    Kind.SB: _sb, Kind.SH: _sh, Kind.SW: _sw,
    Kind.ADDI: _addi, Kind.SLTI: _slti, Kind.SLTIU: _sltiu, Kind.XORI: _xori, Kind.ORI: _ori, Kind.ANDI: _andi,
    Kind.SLLI: _slli, Kind.SRLI: _srli, Kind.SRAI: _srai,
    Kind.ADD: _add, Kind.SUB: _sub, Kind.SLL: _sll, Kind.SLT: _slt, Kind.SLTU: _sltu, Kind.XOR: _xor,
    Kind.SRL: _srl, Kind.SRA: _sra, Kind.OR: _or, Kind.AND: _and,
    Kind.MUL: _m_extension(alu.mul), Kind.MULH: _m_extension(alu.mulh), Kind.MULHSU: _m_extension(alu.mulhsu),
    Kind.MULHU: _m_extension(alu.mulhu), Kind.DIV: _m_extension(alu.div), Kind.DIVU: _m_extension(alu.divu),
    Kind.REM: _m_extension(alu.rem), Kind.REMU: _m_extension(alu.remu),
    Kind.LR_W: _lr_w, Kind.SC_W: _sc_w,
    Kind.AMOSWAP_W: _amo(lambda old, value: value), Kind.AMOADD_W: _amo(lambda old, value: old + value),
    Kind.AMOXOR_W: _amo(lambda old, value: old ^ value), Kind.AMOAND_W: _amo(lambda old, value: old & value),
    Kind.AMOOR_W: _amo(lambda old, value: old | value),
    Kind.AMOMIN_W: _amo(lambda old, value: old if alu.slt(old, value) else value),
    Kind.AMOMAX_W: _amo(lambda old, value: value if alu.slt(old, value) else old),
    Kind.AMOMINU_W: _amo(lambda old, value: min(old, value)), Kind.AMOMAXU_W: _amo(lambda old, value: max(old, value)),
    Kind.FENCE: _fence, Kind.FENCE_I: _fence, Kind.ECALL: _ecall, Kind.EBREAK: _ebreak, Kind.MRET: _mret,
    Kind.WFI: _wfi,
    Kind.CSRRW: _zicsr(0, False), Kind.CSRRS: _zicsr(1, False), Kind.CSRRC: _zicsr(2, False),
    Kind.CSRRWI: _zicsr(0, True), Kind.CSRRSI: _zicsr(1, True), Kind.CSRRCI: _zicsr(2, True),
}.items():
    HANDLERS[int(_kind)] = _handler

__all__ = ["CSR_READERS", "CSR_SLOTS", "CSR_WRITERS", "GAS_PER_INSTRUCTION", "HANDLERS", "HostStop", "MASK64", "MAX_BLOCKS",
           "MAX_RECOMPILATIONS", "MAX_SNAPSHOT_RAW_BYTES", "MCOUNTEREN", "MIE_WRITABLE", "MISA", "MSTATUS_WRITABLE", "Rv32Hart",
           "SNAPSHOT_MAGIC", "SNAPSHOT_VERSION",
           "SNAPSHOT_VERSIONS", "STATEFUL_CSRS", "Stop", "TIME_DIVISOR", "canonical_json", "decode_snapshot", "encode_snapshot",
           "expect_keys", "legalize_csr"]
