"""Anclas del acelerador de bucles: estado idéntico al intérprete, siempre.

El oráculo es el propio intérprete con ``accelerate_loops=False``: para cada
programa se comparan estado final, fallo, gas, ciclos, instrucciones, salida,
memoria y última instrucción. El fuzzing genera cuerpos de bucle aleatorios con
todas las instrucciones acelerables, incluidos accesos fuera de rango, gas
escaso y contadores que el propio cuerpo modifica.
"""

from __future__ import annotations

import random
import struct
import unittest

from cpu_digital.vm32 import CANONICAL_NAN, LOOP_WARMUP, TramoyaVM32, VMConfig
from cpu_digital.vm32_assembler import VM32Assembler

MEMORY = 4096


def program(source: str):
    return VM32Assembler().assemble(source).program


def observe(vm: TramoyaVM32) -> tuple:
    result = vm.result()
    return (
        result.state, result.fault, result.exit_code, result.pause_reason,
        vm.pc, vm.registers, dict(vm.flags), vm.stack, result.output,
        result.gas_remaining, result.cycles, result.instructions,
        vm.memory_slice(0, MEMORY), vm.machine.ctx.get("last_instruction"),
    )


def run_both(source: str, *, gas_limit: int = 50_000, inputs=(), max_instructions=None,
             seed_memory: dict[int, int] | None = None, **config) -> tuple[tuple, tuple, int]:
    prog = program(source)
    states = []
    accelerated = 0
    for accelerate in (False, True):
        vm = TramoyaVM32(VMConfig(memory_words=MEMORY, gas_limit=gas_limit, trace_size=0,
                                  accelerate_loops=accelerate, **config))
        vm.load_program(prog, inputs=inputs)
        for address, value in (seed_memory or {}).items():
            vm.write_memory(address, value)
        vm.run(max_instructions=max_instructions)
        states.append(observe(vm))
        accelerated = vm.accelerated_instructions
    return states[0], states[1], accelerated


MAC_LOOP = """.code
    MOVI R1, 1000
    MOVI R2, 2000
    MOVI R9, 300
L:
    LOAD R3, [R1]
    LOAD R4, [R2]
    FMUL R5, R3, R4
    FADD R6, R6, R5
    ADDI R1, R1, 1
    ADDI R2, R2, 1
    SUBI R9, R9, 1
    JNZ L
    MOV R1, R6
    SYSCALL 1
    HALT
"""


def float_words(seed: int, count: int) -> dict[int, int]:
    rng = random.Random(seed)
    words = {}
    for offset in range(count):
        for base in (1000, 2000):
            words[base + offset] = struct.unpack("<i", struct.pack("<f", rng.uniform(-4, 4)))[0]
    return words


