"""Compilador de bloques básicos del hart RV32 (RFC-EXP-00018, P1.4, P1.5 y P1.6).

Un bloque empieza en un punto de control (el destino o la caída de un salto) y termina en la
primera instrucción de control (saltos condicionales, ``JAL``, ``JALR``: se incluyen y devuelven
el destino), en la primera de sistema, ``FENCE``, ilegal o no implementada (quedan fuera: las
ejecuta el intérprete) o a las 32 instrucciones. Se traduce a una función Python ``run(regs)``
con los registros que usa en variables locales, la aritmética de las plantillas de ``rv32_alu`` y
los accesos a memoria en línea con una guarda cada uno: si el acceso no cae en la RAM alineada
(fuera de rango, desalineado, ventana MMIO), si una escritura cae en una línea de 16 bytes
marcada como código en ``Rv32Memory.code`` (la del propio bloque o la de cualquier otra palabra
decodificada o compilada) o si un ``JALR`` apunta a un destino desalineado, el bloque toma la
**salida lenta** en esa instrucción: vuelca los registros modificados hasta ahí y devuelve
``(pc de la instrucción, ejecutadas, i + 1)``; el intérprete la ejecuta con comprobar-y-confirmar,
así que los efectos de dispositivo, los traps y la invalidación de cachés solo los produce él y
nada se ejecuta dos veces. Una escritura sobre una línea sin código solo marca su página como
escrita (``written``), sin subir la generación (refinamiento de P1.6 del 2026-10-11).

Coste del código generado (medido en E3): las vistas de la memoria (``written``, ``code``,
``words``, ``lanes``) van ligadas como argumentos por defecto y ``base``, ``size`` y las máscaras
son literales, así que el cuerpo no hace ninguna búsqueda global; solo se cargan de ``regs`` los
registros que se leen antes de escribirse y solo se vuelcan los escritos. La dirección efectiva
se calcula como ``(rX + (imm − base)) & M``: el resultado está en [0, 2³²) y la guarda es
``o <= size − ancho`` (una dirección por debajo de la base cae por encima de ``size``). La
función no queda en el espacio de nombres de ``exec`` (sin ciclo función ↔ diccionario).

Antes de usar un bloque, ``compile_block`` marca como código las líneas de todas sus
instrucciones (``Rv32Memory.mark_code``). El bloque recuerda la generación de las páginas de
4 KiB que ocupa; el hart lo descarta al entrar si alguna cambió y nunca entra si el gas no cubre
su coste (horizonte, P1.4).
"""

from __future__ import annotations

import re
from typing import Callable

from .rv32_alu import BRANCH_TEMPLATES, TEMPLATES
from .rv32_decode import decode
from .rv32_isa import Kind
from .rv32_mem import LINE_MASK, LINE_SHIFT

MAX_BLOCK = 32
MASK32 = 0xFFFFFFFF
PAGE_SHIFT = 12

_BRANCHES = frozenset({Kind.BEQ, Kind.BNE, Kind.BLT, Kind.BGE, Kind.BLTU, Kind.BGEU})
_ALU_REG = frozenset({Kind.ADD, Kind.SUB, Kind.SLL, Kind.SLT, Kind.SLTU, Kind.XOR, Kind.SRL, Kind.SRA, Kind.OR, Kind.AND})
_ALU_IMM = frozenset({Kind.ADDI, Kind.SLTI, Kind.SLTIU, Kind.XORI, Kind.ORI, Kind.ANDI})
_SHIFT_IMM = frozenset({Kind.SLLI, Kind.SRLI, Kind.SRAI})
_LOADS = {Kind.LB: 1, Kind.LBU: 1, Kind.LH: 2, Kind.LHU: 2, Kind.LW: 4}
_STORES = {Kind.SB: 1, Kind.SH: 2, Kind.SW: 4}
_STRAIGHT = _ALU_REG | _ALU_IMM | _SHIFT_IMM | frozenset(_LOADS) | frozenset(_STORES) | {Kind.LUI, Kind.AUIPC}
_INCLUDED_CONTROL = _BRANCHES | {Kind.JAL, Kind.JALR}
_READS_RS1 = _ALU_REG | _ALU_IMM | _SHIFT_IMM | frozenset(_LOADS) | frozenset(_STORES) | _BRANCHES | {Kind.JALR}
_READS_RS2 = _ALU_REG | frozenset(_STORES) | _BRANCHES
_PURE = _ALU_REG | _ALU_IMM | _SHIFT_IMM | {Kind.LUI, Kind.AUIPC}   # con rd = x0 no emiten nada ni leen
_WRITES_RD = _ALU_REG | _ALU_IMM | _SHIFT_IMM | frozenset(_LOADS) | {Kind.LUI, Kind.AUIPC, Kind.JAL, Kind.JALR}
_CONSTANTS = re.compile(r"\b[MS]\b")

