"""ISA fija de cuatro palabras para Tramoya VM32."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from types import MappingProxyType
from typing import Literal, Mapping


OperandForm = Literal[
    "none",
    "reg",
    "reg_reg",
    "reg_imm",
    "reg_reg_reg",
    "reg_reg_imm",
    "reg_mem",
    "target",
    "reg_target",
    "imm",
    "imm_target",
]


class VMOpcode(IntEnum):
    NOP = 0
    MOV = 1
    MOVI = 2
    LEA = 3
    LOAD = 4
    STORE = 5

    ADD = 10
    ADDI = 11
    SUB = 12
    SUBI = 13
    MUL = 14
    MULI = 15
    DIV = 16
    MOD = 17

    CMP = 20
    CMPI = 21
    TEST = 22

    AND = 30
    OR = 31
    XOR = 32
    NOT = 33
    SHL = 34
    SHR = 35

    JMP = 40
    JZ = 41
    JNZ = 42
    JNEG = 43
    JPOS = 44
    JC = 45
    JNC = 46

    PUSH = 50
    POP = 51
    CALL = 52
    CALLR = 53
    RET = 54

    SYSCALL = 60
    INT = 61
    IRET = 62
    EI = 63
    DI = 64
    SETIV = 65
    YIELD = 66
    BREAK = 67

    HALT = 99

    # FPU — punto flotante IEEE 754 single-precision
    FADD = 100
    FSUB = 101
    FMUL = 102
    FDIV = 103
    FCMP = 104
    FTOI = 105
    ITOF = 106
    FABS = 107
    FSQRT = 108

    # Aritmética extendida 64 bits
    MULH = 110
    ADDX = 111
    SUBX = 112

    # Fibras cooperativas
    SPAWN = 120
    SWITCH = 121
    FRET = 122


@dataclass(frozen=True, slots=True)
class VMInstruction:
    opcode: VMOpcode
    mnemonic: str
    form: OperandForm
    cost: int = 1
    description: str = ""


_SPECS = (
    VMInstruction(VMOpcode.NOP, "NOP", "none"),
    VMInstruction(VMOpcode.MOV, "MOV", "reg_reg"),
    VMInstruction(VMOpcode.MOVI, "MOVI", "reg_imm"),
    VMInstruction(VMOpcode.LEA, "LEA", "reg_target"),
    VMInstruction(VMOpcode.LOAD, "LOAD", "reg_mem", 2),
    VMInstruction(VMOpcode.STORE, "STORE", "reg_mem", 2),
    VMInstruction(VMOpcode.ADD, "ADD", "reg_reg_reg"),
    VMInstruction(VMOpcode.ADDI, "ADDI", "reg_reg_imm"),
    VMInstruction(VMOpcode.SUB, "SUB", "reg_reg_reg"),
    VMInstruction(VMOpcode.SUBI, "SUBI", "reg_reg_imm"),
    VMInstruction(VMOpcode.MUL, "MUL", "reg_reg_reg", 2),
    VMInstruction(VMOpcode.MULI, "MULI", "reg_reg_imm", 2),
    VMInstruction(VMOpcode.DIV, "DIV", "reg_reg_reg", 4),
    VMInstruction(VMOpcode.MOD, "MOD", "reg_reg_reg", 4),
    VMInstruction(VMOpcode.CMP, "CMP", "reg_reg"),
    VMInstruction(VMOpcode.CMPI, "CMPI", "reg_imm"),
    VMInstruction(VMOpcode.TEST, "TEST", "reg_reg"),
    VMInstruction(VMOpcode.AND, "AND", "reg_reg_reg"),
    VMInstruction(VMOpcode.OR, "OR", "reg_reg_reg"),
    VMInstruction(VMOpcode.XOR, "XOR", "reg_reg_reg"),
    VMInstruction(VMOpcode.NOT, "NOT", "reg_reg"),
    VMInstruction(VMOpcode.SHL, "SHL", "reg_reg_imm"),
    VMInstruction(VMOpcode.SHR, "SHR", "reg_reg_imm"),
    VMInstruction(VMOpcode.JMP, "JMP", "target"),
    VMInstruction(VMOpcode.JZ, "JZ", "target"),
    VMInstruction(VMOpcode.JNZ, "JNZ", "target"),
    VMInstruction(VMOpcode.JNEG, "JNEG", "target"),
    VMInstruction(VMOpcode.JPOS, "JPOS", "target"),
    VMInstruction(VMOpcode.JC, "JC", "target"),
    VMInstruction(VMOpcode.JNC, "JNC", "target"),
    VMInstruction(VMOpcode.PUSH, "PUSH", "reg", 2),
    VMInstruction(VMOpcode.POP, "POP", "reg", 2),
    VMInstruction(VMOpcode.CALL, "CALL", "target", 3),
    VMInstruction(VMOpcode.CALLR, "CALLR", "reg", 3),
    VMInstruction(VMOpcode.RET, "RET", "none", 3),
    VMInstruction(VMOpcode.SYSCALL, "SYSCALL", "imm", 5),
    VMInstruction(VMOpcode.INT, "INT", "imm", 3),
    VMInstruction(VMOpcode.IRET, "IRET", "none", 3),
    VMInstruction(VMOpcode.EI, "EI", "none"),
    VMInstruction(VMOpcode.DI, "DI", "none"),
    VMInstruction(VMOpcode.SETIV, "SETIV", "imm_target", 2),
    VMInstruction(VMOpcode.YIELD, "YIELD", "none"),
    VMInstruction(VMOpcode.BREAK, "BREAK", "none"),
    VMInstruction(VMOpcode.HALT, "HALT", "none"),
    # FPU
    VMInstruction(VMOpcode.FADD, "FADD", "reg_reg_reg", 3),
    VMInstruction(VMOpcode.FSUB, "FSUB", "reg_reg_reg", 3),
    VMInstruction(VMOpcode.FMUL, "FMUL", "reg_reg_reg", 3),
    VMInstruction(VMOpcode.FDIV, "FDIV", "reg_reg_reg", 5),
    VMInstruction(VMOpcode.FCMP, "FCMP", "reg_reg", 2),
    VMInstruction(VMOpcode.FTOI, "FTOI", "reg_reg", 2),
    VMInstruction(VMOpcode.ITOF, "ITOF", "reg_reg", 2),
    VMInstruction(VMOpcode.FABS, "FABS", "reg_reg", 2),
    VMInstruction(VMOpcode.FSQRT, "FSQRT", "reg_reg", 5),
    # Aritmética extendida
    VMInstruction(VMOpcode.MULH, "MULH", "reg_reg_reg", 2),
    VMInstruction(VMOpcode.ADDX, "ADDX", "reg_reg_reg", 1),
    VMInstruction(VMOpcode.SUBX, "SUBX", "reg_reg_reg", 1),
    # Fibras
    VMInstruction(VMOpcode.SPAWN, "SPAWN", "target", 5),
    VMInstruction(VMOpcode.SWITCH, "SWITCH", "reg", 5),
    VMInstruction(VMOpcode.FRET, "FRET", "none", 3),
)

VM32_ISA: Mapping[int, VMInstruction] = MappingProxyType(
    {int(spec.opcode): spec for spec in _SPECS}
)
VM32_MNEMONICS: Mapping[str, VMInstruction] = MappingProxyType(
    {spec.mnemonic: spec for spec in _SPECS}
)

VM32_ALIASES: Mapping[str, str] = MappingProxyType(
    {
        "JE": "JZ",
        "JNE": "JNZ",
        "JLT": "JNEG",
        "JGT": "JPOS",
        "BRK": "BREAK",
        "SYS": "SYSCALL",
    }
)


def vm32_instruction(name: str) -> VMInstruction | None:
    canonical = VM32_ALIASES.get(name.upper(), name.upper())
    return VM32_MNEMONICS.get(canonical)

