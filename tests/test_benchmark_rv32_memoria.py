"""Anclas del microbanco E2 de RFC-EXP-00018: la capa de bytes frente al modelo y los umbrales."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

from cpu_digital.memory import PagedMemory


def _load_benchmark():
    # benchmarks/ no es un paquete instalado: el script se carga por su ruta.
    path = Path(__file__).resolve().parents[1] / "benchmarks" / "benchmark_rv32_memoria.py"
    spec = importlib.util.spec_from_file_location("benchmark_rv32_memoria", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


E2 = _load_benchmark()


def _access(**ns: float) -> list[dict[str, object]]:
    base = {
        "vacío_carga": 10.0, "vacío_escritura": 12.0, "lw": 30.0, "lw_paged": 60.0, "lw_unaligned": 50.0,
        "lb": 25.0, "lbu": 24.0, "lb_shift": 35.0, "lbu_shift": 33.0, "lh": 36.0, "lhu": 34.0,
        "sw": 32.0, "sw_paged": 70.0, "sb": 28.0, "sb_shift": 40.0, "sh": 38.0, "sw_ring": 45.0,
        "sb_ring": 40.0, "lw_repetido": 30.0,
    }
    base.update(ns)
    return [{"op": name, "ns_min": value} for name, value in base.items()]


def _programs(stores: float = 1.1, undo: float = 1.2) -> list[dict[str, object]]:
    rows = []
    for name, plain in (("memcpy_w", 100.0), ("memcpy_b", 90.0), ("strcpy", 95.0)):
        for variant, factor in (("sin anillo", 1.0), ("anillo: escrituras", stores), ("anillo: instrucciones", undo)):
            rows.append({"programa": name, "variante": variant, "ns_instr_min": plain * factor, "s_min": plain * factor})
    rows.append({"programa": "memcpy_w", "variante": "sin anillo (repetido)", "ns_instr_min": 101.0, "s_min": 101.0})
    return rows


class LaneMemoryOracleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lanes = E2.LaneMemory(PagedMemory(E2.MEMORY_WORDS))
        self.region = E2.BASE + (1 << 16)
        self.lanes.materialize(self.region, E2.REGION_BYTES)

    def test_matches_bytearray_model(self) -> None:
        self.assertGreater(E2.verify(self.lanes, self.region, E2.REGION_BYTES), 50_000)

    def test_page_crossing_halfword_and_word(self) -> None:
        # Último byte de la primera página de la región y primero de la siguiente.
        boundary = self.region + (E2.PAGE_MASK + 1)
        self.lanes.sb(boundary - 1, 0x80)
        self.lanes.sb(boundary, 0xFF)
        self.assertEqual(self.lanes.lhu(boundary - 1), 0xFF80)
        self.assertEqual(self.lanes.lh(boundary - 1), -128)
        self.lanes.sh(boundary - 1, 0x1234)
        self.assertEqual((self.lanes.lbu(boundary - 1), self.lanes.lbu(boundary)), (0x34, 0x12))
        self.lanes.sw(boundary - 4, 0x11223344)
        self.lanes.sw(boundary, 0x55667788)
        self.assertEqual(self.lanes.lw_unaligned(boundary - 2), 0x77881122)

    def test_sign_extension_and_masks(self) -> None:
        addr = self.region + 7
        self.lanes.sb(addr, -1)
        self.assertEqual((self.lanes.lbu(addr), self.lanes.lb(addr)), (0xFF, -1))
        self.lanes.sb_shift(addr, 0x7F)
        self.assertEqual((self.lanes.lbu_shift(addr), self.lanes.lb_shift(addr)), (0x7F, 127))
        self.lanes.sw(self.region + 8, -2)
        self.assertEqual(self.lanes.lw(self.region + 8), 0xFFFFFFFE)
        self.assertEqual(self.lanes.memory[(self.region + 8 - E2.BASE) >> 2], -2)

    def test_faults(self) -> None:
        for addr in (E2.BASE - 4, self.lanes.limit, self.lanes.limit + 4):
            with self.assertRaises(E2.AccessFault):
                self.lanes.lw(addr)
            with self.assertRaises(E2.AccessFault):
                self.lanes.sb(addr, 1)
        with self.assertRaises(E2.AccessFault):
            self.lanes.lw(self.region + 2)
        with self.assertRaises(E2.AccessFault):
            self.lanes.lhu(self.lanes.limit - 1)

    def test_ring_keeps_previous_value(self) -> None:
        self.lanes.sw(self.region, 0xCAFEBABE)
        self.lanes.ring.clear()
        self.lanes.sw_ring(self.region, 1)
        self.lanes.sb_undo(77, self.region + 1, 2)
        self.assertEqual(list(self.lanes.ring), [(self.region, 0xCAFEBABE), (77, self.region + 1, 0x00)])

    def test_materialize_keeps_data(self) -> None:
        self.lanes.sw(self.region, 0xDEADBEEF)
        self.lanes.materialize(self.region, 16)
        self.assertEqual(self.lanes.lw(self.region), 0xDEADBEEF)

    def test_constructor_guard_and_empty_methods(self) -> None:
        with self.assertRaises(ValueError):
            E2.LaneMemory(PagedMemory(E2.MEMORY_WORDS, page_words=2048))
        self.assertIsNone(self.lanes.nothing_load(self.region))
        self.assertIsNone(self.lanes.nothing_store(self.region, 1))
        self.assertEqual(len(self.lanes.ring), 0)


class VerdictThresholdTests(unittest.TestCase):
    def test_exact_thresholds_survive(self) -> None:
        # Neto exacto 1,5: lb = vacío + 1,5·(lw − vacío) = 10 + 30 = 40; sb_shift = 12 + 1,5·20 = 42.
        result = E2.verdicts(_access(lb=40.0, sb_shift=42.0), _programs(undo=1.25))
        self.assertEqual(result["peor_vista_neto"], E2.LANE_THRESHOLD)
        self.assertTrue(result["p1_1_vista_sobrevive"])
        self.assertEqual(result["peor_desplazamiento_neto"], E2.LANE_THRESHOLD)
        self.assertTrue(result["p1_1_desplazamiento_sobrevive"])
        self.assertAlmostEqual(result["peor_anillo_instrucciones"], E2.RING_THRESHOLD)
        self.assertTrue(result["anillo_deshacer_se_conserva"])
        self.assertTrue(result["anillo_deshacer_en_el_umbral"])
        self.assertAlmostEqual(result["deriva_programas"], 0.01)

    def test_above_thresholds_kill(self) -> None:
        result = E2.verdicts(_access(sb=42.5), _programs(undo=1.2501))
        self.assertFalse(result["p1_1_vista_sobrevive"])
        self.assertTrue(result["p1_1_desplazamiento_sobrevive"])
        self.assertFalse(result["anillo_deshacer_se_conserva"])
        self.assertTrue(result["anillo_escrituras_se_conserva"])

    def test_degenerate_floor_is_infinite(self) -> None:
        result = E2.verdicts(_access(lw=10.0), _programs())
        self.assertEqual(result["carriles"]["lb/lw neto"], float("inf"))
        self.assertFalse(result["p1_1_vista_sobrevive"])


if __name__ == "__main__":
    unittest.main()
