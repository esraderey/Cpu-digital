"""Ancla de extremo a extremo del acelerador sobre código de juego real.

``vm_programs/juego_raycaster.tasm`` (raycaster con 16 entidades, colisiones,
sprites y minimapa sobre un framebuffer de 160x120) debe dejar exactamente el
mismo estado con y sin acelerador de bucles, y el acelerador debe intervenir.
"""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from cpu_digital.vm32 import TramoyaVM32, VMConfig
from cpu_digital.vm32_assembler import VM32Assembler

ROOT = Path(__file__).resolve().parents[1]
GAME = VM32Assembler().assemble(
    (ROOT / "vm_programs" / "juego_raycaster.tasm").read_text(encoding="utf-8"), "juego_raycaster.tasm"
).program
FB, PIXELS = 0x10000, 160 * 120


def play(frames: int, accelerate: bool, **run) -> tuple[TramoyaVM32, tuple]:
    vm = TramoyaVM32(VMConfig(gas_limit=10**9, trace_size=0, accelerate_loops=accelerate))
    vm.load_program(GAME, inputs=[frames])
    vm.run(**run)
    result = vm.result()
    state = (result, vm.pc, tuple(vm._registers), dict(vm.flags), vm.stack,
             vm.machine.ctx.get("last_instruction"), vm.snapshot_bytes())
    return vm, state


class GameMatchesTheInterpreter(unittest.TestCase):
    def test_two_frames_are_identical_with_and_without_the_accelerator(self) -> None:
        _, plain = play(2, accelerate=False)
        vm, fast = play(2, accelerate=True)
        self.assertEqual(plain, fast)
        self.assertEqual(plain[0].state, "HALTED", plain[0].fault)
        self.assertEqual(len(plain[0].output), 5)
        self.assertGreater(vm.accelerated_instructions, 0)
        framebuffer = vm.memory_slice(FB, PIXELS)
        self.assertTrue(set(framebuffer) <= set(range(16)))
        self.assertGreater(len(set(framebuffer)), 4)

    def test_pausing_mid_frame_is_identical(self) -> None:
        for limit in (50_000, 123_457):
            with self.subTest(limit=limit):
                _, plain = play(2, accelerate=False, max_instructions=limit)
                _, fast = play(2, accelerate=True, max_instructions=limit)
                self.assertEqual(plain, fast)
                self.assertEqual(plain[0].state, "PAUSED")

    def test_framebuffer_is_deterministic(self) -> None:
        digests = set()
        for accelerate in (False, True):
            vm, _ = play(1, accelerate=accelerate)
            digests.add(hashlib.sha256(repr(vm.memory_slice(FB, PIXELS)).encode()).hexdigest())
        self.assertEqual(len(digests), 1)


if __name__ == "__main__":
    unittest.main()
