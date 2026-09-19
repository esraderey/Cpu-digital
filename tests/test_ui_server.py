from __future__ import annotations

import unittest

from cpu_digital.ui_server import ConsoleSession, UI_DIR


class ConsoleSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = ConsoleSession()

    def test_bootstrap_exposes_console_capabilities(self) -> None:
        payload = self.session.bootstrap(memory_count=32)

        self.assertTrue(payload["ok"])
        self.assertGreaterEqual(len(payload["demos"]), 7)
        self.assertGreaterEqual(len(payload["isa"]), 40)
        self.assertEqual(payload["state"]["state"], "READY")
        self.assertEqual(payload["state"]["memory"]["count"], 32)
        self.assertIn("stateDiagram", payload["lifecycle"])

    def test_load_step_and_run_return_observable_state(self) -> None:
        source = """.code
.entry _start
_start:
    MOVI R1, 41
    ADDI R1, R1, 1
    SYSCALL 1
    HALT
"""
        loaded = self.session.load({"source": source, "inputs": []})
        stepped = self.session.action({"action": "step"})
        finished = self.session.action({"action": "run", "max_instructions": 100})

        self.assertEqual(loaded["state"]["state"], "READY")
        self.assertEqual(stepped["state"]["registers"][1], 41)
        self.assertEqual(finished["state"]["state"], "HALTED")
        self.assertEqual(finished["state"]["output"], [42])
        self.assertGreaterEqual(len(finished["state"]["trace"]), 4)

    def test_symbolic_breakpoint_and_snapshot_round_trip(self) -> None:
        source = """.code
.entry _start
_start:
    MOVI R1, 1
LOOP:
    ADDI R1, R1, 1
    HALT
"""
        self.session.load({"source": source})
        paused = self.session.action({"action": "run", "breakpoints": ["LOOP"], "max_instructions": 100})
        snapshot = self.session.snapshot_bytes()
        self.session.action({"action": "run", "max_instructions": 100})
        restored = self.session.restore(snapshot)

        self.assertEqual(paused["state"]["state"], "PAUSED")
        self.assertIn("Breakpoint", paused["state"]["reason"])
        self.assertEqual(restored["state"]["state"], "PAUSED")
        self.assertEqual(restored["state"]["registers"][1], 1)

    def test_static_console_assets_are_packaged(self) -> None:
        for name in ("index.html", "styles.css", "app.js", "og.png"):
            path = UI_DIR / name
            self.assertTrue(path.is_file(), name)
            self.assertGreater(path.stat().st_size, 100, name)


if __name__ == "__main__":
    unittest.main()
