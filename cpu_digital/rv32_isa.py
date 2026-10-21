"""Tablas, codificadores y nombres de RV32IMA, Zifencei y Zicsr para el hart de RFC-EXP-00018.

Fuente: RISC-V ISA Manual v20260120, volumen no privilegiado («RV32I Base Integer
Instruction Set», «M», «A», «Zifencei», «Zicsr» y «RV32/64G Instruction Set Listings») y
volumen privilegiado («Machine-Level ISA», tabla de ``mcause``). Las citas por sección van
junto a cada tabla. El hart de H1 ejecuta RV32I, Zifencei y la lectura de los CSR de
contadores; M, A y los CSR de modo M se decodifican desde ya para que H3 no renumere nada.

Los inmediatos se manejan como enteros Python con signo y en bytes: el de ``LUI``/``AUIPC`` ya
lleva los doce ceros bajos, el de los saltos es el desplazamiento respecto al ``pc``.
"""

from __future__ import annotations

from enum import IntEnum
from typing import NamedTuple

XLEN = 32
MASK32 = 0xFFFFFFFF

# Opcodes mayores («RV32/64G Instruction Set Listings», tabla de opcodes).
OP_LOAD = 0x03
OP_MISC_MEM = 0x0F
OP_OP_IMM = 0x13
OP_AUIPC = 0x17
OP_STORE = 0x23
OP_AMO = 0x2F
OP_OP = 0x33
OP_LUI = 0x37
OP_BRANCH = 0x63
OP_JALR = 0x67
OP_JAL = 0x6F
OP_SYSTEM = 0x73


class Kind(IntEnum):
    """Una entrada por instrucción. El orden es estable: H3 añade al final, nunca renumera."""

    ILLEGAL = 0
    LUI = 1
    AUIPC = 2
    JAL = 3
    JALR = 4
    BEQ = 5
    BNE = 6
    BLT = 7
    BGE = 8
    BLTU = 9
    BGEU = 10
    LB = 11
    LH = 12
    LW = 13
    LBU = 14
    LHU = 15
    SB = 16
    SH = 17
    SW = 18
    ADDI = 19
    SLTI = 20
    SLTIU = 21
    XORI = 22
    ORI = 23
    ANDI = 24
    SLLI = 25
    SRLI = 26
    SRAI = 27
    ADD = 28
    SUB = 29
    SLL = 30
    SLT = 31
    SLTU = 32
    XOR = 33
    SRL = 34
    SRA = 35
    OR = 36
    AND = 37
    FENCE = 38
    FENCE_I = 39
    ECALL = 40
    EBREAK = 41
    CSRRW = 42
    CSRRS = 43
    CSRRC = 44
    CSRRWI = 45
    CSRRSI = 46
    CSRRCI = 47
    MUL = 48
    MULH = 49
    MULHSU = 50
    MULHU = 51
    DIV = 52
    DIVU = 53
    REM = 54
    REMU = 55
    LR_W = 56
    SC_W = 57
    AMOSWAP_W = 58
    AMOADD_W = 59
    AMOXOR_W = 60
    AMOAND_W = 61
    AMOOR_W = 62
    AMOMIN_W = 63
    AMOMAX_W = 64
    AMOMINU_W = 65
    AMOMAXU_W = 66
    MRET = 67
    WFI = 68


class Format(IntEnum):
    TYPE_R = 0   # rd, rs1, rs2 (§2.2)
    TYPE_I = 1   # rd, rs1, imm[11:0] con signo
    TYPE_S = 2   # rs1, rs2, imm[11:0]
    TYPE_B = 3   # rs1, rs2, imm[12:1]
    TYPE_U = 4   # rd, imm[31:12]
    TYPE_J = 5   # rd, imm[20:1]
    SHIFT = 6    # rd, rs1, shamt[4:0]; imm[11:5] fija (§2.4)
    CSR = 7      # rd, rs1, csr (Zicsr)
    CSRI = 8     # rd, uimm[4:0], csr
    SYSTEM = 9   # funct12 fijo, rd = rs1 = 0 (§2.8, privilegiado §3.3)
    FENCE = 10   # rd, rs1, fm/pred/succ en imm[11:0] (§2.7); FENCE.I con funct3 = 1
    AMO = 11     # rd, rs1, rs2, aq/rl (extensión A)


class Spec(NamedTuple):
    kind: Kind
    mnemonic: str
    fmt: Format
    opcode: int
    funct3: int | None
    funct7: int | None   # funct7 en R y AMO (funct5 << 2), imm[11:5] en SHIFT, funct12 en SYSTEM


