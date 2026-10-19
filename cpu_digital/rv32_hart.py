"""Intérprete comprobar-y-confirmar de RV32I para el hart de RFC-EXP-00018 (H1).

Cada manejador calcula, comprueba (rango de direcciones, alineación de destinos, legalidad del
CSR) y solo entonces escribe: una excepción detiene el hart con el ``pc`` en la instrucción que
la produjo, los registros y la memoria intactos y el gas sin cobrar (P1.2). No hay checkpoint ni
journal. En H1 no hay traps: la parada lleva la causa de ``mcause`` y el ``tval`` con los que H3
entrará en ``mtvec``.

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

Snapshot y restauración (H2.1; P1.7, P2.3 y P2.5 del RFC). ``snapshot()`` devuelve el estado
arquitectónico y el contenido de la RAM en un diccionario serializable en JSON::

    {"version": 1,
     "hart": {"regs": [32 enteros en [0, 2³²)], "pc": entero en [0, 2³²), "instret": entero en [0, 2⁶⁴)
              (run lo envuelve a 64 bits al terminar y time/timeh/mtime se calculan sobre el contador
              envuelto, como los contadores de Zicntr),
              "csrs": {},                 # H2: sin CSR con estado propio; H3: {"0x300": valor, ...}
              "reservation": null,        # H2: siempre null; H3: dirección de LR/SC o null
              "time_divisor": entero en [1, 2⁶⁴)},
     "ram": {"base": ..., "size": ..., "page_words": 4096, "pages": [[índice, [enteros con signo]], ...]}}

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

from .rv32_decode import decode
from .rv32_isa import (
    CAUSE_BREAKPOINT,
    CAUSE_ECALL_FROM_M,
    CAUSE_ILLEGAL_INSTRUCTION,
    CAUSE_INSTRUCTION_ACCESS_FAULT,
    CAUSE_INSTRUCTION_MISALIGNED,
    CAUSE_LOAD_ACCESS_FAULT,
    CAUSE_STORE_ACCESS_FAULT,
    CSR_CYCLE,
    CSR_CYCLEH,
    CSR_INSTRET,
    CSR_INSTRETH,
    CSR_TIME,
    CSR_TIMEH,
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
# Snapshot (H2.1): versión del esquema, firma de la serialización y tope del JSON descomprimido (una RAM de
# 64 MiB entera cabe). STATEFUL_CSRS son los CSR con estado propio que entran en snapshot()["hart"]["csrs"]
# con claves hexadecimales en texto ("0x300"); en H2 no hay ninguno (los contadores de Zicntr se derivan de
# instret y del divisor) y cualquier clave se rechaza. H3 añade los de modo M.
SNAPSHOT_VERSION = 1
SNAPSHOT_MAGIC = b"RV32S\x01"
MAX_SNAPSHOT_RAW_BYTES = 256 * 1024 * 1024
STATEFUL_CSRS: frozenset[int] = frozenset()
_TOP_KEYS = frozenset({"version", "hart", "ram"})
_HART_KEYS = frozenset({"regs", "pc", "instret", "csrs", "reservation", "time_divisor"})
_RAM_KEYS = frozenset({"base", "size", "page_words", "pages"})


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
    """Por qué se detuvo ``run``: ``gas``, ``apagado``, ``reinicio``, ``fallo``, ``ebreak`` o ``ecall``.

    ``cause`` es el valor de ``mcause`` cuando la parada es un trap (``fallo``, ``ebreak``, ``ecall``) y
    −1 cuando no lo es; ``value`` es el ``mtval`` correspondiente (dirección, palabra o 0).
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


