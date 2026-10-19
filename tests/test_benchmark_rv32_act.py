"""Anclas de H2.4: los binarios versionados de riscv-arch-test 2.7.4 (rv32i_m/I) coinciden con su sha256, producen la
firma de referencia enteros (apagado por SYSCON sin salida) y fragmentados con 16 cortes por plan no divergen; la
lectura de firmas y su forma canónica."""

from __future__ import annotations

import random
import unittest

from benchmarks.benchmark_rv32_act import (
    ACT_TAG,
    ACT_ZIP_SHA256,
    ARCH,
    BIN_DIR,
    PREFIX,
    SIGNATURES,
    SUMS,
    canonical_reference,
    reference_digest,
    run_fragmented,
    run_whole,
    signature_of,
    versioned,
)
from cpu_digital.rv32_platform import Machine
from tests.test_rv32_hart import BASE, RAM


class SignatureFormat(unittest.TestCase):
    def test_canonical_reference_and_digest(self) -> None:
        text = "DEADBEEF\n  00000001 \n\n0003fffe\n"
        self.assertEqual(canonical_reference(text), ["deadbeef", "00000001", "0003fffe"])
        self.assertEqual(reference_digest(canonical_reference(text)), reference_digest(["deadbeef", "00000001", "0003fffe"]))
        self.assertNotEqual(reference_digest(["deadbeef"]), reference_digest(["deadbeee"]))
        self.assertEqual(len(ACT_ZIP_SHA256), 64)
        self.assertEqual(ACT_TAG, "2.7.4")

    def test_signature_of_reads_little_endian_words(self) -> None:
        machine = Machine(ram_bytes=RAM)
        self.addCleanup(machine.close)
        machine.memory.store32(BASE + 0x1000, 0x11223344)
        machine.memory.store32(BASE + 0x1004, 0xDEADBEEF)
        self.assertEqual(signature_of(machine, BASE + 0x1000, BASE + 0x1008), ["11223344", "deadbeef"])
        self.assertEqual(signature_of(machine, BASE + 0x1000, BASE + 0x1000), [])


class VersionedBattery(unittest.TestCase):
    def test_versioned_binaries_match_their_sha256_signatures_whole_and_fragmented(self) -> None:
        if not SUMS.is_file() or PREFIX not in SUMS.read_text(encoding="utf-8") or not SIGNATURES.is_file():
            self.skipTest("sin binarios de riscv-arch-test versionados")
        binaries = versioned()
        self.assertGreaterEqual(len(binaries), 38)
        rng = random.Random(3)
        for name, (path, begin, end, reference) in sorted(binaries.items()):
            with self.subTest(test=name):
                self.assertTrue(path.name.startswith(PREFIX) and path.name.endswith(f"_{ARCH}.bin"))
                self.assertEqual(path.parent, BIN_DIR)
                self.assertLess(begin, end)
                image = path.read_bytes()
                whole = run_whole(image, begin, end, reference)
                self.assertTrue(whole["apagado"], whole)
                self.assertTrue(whole["firma_igual"], whole)
                fragmented = run_fragmented(name, image, 16, rng)
                self.assertEqual(fragmented["divergencias"], 0, fragmented)
                self.assertEqual(fragmented["parada"], "apagado")


if __name__ == "__main__":
    unittest.main()
