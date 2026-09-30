"""Anclas del acelerador de bucles: estado idéntico al intérprete, siempre.

El oráculo es el propio intérprete con ``accelerate_loops=False``: para cada
programa se comparan estado final, fallo, gas, ciclos, instrucciones, salida,
memoria, pila, última instrucción y ``snapshot_bytes``. El fuzzing genera cuerpos
de bucle aleatorios con todas las instrucciones acelerables, incluidos accesos
fuera de rango, gas escaso y contadores que el propio cuerpo modifica; el fuzzing
ampliado añade saltos hacia delante (dentro y fuera del cuerpo), saltos hacia
atrás que el acelerador debe rechazar, DIV/MOD con divisores cero e
``INT32_MIN / -1``, PUSH/POP con pilas pequeñas y lecturas de R0 y R15, y
observa el estado también en cada salida anticipada con una syscall espía.
"""

from __future__ import annotations

import random
import struct
import unittest
from typing import Callable

from cpu_digital.vm32 import CANONICAL_NAN, LOOP_WARMUP, TramoyaVM32, VMConfig, VMRuntimeError
from cpu_digital.vm32_assembler import VM32Assembler
from cpu_digital.vm32_isa import VMOpcode
from cpu_digital.vm32_loops import compile_loop

MEMORY = 4096


def program(source: str):
    return VM32Assembler().assemble(source).program


def observe(vm: TramoyaVM32) -> tuple:
    result = vm.result()
    # Registros internos tal cual: la propiedad pública resincroniza R15 con la pila
    # y ocultaría un R15 obsoleto que la siguiente instrucción sí leería.
    return (
        result.state, result.fault, result.exit_code, result.pause_reason,
        vm.pc, tuple(vm._registers), dict(vm.flags), vm.stack, result.output,
        result.gas_remaining, result.cycles, result.instructions,
        vm.memory_slice(0, MEMORY), vm.machine.ctx.get("last_instruction"), vm.snapshot_bytes(),
    )


SPY = 100  # syscall espía: registra el estado visible en mitad de la ejecución


def spy(log: list) -> Callable[[TramoyaVM32], None]:
    def record(vm: TramoyaVM32) -> None:
        # _op_syscall sincroniza el ciclo de vida antes: last_instruction es la instrucción anterior.
        log.append((dict(vm.machine.ctx["last_instruction"]), tuple(vm._registers), dict(vm.flags), vm.stack,
                    vm.result().instructions, vm.result().gas_remaining))
    return record


def run_both(source: str, *, gas_limit: int = 50_000, inputs=(), max_instructions=None,
             seed_memory: dict[int, int] | None = None, **config) -> tuple[tuple, tuple, int]:
    prog = program(source)
    states = []
    accelerated = 0
    for accelerate in (False, True):
        vm = TramoyaVM32(VMConfig(memory_words=MEMORY, gas_limit=gas_limit, trace_size=0,
                                  accelerate_loops=accelerate, **config))
        log: list = []
        vm.register_syscall(SPY, spy(log), name="espia")
        vm.load_program(prog, inputs=inputs)
        for address, value in (seed_memory or {}).items():
            vm.write_memory(address, value)
        vm.run(max_instructions=max_instructions)
        states.append((*observe(vm), tuple(log)))
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