Runner = Callable[[list[int]], tuple[int, int, int]]


class Block:
    __slots__ = ("pc", "length", "cost", "pages", "generations", "run", "source", "page0", "gen0", "page1", "gen1")
    # Inventario de estado (P2.5, H2.1): un bloque es contenido de la caché derivada ``Rv32Hart.blocks`` y muere con
    # ella (restore, reset, attach_memory, generación de página distinta); ningún campo entra en el snapshot.
    _RULE = "contenido de la caché derivada Rv32Hart.blocks: se descarta con ella y al cambiar la generación de sus páginas"
    STATE_FIELDS = ()
    DERIVED_FIELDS = dict.fromkeys(__slots__, _RULE)
    CONFIG_FIELDS = ()
    STATS_FIELDS = ()
    HARNESS_FIELDS = ()

    def __init__(self, pc: int, length: int, cost: int, pages: tuple[int, ...], generations: tuple[int, ...],
                 run: Runner, source: str) -> None:
        self.pc = pc
        self.length = length                 # instrucciones del bloque (el salto final incluido)
        self.cost = cost                     # gas que cuesta entero
        self.pages = pages                   # páginas de 4 KiB que ocupa (una o dos)
        self.generations = generations       # generación de cada una al compilar
        self.run = run
        self.source = source
        # Copias planas para la comprobación en línea del hart (una o dos páginas).
        self.page0, self.gen0 = pages[0], generations[0]
        self.page1, self.gen1 = (pages[1], generations[1]) if len(pages) > 1 else (-1, 0)

    def __repr__(self) -> str:
        return f"Block(pc={self.pc:#x}, length={self.length}, pages={self.pages})"


def _reg(index: int) -> str:
    return "0" if index == 0 else f"r{index}"


def _literal(expression: str) -> str:
    """Sustituye ``M`` y ``S`` de las plantillas por sus literales: sin búsquedas globales y con plegado."""
    return _CONSTANTS.sub(lambda match: "4294967295" if match.group() == "M" else "2147483648", expression)


def _offset(ra: str, imm: int, base: int) -> str:
    """Expresión del desplazamiento en la RAM: ``(rX + (imm − base)) & M``, ya en [0, 2³²)."""
    k = imm - base
    if ra == "0":
        return str(k & MASK32)
    if k == 0:
        return ra
    return f"({ra} - {-k}) & 4294967295" if k < 0 else f"({ra} + {k}) & 4294967295"


