"""Anclas de ``rv32_alu`` frente a un oráculo de enteros exactos y de las plantillas frente a las funciones."""

from __future__ import annotations

import ast
import random
import unittest

from cpu_digital import rv32_alu as alu
from cpu_digital.rv32_isa import Kind

M = 0xFFFFFFFF
EDGES = (0, 1, 2, 31, 32, 33, 0x7FFFFFFE, 0x7FFFFFFF, 0x80000000, 0x80000001, 0xFFFFFFFE, 0xFFFFFFFF, 0x12345678,
         0xDEADBEEF)


def s32(x: int) -> int:
    """Oráculo: complemento a dos de 32 bits, escrito desde la definición."""
    if not 0 <= x <= M:
        raise ValueError(x)
    return x - 2**32 if x >= 2**31 else x


def u32(x: int) -> int:
    return x % 2**32


def pairs(count: int, seed: int) -> list[tuple[int, int]]:
    rng = random.Random(seed)
    result = [(a, b) for a in EDGES for b in EDGES]
    for _ in range(count):
        a = rng.choice((rng.getrandbits(32), rng.getrandbits(8), rng.choice(EDGES)))
        b = rng.choice((rng.getrandbits(32), rng.getrandbits(8), rng.choice(EDGES)))
        result.append((a, b))
    return result


