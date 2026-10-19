"""Anclas de la invariante S bajo cortes (H2.2, E4): el arnés de ``benchmarks/benchmark_rv32_invariante.py`` sobre
programas aleatorios y hostiles, los programas ancla de las revisiones ciegas con todos sus cortes, un benchmark de
Embench y CoreMark, con cortes uniformes y sesgados, intérprete y bloques (``warmup`` 1 y 2), destino nuevo y usado."""

from __future__ import annotations

import random
import time
import unittest
from pathlib import Path
from unittest import mock

import benchmarks.benchmark_rv32_invariante as B
from benchmarks.benchmark_rv32_invariante import (
    COREMARK,
    FEED,
    MODES,
    SMALL_RAM,
    anchor_corpus,
    derived_errors,
    new_machine,
    pages_round_trip,
    plan_cuts,
    profile,
    random_corpus,
    run_chained,
    run_chunked,
    verify_program,
)
from cpu_digital.rv32_hart import Rv32Hart, decode_snapshot
from cpu_digital.rv32_isa import Kind, encode
from cpu_digital.rv32_mem import Rv32Memory
from cpu_digital.rv32_platform import CLINT_BASE, CLINT_MTIMECMP, Machine
from tests.test_rv32_blocks import SelfModifyingCode
from tests.test_rv32_hart import CODE, assemble

ROOT = Path(__file__).resolve().parents[1]
EMBENCH_STATEMATE = ROOT / "benchmarks" / "rv32_arranque" / "bin" / "embench_statemate_rv32i_zicsr.bin"
EBREAK = encode(Kind.EBREAK)


def anchors() -> list[tuple[str, bytes, dict[int, int]]]:
    """Los programas ancla del arnés (``anchor_corpus``, en el benchmark) como (nombre, imagen, registros)."""
    return [(name, image, regs) for name, image, _, _, regs, _, _ in anchor_corpus()]


class RandomProgramsUnderCuts(unittest.TestCase):
    def test_random_and_hostile_programs_with_uniform_and_biased_cuts(self) -> None:
        rng = random.Random(20261019)
        totals = {"ventanas": 0, "sesgados_en_ventana": 0, "uniformes_en_ventana": 0, "cortes": 0, "lentos": 0, "codigo": 0,
                  "sin_cortes": 0}
        for name, image, ram, gas, regs, feed, divisor in random_corpus(48, rng):
            result = verify_program(name, image, ram, gas, 12, rng, regs, step=1, bytes_every=3, check_every=1, feed=feed,
                                    time_divisor=divisor)
            with self.subTest(program=name):
                self.assertEqual(result["divergencias"], 0, result)
                for plan in result["planes"].values():
                    self.assertTrue(plan["troceado_sin_restaurar_igual"])
            totals["ventanas"] += int(result["ventanas"])
            totals["sesgados_en_ventana"] += int(result["planes"]["sesgados"]["cortes_en_ventana"])
            totals["uniformes_en_ventana"] += int(result["planes"]["uniformes"]["cortes_en_ventana"])
            totals["cortes"] += int(result["planes"]["sesgados"]["cortes"])
            totals["lentos"] += any("lento" in kind for kind in result["clases_de_ventana"])
            totals["codigo"] += any("código" in kind for kind in result["clases_de_ventana"])
            totals["sin_cortes"] += not result["planes"]["uniformes"]["cortes"]         # hostiles que mueren en la 1.ª instrucción
        self.assertGreater(totals["cortes"], 300, totals)
        self.assertGreater(totals["ventanas"], 100, totals)
        self.assertGreater(totals["lentos"], 10, totals)                                  # hubo eventos que sesgar
        self.assertGreater(totals["codigo"], 5, totals)                                   # y stores sobre código
        self.assertLessEqual(totals["sin_cortes"], 2, totals)                             # casi todos los programas tienen cortes
        self.assertGreaterEqual(totals["sesgados_en_ventana"], totals["uniformes_en_ventana"], totals)