SPECS: tuple[Spec, ...] = (
    Spec(Kind.LUI, "lui", Format.TYPE_U, OP_LUI, None, None),
    Spec(Kind.AUIPC, "auipc", Format.TYPE_U, OP_AUIPC, None, None),
    Spec(Kind.JAL, "jal", Format.TYPE_J, OP_JAL, None, None),
    Spec(Kind.JALR, "jalr", Format.TYPE_I, OP_JALR, 0, None),
    Spec(Kind.BEQ, "beq", Format.TYPE_B, OP_BRANCH, 0, None),
    Spec(Kind.BNE, "bne", Format.TYPE_B, OP_BRANCH, 1, None),
    Spec(Kind.BLT, "blt", Format.TYPE_B, OP_BRANCH, 4, None),
    Spec(Kind.BGE, "bge", Format.TYPE_B, OP_BRANCH, 5, None),
    Spec(Kind.BLTU, "bltu", Format.TYPE_B, OP_BRANCH, 6, None),
    Spec(Kind.BGEU, "bgeu", Format.TYPE_B, OP_BRANCH, 7, None),
    Spec(Kind.LB, "lb", Format.TYPE_I, OP_LOAD, 0, None),
    Spec(Kind.LH, "lh", Format.TYPE_I, OP_LOAD, 1, None),
    Spec(Kind.LW, "lw", Format.TYPE_I, OP_LOAD, 2, None),
    Spec(Kind.LBU, "lbu", Format.TYPE_I, OP_LOAD, 4, None),
    Spec(Kind.LHU, "lhu", Format.TYPE_I, OP_LOAD, 5, None),
    Spec(Kind.SB, "sb", Format.TYPE_S, OP_STORE, 0, None),
    Spec(Kind.SH, "sh", Format.TYPE_S, OP_STORE, 1, None),
    Spec(Kind.SW, "sw", Format.TYPE_S, OP_STORE, 2, None),
    Spec(Kind.ADDI, "addi", Format.TYPE_I, OP_OP_IMM, 0, None),
    Spec(Kind.SLTI, "slti", Format.TYPE_I, OP_OP_IMM, 2, None),
    Spec(Kind.SLTIU, "sltiu", Format.TYPE_I, OP_OP_IMM, 3, None),
    Spec(Kind.XORI, "xori", Format.TYPE_I, OP_OP_IMM, 4, None),
    Spec(Kind.ORI, "ori", Format.TYPE_I, OP_OP_IMM, 6, None),
    Spec(Kind.ANDI, "andi", Format.TYPE_I, OP_OP_IMM, 7, None),
    Spec(Kind.SLLI, "slli", Format.SHIFT, OP_OP_IMM, 1, 0x00),
    Spec(Kind.SRLI, "srli", Format.SHIFT, OP_OP_IMM, 5, 0x00),
    Spec(Kind.SRAI, "srai", Format.SHIFT, OP_OP_IMM, 5, 0x20),
    Spec(Kind.ADD, "add", Format.TYPE_R, OP_OP, 0, 0x00),
    Spec(Kind.SUB, "sub", Format.TYPE_R, OP_OP, 0, 0x20),
    Spec(Kind.SLL, "sll", Format.TYPE_R, OP_OP, 1, 0x00),
    Spec(Kind.SLT, "slt", Format.TYPE_R, OP_OP, 2, 0x00),
    Spec(Kind.SLTU, "sltu", Format.TYPE_R, OP_OP, 3, 0x00),
    Spec(Kind.XOR, "xor", Format.TYPE_R, OP_OP, 4, 0x00),
    Spec(Kind.SRL, "srl", Format.TYPE_R, OP_OP, 5, 0x00),
    Spec(Kind.SRA, "sra", Format.TYPE_R, OP_OP, 5, 0x20),
    Spec(Kind.OR, "or", Format.TYPE_R, OP_OP, 6, 0x00),
    Spec(Kind.AND, "and", Format.TYPE_R, OP_OP, 7, 0x00),
    Spec(Kind.FENCE, "fence", Format.FENCE, OP_MISC_MEM, 0, None),
    Spec(Kind.FENCE_I, "fence.i", Format.FENCE, OP_MISC_MEM, 1, None),
    Spec(Kind.ECALL, "ecall", Format.SYSTEM, OP_SYSTEM, 0, 0x000),
    Spec(Kind.EBREAK, "ebreak", Format.SYSTEM, OP_SYSTEM, 0, 0x001),
    Spec(Kind.CSRRW, "csrrw", Format.CSR, OP_SYSTEM, 1, None),
    Spec(Kind.CSRRS, "csrrs", Format.CSR, OP_SYSTEM, 2, None),
    Spec(Kind.CSRRC, "csrrc", Format.CSR, OP_SYSTEM, 3, None),
    Spec(Kind.CSRRWI, "csrrwi", Format.CSRI, OP_SYSTEM, 5, None),
    Spec(Kind.CSRRSI, "csrrsi", Format.CSRI, OP_SYSTEM, 6, None),
    Spec(Kind.CSRRCI, "csrrci", Format.CSRI, OP_SYSTEM, 7, None),
    Spec(Kind.MUL, "mul", Format.TYPE_R, OP_OP, 0, 0x01),
    Spec(Kind.MULH, "mulh", Format.TYPE_R, OP_OP, 1, 0x01),
    Spec(Kind.MULHSU, "mulhsu", Format.TYPE_R, OP_OP, 2, 0x01),
    Spec(Kind.MULHU, "mulhu", Format.TYPE_R, OP_OP, 3, 0x01),
    Spec(Kind.DIV, "div", Format.TYPE_R, OP_OP, 4, 0x01),
    Spec(Kind.DIVU, "divu", Format.TYPE_R, OP_OP, 5, 0x01),
    Spec(Kind.REM, "rem", Format.TYPE_R, OP_OP, 6, 0x01),
    Spec(Kind.REMU, "remu", Format.TYPE_R, OP_OP, 7, 0x01),
    Spec(Kind.LR_W, "lr.w", Format.AMO, OP_AMO, 2, 0x02 << 2),
    Spec(Kind.SC_W, "sc.w", Format.AMO, OP_AMO, 2, 0x03 << 2),
    Spec(Kind.AMOSWAP_W, "amoswap.w", Format.AMO, OP_AMO, 2, 0x01 << 2),
    Spec(Kind.AMOADD_W, "amoadd.w", Format.AMO, OP_AMO, 2, 0x00 << 2),
    Spec(Kind.AMOXOR_W, "amoxor.w", Format.AMO, OP_AMO, 2, 0x04 << 2),
    Spec(Kind.AMOAND_W, "amoand.w", Format.AMO, OP_AMO, 2, 0x0C << 2),
    Spec(Kind.AMOOR_W, "amoor.w", Format.AMO, OP_AMO, 2, 0x08 << 2),
    Spec(Kind.AMOMIN_W, "amomin.w", Format.AMO, OP_AMO, 2, 0x10 << 2),
    Spec(Kind.AMOMAX_W, "amomax.w", Format.AMO, OP_AMO, 2, 0x14 << 2),
    Spec(Kind.AMOMINU_W, "amominu.w", Format.AMO, OP_AMO, 2, 0x18 << 2),
    Spec(Kind.AMOMAXU_W, "amomaxu.w", Format.AMO, OP_AMO, 2, 0x1C << 2),
    Spec(Kind.MRET, "mret", Format.SYSTEM, OP_SYSTEM, 0, 0x302),
    Spec(Kind.WFI, "wfi", Format.SYSTEM, OP_SYSTEM, 0, 0x105),
)
BY_KIND: dict[Kind, Spec] = {spec.kind: spec for spec in SPECS}
BY_MNEMONIC: dict[str, Spec] = {spec.mnemonic: spec for spec in SPECS}