class Rv32Hart:
    __slots__ = ("memory", "bus", "regs", "pc", "instret", "gas", "decode_cache", "time_divisor", "reservation",
                 "base", "size", "lanes", "words_u", "gens", "written", "code",
                 "accelerate", "horizon", "warmup", "blocks", "block_hits", "block_instructions", "compilations",
                 "recompilations", "keep_block_sources", "max_blocks", "cache_flushes")
    # Inventario de estado (P2.5): cada slot está en exactamente una de estas listas; tests/test_rv32_snapshot.py lo
    # comprueba por introspección sobre toda clase con __slots__ de los módulos rv32_* y fija la clasificación exacta
    # (un slot nuevo sin clasificar, o clasificado de otra forma, pone la suite en rojo).
    STATE_FIELDS = ("regs", "pc", "instret", "reservation", "time_divisor")   # snapshot()["hart"], más "csrs"
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
    CONFIG_FIELDS = ("memory", "bus", "accelerate", "horizon", "warmup", "keep_block_sources", "max_blocks")
    STATS_FIELDS = ("block_instructions", "compilations", "cache_flushes")
    HARNESS_FIELDS = ("gas",)   # presupuesto que run recibe y devuelve: lo fija el arnés, nunca el snapshot

    def __init__(self, memory: Rv32Memory, bus: object | None = None, time_divisor: int = TIME_DIVISOR) -> None:
        if type(time_divisor) is not int or not 1 <= time_divisor < (1 << 64):   # el dominio del snapshot
            raise ValueError("time_divisor debe ser un entero en [1, 2⁶⁴)")
        self.bus = bus
        self.regs: list[int] = [0] * 32
        self.instret = 0
        self.gas = 0
        self.reservation = None   # reserva LR/SC: entra en el snapshot; None hasta H3
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
        calentamiento, recompilaciones y mapa de código de la memoria vacíos. No toca registros, ``pc``,
        ``instret``, gas ni las estadísticas acumuladas (``compilations``, ``block_instructions``,
        ``cache_flushes``): eso es ``reset``."""
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
        """Registros a cero (la misma lista: quien la tenga referenciada sigue viéndola), contadores a cero,
        cachés y mapa de código de la memoria vacíos."""
        self.regs[:] = [0] * 32
        self.pc = self.base if pc is None else pc
        self.instret = 0
        self.gas = 0
        self.reservation = None
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

    def read_csr(self, csr: int, raw: int = 0) -> int:
        """CSR de contadores de solo lectura (Zicntr); lo demás es instrucción ilegal en H1."""
        if csr == CSR_CYCLE or csr == CSR_INSTRET:
            return self.instret & MASK32
        if csr == CSR_CYCLEH or csr == CSR_INSTRETH:
            return (self.instret >> 32) & MASK32
        if csr == CSR_TIME:                                   # sobre el contador envuelto: igual antes y después
            return ((self.instret & MASK64) // self.time_divisor) & MASK32   # de una restauración (ciega 3)
        if csr == CSR_TIMEH:
            return (((self.instret & MASK64) // self.time_divisor) >> 32) & MASK32
        raise _Halt("fallo", CAUSE_ILLEGAL_INSTRUCTION, raw)

    # ── Snapshot y restauración (H2.1) ──

    def snapshot(self) -> dict[str, object]:
        """Estado arquitectónico del hart y contenido de la RAM, serializable en JSON (esquema en la cabecera del
        módulo). No lleva el gas, la configuración, las cachés ni las estadísticas. No modifica nada."""
        memory = self.memory
        return {
            "version": SNAPSHOT_VERSION,
            "hart": {"regs": list(self.regs), "pc": self.pc & MASK32, "instret": self.instret & MASK64, "csrs": {},
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
        ``time_divisor``, que es estado del invitado."""
        regs, pc, instret, time_divisor, pages = self._parse_snapshot(state)
        self.memory.restore_pages(pages)   # valida su entrada antes de escribir: si lanza, nada ha cambiado
        self.regs[:] = regs                # la misma lista, como reset
        self.pc = pc
        self.instret = instret
        self.reservation = None
        self.time_divisor = time_divisor
        self.decode_cache = {}
        self.blocks = {}
        self.block_hits = {}
        self.recompilations = {}
        self.memory.clear_code()

    def restore_bytes(self, data: bytes) -> None:
        self.restore(decode_snapshot(data))

    def _parse_snapshot(self, state: object) -> tuple[list[int], int, int, int, object]:
        """Valida un snapshot contra este hart sin modificarlo; devuelve los campos normalizados: ``regs`` copiada
        antes de validarse y cada clave leída una sola vez (``expect_keys`` copia cada sección), de modo que lo
        validado es lo que se restaura aunque el objeto cambie al releerlo. Las páginas las copia y valida
        ``Rv32Memory.restore_pages`` registro a registro, cada una copiada antes de medirla y validarla (también
        antes de escribir)."""
        if self.memory.buffer.closed:
            raise ValueError("La RAM del hart está cerrada")
        top = expect_keys(state, _TOP_KEYS, "raíz")
        version = top["version"]
        if type(version) is not int or version != SNAPSHOT_VERSION:
            raise ValueError("Versión de snapshot RV32 no compatible")
        hart = expect_keys(top["hart"], _HART_KEYS, "hart")
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
        csrs = hart["csrs"]
        if not isinstance(csrs, dict):
            raise ValueError("CSR inválidos en el snapshot")
        for key in csrs:
            # Una sola grafía por CSR: "0x" y tres dígitos hexadecimales en minúscula ("0x300").
            canonical = (isinstance(key, str) and len(key) == 5 and key[:2] == "0x"
                         and all(digit in "0123456789abcdef" for digit in key[2:]))
            if not canonical or int(key, 16) not in STATEFUL_CSRS:
                raise ValueError(f"CSR sin estado propio en esta implementación o clave no canónica: {key!r}")
        if hart["reservation"] is not None:
            raise ValueError("La reserva LR/SC no existe hasta H3: reservation debe ser null")
        time_divisor = hart["time_divisor"]
        if type(time_divisor) is not int or not 1 <= time_divisor < (1 << 64):
            raise ValueError("time_divisor inválido en el snapshot")
        memory = self.memory
        base, size, page_words = ram["base"], ram["size"], ram["page_words"]
        if type(base) is not int or type(size) is not int or (base, size) != (memory.base, memory.size):
            raise ValueError("El snapshot requiere otra RAM (base o tamaño distintos)")
        if type(page_words) is not int or page_words != SNAPSHOT_PAGE_WORDS:
            raise ValueError("Tamaño de página de snapshot inválido")
        return regs, pc, instret, time_divisor, ram["pages"]

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
                stop = Stop("gas")
        except _Halt as halt:
            stop = Stop(halt.reason, halt.cause, halt.value)
        except HostStop as host:
            # El dispositivo ya ejecutó el efecto: la instrucción se confirma y luego se para.
            instret += 1
            gas -= tariff
            pc += 4
            stop = Stop(host.reason)
        finally:
            self.pc = pc & MASK32   # pc + 4 en la última palabra del espacio envuelve a 0 (RV32)
            self.instret = instret & MASK64   # el contador de Zicntr envuelve a 64 bits (snapshot y time también)
            self.gas = gas
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


