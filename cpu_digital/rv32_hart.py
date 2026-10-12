"""Intérprete comprobar-y-confirmar de RV32I para el hart de RFC-EXP-00018 (H1).

Cada manejador calcula, comprueba (rango de direcciones, alineación de destinos, legalidad del
CSR) y solo entonces escribe: una excepción detiene el hart con el ``pc`` en la instrucción que
la produjo, los registros y la memoria intactos y el gas sin cobrar (P1.2). No hay checkpoint ni
journal. En H1 no hay traps: la parada lleva la causa de ``mcause`` y el ``tval`` con los que H3
entrará en ``mtvec``.

Dos contadores (P1.3): ``gas`` mide el coste del host (tarifa provisional de 1 por instrucción)
e ``instret`` las instrucciones confirmadas del invitado; ``cycle`` = ``instret`` hasta que P3
aporte el modelo de tiempo y ``time`` = ``instret`` // ``TIME_DIVISOR``.

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
"""

from __future__ import annotations

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
from .rv32_mem import LINE_MASK, LINE_SHIFT, AccessFault, Rv32Memory

MASK32 = 0xFFFFFFFF
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
    __slots__ = ("memory", "bus", "regs", "pc", "instret", "gas", "decode_cache", "time_divisor",
                 "base", "size", "lanes", "words_u", "gens", "written", "code",
                 "accelerate", "horizon", "warmup", "blocks", "block_hits", "block_instructions", "compilations",
                 "recompilations", "keep_block_sources", "max_blocks", "cache_flushes")

    def __init__(self, memory: Rv32Memory, bus: object | None = None, time_divisor: int = TIME_DIVISOR) -> None:
        if type(time_divisor) is not int or time_divisor < 1:
            raise ValueError("time_divisor debe ser un entero positivo")
        self.bus = bus
        self.regs: list[int] = [0] * 32
        self.instret = 0
        self.gas = 0
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
        if csr == CSR_TIME:
            return (self.instret // self.time_divisor) & MASK32
        if csr == CSR_TIMEH:
            return ((self.instret // self.time_divisor) >> 32) & MASK32
        raise _Halt("fallo", CAUSE_ILLEGAL_INSTRUCTION, raw)

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
            self.instret = instret
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

__all__ = ["GAS_PER_INSTRUCTION", "HANDLERS", "HostStop", "MAX_BLOCKS", "MAX_RECOMPILATIONS", "Rv32Hart", "Stop",
           "TIME_DIVISOR"]
