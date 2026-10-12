"""Anclas de ``rv32_isa`` y ``rv32_decode``: ida y vuelta, oráculo de inmediatos, ilegales y objdump."""

from __future__ import annotations

import random
import re
import unittest
from pathlib import Path

from cpu_digital.rv32_decode import ILLEGAL, decode, disassemble
from cpu_digital.rv32_isa import BY_KIND, MASK32, SPECS, Format, Kind, encode, sign_extend

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "data" / "coremark_rv32im_zicsr_it10.objdump.txt"
BINARY = ROOT / "benchmarks" / "rv32_arranque" / "bin" / "coremark_rv32im_zicsr_it10.bin"
TEXT_BASE = 0x80000000


def bit(word: int, index: int) -> int:
    return (word >> index) & 1


def bits(word: int, high: int, low: int) -> int:
    return (word >> low) & ((1 << (high - low + 1)) - 1)


def oracle_immediate(fmt: Format, word: int) -> int:
    """Inmediato según las tablas de bits del manual (§2.3), campo a campo y sin reutilizar el decodificador."""
    if fmt == Format.TYPE_I:
        value = bits(word, 30, 20) | bit(word, 31) << 11
        return value - (1 << 12) if bit(word, 31) else value
    if fmt == Format.TYPE_S:
        value = bits(word, 11, 7) | bits(word, 30, 25) << 5 | bit(word, 31) << 11
        return value - (1 << 12) if bit(word, 31) else value
    if fmt == Format.TYPE_B:
        value = bits(word, 11, 8) << 1 | bits(word, 30, 25) << 5 | bit(word, 7) << 11 | bit(word, 31) << 12
        return value - (1 << 13) if bit(word, 31) else value
    if fmt == Format.TYPE_U:
        value = bits(word, 31, 12) << 12
        return value - (1 << 32) if bit(word, 31) else value
    if fmt == Format.TYPE_J:
        value = bits(word, 30, 21) << 1 | bit(word, 20) << 11 | bits(word, 19, 12) << 12 | bit(word, 31) << 20
        return value - (1 << 21) if bit(word, 31) else value
    raise ValueError(fmt)


def random_fields(rng: random.Random, spec) -> dict[str, int]:
    fmt = spec.fmt
    fields = {"rd": rng.randrange(32), "rs1": rng.randrange(32), "rs2": rng.randrange(32), "imm": 0}
    if fmt == Format.TYPE_I or fmt == Format.TYPE_S:
        fields["imm"] = rng.choice((-2048, 2047, -1, 0, 1, rng.randrange(-2048, 2048)))
    elif fmt == Format.TYPE_B:
        fields["imm"] = rng.choice((-4096, 4094, 0, 2, -2, rng.randrange(-2048, 2048) * 2))
    elif fmt == Format.TYPE_U:
        fields["imm"] = rng.choice((-(1 << 31), (1 << 31) - 4096, 0, 4096, rng.randrange(-(1 << 19), 1 << 19) * 4096))
    elif fmt == Format.TYPE_J:
        fields["imm"] = rng.choice((-(1 << 20), (1 << 20) - 2, 0, 2, -2, rng.randrange(-(1 << 19), 1 << 19) * 2))
    elif fmt == Format.SHIFT:
        fields["imm"] = rng.choice((0, 31, rng.randrange(32)))
    elif fmt in (Format.CSR, Format.CSRI):
        fields["imm"] = rng.choice((0, 0xFFF, 0xC00, 0x300, rng.randrange(4096)))
    elif fmt == Format.SYSTEM:
        fields = {"rd": 0, "rs1": 0, "rs2": 0, "imm": 0}
    elif fmt == Format.FENCE:
        fields["imm"] = rng.choice((0xFF, 0x0FF, 0x833, rng.randrange(4096)))
        if spec.kind == Kind.FENCE_I:
            fields = {"rd": 0, "rs1": 0, "rs2": 0, "imm": 0}
    elif fmt == Format.AMO:
        fields["imm"] = rng.randrange(4)
        if spec.kind == Kind.LR_W:
            fields["rs2"] = 0
    if fmt in (Format.TYPE_S, Format.TYPE_B):
        fields["rd"] = 0
    if fmt in (Format.TYPE_U, Format.TYPE_J):
        fields["rs1"] = fields["rs2"] = 0
    if fmt in (Format.TYPE_I, Format.SHIFT, Format.CSR, Format.CSRI, Format.FENCE):
        fields["rs2"] = 0
    return fields