def compile_block(hart, pc: int, gas_per_instruction: int = 1, keep_source: bool = True) -> Block | None:
    """Compila el bloque que empieza en ``pc``; ``None`` si la primera instrucción no es traducible.

    Con ``keep_source=False`` el bloque no retiene el texto generado (unos 4 KB de los ~10 KB que
    ocupa un bloque de 32 instrucciones); el hart lo pide así salvo que ``keep_block_sources`` esté activo.
    """
    base, size, words = hart.base, hart.size, hart.words_u
    instructions: list[tuple[int, tuple]] = []
    address = pc
    while len(instructions) < MAX_BLOCK:
        offset = address - base
        if not 0 <= offset < size:
            break
        decoded = decode(words[offset >> 2])
        kind = Kind(decoded[0])
        if kind in _INCLUDED_CONTROL:
            if kind != Kind.JALR and ((address + decoded[4]) & MASK32) & 3:
                break  # destino desalineado, constante: el intérprete genera el fallo
            instructions.append((address, decoded))
            break
        if kind not in _STRAIGHT:
            break
        instructions.append((address, decoded))
        address += 4
    if not instructions:
        return None

    first_page = (pc - base) >> PAGE_SHIFT
    last_page = (instructions[-1][0] - base) >> PAGE_SHIFT
    pages = tuple(range(first_page, last_page + 1))
    # Las líneas del bloque son código: un store sobre ellas sale al intérprete, que invalida (P1.6).
    hart.memory.mark_code(pc - base, 4 * len(instructions))

    # Registros que se leen antes de escribirse (hay que cargarlos) y orden de escritura.
    needs_load: list[int] = []
    written_so_far: set[int] = set()
    for _, decoded in instructions:
        kind = Kind(decoded[0])
        _, rd, rs1, rs2, _, _ = decoded
        if rd == 0 and kind in _PURE:
            continue   # no se emite: tampoco hace falta cargar sus operandos
        for register, reads in ((rs1, kind in _READS_RS1), (rs2, kind in _READS_RS2)):
            if reads and register and register not in written_so_far and register not in needs_load:
                needs_load.append(register)
        if kind in _WRITES_RD and rd:
            written_so_far.add(rd)

    lines: list[str] = []
    emit = lines.append
    written: list[int] = []

    def flush(registers: list[int]) -> str:
        return "; ".join(f"regs[{r}] = r{r}" for r in registers) if registers else "pass"

    def slow_exit(index: int, pc_i: int) -> None:
        emit(f"        {flush(written)}; return ({pc_i}, {index}, {index + 1})")

    def note(register: int) -> None:
        if register and register not in written:
            written.append(register)

    count = len(instructions)
    for index, (pc_i, decoded) in enumerate(instructions):
        kind = Kind(decoded[0])
        _, rd, rs1, rs2, imm, _ = decoded
        ra, rb = _reg(rs1), _reg(rs2)
        if kind == Kind.LUI:
            if rd:
                emit(f"    r{rd} = {imm & MASK32}")
        elif kind == Kind.AUIPC:
            if rd:
                emit(f"    r{rd} = {(pc_i + imm) & MASK32}")
        elif kind in _ALU_REG:
            if rd:
                emit(f"    r{rd} = {_literal(TEMPLATES[kind].format(a=ra, b=rb))}")
        elif kind in _ALU_IMM:
            if rd:
                if kind == Kind.ADDI and rs1 == 0:
                    emit(f"    r{rd} = {imm & MASK32}")                       # li
                elif kind == Kind.ADDI and imm == 0:
                    emit(f"    r{rd} = {ra}")                                 # mv
                else:
                    emit(f"    r{rd} = {_literal(TEMPLATES[kind].format(a=ra, b=str(imm & MASK32)))}")
        elif kind in _SHIFT_IMM:
            if rd:
                if kind == Kind.SLLI:
                    emit(f"    r{rd} = ({ra} << {imm}) & 4294967295")
                elif kind == Kind.SRLI:
                    emit(f"    r{rd} = {ra} >> {imm}")
                else:
                    emit(f"    r{rd} = ((({ra} ^ 2147483648) - 2147483648) >> {imm}) & 4294967295")
        elif kind in _LOADS:
            width = _LOADS[kind]
            emit(f"    o = {_offset(ra, imm, base)}")
            if width == 4:
                emit(f"    if o <= {size - 4} and not o & 3:")
                emit(f"        r{rd} = words[o >> 2]" if rd else "        pass")
            elif width == 2:
                emit(f"    if o <= {size - 2}:")
                if not rd:
                    emit("        pass")
                elif kind == Kind.LH:
                    emit(f"        v = lanes[o] | lanes[o + 1] << 8; r{rd} = v + 4294901760 if v > 32767 else v")
                else:
                    emit(f"        r{rd} = lanes[o] | lanes[o + 1] << 8")
            else:
                emit(f"    if o <= {size - 1}:")
                if not rd:
                    emit("        pass")
                elif kind == Kind.LB:
                    emit(f"        v = lanes[o]; r{rd} = v + 4294967040 if v > 127 else v")
                else:
                    emit(f"        r{rd} = lanes[o]")
            emit("    else:")
            slow_exit(index, pc_i)
        elif kind in _STORES:
            width = _STORES[kind]
            emit(f"    o = {_offset(ra, imm, base)}")
            # Solo escribe en línea si la línea no tiene código; marca la página como escrita (snapshot).
            if width == 4:
                emit(f"    if o <= {size - 4} and not o & 3 and not code[o >> {LINE_SHIFT}]:")
                emit(f"        written[o >> 12] = 1; words[o >> 2] = {rb}")
            elif width == 2:
                emit(f"    if o <= {size - 2} and o & {LINE_MASK} != {LINE_MASK} and not code[o >> {LINE_SHIFT}]:")
                emit(f"        written[o >> 12] = 1; lanes[o] = {rb} & 255; lanes[o + 1] = ({rb} >> 8) & 255")
            else:
                emit(f"    if o <= {size - 1} and not code[o >> {LINE_SHIFT}]:")
                emit(f"        written[o >> 12] = 1; lanes[o] = {rb} & 255")
            emit("    else:")
            slow_exit(index, pc_i)
        elif kind in _BRANCHES:
            target = (pc_i + imm) & MASK32
            fallthrough = (pc_i + 4) & MASK32
            emit(f"    {flush(written)}")
            if target == fallthrough:
                emit(f"    return ({target}, {count}, 0)")
            else:
                condition = _literal(BRANCH_TEMPLATES[kind].format(a=ra, b=rb))
                emit(f"    return ({target} if {condition} else {fallthrough}, {count}, 0)")
        elif kind == Kind.JAL:
            target = (pc_i + imm) & MASK32
            if rd:
                emit(f"    r{rd} = {(pc_i + 4) & MASK32}")
                note(rd)
            emit(f"    {flush(written)}")
            emit(f"    return ({target}, {count}, 0)")
        else:  # JALR
            emit(f"    t = ({ra} + {imm}) & 4294967294")
            emit("    if t & 3:")
            slow_exit(index, pc_i)
            if rd:
                emit(f"    r{rd} = {(pc_i + 4) & MASK32}")
                note(rd)
            emit(f"    {flush(written)}")
            emit(f"    return (t, {count}, 0)")
        if kind in _STRAIGHT:
            note(rd)
    last_kind = Kind(instructions[-1][1][0])
    if last_kind not in _INCLUDED_CONTROL:
        emit(f"    {flush(written)}")
        emit(f"    return ({(instructions[-1][0] + 4) & MASK32}, {count}, 0)")

    header = "def run(regs, written=written, code=code, words=words, lanes=lanes):"
    loads = "    " + "; ".join(f"r{r} = regs[{r}]" for r in needs_load) if needs_load else "    pass"
    source = "\n".join([header, loads, *lines, ""])
    namespace = {"written": hart.written, "code": hart.code, "words": hart.words_u, "lanes": hart.lanes}
    # El código generado sale de la memoria del invitado ya decodificada, nunca de texto externo.
    exec(compile(source, f"<bloque {pc:#x}>", "exec"), namespace)
    run = namespace.pop("run")   # sin ciclo función ↔ globales: el bloque se libera por recuento
    gens = hart.gens
    return Block(pc, count, count * gas_per_instruction, pages, tuple(gens[page] for page in pages), run,
                 source if keep_source else "")


__all__ = ["Block", "MAX_BLOCK", "compile_block"]