class AnchorProgramsUnderEveryCut(unittest.TestCase):
    def test_every_cut_of_the_anchor_programs(self) -> None:
        rng = random.Random(3)
        for name, image, _, _, regs, _, divisor in anchor_corpus():
            machine = new_machine(image, SMALL_RAM, False, 2, regs, FEED, divisor)
            try:
                stop = machine.run(2000)
                total = machine.hart.instret
            finally:
                machine.close()
            self.assertEqual(stop.reason, "ebreak", name)
            self.assertLess(total, 300, name)
            with self.subTest(program=name, instrucciones=total):
                # Todos los cortes posibles (plan_cuts con count = total − 1 los devuelve todos) en los tres modos.
                result = verify_program(name, image, SMALL_RAM, 2000, total - 1, rng, regs, step=1, bytes_every=2,
                                        check_every=1, feed=FEED, time_divisor=divisor)
                self.assertEqual(result["divergencias"], 0, result)
                self.assertEqual(result["planes"]["uniformes"]["cortes"], total - 1)
                for mode_name, _, _ in MODES:
                    self.assertEqual(result["planes"]["uniformes"]["modos"][mode_name]["cortes_alcanzados"], total - 1)

    def test_profile_locates_code_stores_mmio_and_slow_exits(self) -> None:
        name, image, regs = anchors()[4]                                                # bucle que escribe sobre su línea
        profiled = profile(image, SMALL_RAM, 2000, regs, 1)
        kinds = {kind for _, _, kind in profiled["ventanas"]}
        self.assertTrue(any("código" in kind for kind in kinds), kinds)
        self.assertTrue(any("lento" in kind for kind in kinds), kinds)
        name, image, regs = anchors()[6]                                                # MMIO
        profiled = profile(image, SMALL_RAM, 2000, regs, 1, FEED)
        self.assertTrue(any("mmio" in kind for _, _, kind in profiled["ventanas"]), profiled["ventanas"])
        cuts = plan_cuts(int(profiled["instrucciones"]), 10, random.Random(1), profiled["ventanas"])
        self.assertEqual(len(cuts), 10)
        self.assertTrue(all(any(start - 4 <= cut <= end + 4 for start, end, _ in profiled["ventanas"]) for cut in cuts), cuts)

    def test_derived_errors_detect_a_stale_block_and_an_unwritten_page(self) -> None:
        name, image, regs = anchors()[5]
        machine = new_machine(image, SMALL_RAM, True, 1, regs)
        try:
            self.assertEqual(machine.run(2000).reason, "ebreak")
            self.assertEqual(derived_errors(machine), [])
            block = next(block for block in machine.hart.blocks.values() if block is not None)
            machine.memory.words_u[(block.pc - CODE) >> 2] = encode(Kind.ADDI, 7, 7, imm=2)   # escritura sin marcar: obsoleto
            errors = derived_errors(machine)
            self.assertTrue(any("obsoleto" in error for error in errors), errors)
            machine.memory.lanes[0x3000] = 1                                                    # byte sin página escrita
            self.assertTrue(any("sin marcar como escrita" in error for error in derived_errors(machine)))
        finally:
            machine.close()