# Nombres ABI (capítulo «RISC-V Assembly Programmer's Handbook»).
ABI_NAMES: tuple[str, ...] = (
    "zero", "ra", "sp", "gp", "tp", "t0", "t1", "t2", "s0", "s1", "a0", "a1", "a2", "a3", "a4", "a5",
    "a6", "a7", "s2", "s3", "s4", "s5", "s6", "s7", "s8", "s9", "s10", "s11", "t3", "t4", "t5", "t6",
)

# CSR (privilegiado, «Control and Status Registers»; no privilegiado, «Zicntr»).
CSR_CYCLE = 0xC00
CSR_TIME = 0xC01
CSR_INSTRET = 0xC02
CSR_CYCLEH = 0xC80
CSR_TIMEH = 0xC81
CSR_INSTRETH = 0xC82
CSR_MSTATUS = 0x300
CSR_MISA = 0x301
CSR_MIE = 0x304
CSR_MTVEC = 0x305
CSR_MSCRATCH = 0x340
CSR_MEPC = 0x341
CSR_MCAUSE = 0x342
CSR_MTVAL = 0x343
CSR_MIP = 0x344
CSR_MCYCLE = 0xB00
CSR_MINSTRET = 0xB02
CSR_MCYCLEH = 0xB80
CSR_MINSTRETH = 0xB82
CSR_MVENDORID = 0xF11
CSR_MARCHID = 0xF12
CSR_MIMPID = 0xF13
CSR_MHARTID = 0xF14
# H3.1: CSR del mapa de la plataforma que no tienen nombre en el desensamblado (CSR_NAMES no cambia).
CSR_MCOUNTEREN = 0x306
CSR_MSTATUSH = 0x310
CSR_MCONFIGPTR = 0xF15
CSR_NAMES: dict[int, str] = {
    CSR_CYCLE: "cycle", CSR_TIME: "time", CSR_INSTRET: "instret",
    CSR_CYCLEH: "cycleh", CSR_TIMEH: "timeh", CSR_INSTRETH: "instreth",
    CSR_MSTATUS: "mstatus", CSR_MISA: "misa", CSR_MIE: "mie", CSR_MTVEC: "mtvec",
    CSR_MSCRATCH: "mscratch", CSR_MEPC: "mepc", CSR_MCAUSE: "mcause", CSR_MTVAL: "mtval", CSR_MIP: "mip",
    CSR_MCYCLE: "mcycle", CSR_MINSTRET: "minstret", CSR_MCYCLEH: "mcycleh", CSR_MINSTRETH: "minstreth",
    CSR_MVENDORID: "mvendorid", CSR_MARCHID: "marchid", CSR_MIMPID: "mimpid", CSR_MHARTID: "mhartid",
}

