"""Pruebas de ``PersistentTuringMachine`` sobre un chip en un directorio temporal."""

from __future__ import annotations

import contextlib
import importlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

from cpu_digital.memory_chip import NonVolatileMemoryChip
from cpu_digital.turing_machine import Direction, PersistentTuringMachine, StepResult

CAPACITY = 4_096
PAGE = 256

# Incrementador binario: (estado, leído) -> (estado, escribir, dirección)
INCREMENTER = {
    ("q_seek_end", "0"): ("q_seek_end", "0", "R"),
    ("q_seek_end", "1"): ("q_seek_end", "1", "R"),
    ("q_seek_end", "_"): ("q_add_carry", "_", "L"),
    ("q_add_carry", "1"): ("q_add_carry", "0", "L"),
    ("q_add_carry", "0"): ("q_halt", "1", "S"),
    ("q_add_carry", "_"): ("q_halt", "1", "S"),
}


class PersistentTuringMachineTests(unittest.TestCase):
    """Cinta bilateral centrada en el chip, un paso, ejecución con límite y persistencia."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="turing_test_")
        self.addCleanup(self.tmp.cleanup)
        self.chip_path = Path(self.tmp.name) / "cinta.sqlite"
        self.chip = NonVolatileMemoryChip(self.chip_path, capacity_bytes=CAPACITY, page_bytes=PAGE)
        self.addCleanup(self.chip.close)

    def machine(self, transitions=INCREMENTER, initial_state="q_seek_end", accept_states=frozenset({"q_halt"})):
        return PersistentTuringMachine(
            self.chip,
            transitions=transitions,
            initial_state=initial_state,
            accept_states=accept_states,
        )

    def test_load_input_and_window(self) -> None:
        tm = self.machine()
        tm.load_input("1011")
        self.assertEqual(tm.read_window(-1, 4), "_1011_")
        self.assertEqual(tm.read_cell(), "1")
        self.assertEqual(tm.read_cell(3), "1")
        self.assertEqual(tm.read_window(3, 1), "")
        # La celda lógica 0 vive en el centro del chip; el blanco es el byte 0.
        origin = CAPACITY // 2
        self.assertEqual(self.chip.read(origin, 4), b"1011")
        self.assertEqual(self.chip.read(origin - 1, 1), b"\x00")

    def test_step_follows_transition(self) -> None:
        tm = self.machine()
        tm.load_input("1011")
        result = tm.step()
        self.assertEqual(
            result,
            StepResult(
                step_number=1,
                previous_state="q_seek_end",
                read_symbol="1",
                written_symbol="1",
                direction=Direction.RIGHT,
                next_state="q_seek_end",
                head_position=1,
                halted=False,
            ),
        )
        self.assertEqual((tm.state, tm.head_position, tm.steps), ("q_seek_end", 1, 1))
        self.assertFalse(tm.is_halted)

    def test_run_increments_binary_number(self) -> None:
        tm = self.machine()
        tm.load_input("1011")
        result = tm.run()
        self.assertTrue(result.halted)
        self.assertTrue(tm.is_accepted)
        self.assertEqual(tm.state, "q_halt")
        self.assertEqual(tm.steps, 8)
        self.assertEqual(tm.head_position, 1)
        self.assertEqual(tm.read_window(-1, 4), "_1100_")

    def test_run_overflow_grows_tape_to_the_left(self) -> None:
        tm = self.machine()
        tm.load_input("111")
        tm.run()
        self.assertTrue(tm.is_accepted)
        self.assertEqual(tm.head_position, -1)
        self.assertEqual(tm.read_window(-2, 3), "_1000_")

    def test_run_respects_step_limit(self) -> None:
        tm = self.machine({("q0", "_"): ("q0", "_", "R")}, initial_state="q0", accept_states=frozenset())
        with self.assertRaises(RuntimeError):
            tm.run(max_steps=5)
        self.assertEqual((tm.steps, tm.head_position), (5, 5))
        with self.assertRaises(ValueError):
            tm.run(max_steps=0)

    def test_missing_transition_rejects_and_halted_machine_stays_put(self) -> None:
        tm = self.machine({("q0", "1"): ("q0", "1", "R")}, initial_state="q0", accept_states=frozenset())
        tm.load_input("1")
        tm.step()  # avanza sobre el único «1»
        result = tm.step()  # lee «_» sin transición definida
        self.assertTrue(result.halted)
        self.assertEqual(tm.state, "HALT_REJECT")
        self.assertTrue(tm.is_halted)
        self.assertFalse(tm.is_accepted)
        self.assertEqual(tm.steps, 1)
        again = tm.step()
        self.assertTrue(again.halted)
        self.assertEqual((again.step_number, again.head_position, tm.steps), (1, 1, 1))

    def test_tape_persists_across_reopen(self) -> None:
        tm = self.machine()
        tm.load_input("1011")
        tm.run()
        self.chip.close()
        # Se reabre con el constructor: ``open()`` deja sin cerrar su conexión de sondeo y en
        # Windows el archivo queda bloqueado hasta que el recolector de ciclos la libera.
        with NonVolatileMemoryChip(self.chip_path, capacity_bytes=CAPACITY, page_bytes=PAGE) as chip:
            reopened = PersistentTuringMachine(
                chip, transitions=INCREMENTER, initial_state="q_seek_end", accept_states={"q_halt"}
            )
            self.assertEqual(reopened.read_window(-1, 4), "_1100_")
            reopened.run()
            self.assertEqual(reopened.read_window(-1, 4), "_1101_")


class EjemploTuringTests(unittest.TestCase):
    """El ejemplo se importa sin efectos y escribe la cinta donde se le indica."""

    def test_import_has_no_side_effects(self) -> None:
        with tempfile.TemporaryDirectory(prefix="turing_ejemplo_") as folder:
            previous = os.getcwd()
            os.chdir(folder)
            try:
                sys.modules.pop("cpu_digital.ejemplo_turing", None)
                module = importlib.import_module("cpu_digital.ejemplo_turing")
            finally:
                os.chdir(previous)
            self.assertEqual(os.listdir(folder), [])
        self.assertTrue(callable(module.main))

    def test_main_writes_the_chip_at_the_given_path(self) -> None:
        from cpu_digital import ejemplo_turing

        with tempfile.TemporaryDirectory(prefix="turing_ejemplo_") as folder:
            path = Path(folder) / "cinta.sqlite"
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(ejemplo_turing.main([str(path)]), 0)
                self.assertEqual(ejemplo_turing.main([]), 0)
            self.assertTrue(path.is_file())
            with NonVolatileMemoryChip(path) as chip:
                self.assertEqual(chip.read(chip.capacity_bytes // 2, 4), b"1100")
        self.assertIn("Cinta final   : _1100_", out.getvalue())
        self.assertIn("Estado final  : q_halt (Aceptado: True)", out.getvalue())


if __name__ == "__main__":
    unittest.main()