class HarnessSensitivity(unittest.TestCase):
    """El arnés no es ciego: con el núcleo roto por ``mock.patch`` (restauración sin ``reset`` de la RAM, ``instret``
    desplazado, divisor de tiempo no adoptado, ``mtimecmp`` no restaurado, caché de bloques viva con generaciones
    quietas) ``verify_program`` informa de divergencias; sin romper nada, no."""

    def verify(self, index: int, cuts: int = 24) -> dict[str, object]:
        name, image, regs = anchors()[index]
        return verify_program(name, image, SMALL_RAM, 2000, cuts, random.Random(9), regs, step=1, bytes_every=3, check_every=2, feed=FEED)

    def test_the_intact_core_has_no_divergences(self) -> None:
        for index in (0, 4, 6):
            self.assertEqual(self.verify(index)["divergencias"], 0)

    def test_a_restore_that_does_not_reset_the_ram_is_seen_by_the_finished_probe(self) -> None:
        with mock.patch.object(Rv32Memory, "reset", lambda self: None):
            result = self.verify(4)
        self.assertGreater(result["divergencias"], 0)
        motives = {d["motivo"] for plan in result["planes"].values() for mode in plan["modos"].values() for d in mode["divergencias"]}
        self.assertTrue(any("programa entero" in motive for motive in motives), motives)

    def test_a_field_restored_wrongly_is_seen(self) -> None:
        original = Rv32Hart.restore

        def shifted(self, state):
            original(self, state)
            self.instret += 1

        with mock.patch.object(Rv32Hart, "restore", shifted):
            self.assertGreater(self.verify(0)["divergencias"], 0)

    def test_a_time_divisor_not_adopted_is_seen(self) -> None:
        original = Rv32Hart.restore

        def stubborn(self, state):
            divisor = self.time_divisor
            original(self, state)
            self.time_divisor = divisor

        name, image, _, _, regs, _, _ = anchor_corpus()[7]                       # CSR de tiempo con divisor
        with mock.patch.object(Rv32Hart, "restore", stubborn):
            result = verify_program(name, image, SMALL_RAM, 2000, 12, random.Random(9), regs, step=1, time_divisor=7)
        self.assertGreater(result["divergencias"], 0)
        self.assertEqual(verify_program(name, image, SMALL_RAM, 2000, 12, random.Random(9), regs, step=1, time_divisor=7)["divergencias"], 0)

    def test_a_device_field_not_restored_is_seen(self) -> None:
        original = Machine.restore

        def forgetful(self, state):
            original(self, state)
            self.clint.mtimecmp = 0

        with mock.patch.object(Machine, "restore", forgetful):
            self.assertGreater(self.verify(6)["divergencias"], 0)

    def test_live_block_caches_with_quiet_generations_are_seen(self) -> None:
        original_restore = Rv32Hart.restore

        def keeping(self, state):
            blocks = dict(self.blocks)
            original_restore(self, state)
            self.blocks = blocks                      # los bloques viejos vuelven: solo las generaciones los protegen

        def quiet(self):                              # y clear_code deja de subirlas
            self.code[:] = bytes(len(self.code))

        with mock.patch.object(Rv32Hart, "restore", keeping), mock.patch.object(Rv32Memory, "clear_code", quiet),                 mock.patch.object(Rv32Memory, "_host_write", lambda self, offset, length: None),                 mock.patch.object(Rv32Memory, "reset", lambda self: None):
            result = self.verify(4)
        self.assertGreater(result["divergencias"], 0)


