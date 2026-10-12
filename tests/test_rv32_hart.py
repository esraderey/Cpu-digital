"""Anclas del hart RV32 (H1): diferencial con un intérprete de referencia, bordes, fallos, CSR y CoreMark."""

from __future__ import annotations

import random
import unittest
from pathlib import Path

from cpu_digital import rv32_alu as alu
from cpu_digital.rv32_decode import decode
from cpu_digital.rv32_hart import GAS_PER_INSTRUCTION, Rv32Hart, Stop
from cpu_digital.rv32_isa import (
    CAUSE_BREAKPOINT,
    CAUSE_ECALL_FROM_M,
    CAUSE_ILLEGAL_INSTRUCTION,
    CAUSE_INSTRUCTION_ACCESS_FAULT,
    CAUSE_INSTRUCTION_MISALIGNED,
    CAUSE_LOAD_ACCESS_FAULT,
    CAUSE_STORE_ACCESS_FAULT,
    CSR_CYCLE,
    CSR_INSTRET,
    CSR_MSTATUS,
    CSR_TIME,
    Kind,
    encode,
)
from cpu_digital.rv32_mem import Rv32Memory
from cpu_digital.rv32_platform import Machine

ROOT = Path(__file__).resolve().parents[1]
COREMARK = ROOT / "benchmarks" / "rv32_arranque" / "bin" / "coremark_rv32i_zicsr_it10.bin"
# Recuento de E1 con mini-rv32ima instrumentado (8 505 936) más la escritura en SYSCON que apaga la
# máquina: mini-rv32ima sale de su bucle en esa escritura sin contarla; el hart la confirma y la cuenta.
COREMARK_INSTRUCTIONS = 8_505_936 + 1
BASE = 0x80000000
RAM = 1 << 16          # 64 KiB: código en la primera página, datos en la segunda y tercera
CODE = BASE
DATA = BASE + 0x1000
M = 0xFFFFFFFF


class Reference:
    """Intérprete de referencia: decodificación y aritmética compartidas, control y memoria propios."""

    def __init__(self, image: bytes, size: int = RAM) -> None:
        self.mem = bytearray(size)
        self.mem[:len(image)] = image
        self.regs = [0] * 32
        self.pc = BASE
        self.count = 0
        self.stop: Stop | None = None

    def load(self, address: int, width: int, signed: bool) -> int:
        offset = address - BASE
        if not 0 <= offset <= len(self.mem) - width:
            raise LookupError(CAUSE_LOAD_ACCESS_FAULT, address)
        return int.from_bytes(self.mem[offset:offset + width], "little", signed=signed) & M

    def store(self, address: int, width: int, value: int) -> None:
        offset = address - BASE
        if not 0 <= offset <= len(self.mem) - width:
            raise LookupError(CAUSE_STORE_ACCESS_FAULT, address)
        self.mem[offset:offset + width] = (value & ((1 << (8 * width)) - 1)).to_bytes(width, "little")

    def step(self) -> bool:
        pc = self.pc
        if pc & 3:
            self.stop = Stop("fallo", CAUSE_INSTRUCTION_MISALIGNED, pc)
            return False
        if not 0 <= pc - BASE <= len(self.mem) - 4:
            self.stop = Stop("fallo", CAUSE_INSTRUCTION_ACCESS_FAULT, pc)
            return False
        word = int.from_bytes(self.mem[pc - BASE:pc - BASE + 4], "little")
        kind, rd, rs1, rs2, imm, raw = decode(word)
        k = Kind(kind)
        regs = self.regs
        next_pc = pc + 4
        result: int | None = None
        try:
            if k == Kind.LUI:
                result = imm & M
            elif k == Kind.AUIPC:
                result = (pc + imm) & M
            elif k == Kind.JAL:
                next_pc = (pc + imm) & M
                result = (pc + 4) & M
            elif k == Kind.JALR:
                next_pc = (regs[rs1] + imm) & M & ~1
                result = (pc + 4) & M
            elif k in alu.BRANCHES:
                if alu.BRANCHES[k](regs[rs1], regs[rs2]):
                    next_pc = (pc + imm) & M
            elif k in (Kind.LB, Kind.LBU, Kind.LH, Kind.LHU, Kind.LW):
                width = {Kind.LB: 1, Kind.LBU: 1, Kind.LH: 2, Kind.LHU: 2, Kind.LW: 4}[k]
                result = self.load((regs[rs1] + imm) & M, width, k in (Kind.LB, Kind.LH))
            elif k in (Kind.SB, Kind.SH, Kind.SW):
                self.store((regs[rs1] + imm) & M, {Kind.SB: 1, Kind.SH: 2, Kind.SW: 4}[k], regs[rs2])
            elif k in (Kind.ADDI, Kind.SLTI, Kind.SLTIU, Kind.XORI, Kind.ORI, Kind.ANDI, Kind.SLLI, Kind.SRLI, Kind.SRAI):
                result = alu.FUNCTIONS[k](regs[rs1], imm & M)
            elif k in alu.FUNCTIONS and k not in (Kind.MUL, Kind.MULH, Kind.MULHSU, Kind.MULHU, Kind.DIV, Kind.DIVU,
                                                  Kind.REM, Kind.REMU):
                result = alu.FUNCTIONS[k](regs[rs1], regs[rs2])
            elif k in (Kind.FENCE, Kind.FENCE_I):
                pass
            elif k == Kind.ECALL:
                self.stop = Stop("ecall", CAUSE_ECALL_FROM_M, 0)
                return False
            elif k == Kind.EBREAK:
                self.stop = Stop("ebreak", CAUSE_BREAKPOINT, 0)
                return False
            elif k in (Kind.CSRRS, Kind.CSRRC, Kind.CSRRSI, Kind.CSRRCI) and rs1 == 0:
                if imm in (CSR_CYCLE, CSR_INSTRET, CSR_TIME):
                    result = self.count & M
                elif imm in (0xC80, 0xC82, 0xC81):
                    result = (self.count >> 32) & M
                else:
                    raise LookupError(CAUSE_ILLEGAL_INSTRUCTION, raw)
            else:
                raise LookupError(CAUSE_ILLEGAL_INSTRUCTION, raw)
            if next_pc & 3:
                raise LookupError(CAUSE_INSTRUCTION_MISALIGNED, next_pc)
        except LookupError as fault:
            self.stop = Stop("fallo", fault.args[0], fault.args[1])
            return False
        if result is not None and rd:
            regs[rd] = result
        self.pc = next_pc
        self.count += 1
        return True

    def run(self, steps: int) -> Stop:
        for _ in range(steps):
            if not self.step():
                return self.stop
        return Stop("gas")