def _sb(hart, regs, rd, rs1, rs2, imm, raw, pc):
    address = (regs[rs1] + imm) & MASK32
    offset = address - hart.base
    if 0 <= offset < hart.size:
        hart.written[offset >> PAGE_SHIFT] = 1
        if hart.code[offset >> LINE_SHIFT]:
            hart.gens[offset >> PAGE_SHIFT] += 1
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
        hart.words_u[offset >> 2] = regs[rs2]
    else:
        hart._store_slow(address, 4, regs[rs2])
    return None


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


def _fence(hart, regs, rd, rs1, rs2, imm, raw, pc):
    return None


def _ecall(hart, regs, rd, rs1, rs2, imm, raw, pc):
    raise _Halt("ecall", CAUSE_ECALL_FROM_M, 0)


def _ebreak(hart, regs, rd, rs1, rs2, imm, raw, pc):
    raise _Halt("ebreak", CAUSE_BREAKPOINT, 0)


def _csrrs(hart, regs, rd, rs1, rs2, imm, raw, pc):
    # Con rs1 = x0 no escribe (Zicsr); los contadores son de solo lectura: escribirlos es ilegal.
    if rs1:
        raise _Halt("fallo", CAUSE_ILLEGAL_INSTRUCTION, raw)
    value = hart.read_csr(imm, raw)
    if rd:
        regs[rd] = value
    return None


def _csrrsi(hart, regs, rd, rs1, rs2, imm, raw, pc):
    if rs1:  # uimm ≠ 0 escribe
        raise _Halt("fallo", CAUSE_ILLEGAL_INSTRUCTION, raw)
    value = hart.read_csr(imm, raw)
    if rd:
        regs[rd] = value
    return None


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
    Kind.FENCE: _fence, Kind.FENCE_I: _fence, Kind.ECALL: _ecall, Kind.EBREAK: _ebreak,
    Kind.CSRRS: _csrrs, Kind.CSRRC: _csrrs, Kind.CSRRSI: _csrrsi, Kind.CSRRCI: _csrrsi,
}.items():
    HANDLERS[int(_kind)] = _handler

__all__ = ["GAS_PER_INSTRUCTION", "HANDLERS", "HostStop", "MASK64", "MAX_BLOCKS", "MAX_RECOMPILATIONS", "MAX_SNAPSHOT_RAW_BYTES",
           "Rv32Hart", "SNAPSHOT_MAGIC", "SNAPSHOT_VERSION", "STATEFUL_CSRS", "Stop", "TIME_DIVISOR", "canonical_json",
           "decode_snapshot", "encode_snapshot", "expect_keys"]