class HarnessInternals(unittest.TestCase):
    """Anclas de los mutantes del propio arnés (revisión ciega del arnés): cortes no alcanzados, hashes del primer y
    del último corte, plan de cortes, destino usado y ruta por bytes, cronómetro, estadísticas por tramo, ventana
    simétrica y comprobaciones derivadas en cada corte."""

    LOOP = [encode(Kind.ADDI, 1, 1, imm=1), encode(Kind.ADDI, 20, 20, imm=-1), encode(Kind.BNE, 0, 20, 0, imm=-8), EBREAK]

    def setUp(self) -> None:
        self.image = assemble(self.LOOP)
        self.regs = {20: 40}                                      # 3 · 40 + 1 = 121 instrucciones

    def chunked(self, cuts, warmup: int = 1):
        return run_chunked(self.image, SMALL_RAM, 2000, self.regs, cuts, True, warmup)

    def test_cuts_beyond_the_end_are_not_counted_as_reached(self) -> None:
        cuts = [10, 50, 200, 300]
        chunked = self.chunked(cuts)
        self.assertEqual(len(chunked["hashes"]), 2)
        chained = run_chained(self.image, SMALL_RAM, 2000, self.regs, cuts, False, 2, chunked["hashes"])
        self.assertEqual((chained["cortes_alcanzados"], chained["divergencias"], chained["parada"].reason), (2, [], "ebreak"))

    def test_a_wrong_hash_at_the_first_or_last_cut_is_a_divergence(self) -> None:
        cuts = [5, 30, 60, 100]
        hashes = self.chunked(cuts)["hashes"]
        for index in (0, len(hashes) - 1):
            wrong = list(hashes)
            wrong[index] = "0" * 64
            chained = run_chained(self.image, SMALL_RAM, 2000, self.regs, cuts, True, 1, wrong)
            self.assertEqual([d["corte"] for d in chained["divergencias"]], [cuts[index]])
            self.assertIn("hash distinto", chained["divergencias"][0]["motivo"])

    def test_plan_cuts_are_distinct_sorted_in_range_and_near_the_windows(self) -> None:
        rng = random.Random(4)
        for total, count in ((2, 5), (17, 6), (121, 30), (121, 500), (5000, 64)):
            cuts = plan_cuts(total, count, rng)
            self.assertEqual((cuts, len(cuts)), (sorted(set(cuts)), min(count, total - 1)))
            self.assertTrue(all(1 <= cut < total for cut in cuts))
        windows = [(400, 402, "código"), (900, 901, "mmio")]
        cuts = plan_cuts(5000, 12, rng, windows)
        self.assertEqual(len(cuts), 12)
        near = sum(1 for cut in cuts if any(start - 4 <= cut <= end + 4 for start, end, _ in windows))
        self.assertGreaterEqual(near, 10)                      # casi todos en las ventanas, no repartidos por 5 000
        self.assertEqual(plan_cuts(1, 10, rng), [])

    def test_the_chain_restores_into_used_machines_and_through_the_bytes_route(self) -> None:
        cuts = list(range(5, 120, 5))                             # 23 cortes
        hashes = self.chunked(cuts)["hashes"]
        calls: list[tuple[str, bool]] = []                        # (ruta, ¿la máquina de destino ya había ejecutado algo?)
        original_restore = Machine.restore

        def restore(self, state):
            calls.append(("dict", self.hart.instret > 0))
            original_restore(self, state)

        def restore_bytes(self, data):
            calls.append(("bytes", self.hart.instret > 0))
            original_restore(self, decode_snapshot(data))

        with mock.patch.object(Machine, "restore", restore), mock.patch.object(Machine, "restore_bytes", restore_bytes):
            chained = run_chained(self.image, SMALL_RAM, 2000, self.regs, cuts, True, 1, hashes, bytes_every=3, finished_every=1000)
        self.assertEqual(chained["divergencias"], [])
        # 23 restauraciones en destinos (12 nuevos, 11 usados) y una en la sonda entera (corte 0, usada, por bytes).
        self.assertEqual(len(calls), len(cuts) + 1)
        self.assertEqual(sum(1 for route, _ in calls if route == "bytes"), (len(cuts) + 2) // 3 + 1)
        self.assertEqual(sum(1 for _, used in calls if used), len(cuts) // 2 + 1)
        self.assertEqual(sum(1 for _, used in calls if not used), (len(cuts) + 1) // 2)

    def test_the_timers_cover_the_restoration(self) -> None:
        cuts = [20, 40, 60]
        hashes = self.chunked(cuts)["hashes"]
        original = Machine.restore

        def slow(self, state):
            time.sleep(0.03)
            original(self, state)

        with mock.patch.object(Machine, "restore", slow):                       # restore_bytes pasa por restore
            chained = run_chained(self.image, SMALL_RAM, 2000, self.regs, cuts, False, 2, hashes, bytes_every=2)
        self.assertGreaterEqual(chained["ms_snapshot_restore"], 15)
        self.assertGreaterEqual(chained["ms_snapshot_restore_bytes"], 15)
        self.assertGreaterEqual(chained["ms_por_corte"], 15)

    def test_chain_statistics_add_up_the_segments(self) -> None:
        cuts = [30, 60, 90]
        chunked = self.chunked(cuts)
        chained = run_chained(self.image, SMALL_RAM, 2000, self.regs, cuts, True, 1, chunked["hashes"], finished_every=1000)
        self.assertEqual((chained["divergencias"], chained["consumido"]), ([], chunked["consumido"]))
        self.assertGreaterEqual(chained["compilaciones"], chunked["compilaciones"] + len(cuts))   # cada restauración recompila
        self.assertGreater(chained["cobertura"], 0.5)
        self.assertAlmostEqual(chained["cobertura"], chunked["cobertura"], delta=0.15)              # una vuelta interpretada por corte

    def test_cuts_in_window_count_symmetrically(self) -> None:
        windows = [(50, 52, "código")]
        cuts = [46, 47, 48, 49, 50, 53, 54, 55, 56, 57]

        def fake_profile(*args, **kwargs):
            return {"instrucciones": 121, "parada": "ebreak", "ventanas": windows, "bloques": 0.0, "compilaciones": 0}

        def fake_plan(total, count, rng, windows=None):
            return list(cuts)

        with mock.patch("benchmarks.benchmark_rv32_invariante.profile", fake_profile), \
                mock.patch("benchmarks.benchmark_rv32_invariante.plan_cuts", fake_plan):
            result = verify_program("bucle", self.image, SMALL_RAM, 2000, len(cuts), random.Random(1), self.regs, step=1)
        self.assertEqual(result["divergencias"], 0)
        for plan in result["planes"].values():
            self.assertEqual(plan["cortes_en_ventana"], 9)       # 46–49 y 53–56 a ±4 de los bordes, 50 dentro; 57 fuera

    def test_derived_checks_run_at_every_cut_when_asked_and_leave_the_code_map_intact(self) -> None:
        cuts = list(range(10, 120, 10))
        hashes = self.chunked(cuts)["hashes"]
        calls: list[int] = []
        with mock.patch("benchmarks.benchmark_rv32_invariante.derived_errors", lambda machine: calls.append(1) or []):
            run_chained(self.image, SMALL_RAM, 2000, self.regs, cuts, True, 1, hashes, check_every=1, finished_every=1000)
        self.assertEqual(len(calls), len(cuts))
        # Las cuatro clases de error de derived_errors, y el mapa de código vaciado antes: la comprobación debe informar
        # de las marcas que faltan sin reponerlas (recompilar marca líneas; un mapa ya marcado no podría fallar).
        name, image, ram, gas, regs, feed, divisor = anchor_corpus()[5]
        machine = new_machine(image, ram, True, 1, regs)
        try:
            self.assertEqual(machine.run(2000).reason, "ebreak")
            self.assertEqual(derived_errors(machine), [])
            memory = machine.memory
            self.assertTrue(machine.hart.blocks and machine.hart.decode_cache)
            page, (_, entries) = next(iter(machine.hart.decode_cache.items()))
            index = next(i for i, entry in enumerate(entries) if entry is not None)
            offset = (page << 12) + (index << 2)
            saved = memory.words_u[offset >> 2]
            memory.words_u[offset >> 2] = saved ^ 0x00100000                          # (a) decodificación obsoleta
            self.assertTrue(any("decodificación obsoleta" in e for e in derived_errors(machine)))
            memory.words_u[offset >> 2] = saved
            self.assertEqual(derived_errors(machine), [])
            memory.code[:] = bytes(len(memory.code))                                   # (b) y (c) marcas perdidas
            before = bytes(memory.code)
            errors = derived_errors(machine)
            self.assertTrue(any("decodificación sin marca" in e for e in errors), errors)
            self.assertTrue(any("bloque sin marca" in e for e in errors), errors)
            self.assertEqual(bytes(memory.code), before)                              # y no las repone
        finally:
            machine.close()


RESTORED_HARTS: set[int] = set()


def _mtimecmp_only_program() -> tuple[bytes, dict[int, int]]:
    words = [encode(Kind.SW, 0, 10, 21, imm=4), encode(Kind.SW, 0, 10, 20, imm=0), encode(Kind.ADDI, 20, 20, imm=-1),
             encode(Kind.ADDI, 21, 21, imm=1), encode(Kind.BNE, 0, 20, 0, imm=-16), EBREAK]
    return assemble(words), {10: CLINT_BASE + CLINT_MTIMECMP, 20: 4, 21: 0x100}


def _run(program, cuts: int = 12, **kwargs) -> dict[str, object]:
    name, image, ram, gas, regs, feed, divisor = program
    return verify_program(name, image, ram, gas, cuts, random.Random(9), regs, step=kwargs.pop("step", 1), feed=feed,
                          time_divisor=divisor, **kwargs)


def _first_divergence(result: dict[str, object]) -> dict[str, object] | None:
    for plan in result["planes"].values():
        for mode in plan["modos"].values():
            if mode["divergencias"]:
                return mode["divergencias"][0]
    return None


class SecondHarnessReview(unittest.TestCase):
    """Anclas de la segunda ciega del arnés: mutantes del arnés que la suite dejaba vivos (cola olvidada por el
    snapshot y vista solo por ``observable``, memorias sin cerrar, plan sesgado, valores por defecto, referencia
    troceada con calentamiento 2, perfil en la última página y con ``mtimecmp``, cortes sesgados a ±4, divisores
    rotatorios, referencia sin bloques, gas mal informado, programa sin eventos, ida y vuelta de páginas)."""

    def test_a_queue_forgotten_by_the_snapshot_is_seen_by_a_program_that_does_not_consume_it(self) -> None:
        original = Machine.snapshot

        def forgetful(self):
            state = original(self)
            state["uart"]["input"] = []                         # el hash no ve la cola; el programa no la consume
            return state

        with mock.patch.object(Machine, "snapshot", forgetful):
            result = _run(anchor_corpus()[4])
        self.assertGreater(result["divergencias"], 0)
        self.assertIn("estado observable", _first_divergence(result)["motivo"])

    def test_every_memory_created_by_the_harness_is_closed(self) -> None:
        created, closed = [], set()
        original_init, original_close = Rv32Memory.__init__, Rv32Memory.close

        def init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            created.append(self)

        def close(self):
            closed.add(id(self))
            original_close(self)

        with mock.patch.object(Rv32Memory, "__init__", init), mock.patch.object(Rv32Memory, "close", close):
            _run(anchor_corpus()[4])
        self.assertGreater(len(created), 10)
        self.assertEqual([m for m in created if id(m) not in closed], [])

    def test_the_target_is_closed_when_a_restore_raises(self) -> None:
        created, closed = [], set()
        original_init, original_close = Rv32Memory.__init__, Rv32Memory.close

        def init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            created.append(self)

        def close(self):
            closed.add(id(self))
            original_close(self)

        name, image, ram, gas, regs, feed, divisor = anchor_corpus()[4]
        hashes = run_chunked(image, ram, gas, regs, [5, 10], True, 1, feed, divisor)["hashes"]
        with mock.patch.object(Rv32Memory, "__init__", init), mock.patch.object(Rv32Memory, "close", close), \
                mock.patch.object(Machine, "restore", side_effect=ValueError("roto")):
            with self.assertRaises(ValueError):
                run_chained(image, ram, gas, regs, [5, 10], True, 1, hashes, bytes_every=1000, time_divisor=divisor)
        self.assertGreater(len(created), 1)
        self.assertEqual([m for m in created if id(m) not in closed], [])

    def test_only_the_biased_plan_is_planned_with_windows(self) -> None:
        calls = []
        original = B.plan_cuts

        def spy(total, count, rng, windows=None):
            calls.append(bool(windows))
            return original(total, count, rng, windows)

        with mock.patch.object(B, "plan_cuts", spy):
            _run(anchor_corpus()[4], cuts=10)
        self.assertEqual(calls, [False, True])

    def test_short_programs_get_the_documented_defaults(self) -> None:
        seen = {"step": set(), "check_every": set(), "finished_every": set()}
        original_profile, original_chained = B.profile, B.run_chained

        def profile(*args, **kwargs):
            seen["step"].add(args[4])
            return original_profile(*args, **kwargs)

        def chained(*args, **kwargs):
            seen["check_every"].add(args[9])
            seen["finished_every"].add(args[11])
            return original_chained(*args, **kwargs)

        name, image, ram, gas, regs, feed, divisor = anchor_corpus()[4]
        with mock.patch.object(B, "profile", profile), mock.patch.object(B, "run_chained", chained):
            B.verify_program(name, image, ram, gas, 8, random.Random(9), regs, feed=feed, time_divisor=divisor)
        self.assertEqual(seen, {"step": {1}, "check_every": {1}, "finished_every": {1}})

    def test_a_warmup_two_bug_that_needs_a_warm_machine_is_seen_without_restoring(self) -> None:
        original = Rv32Hart.run

        def buggy(self, gas):                                 # solo con warmup 2 y contadores heredados de otro run
            if self.warmup == 2 and self.accelerate and self.block_hits:
                self.regs[31] ^= 1
            return original(self, gas)

        with mock.patch.object(Rv32Hart, "run", buggy):
            result = _run(anchor_corpus()[4])
        self.assertGreater(result["divergencias"], 0)
        details = [plan["divergencia_troceada"] for plan in result["planes"].values()]
        self.assertTrue(any(detail and detail["calentamiento"] == 2 for detail in details), details)

    def test_the_profile_sees_code_stores_on_the_last_page(self) -> None:
        loop = SelfModifyingCode.LOOP_WITH_STORE
        offset = SMALL_RAM - 0x100
        image = bytearray(offset + 4 * len(loop))
        image[0:4] = encode(Kind.JAL, 0, imm=offset).to_bytes(4, "little")
        image[offset:] = assemble(loop)
        profiled = profile(bytes(image), SMALL_RAM, 2000, {9: CODE + offset + 0x18, 8: 14}, 1)
        kinds = {kind for _, _, kind in profiled["ventanas"]}
        self.assertTrue(any("código" in kind for kind in kinds), kinds)

    def test_biased_cuts_stay_within_four_of_the_windows(self) -> None:
        windows = [(100 + 40 * i, 101 + 40 * i, "código") for i in range(30)]
        for seed in range(5):
            cuts = plan_cuts(5000, 120, random.Random(seed), windows)
            outside = [c for c in cuts if not any(s - 4 <= c <= e + 4 for s, e, _ in windows)]
            self.assertEqual(outside, [], seed)

    def test_the_corpora_rotate_the_time_divisors(self) -> None:
        self.assertTrue(set(B.DIVISORS) <= {program[-1] for program in anchor_corpus()})
        self.assertEqual({program[-1] for program in random_corpus(8, random.Random(1))}, set(B.DIVISORS))

    def test_the_reference_never_compiles_a_block(self) -> None:
        name, image, ram, gas, regs, feed, divisor = anchor_corpus()[4]
        with mock.patch("cpu_digital.rv32_blocks.compile_block", side_effect=AssertionError("la referencia compiló un bloque")):
            reference = B.run_uninterrupted(image, ram, gas, regs, feed, divisor)
        self.assertEqual(reference["parada"].reason, "ebreak")

    def test_the_profile_sees_a_program_that_only_writes_mtimecmp(self) -> None:
        image, regs = _mtimecmp_only_program()
        profiled = profile(image, SMALL_RAM, 2000, regs, 1)
        self.assertTrue(any("mmio" in kind for _, _, kind in profiled["ventanas"]), profiled["ventanas"])

    def test_a_restored_machine_that_misreports_gas_is_seen(self) -> None:
        original_restore, original_run = Rv32Hart.restore, Rv32Hart.run
        RESTORED_HARTS.clear()

        def restore(self, state):
            original_restore(self, state)
            RESTORED_HARTS.add(id(self))

        def run(self, gas):                                   # una máquina restaurada informa de un gas de más
            result = original_run(self, gas)
            if id(self) in RESTORED_HARTS:
                self.gas += 1
            return result

        with mock.patch.object(Rv32Hart, "restore", restore), mock.patch.object(Rv32Hart, "run", run):
            result = _run(anchor_corpus()[4])
        RESTORED_HARTS.clear()
        self.assertGreater(result["divergencias"], 0)

    def test_a_restore_that_changes_the_target_configuration_is_seen(self) -> None:
        original = Rv32Hart.restore

        def adopting(self, state):
            original(self, state)
            self.accelerate, self.warmup = True, 1

        with mock.patch.object(Rv32Hart, "restore", adopting):
            result = _run(anchor_corpus()[4])
        self.assertGreater(result["divergencias"], 0)
        self.assertIn("configuración", _first_divergence(result)["motivo"])

    def test_a_program_without_events_has_no_windows(self) -> None:
        name, image, ram, gas, regs, feed, divisor = anchor_corpus()[5]
        self.assertEqual(profile(image, ram, gas, regs, 1, feed, divisor)["ventanas"], [])

    def test_pages_round_trip_detects_a_page_lost_by_the_snapshot(self) -> None:
        name, image, ram, gas, regs, feed, divisor = anchor_corpus()[4]
        machine = new_machine(image, ram, False, 2, regs)
        try:
            machine.run(2000)
            self.assertTrue(pages_round_trip(machine.memory))
            original = Rv32Memory.snapshot_pages
            with mock.patch.object(Rv32Memory, "snapshot_pages", lambda self: original(self)[:-1]):
                self.assertFalse(pages_round_trip(machine.memory))
        finally:
            machine.close()

    def test_timers_are_none_without_samples(self) -> None:
        name, image, ram, gas, regs, feed, divisor = anchor_corpus()[4]
        hashes = run_chunked(image, ram, gas, regs, [7], True, 1, feed, divisor)["hashes"]
        chained = run_chained(image, ram, gas, regs, [7], True, 1, hashes, bytes_every=1, feed=feed, time_divisor=divisor)
        self.assertIsNone(chained["ms_snapshot_restore"])                  # el único corte fue por bytes
        self.assertIsNotNone(chained["ms_snapshot_restore_bytes"])
        result = _run(anchor_corpus()[4], cuts=1, bytes_every=1)
        for plan in result["planes"].values():
            for mode in plan["modos"].values():
                self.assertIsNone(mode["ms_snapshot_restore"])


class EmbenchUnderCuts(unittest.TestCase):
    def test_statemate_with_64_uniform_and_64_biased_cuts(self) -> None:
        if not EMBENCH_STATEMATE.is_file():
            self.skipTest("falta el binario versionado de Embench statemate")
        rng = random.Random(11)
        result = verify_program("statemate", EMBENCH_STATEMATE.read_bytes(), 1 << 20, 400_000_000, 64, rng, None, step=64,
                                bytes_every=4, check_every=8)
        self.assertEqual(result["divergencias"], 0, result)
        self.assertEqual(result["parada"], "apagado")
        self.assertGreater(int(result["ventanas"]), 0)
        for plan in result["planes"].values():
            self.assertEqual(plan["cortes"], 64)
            for mode_name, _, _ in MODES:
                self.assertEqual(plan["modos"][mode_name]["cortes_alcanzados"], 64)


class CoreMarkUnderCuts(unittest.TestCase):
    def test_coremark_with_48_cuts_per_plan(self) -> None:
        if not COREMARK.is_file():
            self.skipTest("falta el binario versionado de CoreMark")
        rng = random.Random(5)
        result = verify_program("coremark", COREMARK.read_bytes(), 1 << 20, 20_000_000, 48, rng, None, step=64,
                                bytes_every=6, check_every=8)
        self.assertEqual(result["divergencias"], 0, result)
        self.assertEqual((result["parada"], result["instrucciones"]), ("apagado", 8_505_937))
        for plan in result["planes"].values():
            for mode_name, _, _ in MODES:
                self.assertEqual(plan["modos"][mode_name]["cortes_alcanzados"], 48)


if __name__ == "__main__":
    unittest.main()
