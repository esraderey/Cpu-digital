from __future__ import annotations

import unittest

from cpu_digital.vm32_assembler import Program32, VM32Assembler, VMAssemblyError, disassemble_vm32
from cpu_digital.vm32_isa import VMOpcode


class VM32AssemblerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assembler = VM32Assembler()

    def test_sections_labels_and_memory_operands(self) -> None:
        result = self.assembler.assemble(
            """
            .code
            .entry _start
            _start:
                LEA R4, DATOS
                LOAD R1, [R4+1]
                STORE R1, [R4-0]
                HALT
            .data
            DATOS: .WORD 10, 20
            """
        )
        program = result.program
        self.assertEqual(program.entry, 0)
        self.assertEqual(program.code_size, 16)
        self.assertEqual(program.symbols["DATOS"], 16)
        self.assertEqual(program.words[6:8], (4, 1))
        self.assertEqual(program.words[-2:], (10, 20))

    def test_directives_string_space_align_and_constants(self) -> None:
        result = self.assembler.assemble(
            """
            .equ CANTIDAD 3
            .code
            HALT
            .data
            TEXTO: .STRING "A"
            .SPACE CANTIDAD
            .ALIGN 8
            FINAL: .WORD TEXTO, 'Z'
            """
        )
        program = result.program
        self.assertEqual(program.symbols["TEXTO"], 4)
        self.assertEqual(program.symbols["FINAL"], 12)
        self.assertEqual(program.data_size, 10)
        self.assertEqual(program.words[-2:], (4, 90))

    def test_binary_round_trip_and_crc(self) -> None:
        program = self.assembler.assemble(".code\nMOVI R1, 7\nHALT").program
        encoded = program.to_bytes()
        self.assertEqual(Program32.from_bytes(encoded), program)
        damaged = bytearray(encoded)
        damaged[-1] ^= 0x01
        with self.assertRaisesRegex(ValueError, "CRC"):
            Program32.from_bytes(bytes(damaged))

    def test_disassembly_has_fixed_width_addresses(self) -> None:
        program = self.assembler.assemble(".code\nMOVI R1, 7\nHALT").program
        lines = disassemble_vm32(program)
        self.assertTrue(lines[0].startswith("00000000: MOVI"))
        self.assertTrue(lines[1].startswith("00000004: HALT"))

    def test_branch_to_data_is_rejected(self) -> None:
        with self.assertRaisesRegex(VMAssemblyError, "Destino no ejecutable"):
            self.assembler.assemble(".code\nJMP DATO\n.data\nDATO: .WORD 0")

    def test_instruction_in_data_is_rejected(self) -> None:
        with self.assertRaisesRegex(VMAssemblyError, "solo son válidas"):
            self.assembler.assemble(".code\nHALT\n.data\nNOP")

    def test_bad_register_is_rejected(self) -> None:
        with self.assertRaisesRegex(VMAssemblyError, "Registro inválido"):
            self.assembler.assemble(".code\nMOVI R16, 1\nHALT")

    def test_aliases(self) -> None:
        program = self.assembler.assemble(".code\nCMPI R1, 0\nJE FIN\nFIN: HALT").program
        self.assertEqual(program.words[4], int(VMOpcode.JZ))

    def test_program_word_limit_rejects_huge_space_before_allocation(self) -> None:
        source = ".code\nHALT\n.data\nBLOQUE: .SPACE 1000000000"

        with self.assertRaisesRegex(VMAssemblyError, "excede el límite"):
            self.assembler.assemble(source, max_words=1024)


if __name__ == "__main__":
    unittest.main()
