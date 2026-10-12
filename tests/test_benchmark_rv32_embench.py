"""Anclas de Embench 1.0 sobre el hart: binarios versionados con su sha256, un benchmark pequeño corre y
verifica en los tres modos de E3 con el mismo recuento, y el intérprete y los bloques coinciden."""

from __future__ import annotations

import hashlib
import importlib.util
import unittest
from pathlib import Path

from cpu_digital.rv32_hart import Stop
from cpu_digital.rv32_platform import Machine

ROOT = Path(__file__).resolve().parents[1]
BIN_DIR = ROOT / "benchmarks" / "rv32_arranque" / "bin"
DRIVER = ROOT / "benchmarks" / "benchmark_rv32_embench.py"


def load_driver():
    spec = importlib.util.spec_from_file_location("benchmark_rv32_embench", DRIVER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class EmbenchBinaries(unittest.TestCase):
    def test_versioned_binaries_match_their_digests(self) -> None:
        driver = load_driver()
        binaries = driver.versioned()
        self.assertEqual(sorted(binaries), ["aha-mont64", "crc32", "edn", "huffbench", "matmult-int", "nettle-aes",
                                            "nettle-sha256", "nsichneu", "picojpeg", "qrduino", "sglib-combined", "slre",
                                            "statemate", "ud"])
        for path in binaries.values():
            self.assertTrue(path.is_file())
            self.assertLess(path.stat().st_size, 64 * 1024)
        sums = (BIN_DIR / "SHA256SUMS").read_text(encoding="utf-8")
        for name, path in binaries.items():
            self.assertIn(f"{hashlib.sha256(path.read_bytes()).hexdigest()} *{path.name}", sums, name)

    def test_statemate_runs_and_verifies_in_the_three_modes(self) -> None:
        driver = load_driver()
        image = driver.versioned()["statemate"].read_bytes()
        results = []
        for accelerate, horizon in ((False, True), (True, False), (True, True)):
            machine = Machine(ram_bytes=1 << 20)
            self.addCleanup(machine.close)
            machine.load_flat(image)
            machine.hart.accelerate = accelerate
            machine.hart.horizon = horizon
            stop = machine.run(50_000_000)
            text = machine.uart_text
            self.assertEqual(stop, Stop("apagado"), (accelerate, horizon))
            self.assertIn("ticks=", text)
            self.assertTrue(text.rstrip().endswith("V0"), text[-40:])   # verify_benchmark acepta el resultado
            ticks = int(text.split("ticks=")[1].split()[0])
            results.append((machine.hart.instret, ticks))
            if accelerate:
                self.assertGreater(machine.hart.block_instructions / machine.hart.instret, 0.9)
        self.assertEqual(len(set(results)), 1, results)   # mismo recuento e idénticos ticks en los tres modos
        self.assertEqual(results[0], (1_517_030, 1_512_318))   # recuento determinista del binario versionado


if __name__ == "__main__":
    unittest.main()
