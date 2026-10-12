"""Aritmética de RV32I y de la extensión M sobre registros de 32 bits sin signo (RFC-EXP-00018, H1).

Dos presentaciones de la misma semántica: funciones puras, para el intérprete y las pruebas, y
plantillas de expresión (``TEMPLATES``), que el compilador de bloques incrusta en línea sin pagar
una llamada por operación. Que ambas coincidan es un criterio de aceptación (riesgo R-4 del RFC).

Fuente: RISC-V ISA Manual no privilegiado v20260120, §2.4 («Integer Computational
Instructions»: suma y resta módulo 2³², comparaciones con y sin signo, desplazamientos que solo
usan los cinco bits bajos de la cuenta) y capítulo «M Standard Extension» (productos de 64 bits,
división hacia cero, resto con el signo del dividendo, y la tabla de división entre cero y
desbordamiento: ``DIVU`` = 2³²−1, ``REMU`` = dividendo, ``DIV`` = −1, ``REM`` = dividendo;
``INT_MIN / −1``: ``DIV`` = ``INT_MIN``, ``REM`` = 0).
"""

from __future__ import annotations

from collections.abc import Callable

from .rv32_isa import MASK32, Kind

SIGN_BIT = 0x80000000

# Las funciones repiten en línea ``x - 0x100000000 if x & SIGN_BIT else x`` en vez de llamar a
# ``to_signed``: es el camino caliente del intérprete y una llamada por operando cuesta más que la
# propia operación.


def to_signed(x: int) -> int:
    """Entero de 32 bits sin signo → su valor en complemento a dos."""
    return x - 0x100000000 if x & SIGN_BIT else x


def to_unsigned(x: int) -> int:
    """Entero Python cualquiera → sus 32 bits bajos."""
    return x & MASK32


# ── RV32I ──

def add(a: int, b: int) -> int:
    return (a + b) & MASK32


def sub(a: int, b: int) -> int:
    return (a - b) & MASK32


def sll(a: int, b: int) -> int:
    return (a << (b & 31)) & MASK32


def srl(a: int, b: int) -> int:
    return a >> (b & 31)


def sra(a: int, b: int) -> int:
    return ((a - 0x100000000 if a & SIGN_BIT else a) >> (b & 31)) & MASK32


def slt(a: int, b: int) -> int:
    return 1 if (a - 0x100000000 if a & SIGN_BIT else a) < (b - 0x100000000 if b & SIGN_BIT else b) else 0


def sltu(a: int, b: int) -> int:
    return 1 if a < b else 0


def xor(a: int, b: int) -> int:
    return a ^ b


def or_(a: int, b: int) -> int:
    return a | b


def and_(a: int, b: int) -> int:
    return a & b


# ── Condiciones de salto (§2.5) ──

def beq(a: int, b: int) -> bool:
    return a == b


def bne(a: int, b: int) -> bool:
    return a != b


def blt(a: int, b: int) -> bool:
    return (a - 0x100000000 if a & SIGN_BIT else a) < (b - 0x100000000 if b & SIGN_BIT else b)


def bge(a: int, b: int) -> bool:
    return (a - 0x100000000 if a & SIGN_BIT else a) >= (b - 0x100000000 if b & SIGN_BIT else b)


def bltu(a: int, b: int) -> bool:
    return a < b


def bgeu(a: int, b: int) -> bool:
    return a >= b


# ── Extensión M ──

def mul(a: int, b: int) -> int:
    return (a * b) & MASK32


def mulh(a: int, b: int) -> int:
    return (((a - 0x100000000 if a & SIGN_BIT else a) * (b - 0x100000000 if b & SIGN_BIT else b)) >> 32) & MASK32


def mulhsu(a: int, b: int) -> int:
    return (((a - 0x100000000 if a & SIGN_BIT else a) * b) >> 32) & MASK32


def mulhu(a: int, b: int) -> int:
    return (a * b) >> 32


def div(a: int, b: int) -> int:
    """División con signo hacia cero; entre cero da −1 (todo unos); INT_MIN / −1 da INT_MIN.

    El caso de desbordamiento lo resuelve la máscara final: el cociente exacto 2³¹ queda en
    0x80000000, que es ``INT_MIN``; no quitar el ``& MASK32``.
    """
    if b == 0:
        return MASK32
    sa = a - 0x100000000 if a & SIGN_BIT else a
    sb = b - 0x100000000 if b & SIGN_BIT else b
    quotient = abs(sa) // abs(sb)
    return (-quotient if (sa < 0) != (sb < 0) else quotient) & MASK32


def divu(a: int, b: int) -> int:
    return MASK32 if b == 0 else a // b


def rem(a: int, b: int) -> int:
    """Resto con el signo del dividendo; entre cero da el dividendo; INT_MIN % −1 da 0."""
    if b == 0:
        return a
    sa = a - 0x100000000 if a & SIGN_BIT else a
    sb = b - 0x100000000 if b & SIGN_BIT else b
    remainder = abs(sa) % abs(sb)
    return (-remainder if sa < 0 else remainder) & MASK32


def remu(a: int, b: int) -> int:
    return a if b == 0 else a % b