def assemble(words: list[int]) -> bytes:
    return b"".join(word.to_bytes(4, "little") for word in words)


def random_program(rng: random.Random, length: int) -> list[int]:
    """Programa aleatorio sin saltos hacia atrás: aritmética, memoria sobre la zona de datos, saltos adelante."""
    words = [encode(Kind.LUI, 10, imm=alu.to_signed(DATA & 0xFFFFF000)), encode(Kind.ADDI, 10, 10, imm=0),
             encode(Kind.LUI, 11, imm=alu.to_signed((DATA + 0x800) & 0xFFFFF000)), encode(Kind.ADDI, 11, 11, imm=0x7F0)]
    alu_kinds = [Kind.ADD, Kind.SUB, Kind.SLL, Kind.SLT, Kind.SLTU, Kind.XOR, Kind.SRL, Kind.SRA, Kind.OR, Kind.AND]
    imm_kinds = [Kind.ADDI, Kind.SLTI, Kind.SLTIU, Kind.XORI, Kind.ORI, Kind.ANDI]
    shift_kinds = [Kind.SLLI, Kind.SRLI, Kind.SRAI]
    loads = [Kind.LB, Kind.LBU, Kind.LH, Kind.LHU, Kind.LW]
    stores = [Kind.SB, Kind.SH, Kind.SW]
    while len(words) < length:
        roll = rng.random()
        rd = rng.choice((0, rng.randrange(1, 32)))
        if rd in (10, 11) and rng.random() < 0.9:
            rd = 12  # casi nunca se pisan los registros base: así la mayoría de programas llega al ebreak
        rs1 = rng.randrange(32)
        rs2 = rng.randrange(32)
        if roll < 0.30:
            words.append(encode(rng.choice(alu_kinds), rd, rs1, rs2))
        elif roll < 0.45:
            words.append(encode(rng.choice(imm_kinds), rd, rs1, imm=rng.randrange(-2048, 2048)))
        elif roll < 0.50:
            words.append(encode(rng.choice(shift_kinds), rd, rs1, imm=rng.randrange(32)))
        elif roll < 0.55:
            words.append(encode(rng.choice((Kind.LUI, Kind.AUIPC)), rd, imm=rng.randrange(-(1 << 19), 1 << 19) * 4096))
        elif roll < 0.68:
            base = rs1 if rng.random() < 0.015 else rng.choice((10, 11))  # a veces una base cualquiera: fallos
            words.append(encode(rng.choice(loads), rd, base, imm=rng.randrange(0, 0x7F0)))
        elif roll < 0.80:
            base = rs1 if rng.random() < 0.015 else rng.choice((10, 11))
            words.append(encode(rng.choice(stores), 0, base, rs2, imm=rng.randrange(0, 0x7F0)))
        elif roll < 0.90:
            remaining = length - len(words) - 1
            skip = rng.randrange(1, min(remaining, 8) + 1) if remaining > 1 else 1
            kind = rng.choice((Kind.BEQ, Kind.BNE, Kind.BLT, Kind.BGE, Kind.BLTU, Kind.BGEU))
            words.append(encode(kind, 0, rs1, rs2, imm=4 * skip))
        elif roll < 0.95:
            remaining = length - len(words) - 1
            skip = rng.randrange(1, min(remaining, 8) + 1) if remaining > 1 else 1
            words.append(encode(Kind.JAL, rd, imm=4 * skip))
        elif roll < 0.97:
            words.append(encode(Kind.CSRRS, rd, 0, imm=rng.choice((CSR_CYCLE, CSR_INSTRET, CSR_TIME))))
        elif roll < 0.995 and length - len(words) > 3:
            # auipc rT, 0; jalr rd, rT, 8 + 4·skip: salto indirecto hacia delante a una instrucción válida.
            temp = rng.randrange(12, 32)
            skip = rng.randrange(0, min(length - len(words) - 3, 6) + 1)
            words.append(encode(Kind.AUIPC, temp, imm=0))
            words.append(encode(Kind.JALR, rd, temp, imm=8 + 4 * skip))
        else:
            words.append(encode(Kind.JALR, rd, rs1, imm=rng.randrange(-16, 16)))  # destino cualquiera: fallo probable
    words.append(encode(Kind.EBREAK))
    return words


def make_hart(image: bytes, size: int = RAM) -> tuple[Rv32Hart, Rv32Memory]:
    memory = Rv32Memory(size)
    memory.load_bytes(BASE, image)
    hart = Rv32Hart(memory)
    return hart, memory


class DifferentialAgainstReference(unittest.TestCase):
    def check_program(self, words: list[int], rng: random.Random, cuts: bool) -> None:
        image = assemble(words)
        reference = Reference(image)
        reference_stop = reference.run(600)
        hart, memory = make_hart(image)
        self.addCleanup(memory.close)
        issued = 0
        if cuts:
            stop = Stop("gas")
            remaining = 600
            while remaining > 0 and stop.reason == "gas":
                chunk = min(remaining, rng.randrange(1, 25))
                stop = hart.run(chunk)
                issued += chunk
                remaining -= chunk
        else:
            issued = 600
            stop = hart.run(600)
        self.assertEqual(stop, reference_stop)
        self.assertEqual(hart.regs, reference.regs)
        self.assertEqual(hart.pc, reference.pc)
        self.assertEqual(hart.instret, reference.count)
        self.assertEqual(hart.gas, issued - reference.count)   # bitácora independiente: 1 de gas por confirmada
        self.assertEqual(bytes(memory.lanes), bytes(reference.mem))

    def test_random_programs(self) -> None:
        rng = random.Random(2026)
        reasons: dict[str, int] = {}
        for index in range(300):
            words = random_program(rng, rng.randrange(20, 200))
            image = assemble(words)
            expected = Reference(image).run(600)
            reasons[expected.reason] = reasons.get(expected.reason, 0) + 1
            with self.subTest(program=index):
                self.check_program(words, rng, cuts=index % 2 == 1)
        self.assertGreater(reasons.get("ebreak", 0), 100, reasons)
        self.assertGreater(reasons.get("fallo", 0), 20, reasons)


