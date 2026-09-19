from __future__ import annotations

import unittest

from cpu_digital import Assembler, AssemblyError, Opcode
from cpu_digital.assembler import disassemble


class AssemblerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assembler = Assembler()

    def test_labels_and_forward_references(self) -> None:
        result = self.assembler.assemble(
            """
            INICIO: LOADI 3
                    SAVE DATO
                    JMP FIN
            DATO:   .WORD 0
            FIN:    HALT
            """
        )
        self.assertEqual(result.symbols["INICIO"], 0)
        self.assertEqual(result.symbols["DATO"], 6)
        self.assertEqual(result.symbols["FIN"], 7)
        self.assertEqual(result.words[5], 7)

    def test_directives_constants_strings_and_org(self) -> None:
        result = self.assembler.assemble(
            """
            .EQU BASE 4
            .ORG BASE
            TEXTO: .STRING "A"
            VALOR: .WORD TEXTO, ' ', 0x2A
            """
        )
        self.assertEqual(result.words[:4], (0, 0, 0, 0))
        self.assertEqual(result.words[4:6], (65, 0))
        self.assertEqual(result.words[6:], (4, 32, 42))

    def test_aliases_and_character_literal(self) -> None:
        result = self.assembler.assemble("LOADI ' '\nPRINTC\nHALT")
        self.assertEqual(result.words, (int(Opcode.LOADI), 32, int(Opcode.OUTC), int(Opcode.HALT)))

    def test_disassembler(self) -> None:
        result = self.assembler.assemble("LOADI 5\nOUT\nHALT\n.WORD 123")
        lines = list(disassemble(result.words, stop_at_halt=True))
        self.assertEqual(lines, ["0000: LOADI 5", "0002: OUT", "0003: HALT"])

    def test_reports_unknown_instruction(self) -> None:
        with self.assertRaisesRegex(AssemblyError, "Instrucción desconocida"):
            self.assembler.assemble("MAGIA 7")

    def test_reports_duplicate_symbol(self) -> None:
        with self.assertRaisesRegex(AssemblyError, "Símbolo duplicado"):
            self.assembler.assemble("A: NOP\nA: HALT")

    def test_reports_missing_operand(self) -> None:
        with self.assertRaisesRegex(AssemblyError, "requiere un operando"):
            self.assembler.assemble("LOAD\nHALT")


if __name__ == "__main__":
    unittest.main()

