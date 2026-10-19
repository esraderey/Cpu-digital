"""Anclas de E5 (H2.3): el driver de riscv-tests (veredictos, ejecución entera y fragmentada) con imágenes sintéticas
y, si hay binarios versionados, la batería rv32ui con sus sha256; y el corredor de mutantes (anclas que coinciden con
el código real, árbol temporal con un mutante aplicado y restaurado)."""

from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path

from benchmarks.benchmark_rv32_mutantes import MUTANTS, NO_BUMPS, apply, make_tree
from benchmarks.benchmark_rv32_riscv_tests import ARCH, BIN_DIR, PREFIX, SUMS, TESTS, run_fragmented, run_whole, verdict, versioned
from cpu_digital.rv32_hart import Stop
from cpu_digital.rv32_isa import Kind, encode
from tests.test_rv32_hart import assemble

ROOT = Path(__file__).resolve().parents[1]
SYSCON = 0x11100000
UART = 0x10000000
LOOP = [encode(Kind.ADDI, 1, 1, imm=1), encode(Kind.ADDI, 20, 20, imm=-1), encode(Kind.BNE, 0, 20, 0, imm=-8)]
PASSING = [encode(Kind.ADDI, 20, 0, imm=30)] + LOOP + [
    encode(Kind.LUI, 30, imm=SYSCON), encode(Kind.ADDI, 31, 0, imm=0x555), encode(Kind.SLLI, 31, 31, imm=4),
    encode(Kind.ADDI, 31, 31, imm=5), encode(Kind.SW, 0, 30, 31, imm=0), encode(Kind.JAL, 0, imm=0)]
FAILING = [encode(Kind.ADDI, 20, 0, imm=30), encode(Kind.ADDI, 3, 0, imm=7)] + LOOP + [
    encode(Kind.LUI, 30, imm=UART), encode(Kind.SB, 0, 30, 3, imm=0),
    encode(Kind.LUI, 30, imm=SYSCON), encode(Kind.ADDI, 31, 0, imm=0x777), encode(Kind.SLLI, 31, 31, imm=4),
    encode(Kind.ADDI, 31, 31, imm=7), encode(Kind.SW, 0, 30, 31, imm=0), encode(Kind.JAL, 0, imm=0)]


class Verdicts(unittest.TestCase):
    def test_verdict_classifies_the_stops(self) -> None:
        self.assertEqual(verdict(Stop("apagado"), b""), ("pass", None))
        self.assertEqual(verdict(Stop("reinicio"), b"\x07"), ("fail", 7))
        self.assertEqual(verdict(Stop("reinicio"), b"\x02\x0b"), ("fail", 11))
        self.assertEqual(verdict(Stop("apagado"), b"x")[0][:6], "parada")        # apagado con salida: no es un paso limpio
        self.assertEqual(verdict(Stop("reinicio"), b"")[0][:6], "parada")        # reinicio sin número: no es un fallo válido
        self.assertEqual(verdict(Stop("gas"), b""), ("parada gas", None))
        self.assertEqual(verdict(Stop("fallo", 2, 0x13), b""), ("parada fallo causa 2 valor 0x13", None))

    def test_synthetic_pass_and_fail_images_whole_and_fragmented(self) -> None:
        for name, words, outcome, number, stop in (("pasa", PASSING, "pass", None, "apagado"), ("falla", FAILING, "fail", 7, "reinicio")):
            with self.subTest(name=name):
                image = assemble(words)
                whole = run_whole(image)
                self.assertEqual((whole["veredicto"], whole["test_fallado"]), (outcome, number))
                fragmented = run_fragmented(name, image, 16, random.Random(1))
                self.assertEqual((fragmented["divergencias"], fragmented["parada"], fragmented["instrucciones"]), (0, stop, whole["instrucciones"]))
                for plan in fragmented["planes"].values():
                    self.assertEqual(plan["cortes"], 16)
                    for mode in plan["modos"].values():
                        self.assertEqual((mode["divergencias"], mode["cortes_alcanzados"]), (0, 16))

    def test_the_test_list_is_rv32ui(self) -> None:
        self.assertEqual(len(TESTS), 42)
        self.assertEqual(len(set(TESTS)), len(TESTS))
        for name in ("add", "fence_i", "ld_st", "ma_data", "simple", "st_ld", "xori"):
            self.assertIn(name, TESTS)


class VersionedBattery(unittest.TestCase):
    def test_versioned_binaries_match_their_sha256_and_pass_whole_and_fragmented(self) -> None:
        if not SUMS.is_file() or PREFIX not in SUMS.read_text(encoding="utf-8"):
            self.skipTest("sin binarios de riscv-tests versionados (H2.3 pendiente de la descarga)")
        binaries = versioned()
        self.assertGreater(len(binaries), 0)
        for name, path in sorted(binaries.items()):
            with self.subTest(test=name):
                self.assertTrue(path.name.startswith(PREFIX) and path.name.endswith(f"_{ARCH}.bin"))
                self.assertEqual(path.parent, BIN_DIR)
                image = path.read_bytes()
                whole = run_whole(image)
                self.assertEqual(whole["veredicto"], "pass", whole)
                fragmented = run_fragmented(name, image, 16, random.Random(3))
                self.assertEqual(fragmented["divergencias"], 0, fragmented)


class MutantRunner(unittest.TestCase):
    def test_every_mutant_anchor_matches_the_code_exactly_once(self) -> None:
        for name, edits in MUTANTS.items():
            for relative, old, new in edits:
                with self.subTest(mutant=name, file=relative):
                    text = (ROOT / relative).read_text(encoding="utf-8")
                    self.assertEqual(text.count(old), 1)
                    self.assertNotEqual(old, new)
                    self.assertIn("MUTANTE", new)
        self.assertEqual(len(NO_BUMPS), 3)
        self.assertGreaterEqual(len(MUTANTS), 20)

    def test_a_mutant_is_applied_in_a_temporary_tree_and_the_real_tree_is_untouched(self) -> None:
        before = (ROOT / "cpu_digital" / "rv32_hart.py").read_bytes()
        with tempfile.TemporaryDirectory(prefix="rv32_mut_test_") as temporary:
            tree = Path(temporary) / "arbol"
            make_tree(tree)
            for folder in ("cpu_digital", "benchmarks", "tests"):
                self.assertTrue((tree / folder).is_dir())
            self.assertFalse(list(tree.rglob("__pycache__")))
            apply(tree, MUTANTS["pc_sin_restaurar"])
            mutated = (tree / "cpu_digital" / "rv32_hart.py").read_text(encoding="utf-8")
            self.assertIn("MUTANTE: pc sin restaurar", mutated)
            with self.assertRaises(SystemExit):
                apply(tree, MUTANTS["pc_sin_restaurar"])                 # el ancla ya no existe: no se aplica dos veces
        self.assertEqual((ROOT / "cpu_digital" / "rv32_hart.py").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
