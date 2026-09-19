from __future__ import annotations

import json
import unittest
from pathlib import Path

from cpu_digital import Assembler, CPU, Opcode


ROOT = Path(__file__).resolve().parents[1]


def assemble_demo(name: str):
    source = (ROOT / "programs" / name).read_text(encoding="utf-8")
    return Assembler().assemble(source)


class CPUTests(unittest.TestCase):
    def test_original_program_is_compatible(self) -> None:
        cpu = CPU()
        cpu.load_program([10, 100, 20, 101, 30, 102, 99])
        cpu.ctx["memory"][100] = 15
        cpu.ctx["memory"][101] = 25

        result = cpu.run()

        self.assertTrue(result.ok)
        self.assertEqual(result.cycles, 11)
        self.assertEqual(cpu.ctx["memory"][102], 40)

    def test_loop_program(self) -> None:
        program = assemble_demo("suma_1_a_10.asm")
        cpu = CPU()
        cpu.load_program(program.words)
        result = cpu.run()
        self.assertEqual(result.output, (55,))

    def test_factorial_program(self) -> None:
        program = assemble_demo("factorial_5.asm")
        cpu = CPU()
        cpu.load_program(program.words)
        self.assertEqual(cpu.run().output, (120,))

    def test_subroutine_balances_stack(self) -> None:
        program = assemble_demo("subrutina.asm")
        cpu = CPU()
        cpu.load_program(program.words)
        result = cpu.run()
        self.assertEqual(result.output, (49,))
        self.assertEqual(cpu.ctx["stack"], [])

    def test_input_output(self) -> None:
        program = assemble_demo("entrada.asm")
        cpu = CPU()
        cpu.load_program(program.words, inputs=[7, 8])
        self.assertEqual(cpu.run().output, (15,))

    def test_empty_input_is_a_controlled_fault(self) -> None:
        cpu = CPU()
        cpu.load_program([int(Opcode.IN), int(Opcode.HALT)])
        result = cpu.run()
        self.assertEqual(result.state, "FAULT")
        self.assertIn("no tiene datos", result.fault or "")

    def test_unknown_opcode_is_a_controlled_fault(self) -> None:
        cpu = CPU()
        cpu.load_program([1234])
        result = cpu.run()
        self.assertEqual(result.state, "FAULT")
        self.assertEqual(result.cycles, 2)
        self.assertIn("Opcode desconocido", result.fault or "")

    def test_division_by_zero_is_a_controlled_fault(self) -> None:
        program = Assembler().assemble("LOADI 9\nDIV CERO\nHALT\nCERO: .WORD 0")
        cpu = CPU()
        cpu.load_program(program.words)
        result = cpu.run()
        self.assertEqual(result.state, "FAULT")
        self.assertIn("División entre cero", result.fault or "")

    def test_signed_16_bit_overflow_sets_flags(self) -> None:
        program = Assembler().assemble("LOADI 32767\nADDI 1\nHALT")
        cpu = CPU()
        cpu.load_program(program.words)
        result = cpu.run()
        self.assertEqual(result.accumulator, -32768)
        self.assertTrue(cpu.ctx["flags"]["N"])
        self.assertTrue(cpu.ctx["flags"]["O"])

    def test_break_instruction_can_resume(self) -> None:
        program = assemble_demo("breakpoint.asm")
        cpu = CPU()
        cpu.load_program(program.words)
        paused = cpu.run()
        self.assertEqual(paused.state, "PAUSED")
        self.assertIn("BREAK", paused.pause_reason or "")
        self.assertEqual(cpu.resume(), "FETCH")
        self.assertEqual(cpu.run().output, (42,))

    def test_external_pause_returns_to_exact_microstate(self) -> None:
        cpu = CPU()
        cpu.load_program([int(Opcode.NOP), int(Opcode.HALT)])
        cpu.step()
        self.assertEqual(cpu.state, "DECODE")
        cpu.pause("prueba")
        self.assertEqual(cpu.state, "PAUSED")
        self.assertEqual(cpu.resume(), "DECODE")

    def test_timeout_stops_infinite_program(self) -> None:
        cpu = CPU()
        cpu.load_program([int(Opcode.JMP), 0])
        result = cpu.run(max_cycles=5)
        self.assertEqual(result.state, "FAULT")
        self.assertEqual(result.cycles, 5)
        self.assertIn("Límite", result.fault or "")

    def test_undo_restores_state_and_context(self) -> None:
        cpu = CPU()
        cpu.load_program([int(Opcode.LOADI), 5, int(Opcode.HALT)])
        cpu.step()
        cpu.step()
        cpu.step()
        self.assertEqual(cpu.ctx["acc"], 5)
        self.assertEqual(cpu.state, "FETCH")
        cpu.undo()
        self.assertEqual(cpu.state, "EXECUTE")
        self.assertEqual(cpu.ctx["acc"], 0)

    def test_snapshot_round_trip(self) -> None:
        cpu = CPU()
        cpu.load_program([int(Opcode.LOADI), 12, int(Opcode.HALT)])
        cpu.step()
        snapshot = cpu.snapshot()
        expected = json.loads(snapshot)
        cpu.step()
        cpu.restore(snapshot)
        self.assertEqual(cpu.machine.to_dict(), expected)

    def test_diagrams_are_generated_by_tramoya(self) -> None:
        cpu = CPU()
        self.assertIn("stateDiagram-v2", cpu.diagram_mermaid())
        self.assertIn("digraph", cpu.diagram_dot())


if __name__ == "__main__":
    unittest.main()

