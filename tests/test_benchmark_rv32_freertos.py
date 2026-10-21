"""Anclas de FreeRTOS en el hart (H3.5): el binario versionado del puerto propio arranca, intercambia los 20 mensajes con
sus tics, duerme con WFI en el gancho ocioso y apaga; las cifras son las mismas en los tres modos y E4 no diverge."""

from __future__ import annotations

import random
import unittest

import benchmarks.benchmark_rv32_freertos as F
from benchmarks.benchmark_rv32_invariante import verify_program

EXPECTED = ("FreeRTOS V11.1.0 en el hart RV32\n" + "".join(f"rx {n} tic {3 * (n - 1)}\n" for n in range(1, 21))
            + "FreeRTOS ok suma 210\n")


class FreeRtosDemo(unittest.TestCase):
    def test_the_versioned_binary_matches_its_checksum(self) -> None:
        self.assertTrue(F.versioned_ok())

    def test_the_demo_runs_the_same_in_every_mode(self) -> None:
        image = F.BINARY.read_bytes()
        seen = set()
        for name, (accelerate, warmup) in F.MODES.items():
            with self.subTest(mode=name):
                result = F.run_demo(image, accelerate, warmup)
                self.assertEqual(result["parada"], "apagado")
                self.assertEqual(result["uart"], EXPECTED)
                self.assertTrue(result["uart"].endswith(F.EXPECTED_END))        # la constante con la que el arnés juzga la salida
                self.assertGreater(result["instret"], result["gas"])          # el gancho ocioso durmió con WFI
                if accelerate:
                    self.assertGreater(result["cobertura"], 0.5)
                seen.add((result["gas"], result["instret"], result["primer_byte_instret"]))
        self.assertEqual(seen, {(44_120, 68_388, 4_663)})

    def test_e4_with_uniform_and_biased_cuts(self) -> None:
        image = F.BINARY.read_bytes()
        result = verify_program("freertos", image, F.RAM, 45_000, 16, random.Random(5), None, step=1)
        self.assertEqual(result["divergencias"], 0, result)
        self.assertTrue({"csr", "wfi"} <= {kind for classes in result["clases_de_ventana"] for kind in classes.split("+")})


if __name__ == "__main__":
    unittest.main()
