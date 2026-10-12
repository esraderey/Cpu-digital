"""Decodificador y desensamblador de RV32IMA, Zifencei y Zicsr (RFC-EXP-00018, H1).

``decode(word)`` devuelve una tupla plana ``(kind, rd, rs1, rs2, imm, word)``; ``kind`` es un
entero de :class:`cpu_digital.rv32_isa.Kind` y ``imm`` un entero Python con signo en las
unidades de ``rv32_isa.encode`` (``shamt`` en los desplazamientos inmediatos, número de CSR en
Zicsr, ``fm/pred/succ`` en ``FENCE``/``FENCE.I``, ``aq << 1 | rl`` en las atómicas). Los campos
que el formato no tiene salen a cero, salvo en ``FENCE`` y ``FENCE.I``, que devuelven ``rd`` y
``rs1`` crudos (campos reservados que el manual manda ignorar); en ``ILLEGAL`` salen los bits
crudos de ``rd``, ``rs1`` y ``rs2`` y ``imm`` = 0. Las codificaciones no asignadas devuelven
``Kind.ILLEGAL``, nunca una excepción: el hart decide el trap. ``disassemble(word, pc)`` imprime
como ``llvm-objdump`` (alias incluidos) con un espacio entre mnemónico y operandos y sin el
sufijo ``<símbolo+desplazamiento>`` (la prueba del dorado normaliza esas dos cosas), para trazas y
para la prueba contra el desensamblado literal de los binarios versionados.

Lista cerrada de diferencias con ``llvm-objdump -d --mattr=+m,+a,+zicsr,+zifencei`` (LLVM 21.1),
comprobada sobre el espacio completo de opcode × funct3 × funct7 y 500 000 palabras aleatorias
(revisión ciega 2 de H1; ninguna afecta a la ejecución):

1. Nombres de CSR: solo los 23 CSR que la plataforma nombra (``CSR_NAMES``: contadores de Zicntr y
   los de modo M que H3 implementará) salen con nombre; el resto, en hexadecimal (``csrr a0, 0x306``
   donde objdump dice ``mcounteren``).
2. ``FENCE`` con campos reservados (``fm`` ∉ {0, 8}, ``fm`` = 8 sin ``rw, rw``, ``rd`` o ``rs1`` ≠ 0) y
   ``FENCE.I`` con ``rd``, ``rs1`` o ``imm`` ≠ 0: aquí se decodifican y se imprimen como el fence
   equivalente, porque §2.7 y Zifencei mandan ignorar esos campos; objdump imprime ``<unknown>``.
3. ``sret``, ``sfence.vma`` y ``dret`` son ``ILLEGAL`` (``<unknown>``): no hay modo S ni D.
4. Palabras con los dos bits bajos ≠ 11 (comprimidas) y prefijos de instrucción larga (bits bajos
   ``11111``, incluido ``1111111``): aquí ``<unknown>`` palabra a palabra; objdump consume 2 bytes
   en las comprimidas, 6 u 8 con los prefijos ``0x1F``/``0x5F`` y ``0x3F``, de 10 a 22 con ``0x7F``
   según ``funct3`` (y 1 byte con ``funct3`` = 7), así que resincroniza distinto.

Fuente de los campos: manual no privilegiado v20260120, §2.2 (formatos), §2.3 (inmediatos),
§2.4–§2.8 (RV32I), capítulos de M, A, Zifencei y Zicsr; privilegiado §3.3 (``MRET``, ``WFI``).
"""

from __future__ import annotations

from .rv32_isa import (
    ABI_NAMES,
    BY_KIND,
    CSR_NAMES,
    OP_AMO,
    OP_AUIPC,
    OP_BRANCH,
    OP_JAL,
    OP_JALR,
    OP_LOAD,
    OP_LUI,
    OP_MISC_MEM,
    OP_OP,
    OP_OP_IMM,
    OP_STORE,
    OP_SYSTEM,
    Kind,
    sign_extend,
)

Decoded = tuple[int, int, int, int, int, int]