class LoopAcceleratorAnchors(unittest.TestCase):
    def test_mac_loop_bit_identical_and_accelerated(self) -> None:
        plain, fast, accelerated = run_both(MAC_LOOP, seed_memory=float_words(1, 300))
        self.assertEqual(plain, fast)
        self.assertEqual(plain[0], "HALTED")
        self.assertEqual(accelerated, (300 - LOOP_WARMUP) * 8)  # calentamiento en el intérprete

    def test_gas_exhaustion_inside_loop_is_identical(self) -> None:
        for gas in (5, 40, 41, 42, 43, 100, 1234, 4199):
            with self.subTest(gas=gas):
                plain, fast, _ = run_both(MAC_LOOP, gas_limit=gas, seed_memory=float_words(2, 300))
                self.assertEqual(plain, fast)
                self.assertEqual(plain[0], "FAULTED")

    def test_instruction_limit_pauses_identically(self) -> None:
        for limit in (1, 7, 8, 9, 100, 801):
            with self.subTest(limit=limit):
                plain, fast, _ = run_both(MAC_LOOP, max_instructions=limit, seed_memory=float_words(3, 300))
                self.assertEqual(plain, fast)
                self.assertEqual(plain[0], "PAUSED")

    def test_fault_in_the_middle_of_the_loop_is_identical(self) -> None:
        # El bucle sale de la RAM en la vuelta 97: mismo fallo, mismo estado parcial.
        source = MAC_LOOP.replace("MOVI R2, 2000", f"MOVI R2, {MEMORY - 96}")
        plain, fast, accelerated = run_both(source, seed_memory=float_words(4, 300))
        self.assertEqual(plain, fast)
        self.assertEqual(plain[0], "FAULTED")
        self.assertIn("Lectura fuera de memoria", plain[1])
        self.assertGreater(accelerated, 0)

    def test_store_into_code_is_left_to_the_interpreter(self) -> None:
        source = """.code
    MOVI R1, 100
    MOVI R9, 200
L:
    STORE R9, [R1]
    SUBI R1, R1, 1
    SUBI R9, R9, 1
    JNZ L
    HALT
"""
        for protect in (True, False):
            with self.subTest(protect_code=protect):
                plain, fast, _ = run_both(source, protect_code=protect)
                self.assertEqual(plain, fast)

    def test_breakpoint_and_trace_disable_acceleration(self) -> None:
        prog = program(MAC_LOOP)
        with_bp = TramoyaVM32(VMConfig(memory_words=MEMORY, trace_size=0))
        with_bp.load_program(prog)
        with_bp.run(breakpoints=[prog.code_size - 4])
        self.assertEqual(with_bp.accelerated_instructions, 0)
        with_trace = TramoyaVM32(VMConfig(memory_words=MEMORY, trace_size=16))
        with_trace.load_program(prog)
        with_trace.run()
        self.assertEqual(with_trace.accelerated_instructions, 0)
        self.assertEqual(len(with_trace.trace), 16)

    def test_pending_interrupt_is_dispatched_identically(self) -> None:
        source = """.code
    MOVI R9, 50
L:
    ADDI R1, R1, 3
    SUBI R9, R9, 1
    JNZ L
    HALT
ISR:
    ADDI R2, R2, 1
    IRET
"""
        prog = program(source)
        states = []
        for accelerate in (False, True):
            vm = TramoyaVM32(VMConfig(memory_words=MEMORY, trace_size=0, accelerate_loops=accelerate))
            vm.load_program(prog)
            vm.set_interrupt_vector(1, prog.symbols["ISR"])
            vm.run(max_instructions=20)
            vm.request_interrupt(1)
            vm.run()
            states.append(observe(vm))
        self.assertEqual(states[0], states[1])
        self.assertEqual(states[0][5][2], 1)

    def test_self_modifying_code_invalidates_compiled_loops(self) -> None:
        source = """.code
    MOVI R9, 40
    MOVI R3, 11          ; opcode ADDI
    LEA R4, PATCH
L:
    ADDI R1, R1, 1
PATCH:
    ADDI R2, R2, 1
    SUBI R9, R9, 1
    JNZ L
    STORE R3, [R4]       ; no cambia nada
    MOVI R3, 13          ; opcode SUBI: convierte PATCH en SUBI R2, R2, 1
    MOVI R9, 40
    STORE R3, [R4]
    JMP L2
L2:
    ADDI R1, R1, 1
    ADDI R2, R2, 1
    SUBI R9, R9, 1
    JNZ L2
    HALT
"""
        plain, fast, _ = run_both(source.replace("JMP L2\n", ""), protect_code=False)
        self.assertEqual(plain, fast)

    def test_snapshot_after_accelerated_run_matches(self) -> None:
        prog = program(MAC_LOOP)
        snapshots = []
        for accelerate in (False, True):
            vm = TramoyaVM32(VMConfig(memory_words=MEMORY, trace_size=0, accelerate_loops=accelerate))
            vm.load_program(prog)
            for address, value in float_words(5, 300).items():
                vm.write_memory(address, value)
            vm.run(max_instructions=1234)
            snapshots.append(vm.snapshot_bytes())
        self.assertEqual(snapshots[0], snapshots[1])


class G4Anchors(unittest.TestCase):
    """Un ancla por hallazgo de la revisión G4 del acelerador."""

    LOOP_PLUS = """.code
    MOVI R9, 100
L:
    ADDI R1, R1, {step}
    SUBI R9, R9, 1
    JNZ L
    HALT
"""

    def test_restore_discards_compiled_loops_of_the_previous_program(self) -> None:
        # D1: tras restore_bytes se ejecutaba el bucle compilado del programa anterior.
        other = TramoyaVM32(VMConfig(memory_words=MEMORY, trace_size=0))
        other.load_program(program(self.LOOP_PLUS.format(step=7)))
        snapshot = other.snapshot_bytes()
        vm = TramoyaVM32(VMConfig(memory_words=MEMORY, trace_size=0))
        vm.load_program(program(self.LOOP_PLUS.format(step=1)))
        vm.run()
        self.assertEqual(vm.registers[1], 100)
        vm.restore_bytes(snapshot)
        vm.run()
        self.assertEqual(vm.registers[1], 700)

    def test_restore_resets_last_instruction(self) -> None:
        # D2: run() sobre una VM restaurada ya terminada machacaba last_instruction.
        vm = TramoyaVM32(VMConfig(memory_words=MEMORY, trace_size=0))
        vm.load_program(program(".code\n    MOVI R2, 2\n    MOVI R3, 3\n    HALT"))
        vm.run()
        snapshot = vm.snapshot_bytes()
        restored = TramoyaVM32(vm.config)
        restored.load_program(program(".code\n    MOVI R1, 1\n    HALT"))
        restored.run()
        restored.restore_bytes(snapshot)
        restored.run()
        self.assertEqual(restored.machine.ctx["last_instruction"]["opcode"], "HALT")
        self.assertEqual(restored.machine.ctx["last_instruction"]["pc"], 8)

    def test_from_snapshot_bytes_honours_accelerate_loops(self) -> None:
        # D3: --no-loop-acceleration se ignoraba con --load-snapshot.
        vm = TramoyaVM32(VMConfig(memory_words=MEMORY, trace_size=0))
        vm.load_program(program(self.LOOP_PLUS.format(step=1)))
        snapshot = vm.snapshot_bytes()
        slow = TramoyaVM32.from_snapshot_bytes(snapshot, accelerate_loops=False)
        slow.run()
        self.assertFalse(slow.config.accelerate_loops)
        self.assertEqual((slow.accelerated_instructions, slow.registers[1]), (0, 100))
        fast = TramoyaVM32.from_snapshot_bytes(snapshot)
        fast.run()
        self.assertGreater(fast.accelerated_instructions, 0)

    def test_subclass_can_override_an_instruction_handler(self) -> None:
        # D4: la tabla de dispatch enlazaba los manejadores de la clase base.
        class Doubling(TramoyaVM32):
            def _op_addi(self, a: int, b: int, c: int) -> None:
                self._add(a, self._read_register(b), 2 * c)

        vm = Doubling(VMConfig(memory_words=MEMORY, trace_size=0, accelerate_loops=False))
        vm.load_program(program(".code\n    ADDI R1, R1, 5\n    HALT"))
        vm.run()
        self.assertEqual(vm.registers[1], 10)


