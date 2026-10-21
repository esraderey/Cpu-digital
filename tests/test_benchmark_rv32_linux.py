"""Anclas del arnés de arranque de Linux (H3.5) sin la imagen externa: un «núcleo» sintético que escribe por el UART, pide
login, lee la línea tecleada y escribe el prompt; la sonda teclea ``root`` al ver ``login: `` y para con el prompt."""

from __future__ import annotations

import unittest
from unittest import mock

import benchmarks.benchmark_rv32_linux as L
from cpu_digital.rv32_isa import Kind, encode
from cpu_digital.rv32_platform import UART_BASE, Machine
from tests.test_rv32_hart import assemble, load_immediate


def say(text: str) -> list[int]:
    words: list[int] = []
    for char in text.encode("ascii"):
        words += [encode(Kind.ADDI, 6, 0, imm=char), encode(Kind.SB, 0, 5, 6, imm=0)]
    return words


def fake_kernel() -> bytes:
    # x5 = UART; escribe «Linux», «login: », espera y lee bytes hasta '\n' (sondeando LSR.DR), escribe «~ # » y se queda en
    # un bucle (la sonda para antes). x7 cuenta los bytes leídos.
    words = load_immediate(5, UART_BASE) + say("Linux\n") + say("login: ")
    poll = len(words)
    words += [encode(Kind.LBU, 8, 5, imm=5), encode(Kind.ANDI, 8, 8, imm=1), encode(Kind.BEQ, 0, 8, 0, imm=-8),
              encode(Kind.LBU, 9, 5, imm=0), encode(Kind.ADDI, 7, 7, imm=1), encode(Kind.ADDI, 10, 0, imm=10),
              encode(Kind.BNE, 0, 9, 10, imm=-24)]
    assert len(words) == poll + 7
    words += say("~ # ") + [encode(Kind.JAL, 0, imm=0)]
    return assemble(words)


class LinuxHarness(unittest.TestCase):
    def test_the_probe_types_root_at_login_and_stops_exactly_at_the_prompt(self) -> None:
        image = fake_kernel()
        results = []
        with mock.patch.object(L, "RAM", 1 << 20), mock.patch.object(L, "CHUNK", 7):
            for accelerate, warmup in ((False, 2), (True, 1), (True, 2)):
                with self.subTest(accelerate=accelerate, warmup=warmup):
                    result = L.boot_linux(image, accelerate, warmup)
                    self.assertEqual(result["parada"], "prompt")
                    self.assertTrue(result["uart_cola"].endswith("Linux\nlogin: ~ # "), result["uart_cola"])
                    marks = result["marcas_instret"]
                    self.assertLess(marks["primer_byte"], marks["login"])
                    self.assertLess(marks["login"], marks["prompt"])
                    self.assertEqual(marks["prompt"], result["instret"])        # para en la escritura que cierra «# »
                    results.append((result["instret"], result["gas"], marks))
        self.assertEqual(len({repr(r) for r in results}), 1)                  # mismas cifras en los tres modos

    def test_the_marks_do_not_depend_on_the_chunk_size(self) -> None:
        image = fake_kernel()
        seen = set()
        for chunk in (1, 13, 5_000_000):
            with mock.patch.object(L, "RAM", 1 << 20), mock.patch.object(L, "CHUNK", chunk):
                result = L.boot_linux(image, False, 2)
            seen.add((result["instret"], result["gas"], tuple(sorted(result["marcas_instret"].items()))))
        self.assertEqual(len(seen), 1, seen)

    def test_stop_at_login_and_the_flat_image_carry_the_dtb(self) -> None:
        image = fake_kernel()
        with mock.patch.object(L, "RAM", 1 << 20):
            login = L.boot_linux(image, False, 2, stop_at="login")
            flat, regs = L.boot_image(image)
        self.assertEqual(login["parada"], "login")
        self.assertEqual(flat[:len(image)], image)
        self.assertEqual(flat[regs[11] - 0x80000000:][:4], bytes.fromhex("d00dfeed"))
        self.assertEqual(regs[10], 0)
        machine = Machine(ram_bytes=1 << 20)
        self.addCleanup(machine.close)
        machine.load_flat(flat)
        for register, value in regs.items():
            machine.hart.regs[register] = value
        self.assertEqual(machine.run(login["gas"]).reason, "gas")             # el mismo gas llega al mismo sitio
        self.assertTrue(machine.uart.text.endswith("login: "))


if __name__ == "__main__":
    unittest.main()
