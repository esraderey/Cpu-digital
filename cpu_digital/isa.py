"""Definición del conjunto de instrucciones de CPU Digital."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from types import MappingProxyType
from typing import Literal, Mapping


OperandKind = Literal["none", "address", "immediate", "target"]


class Opcode(IntEnum):
    """Códigos binarios estables de la CPU."""

    NOP = 0

    LOAD = 10
    LOADI = 11

    ADD = 20
    ADDI = 21
    SUB = 22
    SUBI = 23
    MUL = 24
    MULI = 25
    DIV = 26
    MOD = 27

    SAVE = 30
    CMP = 32
    CMPI = 33

    JMP = 40
    JZ = 41
    JNZ = 42
    JNEG = 43
    JPOS = 44

    OUT = 50
    OUTC = 51
    IN = 52

    PUSH = 60
    POP = 61
    CALL = 62
    RET = 63

    AND = 70
    OR = 71
    XOR = 72
    NOT = 73
    SHL = 74
    SHR = 75

    BREAK = 98
    HALT = 99


@dataclass(frozen=True, slots=True)
class Instruction:
    opcode: Opcode
    mnemonic: str
    operand: OperandKind = "none"
    description: str = ""

    @property
    def words(self) -> int:
        return 1 if self.operand == "none" else 2


_INSTRUCTIONS = (
    Instruction(Opcode.NOP, "NOP", description="No realiza ninguna operación"),
    Instruction(Opcode.LOAD, "LOAD", "address", "Carga memoria en ACC"),
    Instruction(Opcode.LOADI, "LOADI", "immediate", "Carga un literal en ACC"),
    Instruction(Opcode.ADD, "ADD", "address", "Suma memoria a ACC"),
    Instruction(Opcode.ADDI, "ADDI", "immediate", "Suma un literal a ACC"),
    Instruction(Opcode.SUB, "SUB", "address", "Resta memoria a ACC"),
    Instruction(Opcode.SUBI, "SUBI", "immediate", "Resta un literal a ACC"),
    Instruction(Opcode.MUL, "MUL", "address", "Multiplica ACC por memoria"),
    Instruction(Opcode.MULI, "MULI", "immediate", "Multiplica ACC por un literal"),
    Instruction(Opcode.DIV, "DIV", "address", "División entera con memoria"),
    Instruction(Opcode.MOD, "MOD", "address", "Módulo con memoria"),
    Instruction(Opcode.SAVE, "SAVE", "address", "Guarda ACC en memoria"),
    Instruction(Opcode.CMP, "CMP", "address", "Compara ACC con memoria"),
    Instruction(Opcode.CMPI, "CMPI", "immediate", "Compara ACC con un literal"),
    Instruction(Opcode.JMP, "JMP", "target", "Salto incondicional"),
    Instruction(Opcode.JZ, "JZ", "target", "Salta si la bandera Z está activa"),
    Instruction(Opcode.JNZ, "JNZ", "target", "Salta si Z está inactiva"),
    Instruction(Opcode.JNEG, "JNEG", "target", "Salta si N está activa"),
    Instruction(Opcode.JPOS, "JPOS", "target", "Salta si el resultado es positivo"),
    Instruction(Opcode.OUT, "OUT", description="Emite ACC como número"),
    Instruction(Opcode.OUTC, "OUTC", description="Emite el byte bajo de ACC como carácter"),
    Instruction(Opcode.IN, "IN", description="Consume un valor de la cola de entrada"),
    Instruction(Opcode.PUSH, "PUSH", description="Apila ACC"),
    Instruction(Opcode.POP, "POP", description="Desapila hacia ACC"),
    Instruction(Opcode.CALL, "CALL", "target", "Llama una subrutina"),
    Instruction(Opcode.RET, "RET", description="Regresa de una subrutina"),
    Instruction(Opcode.AND, "AND", "address", "AND bit a bit con memoria"),
    Instruction(Opcode.OR, "OR", "address", "OR bit a bit con memoria"),
    Instruction(Opcode.XOR, "XOR", "address", "XOR bit a bit con memoria"),
    Instruction(Opcode.NOT, "NOT", description="Invierte los bits de ACC"),
    Instruction(Opcode.SHL, "SHL", description="Desplaza ACC un bit a la izquierda"),
    Instruction(Opcode.SHR, "SHR", description="Desplaza ACC un bit a la derecha"),
    Instruction(Opcode.BREAK, "BREAK", description="Pausa la CPU después de esta instrucción"),
    Instruction(Opcode.HALT, "HALT", description="Detiene la CPU"),
)

ISA: Mapping[int, Instruction] = MappingProxyType(
    {int(instruction.opcode): instruction for instruction in _INSTRUCTIONS}
)

MNEMONICS: Mapping[str, Instruction] = MappingProxyType(
    {instruction.mnemonic: instruction for instruction in _INSTRUCTIONS}
)

ALIASES: Mapping[str, str] = MappingProxyType(
    {
        "STORE": "SAVE",
        "PRINT": "OUT",
        "PRINTC": "OUTC",
        "INPUT": "IN",
        "JUMP": "JMP",
        "BRK": "BREAK",
    }
)


def instruction_for_mnemonic(name: str) -> Instruction | None:
    canonical = ALIASES.get(name.upper(), name.upper())
    return MNEMONICS.get(canonical)

