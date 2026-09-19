from __future__ import annotations

import json
import unittest
import zlib
from pathlib import Path

from cpu_digital.vm32 import MAX_MEMORY_WORDS, SNAPSHOT_MAGIC, VMConfig, TramoyaVM32
from cpu_digital.vm32_assembler import VM32Assembler


ROOT = Path(__file__).resolve().parents[1]


def program(source: str):
    return VM32Assembler().assemble(source).program


def demo(name: str):
    path = ROOT / "vm_programs" / name
    return VM32Assembler().assemble(path.read_text(encoding="utf-8"), str(path)).program


class VM32Tests(unittest.TestCase):
    def test_factorial_real_program(self) -> None:
        vm = TramoyaVM32()
        vm.load_program(demo("factorial_10.tasm"))
        result = vm.run()
        self.assertTrue(result.ok)
        self.assertEqual(result.output, (3_628_800,))

    def test_fibonacci_real_program(self) -> None:
        vm = TramoyaVM32()
        vm.load_program(demo("fibonacci_20.tasm"))
        result = vm.run()
        self.assertEqual(result.output[-3:], (1597, 2584, 4181))

    def test_indirect_memory_array_sum(self) -> None:
        vm = TramoyaVM32()
        vm.load_program(demo("array_sum.tasm"))
        self.assertEqual(vm.run().output, (408,))

    def test_waiting_input_and_wakeup(self) -> None:
        vm = TramoyaVM32()
        vm.load_program(demo("entrada.tasm"), inputs=[7])
        waiting = vm.run()
        self.assertEqual(waiting.state, "WAITING")
        self.assertIn("entrada", waiting.wait_reason or "")
        self.assertEqual(vm.provide_input(8), "RUNNING")
        self.assertEqual(vm.run().output, (15,))

    def test_code_is_write_protected(self) -> None:
        vm = TramoyaVM32()
        vm.load_program(demo("proteccion.tasm"))
        result = vm.run()
        self.assertEqual(result.state, "FAULTED")
        self.assertIn("código protegido", result.fault or "")
        self.assertEqual(vm.memory_slice(0, 1)[0], 2)  # MOVI sigue intacto.

    def test_arithmetic_overflow(self) -> None:
        vm = TramoyaVM32()
        vm.load_program(program(".code\nMOVI R1, 0x7fffffff\nADDI R2, R1, 1\nHALT"))
        vm.run()
        self.assertEqual(vm.registers[2], -2_147_483_648)
        self.assertTrue(vm.flags["O"])
        self.assertTrue(vm.flags["N"])

    def test_division_by_zero_fault_is_atomic(self) -> None:
        vm = TramoyaVM32()
        vm.load_program(program(".code\nMOVI R1, 10\nMOVI R2, 0\nMOVI R3, 99\nDIV R3, R1, R2\nHALT"))
        result = vm.run()
        self.assertEqual(result.state, "FAULTED")
        self.assertEqual(vm.registers[3], 99)
        self.assertIn("División entre cero", result.fault or "")

    def test_call_ret_and_stack_pointer(self) -> None:
        vm = TramoyaVM32()
        vm.load_program(
            program(
                """
                .code
                MOVI R1, 6
                CALL DOBLE
                HALT
                DOBLE:
                ADD R1, R1, R1
                RET
                """
            )
        )
        result = vm.run()
        self.assertTrue(result.ok)
        self.assertEqual(vm.registers[1], 12)
        self.assertEqual(vm.registers[15], 0)

    def test_stack_limit_is_enforced(self) -> None:
        vm = TramoyaVM32(VMConfig(stack_limit=2))
        vm.load_program(program(".code\nPUSH R0\nPUSH R0\nPUSH R0\nHALT"))
        result = vm.run()
        self.assertEqual(result.state, "FAULTED")
        self.assertIn("superior de pila", result.fault or "")
        self.assertEqual(len(vm.stack), 2)

    def test_gas_limit_is_enforced_before_instruction(self) -> None:
        vm = TramoyaVM32(VMConfig(gas_limit=3))
        vm.load_program(program(".code\nMOVI R1, 5\nSYSCALL 1\nHALT"))
        result = vm.run()
        self.assertEqual(result.state, "FAULTED")
        self.assertIn("Gas agotado", result.fault or "")
        self.assertEqual(result.output, ())

    def test_capability_policy_blocks_io(self) -> None:
        vm = TramoyaVM32(VMConfig(capabilities=frozenset()))
        vm.load_program(program(".code\nMOVI R1, 5\nSYSCALL 1\nHALT"))
        result = vm.run()
        self.assertEqual(result.state, "FAULTED")
        self.assertIn("Capacidad no autorizada", result.fault or "")

    def test_custom_syscall(self) -> None:
        vm = TramoyaVM32()
        vm.register_syscall(100, lambda machine: machine.set_register(1, 42), name="answer")
        vm.load_program(program(".code\nSYSCALL 100\nHALT"))
        self.assertTrue(vm.run().ok)
        self.assertEqual(vm.registers[1], 42)

    def test_fast_run_syncs_lifecycle_before_host_syscall(self) -> None:
        observed: list[int] = []
        vm = TramoyaVM32(VMConfig(trace_size=0))
        vm.register_syscall(100, lambda machine: observed.append(machine.machine.ctx["instructions"]))
        vm.load_program(program(".code\nMOVI R1, 1\nSYSCALL 100\nHALT"))

        vm.run()

        self.assertEqual(observed, [1])

    def test_host_exception_rolls_back_memory(self) -> None:
        assembled = VM32Assembler().assemble(
            ".code\nLEA R2, DATO\nSYSCALL 101\nHALT\n.data\nDATO: .WORD 7"
        ).program
        address = assembled.symbols["DATO"]
        vm = TramoyaVM32()

        def bad_handler(machine: TramoyaVM32) -> None:
            machine.write_memory(address, 999)
            raise RuntimeError("boom")

        vm.register_syscall(101, bad_handler)
        vm.load_program(assembled)
        result = vm.run()
        self.assertEqual(result.state, "FAULTED")
        self.assertEqual(vm.read_memory(address), 7)
        self.assertIn("Excepción de host", result.fault or "")

    def test_external_interrupt_and_iret(self) -> None:
        assembled = demo("interrupcion.tasm")
        vm = TramoyaVM32()
        vm.load_program(assembled)
        vm.set_interrupt_vector(1, assembled.symbols["ISR_1"])
        vm.request_interrupt(1)
        result = vm.run()
        self.assertEqual(result.output, (999,))
        self.assertTrue(result.ok)

    def test_unknown_interrupt_faults_without_losing_request(self) -> None:
        vm = TramoyaVM32()
        vm.load_program(program(".code\nHALT"))
        vm.request_interrupt(9)
        result = vm.run()
        self.assertEqual(result.state, "FAULTED")
        self.assertIn("no configurado", result.fault or "")

    def test_breakpoint_pause_and_resume(self) -> None:
        assembled = program(".code\nMOVI R1, 1\nADDI R1, R1, 1\nHALT")
        vm = TramoyaVM32()
        vm.load_program(assembled)
        paused = vm.run(breakpoints={4})
        self.assertEqual(paused.state, "PAUSED")
        self.assertEqual(vm.registers[1], 1)
        self.assertTrue(vm.run(breakpoints={4}).ok)
        self.assertEqual(vm.registers[1], 2)

    def test_local_instruction_limit_pauses(self) -> None:
        vm = TramoyaVM32()
        vm.load_program(program(".code\nLOOP: JMP LOOP"))
        result = vm.run(max_instructions=5)
        self.assertEqual(result.state, "PAUSED")
        self.assertEqual(result.instructions, 5)

    def test_snapshot_round_trip(self) -> None:
        vm = TramoyaVM32()
        vm.load_program(program(".code\nMOVI R1, 7\nADDI R1, R1, 5\nHALT"))
        vm.step()
        snapshot = vm.snapshot_bytes()
        vm.run()
        self.assertEqual(vm.registers[1], 12)
        vm.restore_bytes(snapshot)
        self.assertEqual(vm.state, "RUNNING")
        self.assertEqual(vm.registers[1], 7)
        self.assertTrue(vm.run().ok)
        self.assertEqual(vm.registers[1], 12)

    def test_corrupted_snapshot_is_rejected_atomically(self) -> None:
        vm = TramoyaVM32()
        vm.load_program(program(".code\nMOVI R1, 7\nHALT"))
        vm.step()
        before = vm.registers
        damaged = vm.snapshot_bytes()[:-3]
        with self.assertRaises(ValueError):
            vm.restore_bytes(damaged)
        self.assertEqual(vm.registers, before)

    def test_invalid_snapshot_keeps_empty_vm_in_created_state(self) -> None:
        vm = TramoyaVM32(VMConfig(memory_words=256))

        with self.assertRaises(ValueError):
            vm.restore_bytes(SNAPSHOT_MAGIC + zlib.compress(b'{"version":2}'))

        self.assertEqual(vm.state, "CREATED")
        self.assertIsNone(vm.program)

    def test_trace_can_be_disabled_for_hot_path(self) -> None:
        vm = TramoyaVM32(VMConfig(trace_size=0))
        vm.load_program(program(".code\nNOP\nHALT"))
        vm.run()
        self.assertEqual(vm.trace, ())

    def test_output_limit_is_enforced(self) -> None:
        vm = TramoyaVM32(VMConfig(output_limit=1))
        vm.load_program(program(".code\nMOVI R1, 1\nSYSCALL 1\nSYSCALL 1\nHALT"))
        result = vm.run()
        self.assertEqual(result.state, "FAULTED")
        self.assertEqual(result.output, (1,))
        self.assertIn("Límite de salida", result.fault or "")

    def test_deterministic_random(self) -> None:
        bytecode = program(".code\nSYSCALL 6\nMOV R2, R1\nSYSCALL 6\nHALT")
        first, second = TramoyaVM32(), TramoyaVM32()
        first.load_program(bytecode)
        second.load_program(bytecode)
        first.run()
        second.run()
        self.assertEqual(first.registers[1:3], second.registers[1:3])

    def test_lifecycle_is_a_real_tramoya_machine(self) -> None:
        vm = TramoyaVM32()
        self.assertEqual(vm.state, "CREATED")
        vm.load_program(program(".code\nHALT"))
        self.assertEqual(vm.state, "READY")
        self.assertIn("stateDiagram-v2", vm.lifecycle_mermaid())
        vm.step()
        self.assertEqual(vm.state, "HALTED")
        self.assertIn("READY", vm.machine.history)

    def test_sparse_memory_reaches_high_addresses_without_dense_allocation(self) -> None:
        vm = TramoyaVM32(VMConfig(memory_words=MAX_MEMORY_WORDS, trace_size=0))
        vm.load_program(program(".code\nHALT"))
        high_address = MAX_MEMORY_WORDS - 1

        vm.write_memory(high_address, 123456)

        self.assertEqual(vm.read_memory(high_address), 123456)
        self.assertLessEqual(vm.allocated_memory_pages, 2)
        self.assertLess(vm.allocated_memory_bytes, 64 * 1024)

    def test_sparse_snapshot_round_trip_at_maximum_logical_ram(self) -> None:
        config = VMConfig(memory_words=MAX_MEMORY_WORDS, trace_size=0)
        vm = TramoyaVM32(config)
        vm.load_program(program(".code\nHALT"))
        vm.write_memory(MAX_MEMORY_WORDS - 1, -77)

        snapshot = vm.snapshot_bytes()
        restored = TramoyaVM32(config)
        restored.restore_bytes(snapshot)

        self.assertLess(len(snapshot), 100_000)
        self.assertEqual(restored.read_memory(MAX_MEMORY_WORDS - 1), -77)
        self.assertEqual(restored.program.words, vm.program.words)

    def test_snapshot_v2_uses_pages_and_legacy_v1_still_loads(self) -> None:
        config = VMConfig(memory_words=256, trace_size=0)
        vm = TramoyaVM32(config)
        vm.load_program(program(".code\nMOVI R1, 9\nHALT"))
        vm.step()
        payload = json.loads(zlib.decompress(vm.snapshot_bytes()[len(SNAPSHOT_MAGIC):]))
        self.assertEqual(payload["version"], 2)
        self.assertIn("memory_pages", payload["core"])
        self.assertNotIn("memory", payload["core"])

        payload["version"] = 1
        payload["core"]["memory"] = list(vm.memory_slice(0, config.memory_words))
        payload["core"].pop("memory_pages")
        payload["core"].pop("memory_page_words")
        legacy = SNAPSHOT_MAGIC + zlib.compress(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        restored = TramoyaVM32(config)
        restored.restore_bytes(legacy)
        self.assertEqual(restored.registers[1], 9)

    def test_snapshot_factory_restores_embedded_memory_size(self) -> None:
        vm = TramoyaVM32(VMConfig(memory_words=4096, trace_size=0))
        vm.load_program(program(".code\nMOVI R1, 55\nHALT"))
        vm.step()

        restored = TramoyaVM32.from_snapshot_bytes(vm.snapshot_bytes())

        self.assertEqual(restored.config.memory_words, 4096)
        self.assertEqual(restored.registers[1], 55)

    def test_unprotected_code_write_invalidates_decode_cache(self) -> None:
        vm = TramoyaVM32(VMConfig(protect_code=False, trace_size=0))
        vm.load_program(program(".code\nMOVI R1, 1\nHALT"))

        vm.write_memory(2, 42)
        vm.run()

        self.assertEqual(vm.registers[1], 42)

    def test_print_string_scan_is_bounded_by_output_limit(self) -> None:
        vm = TramoyaVM32(VMConfig(output_limit=4, trace_size=0))
        vm.load_program(
            program(
                '.code\nLEA R1, TEXTO\nMOVI R2, 1000000\nSYSCALL 7\nHALT\n'
                '.data\nTEXTO: .STRING "demasiado largo"'
            )
        )

        result = vm.run()

        self.assertEqual(result.state, "FAULTED")
        self.assertIn("límite seguro", result.fault or "")

    def test_continuous_run_matches_instrumented_steps(self) -> None:
        bytecode = program(".code\nMOVI R1, 3\nADDI R1, R1, 4\nMULI R2, R1, 2\nHALT")
        fast = TramoyaVM32()
        stepped = TramoyaVM32()
        fast.load_program(bytecode)
        stepped.load_program(bytecode)

        fast.run()
        while stepped.state not in {"HALTED", "FAULTED"}:
            stepped.step()

        self.assertEqual(fast.result(), stepped.result())
        self.assertEqual(fast.registers, stepped.registers)
        self.assertEqual(fast.memory_slice(0, len(bytecode.words)), stepped.memory_slice(0, len(bytecode.words)))


if __name__ == "__main__":
    unittest.main()
