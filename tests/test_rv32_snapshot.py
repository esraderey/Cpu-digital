"""Anclas del snapshot del hart (H2.1): ida y vuelta, restauración atómica, hash de estado, inventario (P2.5) e
invariante S acotada (``run(B1); snapshot; restore; run(B2)`` frente a ``run(B1 + B2)``)."""

from __future__ import annotations

import collections
import copy
import enum
import json
import pkgutil
import random
import sys
import types
import unittest
import zlib
from pathlib import Path
from unittest import mock

import cpu_digital
from cpu_digital import rv32_alu, rv32_blocks, rv32_decode, rv32_dtb, rv32_hart, rv32_isa, rv32_mem, rv32_platform
from cpu_digital.memory import PagedMemory
from cpu_digital.rv32_blocks import Block
from cpu_digital.rv32_hart import (
    MASK32,
    MASK64,
    MAX_SNAPSHOT_RAW_BYTES,
    SNAPSHOT_MAGIC,
    SNAPSHOT_VERSION,
    Rv32Hart,
    Stop,
    canonical_json,
    decode_snapshot,
    encode_snapshot,
)
from cpu_digital.rv32_isa import CSR_INSTRET, Kind, encode
from cpu_digital.rv32_mem import Rv32Memory
from cpu_digital.rv32_platform import CLINT_BASE, CLINT_MTIME, CLINT_MTIMECMP, UART_BASE, Bus, Clint, Machine, Syscon, Uart
from tests import test_rv32_blocks as blocks_tests
from tests.test_rv32_blocks import check_invariants, hostile_layout, recording_blocks, snapshot_round_trips
from tests.test_rv32_hart import BASE, CODE, DATA, RAM, Reference, assemble, random_program

ROOT = Path(__file__).resolve().parents[1]
COREMARK = ROOT / "benchmarks" / "rv32_arranque" / "bin" / "coremark_rv32i_zicsr_it10.bin"
COREMARK_INSTRUCTIONS = 8_505_936 + 1
EBREAK = encode(Kind.EBREAK)
MTIMECMP = CLINT_BASE + CLINT_MTIMECMP
# Programa de referencia: datos en tres páginas (alineado, cruzando página, último byte de página), un byte por el
# UART, mtimecmp y un ebreak final.
PROGRAM = [encode(Kind.SW, 0, 10, 5, imm=0), encode(Kind.SW, 0, 11, 5, imm=0), encode(Kind.SB, 0, 12, 5, imm=0),
           encode(Kind.SB, 0, 13, 6, imm=0), encode(Kind.SW, 0, 14, 7, imm=0), encode(Kind.ADDI, 1, 0, imm=1), EBREAK]
PROGRAM_REGS = {5: 0x11223344, 6: 0x41, 7: 0x12345, 10: DATA, 11: CODE + 0x1FFE, 12: CODE + 0x4FFF, 13: UART_BASE,
                14: MTIMECMP}
LOOP = [encode(Kind.ADDI, 1, 1, imm=1), encode(Kind.ADDI, 20, 20, imm=-1), encode(Kind.BNE, 0, 20, 0, imm=-8), EBREAK]
PATCHED = encode(Kind.ADDI, 1, 1, imm=1)
# Bucle que reescribe en cada vuelta la misma palabra sobre su propia instrucción 1 (una línea de código): cada
# vuelta invalida el bloque (recompilations) y, con warmup 2, el punto de control se calienta (block_hits).
PATCH_LOOP = [encode(Kind.ADDI, 20, 0, imm=14), PATCHED, encode(Kind.SW, 0, 10, 5, imm=4),
              encode(Kind.ADDI, 20, 20, imm=-1), encode(Kind.BNE, 0, 20, 0, imm=-12), EBREAK]
RV32_MODULES = (rv32_alu, rv32_blocks, rv32_decode, rv32_dtb, rv32_hart, rv32_isa, rv32_mem, rv32_platform)
INVENTORIED = (Rv32Hart, Rv32Memory, Uart, Clint, Bus, Syscon, Machine, Block)
# Clasificación exacta de cada slot (fija la lista correcta, no solo «alguna lista»; hallazgo de las revisiones).
GOLDEN = {
    Rv32Hart: {"STATE_FIELDS": {"regs", "pc", "instret", "reservation", "time_divisor", "privilege", "mstatus", "mie", "mtvec",
                                "mscratch", "mepc", "mcause", "mtval"},
               "DERIVED_FIELDS": {"decode_cache", "blocks", "block_hits", "recompilations", "base", "size", "lanes", "words_u",
                                  "gens", "written", "code"},
               "CONFIG_FIELDS": {"memory", "bus", "interrupts", "accelerate", "horizon", "warmup", "keep_block_sources",
                                 "max_blocks"},
               "STATS_FIELDS": {"block_instructions", "compilations", "cache_flushes"}, "HARNESS_FIELDS": {"gas"}},
    Rv32Memory: {"STATE_FIELDS": {"buffer", "lanes", "words_u", "words_i"}, "DERIVED_FIELDS": {"generations", "written", "code"},
                 "CONFIG_FIELDS": {"base", "size", "limit", "_closed"}, "STATS_FIELDS": set(), "HARNESS_FIELDS": set()},
    Uart: {"STATE_FIELDS": {"output", "input"}, "DERIVED_FIELDS": set(), "CONFIG_FIELDS": set(), "STATS_FIELDS": set(),
           "HARNESS_FIELDS": set()},
    Clint: {"STATE_FIELDS": {"mtimecmp", "msip"}, "DERIVED_FIELDS": set(), "CONFIG_FIELDS": {"hart"}, "STATS_FIELDS": set(),
            "HARNESS_FIELDS": set()},
    Bus: {"STATE_FIELDS": set(), "DERIVED_FIELDS": set(), "CONFIG_FIELDS": {"windows"}, "STATS_FIELDS": set(), "HARNESS_FIELDS": set()},
    Syscon: {"STATE_FIELDS": set(), "DERIVED_FIELDS": set(), "CONFIG_FIELDS": set(), "STATS_FIELDS": set(), "HARNESS_FIELDS": set()},
    Machine: {"STATE_FIELDS": set(), "DERIVED_FIELDS": set(),
              "CONFIG_FIELDS": {"memory", "bus", "hart", "uart", "syscon", "clint", "time_divisor"}, "STATS_FIELDS": set(),
              "HARNESS_FIELDS": set()},
    Block: {"STATE_FIELDS": set(), "DERIVED_FIELDS": {"pc", "length", "cost", "pages", "generations", "run", "source", "page0",
                                                     "gen0", "page1", "gen1"},
            "CONFIG_FIELDS": set(), "STATS_FIELDS": set(), "HARNESS_FIELDS": set()},
}
CATEGORIES = ("STATE_FIELDS", "DERIVED_FIELDS", "CONFIG_FIELDS", "STATS_FIELDS", "HARNESS_FIELDS")
CONFIG_NAMES = ("accelerate", "horizon", "warmup", "max_blocks", "keep_block_sources", "bus", "gas", "block_instructions",
                "compilations", "cache_flushes", "decode_cache", "blocks", "block_hits", "recompilations", "code",
                "generations", "written")


def make_machine(words: list[int], regs: dict[int, int] | None = None, ram: int = RAM, time_divisor: int = 1,
                 accelerate: bool = False, warmup: int = 2) -> Machine:
    machine = Machine(ram_bytes=ram, time_divisor=time_divisor)
    machine.load_flat(assemble(words))
    machine.hart.accelerate = accelerate
    machine.hart.warmup = warmup
    for index, value in (regs or {}).items():
        machine.hart.regs[index] = value
    return machine


def inventory_errors(cls: type) -> list[str]:
    """Puerta de inventario (P2.5): cada slot de ``cls`` (y de sus bases) en exactamente una categoría."""
    errors: list[str] = []
    slots = {slot for klass in cls.__mro__ for slot in getattr(klass, "__slots__", ())}
    seen: dict[str, str] = {}
    for category in CATEGORIES:
        fields = getattr(cls, category, None)
        if fields is None:
            errors.append(f"{cls.__name__} no declara {category}")
            continue
        for field in fields:
            if field in seen:
                errors.append(f"{cls.__name__}.{field} está en {seen[field]} y en {category}")
            seen[field] = category
            if field not in slots:
                errors.append(f"{category} de {cls.__name__} nombra {field}, que no es un slot")
    for field in sorted(slots - set(seen)):
        errors.append(f"{cls.__name__}.{field} sin clasificar")
    for field, rule in getattr(cls, "DERIVED_FIELDS", {}).items():
        if not isinstance(rule, str) or len(rule.strip()) < 12 or not any(ch.isalpha() for ch in rule):   # una regla con palabras
            errors.append(f"{cls.__name__}.{field} sin regla de invalidación")
    return errors


def full_state(machine: Machine) -> tuple:
    """Todo lo observable de una máquina (incluidas cachés por identidad y claves, estadísticas y gas)."""
    hart, memory = machine.hart, machine.memory
    return (machine.snapshot(), machine.state_hash(), hart.gas, hart.block_instructions, hart.compilations,
            hart.cache_flushes, id(hart.decode_cache), sorted(hart.decode_cache), id(hart.blocks), sorted(hart.blocks),
            id(hart.block_hits), dict(hart.block_hits), id(hart.recompilations), dict(hart.recompilations),
            bytes(memory.code), list(memory.generations), bytes(memory.written), bytes(memory.lanes), hart.accelerate,
            hart.warmup, hart.horizon, hart.max_blocks, hart.keep_block_sources, bytes(machine.uart.output),
            list(machine.uart.input), machine.clint.mtimecmp, hart.time_divisor)