class SingleInstructions(unittest.TestCase):
    def run_words(self, words: list[int], gas: int = 100, regs: dict[int, int] | None = None) -> tuple[Rv32Hart, Stop]:
        hart, memory = make_hart(assemble(words))
        self.addCleanup(memory.close)
        for index, value in (regs or {}).items():
            hart.regs[index] = value
        stop = hart.run(gas)
        return hart, stop

    def test_x0_is_never_written(self) -> None:
        hart, stop = self.run_words([encode(Kind.ADDI, 0, 0, imm=5), encode(Kind.LUI, 0, imm=0x1000),
                                     encode(Kind.JAL, 0, imm=4), encode(Kind.LW, 0, 10, imm=0), encode(Kind.EBREAK)],
                                    regs={10: DATA})
        self.assertEqual(stop.reason, "ebreak")
        self.assertEqual(hart.regs[0], 0)
        self.assertEqual(hart.instret, 4)

    def test_jalr_clears_bit_zero_and_misaligned_targets_fault(self) -> None:
        hart, stop = self.run_words([encode(Kind.JALR, 1, 5, imm=1), encode(Kind.EBREAK), encode(Kind.EBREAK)],
                                    regs={5: CODE + 8})
        self.assertEqual((stop.reason, hart.pc, hart.regs[1]), ("ebreak", CODE + 8, CODE + 4))
        hart, stop = self.run_words([encode(Kind.JALR, 1, 5, imm=2)], regs={5: CODE})
        self.assertEqual(stop, Stop("fallo", CAUSE_INSTRUCTION_MISALIGNED, CODE + 2))
        self.assertEqual((hart.pc, hart.regs[1], hart.instret, hart.gas), (CODE, 0, 0, 100))
        hart, stop = self.run_words([encode(Kind.BEQ, 0, 0, 0, imm=6)])
        self.assertEqual(stop, Stop("fallo", CAUSE_INSTRUCTION_MISALIGNED, CODE + 6))
        hart, stop = self.run_words([encode(Kind.JAL, 1, imm=10)])
        self.assertEqual(stop, Stop("fallo", CAUSE_INSTRUCTION_MISALIGNED, CODE + 10))
        self.assertEqual(hart.regs[1], 0)

    def test_loads_and_stores(self) -> None:
        words = [
            encode(Kind.SW, 0, 10, 5, imm=0), encode(Kind.LB, 1, 10, imm=3), encode(Kind.LBU, 2, 10, imm=3),
            encode(Kind.LH, 3, 10, imm=2), encode(Kind.LHU, 4, 10, imm=2), encode(Kind.LW, 6, 10, imm=0),
            encode(Kind.SB, 0, 10, 7, imm=4), encode(Kind.SH, 0, 10, 7, imm=6), encode(Kind.LW, 8, 10, imm=4),
            encode(Kind.LW, 9, 10, imm=1), encode(Kind.SW, 0, 10, 5, imm=9), encode(Kind.LW, 12, 10, imm=8),
            encode(Kind.EBREAK),
        ]
        hart, stop = self.run_words(words, regs={10: DATA, 5: 0x80FF7F01, 7: 0xAABBCCDD})
        self.assertEqual(stop.reason, "ebreak")
        self.assertEqual(hart.regs[1], 0xFFFFFF80)
        self.assertEqual(hart.regs[2], 0x80)
        self.assertEqual(hart.regs[3], 0xFFFF80FF)
        self.assertEqual(hart.regs[4], 0x80FF)
        self.assertEqual(hart.regs[6], 0x80FF7F01)
        self.assertEqual(hart.regs[8], 0xCCDD00DD)
        self.assertEqual(hart.regs[9], 0xDD80FF7F)       # lw desalineada
        self.assertEqual(hart.regs[12], 0xFF7F0100)      # sw desalineada en +9 escribió 01 7F FF 80 en 9..12

    def test_sltiu_with_negative_immediate_and_shifts(self) -> None:
        hart, stop = self.run_words([encode(Kind.SLTIU, 1, 5, imm=-1), encode(Kind.SLTI, 2, 5, imm=-1),
                                     encode(Kind.SRAI, 3, 6, imm=31), encode(Kind.SRLI, 4, 6, imm=31),
                                     encode(Kind.SLL, 7, 6, 8), encode(Kind.EBREAK)],
                                    regs={5: 0xFFFFFFFE, 6: 0x80000000, 8: 33})
        self.assertEqual(hart.regs[1:5], [1, 1, 0xFFFFFFFF, 1])  # −2 < −1 con signo; 0xFFFFFFFE < 0xFFFFFFFF sin signo
        self.assertEqual(hart.regs[7], 0)

    def test_faults_leave_state_untouched(self) -> None:
        cases = [
            ([encode(Kind.LW, 1, 5, imm=0)], {5: BASE + RAM}, Stop("fallo", CAUSE_LOAD_ACCESS_FAULT, BASE + RAM)),
            ([encode(Kind.LW, 1, 5, imm=0)], {5: BASE + RAM - 2}, Stop("fallo", CAUSE_LOAD_ACCESS_FAULT, BASE + RAM - 2)),
            ([encode(Kind.SB, 0, 5, 6, imm=-1)], {5: BASE, 6: 7}, Stop("fallo", CAUSE_STORE_ACCESS_FAULT, BASE - 1)),
            ([0x00000000], {}, Stop("fallo", CAUSE_ILLEGAL_INSTRUCTION, 0)),
            ([encode(Kind.MUL, 1, 2, 3)], {}, Stop("fallo", CAUSE_ILLEGAL_INSTRUCTION, encode(Kind.MUL, 1, 2, 3))),
            ([encode(Kind.ECALL)], {}, Stop("ecall", CAUSE_ECALL_FROM_M, 0)),
            ([encode(Kind.EBREAK)], {}, Stop("ebreak", CAUSE_BREAKPOINT, 0)),
            ([encode(Kind.JALR, 0, 5, imm=0)], {5: BASE + RAM}, Stop("fallo", CAUSE_INSTRUCTION_ACCESS_FAULT, BASE + RAM)),
        ]
        for words, regs, expected in cases:
            with self.subTest(expected=expected):
                hart, memory = make_hart(assemble(words))
                self.addCleanup(memory.close)
                for index, value in regs.items():
                    hart.regs[index] = value
                before = (list(hart.regs), bytes(memory.lanes), list(memory.generations))
                stop = hart.run(50)
                self.assertEqual(stop, expected)
                after = (list(hart.regs), bytes(memory.lanes), list(memory.generations))
                self.assertEqual(before, after)
                if expected.cause == CAUSE_INSTRUCTION_ACCESS_FAULT:
                    self.assertEqual((hart.pc, hart.instret, hart.gas), (BASE + RAM, 1, 49))  # el jalr sí se confirmó
                else:
                    self.assertEqual((hart.pc, hart.instret, hart.gas), (CODE, 0, 50))

    def test_csr_counters(self) -> None:
        words = [encode(Kind.ADDI, 1, 0, imm=1), encode(Kind.CSRRS, 2, 0, imm=CSR_INSTRET),
                 encode(Kind.CSRRS, 3, 0, imm=CSR_CYCLE), encode(Kind.CSRRSI, 4, 0, imm=CSR_TIME),
                 encode(Kind.CSRRC, 5, 0, imm=0xC82), encode(Kind.CSRRS, 0, 0, imm=CSR_CYCLE),
                 encode(Kind.CSRRS, 6, 0, imm=CSR_MSTATUS)]
        hart, stop = self.run_words(words)
        self.assertEqual(stop, Stop("fallo", CAUSE_ILLEGAL_INSTRUCTION, words[-1]))
        self.assertEqual(hart.regs[2:6], [1, 2, 3, 0])
        self.assertEqual(hart.instret, 6)
        # Mitades altas: con instret ≥ 2³² las lecturas bajas envuelven y las altas valen 1.
        words = [encode(Kind.CSRRS, 1, 0, imm=0xC82), encode(Kind.CSRRS, 2, 0, imm=CSR_INSTRET),
                 encode(Kind.CSRRS, 3, 0, imm=0xC80), encode(Kind.CSRRS, 4, 0, imm=0xC81),
                 encode(Kind.CSRRS, 5, 0, imm=CSR_TIME), encode(Kind.EBREAK)]
        hart, memory = make_hart(assemble(words))
        self.addCleanup(memory.close)
        hart.instret = (1 << 32) + 5
        self.assertEqual(hart.run(10).reason, "ebreak")
        self.assertEqual(hart.regs[1:6], [1, 6, 1, 1, 9])
        self.assertEqual(hart.instret, (1 << 32) + 10)
        hart = Rv32Hart(memory, time_divisor=4)
        hart.instret = (1 << 34) + 7
        self.assertEqual(hart.run(10).reason, "ebreak")
        # instreth en 2³⁴+7 → 4; instret en +8 → 8; cycleh en +9 → 4; timeh en +10 → (2³⁴+10)//4 >> 32 = 1;
        # time en +11 → (2³⁴+11)//4 = 2³² + 2 → 2.
        self.assertEqual(hart.regs[1:6], [4, 8, 4, 1, 2])
        with self.assertRaises(ValueError):
            Rv32Hart(memory, time_divisor=0)
        # csrrci con uimm = 0 es una lectura legal; fence y fence.i se ejecutan sin efecto y cuentan.
        words = [encode(Kind.FENCE, 0, 0, imm=0x0FF), encode(Kind.FENCE_I), encode(Kind.CSRRCI, 7, 0, imm=CSR_INSTRET),
                 encode(Kind.FENCE, 0, 0, imm=0x033), encode(Kind.ADDI, 1, 0, imm=1), encode(Kind.EBREAK)]
        hart, stop = self.run_words(words)
        self.assertEqual((stop.reason, hart.regs[7], hart.regs[1], hart.instret), ("ebreak", 2, 1, 5))
        for word in (encode(Kind.CSRRW, 1, 2, imm=CSR_CYCLE), encode(Kind.CSRRS, 1, 2, imm=CSR_CYCLE),
                     encode(Kind.CSRRSI, 1, 3, imm=CSR_INSTRET), encode(Kind.CSRRWI, 1, 0, imm=CSR_CYCLE)):
            hart, stop = self.run_words([word])
            self.assertEqual(stop, Stop("fallo", CAUSE_ILLEGAL_INSTRUCTION, word), hex(word))

    def test_gas_is_exact(self) -> None:
        words = [encode(Kind.ADDI, 1, 1, imm=1)] * 40 + [encode(Kind.EBREAK)]
        hart, stop = self.run_words(words, gas=10)
        self.assertEqual((stop.reason, hart.instret, hart.gas, hart.regs[1], hart.pc), ("gas", 10, 0, 10, CODE + 40))
        self.assertEqual(hart.run(0), Stop("gas"))
        stop = hart.run(1000)
        self.assertEqual((stop.reason, hart.instret, hart.gas, hart.regs[1]), ("ebreak", 40, 1000 - 30 * GAS_PER_INSTRUCTION, 40))

    def test_self_modifying_code_is_seen_immediately(self) -> None:
        # Escribe sobre la instrucción siguiente (misma página) y sobre otra página.
        words = [encode(Kind.SW, 0, 10, 5, imm=8), encode(Kind.ADDI, 0, 0, imm=0),
                 encode(Kind.ADDI, 1, 0, imm=1), encode(Kind.SW, 0, 11, 6, imm=0), encode(Kind.JALR, 0, 11, imm=0)]
        image = assemble(words) + bytes(0x1000 - 4 * len(words)) + assemble([encode(Kind.ADDI, 2, 0, imm=2), encode(Kind.EBREAK)])
        hart, memory = make_hart(image)
        self.addCleanup(memory.close)
        hart.regs[10] = CODE
        hart.regs[5] = encode(Kind.ADDI, 1, 0, imm=9)        # sustituye «addi x1, x0, 1»
        hart.regs[11] = CODE + 0x1000
        hart.regs[6] = encode(Kind.ADDI, 2, 0, imm=7)        # sustituye «addi x2, x0, 2» en la otra página
        stop = hart.run(100)
        self.assertEqual((stop.reason, hart.regs[1], hart.regs[2]), ("ebreak", 9, 7))
        # Segunda ejecución tras reset (caché vacía) con otros parches: mismo mecanismo, otro resultado.
        regs = hart.regs
        hart.reset(CODE)
        self.assertIs(hart.regs, regs)   # reset limpia la misma lista
        hart.regs[10], hart.regs[5], hart.regs[11], hart.regs[6] = CODE, encode(Kind.ADDI, 1, 0, imm=4), CODE + 0x1000, encode(Kind.ADDI, 2, 0, imm=3)
        stop = hart.run(100)
        self.assertEqual((stop.reason, hart.regs[1], hart.regs[2]), ("ebreak", 4, 3))

    def test_overwriting_an_executed_instruction_is_seen_on_the_next_pass(self) -> None:
        # Criterio 6: la instrucción parcheada ya se ejecutó (está en la caché de decodificación) y un
        # salto hacia atrás vuelve a pasar por ella; en la misma página y en otra página, con y sin cortes.
        same_page = [
            encode(Kind.ADDI, 1, 1, imm=1),            # 0: primera pasada +1; segunda pasada, parcheada, +9
            encode(Kind.BNE, 0, 2, 0, imm=8),          # 4: en la segunda pasada x2 ≠ 0 → salta al ebreak
            encode(Kind.JAL, 0, imm=8),                # 8: primera pasada: sigue en 16
            encode(Kind.EBREAK),                       # 12
            encode(Kind.SW, 0, 10, 5, imm=0),          # 16: parchea la palabra 0
            encode(Kind.ADDI, 2, 0, imm=1),            # 20
            encode(Kind.JALR, 0, 11, imm=0),           # 24: vuelve a 0
        ]
        other_page = same_page[:4] + [encode(Kind.JALR, 0, 12, imm=0)]   # 16: salta a la página siguiente
        far = [encode(Kind.SW, 0, 10, 5, imm=0), encode(Kind.ADDI, 2, 0, imm=1), encode(Kind.JALR, 0, 11, imm=0)]
        patch = encode(Kind.ADDI, 1, 1, imm=9)                             # 0x00908093
        self.assertEqual((patch, same_page[0]), (0x00908093, 0x00108093))
        # Variantes del parche de la misma página: sw entera, sb del byte 2, sh alineada y sh desalineada
        # (vía lenta, cruza una palabra): todas dejan la palabra 0x00908093.
        patches = ((encode(Kind.SW, 0, 10, 5, imm=0), patch), (encode(Kind.SB, 0, 10, 5, imm=2), 0x90),
                   (encode(Kind.SH, 0, 10, 5, imm=2), 0x0090), (encode(Kind.SH, 0, 10, 5, imm=1), 0x9080))
        # Confirmadas: misma página 6 + 2; otra página 7 + 2 (el jalr de ida a la otra página).
        images = [(f"misma página, parche {i}", assemble(same_page[:4] + [store] + same_page[5:]), value, 8)
                  for i, (store, value) in enumerate(patches)]
        images.append(("otra página", assemble(other_page) + bytes(0x1000 - 4 * len(other_page)) + assemble(far), patch, 9))
        for name, image, value, instret in images:
            for cuts in (None, 1, 3):
                with self.subTest(name, cuts=cuts):
                    hart, memory = make_hart(image)
                    self.addCleanup(memory.close)
                    hart.regs[10], hart.regs[11], hart.regs[12], hart.regs[5] = CODE, CODE, CODE + 0x1000, value
                    stop = Stop("gas")
                    if cuts is None:
                        stop = hart.run(100)
                    else:
                        while stop.reason == "gas":
                            stop = hart.run(cuts)
                    self.assertEqual((stop.reason, hart.regs[1], hart.regs[2], hart.instret), ("ebreak", 10, 1, instret))
                    self.assertEqual(memory.load32(CODE), patch)

    def test_store_crossing_into_the_next_page_invalidates_its_decoded_code(self) -> None:
        # sh en +0xFFF escribe el byte 0 de una instrucción ya ejecutada de la página 1 (rd 2 → 3).
        page0 = [encode(Kind.JAL, 0, imm=0x1000), encode(Kind.BNE, 0, 4, 0, imm=16), encode(Kind.SH, 0, 10, 5, imm=0x7FF),
                 encode(Kind.ADDI, 4, 0, imm=1), encode(Kind.JAL, 0, imm=-16), encode(Kind.EBREAK)]
        page1 = [encode(Kind.ADDI, 2, 0, imm=2), encode(Kind.JALR, 0, 11, imm=0)]
        self.assertEqual(page1[0], 0x00200113)
        image = assemble(page0) + bytes(0x1000 - 4 * len(page0)) + assemble(page1)
        for cuts in (None, 1, 2):
            with self.subTest(cuts=cuts):
                hart, memory = make_hart(image)
                self.addCleanup(memory.close)
                hart.regs[10], hart.regs[11], hart.regs[5] = CODE + 0x800, CODE + 4, 0x9300
                stop = Stop("gas")
                if cuts is None:
                    stop = hart.run(100)
                else:
                    while stop.reason == "gas":
                        stop = hart.run(cuts)
                self.assertEqual((stop.reason, hart.regs[2], hart.regs[3], hart.regs[4], hart.instret), ("ebreak", 2, 2, 1, 11))
                self.assertEqual(memory.load32(CODE + 0x1000), 0x00200193)
                self.assertEqual(memory.load8u(CODE + 0xFFF), 0)

    def test_fetch_below_the_base_faults_without_executing_the_last_page(self) -> None:
        # Hallazgo bloqueante de la revisión ciega 1: la «página −1» coincidía con el centinela de la caché
        # y el fetch en base − 4 ejecutaba la última palabra de la RAM, además con un resultado distinto
        # según los cortes de gas.
        words = [encode(Kind.LUI, 5, imm=alu.to_signed(BASE)), encode(Kind.ADDI, 5, 5, imm=-4), encode(Kind.JALR, 0, 5, imm=0)]
        image = assemble(words) + bytes(RAM - 4 * len(words) - 4) + assemble([encode(Kind.ADDI, 6, 0, imm=55)])
        expected = Stop("fallo", CAUSE_INSTRUCTION_ACCESS_FAULT, BASE - 4)
        for cuts in (None, [3, 100], [1, 1, 1, 100], [2, 1, 100]):
            with self.subTest(cuts=cuts):
                hart, memory = make_hart(image)
                self.addCleanup(memory.close)
                if cuts is None:
                    stop = hart.run(100)
                else:
                    for chunk in cuts:
                        stop = hart.run(chunk)
                        if stop.reason != "gas":
                            break
                self.assertEqual(stop, expected)
                self.assertEqual((hart.regs[6], hart.pc, hart.instret), (0, BASE - 4, 3))
        for pc in (BASE - 4, BASE - 4096, BASE - 8192, BASE - 4 * 1024 * 1024, 0, 4, BASE + RAM, 0xFFFFFFFC):
            with self.subTest(pc=pc):
                hart, memory = make_hart(image)
                self.addCleanup(memory.close)
                hart.pc = pc
                self.assertEqual(hart.run(10), Stop("fallo", CAUSE_INSTRUCTION_ACCESS_FAULT, pc))
                self.assertEqual((hart.regs[6], hart.pc, hart.instret, hart.gas), (0, pc, 0, 10))

    def test_pc_wraps_at_the_top_of_the_address_space(self) -> None:
        memory = Rv32Memory(4096, base=0xFFFFF000)
        self.addCleanup(memory.close)
        memory.load_bytes(0xFFFFFFF8, assemble([encode(Kind.ADDI, 1, 0, imm=1), encode(Kind.ADDI, 2, 0, imm=2)]))
        hart = Rv32Hart(memory)
        hart.reset(0xFFFFFFF8)
        self.assertEqual(hart.run(10), Stop("fallo", CAUSE_INSTRUCTION_ACCESS_FAULT, 0))
        self.assertEqual((hart.regs[1], hart.regs[2], hart.pc, hart.instret, hart.gas), (1, 2, 0, 2, 8))
        hart.reset(0xFFFFFFFC)
        self.assertEqual(hart.run(1), Stop("gas"))
        self.assertEqual((hart.pc, hart.instret), (0, 1))
        self.assertEqual(hart.run(5), Stop("fallo", CAUSE_INSTRUCTION_ACCESS_FAULT, 0))
        # Enlaces en la última palabra: jal/jalr escriben (pc + 4) & M = 0 y auipc toma el pc sin envolver.
        memory.load_bytes(0xFFFFFFF4, assemble([encode(Kind.EBREAK), encode(Kind.AUIPC, 2, imm=0), encode(Kind.JAL, 1, imm=-8)]))
        hart.reset(0xFFFFFFF8)
        self.assertEqual(hart.run(10), Stop("ebreak", 3, 0))
        self.assertEqual((hart.regs[1], hart.regs[2], hart.pc, hart.instret), (0, 0xFFFFFFF8, 0xFFFFFFF4, 2))
        memory.load_bytes(0xFFFFFFFC, assemble([encode(Kind.JALR, 3, 4, imm=0)]))
        hart.reset(0xFFFFFFFC)
        hart.regs[4] = 0xFFFFFFF4
        self.assertEqual(hart.run(10), Stop("ebreak", 3, 0))
        self.assertEqual((hart.regs[3], hart.pc), (0, 0xFFFFFFF4))

    def test_attach_memory_switches_the_mirrors_and_empties_the_cache(self) -> None:
        hart, memory = make_hart(assemble([encode(Kind.ADDI, 1, 0, imm=1), encode(Kind.EBREAK)]))
        self.addCleanup(memory.close)
        self.assertEqual((hart.run(10).reason, hart.regs[1]), ("ebreak", 1))
        other = Rv32Memory(1 << 14, base=0x40000000)
        self.addCleanup(other.close)
        other.load_bytes(0x40000000, assemble([encode(Kind.ADDI, 1, 0, imm=2), encode(Kind.SW, 0, 0, 1, imm=0), encode(Kind.EBREAK)]))
        hart.attach_memory(other)
        hart.reset()
        self.assertEqual((hart.pc, hart.base, hart.size, hart.words_u is other.words_u, hart.gens is other.generations),
                         (0x40000000, 0x40000000, 1 << 14, True, True))
        self.assertEqual(hart.run(10), Stop("fallo", CAUSE_STORE_ACCESS_FAULT, 0))
        self.assertEqual((hart.regs[1], hart.instret), (2, 1))
        self.assertEqual(memory.load32(BASE), encode(Kind.ADDI, 1, 0, imm=1))   # la memoria antigua no se tocó

    def test_run_rejects_misaligned_pc_and_negative_gas(self) -> None:
        hart, memory = make_hart(assemble([encode(Kind.EBREAK)]))
        self.addCleanup(memory.close)
        hart.pc = CODE + 2
        self.assertEqual(hart.run(10), Stop("fallo", CAUSE_INSTRUCTION_MISALIGNED, CODE + 2))
        self.assertEqual((hart.gas, hart.pc, hart.instret), (10, CODE + 2, 0))   # sin cobrar, como las demás paradas
        self.assertEqual(hart.run(0), Stop("gas"))
        self.assertEqual(hart.gas, 0)
        with self.assertRaises(ValueError):
            hart.run(-1)