class NanDeterminismAnchor(unittest.TestCase):
    def test_fpu_nan_results_are_canonical_however_often_they_run(self) -> None:
        # Hallazgo del fuzz diferencial: CPython 3.13 devolvía la carga útil del
        # segundo NaN la primera vez y la del primero tras especializar `x * y`.
        source = """.code
    MOVI R5, -1000
    MOVI R6, -2734
    MOVI R9, 100
L:
    FMUL R10, R5, R6
    FADD R11, R6, R5
    FSUB R12, R5, R6
    FABS R13, R6
    STORE R10, [R9+200]
    SUBI R9, R9, 1
    JNZ L
    HALT
"""
        plain, fast, _ = run_both(source)
        self.assertEqual(plain, fast)
        self.assertEqual(plain[0], "HALTED")
        self.assertEqual(set(plain[12][201:301]), {CANONICAL_NAN})
        self.assertEqual(plain[5][10:14], (CANONICAL_NAN,) * 4)


class DifferentialFuzz(unittest.TestCase):
    OPS = ["MOV", "MOVI", "LOAD", "STORE", "ADD", "ADDI", "SUB", "SUBI", "MUL", "MULI", "AND", "OR",
           "XOR", "NOT", "SHL", "SHR", "CMP", "CMPI", "TEST", "FADD", "FSUB", "FMUL", "FABS", "ITOF", "NOP"]
    BACKEDGES = ["JNZ", "JZ", "JNEG", "JPOS", "JC", "JNC", "JLT", "JGT"]

    def random_loop(self, rng: random.Random) -> str:
        reg = lambda: f"R{rng.randint(1, 14)}"  # noqa: E731 - generador compacto
        imm = lambda: rng.choice([0, 1, -1, 2, 7, 1000, -1000, 2**31 - 1, -(2**31), rng.randint(-5000, 5000)])  # noqa: E731
        lines = [".code"]
        for index in range(1, 15):
            lines.append(f"    MOVI R{index}, {rng.choice([0, 1, 5, 100, 1000, 3000, -3, imm()])}")
        counter = rng.randint(1, 14)
        lines.append(f"    MOVI R{counter}, {rng.randint(1, 40)}")
        lines.append("L:")
        for _ in range(rng.randint(1, 8)):
            op = rng.choice(self.OPS)
            if op in {"MOV", "NOT", "FABS", "ITOF", "TEST", "CMP"}:
                lines.append(f"    {op} {reg()}, {reg()}")
            elif op in {"MOVI", "CMPI"}:
                lines.append(f"    {op} {reg()}, {imm()}")
            elif op in {"LOAD", "STORE"}:
                lines.append(f"    {op} {reg()}, [{reg()}{rng.choice(['', '+1', '-2', '+500'])}]")
            elif op in {"ADDI", "SUBI", "MULI"}:
                lines.append(f"    {op} {reg()}, {reg()}, {imm()}")
            elif op in {"SHL", "SHR"}:
                lines.append(f"    {op} {reg()}, {reg()}, {rng.randint(0, 31)}")
            elif op == "NOP":
                lines.append("    NOP")
            else:
                lines.append(f"    {op} {reg()}, {reg()}, {reg()}")
        lines.append(f"    SUBI R{counter}, R{counter}, 1")
        lines.append(f"    {rng.choice(self.BACKEDGES)} L")
        lines.append("    MOV R1, R2\n    SYSCALL 1\n    HALT")
        return "\n".join(lines)

    def test_random_loops_match_the_interpreter(self) -> None:
        rng = random.Random(2026)
        accelerated_total = 0
        for case in range(400):
            source = self.random_loop(rng)
            with self.subTest(case=case, source=source):
                plain, fast, accelerated = run_both(source, gas_limit=rng.choice([3_000, 20_000]),
                                                    max_instructions=rng.choice([None, None, 333]))
                self.assertEqual(plain, fast)
                accelerated_total += accelerated
        self.assertGreater(accelerated_total, 0)


if __name__ == "__main__":
    unittest.main()