class Rv32iOracle(unittest.TestCase):
    def test_arithmetic_and_logic(self) -> None:
        for a, b in pairs(100_000, 11):
            self.assertEqual(alu.add(a, b), u32(a + b))
            self.assertEqual(alu.sub(a, b), u32(a - b))
            self.assertEqual(alu.xor(a, b), a ^ b)
            self.assertEqual(alu.or_(a, b), a | b)
            self.assertEqual(alu.and_(a, b), a & b)
            self.assertEqual(alu.slt(a, b), int(s32(a) < s32(b)))
            self.assertEqual(alu.sltu(a, b), int(a < b))
            shift = b % 32
            self.assertEqual(alu.sll(a, b), u32(a * 2**shift))
            self.assertEqual(alu.srl(a, b), a // 2**shift)
            self.assertEqual(alu.sra(a, b), u32(s32(a) // 2**shift))  # // redondea hacia −∞, como SRA

    def test_shifts_use_the_low_five_bits(self) -> None:
        self.assertEqual(alu.sll(0x12345678, 32), 0x12345678)
        self.assertEqual(alu.srl(0x12345678, 33), 0x12345678 >> 1)
        self.assertEqual(alu.sra(0x80000000, 1), 0xC0000000)
        self.assertEqual(alu.sra(0x80000000, 31), 0xFFFFFFFF)
        self.assertEqual(alu.sra(0x7FFFFFFF, 31), 0)
        self.assertEqual(alu.sll(1, 31), 0x80000000)
        self.assertEqual(alu.sll(1, 63), 0x80000000)

    def test_branch_conditions(self) -> None:
        for a, b in pairs(100_000, 12):
            self.assertEqual(alu.beq(a, b), a == b)
            self.assertEqual(alu.bne(a, b), a != b)
            self.assertEqual(alu.blt(a, b), s32(a) < s32(b))
            self.assertEqual(alu.bge(a, b), s32(a) >= s32(b))
            self.assertEqual(alu.bltu(a, b), a < b)
            self.assertEqual(alu.bgeu(a, b), a >= b)

    def test_signed_unsigned_are_inverse(self) -> None:
        rng = random.Random(17)
        for x in list(EDGES) + [rng.getrandbits(32) for _ in range(5_000)]:
            self.assertEqual(alu.to_unsigned(alu.to_signed(x)), x)
            self.assertEqual(alu.to_signed(x), s32(x))
        for v in [-(1 << 31), -1, 0, 1, (1 << 31) - 1] + [rng.randrange(-(1 << 31), 1 << 31) for _ in range(5_000)]:
            self.assertEqual(alu.to_signed(alu.to_unsigned(v)), v)
        self.assertEqual(alu.to_signed(0x80000000), -(1 << 31))
        self.assertEqual(alu.to_signed(0xFFFFFFFF), -1)
        self.assertEqual(alu.to_unsigned(-1), 0xFFFFFFFF)
        self.assertEqual(alu.to_unsigned(1 << 40), 0)

    def test_immediate_forms_take_the_masked_immediate(self) -> None:
        # El llamador enmascara el inmediato extendido con signo; SLTIU lo compara sin signo (§2.4).
        self.assertEqual(alu.FUNCTIONS[Kind.SLTIU](0, -1 & M), 1)
        self.assertEqual(alu.FUNCTIONS[Kind.SLTIU](0xFFFFFFFF, -1 & M), 0)
        self.assertEqual(alu.FUNCTIONS[Kind.SLTI](0xFFFFFFFE, -1 & M), 1)
        self.assertEqual(alu.FUNCTIONS[Kind.XORI](0xF, -1 & M), 0xFFFFFFF0)
        self.assertEqual(alu.FUNCTIONS[Kind.ORI](0xF, -16 & M), 0xFFFFFFFF)
        self.assertEqual(alu.FUNCTIONS[Kind.ANDI](0xF0F0F0F0, -16 & M), 0xF0F0F0F0)
        self.assertEqual(alu.FUNCTIONS[Kind.ADDI](5, -6 & M), 0xFFFFFFFF)

    def test_function_map_is_the_manual_one(self) -> None:
        expected = {
            Kind.ADD: alu.add, Kind.SUB: alu.sub, Kind.SLL: alu.sll, Kind.SLT: alu.slt, Kind.SLTU: alu.sltu,
            Kind.XOR: alu.xor, Kind.SRL: alu.srl, Kind.SRA: alu.sra, Kind.OR: alu.or_, Kind.AND: alu.and_,
            Kind.ADDI: alu.add, Kind.SLTI: alu.slt, Kind.SLTIU: alu.sltu, Kind.XORI: alu.xor, Kind.ORI: alu.or_,
            Kind.ANDI: alu.and_, Kind.SLLI: alu.sll, Kind.SRLI: alu.srl, Kind.SRAI: alu.sra,
            Kind.MUL: alu.mul, Kind.MULH: alu.mulh, Kind.MULHSU: alu.mulhsu, Kind.MULHU: alu.mulhu,
            Kind.DIV: alu.div, Kind.DIVU: alu.divu, Kind.REM: alu.rem, Kind.REMU: alu.remu,
        }
        self.assertEqual(alu.FUNCTIONS, expected)
        for kind, fn in expected.items():
            self.assertIs(alu.FUNCTIONS[kind], fn, kind.name)
        self.assertEqual(alu.BRANCHES, {Kind.BEQ: alu.beq, Kind.BNE: alu.bne, Kind.BLT: alu.blt, Kind.BGE: alu.bge,
                                        Kind.BLTU: alu.bltu, Kind.BGEU: alu.bgeu})


class MExtensionOracle(unittest.TestCase):
    def test_products_are_exact(self) -> None:
        for a, b in pairs(100_000, 13):
            self.assertEqual((alu.mulhu(a, b) << 32) | alu.mul(a, b), a * b)
            self.assertEqual(alu.mulh(a, b), u32((s32(a) * s32(b)) >> 32))
            self.assertEqual(alu.mulh(a, b) << 32 | alu.mul(a, b), (s32(a) * s32(b)) % 2**64)
            self.assertEqual(alu.mulhsu(a, b), u32((s32(a) * b) >> 32))
            # Los 64 bits bajos del producto mixto, reconstruidos desde mulhsu y mul.
            self.assertEqual(alu.mulhsu(a, b) << 32 | alu.mul(a, b), (s32(a) * b) % 2**64)

    def test_division_rounds_toward_zero_with_the_dividend_sign(self) -> None:
        for a, b in pairs(100_000, 14):
            sa, sb = s32(a), s32(b)
            if b != 0:
                self.assertEqual(alu.divu(a, b), a // b)
                self.assertEqual(alu.remu(a, b), a % b)
                self.assertEqual(a, alu.divu(a, b) * b + alu.remu(a, b))
                if not (sa == -(1 << 31) and sb == -1):
                    quotient = abs(sa) // abs(sb) * (-1 if (sa < 0) != (sb < 0) else 1)
                    remainder = sa - quotient * sb
                    self.assertEqual(alu.div(a, b), u32(quotient))
                    self.assertEqual(alu.rem(a, b), u32(remainder))
                    self.assertTrue(remainder == 0 or (remainder < 0) == (sa < 0))
                    self.assertLess(abs(remainder), abs(sb))

    def test_the_manual_table_for_zero_and_overflow(self) -> None:
        # «Semantics for division by zero and division overflow», extensión M.
        for a in EDGES:
            self.assertEqual(alu.divu(a, 0), 0xFFFFFFFF)
            self.assertEqual(alu.remu(a, 0), a)
            self.assertEqual(alu.div(a, 0), 0xFFFFFFFF)   # −1
            self.assertEqual(alu.rem(a, 0), a)
        self.assertEqual(alu.div(0x80000000, 0xFFFFFFFF), 0x80000000)  # INT_MIN / −1 = INT_MIN
        self.assertEqual(alu.rem(0x80000000, 0xFFFFFFFF), 0)
        self.assertEqual(alu.divu(0x80000000, 0xFFFFFFFF), 0)
        self.assertEqual(alu.remu(0x80000000, 0xFFFFFFFF), 0x80000000)
        self.assertEqual(alu.div(7, 0xFFFFFFFE), 0xFFFFFFFD)  # 7 / −2 = −3
        self.assertEqual(alu.rem(7, 0xFFFFFFFE), 1)
        self.assertEqual(alu.div(0xFFFFFFF9, 2), 0xFFFFFFFD)  # −7 / 2 = −3
        self.assertEqual(alu.rem(0xFFFFFFF9, 2), 0xFFFFFFFF)  # resto −1

    def test_results_stay_in_range(self) -> None:
        for a, b in pairs(5_000, 15):
            for fn in alu.FUNCTIONS.values():
                value = fn(a, b)
                self.assertIs(type(value), int, fn.__name__)
                self.assertTrue(0 <= value <= M, fn.__name__)
            for fn in alu.BRANCHES.values():
                self.assertIs(type(fn(a, b)), bool, fn.__name__)


class TemplatesMatchFunctions(unittest.TestCase):
    def test_every_template_equals_its_function(self) -> None:
        namespace = {"M": M, "S": 0x80000000}
        compiled = {kind: compile(template.format(a="a", b="b"), f"<{kind.name}>", "eval")
                    for kind, template in alu.TEMPLATES.items()}
        branches = {kind: compile(template.format(a="a", b="b"), f"<{kind.name}>", "eval")
                    for kind, template in alu.BRANCH_TEMPLATES.items()}
        for a, b in pairs(50_000, 16):
            namespace["a"], namespace["b"] = a, b
            for kind, code in compiled.items():
                got, expected = eval(code, namespace), alu.FUNCTIONS[kind](a, b)
                self.assertEqual(got, expected, kind.name)
                self.assertIs(type(got), int, kind.name)   # nunca un bool en un registro
            for kind, code in branches.items():
                got, expected = eval(code, namespace), alu.BRANCHES[kind](a, b)
                self.assertEqual(got, expected, kind.name)
                self.assertIs(type(got), bool, kind.name)

    def test_templates_cover_every_alu_kind_except_division(self) -> None:
        expected = set(alu.FUNCTIONS) - {Kind.DIV, Kind.DIVU, Kind.REM, Kind.REMU}
        self.assertEqual(set(alu.TEMPLATES), expected)
        self.assertEqual(set(alu.BRANCH_TEMPLATES), set(alu.BRANCHES))

    def test_templates_are_pure_expressions_over_names(self) -> None:
        forbidden = (ast.Call, ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.Await,
                     ast.NamedExpr, ast.Attribute, ast.Subscript)
        for template in list(alu.TEMPLATES.values()) + list(alu.BRANCH_TEMPLATES.values()):
            self.assertEqual((template.count("{a}"), template.count("{b}")), (1, 1), template)
            source = template.format(a="a", b="b")
            tree = ast.parse(source, mode="eval")
            for node in ast.walk(tree):
                self.assertNotIsInstance(node, forbidden, source)
            names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
            self.assertTrue(names <= {"M", "S", "a", "b"}, source)

    def test_each_operand_is_evaluated_exactly_once(self) -> None:
        # Criterio 5 de la especificación: con operandos con efectos, cada uno se evalúa una vez.
        # Varios pares (a < b, a > b, a == b, signos mezclados): una evaluación condicionada a los datos
        # (p. ej. un «and» que corta) no puede esconderse tras un solo par.
        counts = {"a": 0, "b": 0}
        values = {"a": 0, "b": 0}

        def probe(name: str) -> int:
            counts[name] += 1
            return values[name]

        namespace = {"M": M, "S": 0x80000000, "probe": probe}
        for a, b in ((0x80000001, 0x7FFFFFFF), (1, 5), (5, 1), (7, 7), (0, 0xFFFFFFFF), (0xFFFFFFFF, 0)):
            values["a"], values["b"] = a, b
            for kind, template in list(alu.TEMPLATES.items()) + list(alu.BRANCH_TEMPLATES.items()):
                counts["a"] = counts["b"] = 0
                got = eval(template.format(a='probe("a")', b='probe("b")'), namespace)
                self.assertEqual((counts["a"], counts["b"]), (1, 1), f"{kind.name} {a:#x} {b:#x}")
                expected = (alu.FUNCTIONS.get(kind) or alu.BRANCHES[kind])(a, b)
                self.assertEqual(got, expected, kind.name)


if __name__ == "__main__":
    unittest.main()