class CodeLineMapInTheHart(unittest.TestCase):
    """Refinamiento de P1.6: solo los stores sobre líneas con código decodificado suben la generación."""

    def run_with_cuts(self, hart: Rv32Hart, cuts: int | None, gas: int) -> Stop:
        if cuts is None:
            return hart.run(gas)
        stop = Stop("gas")
        while stop.reason == "gas":
            stop = hart.run(cuts)
        return stop

    def test_data_stores_in_the_code_page_keep_the_decode_cache(self) -> None:
        # Bucle de 20 vueltas que escribe sw/sb/sh sobre datos de su propia página (línea 0x800, sin código):
        # la generación no cambia, la lista de decodificación es la misma y el resultado es el de referencia.
        words = [encode(Kind.ADDI, 1, 1, imm=1), encode(Kind.SW, 0, 10, 1, imm=0), encode(Kind.SB, 0, 10, 1, imm=4),
                 encode(Kind.SH, 0, 10, 1, imm=6), encode(Kind.ADDI, 20, 20, imm=-1), encode(Kind.BNE, 0, 20, 0, imm=-20),
                 encode(Kind.EBREAK)]
        image = assemble(words)
        reference = Reference(image)
        reference.regs[10], reference.regs[20] = CODE + 0x800, 20
        expected = reference.run(1000)
        self.assertEqual(expected.reason, "ebreak")
        for cuts in (None, 1, 7):
            with self.subTest(cuts=cuts):
                hart, memory = make_hart(image)
                self.addCleanup(memory.close)
                hart.regs[10], hart.regs[20] = CODE + 0x800, 20
                self.assertEqual(hart.run(3), Stop("gas"))            # las tres primeras ya están decodificadas
                entry = hart.decode_cache[0]
                stop = self.run_with_cuts(hart, cuts, 1000)
                self.assertEqual(stop, expected)
                self.assertEqual((hart.regs, hart.pc, hart.instret), (reference.regs, reference.pc, reference.count))
                self.assertEqual(bytes(memory.lanes), bytes(reference.mem))
                self.assertEqual(memory.generation(0), 1)             # solo la carga de la imagen
                self.assertIs(hart.decode_cache[0], entry)            # la misma entrada: nada se descartó
                self.assertEqual(memory.written_pages(), [0])
                self.assertEqual([i for i, c in enumerate(memory.code) if c], [0, 1])   # 7 instrucciones: 2 líneas

    def test_store_into_a_code_line_is_conservative_and_lazy_decoding_sees_later_words(self) -> None:
        patch = encode(Kind.ADDI, 3, 0, imm=7)
        # (a) sw sobre la palabra aún no ejecutada (0xC, un ebreak) de una línea con instrucciones ya decodificadas:
        # sube la generación (sobreaproximación) y la palabra nueva se ejecuta.
        words = [encode(Kind.ADDI, 1, 0, imm=1), encode(Kind.SW, 0, 10, 5, imm=0xC), encode(Kind.ADDI, 2, 0, imm=2),
                 encode(Kind.EBREAK), encode(Kind.EBREAK)]
        for cuts in (None, 1, 2):
            with self.subTest("misma línea", cuts=cuts):
                hart, memory = make_hart(assemble(words))
                self.addCleanup(memory.close)
                hart.regs[10], hart.regs[5] = CODE, patch
                stop = self.run_with_cuts(hart, cuts, 100)
                self.assertEqual((stop.reason, hart.regs[1], hart.regs[2], hart.regs[3], hart.pc, hart.instret),
                                 ("ebreak", 1, 2, 7, CODE + 0x10, 4))
                self.assertEqual(memory.generation(0), 2)   # la carga y el store sobre una línea de código
        # (b) sw sobre una línea sin ninguna palabra decodificada (0x10..0x1F) que se ejecuta después: la
        # decodificación es perezosa y ve la palabra nueva sin que la generación cambie.
        words = [encode(Kind.ADDI, 1, 0, imm=1), encode(Kind.SW, 0, 10, 5, imm=0x14), encode(Kind.ADDI, 2, 0, imm=2),
                 encode(Kind.JAL, 0, imm=8), encode(Kind.EBREAK), encode(Kind.EBREAK), encode(Kind.EBREAK)]
        for cuts in (None, 1, 2):
            with self.subTest("línea sin decodificar", cuts=cuts):
                hart, memory = make_hart(assemble(words))
                self.addCleanup(memory.close)
                hart.regs[10], hart.regs[5] = CODE, patch
                stop = self.run_with_cuts(hart, cuts, 100)
                self.assertEqual((stop.reason, hart.regs[3], hart.pc, hart.instret), ("ebreak", 7, CODE + 0x18, 5))
                self.assertEqual(memory.generation(0), 1)   # solo la carga: la línea 0x10 no tenía código
                self.assertEqual([i for i, c in enumerate(memory.code) if c], [0, 1])

    def test_halfword_store_crossing_from_a_data_line_into_a_code_line_invalidates(self) -> None:
        # La instrucción de 0x10 («addi x1, x1, 1») ya se ejecutó; un sh en 0xF (vía lenta: cruza línea) escribe
        # 0x00 en el dato 0xF y 0x13 en el byte 0 de la instrucción, que pasa a «addi x0, x1, 1».
        words = [encode(Kind.JAL, 0, imm=0x10), 0, 0, 0,
                 encode(Kind.ADDI, 1, 1, imm=1),            # 0x10
                 encode(Kind.BNE, 0, 2, 0, imm=0x18),       # 0x14 → 0x2C
                 encode(Kind.SH, 0, 10, 5, imm=0xF),        # 0x18
                 encode(Kind.ADDI, 2, 0, imm=1),            # 0x1C
                 encode(Kind.JAL, 0, imm=-16),              # 0x20 → 0x10
                 encode(Kind.EBREAK), encode(Kind.EBREAK),  # 0x24, 0x28
                 encode(Kind.EBREAK)]                       # 0x2C
        self.assertEqual(words[4], 0x00108093)
        for cuts in (None, 1, 3):
            with self.subTest(cuts=cuts):
                hart, memory = make_hart(assemble(words))
                self.addCleanup(memory.close)
                hart.regs[10], hart.regs[5] = CODE, 0x1300
                stop = self.run_with_cuts(hart, cuts, 100)
                self.assertEqual((stop.reason, hart.regs[1], hart.regs[2], hart.pc, hart.instret), ("ebreak", 1, 1, CODE + 0x2C, 8))
                self.assertEqual(memory.load32(CODE + 0x10), 0x00108013)
                self.assertEqual((memory.load8u(CODE + 0xF), memory.generation(0)), (0, 2))

    def test_host_writes_invalidate_decoded_code(self) -> None:
        hart, memory = make_hart(assemble([encode(Kind.ADDI, 1, 0, imm=1), encode(Kind.EBREAK)]))
        self.addCleanup(memory.close)
        self.assertEqual((hart.run(10).reason, hart.regs[1]), ("ebreak", 1))
        memory.load_bytes(CODE, assemble([encode(Kind.ADDI, 1, 0, imm=2)]))   # el host reescribe la instrucción ejecutada
        self.assertEqual(memory.generation(0), 2)
        hart.pc = CODE
        self.assertEqual((hart.run(10).reason, hart.regs[1]), ("ebreak", 2))
        memory.fill([encode(Kind.ADDI, 1, 0, imm=3)], CODE)
        hart.pc = CODE
        self.assertEqual((hart.run(10).reason, hart.regs[1], memory.generation(0)), ("ebreak", 3, 3))

    def test_a_store_over_the_instruction_being_decoded_for_the_first_time_is_seen(self) -> None:
        # Reproductor del oráculo matemático (mata el mutante «marcar después de ejecutar»): el sw de 0x20 se
        # reescribe a sí mismo en su primera ejecución como «addi x6, x6, 100»; las dos vueltas siguientes deben
        # ejecutar la palabra nueva. Resultado calculado a mano: x5 = 3, x6 = 200, x24 = 0, instret = 20.
        patch = encode(Kind.ADDI, 6, 6, imm=100)
        self.assertEqual(patch, 0x06430313)
        words = [encode(Kind.ADDI, 24, 0, imm=3), encode(Kind.AUIPC, 10, imm=0), encode(Kind.ADDI, 10, 10, imm=0x1C),
                 encode(Kind.LUI, 25, imm=0x06430000), encode(Kind.ADDI, 25, 25, imm=0x313),
                 encode(Kind.ADDI, 0, 0, imm=0), encode(Kind.ADDI, 0, 0, imm=0), encode(Kind.ADDI, 0, 0, imm=0),
                 encode(Kind.SW, 0, 10, 25, imm=0),            # 0x20: se sobrescribe a sí mismo
                 encode(Kind.ADDI, 5, 5, imm=1),               # 0x24
                 encode(Kind.ADDI, 24, 24, imm=-1),            # 0x28
                 encode(Kind.BNE, 0, 24, 0, imm=-12),          # 0x2C → 0x20
                 encode(Kind.EBREAK)]
        for accelerate, cuts in ((False, None), (False, 1), (False, 3), (True, None), (True, 3), (True, 7)):
            with self.subTest(accelerate=accelerate, cuts=cuts):
                hart, memory = make_hart(assemble(words))
                self.addCleanup(memory.close)
                hart.accelerate = accelerate
                hart.warmup = 1
                stop = self.run_with_cuts(hart, cuts, 100)
                self.assertEqual((stop.reason, hart.regs[5], hart.regs[6], hart.regs[24], hart.instret), ("ebreak", 3, 200, 0, 20))
                self.assertEqual(memory.load32(CODE + 0x20), patch)

    def test_misaligned_stores_crossing_into_fresh_pages_mark_them_written(self) -> None:
        # Hallazgo de la ciega 1 y del oráculo: un sw desalineado en 0x1FFE y un sh en 0x4FFF tocan páginas
        # vírgenes (2 y 5) que deben quedar escritas, o el snapshot perdería sus bytes.
        words = [encode(Kind.SW, 0, 10, 5, imm=0), encode(Kind.SH, 0, 11, 5, imm=0), encode(Kind.EBREAK)]
        for accelerate in (False, True):
            with self.subTest(accelerate=accelerate):
                hart, memory = make_hart(assemble(words))
                self.addCleanup(memory.close)
                hart.accelerate = accelerate
                hart.warmup = 1
                hart.regs[10], hart.regs[11], hart.regs[5] = CODE + 0x1FFE, CODE + 0x4FFF, 0x11223344
                self.assertEqual(hart.run(10).reason, "ebreak")
                self.assertEqual(memory.written_pages(), [0, 1, 2, 4, 5])
                self.assertEqual((memory.load32(CODE + 0x1FFE), memory.load16u(CODE + 0x4FFF)), (0x11223344, 0x3344))
                restored = Rv32Memory(RAM)
                self.addCleanup(restored.close)
                restored.restore_pages(memory.snapshot_pages())
                self.assertEqual(bytes(restored.lanes), bytes(memory.lanes))

    def test_two_harts_sharing_a_memory_stay_coherent(self) -> None:
        # Hallazgo de las dos revisiones: crear (o reiniciar) un segundo hart sobre la misma memoria vacía el
        # mapa de código; como clear_code sube la generación de las páginas marcadas, las cachés del primero
        # dejan de coincidir y el parche que escribe el segundo se ve.
        memory = Rv32Memory(RAM)
        self.addCleanup(memory.close)
        memory.load_bytes(CODE, assemble([encode(Kind.ADDI, 1, 1, imm=1), encode(Kind.EBREAK)]))
        memory.load_bytes(CODE + 0x1000, assemble([encode(Kind.SW, 0, 10, 5, imm=0), encode(Kind.EBREAK)]))
        for accelerate in (False, True):
            with self.subTest(accelerate=accelerate):
                first = Rv32Hart(memory)
                first.accelerate = accelerate
                first.warmup = 1
                first.reset(CODE)
                memory.load_bytes(CODE, assemble([encode(Kind.ADDI, 1, 1, imm=1)]))
                self.assertEqual((first.run(10).reason, first.regs[1]), ("ebreak", 1))
                before = memory.generation(0)
                second = Rv32Hart(memory)                        # vacía el mapa: la página 0 sube de generación
                self.assertEqual(memory.generation(0), before + 1)
                second.pc = CODE + 0x1000
                second.regs[10], second.regs[5] = CODE, encode(Kind.ADDI, 1, 1, imm=5)
                self.assertEqual(second.run(10).reason, "ebreak")
                first.pc = CODE
                first.regs[1] = 0
                self.assertEqual((first.run(10).reason, first.regs[1]), ("ebreak", 5))
                # Y al revés: el primero vuelve a cachear, el segundo se reinicia y parchea otra vez.
                second.reset(CODE + 0x1000)
                second.regs[10], second.regs[5] = CODE, encode(Kind.ADDI, 1, 1, imm=9)
                self.assertEqual(second.run(10).reason, "ebreak")
                first.pc = CODE
                first.regs[1] = 0
                self.assertEqual((first.run(10).reason, first.regs[1]), ("ebreak", 9))

    def test_reset_and_attach_memory_clear_the_code_map(self) -> None:
        hart, memory = make_hart(assemble([encode(Kind.ADDI, 1, 0, imm=1), encode(Kind.EBREAK)]))
        self.addCleanup(memory.close)
        self.assertEqual(hart.run(10).reason, "ebreak")
        self.assertEqual([i for i, c in enumerate(memory.code) if c], [0])
        hart.reset()
        self.assertFalse(any(memory.code))
        self.assertEqual(memory.generation(0), 2)                          # la carga y el vaciado del mapa
        self.assertEqual(hart.run(10).reason, "ebreak")                   # vuelve a marcar al decodificar
        self.assertEqual([i for i, c in enumerate(memory.code) if c], [0])
        other = Rv32Memory(RAM)
        self.addCleanup(other.close)
        other.mark_code(0x40, 4)
        hart.attach_memory(other)
        self.assertFalse(any(other.code))                                  # attach_memory vacía el mapa de la nueva
        self.assertEqual([i for i, c in enumerate(memory.code) if c], [0])   # y no toca el de la antigua
        self.assertIs(hart.code, other.code)
        self.assertIs(hart.written, other.written)


class CoreMarkOnTheHart(unittest.TestCase):
    def test_coremark_rv32i_runs_to_poweroff_with_the_e1_count(self) -> None:
        self.assertTrue(COREMARK.is_file(), "falta el binario versionado de CoreMark (benchmarks/rv32_arranque/bin)")
        machine = Machine(ram_bytes=1 << 20)
        self.addCleanup(machine.close)
        machine.load_flat(COREMARK.read_bytes())
        stop = machine.run(20_000_000)
        self.assertEqual(stop, Stop("apagado"))
        self.assertEqual(machine.hart.instret, COREMARK_INSTRUCTIONS)
        text = machine.uart_text
        for line in ("[0]crclist       : 0xe714", "[0]crcmatrix     : 0x1fd7", "[0]crcstate      : 0x8e3a",
                     "[0]crcfinal      : 0xfcaf", "Total ticks      : 8458371"):
            self.assertIn(line, text)


if __name__ == "__main__":
    unittest.main()