class OverrideAnchors(unittest.TestCase):
    """G4 de las extensiones: el código generado reproduce TramoyaVM32; si una subclase o una
    instancia cambia un manejador o un método del que dependen, el acelerador no debe actuar."""

    LOOP = ".code\n    MOVI R9, 100\nL:\n    ADDI R1, R1, 5\n    PUSH R1\n    SUBI R9, R9, 1\n    JNZ L\n    HALT\n"
    LINEAR = ".code\n    MOVI R9, 100\nL:\n    ADDI R1, R1, 5\n    SUBI R9, R9, 1\n    JNZ L\n    HALT\n"

    def both(self, cls: type[TramoyaVM32], patch=None, source: str = LOOP) -> tuple[tuple, tuple, int]:
        states, accelerated = [], 0
        for accelerate in (False, True):
            vm = cls(VMConfig(memory_words=MEMORY, trace_size=0, accelerate_loops=accelerate))
            if patch:
                patch(vm)
            vm.load_program(program(source))
            vm.run()
            states.append(observe(vm))
            accelerated = vm.accelerated_instructions
        return states[0], states[1], accelerated

    def test_overridden_instruction_handler_is_honoured_with_the_accelerator_on(self) -> None:
        class Doubling(TramoyaVM32):
            def _op_addi(self, a: int, b: int, c: int) -> None:
                self._add(a, self._read_register(b), 2 * c)

        # Bucle lineal: la primera versión del acelerador ya lo compilaba e ignoraba _op_addi.
        plain, fast, accelerated = self.both(Doubling, source=self.LINEAR)
        self.assertEqual(plain, fast)
        self.assertEqual((fast[5][1], accelerated), (1000, 0))

    def test_overridden_helper_is_honoured_with_the_accelerator_on(self) -> None:
        class StackQuota(TramoyaVM32):
            def _push(self, value: int) -> None:
                if len(self._stack) >= 50:
                    raise VMRuntimeError("Cuota de pila de la subclase")
                super()._push(value)

        plain, fast, accelerated = self.both(StackQuota)
        self.assertEqual(plain, fast)
        self.assertEqual((fast[0], len(fast[7]), accelerated), ("FAULTED", 50, 0))

    def test_instruction_hook_sees_every_instruction(self) -> None:
        class Counting(TramoyaVM32):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.seen = 0

            def _execute_one(self) -> None:
                self.seen += 1
                super()._execute_one()

        vm = Counting(VMConfig(memory_words=MEMORY, trace_size=0))
        vm.load_program(program(self.LOOP))
        vm.run()
        self.assertEqual((vm.seen, vm.accelerated_instructions), (vm.result().instructions, 0))

    def test_instance_level_patch_is_honoured(self) -> None:
        def patch(vm: TramoyaVM32) -> None:
            base = vm._add
            vm._add = lambda destination, left, right: base(destination, left, right + 1)

        plain, fast, accelerated = self.both(TramoyaVM32, patch)
        self.assertEqual(plain, fast)
        self.assertEqual((fast[5][1], accelerated), (600, 0))


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


