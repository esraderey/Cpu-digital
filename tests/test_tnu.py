"""Anclas del Chip Neuronal Tramoya (TNU): capacidades, atomicidad, gas, ROM y semántica."""

from __future__ import annotations

import json
import math
import struct
import tempfile
import unittest
import zlib
from array import array
from pathlib import Path
from unittest import mock

from cpu_digital import tnu
from cpu_digital import vm32 as vm32_module
from cpu_digital.memory import PAGE_WORDS, PagedMemory
from cpu_digital.tnu import ROM_BASE, TensorROM, TramoyaNeuralUnit
from cpu_digital.vm32 import LOOP_WARMUP, SNAPSHOT_MAGIC, TramoyaVM32, VMConfig
from cpu_digital.vm32_assembler import VM32Assembler
from cpu_digital.vm32_isa import VM32_ISA, VMOpcode
from cpu_digital.vm32_loops import compile_loop

if not hasattr(math, "sumprod"):  # el resto de VM32 soporta 3.10; el TNU exige 3.12
    raise unittest.SkipTest("El TNU requiere Python 3.12 o superior (math.sumprod)")

NPU_CAPS = frozenset({"io", "introspection", "random", "memory", "npu"})
BUF = 4096  # dirección de trabajo en RAM, fuera del código


def f2w(value: float) -> int:
    return struct.unpack("<i", struct.pack("<f", value))[0]


def w2f(word: int) -> float:
    return struct.unpack("<f", struct.pack("<i", word))[0]


def f32(value: float) -> float:
    return array("f", [value])[0]


def program(source: str):
    return VM32Assembler().assemble(source).program


def rom_from_floats(values: list[float]) -> TensorROM:
    return TensorROM(array("f", values).tobytes(), name="rom-test")


def make_vm(source: str, *, rom: TensorROM | None = None, caps=NPU_CAPS, npu=True, **config) -> TramoyaVM32:
    config.setdefault("trace_size", 0)
    vm = TramoyaVM32(
        VMConfig(capabilities=caps, **config),
        npu=TramoyaNeuralUnit(rom) if npu else None,
    )
    vm.load_program(program(source))
    return vm


def put_floats(vm: TramoyaVM32, address: int, values: list[float]) -> None:
    for offset, value in enumerate(values):
        vm.write_memory(address + offset, f2w(value))


def get_floats(vm: TramoyaVM32, address: int, count: int) -> list[float]:
    return [w2f(word) for word in vm.memory_slice(address, count)]


def state(vm: TramoyaVM32) -> tuple:
    return (vm.pc, vm.registers, dict(vm.flags), vm.stack, vm.output, vm.result().gas_remaining,
            vm.result().instructions, vm.memory_slice(0, 3 * PAGE_WORDS))


def vector_program(body: str, vl: int, vr: int = 1, vs: int = 0) -> str:
    return f""".code
    MOVI R10, {vl}
    MOVI R11, {vr}
    MOVI R12, {vs}
    VCFG R10, R11, R12
{body}
    HALT
"""


TNU_MNEMONICS = [spec.mnemonic for code, spec in sorted(VM32_ISA.items()) if code >= 130]


class CapabilityAnchors(unittest.TestCase):
    def test_isa_declares_the_tnu_opcodes(self) -> None:
        self.assertEqual(
            TNU_MNEMONICS,
            ["VCFG", "VCOPY", "VADD", "VMUL", "VSCALE", "FDOT", "MATVEC", "MATTV", "RMSNORM",
             "VSOFTMAX", "VEXP", "VSILU", "ROPE", "VARGMAX", "VSAMPLE", "VQUANT", "QMATVEC", "QROW"],
        )

    def test_every_tnu_opcode_faults_without_npu_capability_and_changes_nothing(self) -> None:
        for code, spec in sorted(VM32_ISA.items()):
            if code < 130:
                continue
            with self.subTest(spec.mnemonic):
                operands = {"reg_reg": "R1, R2", "reg_reg_reg": "R1, R2, R3"}[spec.form]
                vm = make_vm(f".code\n    {spec.mnemonic} {operands}\n    HALT",
                             caps=frozenset({"io"}))
                before = state(vm)
                result = vm.run()
                self.assertEqual(result.state, "FAULTED")
                self.assertIn("npu", result.fault)
                self.assertEqual(state(vm)[:7], before[:7])

    def test_tnu_opcode_faults_when_no_chip_is_attached(self) -> None:
        vm = make_vm(vector_program("", 4), npu=False)
        result = vm.run()
        self.assertEqual(result.state, "FAULTED")
        self.assertIn("TNU", result.fault)

    def test_vector_ops_require_vcfg(self) -> None:
        vm = make_vm(".code\n    MOVI R1, 4096\n    VADD R1, R1, R1\n    HALT")
        result = vm.run()
        self.assertEqual(result.state, "FAULTED")
        self.assertIn("VCFG", result.fault)

    def test_vcfg_rejects_invalid_lengths(self) -> None:
        for vl, vr, vs in ((0, 1, 0), (-1, 1, 0), (tnu.MAX_TNU_LENGTH + 1, 1, 0), (4, -1, 0), (4, 1, -2)):
            with self.subTest((vl, vr, vs)):
                result = make_vm(vector_program("", vl, vr, vs)).run()
                self.assertEqual(result.state, "FAULTED")