ILLEGAL = int(Kind.ILLEGAL)
_LOADS = {0: Kind.LB, 1: Kind.LH, 2: Kind.LW, 4: Kind.LBU, 5: Kind.LHU}
_STORES = {0: Kind.SB, 1: Kind.SH, 2: Kind.SW}
_BRANCHES = {0: Kind.BEQ, 1: Kind.BNE, 4: Kind.BLT, 5: Kind.BGE, 6: Kind.BLTU, 7: Kind.BGEU}
_OP_IMM = {0: Kind.ADDI, 2: Kind.SLTI, 3: Kind.SLTIU, 4: Kind.XORI, 6: Kind.ORI, 7: Kind.ANDI}
# Clave entera funct3 << 7 | funct7: sin construir una tupla por decodificación.
_OP = {funct3 << 7 | funct7: kind for (funct3, funct7), kind in {
    (0, 0x00): Kind.ADD, (0, 0x20): Kind.SUB, (1, 0x00): Kind.SLL, (2, 0x00): Kind.SLT, (3, 0x00): Kind.SLTU,
    (4, 0x00): Kind.XOR, (5, 0x00): Kind.SRL, (5, 0x20): Kind.SRA, (6, 0x00): Kind.OR, (7, 0x00): Kind.AND,
    (0, 0x01): Kind.MUL, (1, 0x01): Kind.MULH, (2, 0x01): Kind.MULHSU, (3, 0x01): Kind.MULHU,
    (4, 0x01): Kind.DIV, (5, 0x01): Kind.DIVU, (6, 0x01): Kind.REM, (7, 0x01): Kind.REMU,
}.items()}
_CSR = {1: Kind.CSRRW, 2: Kind.CSRRS, 3: Kind.CSRRC, 5: Kind.CSRRWI, 6: Kind.CSRRSI, 7: Kind.CSRRCI}
_SYSTEM = {0x000: Kind.ECALL, 0x001: Kind.EBREAK, 0x302: Kind.MRET, 0x105: Kind.WFI}
_AMO = {
    0x02: Kind.LR_W, 0x03: Kind.SC_W, 0x01: Kind.AMOSWAP_W, 0x00: Kind.AMOADD_W, 0x04: Kind.AMOXOR_W,
    0x0C: Kind.AMOAND_W, 0x08: Kind.AMOOR_W, 0x10: Kind.AMOMIN_W, 0x14: Kind.AMOMAX_W, 0x18: Kind.AMOMINU_W,
    0x1C: Kind.AMOMAXU_W,
}


def decode(word: int) -> Decoded:
    """Decodifica una palabra de 32 bits; nunca lanza."""
    word &= 0xFFFFFFFF
    opcode = word & 0x7F
    rd = (word >> 7) & 0x1F
    funct3 = (word >> 12) & 0x7
    rs1 = (word >> 15) & 0x1F
    rs2 = (word >> 20) & 0x1F
    funct7 = word >> 25
    if opcode == OP_OP_IMM:
        if funct3 == 1 or funct3 == 5:
            if funct7 == 0x00:
                kind = Kind.SLLI if funct3 == 1 else Kind.SRLI
            elif funct7 == 0x20 and funct3 == 5:
                kind = Kind.SRAI
            else:
                return (ILLEGAL, rd, rs1, rs2, 0, word)
            return (int(kind), rd, rs1, 0, rs2, word)  # imm = shamt
        return (int(_OP_IMM[funct3]), rd, rs1, 0, sign_extend(word >> 20, 12), word)
    if opcode == OP_OP:
        kind = _OP.get(funct3 << 7 | funct7)
        if kind is None:
            return (ILLEGAL, rd, rs1, rs2, 0, word)
        return (int(kind), rd, rs1, rs2, 0, word)
    if opcode == OP_LOAD:
        kind = _LOADS.get(funct3)
        if kind is None:
            return (ILLEGAL, rd, rs1, rs2, 0, word)
        return (int(kind), rd, rs1, 0, sign_extend(word >> 20, 12), word)
    if opcode == OP_STORE:
        kind = _STORES.get(funct3)
        if kind is None:
            return (ILLEGAL, rd, rs1, rs2, 0, word)
        imm = sign_extend((funct7 << 5) | rd, 12)
        return (int(kind), 0, rs1, rs2, imm, word)
    if opcode == OP_BRANCH:
        kind = _BRANCHES.get(funct3)
        if kind is None:
            return (ILLEGAL, rd, rs1, rs2, 0, word)
        imm = sign_extend(((word >> 31) << 12) | (((word >> 7) & 1) << 11) | (((word >> 25) & 0x3F) << 5)
                          | (((word >> 8) & 0xF) << 1), 13)
        return (int(kind), 0, rs1, rs2, imm, word)
    if opcode == OP_LUI or opcode == OP_AUIPC:
        kind = Kind.LUI if opcode == OP_LUI else Kind.AUIPC
        return (int(kind), rd, 0, 0, sign_extend(word & 0xFFFFF000, 32), word)
    if opcode == OP_JAL:
        imm = sign_extend(((word >> 31) << 20) | (((word >> 12) & 0xFF) << 12) | (((word >> 20) & 1) << 11)
                          | (((word >> 21) & 0x3FF) << 1), 21)
        return (int(Kind.JAL), rd, 0, 0, imm, word)
    if opcode == OP_JALR:
        if funct3 != 0:
            return (ILLEGAL, rd, rs1, rs2, 0, word)
        return (int(Kind.JALR), rd, rs1, 0, sign_extend(word >> 20, 12), word)
    if opcode == OP_SYSTEM:
        if funct3 == 0:
            kind = _SYSTEM.get(word >> 20)
            if kind is None or rd or rs1:
                return (ILLEGAL, rd, rs1, rs2, 0, word)
            return (int(kind), 0, 0, 0, 0, word)
        kind = _CSR.get(funct3)
        if kind is None:
            return (ILLEGAL, rd, rs1, rs2, 0, word)
        return (int(kind), rd, rs1, 0, word >> 20, word)  # imm = csr; rs1 = registro o uimm
    if opcode == OP_MISC_MEM:
        # §2.7 y Zifencei: los campos no usados (imm[11:0], rs1, rd) están reservados para fences más
        # finos; una implementación base «debe ignorarlos», así que se decodifican, no se rechazan.
        if funct3 == 0:
            return (int(Kind.FENCE), rd, rs1, 0, word >> 20, word)
        if funct3 == 1:
            return (int(Kind.FENCE_I), rd, rs1, 0, word >> 20, word)
        return (ILLEGAL, rd, rs1, rs2, 0, word)
    if opcode == OP_AMO:
        if funct3 != 2:
            return (ILLEGAL, rd, rs1, rs2, 0, word)
        kind = _AMO.get(funct7 >> 2)
        if kind is None or (kind == Kind.LR_W and rs2 != 0):
            return (ILLEGAL, rd, rs1, rs2, 0, word)
        return (int(kind), rd, rs1, rs2, funct7 & 3, word)
    return (ILLEGAL, rd, rs1, rs2, 0, word)