class EncodeDecodeRoundTrip(unittest.TestCase):
    def test_every_kind_round_trips(self) -> None:
        rng = random.Random(2026)
        seen: set[int] = set()
        for spec in SPECS:
            for _ in range(400):
                fields = random_fields(rng, spec)
                word = encode(spec.kind, **fields)
                kind, rd, rs1, rs2, imm, raw = decode(word)
                self.assertEqual(Kind(kind), spec.kind, f"{spec.mnemonic}: {word:#010x}")
                self.assertEqual((rd, rs1, rs2, imm, raw), (fields["rd"], fields["rs1"], fields["rs2"], fields["imm"], word),
                                 f"{spec.mnemonic}: {word:#010x} {fields}")
                seen.add(kind)
        self.assertEqual(seen, {int(spec.kind) for spec in SPECS})

    def test_immediates_match_the_manual_bit_tables(self) -> None:
        rng = random.Random(7)
        formats = {Format.TYPE_I: Kind.ADDI, Format.TYPE_S: Kind.SW, Format.TYPE_B: Kind.BEQ, Format.TYPE_U: Kind.LUI, Format.TYPE_J: Kind.JAL}
        for _ in range(20_000):
            word = rng.getrandbits(32)
            for fmt, kind in formats.items():
                spec = BY_KIND[kind]
                probe = (word & ~0x7F) | spec.opcode
                if spec.funct3 is not None:
                    probe = (probe & ~(7 << 12)) | spec.funct3 << 12
                decoded_kind, _, _, _, imm, _ = decode(probe)
                self.assertEqual(Kind(decoded_kind), kind, f"{probe:#010x}")
                self.assertEqual(imm, oracle_immediate(fmt, probe), f"{fmt.name} {probe:#010x}")

    def test_sign_extend(self) -> None:
        self.assertEqual(sign_extend(0xFFF, 12), -1)
        self.assertEqual(sign_extend(0x800, 12), -2048)
        self.assertEqual(sign_extend(0x7FF, 12), 2047)
        self.assertEqual(sign_extend(0x80000000, 32), -(1 << 31))
        self.assertEqual(sign_extend(0x1_0000_0001, 32), 1)

    def test_external_anchors_assembled_with_clang(self) -> None:
        """Palabras ensambladas con clang 21.1 (``--target=riscv32-unknown-elf -march=rv32ima_zicsr_zifencei``)."""
        anchors = {
            0xFFFFF2B7: (Kind.LUI, 5, 0, 0, -4096), 0xFFFFF297: (Kind.AUIPC, 5, 0, 0, -4096),
            0x7FFFF2EF: (Kind.JAL, 5, 0, 0, 1048574), 0x800502E7: (Kind.JALR, 5, 10, 0, -2048),
            0x7EB50FE3: (Kind.BEQ, 0, 10, 11, 4094), 0x7EB57FE3: (Kind.BGEU, 0, 10, 11, 4094),
            0x80050283: (Kind.LB, 5, 10, 0, -2048), 0x80055283: (Kind.LHU, 5, 10, 0, -2048),
            0x80550023: (Kind.SB, 0, 10, 5, -2048), 0x80552023: (Kind.SW, 0, 10, 5, -2048),
            0x80053293: (Kind.SLTIU, 5, 10, 0, -2048), 0x01F51293: (Kind.SLLI, 5, 10, 0, 31),
            0x41F55293: (Kind.SRAI, 5, 10, 0, 31), 0x41F502B3: (Kind.SUB, 5, 10, 31, 0),
            0x01F522B3: (Kind.SLT, 5, 10, 31, 0), 0x41F552B3: (Kind.SRA, 5, 10, 31, 0),
            0x0330000F: (Kind.FENCE, 0, 0, 0, 0x33), 0x8330000F: (Kind.FENCE, 0, 0, 0, 0x833),
            0x0000100F: (Kind.FENCE_I, 0, 0, 0, 0), 0x00000073: (Kind.ECALL, 0, 0, 0, 0),
            0x00100073: (Kind.EBREAK, 0, 0, 0, 0), 0xC00512F3: (Kind.CSRRW, 5, 10, 0, 0xC00),
            0xC00532F3: (Kind.CSRRC, 5, 10, 0, 0xC00), 0xC00FD2F3: (Kind.CSRRWI, 5, 31, 0, 0xC00),
            0xC00FE2F3: (Kind.CSRRSI, 5, 31, 0, 0xC00), 0xC00FF2F3: (Kind.CSRRCI, 5, 31, 0, 0xC00),
            0x03F502B3: (Kind.MUL, 5, 10, 31, 0), 0x03F512B3: (Kind.MULH, 5, 10, 31, 0),
            0x03F522B3: (Kind.MULHSU, 5, 10, 31, 0), 0x03F532B3: (Kind.MULHU, 5, 10, 31, 0),
            0x03F542B3: (Kind.DIV, 5, 10, 31, 0), 0x03F552B3: (Kind.DIVU, 5, 10, 31, 0),
            0x03F562B3: (Kind.REM, 5, 10, 31, 0), 0x03F572B3: (Kind.REMU, 5, 10, 31, 0),
            0x1005A2AF: (Kind.LR_W, 5, 11, 0, 0), 0x18A5A2AF: (Kind.SC_W, 5, 11, 10, 0),
            0x08A5A2AF: (Kind.AMOSWAP_W, 5, 11, 10, 0), 0x00A5A2AF: (Kind.AMOADD_W, 5, 11, 10, 0),
            0x20A5A2AF: (Kind.AMOXOR_W, 5, 11, 10, 0), 0x60A5A2AF: (Kind.AMOAND_W, 5, 11, 10, 0),
            0x40A5A2AF: (Kind.AMOOR_W, 5, 11, 10, 0), 0x80A5A2AF: (Kind.AMOMIN_W, 5, 11, 10, 0),
            0xA0A5A2AF: (Kind.AMOMAX_W, 5, 11, 10, 0), 0xC0A5A2AF: (Kind.AMOMINU_W, 5, 11, 10, 0),
            0xE0A5A2AF: (Kind.AMOMAXU_W, 5, 11, 10, 0), 0x1605A52F: (Kind.LR_W, 10, 11, 0, 3),
            0x30200073: (Kind.MRET, 0, 0, 0, 0), 0x10500073: (Kind.WFI, 0, 0, 0, 0),
        }
        for word, (kind, rd, rs1, rs2, imm) in anchors.items():
            self.assertEqual(decode(word), (int(kind), rd, rs1, rs2, imm, word), hex(word))
            self.assertEqual(encode(kind, rd, rs1, rs2, imm), word, kind.name)

    def test_kind_numbering_is_fixed(self) -> None:
        self.assertEqual([int(kind) for kind in Kind], list(range(69)))
        self.assertEqual((int(Kind.ILLEGAL), int(Kind.LUI), int(Kind.FENCE), int(Kind.MUL), int(Kind.LR_W),
                          int(Kind.MRET), int(Kind.WFI)), (0, 1, 38, 48, 56, 67, 68))
        self.assertEqual(len(SPECS), 68)

    def test_encode_rejects_out_of_range_fields(self) -> None:
        with self.assertRaises(ValueError):
            encode(Kind.ILLEGAL)
        with self.assertRaises(ValueError):
            encode(Kind.LR_W, 1, 2, 3)
        for kind, fields in (
            (Kind.ADDI, {"imm": 2048}), (Kind.ADDI, {"imm": -2049}), (Kind.BEQ, {"imm": 1}), (Kind.BEQ, {"imm": 4096}),
            (Kind.JAL, {"imm": 3}), (Kind.JAL, {"imm": 1 << 20}), (Kind.LUI, {"imm": 1}), (Kind.SLLI, {"imm": 32}),
            (Kind.CSRRW, {"imm": 4096}), (Kind.ECALL, {"rd": 1}), (Kind.ADD, {"rd": 32}), (Kind.AMOADD_W, {"imm": 4}),
        ):
            with self.assertRaises(ValueError, msg=f"{kind.name} {fields}"):
                encode(kind, **fields)