class AtomicityAnchors(unittest.TestCase):
    def test_destination_range_past_ram_faults_without_partial_write(self) -> None:
        body = "    MOVI R1, 100\n    MOVI R2, 1044\n    VCOPY R2, R1"
        vm = make_vm(vector_program(body, 16), memory_words=1050)
        put_floats(vm, 100, [1.0] * 16)
        result = vm.run()
        self.assertEqual(result.state, "FAULTED")
        self.assertEqual(vm.memory_slice(1044, 6), (0,) * 6)

    def test_region_straddling_ram_end_and_rom_faults(self) -> None:
        vm = make_vm(vector_program("    MOVI R1, 250\n    VCOPY R1, R1", 16), memory_words=256)
        self.assertEqual(vm.run().state, "FAULTED")

    def test_write_to_rom_faults(self) -> None:
        rom = rom_from_floats([1.0] * 8)
        body = f"    MOVI R1, {ROM_BASE}\n    VADD R1, R1, R1"
        vm = make_vm(vector_program(body, 4), rom=rom)
        result = vm.run()
        self.assertEqual(result.state, "FAULTED")
        self.assertIn("ROM", result.fault)

    def test_failed_commit_rolls_back_multi_page_write(self) -> None:
        start = 2 * PAGE_WORDS - 2
        body = f"    MOVI R1, {BUF}\n    MOVI R2, {start}\n    VCOPY R2, R1"
        vm = make_vm(vector_program(body, 4), memory_words=4 * PAGE_WORDS)
        put_floats(vm, BUF, [1.0, 2.0, 3.0, 4.0])
        put_floats(vm, start, [9.0, 9.0, 9.0, 9.0])
        original = PagedMemory.write_bytes
        calls = []

        def failing(memory, address, data):
            # Solo falla la escritura del resultado; la de deshacer debe funcionar.
            calls.append(address)
            original(memory, address, data)
            if len(calls) == 1:
                raise RuntimeError("fallo inyectado tras escribir")

        with mock.patch.object(PagedMemory, "write_bytes", failing):
            result = vm.run()
        self.assertEqual(result.state, "FAULTED")
        self.assertTrue(calls)
        self.assertEqual(get_floats(vm, start, 4), [9.0, 9.0, 9.0, 9.0])

    def test_protected_code_cannot_be_a_destination(self) -> None:
        vm = make_vm(vector_program(f"    MOVI R1, {BUF}\n    MOVI R2, 0\n    VCOPY R2, R1", 4))
        result = vm.run()
        self.assertEqual(result.state, "FAULTED")
        self.assertIn("protegido", result.fault)

    def test_unprotected_code_write_invalidates_decode_cache(self) -> None:
        # Copia un HALT sobre la instrucción siguiente (dirección 28): debe ejecutarse el HALT nuevo.
        source = vector_program(
            f"    MOVI R1, {BUF}\n    MOVI R2, 28\n    VCOPY R2, R1\n    MOVI R1, 7\n    SYSCALL 1",
            4,
        )
        vm = make_vm(source, protect_code=False)
        vm.write_memory(BUF, int(VMOpcode.HALT))
        result = vm.run()
        self.assertEqual(result.state, "HALTED")
        self.assertEqual(result.output, ())

    def test_unprotected_code_write_invalidates_compiled_loops(self) -> None:
        # VCOPY cambia el inmediato de un bucle ya compilado: la segunda tanda suma 7, no 1.
        # Con 400 vueltas, lo que la primera tanda ejecuta compilada paga su compilación y la
        # segunda vuelve a compilar en cuanto se calienta.
        source = vector_program(
            f"    MOVI R1, {BUF}\n    LEA R2, L\n    ADDI R2, R2, 3\n    MOVI R8, 2\n"
            "OUT:\n    MOVI R9, 400\nL:\n    ADDI R5, R5, 1\n    SUBI R9, R9, 1\n    JNZ L\n"
            "    VCOPY R2, R1\n    SUBI R8, R8, 1\n    JNZ OUT",
            1,
        )
        states = []
        for accelerate in (False, True):
            vm = make_vm(source, protect_code=False, accelerate_loops=accelerate)
            vm.write_memory(BUF, 7)
            result = vm.run()
            self.assertEqual((result.state, vm.registers[5]), ("HALTED", 400 + 2800))
            states.append(state(vm))
        self.assertEqual(states[0], states[1])
        self.assertEqual(vm.accelerated_instructions, 2 * (400 - LOOP_WARMUP) * 3)

    def test_unprotected_code_write_restarts_the_loop_warmup(self) -> None:
        # Como con STORE: VCOPY reescribe un NOP del cuerpo en cada vuelta, así que la cabecera
        # nunca junta el calentamiento. Antes se intentaba compilar el bucle en cada vuelta.
        source = vector_program(
            f"    MOVI R1, {BUF}\n    LEA R2, OBJ\n    MOVI R9, 200\n"
            "L:\n    VCOPY R2, R1\n    ADDI R5, R5, 1\nOBJ:\n    NOP\n    SUBI R9, R9, 1\n    JNZ L",
            1,
        )
        states = []
        for accelerate in (False, True):
            vm = make_vm(source, protect_code=False, accelerate_loops=accelerate)
            with mock.patch("cpu_digital.vm32.compile_loop", wraps=compile_loop) as attempts:
                result = vm.run()
            self.assertEqual((result.state, vm.registers[5]), ("HALTED", 200))
            self.assertEqual(attempts.call_count, 0)
            states.append(state(vm))
        self.assertEqual(states[0], states[1])

    def test_unprotected_code_write_makes_later_compilations_wait(self) -> None:
        # Como con STORE: una escritura del TNU en código obliga a compilar otra vez, y cada
        # compilación aplaza la siguiente. Con 40 rondas de 40 vueltas el bucle se compila 3
        # veces, no una por ronda.
        source = vector_program(
            f"    MOVI R1, {BUF}\n    LEA R2, L\n    ADDI R2, R2, 3\n    MOVI R8, 40\n"
            "OUT:\n    MOVI R9, 40\nL:\n    ADDI R5, R5, 1\n    SUBI R9, R9, 1\n    JNZ L\n"
            "    VCOPY R2, R1\n    SUBI R8, R8, 1\n    JNZ OUT",
            1,
        )
        attempts: list[int] = []

        def noting(machine: TramoyaVM32, header: int):
            attempts.append(machine.result().instructions)
            return compile_loop(machine, header)

        vm = make_vm(source, protect_code=False)
        vm.write_memory(BUF, 7)
        with mock.patch("cpu_digital.vm32.compile_loop", noting):
            result = vm.run()
        self.assertEqual((result.state, vm.registers[5]), ("HALTED", 40 + 39 * 40 * 7))
        self.assertEqual(len(attempts), 3)
        # El bucle tiene 3 instrucciones. Tras compilar da 8 vueltas más en esa ronda, que el
        # acelerador ejecuta y cuentan doble al pagar la espera.
        wait = vm32_module.LOOP_COOLDOWN + 3 * vm32_module.LOOP_COOLDOWN_PER_INSTRUCTION
        for at, following in zip(attempts, attempts[1:]):
            self.assertGreaterEqual(following - at + (40 - LOOP_WARMUP) * 3, wait)