# ── Desensamblado con los alias de llvm-objdump ──

def _hex(value: int) -> str:
    return f"{value:#x}"   # Python ya imprime «-0x1» para los negativos, como objdump


def _reg(index: int) -> str:
    return ABI_NAMES[index]


def _csr_name(csr: int) -> str:
    return CSR_NAMES.get(csr, _hex(csr))


_RD_CSR_ALIAS = {"cycle": "rdcycle", "time": "rdtime", "instret": "rdinstret",
                 "cycleh": "rdcycleh", "timeh": "rdtimeh", "instreth": "rdinstreth"}
_FENCE_BITS = "iorw"


def _fence_set(bits: int) -> str:
    return "".join(letter for shift, letter in zip((3, 2, 1, 0), _FENCE_BITS) if bits >> shift & 1) or "0"


def disassemble(word: int, pc: int = 0) -> str:
    """Texto de la instrucción con la sintaxis y los alias de ``llvm-objdump -d``."""
    word &= 0xFFFFFFFF
    kind, rd, rs1, rs2, imm, _ = decode(word)
    k = Kind(kind)
    if k == Kind.ILLEGAL:
        return "<unknown>"
    mnemonic = BY_KIND[k].mnemonic
    if k in (Kind.LUI, Kind.AUIPC):
        return f"{mnemonic} {_reg(rd)}, {_hex((imm >> 12) & 0xFFFFF)}"
    if k == Kind.JAL:
        target = _hex((pc + imm) & 0xFFFFFFFF)
        if rd == 0:
            return f"j {target}"
        if rd == 1:
            return f"jal {target}"
        return f"jal {_reg(rd)}, {target}"
    if k == Kind.JALR:
        # Alias de llvm-objdump: jr/jalr con «imm(rs1)» cuando hay inmediato y «rs1» cuando no.
        operand = f"{_hex(imm)}({_reg(rs1)})" if imm else _reg(rs1)
        if rd == 0:
            return "ret" if rs1 == 1 and imm == 0 else f"jr {operand}"
        if rd == 1:
            return f"jalr {operand}"
        return f"jalr {_reg(rd)}, {operand}"
    if k in (Kind.BEQ, Kind.BNE, Kind.BLT, Kind.BGE, Kind.BLTU, Kind.BGEU):
        target = _hex((pc + imm) & 0xFFFFFFFF)
        # Prioridad de alias de llvm-objdump con los dos registros a cero: blt → bltz, bge → blez.
        if rs2 == 0 and k in (Kind.BEQ, Kind.BNE, Kind.BLT):
            alias = {Kind.BEQ: "beqz", Kind.BNE: "bnez", Kind.BLT: "bltz"}[k]
            return f"{alias} {_reg(rs1)}, {target}"
        if rs1 == 0 and k in (Kind.BLT, Kind.BGE):
            return f"{'bgtz' if k == Kind.BLT else 'blez'} {_reg(rs2)}, {target}"
        if rs2 == 0 and k == Kind.BGE:
            return f"bgez {_reg(rs1)}, {target}"
        return f"{mnemonic} {_reg(rs1)}, {_reg(rs2)}, {target}"
    if k in (Kind.LB, Kind.LH, Kind.LW, Kind.LBU, Kind.LHU):
        return f"{mnemonic} {_reg(rd)}, {_hex(imm)}({_reg(rs1)})"
    if k in (Kind.SB, Kind.SH, Kind.SW):
        return f"{mnemonic} {_reg(rs2)}, {_hex(imm)}({_reg(rs1)})"
    if k == Kind.ADDI:
        if rd == 0 and rs1 == 0 and imm == 0:
            return "nop"
        if rs1 == 0:
            return f"li {_reg(rd)}, {_hex(imm)}"
        if imm == 0:
            return f"mv {_reg(rd)}, {_reg(rs1)}"
        return f"addi {_reg(rd)}, {_reg(rs1)}, {_hex(imm)}"
    if k == Kind.SLTIU and imm == 1:
        return f"seqz {_reg(rd)}, {_reg(rs1)}"
    if k == Kind.XORI and imm == -1:
        return f"not {_reg(rd)}, {_reg(rs1)}"
    if k == Kind.ANDI and imm == 255:
        return f"zext.b {_reg(rd)}, {_reg(rs1)}"
    if k in (Kind.SLTI, Kind.SLTIU, Kind.XORI, Kind.ORI, Kind.ANDI, Kind.SLLI, Kind.SRLI, Kind.SRAI):
        return f"{mnemonic} {_reg(rd)}, {_reg(rs1)}, {_hex(imm)}"
    if k == Kind.SUB and rs1 == 0:
        return f"neg {_reg(rd)}, {_reg(rs2)}"
    if k == Kind.SLTU and rs1 == 0:
        return f"snez {_reg(rd)}, {_reg(rs2)}"
    if k == Kind.SLT and rs2 == 0:
        return f"sltz {_reg(rd)}, {_reg(rs1)}"
    if k == Kind.SLT and rs1 == 0:
        return f"sgtz {_reg(rd)}, {_reg(rs2)}"
    if k in (Kind.ADD, Kind.SUB, Kind.SLL, Kind.SLT, Kind.SLTU, Kind.XOR, Kind.SRL, Kind.SRA, Kind.OR, Kind.AND,
             Kind.MUL, Kind.MULH, Kind.MULHSU, Kind.MULHU, Kind.DIV, Kind.DIVU, Kind.REM, Kind.REMU):
        return f"{mnemonic} {_reg(rd)}, {_reg(rs1)}, {_reg(rs2)}"
    if k == Kind.FENCE:
        pred, succ = (imm >> 4) & 0xF, imm & 0xF
        if (imm >> 8) == 0x8 and pred == 3 and succ == 3:
            return "fence.tso"
        if pred == 0xF and succ == 0xF:
            return "fence"
        return f"fence {_fence_set(pred)}, {_fence_set(succ)}"
    if k in (Kind.FENCE_I, Kind.ECALL, Kind.EBREAK, Kind.MRET, Kind.WFI):
        return mnemonic
    if k in (Kind.CSRRW, Kind.CSRRS, Kind.CSRRC):
        if word == 0xC0001073:  # csrrw zero, cycle, zero: la codificación canónica de «unimp»
            return "unimp"
        name = _csr_name(imm)
        if k == Kind.CSRRS and rs1 == 0:
            return f"{_RD_CSR_ALIAS[name]} {_reg(rd)}" if name in _RD_CSR_ALIAS else f"csrr {_reg(rd)}, {name}"
        if rd == 0:
            alias = {Kind.CSRRW: "csrw", Kind.CSRRS: "csrs", Kind.CSRRC: "csrc"}[k]
            return f"{alias} {name}, {_reg(rs1)}"
        return f"{mnemonic} {_reg(rd)}, {name}, {_reg(rs1)}"
    if k in (Kind.CSRRWI, Kind.CSRRSI, Kind.CSRRCI):
        name = _csr_name(imm)
        if rd == 0:
            alias = {Kind.CSRRWI: "csrwi", Kind.CSRRSI: "csrsi", Kind.CSRRCI: "csrci"}[k]
            return f"{alias} {name}, {_hex(rs1)}"
        return f"{mnemonic} {_reg(rd)}, {name}, {_hex(rs1)}"
    # Atómicas: lr.w rd, (rs1); sc.w rd, rs2, (rs1); amo*.w rd, rs2, (rs1); sufijo .aq/.rl/.aqrl.
    suffix = {0: "", 1: ".rl", 2: ".aq", 3: ".aqrl"}[imm]
    if k == Kind.LR_W:
        return f"lr.w{suffix} {_reg(rd)}, ({_reg(rs1)})"
    return f"{mnemonic}{suffix} {_reg(rd)}, {_reg(rs2)}, ({_reg(rs1)})"


__all__ = ["Decoded", "ILLEGAL", "decode", "disassemble"]