# Causas de trap (privilegiado, tabla «Machine cause register (mcause) values»).
CAUSE_INSTRUCTION_MISALIGNED = 0
CAUSE_INSTRUCTION_ACCESS_FAULT = 1
CAUSE_ILLEGAL_INSTRUCTION = 2
CAUSE_BREAKPOINT = 3
CAUSE_LOAD_MISALIGNED = 4
CAUSE_LOAD_ACCESS_FAULT = 5
CAUSE_STORE_MISALIGNED = 6
CAUSE_STORE_ACCESS_FAULT = 7
CAUSE_ECALL_FROM_U = 8
CAUSE_ECALL_FROM_M = 11
INTERRUPT_BIT = 1 << 31
INTERRUPT_MACHINE_SOFTWARE = 3
INTERRUPT_MACHINE_TIMER = 7
INTERRUPT_MACHINE_EXTERNAL = 11

# Modos de privilegio (privilegiado, «Privilege Levels»): la plataforma implementa M y U.
PRIV_U = 0
PRIV_M = 3

# Campos de mstatus y de mie/mip (privilegiado §3.1.6 y §3.1.9) que la plataforma implementa (H3). En RV32 sin modo S ni
# F/V, los demás campos de mstatus son de solo lectura a cero; MPRV y TW existen porque hay modo U.
MSTATUS_MIE = 1 << 3
MSTATUS_MPIE = 1 << 7
MSTATUS_MPP = 3 << 11
MSTATUS_MPRV = 1 << 17
MSTATUS_TW = 1 << 21
MIE_MSIE = 1 << INTERRUPT_MACHINE_SOFTWARE
MIE_MTIE = 1 << INTERRUPT_MACHINE_TIMER
MIE_MEIE = 1 << INTERRUPT_MACHINE_EXTERNAL
MIP_MSIP = MIE_MSIE   # mip tiene los mismos bits que mie
MIP_MTIP = MIE_MTIE
MIP_MEIP = MIE_MEIE


def sign_extend(value: int, bits: int) -> int:
    """Interpreta los ``bits`` bajos de ``value`` como entero en complemento a dos."""
    value &= (1 << bits) - 1
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def _check_register(name: str, value: int) -> None:
    if not 0 <= value <= 31:
        raise ValueError(f"{name} fuera de rango: {value}")