class GasAnchors(unittest.TestCase):
    def run_matvec(self, gas_limit: int):
        rows, cols = 7, 50
        body = f"    MOVI R1, {BUF}\n    MOVI R2, {BUF + 1000}\n    MOVI R3, {BUF + 2000}\n    MATVEC R1, R2, R3"
        vm = make_vm(vector_program(body, cols, rows), gas_limit=gas_limit)
        put_floats(vm, BUF + 1000, [0.5] * rows * cols)
        put_floats(vm, BUF + 2000, [2.0] * cols)
        return vm, vm.run(), rows * cols + tnu.TNU_ROW_UNITS * rows  # MAC + coste fijo por fila

    def test_gas_is_base_plus_ceil_work_over_divisor(self) -> None:
        vm, result, units = self.run_matvec(10_000)
        self.assertEqual(result.state, "HALTED")
        vcfg = VM32_ISA[int(VMOpcode.VCFG)].cost
        matvec = VM32_ISA[int(VMOpcode.MATVEC)].cost
        expected = 3 + vcfg + 3 + matvec + tnu.gas_extra(units) + 1  # MOVI×3, VCFG, MOVI×3, MATVEC, HALT
        self.assertEqual(10_000 - result.gas_remaining, expected)
        self.assertEqual(get_floats(vm, BUF, 7), [50.0] * 7)

    def test_insufficient_gas_for_the_work_faults_before_any_effect(self) -> None:
        _, full, _ = self.run_matvec(10_000)
        used = 10_000 - full.gas_remaining
        vm, result, _ = self.run_matvec(used - 2)  # alcanza para el coste base, no para el trabajo
        self.assertEqual(result.state, "FAULTED")
        self.assertIn("Gas agotado", result.fault)
        self.assertEqual(get_floats(vm, BUF, 7), [0.0] * 7)

    def test_work_cap_per_instruction(self) -> None:
        body = f"    MOVI R1, {BUF}\n    MATVEC R1, R1, R1"
        vm = make_vm(vector_program(body, 1 << 13, 1 << 14), gas_limit=10**9)
        result = vm.run()
        self.assertEqual(result.state, "FAULTED")
        self.assertIn("límite", result.fault)