class IllegalEncodings(unittest.TestCase):
    def assert_illegal(self, word: int) -> None:
        self.assertEqual(decode(word)[0], ILLEGAL, f"{word:#010x} debería ser ILLEGAL")

    def test_known_illegal_words(self) -> None:
        self.assert_illegal(0x00000000)
        self.assert_illegal(0xFFFFFFFF)
        self.assert_illegal(encode(Kind.SLLI, 1, 1, imm=1) | 1 << 25)       # imm[11:5] ≠ 0 en SLLI
        self.assert_illegal(encode(Kind.SLLI, 1, 1, imm=1) | 1 << 30)       # imm[11:5] = 0100000 con funct3 = 1 no existe
        self.assert_illegal(encode(Kind.SRAI, 1, 1, imm=1) | 1 << 26)       # imm[11:5] = 0100010
        self.assert_illegal(encode(Kind.ADD, 1, 2, 3) | 0x02 << 25)         # funct7 no asignado
        self.assert_illegal(encode(Kind.LW, 1, 2, imm=0) | 3 << 12)         # funct3 = 3 en LOAD
        self.assert_illegal(encode(Kind.SW, 0, 1, 2, imm=0) | 3 << 12)      # funct3 = 3 en STORE
        self.assert_illegal(encode(Kind.BEQ, 0, 1, 2, imm=0) | 2 << 12)     # funct3 = 2 en BRANCH
        self.assert_illegal(encode(Kind.JALR, 1, 2, imm=0) | 1 << 12)       # funct3 ≠ 0 en JALR
        self.assert_illegal(encode(Kind.ECALL) | 1 << 7)                    # rd ≠ 0 en ECALL
        self.assert_illegal(encode(Kind.ECALL) | 2 << 20)                   # funct12 = 2 (URET no existe en M/U)
        self.assert_illegal((encode(Kind.CSRRW, 1, 2, imm=0x300) & ~(7 << 12)) | 4 << 12)  # funct3 = 4 en SYSTEM
        self.assert_illegal(encode(Kind.WFI) | 1 << 15)                     # rs1 ≠ 0 en WFI
        self.assert_illegal(encode(Kind.MRET) | 1 << 7)                     # rd ≠ 0 en MRET
        for word in (0x10200073, 0x12000073, 0x7B200073):                   # sret, sfence.vma, dret: no hay modo S ni D
            self.assert_illegal(word)
        for opcode in (0x1F, 0x3F, 0x5F, 0x7F):                             # prefijos de instrucción larga
            self.assert_illegal(opcode | 0x100)
        self.assert_illegal(encode(Kind.ADD, 1, 2, 3) | 0x20 << 25 | 1 << 12)  # funct7 0x20 con funct3 = 1
        # Zifencei: FENCE.I ignora rd, rs1 e imm (reservados para fences más finos), igual que FENCE.
        for word in (0x0000108F, 0x0000900F, 0x0010100F):
            self.assertEqual(decode(word)[0], int(Kind.FENCE_I), hex(word))
        self.assertEqual(decode(0x0FF0008F)[0], int(Kind.FENCE))
        # Los campos reservados de FENCE.I salen tal cual (rd, rs1, imm = word >> 20), como en FENCE.
        self.assertEqual(decode(0x0000108F), (int(Kind.FENCE_I), 1, 0, 0, 0, 0x0000108F))
        self.assertEqual(decode(0x0000900F), (int(Kind.FENCE_I), 0, 1, 0, 0, 0x0000900F))
        self.assertEqual(decode(0x0010100F), (int(Kind.FENCE_I), 0, 0, 0, 1, 0x0010100F))
        self.assertEqual(decode(0x0FF0008F), (int(Kind.FENCE), 1, 0, 0, 0x0FF, 0x0FF0008F))
        self.assert_illegal(encode(Kind.FENCE) | 2 << 12)                   # funct3 = 2 en MISC-MEM
        self.assert_illegal(encode(Kind.AMOADD_W, 1, 2, 3) | 1 << 12)       # funct3 ≠ 2 en AMO
        self.assert_illegal(encode(Kind.AMOADD_W, 1, 2, 3) | 0x05 << 27)    # funct5 no asignado
        self.assert_illegal(encode(Kind.LR_W, 1, 2) | 3 << 20)              # rs2 ≠ 0 en LR.W
        for opcode in (0x07, 0x0B, 0x1B, 0x27, 0x2B, 0x3B, 0x53, 0x5B, 0x6B, 0x7B, 0x01, 0x02):
            self.assert_illegal(opcode | 0x00000080)

    def test_illegal_words_keep_the_raw_registers_and_imm_zero(self) -> None:
        # Convención documentada en rv32_decode: en ILLEGAL salen los bits crudos de rd, rs1 y rs2 e imm = 0.
        rng = random.Random(5)
        checked = 0
        for _ in range(20_000):
            word = rng.getrandbits(32)
            kind, rd, rs1, rs2, imm, raw = decode(word)
            if kind == ILLEGAL:
                checked += 1
                self.assertEqual((rd, rs1, rs2, imm, raw),
                                 ((word >> 7) & 0x1F, (word >> 15) & 0x1F, (word >> 20) & 0x1F, 0, word), hex(word))
        self.assertGreater(checked, 10_000)
        for word in (encode(Kind.SLLI, 3, 4, imm=1) | 1 << 25, encode(Kind.ADD, 5, 6, 7) | 0x02 << 25,
                     encode(Kind.LW, 8, 9, imm=0) | 3 << 12, encode(Kind.ECALL) | 1 << 7, 0x10200073,
                     encode(Kind.FENCE) | 2 << 12, encode(Kind.LR_W, 1, 2) | 3 << 20, 0x7F | 0x100):
            kind, rd, rs1, rs2, imm, raw = decode(word)
            self.assertEqual(kind, ILLEGAL, hex(word))
            self.assertEqual((rd, rs1, rs2, imm, raw),
                             ((word >> 7) & 0x1F, (word >> 15) & 0x1F, (word >> 20) & 0x1F, 0, word), hex(word))

    def test_encode_rejects_fields_that_do_not_fit(self) -> None:
        for kind, fields in (
            (Kind.SW, {"rs1": 1, "rs2": 2, "imm": 2048}), (Kind.SW, {"rs1": 1, "rs2": 2, "imm": -2049}),
            (Kind.ADDI, {"rd": 1, "rs1": 1, "imm": 2048}), (Kind.ADDI, {"rd": 1, "rs1": 1, "imm": -2049}),
            (Kind.SLLI, {"rd": 1, "rs1": 1, "imm": 32}), (Kind.SLLI, {"rd": 1, "rs1": 1, "imm": -1}),
            (Kind.BEQ, {"rs1": 1, "rs2": 2, "imm": 4096}), (Kind.BEQ, {"rs1": 1, "rs2": 2, "imm": -4098}),
            (Kind.BEQ, {"rs1": 1, "rs2": 2, "imm": 3}),
            (Kind.LUI, {"rd": 1, "imm": 1 << 31}), (Kind.LUI, {"rd": 1, "imm": -(1 << 31) - 4096}),
            (Kind.LUI, {"rd": 1, "imm": 0x1000 | 1}), (Kind.LUI, {"rd": 1, "imm": 0x800}), (Kind.LUI, {"rd": 1, "imm": 0x400}),
            (Kind.JAL, {"rd": 1, "imm": 1 << 20}), (Kind.JAL, {"rd": 1, "imm": -(1 << 20) - 2}), (Kind.JAL, {"rd": 1, "imm": 1}),
            (Kind.CSRRW, {"rd": 1, "rs1": 1, "imm": 0x1000}), (Kind.CSRRW, {"rd": 1, "rs1": 1, "imm": -1}),
            (Kind.FENCE, {"imm": 0x1000}), (Kind.FENCE, {"imm": -1}),
            (Kind.AMOADD_W, {"rd": 1, "rs1": 2, "rs2": 3, "imm": 4}),
            (Kind.ECALL, {"rd": 1}), (Kind.ECALL, {"rs1": 1}), (Kind.ECALL, {"rs2": 1}), (Kind.ECALL, {"imm": 1}),
            (Kind.MRET, {"rs1": 1}), (Kind.WFI, {"imm": 1}),
            (Kind.ADD, {"rd": 32, "rs1": 1, "rs2": 2}), (Kind.ADD, {"rd": 1, "rs1": -1, "rs2": 2}),
            (Kind.ADD, {"rd": 1, "rs1": 2, "rs2": 32}), (Kind.AMOADD_W, {"rd": 1, "rs1": 2, "rs2": 3, "imm": -1}),
            (Kind.SW, {"rs1": 1, "rs2": 32}), (Kind.BEQ, {"rs1": 32, "rs2": 1}),
            (Kind.LUI, {"rd": 1, "rs1": 40}), (Kind.LR_W, {"rd": 1, "rs1": 2, "rs2": 3}), (Kind.ILLEGAL, {}),
        ):
            with self.assertRaises(ValueError, msg=f"{kind.name} {fields}"):
                encode(kind, **fields)
        # Los extremos válidos sí codifican y vuelven.
        for kind, fields in ((Kind.SW, {"rs1": 1, "rs2": 2, "imm": 2047}), (Kind.SW, {"rs1": 1, "rs2": 2, "imm": -2048}),
                             (Kind.BEQ, {"rs1": 1, "rs2": 2, "imm": 4094}), (Kind.BEQ, {"rs1": 1, "rs2": 2, "imm": -4096}),
                             (Kind.LUI, {"rd": 1, "imm": (1 << 31) - 4096}), (Kind.LUI, {"rd": 1, "imm": -(1 << 31)}),
                             (Kind.JAL, {"rd": 1, "imm": (1 << 20) - 2}), (Kind.JAL, {"rd": 1, "imm": -(1 << 20)}),
                             (Kind.FENCE, {"imm": 0xFFF}), (Kind.CSRRW, {"rd": 1, "rs1": 1, "imm": 0xFFF})):
            self.assertEqual(decode(encode(kind, **fields))[4], fields["imm"], f"{kind.name} {fields}")

    def test_every_opcode_funct3_funct7_matches_the_manual_tables(self) -> None:
        """Oráculo escrito desde las tablas del manual (RV32I, M, A, Zifencei, Zicsr, MRET/WFI): para cada
        combinación de opcode × funct3 × funct7 (y funct12 en SYSTEM) dice si la palabra es legal y de qué
        kind; se compara con ``decode`` en las 131 072 combinaciones con registros a cero y en una muestra
        con registros al azar."""
        op = {0x37: "LUI", 0x17: "AUIPC", 0x6F: "JAL", 0x67: "JALR", 0x63: "BRANCH", 0x03: "LOAD", 0x23: "STORE",
              0x13: "OP-IMM", 0x33: "OP", 0x0F: "MISC-MEM", 0x73: "SYSTEM", 0x2F: "AMO"}
        branches = {0: Kind.BEQ, 1: Kind.BNE, 4: Kind.BLT, 5: Kind.BGE, 6: Kind.BLTU, 7: Kind.BGEU}
        loads = {0: Kind.LB, 1: Kind.LH, 2: Kind.LW, 4: Kind.LBU, 5: Kind.LHU}
        stores = {0: Kind.SB, 1: Kind.SH, 2: Kind.SW}
        op_imm = {0: Kind.ADDI, 2: Kind.SLTI, 3: Kind.SLTIU, 4: Kind.XORI, 6: Kind.ORI, 7: Kind.ANDI}
        base_ops = {(0, 0): Kind.ADD, (0, 0x20): Kind.SUB, (1, 0): Kind.SLL, (2, 0): Kind.SLT, (3, 0): Kind.SLTU,
                    (4, 0): Kind.XOR, (5, 0): Kind.SRL, (5, 0x20): Kind.SRA, (6, 0): Kind.OR, (7, 0): Kind.AND}
        m_ops = {(0, 1): Kind.MUL, (1, 1): Kind.MULH, (2, 1): Kind.MULHSU, (3, 1): Kind.MULHU, (4, 1): Kind.DIV,
                 (5, 1): Kind.DIVU, (6, 1): Kind.REM, (7, 1): Kind.REMU}
        csr_ops = {1: Kind.CSRRW, 2: Kind.CSRRS, 3: Kind.CSRRC, 5: Kind.CSRRWI, 6: Kind.CSRRSI, 7: Kind.CSRRCI}
        system = {0x000: Kind.ECALL, 0x001: Kind.EBREAK, 0x302: Kind.MRET, 0x105: Kind.WFI}
        amo = {0x02: Kind.LR_W, 0x03: Kind.SC_W, 0x01: Kind.AMOSWAP_W, 0x00: Kind.AMOADD_W, 0x04: Kind.AMOXOR_W,
               0x0C: Kind.AMOAND_W, 0x08: Kind.AMOOR_W, 0x10: Kind.AMOMIN_W, 0x14: Kind.AMOMAX_W, 0x18: Kind.AMOMINU_W,
               0x1C: Kind.AMOMAXU_W}

        def oracle(word: int) -> Kind:
            opcode, funct3, funct7 = word & 0x7F, bits(word, 14, 12), bits(word, 31, 25)
            rd, rs1, rs2 = bits(word, 11, 7), bits(word, 19, 15), bits(word, 24, 20)
            group = op.get(opcode)
            if group is None:
                return Kind.ILLEGAL
            if group == "LUI":
                return Kind.LUI
            if group == "AUIPC":
                return Kind.AUIPC
            if group == "JAL":
                return Kind.JAL
            if group == "JALR":
                return Kind.JALR if funct3 == 0 else Kind.ILLEGAL
            if group == "BRANCH":
                return branches.get(funct3, Kind.ILLEGAL)
            if group == "LOAD":
                return loads.get(funct3, Kind.ILLEGAL)
            if group == "STORE":
                return stores.get(funct3, Kind.ILLEGAL)
            if group == "OP-IMM":
                if funct3 == 1:
                    return Kind.SLLI if funct7 == 0 else Kind.ILLEGAL
                if funct3 == 5:
                    return {0: Kind.SRLI, 0x20: Kind.SRAI}.get(funct7, Kind.ILLEGAL)
                return op_imm[funct3]
            if group == "OP":
                return base_ops.get((funct3, funct7)) or m_ops.get((funct3, funct7)) or Kind.ILLEGAL
            if group == "MISC-MEM":
                return {0: Kind.FENCE, 1: Kind.FENCE_I}.get(funct3, Kind.ILLEGAL)   # campos reservados: se ignoran
            if group == "SYSTEM":
                if funct3 == 0:
                    kind = system.get(bits(word, 31, 20))
                    return kind if kind is not None and rd == 0 and rs1 == 0 else Kind.ILLEGAL
                return csr_ops.get(funct3, Kind.ILLEGAL)
            if funct3 != 2:
                return Kind.ILLEGAL
            kind = amo.get(funct7 >> 2)
            if kind is None or (kind == Kind.LR_W and rs2 != 0):
                return Kind.ILLEGAL
            return kind

        legal = 0
        for opcode in range(128):
            for funct3 in range(8):
                for funct7 in range(128):
                    word = opcode | funct3 << 12 | funct7 << 25
                    expected = oracle(word)
                    self.assertEqual(Kind(decode(word)[0]), expected, f"{word:#010x}")
                    legal += expected != Kind.ILLEGAL
        self.assertGreater(legal, 1000)
        rng = random.Random(31)
        for _ in range(100_000):
            word = rng.getrandbits(32)
            self.assertEqual(Kind(decode(word)[0]), oracle(word), f"{word:#010x}")
        for funct12 in range(4096):   # SYSTEM con funct3 = 0: solo los cuatro funct12 con rd = rs1 = 0
            word = 0x73 | funct12 << 20
            self.assertEqual(Kind(decode(word)[0]), oracle(word), hex(word))
            self.assertEqual(decode(word | 1 << 7)[0], ILLEGAL, hex(word))
            self.assertEqual(decode(word | 1 << 15)[0], ILLEGAL, hex(word))

    def test_decode_never_raises_on_random_words(self) -> None:
        rng = random.Random(99)
        legal = 0
        for _ in range(50_000):
            word = rng.getrandbits(32)
            kind, rd, rs1, rs2, imm, raw = decode(word)
            self.assertEqual(raw, word)
            self.assertTrue(0 <= rd <= 31 and 0 <= rs1 <= 31 and 0 <= rs2 <= 31)
            if kind != ILLEGAL:
                legal += 1
                disassemble(word, 0x80000000)
        self.assertGreater(legal, 1000)