class RoundTrip(unittest.TestCase):
    def make_source(self) -> Machine:
        machine = make_machine(PROGRAM, PROGRAM_REGS)
        self.addCleanup(machine.close)
        self.assertEqual(machine.run(100).reason, "ebreak")
        machine.uart.feed("zz")
        return machine

    def test_round_trip_is_exact_in_dict_bytes_and_constructor(self) -> None:
        machine = self.make_source()
        state = machine.snapshot()
        self.assertEqual(json.loads(json.dumps(state)), state)                 # serializable y estable en JSON
        self.assertEqual(set(state), {"version", "hart", "ram", "uart", "clint"})
        self.assertEqual(state["version"], SNAPSHOT_VERSION)
        hart = state["hart"]
        zero_csrs = {f"{number:#05x}": 0 for number, _ in rv32_hart.CSR_SLOTS}   # v2: los siete CSR de modo M (H3.0)
        self.assertEqual((hart["pc"], hart["instret"], hart["csrs"], hart["privilege"], hart["reservation"], hart["time_divisor"]),
                         (CODE + 24, 6, zero_csrs, "M", None, 1))
        self.assertEqual(hart["regs"][1], 1)
        self.assertEqual(state["uart"], {"output": [0x41], "input": [0x7A, 0x7A]})
        self.assertEqual(state["clint"], {"mtimecmp": 0x12345, "msip": 0})
        self.assertEqual((state["ram"]["base"], state["ram"]["size"], state["ram"]["page_words"]), (BASE, RAM, 4096))
        self.assertEqual([index for index, _ in state["ram"]["pages"]], [0, 1])   # páginas 0–2 y 4 de 4 KiB
        for maker in (self.restore_dict, self.restore_bytes, self.from_bytes):
            with self.subTest(maker=maker.__name__):
                restored = maker(machine)
                self.assertEqual(restored.snapshot(), state)
                self.assertEqual(bytes(restored.memory.lanes), bytes(machine.memory.lanes))
                self.assertEqual(restored.state_hash(), machine.state_hash())
                self.assertEqual((restored.hart.pc, restored.hart.regs, restored.hart.instret), (CODE + 24, machine.hart.regs, 6))
                self.assertEqual((bytes(restored.uart.output), list(restored.uart.input), restored.clint.mtimecmp),
                                 (b"A", [0x7A, 0x7A], 0x12345))
                self.assertEqual(restored.memory.written_pages(), [0, 1, 2, 4])
        data = machine.snapshot_bytes()
        self.assertTrue(data.startswith(SNAPSHOT_MAGIC))
        self.assertEqual(data, machine.snapshot_bytes())                       # determinista
        self.assertEqual(decode_snapshot(data, MAX_SNAPSHOT_RAW_BYTES), state)
        self.assertEqual(canonical_json(state), canonical_json(json.loads(json.dumps(state))))

    def restore_dict(self, machine: Machine) -> Machine:
        other = Machine(ram_bytes=RAM)
        self.addCleanup(other.close)
        other.restore(machine.snapshot())
        return other

    def restore_bytes(self, machine: Machine) -> Machine:
        other = Machine(ram_bytes=RAM)
        self.addCleanup(other.close)
        other.restore_bytes(machine.snapshot_bytes())
        return other

    def from_bytes(self, machine: Machine) -> Machine:
        other = Machine.from_snapshot_bytes(machine.snapshot_bytes())
        self.addCleanup(other.close)
        self.assertEqual((other.memory.size, other.memory.base), (RAM, BASE))
        return other

    def test_bare_hart_snapshot_has_only_its_sections(self) -> None:
        machine = self.make_source()
        hart_state = machine.hart.snapshot()
        self.assertEqual(set(hart_state), {"version", "hart", "ram"})
        memory = Rv32Memory(RAM)
        self.addCleanup(memory.close)
        hart = Rv32Hart(memory)
        hart.restore(hart_state)
        self.assertEqual(hart.snapshot(), hart_state)
        self.assertEqual(hart.state_hash(), machine.hart.state_hash())
        hart.restore_bytes(machine.hart.snapshot_bytes())
        self.assertEqual(hart.snapshot(), hart_state)
        with self.assertRaises(ValueError):
            hart.restore(machine.snapshot())            # secciones de más (uart, clint)
        with self.assertRaises(ValueError):
            machine.restore(hart_state)                 # secciones de menos
        self.assertEqual(hart.snapshot(), hart_state)
        self.assertEqual(machine.state_hash(), RoundTrip.hash_of(machine))
        other = Rv32Memory(RAM * 2)
        self.addCleanup(other.close)
        with self.assertRaises(ValueError):
            Rv32Hart(other).restore(hart_state)         # otra geometría de RAM
        self.assertEqual(other.snapshot_pages(), [])

    @staticmethod
    def hash_of(machine: Machine) -> str:
        return machine.state_hash()

    def test_paged_memory_accepts_the_v2_pages(self) -> None:
        machine = self.make_source()
        pages = machine.snapshot()["ram"]["pages"]
        paged = PagedMemory.from_snapshot(RAM // 4, pages)
        self.assertEqual(paged.read_block(0, RAM // 4), tuple(machine.memory.words_i))
        self.assertEqual(paged.snapshot_pages(), pages)


class UsedMachine(unittest.TestCase):
    def test_restore_on_a_hot_machine_matches_a_fresh_one(self) -> None:
        source = make_machine(PROGRAM, PROGRAM_REGS)
        self.addCleanup(source.close)
        self.assertEqual(source.run(3), Stop("gas"))          # a mitad del programa
        state = source.snapshot()
        # Una máquina que ejecutó OTRO código en las mismas direcciones con bloques: cachés, calentamiento,
        # recompilaciones y mapa calientes (hallazgo de las revisiones: con warmup 1 y sin parches, block_hits y
        # recompilations estaban vacíos antes de restaurar y la aserción de abajo era vacua).
        used = make_machine(PATCH_LOOP, {10: CODE, 5: PATCHED}, accelerate=True, warmup=2)
        self.addCleanup(used.close)
        self.assertEqual(used.run(1000).reason, "ebreak")
        self.assertTrue(used.hart.blocks and used.hart.decode_cache and any(used.memory.code))
        self.assertTrue(used.hart.block_hits and used.hart.recompilations)
        regs = used.hart.regs
        used.uart.feed("old")
        used.clint.mtimecmp = 99
        before = list(used.memory.generations)
        fresh = Machine(ram_bytes=RAM)
        self.addCleanup(fresh.close)
        for machine in (used, fresh):
            machine.restore(state)
        self.assertEqual(used.state_hash(), fresh.state_hash())
        self.assertEqual(used.state_hash(), source.state_hash())
        self.assertEqual((used.hart.decode_cache, used.hart.blocks, used.hart.block_hits, used.hart.recompilations), ({}, {}, {}, {}))
        self.assertIs(used.hart.regs, regs)                          # la misma lista, como reset
        self.assertFalse(any(used.memory.code))
        self.assertTrue(all(after >= old for after, old in zip(used.memory.generations, before)))
        self.assertGreater(used.memory.generation(0), before[0])   # la página del código cambió de generación
        self.assertTrue(used.hart.accelerate)                        # la configuración no se adopta
        self.assertEqual((list(used.uart.input), used.clint.mtimecmp), ([], 0))
        for machine in (source, used, fresh):
            self.assertEqual(machine.run(100).reason, "ebreak")
        for machine in (used, fresh):
            self.assertEqual(machine.state_hash(), source.state_hash())
            self.assertEqual((bytes(machine.uart.output), machine.clint.mtimecmp, machine.hart.instret), (b"A", 0x12345, 6))

    def test_restore_raises_generations_and_clears_the_code_map(self) -> None:
        machine = make_machine(LOOP, {20: 5}, accelerate=True, warmup=1)
        self.addCleanup(machine.close)
        self.assertEqual(machine.run(100).reason, "ebreak")
        state = machine.snapshot()
        before = list(machine.memory.generations)
        machine.restore(state)
        self.assertEqual(machine.snapshot(), state)
        self.assertFalse(any(machine.memory.code))
        self.assertGreater(machine.memory.generation(0), before[0])
        self.assertTrue(all(after >= old for after, old in zip(machine.memory.generations, before)))
        self.assertEqual(machine.run(100).reason, "ebreak")         # vuelve a ejecutar el ebreak sin cambiar nada
        self.assertEqual(machine.snapshot(), state)


class AtomicRejection(unittest.TestCase):
    BAD_PAGES = ("x", [[0]], [[4, [1]]], [[0, []]], [[0, [1 << 31]]], [[0, [-(1 << 31) - 1]]], [[0, [1]], [0, [2]]],
                 [[3, [1] * 4097]], [[-1, [1]]], [[0.0, [1]]], [[0, [True]]], [[True, [1]]], [[0, (1, 2)]], [[0, [1.0]]],
                 [(0, [1])], [[0, [1], 2]], None, {})

    def cases(self) -> list[tuple[str, object]]:
        def mutate(name, change):
            return name, change

        cases = [
            mutate("sin version", lambda s: s.pop("version")),
            mutate("clave de más en la raíz", lambda s: s.__setitem__("extra", 1)),
            mutate("raíz lista", lambda s: s.clear() or s.__setitem__("x", 1)),
            mutate("hart sin regs", lambda s: s["hart"].pop("regs")),
            mutate("hart con clave extra", lambda s: s["hart"].__setitem__("mode", 3)),
            mutate("hart no diccionario", lambda s: s.__setitem__("hart", [])),
            mutate("ram sin pages", lambda s: s["ram"].pop("pages")),
            mutate("ram con clave extra", lambda s: s["ram"].__setitem__("x", 0)),
            mutate("uart sin input", lambda s: s["uart"].pop("input")),
            mutate("uart con clave extra", lambda s: s["uart"].__setitem__("baud", 9600)),
            mutate("uart lista", lambda s: s.__setitem__("uart", [])),
            mutate("clint con clave extra", lambda s: s["clint"].__setitem__("mtime", 0)),
            mutate("clint texto", lambda s: s.__setitem__("clint", "x")),
            mutate("regs de 31", lambda s: s["hart"]["regs"].pop()),
            mutate("regs de 33", lambda s: s["hart"]["regs"].append(0)),
            mutate("x0 = 1", lambda s: s["hart"]["regs"].__setitem__(0, 1)),
            mutate("regs texto", lambda s: s["hart"].__setitem__("regs", "x" * 32)),
        ]
        for value, label in ((1 << 32, "2^32"), (-1, "-1"), (True, "True"), (1.0, "1.0"), ("1", "'1'"), (None, "None")):
            cases.append(mutate(f"registro {label}", lambda s, v=value: s["hart"]["regs"].__setitem__(5, v)))
            cases.append(mutate(f"pc {label}", lambda s, v=value: s["hart"].__setitem__("pc", v)))
        for value, label in ((-1, "-1"), (True, "True"), (1.5, "1.5"), ("3", "'3'"), (None, "None")):
            cases.append(mutate(f"instret {label}", lambda s, v=value: s["hart"].__setitem__("instret", v)))
        for value, label in ((0, "0"), (-1, "-1"), (True, "True"), (2.0, "2.0"), (None, "None")):
            cases.append(mutate(f"time_divisor {label}", lambda s, v=value: s["hart"].__setitem__("time_divisor", v)))
        # v2 (H3.0): 1 y 2 son versiones válidas; una reserva es una dirección de palabra en la RAM.
        for value, label in ((0, "0"), (3, "3"), ("2", "'2'"), (True, "True"), (None, "None")):
            cases.append(mutate(f"version {label}", lambda s, v=value: s.__setitem__("version", v)))
        for value, label in (([], "lista"), ({"0x300": 1}, "con una clave"), ({"x": 1}, "clave no numérica"), (None, "None")):
            cases.append(mutate(f"csrs {label}", lambda s, v=value: s["hart"].__setitem__("csrs", v)))
        for value, label in ((0, "0"), (False, "False"), (CODE + 2, "desalineada"), (CODE + RAM, "fuera de la RAM"), ("", "''")):
            cases.append(mutate(f"reservation {label}", lambda s, v=value: s["hart"].__setitem__("reservation", v)))
        cases.append(mutate("base distinta", lambda s: s["ram"].__setitem__("base", BASE + 0x1000)))
        cases.append(mutate("size distinto", lambda s: s["ram"].__setitem__("size", RAM * 2)))
        cases.append(mutate("size texto", lambda s: s["ram"].__setitem__("size", str(RAM))))
        cases.append(mutate("page_words 1024", lambda s: s["ram"].__setitem__("page_words", 1024)))
        cases.append(mutate("page_words texto", lambda s: s["ram"].__setitem__("page_words", "4096")))
        for index, bad in enumerate(self.BAD_PAGES):
            cases.append(mutate(f"pages inválidas {index}", lambda s, v=bad: s["ram"].__setitem__("pages", v)))
        for value, label in ((256, "256"), (-1, "-1"), ("A", "'A'"), (True, "True"), (1.0, "1.0")):
            cases.append(mutate(f"salida UART {label}", lambda s, v=value: s["uart"]["output"].append(v)))
            cases.append(mutate(f"entrada UART {label}", lambda s, v=value: s["uart"]["input"].append(v)))
        cases.append(mutate("salida UART texto", lambda s: s["uart"].__setitem__("output", "AB")))
        cases.append(mutate("entrada UART tupla", lambda s: s["uart"].__setitem__("input", (1, 2))))
        for value, label in ((-1, "-1"), (1 << 64, "2^64"), (True, "True"), ("0", "'0'"), (None, "None")):
            cases.append(mutate(f"mtimecmp {label}", lambda s, v=value: s["clint"].__setitem__("mtimecmp", v)))
        return cases

    def test_every_malformed_snapshot_is_rejected_without_touching_anything(self) -> None:
        source = make_machine(PROGRAM, PROGRAM_REGS)
        self.addCleanup(source.close)
        self.assertEqual(source.run(100).reason, "ebreak")
        source.uart.feed("zz")
        good = source.snapshot()
        # El destino tiene cachés calientes, estadísticas, gas y configuración propios: nada debe moverse.
        target = make_machine(LOOP, {20: 20}, accelerate=True, warmup=1)
        self.addCleanup(target.close)
        self.assertEqual(target.run(1000).reason, "ebreak")
        target.hart.gas = 37
        target.uart.feed("q")
        target.clint.mtimecmp = 5
        target.hart.max_blocks = 7
        before = full_state(target)
        count = 0
        for name, change in self.cases():
            state = copy.deepcopy(good)
            change(state)
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    target.restore(state)
                self.assertEqual(full_state(target), before)
                count += 1
        self.assertGreater(count, 80)
        target.restore(good)                                     # el bueno sí entra, y es exacto
        self.assertEqual(target.snapshot(), good)
        self.assertEqual(target.hart.gas, 37)                    # el gas no lo toca el snapshot
        self.assertEqual((target.hart.accelerate, target.hart.max_blocks), (True, 7))

    def test_malformed_bytes_are_rejected(self) -> None:
        source = make_machine(PROGRAM, PROGRAM_REGS)
        self.addCleanup(source.close)
        self.assertEqual(source.run(100).reason, "ebreak")
        data = source.snapshot_bytes()
        raw = canonical_json(source.snapshot())
        target = Machine(ram_bytes=RAM)
        self.addCleanup(target.close)
        before = full_state(target)
        bad_inputs = {
            "firma de VM32": b"TVMS32\x01" + data[len(SNAPSHOT_MAGIC):],
            "sin firma": data[len(SNAPSHOT_MAGIC):],
            "vacío": b"",
            "truncado": data[:-7],
            "sin el último byte": data[:-1],
            "sin el adler32": data[:-4],              # JSON completo, flujo sin terminar (hallazgo de las revisiones)
            "otra versión de firma": SNAPSHOT_MAGIC[:-1] + b"\x02" + data[len(SNAPSHOT_MAGIC):],
            "anidamiento sin fondo": SNAPSHOT_MAGIC + zlib.compress(b"[" * 200_000),
            "cola de más": data + b"\x00",
            "zlib corrupto": SNAPSHOT_MAGIC + b"\x78\x9c\x00\x00",
            "JSON lista": SNAPSHOT_MAGIC + zlib.compress(b"[1, 2]"),
            "JSON roto": SNAPSHOT_MAGIC + zlib.compress(b"{\"a\": "),
            "no UTF-8": SNAPSHOT_MAGIC + zlib.compress(b"\xff\xfe"),
            "texto": "RV32S\x01",
            "None": None,
        }
        for name, bad in bad_inputs.items():
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    target.restore_bytes(bad)
                with self.assertRaises(ValueError):
                    Machine.from_snapshot_bytes(bad)
                self.assertEqual(full_state(target), before)
        with self.assertRaises(ValueError):
            decode_snapshot(data, len(raw) - 1)                   # tope de tamaño descomprimido
        self.assertEqual(decode_snapshot(data, len(raw)), source.snapshot())
        with mock.patch.object(rv32_hart, "MAX_SNAPSHOT_RAW_BYTES", 16):
            with self.assertRaises(ValueError):
                target.restore_bytes(data)
            with self.assertRaises(ValueError):
                Machine.from_snapshot_bytes(data)
        self.assertEqual(full_state(target), before)
        with self.assertRaises(ValueError):
            Machine.from_snapshot_bytes(data, max_ram_bytes=RAM - 1)   # RAM mayor que el tope: no se reserva
        for base, size in ((BASE + 100, RAM), (BASE, RAM + 100), (BASE, -RAM), (BASE, 0), (0xFFFFF000, RAM)):
            with self.subTest(base=hex(base), size=size):
                state = source.snapshot()
                state["ram"]["base"], state["ram"]["size"] = base, size
                with self.assertRaises(ValueError):
                    Machine.from_snapshot_bytes(encode_snapshot(state))
        # Geometría válida pero contenido inválido: la máquina creada se cierra antes de relanzar.
        state = source.snapshot()
        state["hart"]["pc"] = -1
        with mock.patch.object(Machine, "close", autospec=True) as closed:
            with self.assertRaises(ValueError):
                Machine.from_snapshot_bytes(encode_snapshot(state))
        closed.assert_called_once()
        closed.call_args.args[0].memory.close()


class Configuration(unittest.TestCase):
    def test_host_configuration_is_never_in_the_snapshot_nor_adopted(self) -> None:
        source = make_machine(PROGRAM, PROGRAM_REGS, time_divisor=7, accelerate=True, warmup=1)
        self.addCleanup(source.close)
        source.hart.horizon = False
        source.hart.max_blocks = 4
        source.hart.keep_block_sources = True
        self.assertEqual(source.run(3), Stop("gas"))
        state = source.snapshot()
        text = json.dumps(state)
        for name in CONFIG_NAMES:
            self.assertNotIn(f'"{name}"', text)
        self.assertEqual(state["hart"]["time_divisor"], 7)
        defaults = Machine(ram_bytes=RAM)
        self.addCleanup(defaults.close)
        defaults.hart.gas = 11
        defaults.restore(state)
        self.assertEqual((defaults.hart.accelerate, defaults.hart.horizon, defaults.hart.warmup, defaults.hart.max_blocks,
                          defaults.hart.keep_block_sources, defaults.hart.gas), (False, True, 2, 4096, False, 11))
        self.assertEqual((defaults.hart.time_divisor, defaults.clint.time_divisor), (7, 7))   # estado del invitado
        contrary = make_machine(LOOP, {20: 3}, accelerate=True, warmup=1)
        self.addCleanup(contrary.close)
        self.assertEqual(contrary.run(100).reason, "ebreak")
        stats = (contrary.hart.block_instructions, contrary.hart.compilations, contrary.hart.cache_flushes)
        self.assertGreater(stats[0], 0)
        contrary.restore(state)
        self.assertEqual((contrary.hart.accelerate, contrary.hart.warmup), (True, 1))
        self.assertEqual((contrary.hart.block_instructions, contrary.hart.compilations, contrary.hart.cache_flushes), stats)
        for machine in (source, defaults, contrary):
            self.assertEqual(machine.run(100).reason, "ebreak")
            self.assertEqual(machine.state_hash(), source.state_hash())
        self.assertEqual((defaults.hart.block_instructions, defaults.hart.compilations), (0, 0))
        # El destino acelerado siguió compilando tras restaurar (el bloque de la cola del programa).
        self.assertGreater(contrary.hart.compilations, stats[1])
        self.assertGreaterEqual(contrary.hart.block_instructions, stats[0])

    def test_snapshot_is_the_same_with_or_without_blocks_and_cuts(self) -> None:
        words = LOOP
        hashes = set()
        snapshots = []
        for accelerate, warmup, cuts in ((False, 2, None), (True, 1, None), (True, 2, None), (False, 2, 3), (True, 1, 5), (True, 2, 7)):
            machine = make_machine(words, {20: 50}, accelerate=accelerate, warmup=warmup)
            self.addCleanup(machine.close)
            if cuts is None:
                self.assertEqual(machine.run(100), Stop("gas"))
            else:
                remaining = 100
                while remaining:
                    chunk = min(cuts, remaining)
                    self.assertEqual(machine.run(chunk), Stop("gas"))
                    remaining -= chunk
            self.assertEqual(machine.hart.instret, 100)
            hashes.add(machine.state_hash())
            snapshots.append(machine.snapshot())
            if accelerate:
                self.assertGreater(machine.hart.block_instructions, 0)
        self.assertEqual(len(hashes), 1)
        self.assertTrue(all(snapshot == snapshots[0] for snapshot in snapshots))


class StateHash(unittest.TestCase):
    # Valores calculados en otro proceso (un guion aparte sobre la copia aislada de H3.0, esquema v2): fijan la
    # serialización canónica. Los de H2.1 (v1) eran 31aa6133… y 5273dc1c….
    MACHINE_HASH = "140ca2ab8eea8a1bdd92617f13aa447dda32f697ea81832937d2fc1769d850e1"
    HART_HASH = "a63c7078aca2e683258b0b97999d63990ed8864e79e35e5ed4b0b54f97be0989"

    def deterministic(self) -> Machine:
        machine = make_machine([encode(Kind.ADDI, 1, 0, imm=5), encode(Kind.SB, 0, 10, 1, imm=0), EBREAK], {10: UART_BASE})
        self.addCleanup(machine.close)
        self.assertEqual(machine.run(10).reason, "ebreak")
        return machine

    def test_hash_is_stable_across_processes(self) -> None:
        machine = self.deterministic()
        self.assertEqual(machine.state_hash(), self.MACHINE_HASH)
        self.assertEqual(machine.hart.state_hash(), self.HART_HASH)
        self.assertEqual(len(machine.state_hash()), 64)

    def test_every_state_field_changes_the_hash_and_configuration_does_not(self) -> None:
        machine = self.deterministic()
        reference = machine.state_hash()
        hart_reference = machine.hart.state_hash()
        changes = {
            "byte de memoria": lambda: machine.memory.store8(DATA, 1),
            "registro": lambda: machine.hart.regs.__setitem__(2, 9),
            "pc": lambda: setattr(machine.hart, "pc", CODE + 4),
            "instret": lambda: setattr(machine.hart, "instret", 50),
            "time_divisor": lambda: setattr(machine.hart, "time_divisor", 3),
            "salida UART": lambda: machine.uart.output.append(0x42),
            "entrada UART": lambda: machine.uart.feed("x"),
            "mtimecmp": lambda: setattr(machine.clint, "mtimecmp", 1),
        }
        seen = {reference}
        for name, change in changes.items():
            with self.subTest(change=name):
                state = machine.snapshot()
                change()
                digest = machine.state_hash()
                self.assertNotIn(digest, seen)
                seen.add(digest)
                machine.restore(state)                           # deshacer por el propio snapshot
                self.assertEqual(machine.state_hash(), reference)
        for name, change in (("accelerate", lambda: setattr(machine.hart, "accelerate", True)),
                             ("warmup", lambda: setattr(machine.hart, "warmup", 1)),
                             ("horizon", lambda: setattr(machine.hart, "horizon", False)),
                             ("gas", lambda: setattr(machine.hart, "gas", 123)),
                             ("estadísticas", lambda: setattr(machine.hart, "block_instructions", 99)),
                             ("mapa de código", lambda: machine.memory.mark_code(0x100, 4))):
            with self.subTest(change=name):
                change()
                self.assertEqual(machine.state_hash(), reference)
        self.assertEqual(machine.hart.state_hash(), hart_reference)


class Inventory(unittest.TestCase):
    def test_every_slot_is_classified(self) -> None:
        for cls in INVENTORIED:
            with self.subTest(cls=cls.__name__):
                self.assertEqual(inventory_errors(cls), [])
        self.assertEqual(set(Rv32Hart.STATE_FIELDS), {"regs", "pc", "instret", "reservation", "time_divisor", "privilege",
                                                      "mstatus", "mie", "mtvec", "mscratch", "mepc", "mcause", "mtval"})
        self.assertEqual(set(Rv32Hart.HARNESS_FIELDS), {"gas"})
        self.assertEqual(set(Rv32Memory.STATE_FIELDS), {"buffer", "lanes", "words_u", "words_i"})
        self.assertEqual(set(Rv32Memory.DERIVED_FIELDS), {"generations", "written", "code"})
        self.assertEqual((set(Uart.STATE_FIELDS), set(Clint.STATE_FIELDS)), ({"output", "input"}, {"mtimecmp", "msip"}))
        self.assertEqual((Bus.STATE_FIELDS, Syscon.STATE_FIELDS, Machine.STATE_FIELDS), ((), (), ()))

    def test_the_gate_flags_an_unclassified_slot(self) -> None:
        class Extended(Rv32Hart):
            __slots__ = ("new_field",)

        class Reclassified(Rv32Hart):
            __slots__ = ()
            STATE_FIELDS = Rv32Hart.STATE_FIELDS + ("gas",)

        class Unruled(Rv32Memory):
            __slots__ = ()
            DERIVED_FIELDS = {**Rv32Memory.DERIVED_FIELDS, "code": ""}

        class Phantom(Uart):
            __slots__ = ()
            CONFIG_FIELDS = ("baud",)

        self.assertEqual(inventory_errors(Extended), ["Extended.new_field sin clasificar"])
        self.assertEqual(inventory_errors(Reclassified), ["Reclassified.gas está en STATE_FIELDS y en HARNESS_FIELDS"])
        self.assertEqual(inventory_errors(Unruled), ["Unruled.code sin regla de invalidación"])
        self.assertEqual(inventory_errors(Phantom), ["CONFIG_FIELDS de Phantom nombra baud, que no es un slot"])

    def test_snapshot_keys_follow_the_inventory(self) -> None:
        machine = make_machine(PROGRAM, PROGRAM_REGS)
        self.addCleanup(machine.close)
        state = machine.snapshot()
        csr_slots = {slot for _, slot in Rv32Hart.CSR_FIELDS}
        self.assertLessEqual(csr_slots, set(Rv32Hart.STATE_FIELDS))                     # cada CSR con estado es un STATE_FIELD
        self.assertEqual(set(state["hart"]), (set(Rv32Hart.STATE_FIELDS) - csr_slots) | {"csrs"})
        self.assertEqual(set(state["hart"]["csrs"]), {f"{number:#05x}" for number, _ in Rv32Hart.CSR_FIELDS})
        self.assertEqual({number for number, _ in Rv32Hart.CSR_FIELDS}, rv32_hart.STATEFUL_CSRS)
        self.assertEqual(len({slot for _, slot in Rv32Hart.CSR_FIELDS}), len(Rv32Hart.CSR_FIELDS))   # un slot por CSR
        self.assertEqual(set(state["uart"]), set(Uart.STATE_FIELDS))
        self.assertEqual(set(state["clint"]), set(Clint.STATE_FIELDS))
        self.assertEqual(set(state["ram"]), {"base", "size", "page_words", "pages"})


class Devices(unittest.TestCase):
    def test_restored_uart_input_and_mtimecmp_are_seen_by_the_guest(self) -> None:
        words = [encode(Kind.LBU, 1, 10, imm=0), encode(Kind.SB, 0, 10, 1, imm=0), encode(Kind.LW, 2, 11, imm=0),
                 encode(Kind.LBU, 3, 10, imm=5), EBREAK]
        source = make_machine(words, {10: UART_BASE, 11: MTIMECMP})
        self.addCleanup(source.close)
        source.uart.feed("QR")
        source.uart.output.extend(b"pre")
        source.clint.mtimecmp = 0xABCDEF
        state = source.snapshot()
        target = Machine(ram_bytes=RAM)
        self.addCleanup(target.close)
        target.restore(state)
        self.assertEqual(target.run(10).reason, "ebreak")
        self.assertEqual((target.hart.regs[1], target.hart.regs[2], target.hart.regs[3]), (ord("Q"), 0xABCDEF, 0x61))
        self.assertEqual((bytes(target.uart.output), list(target.uart.input)), (b"preQ", [ord("R")]))
        self.assertEqual(source.run(10).reason, "ebreak")
        self.assertEqual(target.state_hash(), source.state_hash())
        target.load_flat(assemble([EBREAK]))
        pristine = Machine(ram_bytes=RAM)
        self.addCleanup(pristine.close)
        pristine.load_flat(assemble([EBREAK]))
        self.assertEqual(target.state_hash(), pristine.state_hash())


class BoundedInvariantS(unittest.TestCase):
    """``run(B1); snapshot; restore; run(B2)`` frente al intérprete de referencia, en máquina nueva y usada, con
    cualquier combinación de aceleración en origen y destino (el arnés de E4 con 1 000 cortes es H2.2)."""

    COMBOS = tuple((source, target, warmup) for warmup in (1, 2) for source in (False, True) for target in (False, True))

    def run_cut(self, image: bytes, setup, gas_total: int, cut: int, accelerate_source: bool, accelerate_target: bool,
                warmup: int, stats: dict[str, int]) -> None:
        reference = Reference(image)
        setup(reference.regs)
        expected_stop = reference.run(gas_total)
        expected = (list(reference.regs), reference.pc, reference.count, bytes(reference.mem), expected_stop)
        source = Rv32Memory(RAM)
        self.addCleanup(source.close)
        source.load_bytes(BASE, image)
        hart = Rv32Hart(source)
        setup(hart.regs)
        hart.accelerate, hart.warmup = accelerate_source, warmup
        with recording_blocks() as recorded:
            hart.run(cut)
            self.assertEqual(check_invariants(hart, source, recorded), [])
        self.assertTrue(snapshot_round_trips(source))
        consumed = cut - hart.gas
        state = hart.snapshot()
        digest = hart.state_hash()
        # Destino usado: ejecutó el mismo programa entero con bloques (cachés calientes sobre el mismo código).
        used_memory = Rv32Memory(RAM)
        self.addCleanup(used_memory.close)
        used_memory.load_bytes(BASE, image)
        used = Rv32Hart(used_memory)
        setup(used.regs)
        used.accelerate, used.warmup = True, 1
        used.run(gas_total)
        fresh_memory = Rv32Memory(RAM)
        self.addCleanup(fresh_memory.close)
        fresh = Rv32Hart(fresh_memory)
        for name, target, memory in (("nueva", fresh, fresh_memory), ("usada", used, used_memory)):
            with self.subTest(destino=name):
                target.restore(state)
                self.assertEqual(target.state_hash(), digest)
                self.assertEqual(bytes(memory.lanes), bytes(source.lanes))
                target.accelerate, target.warmup = accelerate_target, warmup
                with recording_blocks() as recorded:
                    stop = target.run(gas_total - cut)
                    self.assertEqual(check_invariants(target, memory, recorded), [])
                self.assertTrue(snapshot_round_trips(memory))
                self.assertEqual((list(target.regs), target.pc, target.instret, bytes(memory.lanes), stop), expected)
                self.assertEqual(consumed + (gas_total - cut) - target.gas, reference.count)
                stats["bloques"] += target.block_instructions > 0
        stop = hart.run(gas_total - cut)                       # y el origen sin restaurar llega a lo mismo
        self.assertEqual((list(hart.regs), hart.pc, hart.instret, bytes(source.lanes), stop), expected)
        self.assertEqual(hart.state_hash(), fresh.state_hash())

    def test_random_and_hostile_programs(self) -> None:
        rng = random.Random(20261019)
        stats = {"bloques": 0, "cortes": 0}
        for index in range(80):
            if index % 3 == 2:
                image, regs = blocks_tests.HostileProgramsAgainstReference.generate(rng)

                def setup(target, regs=regs):
                    for register, value in regs.items():
                        target[register] = value
            else:
                words = random_program(rng, rng.randrange(20, 200))
                if index % 3 == 1:
                    words = hostile_layout(words, rng, 0x40, max(64, 4 * len(words) - 0x40))
                image = assemble(words)

                def setup(target):
                    pass
            gas_total = 600 if index % 3 != 2 else 3000
            for cut in (rng.randrange(1, 40), rng.randrange(1, gas_total)):
                for accelerate_source, accelerate_target, warmup in self.COMBOS:
                    with self.subTest(program=index, cut=cut, origen=accelerate_source, destino=accelerate_target, warmup=warmup):
                        self.run_cut(image, setup, gas_total, cut, accelerate_source, accelerate_target, warmup, stats)
                        stats["cortes"] += 1
        self.assertGreater(stats["bloques"], 300, stats)

    def test_the_anchor_programs_of_the_blind_reviews_survive_cuts(self) -> None:
        rng = random.Random(7)
        stats = {"bloques": 0}
        programs = [
            ([encode(Kind.JAL, 0, imm=0x20)] + [EBREAK] * 7 + [encode(Kind.ADDI, 1, 1, imm=1), encode(Kind.SB, 0, 10, 5, imm=2),
             encode(Kind.ADDI, 20, 20, imm=-1), encode(Kind.BNE, 0, 20, 0, imm=-12), EBREAK], {10: CODE + 0x20, 5: 0x30, 20: 2}),
            ([encode(Kind.JAL, 0, imm=0x20)] + [EBREAK] * 7 + [encode(Kind.SW, 0, 10, 5, imm=0), encode(Kind.ADDI, 20, 20, imm=-1),
             encode(Kind.BNE, 0, 20, 0, imm=-8), EBREAK], {10: CODE + 0x20, 5: encode(Kind.ADDI, 1, 1, imm=5), 20: 2}),
            ([encode(Kind.JAL, 0, imm=0x20)] + [EBREAK] * 7 + [encode(Kind.ADDI, 20, 20, imm=-1), encode(Kind.XORI, 5, 5, imm=1),
             encode(Kind.SB, 0, 11, 5, imm=3), encode(Kind.BNE, 0, 20, 0, imm=-12), EBREAK], {11: CODE + 0x2C, 5: 0xFE, 16: 2, 20: 4}),
            ([encode(Kind.JAL, 0, imm=0x20)] + [EBREAK] * 7 + [encode(Kind.SW, 0, 11, 5, imm=0), encode(Kind.ADD, 5, 5, 6),
             encode(Kind.JAL, 0, imm=0x18), EBREAK, EBREAK, EBREAK, EBREAK, EBREAK, encode(Kind.ADDI, 1, 1, imm=0),
             encode(Kind.ADDI, 20, 20, imm=-1), encode(Kind.BNE, 0, 20, 0, imm=-0x28), EBREAK],
             {11: CODE + 0x40, 5: encode(Kind.ADDI, 1, 1, imm=1), 6: 0x00100000, 20: 3}),
            (blocks_tests.SelfModifyingCode.LOOP_WITH_STORE, {9: CODE + 0x18, 8: 100}),
            (blocks_tests.SelfModifyingCode.LOOP_WITH_STORE, {9: CODE + 0x100, 8: 100}),
        ]
        for index, (words, regs) in enumerate(programs):
            image = assemble(words)

            def setup(target, regs=regs):
                for register, value in regs.items():
                    target[register] = value

            for cut in sorted({1, 2, 3, 5, 8, 13, 21, rng.randrange(1, 60), rng.randrange(1, 400)}):
                for accelerate_source, accelerate_target, warmup in self.COMBOS:
                    with self.subTest(program=index, cut=cut, origen=accelerate_source, destino=accelerate_target, warmup=warmup):
                        self.run_cut(image, setup, 600, cut, accelerate_source, accelerate_target, warmup, stats)
        self.assertGreater(stats["bloques"], 100, stats)


class MmioUnderCuts(unittest.TestCase):
    """La invariante S acotada con MMIO (la clase anterior usa harts sueltos): UART con cola de entrada, mtimecmp y
    apagado por SYSCON, con todos los cortes, en máquina nueva y usada y con todas las combinaciones de aceleración en origen y destino."""

    def test_mmio_program_under_every_cut(self) -> None:
        words = [encode(Kind.LBU, 1, 10, imm=5), encode(Kind.LBU, 2, 10, imm=0), encode(Kind.SB, 0, 10, 2, imm=0),
                 encode(Kind.SW, 0, 11, 2, imm=0), encode(Kind.LW, 3, 11, imm=0), encode(Kind.SW, 0, 12, 3, imm=0),
                 encode(Kind.ADDI, 20, 20, imm=-1), encode(Kind.BNE, 0, 20, 0, imm=-28),
                 encode(Kind.LUI, 13, imm=0x11100000), encode(Kind.ADDI, 14, 0, imm=0x555), encode(Kind.SLLI, 14, 14, imm=4),
                 encode(Kind.ADDI, 14, 14, imm=5), encode(Kind.SW, 0, 13, 14, imm=0), EBREAK]
        regs = {10: UART_BASE, 11: MTIMECMP, 12: DATA, 20: 5}
        whole = make_machine(words, regs, time_divisor=3)
        self.addCleanup(whole.close)
        whole.uart.feed("abcdefgh")
        self.assertEqual(whole.run(1000), Stop("apagado"))
        total = whole.hart.instret
        expected = (whole.state_hash(), bytes(whole.uart.output), list(whole.uart.input), whole.clint.mtimecmp)
        self.assertEqual(expected[1], b"abcde")
        for cut in range(1, total):
            for accelerate_source, accelerate_target in ((False, True), (True, False), (True, True)):
                with self.subTest(cut=cut, origen=accelerate_source, destino=accelerate_target):
                    source = make_machine(words, regs, time_divisor=3, accelerate=accelerate_source, warmup=1)
                    self.addCleanup(source.close)
                    source.uart.feed("abcdefgh")
                    self.assertEqual(source.run(cut), Stop("gas"))
                    used = make_machine(words, regs, accelerate=True, warmup=1)   # el mismo programa, otro divisor, entero
                    self.addCleanup(used.close)
                    used.uart.feed("abcdefgh")
                    self.assertEqual(used.run(1000), Stop("apagado"))
                    fresh = Machine(ram_bytes=RAM)
                    self.addCleanup(fresh.close)
                    for target in (fresh, used):
                        target.restore(source.snapshot())
                        target.hart.accelerate, target.hart.warmup = accelerate_target, 1
                        self.assertEqual(target.run(1000 - cut), Stop("apagado"))
                        self.assertEqual((target.state_hash(), bytes(target.uart.output), list(target.uart.input),
                                          target.clint.mtimecmp, target.hart.instret), expected + (total,))


class ReviewFindings(unittest.TestCase):
    """Anclas de los hallazgos del oráculo y de la ciega 1 de H2.1 (cada una mataba un mutante vivo o fijaba un
    comportamiento que la suite no cubría)."""

    def machine(self, **kwargs) -> Machine:
        machine = Machine(ram_bytes=RAM, **kwargs)
        self.addCleanup(machine.close)
        return machine

    def test_restore_empties_the_warmup_and_recompilation_counters(self) -> None:
        hot = make_machine(PATCH_LOOP, {10: CODE, 5: PATCHED}, accelerate=True, warmup=2)
        self.addCleanup(hot.close)
        self.assertEqual(hot.run(200).reason, "ebreak")
        self.assertTrue(hot.hart.block_hits and hot.hart.recompilations)
        hot.restore(self.machine().snapshot())
        self.assertEqual((hot.hart.block_hits, hot.hart.recompilations), ({}, {}))

    def test_restore_keeps_the_regs_list_object(self) -> None:
        machine = self.machine()
        state = machine.snapshot()
        regs = machine.hart.regs
        regs[5] = 7
        machine.restore(state)
        self.assertIs(machine.hart.regs, regs)
        self.assertEqual(regs[5], 0)

    def test_64_bit_counters_and_a_host_pc_out_of_range(self) -> None:
        machine = self.machine()
        state = machine.snapshot()
        state["hart"]["instret"] = (1 << 40) + 7
        machine.restore(state)
        self.assertEqual((machine.hart.instret, machine.snapshot()), ((1 << 40) + 7, state))
        machine.hart.instret = (1 << 64) - 1
        self.assertEqual(machine.snapshot()["hart"]["instret"], (1 << 64) - 1)
        self.machine().restore(machine.snapshot())                     # el máximo representable se restaura
        machine.hart.pc = (1 << 32) + 8
        self.assertEqual(machine.snapshot()["hart"]["pc"], 8)          # el host puso un pc fuera de rango: se enmascara
        machine.clint.mtimecmp = (1 << 64) - 3
        other = self.machine()
        other.restore(machine.snapshot())
        self.assertEqual(other.clint.mtimecmp, (1 << 64) - 3)

    def test_restore_rejects_what_it_could_not_serialize(self) -> None:
        machine = self.machine()
        good = machine.snapshot()
        for key, value in (("instret", 1 << 64), ("instret", 10 ** 5000), ("time_divisor", 1 << 64), ("time_divisor", 10 ** 5000)):
            state = copy.deepcopy(good)
            state["hart"][key] = value
            with self.subTest(key=key, bits=value.bit_length()):
                with self.assertRaises(ValueError):
                    machine.restore(state)
                self.assertEqual(machine.snapshot(), good)
                machine.snapshot_bytes()

    def test_from_snapshot_bytes_adopts_the_base_and_accepts_a_limit_equal_to_the_ram(self) -> None:
        source = Machine(ram_bytes=RAM, base=0x10000000, time_divisor=5)
        self.addCleanup(source.close)
        source.memory.store32(0x10000000 + 8, 0xDEADBEEF)
        clone = Machine.from_snapshot_bytes(source.snapshot_bytes(), max_ram_bytes=RAM)
        self.addCleanup(clone.close)
        self.assertEqual((clone.memory.base, clone.memory.load32(0x10000000 + 8)), (0x10000000, 0xDEADBEEF))
        self.assertEqual((clone.hart.time_divisor, clone.time_divisor), (5, 1))   # estado adoptado; configuración no

    def test_machine_restore_is_atomic_with_an_exported_uart_buffer(self) -> None:
        source = make_machine(PROGRAM, PROGRAM_REGS)
        self.addCleanup(source.close)
        self.assertEqual(source.run(100).reason, "ebreak")
        target = self.machine()
        target.uart.output.extend(b"ab")
        old = target.uart.output
        view = memoryview(old)                                           # una vista viva impide redimensionar «old»
        try:
            target.restore(source.snapshot())
            self.assertEqual((bytes(target.uart.output), view.tobytes()), (b"A", b"ab"))
            self.assertEqual(target.snapshot(), source.snapshot())
        finally:
            view.release()

    def test_load_flat_after_a_restore_returns_to_the_constructor_divisor(self) -> None:
        image = assemble([encode(Kind.ADDI, 2, 0, imm=3), EBREAK])
        source = self.machine(time_divisor=7)
        source.load_flat(image)
        self.assertEqual(source.run(5).reason, "ebreak")
        for divisor in (1, 4):
            with self.subTest(divisor=divisor):
                target = self.machine(time_divisor=divisor)
                target.restore(source.snapshot())
                self.assertEqual((target.hart.time_divisor, target.time_divisor), (7, divisor))
                target.load_flat(image)
                pristine = self.machine(time_divisor=divisor)
                pristine.load_flat(image)
                self.assertEqual((target.hart.time_divisor, target.state_hash()), (divisor, pristine.state_hash()))

    def test_csr_keys_have_a_single_spelling(self) -> None:
        # v2 (H3.0): csrs lleva exactamente las siete claves canónicas; cualquier otra grafía de "0x300" es una clave
        # de más y otra de menos. En v1, csrs vacío.
        memory = Rv32Memory(RAM)
        self.addCleanup(memory.close)
        hart = Rv32Hart(memory)
        for key in ("300", "0X300", " 0x300 ", "0x_3_00", "+0x300", "0x0300", "0x30", 0x300, "0x30g"):
            state = hart.snapshot()
            state["hart"]["csrs"][key] = state["hart"]["csrs"].pop("0x300")
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    hart.restore(state)
        state = hart.snapshot()
        state["hart"]["csrs"]["0x300"] = rv32_isa.MSTATUS_MIE         # la grafía canónica entra, y el valor se guarda
        hart.restore(state)
        self.assertEqual((hart.mstatus, hart.snapshot()["hart"]["csrs"]["0x300"]), (rv32_isa.MSTATUS_MIE,) * 2)
        state["version"] = 1
        del state["hart"]["privilege"]
        state["hart"]["csrs"] = {"0x300": 1}
        with self.assertRaises(ValueError):                         # un snapshot v1 no lleva CSR con estado
            hart.restore(state)
        state["hart"]["csrs"] = {}
        hart.restore(state)
        self.assertEqual(hart.mstatus, 0)                           # v1: los CSR quedan en su reinicio

    def test_the_validator_reads_the_registers_once(self) -> None:
        class Shifty(list):
            calls = 0

            def __iter__(self):
                Shifty.calls += 1
                return super().__iter__() if Shifty.calls <= 1 else iter([0] + [1 << 40] * 31)

        machine = self.machine()
        state = machine.snapshot()
        state["hart"]["regs"] = Shifty(state["hart"]["regs"])
        machine.hart.restore({key: state[key] for key in ("version", "hart", "ram")})
        self.assertTrue(all(0 <= value < (1 << 32) for value in machine.hart.regs))
        machine.snapshot_bytes()

    def test_restore_on_a_closed_machine_is_refused(self) -> None:
        machine = self.machine()
        state = machine.snapshot()
        state["hart"]["regs"][3] = 99
        machine.close()
        with self.assertRaises(ValueError):
            machine.restore(state)
        self.assertEqual(machine.hart.regs[3], 0)

    def test_incomparable_extra_keys_are_still_a_value_error(self) -> None:
        machine = self.machine()
        state = machine.snapshot()
        state["extra"] = 1
        state[5] = 2
        with self.assertRaises(ValueError):
            machine.restore(state)
        with self.assertRaises(ValueError):
            machine.hart.restore({key: state[key] for key in ("version", "hart", "ram", "extra", 5)})

    def test_snapshot_bytes_is_zlib_level_9_of_the_canonical_json(self) -> None:
        machine = self.machine()
        self.assertEqual(machine.snapshot_bytes(), SNAPSHOT_MAGIC + zlib.compress(canonical_json(machine.snapshot()), 9))

    def test_the_classification_of_every_field_is_pinned_and_every_slotted_class_is_inventoried(self) -> None:
        for cls, expected in GOLDEN.items():
            for category, fields in expected.items():
                with self.subTest(cls=cls.__name__, category=category):
                    self.assertEqual(set(getattr(cls, category)), fields)
        # Toda clase con slots de los módulos rv32_* pasa por la puerta (Stop es una NamedTuple sin slots propios).
        missing = []
        for module in RV32_MODULES:
            for name, cls in vars(module).items():
                if isinstance(cls, type) and cls.__module__ == module.__name__ and "__slots__" in cls.__dict__ \
                        and not issubclass(cls, (tuple, BaseException)) \
                        and cls not in INVENTORIED:
                    missing.append(f"{module.__name__}.{name}")
        self.assertEqual(missing, [])
        machine = self.machine()
        for instance in (machine, machine.hart, machine.memory, machine.uart, machine.clint, machine.bus, machine.syscon):
            self.assertFalse(hasattr(instance, "__dict__"), type(instance).__name__)   # sin atributos dinámicos


class SecondBlindReview(unittest.TestCase):
    """Anclas de la ciega 2 de H2.1: objetos hostiles que cambian entre validar y usar, tipos estrictos de la
    geometría, claves CSR en minúscula, registros como lista, divisor de 64 bits, pc exótico, reserva sucia,
    interrupciones en from_snapshot_bytes, constantes del formato, zlib de nivel 9 con memoria densa, instret que
    envuelve, load_flat con una vista exportada y tope negativo."""

    def machine(self, **kwargs) -> Machine:
        machine = Machine(ram_bytes=RAM, **kwargs)
        self.addCleanup(machine.close)
        return machine

    def test_hostile_objects_cannot_slip_unvalidated_values_past_restore(self) -> None:
        class Shifty(list):
            """Lista que itera sus elementos la primera vez y otros distintos después."""

            def __init__(self, items, second):
                super().__init__(items)
                self.calls, self.second = 0, list(second)

            def __iter__(self):
                self.calls += 1
                return list.__iter__(self) if self.calls == 1 else iter(self.second)

        class ShiftyDict(dict):
            """Diccionario que devuelve otro valor para una clave a partir de la segunda lectura."""

            def __init__(self, mapping, key, second):
                super().__init__(mapping)
                self.key, self.second, self.reads = key, second, 0

            def __getitem__(self, name):
                if name == self.key:
                    self.reads += 1
                    if self.reads > 1:
                        return self.second
                return dict.__getitem__(self, name)

        source = make_machine(PROGRAM, PROGRAM_REGS)
        self.addCleanup(source.close)
        self.assertEqual(source.run(100).reason, "ebreak")
        good = source.snapshot()
        # (a) salida del UART que cambia en la segunda iteración: o se rechaza sin tocar nada, o se restaura lo validado.
        target = self.machine()
        before = full_state(target)
        state = copy.deepcopy(good)
        state["uart"]["output"] = Shifty([65, 66], [300, 300])
        try:
            target.restore(state)
        except ValueError:
            self.assertEqual(full_state(target), before)
        else:
            self.assertEqual(bytes(target.uart.output), b"AB")
            target.snapshot_bytes()
        # (b) pc que cambia en la segunda lectura: se restaura el valor validado.
        state = copy.deepcopy(good)
        state["hart"] = ShiftyDict(state["hart"], "pc", "no-es-un-entero")
        target = self.machine()
        target.restore(state)
        self.assertEqual(target.hart.pc, good["hart"]["pc"])
        target.snapshot_bytes()
        # (c) página cuyos elementos reales no son los que itera: o rechazo atómico, o exactamente lo validado.
        state = copy.deepcopy(good)
        state["ram"]["pages"] = [[0, Shifty([1, 2, 3], [1 << 40, 1 << 40, 1 << 40])]]
        target = self.machine()
        before = full_state(target)
        try:
            target.restore(state)
        except ValueError:
            self.assertEqual(full_state(target), before)
        else:
            self.assertEqual([target.memory.load32(BASE + 4 * i) for i in range(3)], [1, 2, 3])
            target.snapshot_bytes()

    def test_geometry_types_are_strict_in_restore_and_from_snapshot_bytes(self) -> None:
        machine = self.machine()
        good = machine.snapshot()
        for key, value in (("base", float(BASE)), ("size", float(RAM)), ("page_words", 4096.0), ("size", str(RAM)),
                           ("base", None), ("size", None), ("page_words", True)):
            state = copy.deepcopy(good)
            state["ram"][key] = value
            with self.subTest(key=key, value=repr(value)):
                with self.assertRaises(ValueError):
                    machine.restore(state)
                with self.assertRaises(ValueError):
                    Machine.from_snapshot_bytes(encode_snapshot(state))
        low = Machine(ram_bytes=RAM, base=0)
        self.addCleanup(low.close)
        state = low.snapshot()
        state["ram"]["base"] = False                                  # False == 0, pero no es un entero
        with self.assertRaises(ValueError):
            low.restore(state)
        state = copy.deepcopy(good)
        state["hart"]["regs"] = tuple(state["hart"]["regs"])
        with self.assertRaises(ValueError):
            machine.restore(state)
        self.assertEqual(machine.snapshot(), good)

    def test_csr_keys_must_be_lowercase_hex(self) -> None:
        memory = Rv32Memory(RAM)
        self.addCleanup(memory.close)
        hart = Rv32Hart(memory)
        for key in ("0X305", "0x30F", "0x3O5", " 0x305"):            # v2: solo "0x305", en minúscula y sin adornos
            state = hart.snapshot()
            state["hart"]["csrs"][key] = state["hart"]["csrs"].pop("0x305")
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    hart.restore(state)
        for key in ("0xc00", "0x344", "0x301"):                         # contador, mip y misa: CSR sin estado propio
            state = hart.snapshot()
            state["hart"]["csrs"][key] = 0
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    hart.restore(state)
        state = hart.snapshot()
        state["hart"]["csrs"]["0x305"] = BASE + 0x100
        hart.restore(state)
        self.assertEqual(hart.mtvec, BASE + 0x100)

    def test_time_divisor_of_64_bits_round_trips(self) -> None:
        for divisor in (1 << 32, (1 << 40) + 3, (1 << 64) - 1):
            with self.subTest(divisor=divisor):
                source = Machine(ram_bytes=RAM, time_divisor=divisor)
                self.addCleanup(source.close)
                state = source.snapshot()
                target = self.machine()
                target.restore(state)
                self.assertEqual((target.hart.time_divisor, target.snapshot()), (divisor, state))

    def test_misaligned_and_out_of_ram_pcs_survive_a_restore(self) -> None:
        words = [encode(Kind.ADDI, 1, 1, imm=1)] * 4 + [EBREAK]
        for pc in (BASE + 1, BASE + 2, BASE + 6, 0, 0xFFFFFFFC, UART_BASE, BASE + RAM):
            with self.subTest(pc=hex(pc)):
                source = make_machine(words)
                self.addCleanup(source.close)
                source.hart.pc = pc
                control = make_machine(words)
                self.addCleanup(control.close)
                control.hart.pc = pc
                target = self.machine()
                target.restore(source.snapshot())
                self.assertEqual(target.hart.pc, pc)
                self.assertEqual((target.run(3), target.state_hash()), (control.run(3), control.state_hash()))

    def test_restore_and_reset_clear_a_reservation_the_host_set(self) -> None:
        machine = self.machine()
        good = machine.snapshot()
        machine.hart.reservation = BASE
        machine.restore(good)
        self.assertIsNone(machine.hart.reservation)
        machine.hart.reservation = BASE
        machine.hart.reset()
        self.assertIsNone(machine.hart.reservation)

    def test_from_snapshot_bytes_closes_the_machine_on_any_interruption(self) -> None:
        data = self.machine().snapshot_bytes()
        for exc in (KeyboardInterrupt, SystemExit, RuntimeError):
            with self.subTest(exc=exc.__name__):
                with mock.patch.object(Machine, "restore", side_effect=exc), \
                        mock.patch.object(Machine, "close", autospec=True) as closed:
                    with self.assertRaises(exc):
                        Machine.from_snapshot_bytes(data)
                closed.assert_called_once()
                closed.call_args.args[0].memory.close()

    def test_wire_format_constants_are_pinned(self) -> None:
        self.assertEqual((SNAPSHOT_MAGIC, SNAPSHOT_VERSION, MAX_SNAPSHOT_RAW_BYTES), (b"RV32S\x01", 2, 256 * 1024 * 1024))
        self.assertEqual(rv32_hart.SNAPSHOT_VERSIONS, frozenset({1, 2}))      # H3.0: emite v2 y lee v1 y v2

    def test_snapshot_bytes_is_level_9_on_dense_memory(self) -> None:
        machine = self.machine()
        rng = random.Random(8)
        machine.memory.fill([rng.getrandbits(32) for _ in range(RAM // 4)], BASE)
        raw = canonical_json(machine.snapshot())
        self.assertGreater(len(raw), 100_000)
        self.assertEqual(machine.snapshot_bytes(), SNAPSHOT_MAGIC + zlib.compress(raw, 9))
        self.assertNotEqual(machine.snapshot_bytes(), SNAPSHOT_MAGIC + zlib.compress(raw, 1))

    def test_instret_wraps_at_64_bits_so_a_restored_hart_stays_restorable(self) -> None:
        machine = make_machine([encode(Kind.ADDI, 1, 1, imm=1)] * 4 + [EBREAK])
        self.addCleanup(machine.close)
        state = machine.snapshot()
        state["hart"]["instret"] = (1 << 64) - 1
        machine.restore(state)
        self.assertEqual(machine.run(2), Stop("gas"))
        self.assertEqual(machine.snapshot()["hart"]["instret"], 1)        # 2⁶⁴ + 1 envuelve, como el contador
        self.assertEqual(machine.hart.read_csr(CSR_INSTRET), 1)
        self.machine().restore(machine.snapshot())

    def test_load_flat_with_an_exported_uart_view_is_whole(self) -> None:
        machine = make_machine(PROGRAM, PROGRAM_REGS)
        self.addCleanup(machine.close)
        self.assertEqual(machine.run(100).reason, "ebreak")
        view = memoryview(machine.uart.output)
        try:
            machine.load_flat(assemble([EBREAK]))
            self.assertEqual((bytes(machine.uart.output), view.tobytes(), machine.hart.pc, machine.hart.instret),
                             (b"", b"A", CODE, 0))
        finally:
            view.release()

    def test_a_negative_or_non_integer_size_cap_is_rejected(self) -> None:
        data = self.machine().snapshot_bytes()
        for maximum in (-1, -100, True, 1.5, "10", None):
            with self.subTest(maximum=repr(maximum)):
                if maximum is not None:                                    # None es «el tope predeterminado»
                    with self.assertRaises(ValueError):
                        decode_snapshot(data, maximum)
                with self.assertRaises(ValueError):
                    Machine.from_snapshot_bytes(data, max_ram_bytes=maximum)
        self.assertEqual(decode_snapshot(data, None), decode_snapshot(data))
        Machine.from_snapshot_bytes(data, max_ram_bytes=RAM).close()
        with self.assertRaises(ValueError):
            Machine.from_snapshot_bytes(data, max_ram_bytes=RAM - 1)


class GateHardening(unittest.TestCase):
    """La puerta de inventario (P2.5) también vigila lo que la introspección de slots no ve: módulos rv32_* no listados,
    subclases sin slots, atributos de clase mutables y campos de estado que el snapshot no lleve (ciega 2)."""

    def test_every_rv32_module_is_inventoried(self) -> None:
        found = {info.name for info in pkgutil.iter_modules(cpu_digital.__path__) if info.name.startswith("rv32_")}
        self.assertEqual(found, {module.__name__.rsplit(".", 1)[-1] for module in RV32_MODULES})

    def test_subclasses_of_inventoried_classes_declare_slots(self) -> None:
        for module in RV32_MODULES:
            for name, cls in vars(module).items():
                if isinstance(cls, type) and cls.__module__ == module.__name__ and issubclass(cls, INVENTORIED):
                    self.assertIn("__slots__", vars(cls), name)

    def test_inventoried_classes_have_no_mutable_class_attributes(self) -> None:
        allowed = (tuple, str, int, float, bool, frozenset, type(None))
        skip = (types.FunctionType, staticmethod, classmethod, property, types.MemberDescriptorType)
        for cls in INVENTORIED:
            for name, value in vars(cls).items():
                if name.startswith("__") or name == "DERIVED_FIELDS" or isinstance(value, skip):
                    continue
                self.assertIsInstance(value, allowed, f"{cls.__name__}.{name}")

    def test_every_state_field_of_the_hart_is_carried_by_the_snapshot(self) -> None:
        # Cada perturbación pone un valor legal distinto del de reinicio (H3.0: CSR de modo M, privilegio y reserva).
        perturb = {"regs": lambda h: h.regs.__setitem__(7, 0xDEADBEEF), "pc": lambda h: setattr(h, "pc", BASE + 12),
                   "instret": lambda h: setattr(h, "instret", (1 << 40) + 5),
                   "reservation": lambda h: setattr(h, "reservation", BASE + 8),
                   "time_divisor": lambda h: setattr(h, "time_divisor", 9),
                   "privilege": lambda h: setattr(h, "privilege", rv32_isa.PRIV_U),
                   "mstatus": lambda h: setattr(h, "mstatus", rv32_isa.MSTATUS_MPIE | rv32_isa.MSTATUS_MPP | rv32_isa.MSTATUS_TW),
                   "mie": lambda h: setattr(h, "mie", rv32_isa.MIE_MTIE | rv32_isa.MIE_MEIE),
                   "mtvec": lambda h: setattr(h, "mtvec", BASE + 0x101),
                   "mscratch": lambda h: setattr(h, "mscratch", 0x12345678),
                   "mepc": lambda h: setattr(h, "mepc", BASE + 0x40),
                   "mcause": lambda h: setattr(h, "mcause", 0x80000007),
                   "mtval": lambda h: setattr(h, "mtval", 0xCAFEBABE)}
        self.assertEqual(set(perturb), set(Rv32Hart.STATE_FIELDS))   # un campo de estado nuevo obliga a añadir su perturbación
        pristine = Rv32Memory(RAM)
        self.addCleanup(pristine.close)
        pristine_hash = Rv32Hart(pristine).state_hash()
        csr_keys = {slot: f"{number:#05x}" for number, slot in Rv32Hart.CSR_FIELDS}
        for field, change in perturb.items():
            with self.subTest(field=field):
                memory = Rv32Memory(RAM)
                self.addCleanup(memory.close)
                hart = Rv32Hart(memory)
                reset_value = getattr(hart, field) if field != "regs" else list(hart.regs)
                change(hart)
                self.assertNotEqual(getattr(hart, field), reset_value)                    # la perturbación no es vacua
                carried = hart.snapshot()["hart"]
                if field in csr_keys:
                    self.assertEqual(carried["csrs"][csr_keys[field]], getattr(hart, field))   # el snapshot lo lleva
                elif field == "privilege":
                    self.assertEqual(carried["privilege"], "U")
                else:
                    self.assertEqual(carried[field], getattr(hart, field))
                self.assertNotEqual(hart.state_hash(), pristine_hash)                     # y el hash lo ve
                other = Rv32Memory(RAM)
                self.addCleanup(other.close)
                twin = Rv32Hart(other)
                twin.restore(hart.snapshot())
                self.assertEqual(getattr(twin, field), getattr(hart, field))             # y restore lo restaura
                self.assertEqual(twin.state_hash(), hart.state_hash())


class ThirdBlindReview(unittest.TestCase):
    """Anclas de la ciega 3 de H2.1: time/timeh/mtime iguales con y sin corte cuando instret cruza 2⁶⁴ (B-1), con el
    cruce en el origen, en el corte o en el destino (la ciega 4 vio que con un solo corte los relojes se leían en el
    origen y la comparación era vacua); el
    constructor rechaza divisores que el snapshot no representa; tope de decode_snapshot de cualquier tamaño; un
    mapeo que cambia entre lecturas; restauración fallida que no toca divisor, reserva ni registros; la reserva en el
    snapshot y en el hash; x0 reparado; todos los registros y todas las claves CSR validados; bytes-like aceptados;
    estados y secciones que no son diccionarios; geometría de from_snapshot_bytes; tope por defecto de 64 MiB;
    identidad de la cola del UART."""

    def machine(self, **kwargs) -> Machine:
        machine = Machine(ram_bytes=RAM, **kwargs)
        self.addCleanup(machine.close)
        return machine

    def test_time_survives_a_cut_across_the_64_bit_instret_wrap(self) -> None:
        words = [encode(Kind.ADDI, 1, 1, imm=1)] * 6 + [encode(Kind.CSRRS, 5, 0, imm=0xC01), encode(Kind.CSRRS, 6, 0, imm=0xC81),
                                                          encode(Kind.LW, 7, 10, imm=0), encode(Kind.LW, 8, 10, imm=4), EBREAK]
        image = assemble(words)
        for divisor in (2, 3, 1000):
            # El cruce de 2⁶⁴ cae en el destino (corte 4), justo en el corte (5) o en el origen (6, 7); con 4–6 los relojes
            # se leen en el destino, que es lo que vigila B-1 (con solo el corte 7 se leían en el origen: ciega 4).
            for cut in (4, 5, 6, 7):
                with self.subTest(divisor=divisor, cut=cut):
                    def start() -> Machine:
                        machine = self.machine(time_divisor=divisor)
                        machine.load_flat(image)
                        machine.hart.regs[10] = CLINT_BASE + CLINT_MTIME
                        state = machine.snapshot()
                        state["hart"]["instret"] = (1 << 64) - 5
                        machine.restore(state)
                        return machine

                    control = start()
                    control.run(100)
                    source = start()
                    source.run(cut)
                    self.assertLess(source.hart.instret, 1 << 64)   # run envuelve el contador
                    dest = self.machine()
                    dest.restore(source.snapshot())
                    dest.run(100)
                    self.assertEqual(dest.hart.regs[5:9], control.hart.regs[5:9])   # time, timeh, mtime bajo y alto
                    self.assertEqual((dest.state_hash(), dest.hart.instret), (control.state_hash(), control.hart.instret))

    def test_the_constructor_only_accepts_divisors_the_snapshot_can_carry(self) -> None:
        for divisor in (1 << 64, (1 << 64) + 7, 1 << 100, 0, -1, True, 1.0):
            with self.subTest(divisor=repr(divisor)):
                with self.assertRaises(ValueError):
                    Machine(ram_bytes=RAM, time_divisor=divisor)
        machine = self.machine(time_divisor=(1 << 64) - 1)
        machine.restore(machine.snapshot())

    def test_decode_snapshot_accepts_a_cap_of_any_size(self) -> None:
        machine = self.machine()
        state, data = machine.snapshot(), machine.snapshot_bytes()
        for maximum in (sys.maxsize - 1, sys.maxsize, 1 << 63, 1 << 64, 10 ** 30):
            with self.subTest(maximum=maximum):
                self.assertEqual(decode_snapshot(data, maximum), state)

    def test_a_mapping_that_changes_between_reads_restores_what_was_validated(self) -> None:
        class SecondRead(dict):
            reads = 0

            def __getitem__(self, key):
                if key == "regs":
                    SecondRead.reads += 1
                    if SecondRead.reads > 1:
                        return 5
                return dict.__getitem__(self, key)

        machine = make_machine(PROGRAM, PROGRAM_REGS)
        self.addCleanup(machine.close)
        state = machine.hart.snapshot()
        state["hart"] = SecondRead(state["hart"])
        target = self.machine()
        target.hart.restore(state)
        self.assertEqual(target.hart.regs, machine.hart.regs)

    def test_a_failed_restore_keeps_time_divisor_reservation_and_registers(self) -> None:
        good = self.machine(time_divisor=7).snapshot()
        for bad_pages in ([[0, [1 << 31]]], [[0, []]], [[9, [1]]], [[0, [1]], [0, [2]]], "x", None):
            for route in ("machine", "hart"):
                target = self.machine(time_divisor=5)
                target.hart.reservation = BASE + 4
                target.hart.regs[3] = 99
                target.hart.pc = BASE + 8
                target.hart.instret = 12
                state = copy.deepcopy(good)
                state["ram"]["pages"] = bad_pages
                if route == "hart":
                    state = {key: state[key] for key in ("version", "hart", "ram")}
                before = (target.hart.time_divisor, target.hart.reservation, list(target.hart.regs), target.hart.pc,
                          target.hart.instret)
                with self.subTest(pages=repr(bad_pages)[:20], route=route):
                    with self.assertRaises(ValueError):
                        (target if route == "machine" else target.hart).restore(state)
                    self.assertEqual((target.hart.time_divisor, target.hart.reservation, list(target.hart.regs),
                                      target.hart.pc, target.hart.instret), before)

    def test_snapshot_reports_the_reservation_the_host_set(self) -> None:
        machine = self.machine()
        before = machine.state_hash()
        machine.hart.reservation = BASE + 8
        self.assertEqual(machine.snapshot()["hart"]["reservation"], BASE + 8)
        self.assertNotEqual(machine.state_hash(), before)

    def test_restore_repairs_an_x0_the_host_dirtied(self) -> None:
        machine = self.machine()
        good = machine.snapshot()
        machine.hart.regs[0] = 5
        machine.restore(good)
        self.assertEqual(machine.hart.regs[0], 0)
        self.assertEqual(machine.snapshot(), good)

    def test_every_register_slot_is_validated(self) -> None:
        machine = self.machine()
        good = machine.snapshot()
        for index in range(32):
            for value in (1 << 32, -1, True, 1.0, None, "1"):
                state = copy.deepcopy(good)
                state["hart"]["regs"][index] = value
                with self.subTest(index=index, value=repr(value)):
                    with self.assertRaises(ValueError):
                        machine.restore(state)
                    self.assertEqual(machine.snapshot(), good)

    def test_every_csr_key_is_validated_in_every_position(self) -> None:
        machine = self.machine()
        good = machine.snapshot()
        complete = dict(good["hart"]["csrs"])
        # v2: una clave de más en cualquier posición del diccionario, o una de menos, se rechaza.
        for extra in ("bad", "0X300", "0x3000", "0x344"):
            for first in (True, False):
                csrs = {extra: 2, **complete} if first else {**complete, extra: 2}
                state = copy.deepcopy(good)
                state["hart"]["csrs"] = csrs
                with self.subTest(extra=extra, first=first):
                    with self.assertRaises(ValueError):
                        machine.restore(state)
        for missing in complete:
            state = copy.deepcopy(good)
            del state["hart"]["csrs"][missing]
            with self.subTest(missing=missing):
                with self.assertRaises(ValueError):
                    machine.restore(state)
        state = copy.deepcopy(good)
        state["hart"]["csrs"]["0x340"] = 1
        machine.restore(state)
        self.assertEqual(machine.hart.mscratch, 1)

    def test_decode_snapshot_and_restore_bytes_accept_every_bytes_like(self) -> None:
        machine = self.machine()
        state = machine.snapshot()
        data = machine.snapshot_bytes()
        for wrap in (bytes, bytearray, memoryview):
            with self.subTest(wrap=wrap.__name__):
                self.assertEqual(decode_snapshot(wrap(data)), state)
                other = self.machine()
                other.restore_bytes(wrap(data))
                self.assertEqual(other.snapshot(), state)
                Machine.from_snapshot_bytes(wrap(data)).close()
        for bad in ("RV32S\x01", 5, None, [1, 2], 1.5):
            with self.assertRaises(ValueError):
                decode_snapshot(bad)

    def test_non_mapping_states_and_sections_are_value_errors(self) -> None:
        machine = self.machine()
        good = machine.snapshot()
        for bad in (None, 5, 1.5, "x", [], (), b"", True):
            with self.subTest(state=repr(bad)):
                with self.assertRaises(ValueError):
                    machine.restore(bad)
                with self.assertRaises(ValueError):
                    machine.hart.restore(bad)
        for section in ("hart", "ram", "uart", "clint"):
            for bad in (None, 5, "x", [], 1.5):
                state = copy.deepcopy(good)
                state[section] = bad
                with self.subTest(section=section, value=repr(bad)):
                    with self.assertRaises(ValueError):
                        machine.restore(state)

    def test_from_snapshot_bytes_geometry_problems_are_value_errors(self) -> None:
        for payload in ({}, {"ram": None}, {"ram": []}, {"ram": 5}, {"ram": "x"}, {"ram": {}}, {"ram": {"base": BASE}},
                        {"ram": {"size": RAM}}, {"ram": {"base": BASE, "size": "65536"}}):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    Machine.from_snapshot_bytes(encode_snapshot(payload))

    def test_the_default_ram_cap_is_exactly_64_mib(self) -> None:
        state = self.machine().snapshot()
        state["ram"]["pages"] = []
        state["ram"]["size"] = (64 << 20) + 4096
        with self.assertRaises(ValueError):
            Machine.from_snapshot_bytes(encode_snapshot(state))
        state["ram"]["size"] = 64 << 20
        Machine.from_snapshot_bytes(encode_snapshot(state)).close()

    def test_the_uart_queue_object_survives_restore_and_reset(self) -> None:
        machine = self.machine()
        queue = machine.uart.input
        machine.uart.feed("ab")
        machine.restore(machine.snapshot())
        self.assertIs(machine.uart.input, queue)
        self.assertEqual(list(queue), [97, 98])
        machine.load_flat(b"")
        self.assertIs(machine.uart.input, queue)
        self.assertEqual(list(queue), [])


class FourthBlindReview(unittest.TestCase):
    """Anclas de la ciega 4 de H2.1 (el delta de la ciega 3): los seis contadores y mtime leídos justo tras el cruce de
    2⁶⁴ en el mismo run, contra un modelo a mano; stores de mtimecmp que rebasan el registro; snapshot envuelve un instret
    del host; decode_snapshot nunca descomprime sin cota (tope 0 rechaza; negativo se rechaza antes de zlib); UTF-8
    inválido dentro de un JSON válido; cada clave leída una sola vez por las dos rutas; page_words exacto; solo
    diccionarios de verdad; bordes de los dominios (bytes 0 y 255, mtimecmp 2⁶⁴ − 1, int32 extremos); zlib de nivel 9 y
    no inferior; restore_pages escribe la copia que validó y mide la longitud sobre la copia (F2); run envuelve instret
    también al parar por pc desalineado (F1); la RAM que Machine.__init__ reservó se cierra si el divisor se rechaza; un
    diccionario cuyas claves mienten es ValueError; ninguna clase sin slots en los módulos de estado y los atributos de
    clase con nombre dunder tampoco escapan a la puerta."""

    STANDARD_CLASS_ATTRIBUTES = frozenset({"__module__", "__slots__", "__doc__", "__qualname__", "__annotations__", "__firstlineno__",
                                           "__static_attributes__", "__dict__", "__weakref__", "__annotate_func__",
                                           "__annotations_cache__"})

    def machine(self, **kwargs) -> Machine:
        machine = Machine(ram_bytes=RAM, **kwargs)
        self.addCleanup(machine.close)
        return machine

    def test_every_counter_read_right_after_the_wrap_in_the_same_run(self) -> None:
        reads = [0xC01, 0xC81, 0xC80, 0xC82, 0xC00, 0xC02]          # time, timeh, cycleh, instreth, cycle, instret
        words = ([encode(Kind.ADDI, 1, 1, imm=1)] * 2 + [encode(Kind.CSRRS, 5 + i, 0, imm=c) for i, c in enumerate(reads)]
                 + [encode(Kind.LW, 11, 20, imm=0), encode(Kind.LW, 12, 20, imm=4), EBREAK])
        image = assemble(words)
        for divisor in (3, 7):
            for start in ((1 << 64) - 2, (1 << 64) - 1):
                with self.subTest(divisor=divisor, start=start):
                    machine = self.machine(time_divisor=divisor)
                    machine.load_flat(image)
                    machine.hart.regs[20] = CLINT_BASE + CLINT_MTIME
                    state = machine.snapshot()
                    state["hart"]["instret"] = start
                    machine.restore(state)
                    self.assertEqual(machine.run(100).reason, "ebreak")
                    expected = []
                    for k, csr in enumerate(reads):                  # la lectura k ocurre con instret = start + 2 + k
                        before = (start + 2 + k) & MASK64
                        ticks = before // divisor
                        expected.append({0xC01: ticks & MASK32, 0xC81: (ticks >> 32) & MASK32, 0xC80: before >> 32,
                                         0xC82: before >> 32, 0xC00: before & MASK32, 0xC02: before & MASK32}[csr])
                    low = (((start + 8) & MASK64) // divisor) & MASK32
                    high = ((((start + 9) & MASK64) // divisor) >> 32) & MASK32
                    self.assertEqual(machine.hart.regs[5:13], expected + [low, high])

    def test_mtimecmp_stores_that_overhang_the_register_stay_64_bit(self) -> None:
        machine = self.machine()
        words = [encode(Kind.SW, 0, 10, 5, imm=6), encode(Kind.SW, 0, 10, 5, imm=7), encode(Kind.SH, 0, 10, 5, imm=7), EBREAK]
        machine.load_flat(assemble(words))
        machine.hart.regs[10] = CLINT_BASE + CLINT_MTIMECMP
        machine.hart.regs[5] = 0xFFFFFFFF
        self.assertEqual(machine.run(10).reason, "ebreak")
        self.assertEqual(machine.clint.mtimecmp, 0xFFFF << 48)
        self.machine().restore(machine.snapshot())

    def test_snapshot_wraps_an_instret_the_host_pushed_past_64_bits(self) -> None:
        for raw in (1 << 64, (1 << 64) + 5, (1 << 65) + 7):
            with self.subTest(raw=raw):
                machine = self.machine()
                machine.hart.instret = raw
                self.assertEqual(machine.snapshot()["hart"]["instret"], raw & MASK64)
                self.machine().restore(machine.snapshot())

    def test_decode_never_lets_zlib_decompress_without_a_bound(self) -> None:
        machine = self.machine()
        data = machine.snapshot_bytes()
        size = len(canonical_json(machine.snapshot()))
        calls: list[int] = []
        real = zlib.decompressobj

        class Spy:
            def __init__(self) -> None:
                self.inner = real()

            def decompress(self, chunk, limit=0):
                calls.append(limit)
                return self.inner.decompress(chunk, limit)

            def __getattr__(self, name):
                return getattr(self.inner, name)

        with mock.patch.object(zlib, "decompressobj", Spy):
            for maximum in (0, 1, size - 1, size, 10 ** 30):
                calls.clear()
                try:
                    decode_snapshot(data, maximum)
                except ValueError:
                    pass
                self.assertTrue(calls and all(limit > 0 for limit in calls), (maximum, calls))   # 0 es «sin límite» en zlib
            for maximum in (-1, -100):
                calls.clear()
                with self.assertRaises(ValueError):
                    decode_snapshot(data, maximum)
                self.assertEqual(calls, [], maximum)                                              # se rechaza antes de descomprimir

    def test_a_cap_of_zero_rejects_every_snapshot(self) -> None:
        data = self.machine().snapshot_bytes()
        with self.assertRaises(ValueError):
            decode_snapshot(data, 0)

    def test_invalid_utf8_inside_otherwise_valid_json_is_rejected(self) -> None:
        for raw in (b'{"a":"\xff"}', b'{"\xc3":1}', b'{"a":"\xed\xa0\x80"}'):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    decode_snapshot(SNAPSHOT_MAGIC + zlib.compress(raw))

    def test_every_key_is_read_once_by_both_restores(self) -> None:
        source = make_machine(PROGRAM, PROGRAM_REGS)
        self.addCleanup(source.close)
        source.run(100)
        source.uart.feed("zz")
        good = source.snapshot()
        other_hart = copy.deepcopy(good["hart"])
        other_hart.update(pc=BASE + 0x100, instret=5, time_divisor=9)
        other_hart["regs"][3] = 77
        other_ram = copy.deepcopy(good["ram"])
        other_ram["pages"] = [[0, [7, 8, 9]]]
        alts = {
            ("root", "version"): [2, True, "1", None], ("root", "hart"): [other_hart, {}, None], ("root", "ram"): [other_ram, {}, None],
            ("root", "uart"): [{"output": [1], "input": [2]}, None], ("root", "clint"): [{"mtimecmp": 5}, None],
            ("hart", "regs"): [good["hart"]["regs"][:31], [0] + [1 << 32] * 31, [0] + [9] * 31, None],
            ("hart", "pc"): [-1, 1 << 32, BASE + 0x100, None], ("hart", "instret"): [1 << 64, -1, 5, None],
            ("hart", "csrs"): [{"0x300": 1}, None], ("hart", "reservation"): [5, 0], ("hart", "time_divisor"): [0, 1 << 64, 9, None],
            ("ram", "base"): [BASE + 4096, None], ("ram", "size"): [RAM * 2, None], ("ram", "page_words"): [1024, 8192, None],
            ("ram", "pages"): [[[0, [1 << 40]]], "x", [[0, [7, 8, 9]]]],
            ("uart", "output"): [[300], [1, 2, 3], None], ("uart", "input"): [[300], [1, 2, 3], None],
            ("clint", "mtimecmp"): [-1, 1 << 64, 5, None],
        }

        class Shifty(dict):
            """Devuelve otro valor para una clave a partir de la segunda lectura."""

            def __init__(self, data, key, alt):
                super().__init__(data)
                self.key, self.alt, self.reads = key, alt, 0

            def __getitem__(self, name):
                if name == self.key:
                    self.reads += 1
                    if self.reads > 1:
                        return self.alt
                return dict.__getitem__(self, name)

        count = 0
        for (level, key), options in alts.items():
            for alt in options:
                for route in ("hart", "machine"):
                    state = copy.deepcopy(good)
                    if route == "hart":
                        state = {k: state[k] for k in ("version", "hart", "ram")}
                        if level in ("uart", "clint") or (level == "root" and key in ("uart", "clint")):
                            continue
                    if level == "root":
                        state = Shifty(state, key, alt)
                    else:
                        state[level] = Shifty(state[level], key, alt)
                    target = make_machine(LOOP, {20: 20}, accelerate=True, warmup=1)
                    self.addCleanup(target.close)
                    target.run(1000)
                    before = full_state(target)
                    expected = good if route == "machine" else {k: good[k] for k in ("version", "hart", "ram")}
                    with self.subTest(route=route, level=level, key=key, alt=repr(alt)[:30]):
                        try:
                            (target.hart if route == "hart" else target).restore(state)
                        except ValueError:
                            self.assertEqual(full_state(target), before)
                        else:
                            snap = target.snapshot()
                            self.assertEqual(snap if route == "machine" else {k: snap[k] for k in ("version", "hart", "ram")}, expected)
                        count += 1
        self.assertGreater(count, 80)

    def test_page_words_must_be_exactly_4096(self) -> None:
        machine = self.machine()
        good = machine.snapshot()
        for value in (8192, 1 << 31, 4097, 4095, 0, -4096):
            state = copy.deepcopy(good)
            state["ram"]["page_words"] = value
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    machine.restore(state)
                with self.assertRaises(ValueError):
                    machine.hart.restore({k: state[k] for k in ("version", "hart", "ram")})

    def test_only_real_dicts_are_accepted_as_states_and_sections(self) -> None:
        machine = self.machine()
        good = machine.snapshot()
        for wrap in (types.MappingProxyType, collections.UserDict):
            with self.subTest(wrap=wrap.__name__, level="state"):
                with self.assertRaises(ValueError):
                    machine.restore(wrap(good))
                with self.assertRaises(ValueError):
                    machine.hart.restore(wrap({k: good[k] for k in ("version", "hart", "ram")}))
            for section in ("hart", "ram", "uart", "clint"):
                state = copy.deepcopy(good)
                state[section] = wrap(state[section])
                with self.subTest(wrap=wrap.__name__, level=section):
                    with self.assertRaises(ValueError):
                        machine.restore(state)

    def test_domain_edges_round_trip(self) -> None:
        source = self.machine()
        source.uart.output.extend(bytes([0, 1, 254, 255]))
        source.uart.feed(bytes([0, 255, 7]))
        source.clint.mtimecmp = (1 << 64) - 1
        source.memory.store32(DATA, 0x7FFFFFFF)
        source.memory.store32(DATA + 4, 0x80000000)
        source.memory.store32(DATA + 8, 0xFFFFFFFF)
        state = source.snapshot()
        for label, restore in (("dict", lambda t: t.restore(state)), ("bytes", lambda t: t.restore_bytes(source.snapshot_bytes()))):
            with self.subTest(route=label):
                target = self.machine()
                restore(target)
                self.assertEqual(target.snapshot(), state)
        clone = Machine.from_snapshot_bytes(source.snapshot_bytes())
        self.addCleanup(clone.close)
        self.assertEqual(clone.snapshot(), state)
        for value in (1 << 63, (1 << 64) - 2):
            source.clint.mtimecmp = value
            target = self.machine()
            target.restore(source.snapshot())
            self.assertEqual(target.clint.mtimecmp, value)

    def test_snapshot_bytes_is_level_9_and_not_a_lower_level(self) -> None:
        machine = self.machine()
        distinguishing = 0
        for period in (3, 5, 7, 11, 13, 64, 97, 251):
            machine.memory.fill([(i * 2654435761 + (i % period) * 40503) & 0xFFFFFFFF for i in range(RAM // 4)], BASE)
            raw = canonical_json(machine.snapshot())
            self.assertEqual(machine.snapshot_bytes(), SNAPSHOT_MAGIC + zlib.compress(raw, 9))
            lower = [level for level in range(1, 9) if zlib.compress(raw, level) != zlib.compress(raw, 9)]
            distinguishing += bool(lower)
            for level in lower:
                self.assertNotEqual(machine.snapshot_bytes(), SNAPSHOT_MAGIC + zlib.compress(raw, level), (period, level))
        self.assertGreater(distinguishing, 0)

    def test_restore_pages_writes_exactly_the_copy_it_validated(self) -> None:
        class Split(list):
            """Itera [1, 2, 3] pero su almacenamiento real no cabe en int32."""

            def __iter__(self):
                return iter([1, 2, 3])

        source = self.machine()
        good = source.snapshot()
        target = make_machine(LOOP, {20: 3}, accelerate=True, warmup=1)
        self.addCleanup(target.close)
        target.run(100)
        before = full_state(target)
        state = copy.deepcopy(good)
        state["ram"]["pages"] = [[0, Split([1 << 40, 1 << 40, 1 << 40])]]
        try:
            target.restore(state)
        except ValueError:
            self.assertEqual(full_state(target), before)
        else:
            self.assertEqual([target.memory.load32(BASE + 4 * i) for i in range(3)], [1, 2, 3])

    def test_restore_pages_measures_the_length_of_the_copy_not_of_the_object(self) -> None:
        class LenLiar(list):
            def __len__(self):
                return 100

        machine = self.machine()
        before = full_state(machine)
        state = machine.snapshot()
        state["ram"]["pages"] = [[0, LenLiar(range(1, 5001))]]
        with self.assertRaises(ValueError):                       # más de 4 096 palabras: ningún registro de página puede ser tan largo
            machine.restore(state)
        self.assertEqual(full_state(machine), before)
        state["ram"]["pages"] = [[0, LenLiar([])]]
        with self.assertRaises(ValueError):                       # y un registro vacío tampoco es válido
            machine.restore(state)
        self.assertEqual(full_state(machine), before)

    def test_run_wraps_instret_also_when_it_stops_on_a_misaligned_pc(self) -> None:
        for gas in (0, 5):
            with self.subTest(gas=gas):
                machine = self.machine(time_divisor=3)
                machine.load_flat(assemble([EBREAK]))
                machine.hart.instret = (1 << 64) + 7
                machine.hart.pc = BASE + 2
                machine.run(gas)
                self.assertEqual(machine.hart.instret, 7)

    def test_a_rejected_divisor_closes_the_ram_the_machine_reserved(self) -> None:
        for divisor in (0, 1 << 64, True):
            with self.subTest(divisor=repr(divisor)):
                with mock.patch.object(Rv32Memory, "close", autospec=True, side_effect=Rv32Memory.close) as closer:
                    with self.assertRaises(ValueError):
                        Machine(ram_bytes=RAM, time_divisor=divisor)
                self.assertEqual(closer.call_count, 1)

    def test_a_mapping_whose_keys_lie_is_a_value_error(self) -> None:
        class LyingKeys(dict):
            """Itera (y declara) las claves prometidas aunque no las tenga todas."""

            def __init__(self, data, promised):
                super().__init__(data)
                self.promised = tuple(promised)

            def __iter__(self):
                return iter(self.promised)

            def keys(self):
                return self.promised

        machine = make_machine(LOOP, {20: 5}, accelerate=True, warmup=1)
        self.addCleanup(machine.close)
        machine.run(100)
        good = machine.snapshot()
        before = full_state(machine)
        for route, keys in (("machine", ("version", "hart", "ram", "uart", "clint")), ("hart", ("version", "hart", "ram"))):
            for missing in keys:
                state = LyingKeys({key: good[key] for key in keys if key != missing}, keys)
                with self.subTest(route=route, missing=missing):
                    with self.assertRaises(ValueError):
                        (machine if route == "machine" else machine.hart).restore(state)
                    self.assertEqual(full_state(machine), before)
        for section in ("hart", "ram", "uart", "clint"):
            for missing in good[section]:
                state = copy.deepcopy(good)
                state[section] = LyingKeys({key: value for key, value in good[section].items() if key != missing}, good[section])
                with self.subTest(section=section, missing=missing):
                    with self.assertRaises(ValueError):
                        machine.restore(state)
                    self.assertEqual(full_state(machine), before)

    def test_no_slotless_class_is_defined_in_the_state_modules(self) -> None:
        offenders = []
        for module in (rv32_hart, rv32_mem, rv32_platform, rv32_blocks):
            for name, cls in vars(module).items():
                if (isinstance(cls, type) and cls.__module__ == module.__name__ and "__slots__" not in cls.__dict__
                        and not issubclass(cls, (BaseException, tuple, enum.Enum))):
                    offenders.append(f"{module.__name__}.{name}")
        self.assertEqual(offenders, [])   # una clase sin slots podría guardar estado que ningún inventario clasifica

    def test_dunder_named_class_attributes_are_not_exempt_from_the_gate(self) -> None:
        allowed = (tuple, str, int, float, bool, frozenset, type(None))
        skip = (types.FunctionType, staticmethod, classmethod, property, types.MemberDescriptorType)
        for cls in INVENTORIED:
            for name, value in vars(cls).items():
                if name in self.STANDARD_CLASS_ATTRIBUTES or name == "DERIVED_FIELDS" or isinstance(value, skip):
                    continue
                self.assertIsInstance(value, allowed, f"{cls.__name__}.{name}")   # «__log__ = []» tampoco se cuela


def set_new_state(machine: Machine) -> dict[str, object]:
    """Pone, como el host, un valor legal y distinto del reinicio en cada campo que H3.0 añade (CSR de modo M,
    privilegio, reserva y ``msip``); devuelve lo puesto por nombre de atributo."""
    isa, hart = rv32_isa, machine.hart
    # mtvec fuera de la RAM (vectorizado, base 0x200): con H3.1 un trap sigue deteniendo el hart (modo compatible), así
    # que ninguna instrucción toca este estado y debe viajar intacto por cada corte.
    values = {"mstatus": isa.MSTATUS_MPIE | isa.MSTATUS_MPP | isa.MSTATUS_MPRV, "mie": isa.MIE_MSIE | isa.MIE_MTIE,
              "mtvec": 0x201, "mscratch": 0xA5A5A5A5, "mepc": BASE + 0x80, "mcause": 0x8000000B, "mtval": 0x1234,
              "privilege": isa.PRIV_U, "reservation": DATA + 0x10}
    for name, value in values.items():
        setattr(hart, name, value)
    machine.clint.msip = 1
    return {**values, "msip": 1}


def new_state_of(machine: Machine) -> dict[str, object]:
    hart = machine.hart
    names = ("mstatus", "mie", "mtvec", "mscratch", "mepc", "mcause", "mtval", "privilege", "reservation")
    return {**{name: getattr(hart, name) for name in names}, "msip": machine.clint.msip}


def as_v1(state: dict) -> dict:
    """El mismo estado con el esquema de H2 (versión 1): sin privilegio, sin CSR, sin reserva ni ``msip``."""
    old = copy.deepcopy(state)
    old["version"] = 1
    del old["hart"]["privilege"]
    old["hart"]["csrs"] = {}
    old["hart"]["reservation"] = None
    if "clint" in old:
        old["clint"] = {"mtimecmp": old["clint"]["mtimecmp"]}
    return old


class SchemaV2(unittest.TestCase):
    """Esquema v2 del snapshot (H3.0): campos nuevos de ida y vuelta, lectura de v1, rechazo atómico, lecturas únicas,
    hash, reinicio e invariante S acotada con el estado nuevo puesto por el host."""

    def source(self) -> Machine:
        machine = make_machine(PROGRAM, PROGRAM_REGS)
        self.addCleanup(machine.close)
        self.assertEqual(machine.run(100).reason, "ebreak")
        machine.uart.feed("zz")
        return machine

    def used_target(self) -> Machine:
        target = make_machine(LOOP, {20: 20}, accelerate=True, warmup=1)
        self.addCleanup(target.close)
        self.assertEqual(target.run(1000).reason, "ebreak")
        return target

    def test_every_new_field_round_trips_by_dict_bytes_and_constructor(self) -> None:
        machine = self.source()
        put = set_new_state(machine)
        state = machine.snapshot()
        self.assertEqual(state["version"], 2)
        self.assertEqual(state["hart"]["csrs"], {"0x300": put["mstatus"], "0x304": put["mie"], "0x305": put["mtvec"],
                                                 "0x340": put["mscratch"], "0x341": put["mepc"], "0x342": put["mcause"],
                                                 "0x343": put["mtval"]})
        self.assertEqual((state["hart"]["privilege"], state["hart"]["reservation"], state["clint"]["msip"]), ("U", DATA + 0x10, 1))
        self.assertEqual(json.loads(json.dumps(state)), state)
        for route in ("diccionario", "bytes", "constructor", "usada"):
            with self.subTest(route=route):
                if route == "constructor":
                    target = Machine.from_snapshot_bytes(machine.snapshot_bytes())
                    self.addCleanup(target.close)
                else:
                    target = self.used_target() if route == "usada" else Machine(ram_bytes=RAM)
                    if route != "usada":
                        self.addCleanup(target.close)
                    (target.restore_bytes(machine.snapshot_bytes()) if route == "bytes" else target.restore(state))
                self.assertEqual(target.snapshot(), state)
                self.assertEqual(target.state_hash(), machine.state_hash())
                self.assertEqual(new_state_of(target), put)

    def test_a_v1_snapshot_puts_the_new_state_at_its_reset_values(self) -> None:
        machine = self.source()
        v2 = machine.snapshot()                     # el origen tiene el estado nuevo en su reinicio
        v1 = as_v1(v2)
        reset = {"mstatus": 0, "mie": 0, "mtvec": 0, "mscratch": 0, "mepc": 0, "mcause": 0, "mtval": 0,
                 "privilege": rv32_isa.PRIV_M, "reservation": None, "msip": 0}
        targets = []
        for route in ("diccionario", "bytes", "hart suelto"):
            with self.subTest(route=route):
                target = self.used_target()
                set_new_state(target)
                if route == "hart suelto":
                    target.hart.restore({key: v1[key] for key in ("version", "hart", "ram")})
                    self.assertEqual(target.hart.snapshot(), {key: v2[key] for key in ("version", "hart", "ram")})
                    continue
                (target.restore_bytes(encode_snapshot(v1)) if route == "bytes" else target.restore(v1))
                self.assertEqual(new_state_of(target), reset)
                self.assertEqual(target.snapshot(), v2)          # snapshot() emite siempre v2
                targets.append(target)
        for target in targets:                                 # y sigue como una máquina nueva que restauró el mismo v1
            twin = Machine(ram_bytes=RAM)
            self.addCleanup(twin.close)
            twin.restore(v1)
            for machine_ in (target, twin):
                machine_.uart.feed("q")
            self.assertEqual(target.run(50), twin.run(50))
            self.assertEqual(target.snapshot(), twin.snapshot())
        # Un v1 escrito a mano con el esquema de H2 sigue restaurándose y ejecutándose.
        literal = {"version": 1,
                   "hart": {"regs": [0] * 32, "pc": BASE, "instret": 3, "csrs": {}, "reservation": None, "time_divisor": 7},
                   "ram": {"base": BASE, "size": RAM, "page_words": 4096,
                           "pages": [[0, [rv32_alu.to_signed(encode(Kind.ADDI, 1, 0, imm=7)), rv32_alu.to_signed(EBREAK)]]]},
                   "uart": {"output": [65], "input": []}, "clint": {"mtimecmp": 9}}
        hand = Machine(ram_bytes=RAM)
        self.addCleanup(hand.close)
        hand.restore(literal)
        self.assertEqual(hand.run(10).reason, "ebreak")
        self.assertEqual((hand.hart.regs[1], hand.hart.instret, hand.hart.time_divisor, hand.clint.mtimecmp, hand.uart.text),
                         (7, 4, 7, 9, "A"))

    def test_mixing_v1_and_v2_keys_is_rejected_atomically(self) -> None:
        good = self.source().snapshot()
        target = self.used_target()
        set_new_state(target)
        target.hart.gas = 11
        before = full_state(target)
        v1 = as_v1(good)
        full_csrs = dict(good["hart"]["csrs"])
        cases = {
            "v1 con privilege": lambda: {**v1, "hart": {**v1["hart"], "privilege": "M"}},
            "v1 con los CSR": lambda: {**v1, "hart": {**v1["hart"], "csrs": full_csrs}},
            "v1 con una reserva": lambda: {**v1, "hart": {**v1["hart"], "reservation": DATA}},
            "v1 con msip": lambda: {**v1, "clint": {"mtimecmp": 0, "msip": 0}},
            "v2 sin privilege": lambda: {**good, "hart": {k: v for k, v in good["hart"].items() if k != "privilege"}},
            "v2 con csrs vacío": lambda: {**good, "hart": {**good["hart"], "csrs": {}}},
            "v2 sin msip": lambda: {**good, "clint": {"mtimecmp": 0}},
            "v2 con clave de más en clint": lambda: {**good, "clint": {**good["clint"], "mip": 0}},
        }
        for name, make in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    target.restore(copy.deepcopy(make()))
                self.assertEqual(full_state(target), before)
        with self.assertRaises(ValueError):                   # y por la ruta del hart suelto
            target.hart.restore({"version": 1, "hart": {**v1["hart"], "privilege": "M"}, "ram": v1["ram"]})
        self.assertEqual(full_state(target), before)

    def test_the_version_is_an_int_on_both_routes(self) -> None:
        # True == 1 y 2.0 == 2 están en {1, 2}: solo la comprobación de tipo los rechaza, con la forma de su versión
        # (si no, otra clave los delataría); la máquina comprueba antes que el hart, así que la ruta del hart va aparte
        # (oráculo 1 de H3, mutante parse_sin_check_version_range).
        good = self.source().snapshot()
        target = self.used_target()
        set_new_state(target)
        target.hart.gas = 11
        before = full_state(target)
        v1 = as_v1(good)
        hart_keys = ("version", "hart", "ram")
        for name, state in (("True con forma v1", {**v1, "version": True}), ("1.0 con forma v1", {**v1, "version": 1.0}),
                            ("2.0 con forma v2", {**good, "version": 2.0})):
            for route in ("machine", "hart"):
                with self.subTest(case=name, route=route):
                    with self.assertRaises(ValueError):
                        if route == "machine":
                            target.restore(copy.deepcopy(state))
                        else:
                            target.hart.restore(copy.deepcopy({key: state[key] for key in hart_keys}))
                    self.assertEqual(full_state(target), before)

    def test_illegal_values_of_the_new_fields_are_rejected_atomically(self) -> None:
        good = self.source().snapshot()
        target = self.used_target()
        set_new_state(target)
        before = full_state(target)
        isa = rv32_isa
        generic = (True, -1, 1 << 32, 1.0, "0", None)
        csr_cases = {"0x300": (1, 1 << 2, isa.MSTATUS_MPP & (1 << 11), isa.MSTATUS_MPP & (2 << 11), 1 << 31, 1 << 22),
                     "0x304": (1 << 5, 1 << 1, 1 << 9, 1 << 31), "0x305": (2, 3, BASE + 2), "0x341": (1, 2, 3, BASE + 1),
                     "0x340": (), "0x342": (), "0x343": ()}
        cases: list[tuple[str, object]] = []
        for key, illegal in csr_cases.items():
            for value in illegal + generic:
                cases.append((f"csr {key} = {value!r}", lambda s, k=key, v=value: s["hart"]["csrs"].__setitem__(k, v)))
        for value in ("S", "m", "MU", "", 3, 0, None, True):
            cases.append((f"privilege {value!r}", lambda s, v=value: s["hart"].__setitem__("privilege", v)))
        for value in (DATA + 1, DATA + 2, DATA + 3, BASE - 4, BASE + RAM - 3, BASE + RAM, BASE + RAM + 4, 1 << 40, True, -4,
                      float(DATA), str(DATA)):
            cases.append((f"reservation {value!r}", lambda s, v=value: s["hart"].__setitem__("reservation", v)))
        for value in (2, -1, True, None, "1", 1.0, 1 << 32):
            cases.append((f"msip {value!r}", lambda s, v=value: s["clint"].__setitem__("msip", v)))
        for name, change in cases:
            state = copy.deepcopy(good)
            change(state)
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    target.restore(state)
                self.assertEqual(full_state(target), before)
        self.assertGreater(len(cases), 60)
        # Los bordes legales sí entran.
        for key, value in (("0x300", rv32_hart.MSTATUS_WRITABLE), ("0x304", rv32_hart.MIE_WRITABLE), ("0x305", 0xFFFFFFFD),
                           ("0x341", 0xFFFFFFFC), ("0x340", MASK32), ("0x342", MASK32), ("0x343", MASK32)):
            state = copy.deepcopy(good)
            state["hart"]["csrs"][key] = value
            state["hart"]["reservation"] = BASE + RAM - 4
            target.restore(state)
            self.assertEqual(target.snapshot()["hart"]["csrs"][key], value)
        self.assertEqual(target.hart.reservation, BASE + RAM - 4)

    def test_the_legalization_is_the_fixed_warl_profile(self) -> None:
        isa, legalize = rv32_isa, rv32_hart.legalize_csr
        self.assertEqual(legalize(isa.CSR_MSTATUS, MASK32), rv32_hart.MSTATUS_WRITABLE)
        self.assertEqual(legalize(isa.CSR_MSTATUS, 1 << 11), 0)                     # MPP = S → U
        self.assertEqual(legalize(isa.CSR_MSTATUS, (2 << 11) | isa.MSTATUS_MIE), isa.MSTATUS_MIE)   # MPP = 2 → U
        self.assertEqual(legalize(isa.CSR_MSTATUS, isa.MSTATUS_MPP), isa.MSTATUS_MPP)
        self.assertEqual(legalize(isa.CSR_MIE, MASK32), isa.MIE_MSIE | isa.MIE_MTIE | isa.MIE_MEIE)
        self.assertEqual(legalize(isa.CSR_MTVEC, MASK32), 0xFFFFFFFD)
        self.assertEqual(legalize(isa.CSR_MEPC, MASK32), 0xFFFFFFFC)
        for csr in (isa.CSR_MSCRATCH, isa.CSR_MCAUSE, isa.CSR_MTVAL):
            self.assertEqual(legalize(csr, 0xDEADBEEF), 0xDEADBEEF)
        for csr in (isa.CSR_MIP, isa.CSR_MISA, isa.CSR_CYCLE, 0x7C0):
            with self.assertRaises(ValueError):
                legalize(csr, 0)
        self.assertEqual(rv32_hart.MSTATUS_WRITABLE, 0x221888)
        self.assertEqual(rv32_hart.MIE_WRITABLE, 0x888)

    def test_the_new_keys_are_read_once_by_both_restores(self) -> None:
        machine = self.source()
        set_new_state(machine)
        good = machine.snapshot()

        class Shifty(dict):
            def __init__(self, data, key, alt):
                super().__init__(data)
                self.key, self.alt, self.reads = key, alt, 0

            def __getitem__(self, name):
                if name == self.key:
                    self.reads += 1
                    if self.reads > 1:
                        return self.alt
                return dict.__getitem__(self, name)

        alts = {("root", "version"): [1, 3], ("hart", "privilege"): ["M", "S", None], ("hart", "reservation"): [DATA, DATA + 1, None],
                ("hart", "csrs"): [{}, None], ("clint", "msip"): [0, 2, None]}
        for key in good["hart"]["csrs"]:
            alts[("csrs", key)] = [0, 3, None]
        count = 0
        for (level, key), options in alts.items():
            for alt in options:
                for route in ("hart", "machine"):
                    if route == "hart" and level == "clint":
                        continue
                    state = copy.deepcopy(good)
                    if route == "hart":
                        state = {k: state[k] for k in ("version", "hart", "ram")}
                    if level == "root":
                        state = Shifty(state, key, alt)
                    elif level == "csrs":
                        state["hart"]["csrs"] = Shifty(state["hart"]["csrs"], key, alt)
                    else:
                        state[level] = Shifty(state[level], key, alt)
                    target = self.used_target()
                    before = full_state(target)
                    expected = good if route == "machine" else {k: good[k] for k in ("version", "hart", "ram")}
                    with self.subTest(route=route, level=level, key=key, alt=repr(alt)[:20]):
                        try:
                            (target.hart if route == "hart" else target).restore(state)
                        except ValueError:
                            self.assertEqual(full_state(target), before)
                        else:
                            snap = target.snapshot()
                            self.assertEqual(snap if route == "machine" else {k: snap[k] for k in ("version", "hart", "ram")},
                                             expected)
                        count += 1
        self.assertGreater(count, 40)

    def test_every_new_field_changes_the_hash(self) -> None:
        machine = self.source()
        reference = machine.state_hash()
        seen = {reference}
        isa = rv32_isa
        changes = {"mstatus": isa.MSTATUS_MIE, "mie": isa.MIE_MSIE, "mtvec": 4, "mscratch": 1, "mepc": 4, "mcause": 1, "mtval": 1,
                   "privilege": isa.PRIV_U, "reservation": DATA}
        for name, value in [*changes.items(), ("msip", 1)]:
            with self.subTest(field=name):
                state = machine.snapshot()
                setattr(machine.clint if name == "msip" else machine.hart, name, value)
                digest = machine.state_hash()
                self.assertNotIn(digest, seen)
                seen.add(digest)
                machine.restore(state)
                self.assertEqual(machine.state_hash(), reference)

    def test_reset_attach_memory_and_load_flat_return_the_new_state_to_reset(self) -> None:
        machine = self.source()
        put = set_new_state(machine)
        reset = {"mstatus": 0, "mie": 0, "mtvec": 0, "mscratch": 0, "mepc": 0, "mcause": 0, "mtval": 0,
                 "privilege": rv32_isa.PRIV_M, "reservation": None}
        other = Rv32Memory(RAM)
        self.addCleanup(other.close)
        original = machine.memory
        machine.hart.attach_memory(other)                      # pierde la reserva; conserva CSR y privilegio
        self.assertEqual(new_state_of(machine), {**put, "reservation": None})
        machine.hart.attach_memory(original)
        machine.hart.reset()
        self.assertEqual(new_state_of(machine), {**reset, "msip": 1})   # el reinicio del hart no toca el CLINT
        set_new_state(machine)
        machine.load_flat(assemble(PROGRAM))
        self.assertEqual(new_state_of(machine), {**reset, "msip": 0})

    def test_bounded_invariant_s_carries_the_new_state_through_every_cut(self) -> None:
        rng = random.Random(30)
        for index in range(12):
            words = random_program(rng, rng.randrange(20, 120))
            if index % 2:
                words = hostile_layout(words, rng, 0x40, max(64, 4 * len(words) - 0x40))
            image = assemble(words)

            def fresh(accelerate: bool, warmup: int = 1, image: bytes = image) -> Machine:
                machine = Machine(ram_bytes=RAM)
                self.addCleanup(machine.close)
                machine.load_flat(image)
                machine.hart.accelerate, machine.hart.warmup = accelerate, warmup
                return machine

            whole = fresh(False)
            put = set_new_state(whole)
            whole_stop = whole.run(600)
            expected = whole.snapshot()
            self.assertEqual(new_state_of(whole), put)          # ninguna instrucción de H3.0 los toca
            current = fresh(True)
            set_new_state(current)
            consumed = 0
            stop = Stop("gas")
            for step in range(40):
                chunk = min(rng.randrange(1, 30), 600 - consumed)
                stop = current.run(chunk)
                consumed += chunk - current.hart.gas
                if stop.reason != "gas" or consumed == 600:
                    break
                target = fresh(step % 2 == 0, 1 + step % 2) if step % 3 else self.used_target()
                (target.restore_bytes(current.snapshot_bytes()) if step % 4 == 0 else target.restore(current.snapshot()))
                self.assertEqual(target.state_hash(), current.state_hash())
                current = target
            if stop.reason == "gas" and consumed < 600:
                stop = current.run(600 - consumed)
            with self.subTest(program=index):
                self.assertEqual(stop, whole_stop)
                self.assertEqual(current.snapshot(), expected)
                self.assertEqual(new_state_of(current), put)


class TrapsUnderSnapshots(unittest.TestCase):
    """H3.1: la invariante S acotada con traps manejados, MRET y modo U: cada corte (snapshot, restauración en máquina
    nueva o usada, a veces por bytes, con aceleración distinta) da lo mismo que la ejecución entera."""

    def test_random_trap_programs_survive_every_cut(self) -> None:
        from tests.test_rv32_hart import random_trap_program, with_handler
        rng = random.Random(311)
        traps = users = 0
        for index in range(40):
            image = with_handler(random_trap_program(rng, rng.randrange(20, 120)))
            gas = 500

            def fresh(accelerate: bool, warmup: int = 1, image: bytes = image) -> Machine:
                machine = Machine(ram_bytes=RAM)
                self.addCleanup(machine.close)
                machine.load_flat(image)
                machine.hart.accelerate, machine.hart.warmup = accelerate, warmup
                return machine

            whole = fresh(False)
            whole_stop = whole.run(gas)
            expected = whole.snapshot()
            traps += whole.hart.mcause != 0
            users += whole.hart.privilege == rv32_isa.PRIV_U
            cuts = sorted({rng.randrange(1, gas) for _ in range(12)})
            current = fresh(True)
            last, stop = 0, Stop("gas")
            for step, cut in enumerate(cuts):
                stop = current.run(cut - last)
                if stop.reason != "gas":
                    break
                last = cut
                target = fresh(step % 2 == 1, 1 + step % 2) if step % 3 else make_machine(LOOP, {20: 20}, accelerate=True, warmup=1)
                if not step % 3:
                    self.addCleanup(target.close)
                    target.run(1000)                                   # destino usado
                (target.restore_bytes(current.snapshot_bytes()) if step % 4 == 0 else target.restore(current.snapshot()))
                self.assertEqual(target.state_hash(), current.state_hash())
                current = target
            if stop.reason == "gas":
                stop = current.run(gas - last)
            with self.subTest(program=index):
                self.assertEqual(stop, whole_stop)
                self.assertEqual(current.snapshot(), expected)
        self.assertGreater(traps, 30)
        self.assertGreater(users, 2)


class InterruptsUnderSnapshots(unittest.TestCase):
    """H3.4: la invariante S acotada con interrupciones del CLINT y WFI: todo lo que decide la entrega (mie, mstatus,
    privilegio, msip, mtimecmp, instret, divisor) viaja en el snapshot y el plazo se recalcula en cada run, así que cada
    corte (también el que cae justo tras una WFI que durmió o justo antes de una entrada en interrupción) da lo mismo
    que la ejecución entera."""

    def test_random_interrupt_programs_survive_every_cut(self) -> None:
        from tests.test_rv32_hart import Reference, random_interrupt_program, with_interrupt_handler
        rng = random.Random(341)
        interrupted = slept = 0
        for index in range(40):
            divisor = (1, 3, 7)[index % 3]
            image = with_interrupt_handler(random_interrupt_program(rng, rng.randrange(20, 120), rng.random() < 0.4),
                                           rng.choice((0, 7, 25)))
            gas = 600

            def fresh(accelerate: bool, warmup: int = 1, image: bytes = image, divisor: int = divisor) -> Machine:
                machine = Machine(ram_bytes=RAM, time_divisor=divisor)
                self.addCleanup(machine.close)
                machine.load_flat(image)
                machine.hart.accelerate, machine.hart.warmup = accelerate, warmup
                return machine

            whole = fresh(False)
            whole_stop = whole.run(gas)
            expected, expected_gas = whole.snapshot(), whole.hart.gas
            census = Reference(image, clint=True, divisor=divisor)   # recuento de cobertura: interrupciones tomadas
            census.run(gas)
            interrupted += bool(census.taken)
            slept += whole.hart.instret > gas - whole.hart.gas
            cuts = sorted({rng.randrange(1, gas) for _ in range(14)})
            current = fresh(True)
            last, stop, consumed = 0, Stop("gas"), 0
            for step, cut in enumerate(cuts):
                stop = current.run(cut - last)
                consumed += cut - last - current.hart.gas
                if stop.reason != "gas":
                    break
                last = cut
                target = fresh(step % 2 == 1, 1 + step % 2) if step % 3 else make_machine(LOOP, {20: 20}, accelerate=True, warmup=1)
                if not step % 3:
                    self.addCleanup(target.close)
                    target.run(1000)                                   # destino usado (y con el divisor predeterminado)
                (target.restore_bytes(current.snapshot_bytes()) if step % 4 == 0 else target.restore(current.snapshot()))
                self.assertEqual(target.state_hash(), current.state_hash())
                current = target
            if stop.reason == "gas":
                stop = current.run(gas - last)
                consumed += gas - last - current.hart.gas
            with self.subTest(program=index):
                self.assertEqual(stop, whole_stop)
                self.assertEqual(current.snapshot(), expected)
                self.assertEqual(consumed, gas - expected_gas)             # el mismo gas consumido, troceado o no
        self.assertGreater(interrupted, 15)
        self.assertGreater(slept, 3)


class CoreMarkUnderSnapshots(unittest.TestCase):
    def test_coremark_cut_in_eight_pieces_with_alternating_modes(self) -> None:
        if not COREMARK.is_file():
            self.skipTest("falta el binario versionado de CoreMark")
        image = COREMARK.read_bytes()
        whole = Machine(ram_bytes=1 << 20)
        self.addCleanup(whole.close)
        whole.load_flat(image)
        whole.hart.accelerate = True
        self.assertEqual(whole.run(20_000_000), Stop("apagado"))
        final_hash = whole.state_hash()
        chunk = 1_200_000
        machine = Machine(ram_bytes=1 << 20)
        machine.load_flat(image)
        machine.hart.accelerate = True
        consumed = 0
        pieces = 0
        try:
            while True:
                stop = machine.run(chunk)
                consumed += chunk - machine.hart.gas
                pieces += 1
                if stop.reason != "gas":
                    break
                data = machine.snapshot_bytes()
                digest = machine.state_hash()
                successor = Machine(ram_bytes=1 << 20)
                try:
                    successor.restore_bytes(data)
                except BaseException:
                    successor.close()
                    raise
                successor.hart.accelerate = not machine.hart.accelerate   # bloques e intérprete alternados
                self.assertEqual(successor.state_hash(), digest)
                machine.close()
                machine = successor
            self.assertEqual(stop, Stop("apagado"))
            self.assertEqual((machine.hart.instret, consumed, pieces), (COREMARK_INSTRUCTIONS, COREMARK_INSTRUCTIONS, 8))
            for line in ("[0]crclist       : 0xe714", "[0]crcmatrix     : 0x1fd7", "[0]crcstate      : 0x8e3a",
                         "[0]crcfinal      : 0xfcaf"):
                self.assertIn(line, machine.uart_text)
            self.assertEqual(machine.state_hash(), final_hash)
            self.assertEqual(machine.uart_text, whole.uart_text)
        finally:
            machine.close()


if __name__ == "__main__":
    unittest.main()