class SemanticsAnchors(unittest.TestCase):
    def run_op(self, body: str, vl: int, inputs: dict[int, list[float]], vr: int = 1, vs: int = 0,
               rom: TensorROM | None = None) -> TramoyaVM32:
        vm = make_vm(vector_program(body, vl, vr, vs), rom=rom, memory_words=1 << 16)
        for address, values in inputs.items():
            put_floats(vm, address, values)
        result = vm.run()
        self.assertEqual(result.state, "HALTED", result.fault)
        return vm

    def test_vadd_vmul_vscale_are_bit_identical_to_scalar_fpu(self) -> None:
        a = [1.1, -2.5e-8, 3.4e38, 7.0, 1e-40, -0.0, 123.456, 2.0**-126]
        b = [2.2, 3.3, 2.0, -7.0, 1e-40, 0.0, -654.321, 3.0]
        body = (f"    MOVI R1, {BUF}\n    MOVI R2, {BUF + 100}\n    MOVI R3, {BUF + 200}\n"
                f"    MOVI R4, {BUF + 300}\n    MOVI R5, {BUF + 400}\n    MOVI R6, {f2w(0.37)}\n"
                "    VADD R3, R1, R2\n    VMUL R4, R1, R2\n    VSCALE R5, R1, R6")
        vm = self.run_op(body, len(a), {BUF: a, BUF + 100: b})
        for index in range(len(a)):
            scalar = TramoyaVM32(VMConfig(trace_size=0))
            scalar.load_program(program(
                f".code\nMOVI R1, {f2w(a[index])}\nMOVI R2, {f2w(b[index])}\nMOVI R3, {f2w(0.37)}\n"
                "FADD R4, R1, R2\nFMUL R5, R1, R2\nFMUL R6, R1, R3\nHALT"))
            scalar.run()
            self.assertEqual(vm.memory_slice(BUF + 200 + index, 1)[0], scalar.registers[4])
            self.assertEqual(vm.memory_slice(BUF + 300 + index, 1)[0], scalar.registers[5])
            self.assertEqual(vm.memory_slice(BUF + 400 + index, 1)[0], scalar.registers[6])

    def test_fdot_rounds_once_to_float32(self) -> None:
        a = [f32(0.1 * i - 3) for i in range(100)]
        b = [f32(math.sin(i)) for i in range(100)]
        body = f"    MOVI R1, {BUF}\n    MOVI R2, {BUF + 200}\n    FDOT R7, R1, R2"
        vm = self.run_op(body, 100, {BUF: a, BUF + 200: b})
        self.assertEqual(w2f(vm.registers[7]), f32(math.fsum(x * y for x, y in zip(a, b))))

    def test_matvec_strided_rows_crossing_pages_match_naive(self) -> None:
        rows, cols, stride = 9, 13, 700  # filas repartidas en varias páginas de RAM
        weights = [f32(((r * 31 + c * 7) % 17 - 8) / 8) for r in range(rows) for c in range(stride)]
        x = [f32((c % 5 - 2) / 3) for c in range(cols)]
        base = PAGE_WORDS - 100
        body = f"    MOVI R1, {BUF * 8}\n    MOVI R2, {base}\n    MOVI R3, 100\n    MATVEC R1, R2, R3"
        vm = self.run_op(body, cols, {base: weights, 100: x}, vr=rows, vs=stride)
        expected = [f32(math.fsum(weights[r * stride + c] * x[c] for c in range(cols))) for r in range(rows)]
        self.assertEqual(get_floats(vm, BUF * 8, rows), expected)

    def test_mattv_is_weighted_sum_of_rows(self) -> None:
        rows, cols, stride = 5, 4, 6
        weights = [float(r * stride + c) for r in range(rows) for c in range(stride)]
        coefficients = [0.5, -1.0, 0.25, 2.0, 0.0]
        body = f"    MOVI R1, {BUF}\n    MOVI R2, {BUF + 100}\n    MOVI R3, {BUF + 200}\n    MATTV R1, R2, R3"
        vm = self.run_op(body, cols, {BUF + 100: weights, BUF + 200: coefficients}, vr=rows, vs=stride)
        expected = [f32(math.fsum(coefficients[r] * weights[r * stride + j] for r in range(rows)))
                    for j in range(cols)]
        self.assertEqual(get_floats(vm, BUF, cols), expected)

    def test_rmsnorm_softmax_exp_silu(self) -> None:
        a = [0.5, -1.5, 2.0, 0.0, 3.0, -0.25]
        w = [1.0, 2.0, 0.5, 1.0, -1.0, 3.0]
        body = (f"    MOVI R1, {BUF}\n    MOVI R2, {BUF + 100}\n    MOVI R3, {BUF + 200}\n"
                f"    MOVI R4, {BUF + 300}\n    MOVI R5, {BUF + 400}\n    MOVI R6, {BUF + 500}\n"
                "    RMSNORM R3, R1, R2\n    VSOFTMAX R4, R1\n    VEXP R5, R1\n    VSILU R6, R1")
        vm = self.run_op(body, len(a), {BUF: a, BUF + 100: w})
        scale = 1 / math.sqrt(sum(v * v for v in a) / len(a) + 1e-5)
        for got, value, weight in zip(get_floats(vm, BUF + 200, 6), a, w):
            self.assertAlmostEqual(got, weight * value * scale, places=5)
        probabilities = get_floats(vm, BUF + 300, 6)
        self.assertAlmostEqual(sum(probabilities), 1.0, places=6)
        total = sum(math.exp(v) for v in a)
        for got, value in zip(probabilities, a):
            self.assertAlmostEqual(got, math.exp(value) / total, places=6)
        self.assertEqual(get_floats(vm, BUF + 400, 6), [f32(math.exp(v)) for v in a])
        for got, value in zip(get_floats(vm, BUF + 500, 6), a):
            self.assertAlmostEqual(got, value / (1 + math.exp(-value)), places=6)

    def test_vexp_overflow_saturates_to_infinity(self) -> None:
        vm = self.run_op(f"    MOVI R1, {BUF}\n    VEXP R1, R1", 3, {BUF: [100.0, 1e30, -1e30]})
        self.assertEqual(get_floats(vm, BUF, 3), [math.inf, math.inf, 0.0])

    def test_rope_matches_llama2c_formula(self) -> None:
        head_size, pos = 4, 3
        v = [0.5, -1.0, 2.0, 0.25, 1.0, 1.0, -3.0, 0.5]
        body = f"    MOVI R1, {BUF}\n    MOVI R2, {pos}\n    MOVI R3, {head_size}\n    ROPE R1, R2, R3"
        vm = self.run_op(body, len(v), {BUF: v})
        expected = []
        for i in range(0, len(v), 2):
            freq = 1.0 / (10000.0 ** ((i % head_size) / head_size))
            c, s = math.cos(pos * freq), math.sin(pos * freq)
            expected += [f32(v[i] * c - v[i + 1] * s), f32(v[i] * s + v[i + 1] * c)]
        self.assertEqual(get_floats(vm, BUF, len(v)), expected)

    def test_rope_rejects_invalid_head_size(self) -> None:
        for head_size in (0, 3, 6):
            with self.subTest(head_size):
                body = f"    MOVI R1, {BUF}\n    MOVI R2, 1\n    MOVI R3, {head_size}\n    ROPE R1, R2, R3"
                vm = make_vm(vector_program(body, 8), memory_words=1 << 16)
                self.assertEqual(vm.run().state, "FAULTED")

    def test_argmax_and_sample(self) -> None:
        p = [0.1, 0.2, 0.4, 0.2, 0.1]
        body = (f"    MOVI R1, {BUF}\n    VARGMAX R4, R1\n"
                f"    MOVI R6, {f2w(0.25)}\n    VSAMPLE R5, R1, R6\n"
                f"    MOVI R6, {f2w(0.9999999)}\n    VSAMPLE R7, R1, R6")
        vm = self.run_op(body, 5, {BUF: p})
        self.assertEqual(vm.registers[4], 2)
        self.assertEqual(vm.registers[5], 1)
        self.assertEqual(vm.registers[7], 4)

    def test_quantized_matvec_matches_exact_integer_dot(self) -> None:
        rows, cols = 3, 8
        q_rows = [[(r * 37 + c * 11) % 255 - 127 for c in range(cols)] for r in range(rows)]
        scales = [f32(0.01 * (r + 1)) for r in range(rows)]
        matrix = bytes(q + 128 for row in q_rows for q in row) + array("f", scales).tobytes()
        rom = TensorROM(matrix + bytes(4 * 8))
        x = [0.5, -1.0, 0.25, 0.0, 1.0, -0.75, 0.125, 0.9]
        body = (f"    MOVI R1, {BUF}\n    MOVI R2, {BUF + 100}\n    VQUANT R2, R1\n"
                f"    MOVI R3, {BUF + 200}\n    MOVI R4, {ROM_BASE}\n    QMATVEC R3, R4, R2\n"
                f"    MOVI R5, {BUF + 300}\n    MOVI R6, 2\n    QROW R5, R4, R6")
        vm = self.run_op(body, cols, {BUF: x}, vr=rows, rom=rom)
        x_scale = f32(max(abs(v) for v in x) / 127)
        qx = [max(-127, min(127, round(v / x_scale))) for v in x]
        stored = vm.memory_slice(BUF + 100, cols // 4 + 1)
        self.assertEqual(list(struct.pack(f"<{cols // 4}i", *stored[:-1])), [q + 128 for q in qx])
        self.assertEqual(w2f(stored[-1]), x_scale)
        expected = [f32(sum(a * b for a, b in zip(q_rows[r], qx)) * scales[r] * x_scale) for r in range(rows)]
        self.assertEqual(get_floats(vm, BUF + 200, rows), expected)
        self.assertEqual(get_floats(vm, BUF + 300, cols), [f32(q * scales[2]) for q in q_rows[2]])

    def test_q8_ops_require_length_multiple_of_four(self) -> None:
        body = f"    MOVI R1, {BUF}\n    MOVI R2, {BUF + 100}\n    VQUANT R2, R1"
        vm = make_vm(vector_program(body, 6), memory_words=1 << 16)
        put_floats(vm, BUF, [1.0] * 6)
        self.assertEqual(vm.run().state, "FAULTED")


class PeritajeAnchors(unittest.TestCase):
    """Un ancla por hallazgo confirmado en el peritaje A2 y la revisión G4 del TNU."""

    def test_qrow_reads_only_the_row_and_its_scale(self) -> None:
        # R-01/H3/D1: QROW validaba materializando la matriz completa por 2·VL de gas.
        rows, cols, row = 5000, 8, 4321
        base = BUF
        vm = make_vm(vector_program(f"    MOVI R1, {BUF * 8}\n    MOVI R2, {base}\n    MOVI R3, {row}\n"
                                    "    QROW R1, R2, R3", cols, rows), memory_words=1 << 16)
        q = [-127, -1, 0, 1, 2, 64, 100, 127]
        row_words = struct.unpack("<2i", bytes(v + 128 for v in q))
        vm.write_memory(base + row * cols // 4, row_words[0])
        vm.write_memory(base + row * cols // 4 + 1, row_words[1])
        put_floats(vm, base + rows * cols // 4 + row, [0.5])
        sizes = []
        original = PagedMemory.view_bytes

        def spy(memory, start, count):
            sizes.append(count)
            return original(memory, start, count)

        with mock.patch.object(PagedMemory, "view_bytes", spy):
            result = vm.run()
        self.assertEqual(result.state, "HALTED", result.fault)
        self.assertLessEqual(max(sizes), cols)
        self.assertEqual(get_floats(vm, BUF * 8, cols), [f32(v * 0.5) for v in q])

    def test_invalid_destination_faults_before_computing(self) -> None:
        # R-02: el destino se validaba después del cálculo y el rollback devolvía el gas.
        body = f"    MOVI R1, {BUF}\n    MOVI R2, {1 << 20}\n    MATVEC R2, R1, R1"
        vm = make_vm(vector_program(body, 4, 4), memory_words=1 << 16)
        with mock.patch.object(tnu, "matvec", side_effect=AssertionError("no debe calcular")) as kernel:
            result = vm.run()
        self.assertEqual(result.state, "FAULTED")
        self.assertIn("Destino", result.fault)
        kernel.assert_not_called()

    def test_vsilu_of_very_negative_inputs_tends_to_zero(self) -> None:
        # H1/D2: exp(-x) se recortaba a exp(89) y x/(1+e^89) daba valores erróneos.
        values = [-90.0, -100.0, -200.0, -1e30]
        vm = make_vm(vector_program(f"    MOVI R1, {BUF}\n    VSILU R1, R1", 4), memory_words=1 << 16)
        put_floats(vm, BUF, values)
        vm.run()
        expected = [f32(v / (1.0 + math.exp(min(-v, 709.0)))) for v in values]
        self.assertEqual(get_floats(vm, BUF, 4), expected)
        self.assertEqual(expected[-1], -0.0)

    def test_vsample_follows_declared_rule_with_negative_entries(self) -> None:
        # H2: bisect sobre una suma no monótona devolvía un índice distinto del primero.
        cases = (([0.5, -0.4, 0.9], 0.3, 0), ([0.6, -0.5, 0.2, 0.9], 0.4, 0), ([0.1, 0.1], 0.9, 1))
        for probabilities, coin, expected in cases:
            with self.subTest(probabilities):
                body = f"    MOVI R1, {BUF}\n    MOVI R2, {f2w(coin)}\n    VSAMPLE R3, R1, R2"
                vm = make_vm(vector_program(body, len(probabilities)), memory_words=1 << 16)
                put_floats(vm, BUF, probabilities)
                vm.run()
                self.assertEqual(vm.registers[3], expected)

    def test_rom_write_without_npu_does_not_reveal_the_rom(self) -> None:
        # SEG-02: el mensaje de STORE distinguía si había ROM conectada sin la capacidad npu.
        source = f".code\n    MOVI R2, {ROM_BASE}\n    STORE R1, [R2]\n    HALT"
        with_rom = make_vm(source, rom=rom_from_floats([1.0]), caps=frozenset({"io"})).run().fault
        without = make_vm(source, caps=frozenset({"io"}), npu=False).run().fault
        self.assertEqual(with_rom, without)

    def test_snapshot_keeps_only_the_rom_file_name(self) -> None:
        # SEG-04: el snapshot filtraba la ruta absoluta de la ROM del host.
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "secreto" / "pesos.trom"
            path.parent.mkdir()
            path.write_bytes(bytes(16))
            vm = make_vm(".code\n    HALT", rom=TensorROM.from_file(path))
            payload = json.loads(zlib.decompress(vm.snapshot_bytes()[len(SNAPSHOT_MAGIC):]))
        self.assertEqual(payload["rom"]["path"], "pesos.trom")

    def test_rope_keeps_no_process_wide_cache(self) -> None:
        # R-04/D4: una caché global retenía ~70 MiB elegidos por el invitado entre VMs.
        self.assertFalse(hasattr(tnu._rope_table, "cache_info"))


class RomAnchors(unittest.TestCase):
    def test_load_reads_rom_only_with_npu(self) -> None:
        rom = rom_from_floats([1.5, 2.5])
        source = f".code\n    MOVI R2, {ROM_BASE}\n    LOAD R1, [R2+1]\n    SYSCALL 1\n    HALT"
        vm = make_vm(source, rom=rom)
        self.assertEqual(vm.run().output, (f2w(2.5),))
        denied = make_vm(source, rom=rom, caps=frozenset({"io"}))
        self.assertEqual(denied.run().state, "FAULTED")

    def test_store_to_rom_faults(self) -> None:
        rom = rom_from_floats([1.5])
        vm = make_vm(f".code\n    MOVI R2, {ROM_BASE}\n    STORE R1, [R2]\n    HALT", rom=rom)
        result = vm.run()
        self.assertEqual(result.state, "FAULTED")
        self.assertIn("ROM", result.fault)

    def test_npu_info_syscall(self) -> None:
        rom = rom_from_floats([0.0] * 10)
        vm = make_vm(".code\n    SYSCALL 12\n    SYSCALL 1\n    MOV R1, R2\n    SYSCALL 1\n    HALT", rom=rom)
        self.assertEqual(vm.run().output, (ROM_BASE, 10))

    def test_snapshot_excludes_rom_and_requires_same_hash(self) -> None:
        rom = TensorROM(bytes(range(256)) * 4096)  # 1 MiB de ROM
        source = vector_program(f"    MOVI R1, {BUF}\n    MOVI R2, {ROM_BASE}\n    VCOPY R1, R2\n    BREAK", 16)
        vm = make_vm(source, rom=rom)
        vm.run()
        snapshot = vm.snapshot_bytes()
        self.assertLess(len(snapshot), 20_000)
        payload = json.loads(zlib.decompress(snapshot[len(SNAPSHOT_MAGIC):]))
        self.assertEqual(payload["rom"]["sha256"], rom.sha256)
        self.assertEqual(payload["core"]["tnu"], {"vl": 16, "vr": 1, "vs": 0})

        restored = TramoyaVM32(vm.config, npu=TramoyaNeuralUnit(rom))
        restored.restore_bytes(snapshot)
        self.assertEqual(restored.memory_slice(BUF, 16), vm.memory_slice(BUF, 16))
        self.assertEqual(restored.run().state, "HALTED")

        other = TramoyaVM32(vm.config, npu=TramoyaNeuralUnit(TensorROM(bytes(8))))
        other.load_program(program(".code\nHALT"))
        before = other.memory_slice(0, 8)
        with self.assertRaises(ValueError):
            other.restore_bytes(snapshot)
        self.assertEqual(other.memory_slice(0, 8), before)
        with self.assertRaises(ValueError):
            TramoyaVM32.from_snapshot_bytes(snapshot, capabilities=NPU_CAPS)
        factory = TramoyaVM32.from_snapshot_bytes(snapshot, capabilities=NPU_CAPS, npu=TramoyaNeuralUnit(rom))
        self.assertEqual(factory.run().state, "HALTED")

    def test_snapshot_without_tnu_state_has_no_tnu_keys(self) -> None:
        vm = TramoyaVM32(VMConfig(trace_size=0))
        vm.load_program(program(".code\nMOVI R1, 3\nHALT"))
        vm.run()
        payload = json.loads(zlib.decompress(vm.snapshot_bytes()[len(SNAPSHOT_MAGIC):]))
        self.assertNotIn("rom", payload)
        self.assertNotIn("tnu", payload["core"])

    def test_snapshot_rejects_invalid_tnu_state(self) -> None:
        vm = make_vm(vector_program("    BREAK", 8))
        vm.run()
        payload = json.loads(zlib.decompress(vm.snapshot_bytes()[len(SNAPSHOT_MAGIC):]))
        for bad in ({"vl": -1, "vr": 1, "vs": 0}, {"vl": 8, "vr": "x", "vs": 0}, [1, 2, 3]):
            payload["core"]["tnu"] = bad
            raw = SNAPSHOT_MAGIC + zlib.compress(json.dumps(payload).encode())
            with self.subTest(bad), self.assertRaises(ValueError):
                make_vm(".code\nHALT").restore_bytes(raw)

    def test_rom_rejects_bad_sizes(self) -> None:
        for data in (b"", b"abc"):
            with self.subTest(data), self.assertRaises(ValueError):
                TensorROM(data)

    def test_vcfg_validates_before_mutating(self) -> None:
        vm = make_vm(vector_program("    MOVI R1, 3\n    MOVI R2, 0\n    VCFG R1, R1, R2\n    BREAK", 8))
        vm.run()
        self.assertEqual(vm.tnu_config, (3, 3, 0))
        vm2 = make_vm(".code\n    MOVI R1, 3\n    MOVI R2, -5\n    VCFG R1, R1, R2\n    HALT")
        self.assertEqual(vm2.run().state, "FAULTED")
        self.assertEqual(vm2.tnu_config, (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