def encode(kind: Kind, rd: int = 0, rs1: int = 0, rs2: int = 0, imm: int = 0) -> int:
    """Palabra de 32 bits de la instrucción ``kind`` con los campos dados.

    ``imm`` sigue las unidades de ``decode``: bytes con signo (saltos y ``LUI``/``AUIPC`` con los
    doce ceros bajos), ``shamt`` en los desplazamientos inmediatos, número de CSR en Zicsr,
    ``fm/pred/succ`` en ``FENCE`` y ``FENCE.I`` (campos reservados que el manual manda ignorar) y
    ``aq << 1 | rl`` en las atómicas. En ``CSRRWI`` y compañía ``rs1`` es el inmediato de cinco
    bits. Los tres registros deben estar en 0–31 aunque el formato no los use; los campos que el
    formato no usa se ignoran, salvo en ``ECALL``/``EBREAK``/``MRET``/``WFI``, que exigen todos a
    cero, y en ``LR.W``, cuyo ``rs2`` debe ser 0 (la codificación lo fija). Lanza ``ValueError`` si
    un campo no cabe.
    """
    spec = BY_KIND.get(kind)
    if spec is None:
        raise ValueError(f"no hay codificación para {kind!r}")
    for name, value in (("rd", rd), ("rs1", rs1), ("rs2", rs2)):
        _check_register(name, value)
    if kind == Kind.LR_W and rs2:
        raise ValueError("LR.W lleva rs2 = 0")
    word = spec.opcode
    fmt = spec.fmt
    if fmt == Format.TYPE_R:
        word |= rd << 7 | spec.funct3 << 12 | rs1 << 15 | rs2 << 20 | spec.funct7 << 25
    elif fmt == Format.TYPE_I:
        if not -2048 <= imm <= 2047:
            raise ValueError(f"inmediato I fuera de rango: {imm}")
        word |= rd << 7 | spec.funct3 << 12 | rs1 << 15 | (imm & 0xFFF) << 20
    elif fmt == Format.SHIFT:
        if not 0 <= imm <= 31:
            raise ValueError(f"shamt fuera de rango: {imm}")
        word |= rd << 7 | spec.funct3 << 12 | rs1 << 15 | imm << 20 | spec.funct7 << 25
    elif fmt == Format.TYPE_S:
        if not -2048 <= imm <= 2047:
            raise ValueError(f"inmediato S fuera de rango: {imm}")
        word |= (imm & 0x1F) << 7 | spec.funct3 << 12 | rs1 << 15 | rs2 << 20 | ((imm >> 5) & 0x7F) << 25
    elif fmt == Format.TYPE_B:
        if not -4096 <= imm <= 4094 or imm & 1:
            raise ValueError(f"desplazamiento B fuera de rango o impar: {imm}")
        word |= (((imm >> 11) & 1) << 7 | ((imm >> 1) & 0xF) << 8 | spec.funct3 << 12 | rs1 << 15 | rs2 << 20
                 | ((imm >> 5) & 0x3F) << 25 | ((imm >> 12) & 1) << 31)
    elif fmt == Format.TYPE_U:
        if not -(1 << 31) <= imm < (1 << 31) or imm & 0xFFF:
            raise ValueError(f"inmediato U fuera de rango o con bits bajos: {imm}")
        word |= rd << 7 | (imm & 0xFFFFF000)
    elif fmt == Format.TYPE_J:
        if not -(1 << 20) <= imm <= (1 << 20) - 2 or imm & 1:
            raise ValueError(f"desplazamiento J fuera de rango o impar: {imm}")
        word |= (rd << 7 | ((imm >> 12) & 0xFF) << 12 | ((imm >> 11) & 1) << 20 | ((imm >> 1) & 0x3FF) << 21
                 | ((imm >> 20) & 1) << 31)
    elif fmt in (Format.CSR, Format.CSRI):
        if not 0 <= imm <= 0xFFF:
            raise ValueError(f"número de CSR fuera de rango: {imm}")
        word |= rd << 7 | spec.funct3 << 12 | rs1 << 15 | imm << 20
    elif fmt == Format.SYSTEM:
        if rd or rs1 or rs2 or imm:
            raise ValueError(f"{spec.mnemonic} no lleva operandos")
        word |= spec.funct7 << 20
    elif fmt == Format.FENCE:
        if not 0 <= imm <= 0xFFF:
            raise ValueError(f"campo fm/pred/succ fuera de rango: {imm}")
        word |= rd << 7 | spec.funct3 << 12 | rs1 << 15 | imm << 20
    else:  # AMO
        if not 0 <= imm <= 3:
            raise ValueError(f"bits aq/rl fuera de rango: {imm}")
        word |= rd << 7 | spec.funct3 << 12 | rs1 << 15 | rs2 << 20 | imm << 25 | spec.funct7 << 25
    return word


__all__ = [
    "ABI_NAMES", "BY_KIND", "BY_MNEMONIC", "CSR_NAMES", "Format", "Kind", "MASK32", "SPECS", "Spec", "XLEN",
    "encode", "sign_extend",
] + [name for name in dir() if name.startswith(("OP_", "CSR_", "CAUSE_", "INTERRUPT_", "PRIV_", "MSTATUS_", "MIE_", "MIP_"))
       and name != "CSR_NAMES"]