class DisassemblyAgainstObjdump(unittest.TestCase):
    """Cada instrucción de .text del CoreMark rv32im versionado debe desensamblarse como llvm-objdump."""

    LINE = re.compile(r"^\s*([0-9a-f]{8}):\s+(\S+)(?:\s+(.*?))?\s*(?:<[^>]*>)?\s*$")

    def test_matches_llvm_objdump(self) -> None:
        self.assertTrue(GOLDEN.is_file() and BINARY.is_file(), "faltan el desensamblado dorado o el binario versionado")
        image = BINARY.read_bytes()
        compared = 0
        mismatches: list[str] = []
        for line in GOLDEN.read_text(encoding="utf-8").splitlines():
            match = self.LINE.match(line)
            if not match:
                continue
            pc = int(match.group(1), 16)
            expected = f"{match.group(2)} {match.group(3) or ''}".strip()
            offset = pc - TEXT_BASE
            word = int.from_bytes(image[offset:offset + 4], "little")
            actual = disassemble(word, pc)
            compared += 1
            if actual != expected:
                mismatches.append(f"{pc:08x}: {word:08x}  objdump «{expected}»  nuestro «{actual}»")
        self.assertGreater(compared, 3000)
        self.assertEqual(mismatches, [], "\n".join(mismatches[:20]))

    def test_aliases_and_formats(self) -> None:
        cases = {
            encode(Kind.ADDI, 0, 0, imm=0): "nop",
            encode(Kind.ADDI, 10, 0, imm=-1): "li a0, -0x1",
            encode(Kind.ADDI, 8, 10, imm=0): "mv s0, a0",
            encode(Kind.ADDI, 2, 2, imm=-32): "addi sp, sp, -0x20",
            encode(Kind.JAL, 0, imm=0): "j 0x80000000",
            encode(Kind.JAL, 1, imm=0x10): "jal 0x80000010",
            encode(Kind.JAL, 5, imm=-4): "jal t0, 0x7ffffffc",
            encode(Kind.JALR, 0, 1, imm=0): "ret",
            encode(Kind.JALR, 0, 1, imm=4): "jr 0x4(ra)",
            encode(Kind.JALR, 0, 10, imm=0): "jr a0",
            encode(Kind.JALR, 0, 10, imm=8): "jr 0x8(a0)",
            encode(Kind.JALR, 1, 10, imm=0): "jalr a0",
            encode(Kind.JALR, 1, 10, imm=8): "jalr 0x8(a0)",
            encode(Kind.JALR, 5, 10, imm=0): "jalr t0, a0",
            encode(Kind.JALR, 5, 10, imm=8): "jalr t0, 0x8(a0)",
            encode(Kind.BGE, 0, 0, 0, imm=4): "blez zero, 0x80000004",
            encode(Kind.BLT, 0, 0, 0, imm=4): "bltz zero, 0x80000004",
            encode(Kind.BEQ, 0, 0, 0, imm=4): "beqz zero, 0x80000004",
            encode(Kind.BNE, 0, 0, 0, imm=4): "bnez zero, 0x80000004",
            encode(Kind.BGE, 0, 10, 0, imm=8): "bgez a0, 0x80000008",
            encode(Kind.BNE, 0, 10, 0, imm=8): "bnez a0, 0x80000008",
            0xC0001073: "unimp",
            encode(Kind.CSRRS, 0, 0, imm=0xC00): "rdcycle zero",
            encode(Kind.ADDI, 0, 0, imm=4): "li zero, 0x4",
            encode(Kind.FENCE, 0, 0, imm=0x833): "fence.tso",
            # fm = 8 sin pred = succ = rw no es fence.tso; objdump imprime <unknown> (diferencia 2 de la lista cerrada).
            encode(Kind.FENCE, 0, 0, imm=0x832): "fence rw, r",
            encode(Kind.FENCE, 0, 0, imm=0x0F1): "fence iorw, w",
            encode(Kind.FENCE, 0, 0, imm=0x0C3): "fence io, rw",
            encode(Kind.FENCE, 0, 0, imm=0x0F0): "fence iorw, 0",
            encode(Kind.FENCE, 0, 0, imm=0x031): "fence rw, w",
            encode(Kind.JAL, 0, imm=8): "j 0x80000008",
            encode(Kind.BEQ, 0, 10, 0, imm=8): "beqz a0, 0x80000008",
            encode(Kind.BGE, 0, 0, 10, imm=8): "blez a0, 0x80000008",
            encode(Kind.BLT, 0, 0, 10, imm=8): "bgtz a0, 0x80000008",
            encode(Kind.BLT, 0, 10, 0, imm=-8): "bltz a0, 0x7ffffff8",
            encode(Kind.BLTU, 0, 10, 11, imm=4): "bltu a0, a1, 0x80000004",
            encode(Kind.LUI, 5, imm=0x11100000): "lui t0, 0x11100",
            encode(Kind.LUI, 5, imm=-4096): "lui t0, 0xfffff",
            encode(Kind.AUIPC, 10, imm=0): "auipc a0, 0x0",
            encode(Kind.LW, 10, 2, imm=28): "lw a0, 0x1c(sp)",
            encode(Kind.SB, 0, 10, 11, imm=-1): "sb a1, -0x1(a0)",
            encode(Kind.SLLI, 11, 19, imm=25): "slli a1, s3, 0x19",
            encode(Kind.SLTIU, 10, 11, imm=1): "seqz a0, a1",
            encode(Kind.XORI, 10, 11, imm=-1): "not a0, a1",
            encode(Kind.XORI, 10, 11, imm=-2): "xori a0, a1, -0x2",
            encode(Kind.ANDI, 10, 11, imm=-1): "andi a0, a1, -0x1",
            encode(Kind.ORI, 10, 11, imm=-1): "ori a0, a1, -0x1",
            encode(Kind.ANDI, 10, 11, imm=254): "andi a0, a1, 0xfe",
            encode(Kind.SLTIU, 10, 11, imm=2): "sltiu a0, a1, 0x2",
            encode(Kind.ANDI, 10, 11, imm=255): "zext.b a0, a1",
            encode(Kind.SUB, 10, 0, 11): "neg a0, a1",
            encode(Kind.SLTU, 10, 0, 11): "snez a0, a1",
            encode(Kind.SLT, 10, 11, 0): "sltz a0, a1",
            encode(Kind.SLT, 10, 0, 11): "sgtz a0, a1",
            encode(Kind.MUL, 10, 11, 12): "mul a0, a1, a2",
            encode(Kind.CSRRS, 10, 0, imm=0xC00): "rdcycle a0",
            encode(Kind.CSRRS, 10, 0, imm=0x300): "csrr a0, mstatus",
            encode(Kind.CSRRW, 0, 10, imm=0x305): "csrw mtvec, a0",
            encode(Kind.CSRRW, 5, 10, imm=0x340): "csrrw t0, mscratch, a0",
            encode(Kind.CSRRW, 5, 0, imm=0x340): "csrrw t0, mscratch, zero",
            encode(Kind.CSRRS, 5, 10, imm=0x340): "csrrs t0, mscratch, a0",
            encode(Kind.CSRRC, 0, 10, imm=0x340): "csrc mscratch, a0",
            encode(Kind.CSRRC, 5, 0, imm=0x340): "csrrc t0, mscratch, zero",
            encode(Kind.CSRRS, 10, 0, imm=0xC01): "rdtime a0",
            encode(Kind.CSRRS, 10, 0, imm=0xC82): "rdinstreth a0",
            encode(Kind.CSRRWI, 0, 3, imm=0x304): "csrwi mie, 0x3",
            encode(Kind.CSRRCI, 5, 3, imm=0x7C0): "csrrci t0, 0x7c0, 0x3",
            encode(Kind.FENCE, 0, 0, imm=0x0FF): "fence",
            encode(Kind.FENCE, 0, 0, imm=0x033): "fence rw, rw",
            encode(Kind.FENCE_I): "fence.i",
            encode(Kind.ECALL): "ecall",
            encode(Kind.EBREAK): "ebreak",
            encode(Kind.MRET): "mret",
            encode(Kind.WFI): "wfi",
            encode(Kind.LR_W, 10, 11, imm=2): "lr.w.aq a0, (a1)",
            encode(Kind.SC_W, 10, 11, 12, imm=1): "sc.w.rl a0, a2, (a1)",
            encode(Kind.AMOSWAP_W, 10, 11, 12, imm=3): "amoswap.w.aqrl a0, a2, (a1)",
            encode(Kind.AMOADD_W, 10, 11, 12): "amoadd.w a0, a2, (a1)",
            encode(Kind.AMOXOR_W, 10, 11, 12, imm=2): "amoxor.w.aq a0, a2, (a1)",
            encode(Kind.AMOAND_W, 10, 11, 12): "amoand.w a0, a2, (a1)",
            encode(Kind.AMOOR_W, 10, 11, 12): "amoor.w a0, a2, (a1)",
            encode(Kind.AMOMIN_W, 10, 11, 12): "amomin.w a0, a2, (a1)",
            encode(Kind.AMOMAX_W, 10, 11, 12): "amomax.w a0, a2, (a1)",
            encode(Kind.AMOMINU_W, 10, 11, 12): "amominu.w a0, a2, (a1)",
            encode(Kind.AMOMAXU_W, 10, 11, 12, imm=1): "amomaxu.w.rl a0, a2, (a1)",
            encode(Kind.DIV, 10, 11, 12): "div a0, a1, a2",
            encode(Kind.REM, 10, 11, 12): "rem a0, a1, a2",
            encode(Kind.REMU, 10, 11, 12): "remu a0, a1, a2",
            encode(Kind.MULH, 10, 11, 12): "mulh a0, a1, a2",
            encode(Kind.MULHSU, 10, 11, 12): "mulhsu a0, a1, a2",
            encode(Kind.SLL, 10, 11, 12): "sll a0, a1, a2",
            encode(Kind.SRL, 10, 11, 12): "srl a0, a1, a2",
            encode(Kind.SRA, 10, 11, 12): "sra a0, a1, a2",
            encode(Kind.SLTI, 10, 11, imm=-1): "slti a0, a1, -0x1",
            encode(Kind.LB, 10, 11, imm=3): "lb a0, 0x3(a1)",
            encode(Kind.CSRRSI, 5, 3, imm=0x340): "csrrsi t0, mscratch, 0x3",
            encode(Kind.CSRRSI, 0, 3, imm=0x304): "csrsi mie, 0x3",
            encode(Kind.CSRRCI, 0, 3, imm=0x304): "csrci mie, 0x3",
            encode(Kind.ADDI, 0, 10, imm=0): "mv zero, a0",
            encode(Kind.ADDI, 3, 4, imm=1): "addi gp, tp, 0x1",
            encode(Kind.ADD, 8, 9, 18): "add s0, s1, s2",
            encode(Kind.ADD, 26, 27, 28): "add s10, s11, t3",
            encode(Kind.ADD, 29, 30, 31): "add t4, t5, t6",
            encode(Kind.SLT, 0, 0, 0): "sltz zero, zero",
            encode(Kind.FENCE, 0, 0, imm=0x03F): "fence rw, iorw",
            encode(Kind.CSRRS, 10, 0, imm=0xC02): "rdinstret a0",
            encode(Kind.CSRRS, 10, 0, imm=0xC80): "rdcycleh a0",
            encode(Kind.CSRRS, 10, 0, imm=0xC81): "rdtimeh a0",
            encode(Kind.CSRRS, 0, 10, imm=0x300): "csrs mstatus, a0",
            0x1C0001073: "unimp",   # disassemble enmascara a 32 bits, como decode
            0x00000000: "<unknown>",
        }
        for word, expected in cases.items():
            self.assertEqual(disassemble(word, 0x80000000), expected, f"{word:#010x}")
        # Los destinos se enmascaran a 32 bits: un salto hacia atrás desde el principio del espacio.
        self.assertEqual(disassemble(encode(Kind.JAL, 0, imm=-8), 0x4), "j 0xfffffffc")
        self.assertEqual(disassemble(encode(Kind.BEQ, 0, 1, 2, imm=8), 0xFFFFFFFC), "beq ra, sp, 0x4")

    def test_mask_is_consistent(self) -> None:
        self.assertEqual(MASK32, (1 << 32) - 1)

    def test_kind_numbering_is_frozen(self) -> None:
        # «El orden es estable: H3 añade al final, nunca renumera»: la tabla completa queda fijada.
        expected = ("ILLEGAL LUI AUIPC JAL JALR BEQ BNE BLT BGE BLTU BGEU LB LH LW LBU LHU SB SH SW ADDI SLTI SLTIU XORI "
                    "ORI ANDI SLLI SRLI SRAI ADD SUB SLL SLT SLTU XOR SRL SRA OR AND FENCE FENCE_I ECALL EBREAK CSRRW CSRRS "
                    "CSRRC CSRRWI CSRRSI CSRRCI MUL MULH MULHSU MULHU DIV DIVU REM REMU LR_W SC_W AMOSWAP_W AMOADD_W "
                    "AMOXOR_W AMOAND_W AMOOR_W AMOMIN_W AMOMAX_W AMOMINU_W AMOMAXU_W MRET WFI").split()
        self.assertEqual([kind.name for kind in sorted(Kind, key=int)], expected)
        self.assertEqual([int(kind) for kind in sorted(Kind, key=int)], list(range(len(expected))))

    def test_csr_numbers_follow_the_privileged_manual(self) -> None:
        from cpu_digital import rv32_isa as isa
        expected = {
            "cycle": 0xC00, "time": 0xC01, "instret": 0xC02, "cycleh": 0xC80, "timeh": 0xC81, "instreth": 0xC82,
            "mstatus": 0x300, "misa": 0x301, "mie": 0x304, "mtvec": 0x305, "mscratch": 0x340, "mepc": 0x341,
            "mcause": 0x342, "mtval": 0x343, "mip": 0x344, "mcycle": 0xB00, "minstret": 0xB02, "mcycleh": 0xB80,
            "minstreth": 0xB82, "mvendorid": 0xF11, "marchid": 0xF12, "mimpid": 0xF13, "mhartid": 0xF14,
        }
        self.assertEqual({name: number for number, name in isa.CSR_NAMES.items()}, expected)
        for name, number in expected.items():
            self.assertEqual(getattr(isa, f"CSR_{name.upper()}"), number)
        # Tabla «Machine cause register (mcause) values» del manual privilegiado.
        self.assertEqual((isa.CAUSE_INSTRUCTION_MISALIGNED, isa.CAUSE_INSTRUCTION_ACCESS_FAULT,
                          isa.CAUSE_ILLEGAL_INSTRUCTION, isa.CAUSE_BREAKPOINT, isa.CAUSE_LOAD_MISALIGNED,
                          isa.CAUSE_LOAD_ACCESS_FAULT, isa.CAUSE_STORE_MISALIGNED, isa.CAUSE_STORE_ACCESS_FAULT,
                          isa.CAUSE_ECALL_FROM_U, isa.CAUSE_ECALL_FROM_M), (0, 1, 2, 3, 4, 5, 6, 7, 8, 11))
        self.assertEqual(isa.INTERRUPT_BIT, 0x80000000)
        self.assertEqual((isa.INTERRUPT_MACHINE_SOFTWARE, isa.INTERRUPT_MACHINE_TIMER, isa.INTERRUPT_MACHINE_EXTERNAL),
                         (3, 7, 11))
        for name in ("OP_OP", "OP_LOAD", "CSR_MSTATUS", "CAUSE_BREAKPOINT", "INTERRUPT_BIT", "CSR_NAMES"):
            self.assertIn(name, isa.__all__)
        self.assertEqual(len(isa.__all__), len(set(isa.__all__)))


if __name__ == "__main__":
    unittest.main()