class ExtensionAnchors(unittest.TestCase):
    """Un ancla por extensión: saltos hacia delante, salidas anticipadas, DIV/MOD y PUSH/POP."""

    def accelerated(self, source: str, **config) -> tuple[tuple, int]:
        plain, fast, accelerated = run_both(source, **config)
        self.assertEqual(plain, fast)
        self.assertGreater(accelerated, 0)
        return plain, accelerated

    def test_forward_branch_inside_the_body(self) -> None:
        source = """.code
    MOVI R3, 200
L:
    SUBI R6, R3, 100
    JPOS SKIP
    SUB R6, R0, R6          ; R6 = |R3 - 100|
SKIP:
    ADD R5, R5, R6
    SUBI R3, R3, 1
    JNZ L
    HALT
"""
        plain, accelerated = self.accelerated(source)
        self.assertEqual(plain[5][5], sum(abs(i - 100) for i in range(1, 201)))
        # Tras el calentamiento, 68 vueltas saltan el SUB (5 instrucciones) y 100 no (6).
        self.assertEqual(accelerated, 68 * 5 + 100 * 6)

    def test_if_else_with_a_jump_over_the_else(self) -> None:
        source = """.code
    MOVI R3, 120
    MOVI R8, 1
L:
    AND R9, R3, R8
    JZ PAR
    ADDI R5, R5, 3          ; impar
    JMP SIGUE
PAR:
    SUBI R5, R5, 1          ; par
SIGUE:
    SUBI R3, R3, 1
    JNZ L
    HALT
"""
        plain, _ = self.accelerated(source)
        self.assertEqual(plain[5][5], 60 * 3 - 60)

    def test_early_exit_is_the_last_instruction(self) -> None:
        source = """.code
    MOVI R3, 0
TOP:
    CMPI R3, 150
    JZ FIN
    ADDI R3, R3, 1
    JMP TOP
FIN:
    SYSCALL 100
    HALT
"""
        plain, accelerated = self.accelerated(source)
        (last, registers, *_), = plain[-1]  # lo que vio la syscall espía justo tras la salida
        self.assertEqual((last["pc"], last["opcode"], registers[3]), (8, "JZ", 150))
        self.assertEqual(accelerated, (150 - LOOP_WARMUP) * 4 + 2)

    def test_division_by_zero_inside_the_loop_faults_identically(self) -> None:
        source = """.code
    MOVI R3, 100
    MOVI R10, 100000
L:
    SUBI R4, R3, 40
    DIV R11, R10, R4
    MOD R12, R10, R3
    ADD R13, R13, R11
    SUBI R3, R3, 1
    JNZ L
    HALT
"""
        plain, accelerated = self.accelerated(source)
        self.assertEqual((plain[0], plain[1], plain[5][3]), ("FAULTED", "División entre cero", 40))
        # 28 vueltas aceleradas y el SUBI de la vuelta que falla; el DIV lo ejecuta el intérprete.
        self.assertEqual(accelerated, (60 - LOOP_WARMUP) * 6 + 1)

    def test_min_int_divided_by_minus_one_overflows(self) -> None:
        source = """.code
    MOVI R3, 100
    MOVI R10, -2147483648
    MOVI R9, -1
L:
    DIV R11, R10, R9        ; INT32_MIN / -1 = INT32_MIN con O = 1
    JGT DESBORDA            ; N = O = 1: salta
    ADDI R5, R5, 1
DESBORDA:
    MOD R12, R10, R9        ; 0 sin desbordamiento
    ADD R6, R6, R12
    DIV R13, R10, R3
    SUBI R3, R3, 1
    JNZ L
    HALT
"""
        plain, _ = self.accelerated(source)
        self.assertEqual(plain[5][5:7], (0, 0))
        self.assertEqual(plain[5][11:14], (-(2**31), 0, -(2**31)))

    def test_push_overflow_and_r15_inside_the_loop(self) -> None:
        source = """.code
    MOVI R3, 100
L:
    PUSH R3
    MOV R7, R15
    ADD R8, R8, R15
    SUBI R3, R3, 1
    JNZ L
    HALT
"""
        plain, accelerated = self.accelerated(source, stack_limit=64)
        self.assertEqual((plain[0], plain[1]), ("FAULTED", "Desbordamiento superior de pila"))
        self.assertEqual((len(plain[7]), plain[5][7], plain[5][8], plain[5][15]), (64, 64, 64 * 65 // 2, 64))
        self.assertEqual(accelerated, (64 - LOOP_WARMUP) * 5)

    def test_r15_is_current_right_after_the_loop(self) -> None:
        source = """.code
    MOVI R3, 100
L:
    PUSH R3
    SUBI R3, R3, 1
    JNZ L
    MOV R1, R15             ; el intérprete lee R15 justo al salir del bucle
    HALT
"""
        plain, accelerated = self.accelerated(source)
        self.assertEqual((plain[5][1], plain[5][15]), (100, 100))
        self.assertEqual(accelerated, (100 - LOOP_WARMUP) * 3)

    def test_pop_underflow_inside_the_loop(self) -> None:
        source = """.code
    MOVI R3, 50
L:
    PUSH R3
    PUSH R3
    POP R4
    SUBI R3, R3, 1
    JNZ L
    MOVI R3, 200
M:
    POP R5
    ADD R6, R6, R5
    SUBI R3, R3, 1
    JNZ M
    HALT
"""
        plain, _ = self.accelerated(source)
        self.assertEqual((plain[0], plain[1]), ("FAULTED", "Desbordamiento inferior de pila"))
        self.assertEqual((plain[5][6], plain[7], plain[5][15]), (sum(range(1, 51)), (), 0))

    def test_backward_jump_inside_the_body_is_left_to_the_interpreter(self) -> None:
        source = """.code
    MOVI R7, 40
OUT:
    MOVI R8, 40
IN:
    ADDI R9, R9, 1
    SUBI R8, R8, 1
    JNZ IN
    SUBI R7, R7, 1
    JNZ OUT
    HALT
"""
        plain, _ = self.accelerated(source)
        self.assertEqual(plain[5][9], 1600)
        prog = program(source)
        vm = TramoyaVM32(VMConfig(memory_words=MEMORY, trace_size=0))
        vm.load_program(prog)
        self.assertIsNone(compile_loop(vm, prog.symbols["OUT"]))
        self.assertIsNotNone(compile_loop(vm, prog.symbols["IN"]))


class FpuExtensionAnchors(unittest.TestCase):
    """FCMP, FTOI, FDIV y FSQRT dentro del cuerpo; sus fallos los ejecuta el intérprete."""

    def test_float_compare_drives_a_dda_like_loop(self) -> None:
        # Como el DDA de un raycaster: avanza el lado con menor distancia acumulada.
        source = """.code
    MOVI R1, 0x3F000000     ; sideX = 0.5
    MOVI R2, 0x3F400000     ; sideY = 0.75
    MOVI R3, 0x3F800000     ; deltaX = 1.0
    MOVI R4, 0x3FC00000     ; deltaY = 1.5
    MOVI R9, 300
L:
    FCMP R1, R2
    JLT PASO_X
    FADD R2, R2, R4
    ADDI R6, R6, 1
    JMP SIGUE
PASO_X:
    FADD R1, R1, R3
    ADDI R5, R5, 1
SIGUE:
    SUBI R9, R9, 1
    JNZ L
    FTOI R7, R1
    FSQRT R8, R2
    HALT
"""
        plain, fast, accelerated = run_both(source)
        self.assertEqual(plain, fast)
        self.assertEqual(plain[5][5] + plain[5][6], 300)
        self.assertGreater(accelerated, 0)

    def test_faults_inside_the_loop_are_left_to_the_interpreter(self) -> None:
        cases = {
            "FDIV R6, R1, R5": "División flotante entre cero",
            "FSQRT R6, R5": "Raíz cuadrada de número negativo",
            "FTOI R6, R5": "Conversión float→int de NaN o infinito",
        }
        for instruction, message in cases.items():
            with self.subTest(instruction=instruction):
                # R5 recorre 60 flotantes sanos y en la vuelta 61 vale 0.0, -1.0 o NaN.
                bad = {"FDIV": 0, "FSQRT": -1082130432, "FTOI": 0x7FC00000}[instruction.split()[0]]
                source = f""".code
    MOVI R1, 0x40A00000     ; 5.0
    MOVI R9, 100
L:
    MOVI R5, 0x3F800000     ; 1.0
    CMPI R9, 40
    JNZ SANO
    MOVI R5, {bad}
SANO:
    {instruction}
    ADD R7, R7, R6
    SUBI R9, R9, 1
    JNZ L
    HALT
"""
                plain, fast, accelerated = run_both(source)
                self.assertEqual(plain, fast)
                self.assertEqual((plain[0], plain[1], plain[5][9]), ("FAULTED", message, 40))
                self.assertGreater(accelerated, 0)

    def test_ftoi_out_of_int32_range_faults_identically(self) -> None:
        source = """.code
    MOVI R1, 0x4EFFFFC4     ; 2147475968.0 = 2^31 - 60 * 128
    MOVI R2, 0x43000000     ; 128.0 (un ulp a esta escala)
    MOVI R9, 100
L:
    FADD R1, R1, R2         ; en la vuelta 60 vale 2^31: FTOI desborda
    FTOI R6, R1
    SUBI R9, R9, 1
    JNZ L
    HALT
"""
        plain, fast, accelerated = run_both(source)
        self.assertEqual(plain, fast)
        self.assertEqual((plain[0], plain[1]), ("FAULTED", "Desbordamiento en conversión float→int"))
        self.assertEqual(plain[5][6], 2**31 - 128)
        self.assertEqual(accelerated, (59 - LOOP_WARMUP) * 4 + 1)

    def test_ftoi_truncates_towards_zero(self) -> None:
        source = """.code
    MOVI R1, 0xC1200000     ; -10.0
    MOVI R2, 0x3F333333     ; 0.7
    MOVI R9, 100
L:
    FADD R1, R1, R2
    FTOI R6, R1             ; trunca: -9.3 -> -9, 0.5 -> 0, 2.7 -> 2
    ADD R7, R7, R6
    SUBI R9, R9, 1
    JNZ L
    HALT
"""
        plain, fast, accelerated = run_both(source)
        self.assertEqual(plain, fast)
        self.assertGreater(accelerated, 0)
        expected, value = 0, -10.0
        for _ in range(100):
            value = struct.unpack("<f", struct.pack("<f", value + struct.unpack("<f", struct.pack("<I", 0x3F333333))[0]))[0]
            expected += int(value)
        self.assertEqual(plain[5][7], expected)

    def test_nan_comparison_sets_only_carry(self) -> None:
        source = """.code
    MOVI R1, 0x7FC00000     ; NaN
    MOVI R2, 0x3F800000
    MOVI R9, 100
L:
    FCMP R1, R2
    JNC NUNCA               ; con NaN, C = 1
    ADDI R5, R5, 1
NUNCA:
    FCMP R2, R2             ; iguales: Z = 1, N = 0
    JNEG MAL                ; lee N del FCMP, antes de que otra instrucción lo pise
    JNZ NUNCA2
    ADDI R6, R6, 1
NUNCA2:
    ADDI R7, R7, 1
MAL:
    SUBI R9, R9, 1
    JNZ L
    HALT
"""
        plain, fast, accelerated = run_both(source)
        self.assertEqual(plain, fast)
        self.assertEqual(plain[5][5:8], (100, 100, 100))
        self.assertGreater(accelerated, 0)


class ExtendedDifferentialFuzz(unittest.TestCase):
    """Fuzzing diferencial de las extensiones, con la syscall espía en cada salida del bucle."""

    OPS = DifferentialFuzz.OPS + ["DIV", "MOD", "PUSH", "POP"] * 3 + ["FDIV", "FSQRT", "FTOI", "FCMP"] * 2
    JUMPS = ["JMP", *DifferentialFuzz.BACKEDGES]
    # Bits float32: 1.0, -2.5, 0.5, 0.75, 2.7, -3.3, -0.0, +inf, -inf, NaN, 1e10, subnormal y los
    # bordes de FTOI: 2147483520.0 (cabe), 2^31 (desborda) y -2^31 (cabe justo).
    FLOATS = [0x3F800000, -1071644672, 0x3F000000, 0x3F400000, 0x402CCCCD, -1069128090, -(2**31),
              0x7F800000, -8388608, 0x7FC00000, 0x501502F9, 1, 0x4EFFFFFF, 0x4F000000, -822083584]

    def random_instruction(self, rng: random.Random, dest, src, imm) -> str:
        op = rng.choice(self.OPS)
        if op in {"MOV", "NOT", "FABS", "ITOF", "FSQRT", "FTOI"}:
            return f"{op} {dest()}, {src()}"
        if op in {"TEST", "CMP", "FCMP"}:
            return f"{op} {src()}, {src()}"
        if op in {"MOVI", "CMPI"}:
            return f"{op} {dest() if op == 'MOVI' else src()}, {imm()}"
        if op in {"LOAD", "STORE"}:
            first = dest() if op == "LOAD" else src()
            return f"{op} {first}, [{src()}{rng.choice(['', '+1', '-2', '+500'])}]"
        if op in {"ADDI", "SUBI", "MULI"}:
            return f"{op} {dest()}, {src()}, {imm()}"
        if op in {"SHL", "SHR"}:
            return f"{op} {dest()}, {src()}, {rng.randint(0, 31)}"
        if op == "NOP":
            return "NOP"
        if op == "PUSH":
            return f"PUSH {src()}"
        if op == "POP":
            return f"POP {dest()}"
        return f"{op} {dest()}, {src()}, {src()}"

    def random_loop(self, rng: random.Random) -> str:
        counter = rng.randint(1, 14)
        # Casi siempre el cuerpo respeta el contador, para que el bucle pase del calentamiento.
        writable = [0, *(i for i in range(1, 15) if i != counter or rng.random() < 0.3)]
        dest = lambda: f"R{rng.choice(writable)}"  # noqa: E731
        src = lambda: f"R{rng.choice([0, 15, *range(1, 15), *range(1, 15)])}"  # noqa: E731
        imm = lambda: rng.choice([0, 1, -1, 2, 7, 1000, -1000, 2**31 - 1, -(2**31), rng.randint(-5000, 5000)])  # noqa: E731
        lines = [".code"]
        for index in range(1, 15):
            lines.append(f"    MOVI R{index}, {rng.choice([0, 1, 5, 100, 1000, 3000, -3, -1, imm(), *self.FLOATS])}")
        lines += [f"    PUSH R{rng.randint(1, 14)}" for _ in range(rng.randint(0, 6))]
        lines.append(f"    MOVI R{counter}, {rng.choice([rng.randint(1, 40), rng.randint(40, 120), rng.randint(40, 120)])}")
        size = rng.randint(1, 10)
        body = [self.random_instruction(rng, dest, src, imm) for _ in range(size)]
        if rng.random() < 0.3:  # divisor que solo llega a cero al final, como en código real
            body[rng.randrange(size)] = f"{rng.choice(['DIV', 'MOD'])} {dest()}, {src()}, R{counter}"
        for index in range(size - 1):  # la mitad de los PUSH se equilibran con un POP posterior
            if body[index].startswith("PUSH") and rng.random() < 0.5:
                body[rng.randint(index + 1, size - 1)] = f"POP {dest()}"
        jumps: dict[int, list[str]] = {}
        for _ in range(rng.choice([0, 1, 1, 2, 2, 3])):
            at = rng.randint(0, size)
            kind = rng.random()
            if kind < 0.55:   # hacia delante dentro del cuerpo (B{size+1} es el salto de vuelta)
                jump = f"{rng.choice(self.JUMPS)} B{rng.randint(at + 1, size + 1)}"
            elif kind < 0.7:  # salida anticipada con condición arbitraria
                jump = f"{rng.choice(DifferentialFuzz.BACKEDGES)} X{rng.randint(0, 1)}"
            elif kind < 0.9:  # salida anticipada tardía: la de un bucle con prueba de fin
                jump = (f"CMPI R{counter}, {rng.randint(0, 5)}\n"
                        f"    {rng.choice(['JZ', 'JLT', 'JNC'])} X{rng.randint(0, 1)}")
            else:             # hacia atrás: el acelerador debe rechazarlo
                jump = f"{rng.choice(self.JUMPS)} {rng.choice(['L', *(f'B{k}' for k in range(1, at + 1))])}"
            jumps.setdefault(at, []).append(f"    {jump}")
        lines.append("L:")
        for k in range(size + 1):
            lines.append(f"B{k}:")
            lines += jumps.get(k, [])
            lines.append(f"    {body[k]}" if k < size else f"    SUBI R{counter}, R{counter}, 1")
        backedge = rng.choice(["JNZ", "JNZ", "JNZ", "JPOS", *DifferentialFuzz.BACKEDGES])
        lines.append(f"B{size + 1}:\n    {backedge} L")
        lines.append(f"X0:\n    SYSCALL {SPY}\n    ADDI R2, R2, 7")
        lines.append(f"X1:\n    SYSCALL {SPY}\n    MOV R1, R2\n    SYSCALL 1\n    HALT")
        return "\n".join(lines)

    @staticmethod
    def features(source: str) -> set[str]:
        """Extensiones que usa el bucle L si el acelerador lo compila."""
        prog = program(source)
        vm = TramoyaVM32(VMConfig(memory_words=MEMORY, trace_size=0))
        vm.load_program(prog)
        loop = compile_loop(vm, prog.symbols["L"])
        if loop is None:
            return set()
        found = {"salida"} if loop.exits else set()
        for pc in range(loop.header, loop.backedge[0], 4):
            opcode, target = prog.words[pc], prog.words[pc + 1]
            if VMOpcode(opcode).name.startswith("J") and target <= loop.backedge[0]:
                found.add("adelante")
            elif opcode in {VMOpcode.DIV, VMOpcode.MOD}:
                found.add("division")
            elif opcode in {VMOpcode.PUSH, VMOpcode.POP}:
                found.add("pila")
            elif opcode in {VMOpcode.FDIV, VMOpcode.FSQRT, VMOpcode.FTOI, VMOpcode.FCMP}:
                found.add("fpu")
        return found

    def test_random_loops_with_extensions_match_the_interpreter(self) -> None:
        rng = random.Random(2027)
        exercised: dict[str, int] = {"adelante": 0, "salida": 0, "division": 0, "pila": 0, "fpu": 0}
        for case in range(800):
            source = self.random_loop(rng)
            with self.subTest(case=case, source=source):
                plain, fast, accelerated = run_both(
                    source, gas_limit=rng.choice([3_000, 20_000]), max_instructions=rng.choice([None, None, 333]),
                    stack_limit=rng.choice([8_192, 8_192, 4, 16]), protect_code=rng.choice([True, True, False]))
                self.assertEqual(plain, fast)
                if accelerated:
                    for feature in self.features(source):
                        exercised[feature] += 1
        # El fuzz debe ejercitar de verdad cada extensión en bucles acelerados.
        for feature, count in exercised.items():
            self.assertGreaterEqual(count, 30, (feature, exercised))


if __name__ == "__main__":
    unittest.main()