# Contrato de los operandos: ambos en [0, 2³²); no se valida. Para las formas inmediatas el
# llamador pasa ``imm & MASK32`` (el inmediato extendido con signo a 32 bits; en ``SLTIU`` se
# compara sin signo, como manda §2.4): ``FUNCTIONS[Kind.SLTIU](0, -1)`` sería incorrecto
# (``(0, 0xFFFFFFFF)`` da 1), y con un inmediato negativo sin enmascarar ``XORI``/``ORI`` devuelven
# un negativo fuera de rango y ``SLTI`` compara mal.
FUNCTIONS: dict[Kind, Callable[[int, int], int]] = {
    Kind.ADD: add, Kind.SUB: sub, Kind.SLL: sll, Kind.SLT: slt, Kind.SLTU: sltu, Kind.XOR: xor,
    Kind.SRL: srl, Kind.SRA: sra, Kind.OR: or_, Kind.AND: and_,
    Kind.ADDI: add, Kind.SLTI: slt, Kind.SLTIU: sltu, Kind.XORI: xor, Kind.ORI: or_, Kind.ANDI: and_,
    Kind.SLLI: sll, Kind.SRLI: srl, Kind.SRAI: sra,
    Kind.MUL: mul, Kind.MULH: mulh, Kind.MULHSU: mulhsu, Kind.MULHU: mulhu,
    Kind.DIV: div, Kind.DIVU: divu, Kind.REM: rem, Kind.REMU: remu,
}
BRANCHES: dict[Kind, Callable[[int, int], bool]] = {
    Kind.BEQ: beq, Kind.BNE: bne, Kind.BLT: blt, Kind.BGE: bge, Kind.BLTU: bltu, Kind.BGEU: bgeu,
}

# Plantillas para el código generado: {a} y {b} son átomos (nombres de variables locales o
# literales) ya enmascarados a [0, 2³²); una expresión compuesta sin paréntesis o un literal
# negativo rompen la precedencia o el rango. Cada operando aparece y se evalúa exactamente una vez.
# ``M`` = 0xFFFFFFFF y ``S`` = 0x80000000 deben existir en el espacio de nombres del código
# generado. El valor con signo es ``(x ^ S) - S`` (para x < 2³¹ da x; para x ≥ 2³¹ da x − 2³²), y el
# orden con signo coincide con el orden sin signo de ``x ^ S``. División y resto no tienen
# plantilla: el bloque llama a la función o deja la instrucción al intérprete.
_SIGNED_A = "(({a} ^ S) - S)"
_SIGNED_B = "(({b} ^ S) - S)"
TEMPLATES: dict[Kind, str] = {
    Kind.ADD: "(({a} + {b}) & M)",
    Kind.SUB: "(({a} - {b}) & M)",
    Kind.SLL: "(({a} << ({b} & 31)) & M)",
    Kind.SRL: "({a} >> ({b} & 31))",
    Kind.SRA: f"(({_SIGNED_A} >> ({{b}} & 31)) & M)",
    Kind.SLT: "(1 if ({a} ^ S) < ({b} ^ S) else 0)",
    Kind.SLTU: "(1 if {a} < {b} else 0)",
    Kind.XOR: "({a} ^ {b})",
    Kind.OR: "({a} | {b})",
    Kind.AND: "({a} & {b})",
    Kind.MUL: "(({a} * {b}) & M)",
    Kind.MULH: f"((({_SIGNED_A} * {_SIGNED_B}) >> 32) & M)",
    Kind.MULHSU: f"((({_SIGNED_A} * {{b}}) >> 32) & M)",
    Kind.MULHU: "(({a} * {b}) >> 32)",
}
BRANCH_TEMPLATES: dict[Kind, str] = {
    Kind.BEQ: "({a} == {b})",
    Kind.BNE: "({a} != {b})",
    Kind.BLT: "(({a} ^ S) < ({b} ^ S))",
    Kind.BGE: "(({a} ^ S) >= ({b} ^ S))",
    Kind.BLTU: "({a} < {b})",
    Kind.BGEU: "({a} >= {b})",
}
for _imm, _reg in ((Kind.ADDI, Kind.ADD), (Kind.SLTI, Kind.SLT), (Kind.SLTIU, Kind.SLTU), (Kind.XORI, Kind.XOR),
                   (Kind.ORI, Kind.OR), (Kind.ANDI, Kind.AND), (Kind.SLLI, Kind.SLL), (Kind.SRLI, Kind.SRL),
                   (Kind.SRAI, Kind.SRA)):
    TEMPLATES[_imm] = TEMPLATES[_reg]
del _imm, _reg

__all__ = [
    "BRANCHES", "BRANCH_TEMPLATES", "FUNCTIONS", "MASK32", "SIGN_BIT", "TEMPLATES",
    "add", "and_", "beq", "bge", "bgeu", "blt", "bltu", "bne", "div", "divu", "mul", "mulh", "mulhsu", "mulhu",
    "or_", "rem", "remu", "sll", "slt", "sltu", "sra", "srl", "sub", "to_signed", "to_unsigned", "xor",
]
