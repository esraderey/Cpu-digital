"""Anclas de ``rv32_mem``: equivalencia con un modelo bytearray, generaciones, snapshot y fallos."""

from __future__ import annotations

import random
import sys
import unittest
from array import array
from unittest import mock

from cpu_digital.memory import PagedMemory
from cpu_digital.rv32_mem import DEFAULT_BASE, LINE_BYTES, LINE_MASK, LINE_SHIFT, PAGE_BYTES, AccessFault, Rv32Memory

SIZE = 1 << 17  # 128 KiB: 32 páginas de 4 KiB, 8 páginas de snapshot
BASE = DEFAULT_BASE


class Model:
    """Memoria de referencia: bytearray little-endian con su propio mapa de líneas de código, sus páginas
    escritas y sus generaciones (que suben solo cuando el store toca una línea marcada; P1.6 refinado)."""

    def __init__(self, size: int) -> None:
        self.data = bytearray(size)
        self.gens = [0] * (size >> 12)
        self.written = [0] * (size >> 12)
        self.code = [0] * (size >> LINE_SHIFT)
        self.silent = 0   # stores que no tocaron ninguna línea marcada (no suben la generación)

    def mark(self, offset: int, length: int) -> None:
        for line in range(offset >> LINE_SHIFT, ((offset + length - 1) >> LINE_SHIFT) + 1):
            self.code[line] = 1

    def clear(self) -> None:
        """Desmarcar sube la generación de cada página que tenía alguna línea marcada."""
        lines_per_page = 4096 >> LINE_SHIFT
        for page in range(len(self.gens)):
            if any(self.code[page * lines_per_page:(page + 1) * lines_per_page]):
                self.gens[page] += 1
        self.code = [0] * len(self.code)

    def reset(self) -> None:
        """Las páginas escritas se borran, suben de generación y dejan de estar escritas; el mapa no cambia."""
        for page in range(len(self.gens)):
            if self.written[page]:
                self.data[page << 12:(page + 1) << 12] = bytes(4096)
                self.gens[page] += 1
                self.written[page] = 0

    def restore(self, pages: list[list[object]]) -> None:
        """Restaurar el propio snapshot: reset y después una escritura del host por registro (grupos de 4 páginas
        de 4 KiB; cada registro cubre desde su inicio hasta su última palabra)."""
        self.reset()
        for index, values in pages:
            start = index * 4096 * 4
            self.host(start, b"".join((value & 0xFFFFFFFF).to_bytes(4, "little") for value in values))

    def host(self, offset: int, data: bytes) -> None:
        """Escritura del host: marca y sube la generación de cada página tocada, haya o no código."""
        self.data[offset:offset + len(data)] = data
        for page in range(offset >> 12, ((offset + len(data) - 1) >> 12) + 1):
            self.written[page] = 1
            self.gens[page] += 1

    def store(self, offset: int, value: int, width: int) -> None:
        self.data[offset:offset + width] = (value & ((1 << (8 * width)) - 1)).to_bytes(width, "little")
        lines = {offset >> LINE_SHIFT, (offset + width - 1) >> LINE_SHIFT}
        marked = any(self.code[line] for line in lines)
        if not marked:
            self.silent += 1
        for page in sorted({offset >> 12, (offset + width - 1) >> 12}):
            self.written[page] = 1
            if marked:
                self.gens[page] += 1

    def load(self, offset: int, width: int, signed: bool) -> int:
        return int.from_bytes(self.data[offset:offset + width], "little", signed=signed)


class DifferentialFuzz(unittest.TestCase):
    def setUp(self) -> None:
        self.memory = Rv32Memory(SIZE)
        self.addCleanup(self.memory.close)

    def test_matches_the_model(self) -> None:
        rng = random.Random(1234)
        memory, model = self.memory, Model(SIZE)
        hot = [rng.randrange(SIZE >> 12) for _ in range(6)]
        loads = ((memory.load8u, 1, False), (memory.load8s, 1, True), (memory.load16u, 2, False),
                 (memory.load16s, 2, True), (memory.load32, 4, False))
        stores = ((memory.store8, 1), (memory.store16, 2), (memory.store32, 4))
        marks = 0
        for _ in range(60_000):
            page = rng.choice(hot) if rng.random() < 0.7 else rng.randrange(SIZE >> 12)
            offset = (page << 12) + rng.choice((rng.randrange(4096), 4095, 4094, 4093, 0, 1, 2, 3))
            address = BASE + offset
            roll = rng.random()
            if roll < 0.02:
                # El mapa de código: marcas de longitud 0, de una palabra, de línea entera y que abarcan dos
                # líneas o dos páginas, casi siempre en las páginas calientes; rara vez se vacía entero.
                if rng.random() < 0.05:
                    memory.clear_code()
                    model.clear()
                else:
                    length = rng.choice((0, 2, 4, 4, 16, 20, rng.randrange(1, 64)))
                    start = min(offset if rng.random() < 0.3 else offset & ~3, SIZE - 64)   # también desalineado
                    memory.mark_code(start, length)
                    if length:
                        model.mark(start, length)
                        marks += 1
                continue
            if roll < 0.025:
                # Borrado completo o restauración del propio snapshot (hallazgo de la ciega 3: el fuzz no las ejercitaba).
                if rng.random() < 0.5:
                    memory.reset()
                    model.reset()
                else:
                    snapshot = memory.snapshot_pages()
                    memory.restore_pages(snapshot)
                    model.restore(snapshot)
                self.assertEqual(bytes(memory.lanes), bytes(model.data))
                continue
            if roll < 0.04:
                # Escrituras del host en bordes de página: load_bytes de 1 a 40 bytes (también exactamente hasta el
                # primer byte de la página siguiente) y fill de 1 a 8 palabras.
                start = min(offset, SIZE - 64)
                if rng.random() < 0.5:
                    data = bytes(rng.getrandbits(8) for _ in range(rng.choice((1, 2, 3, rng.randrange(1, 41)))))
                    if rng.random() < 0.3:
                        start = (start & ~0xFFF) + 0x1000 - len(data) + 1 if start < SIZE - 0x1000 else start
                        start = min(start, SIZE - len(data))
                    memory.load_bytes(BASE + start, data)
                    model.host(start, data)
                else:
                    start &= ~3
                    values = [rng.getrandbits(32) for _ in range(rng.randrange(1, 9))]
                    memory.fill(values, BASE + start)
                    model.host(start, b"".join(value.to_bytes(4, "little") for value in values))
                continue
            if roll < 0.5:
                fn, width = rng.choice(stores)
                if offset + width > SIZE:
                    continue
                value = rng.choice((rng.getrandbits(32), rng.getrandbits(32) - (1 << 32), rng.getrandbits(40),
                                    -1, 0x80, 0x8000, 0x80000000))
                fn(address, value)
                model.store(offset, value, width)
            else:
                fn, width, signed = rng.choice(loads)
                if offset + width > SIZE:
                    with self.assertRaises(AccessFault):
                        fn(address)
                    continue
                self.assertEqual(fn(address), model.load(offset, width, signed), f"{fn.__name__} en {address:#x}")
        self.assertGreater(marks, 500)
        self.assertEqual(bytes(memory.lanes), bytes(model.data))
        self.assertEqual(list(memory.generations), model.gens)
        self.assertEqual(list(memory.written), model.written)
        self.assertEqual(list(memory.code), model.code)
        self.assertGreater(sum(model.gens), 1000)   # hubo stores sobre líneas marcadas (suben la generación)
        self.assertGreater(model.silent, 10_000)    # y muchos más sobre líneas sin código (solo marcan written)
        self.assertEqual(memory.written_pages(), [page for page, flag in enumerate(model.written) if flag])
        # Invariante W: todo byte distinto de cero vive en una página marcada como escrita.
        for page in range(SIZE >> 12):
            if any(memory.lanes[page << 12:(page + 1) << 12]):
                self.assertEqual(memory.written[page], 1, page)

    def test_sign_extension_and_lane_math(self) -> None:
        memory = self.memory
        memory.store32(BASE + 16, 0x80FF7F01)
        self.assertEqual((memory.load8u(BASE + 19), memory.load8s(BASE + 19)), (0x80, -128))
        self.assertEqual((memory.load8u(BASE + 17), memory.load8s(BASE + 17)), (0x7F, 127))
        self.assertEqual((memory.load16u(BASE + 18), memory.load16s(BASE + 18)), (0x80FF, -32513))
        self.assertEqual((memory.load16u(BASE + 16), memory.load16s(BASE + 16)), (0x7F01, 0x7F01))
        self.assertEqual(memory.load32(BASE + 17), 0x0080FF7F)     # desalineada, sin trap
        memory.store32(BASE + 4095, 0x11223344)                      # cruza dos páginas
        self.assertEqual(memory.load32(BASE + 4095), 0x11223344)
        self.assertEqual((memory.load8u(BASE + 4095), memory.load8u(BASE + 4096)), (0x44, 0x33))
        self.assertEqual(memory.words_i[4], 0x80FF7F01 - (1 << 32))   # -2130739455, con signo
        self.assertEqual(memory.words_u[4], 0x80FF7F01)


class PagesAndGenerations(unittest.TestCase):
    def setUp(self) -> None:
        self.memory = Rv32Memory(SIZE)
        self.addCleanup(self.memory.close)

    def test_untouched_pages_read_zero_and_stay_unmarked(self) -> None:
        self.assertEqual(self.memory.load32(BASE + SIZE - 4), 0)
        self.assertEqual(self.memory.load8s(BASE), 0)
        self.assertEqual(self.memory.written_pages(), [])
        self.assertEqual(sum(self.memory.generations), 0)

    def test_generation_rises_only_for_stores_on_code_lines(self) -> None:
        memory = self.memory
        memory.store8(BASE, 1)
        memory.store16(BASE + 4095, 1)   # páginas 0 y 1
        memory.store32(BASE + 8190, 1)   # páginas 1 y 2
        memory.store32(BASE + 8192, 1)   # página 2
        self.assertEqual(list(memory.generations[:4]), [0, 0, 0, 0])   # ninguna línea tiene código
        self.assertEqual(memory.written_pages(), [0, 1, 2])
        self.assertEqual(memory.page_of(BASE + 8192), 2)
        memory.mark_code(0x100, 4)                                     # línea 0x100..0x10F de la página 0
        memory.store8(BASE + 0x100, 1)
        memory.store16(BASE + 0x102, 1)
        memory.store32(BASE + 0x104, 1)
        self.assertEqual(memory.generation(0), 3)                      # +1 por store sobre la línea marcada
        memory.store32(BASE + 0x110, 1)                                # línea contigua, sin marca
        memory.store8(BASE + 0xFF, 1)                                  # línea anterior, sin marca
        memory.store16(BASE + 0x10E, 1)                                # dentro de la marcada sin cruzar
        self.assertEqual(memory.generation(0), 4)
        memory.store16(BASE + 0xFF, 1)                                 # cruza a la línea marcada: sube
        self.assertEqual(memory.generation(0), 5)
        memory.store32(BASE + 0x10D, 1)                                # desalineada, nace en la marcada: sube
        self.assertEqual(memory.generation(0), 6)
        memory.store32(BASE + 0x0FD, 1)                                # desalineada, acaba en la marcada: sube
        self.assertEqual(memory.generation(0), 7)
        # Cruce de página con la marca solo en la segunda: suben las dos páginas tocadas.
        memory.mark_code(0x1000, 4)
        memory.store32(BASE + 0xFFE, 1)                                # bytes 0xFFE..0x1001
        self.assertEqual((memory.generation(0), memory.generation(1)), (8, 1))
        memory.store16(BASE + 0xFFF, 1)
        self.assertEqual((memory.generation(0), memory.generation(1)), (9, 2))
        self.assertEqual(memory.written_pages(), [0, 1, 2])
        self.assertEqual(memory.generation(2), 0)

    def test_load_bytes_marks_pages_and_fill_writes_words(self) -> None:
        memory = self.memory
        memory.load_bytes(BASE + 4090, bytes(range(1, 11)))
        self.assertEqual(memory.read_bytes(BASE + 4090, 10), bytes(range(1, 11)))
        self.assertEqual(memory.written_pages(), [0, 1])
        self.assertEqual(list(memory.generations[:3]), [1, 1, 0])     # el host siempre sube la generación
        memory.fill([1, -1, 0x80000000], BASE + 8192)
        self.assertEqual([memory.load32(BASE + 8192 + 4 * i) for i in range(3)], [1, 0xFFFFFFFF, 0x80000000])
        self.assertEqual(memory.written_pages(), [0, 1, 2])
        self.assertEqual(list(memory.generations[:3]), [1, 1, 1])
        with self.assertRaises(AccessFault):
            memory.load_bytes(BASE + SIZE - 4, bytes(8))
        with self.assertRaises(ValueError):
            memory.fill([1], BASE + 2)
        # Hallazgo de la revisión ciega 2: escrituras del host que acaban justo en el primer byte de una página o que
        # empiezan en él deben marcar esa página (un mutante «len − 1» en load_bytes sobrevivía a toda la suite).
        memory.load_bytes(BASE + 0x3FFF, b"\xAA\xBB")        # último byte de la página 3 y primero de la 4
        memory.load_bytes(BASE + 0x5000, b"\x01")            # un solo byte, el primero de la página 5
        memory.fill([7], BASE + 0x6000)                      # una palabra, la primera de la página 6
        self.assertEqual(memory.written_pages(), [0, 1, 2, 3, 4, 5, 6])
        self.assertEqual(list(memory.generations[3:7]), [1, 1, 1, 1])
        self.assertEqual((memory.load8u(BASE + 0x4000), memory.load8u(BASE + 0x5000), memory.load32(BASE + 0x6000)), (0xBB, 1, 7))
        restored = Rv32Memory(SIZE)
        self.addCleanup(restored.close)
        restored.restore_pages(memory.snapshot_pages())
        self.assertEqual(bytes(restored.lanes), bytes(memory.lanes))

    def test_host_writes_always_raise_generations_and_reset_keeps_the_code_map(self) -> None:
        memory = self.memory
        memory.mark_code(0x2000, 8)                       # página 2, línea 0x2000
        memory.store32(BASE + 0x100, 5)                   # dato en la página 0: escrita, generación 0
        memory.load_bytes(BASE + 0x100, b"\x01\x02")      # host: sube aunque no haya código
        memory.fill([7], BASE + 0x2000)                   # host sobre la línea marcada: sube
        memory.fill([9], BASE + 0x3000)                   # host sobre página virgen sin código: sube y marca
        self.assertEqual(list(memory.generations[:4]), [1, 0, 1, 1])
        self.assertEqual(memory.written_pages(), [0, 2, 3])
        memory.store32(BASE + 0x2004, 1)                  # invitado sobre la línea marcada: sube
        memory.store32(BASE + 0x2010, 1)                  # línea contigua sin marca: no
        self.assertEqual(memory.generation(2), 2)
        memory.store8(BASE + 0xFFF, 7)                    # último byte de la página 0 (hallazgo de la ciega 3)
        memory.store8(BASE + 0x3FFF, 9)                   # último byte de la página 3
        before_code = bytes(memory.code)
        memory.reset()
        self.assertEqual(memory.written_pages(), [])                      # exacto: ya no hay nada escrito
        self.assertEqual(list(memory.generations[:4]), [2, 0, 3, 2])     # +1 en las páginas borradas
        self.assertEqual(bytes(memory.code), before_code)                 # el mapa de código no se toca
        self.assertEqual(memory.snapshot_pages(), [])
        self.assertEqual((memory.load8u(BASE + 0xFFF), memory.load8u(BASE + 0x3FFF)), (0, 0))
        self.assertFalse(any(memory.lanes))
        memory.restore_pages([[0, [1, 2]], [2, [0] * 4 + [3]]])          # grupo 2 de snapshot = página 8 de 4 KiB
        self.assertEqual(memory.written_pages(), [0, 8])
        self.assertEqual((memory.generation(0), memory.generation(8)), (3, 1))
        self.assertEqual(bytes(memory.code), before_code)
        self.assertEqual((memory.load32(BASE + 4), memory.load32(BASE + 0x8010)), (2, 3))


class SnapshotRoundTrip(unittest.TestCase):
    def test_snapshot_is_exact_and_v2_compatible(self) -> None:
        rng = random.Random(77)
        source = Rv32Memory(SIZE)
        self.addCleanup(source.close)
        for _ in range(3000):
            source.store32(BASE + rng.randrange(0, SIZE, 4), rng.getrandbits(32))
        source.store8(BASE + SIZE - 1, 0x5A)
        source.store32(BASE + 0x4000, 0)  # escritura de cero: la página cuenta como escrita
        pages = source.snapshot_pages()
        self.assertTrue(pages and all(isinstance(index, int) and values and values[-1] != 0 for index, values in pages))
        self.assertEqual(sorted(index for index, _ in pages), [index for index, _ in pages])
        restored = Rv32Memory(SIZE)
        self.addCleanup(restored.close)
        restored.store32(BASE + 12, 0xDEADBEEF)  # basura previa que restore_pages debe borrar
        restored.restore_pages(pages)
        self.assertEqual(bytes(restored.lanes), bytes(source.lanes))
        # La restaurada marca las páginas cubiertas por los datos restaurados: toda página con datos
        # distintos de cero está marcada, y las generaciones no bajan respecto a la basura previa.
        for page in range(SIZE >> 12):
            if any(restored.lanes[page << 12:(page + 1) << 12]):
                self.assertIn(page, restored.written_pages())
        self.assertEqual(restored.generation(0), 2)  # basura previa: store de datos (0); borrado de reset (+1); restauración (+1)
        # El formato lo acepta la memoria paginada de VM32 y reproduce las mismas palabras.
        paged = PagedMemory.from_snapshot(SIZE // 4, pages)
        self.assertEqual(paged.read_block(0, SIZE // 4), tuple(source.words_i))
        self.assertEqual(paged.snapshot_pages(), pages)

    def test_restore_validates_before_touching(self) -> None:
        memory = Rv32Memory(SIZE)
        self.addCleanup(memory.close)
        memory.store32(BASE, 7)
        for bad in ("x", [[0]], [[8, [1]]], [[0, []]], [[0, [1 << 31]]], [[0, [-(1 << 31) - 1]]],
                    [[0, [1]], [0, [2]]], [[7, [1] * 4097]], [[-1, [1]]], [[0.0, [1]]], [[0, [True]]],
                    [[True, [1]]], [[0, (1, 2)]], [[0, [1.0]]], [(0, [1])], [[0, [1], 2]]):
            with self.assertRaises(ValueError, msg=repr(bad)[:60]):
                memory.restore_pages(bad)
            self.assertEqual(memory.load32(BASE), 7)
            self.assertEqual(memory.generation(0), 0)             # un store de datos no sube la generación
            self.assertEqual(memory.written_pages(), [0])
        generation = memory.generation(0)
        memory.restore_pages([])
        self.assertEqual(memory.load32(BASE), 0)
        self.assertEqual(memory.snapshot_pages(), [])
        self.assertEqual(memory.written_pages(), [])              # exacto tras borrar: nada escrito
        self.assertEqual(memory.generation(0), generation + 1)    # el borrado sube; nunca baja

    def test_generations_never_go_down_so_a_stale_cache_cannot_match(self) -> None:
        memory = Rv32Memory(SIZE)
        self.addCleanup(memory.close)
        memory.store32(BASE, 0x1111)
        seen = memory.generation(0)
        memory.restore_pages([[0, [0x2222]]])
        self.assertEqual(memory.load32(BASE), 0x2222)
        self.assertGreater(memory.generation(0), seen)
        memory.reset()
        self.assertGreater(memory.generation(0), seen + 1)
        self.assertEqual(memory.load32(BASE), 0)


class SparseSnapshots(unittest.TestCase):
    def test_partially_written_groups_and_odd_ram_size(self) -> None:
        # RAM de 20 KiB: cinco páginas de 4 KiB, un grupo de snapshot entero y otro parcial.
        memory = Rv32Memory(5 * PAGE_BYTES)
        self.addCleanup(memory.close)
        memory.store8(BASE + 3 * PAGE_BYTES + 7, 0xAB)   # solo el cuarto 3 del primer grupo
        memory.store32(BASE + 4 * PAGE_BYTES, 0)          # el grupo parcial, escrito con ceros
        memory.store8(BASE + 5 * PAGE_BYTES - 1, 0x5A)   # último byte de la RAM
        pages = memory.snapshot_pages()
        self.assertEqual([index for index, _ in pages], [0, 1])
        self.assertEqual(pages[0][1][-1] & 0xFFFFFFFF, 0xAB << 24)
        self.assertEqual(len(pages[0][1]), 3 * 1024 + 2)
        self.assertEqual(len(pages[1][1]), 1024)
        restored = Rv32Memory(5 * PAGE_BYTES)
        self.addCleanup(restored.close)
        restored.restore_pages(pages)
        self.assertEqual(bytes(restored.lanes), bytes(memory.lanes))
        self.assertEqual(restored.written_pages(), [0, 1, 2, 3, 4])
        # Un registro que sobresale del grupo parcial se rechaza entero, antes de borrar nada:
        # 1025 palabras desde el grupo 1 (4 096 bytes de RAM) no caben en 20 KiB.
        restored.store32(BASE + 8, 0x1234)
        before = (bytes(restored.lanes), list(restored.generations))
        for bad in ([[1, [1] * 1025]], [[0, [1] * 4096], [1, [1] * 1025]], [[1, [0] * 1024 + [-(1 << 31) - 1]]]):
            with self.assertRaises(ValueError):
                restored.restore_pages(bad)
            self.assertEqual((bytes(restored.lanes), list(restored.generations)), before)
        restored.restore_pages([[1, [1] * 1024]])   # exactamente el grupo parcial sí cabe
        self.assertEqual(restored.load32(BASE + 5 * PAGE_BYTES - 4), 1)
        self.assertEqual(restored.load32(BASE + 8), 0)
        paged = PagedMemory(5 * 1024, page_words=4096)
        for index, values in pages:
            paged.write_block(index * 4096, values)
        self.assertEqual(paged.snapshot_pages(), pages)
        # Un grupo con generación > 0 pero todo a cero no se emite.
        memory.reset()
        memory.store32(BASE, 0)
        self.assertEqual(memory.snapshot_pages(), [])


class Faults(unittest.TestCase):
    def test_out_of_range_and_boundary(self) -> None:
        memory = Rv32Memory(SIZE)
        self.addCleanup(memory.close)
        memory.store32(BASE + SIZE - 4, 0x11223344)
        before = (bytes(memory.lanes), list(memory.generations), bytes(memory.written), bytes(memory.code))
        for address in (BASE - 1, BASE - 4, memory.limit, memory.limit + 4, 0, -1, 0xFFFFFFFF, 1 << 40):
            for load in (memory.load8u, memory.load8s, memory.load16u, memory.load16s, memory.load32):
                with self.assertRaises(AccessFault) as caught:
                    load(address)
                self.assertEqual(caught.exception.address, address)
            for store in (memory.store8, memory.store16, memory.store32):
                with self.assertRaises(AccessFault) as caught:
                    store(address, 1)
                self.assertEqual(caught.exception.address, address)
        for width, address in ((2, memory.limit - 1), (4, memory.limit - 3), (4, memory.limit - 2), (4, memory.limit - 1)):
            load = memory.load16u if width == 2 else memory.load32
            store = memory.store16 if width == 2 else memory.store32
            with self.assertRaises(AccessFault):
                load(address)
            with self.assertRaises(AccessFault):
                store(address, 1)
        for bad in (lambda: memory.read_bytes(BASE, SIZE + 1), lambda: memory.read_bytes(BASE + 1, SIZE),
                    lambda: memory.load_bytes(BASE + SIZE - 3, bytes(4)),
                    lambda: memory.load_bytes(BASE - 1, bytes(1)), lambda: memory.fill([1, 2], BASE + SIZE - 4),
                    lambda: memory.page_of(BASE - 1), lambda: memory.page_of(memory.limit)):
            with self.assertRaises(AccessFault):
                bad()
        with self.assertRaises(ValueError):
            memory.fill([1], BASE + 2)
        with self.assertRaises(ValueError):
            memory.read_bytes(BASE, -1)
        for bad_value in ("x", 1.5, None, b"\x01"):
            for store in (memory.store8, memory.store16, memory.store32):
                with self.assertRaises(TypeError):
                    store(BASE + 5, bad_value)
            with self.assertRaises(TypeError):
                memory.store32(BASE + 4095, bad_value)   # desalineada, cruza página
        with self.assertRaises(IndexError):
            memory.generation(-1)
        with self.assertRaises(IndexError):
            memory.generation(SIZE >> 12)
        for offset, length in ((-1, 4), (SIZE - 2, 4), (SIZE, 1), (-16, 16)):
            with self.assertRaises(IndexError, msg=f"{offset=} {length=}"):
                memory.mark_code(offset, length)
        memory.mark_code(5, 0)             # longitud 0: no marca nada
        memory.mark_code(SIZE - 4, 0)
        self.assertEqual((bytes(memory.lanes), list(memory.generations), bytes(memory.written), bytes(memory.code)), before)
        # Ajustes exactos contra el límite.
        memory.store16(memory.limit - 2, 0xBEEF)
        memory.store32(memory.limit - 4, 0x0BADF00D)
        memory.store8(memory.limit - 1, 0x80)
        self.assertEqual(memory.load8s(memory.limit - 1), -128)
        self.assertEqual(memory.load16u(memory.limit - 2), 0x80AD)
        self.assertEqual(memory.read_bytes(memory.limit - 4, 4), bytes((0x0D, 0xF0, 0xAD, 0x80)))
        self.assertEqual(memory.read_bytes(memory.limit, 0), b"")
        memory.load_bytes(memory.limit - 4, b"\x01\x02\x03\x04")
        memory.load_bytes(memory.limit, b"")
        memory.fill([7], memory.limit - 4)
        self.assertEqual(memory.load32(memory.limit - 4), 7)
        # Solo las escrituras del host (load_bytes y fill) suben la generación: los cuatro stores del invitado
        # no tocan ninguna línea de código. La página sí queda escrita.
        self.assertEqual(memory.generation((SIZE >> 12) - 1), 2)
        self.assertIn((SIZE >> 12) - 1, memory.written_pages())
        # Un registro vacío en una dirección interior no marca su página.
        memory.load_bytes(BASE + 5 * PAGE_BYTES + 5, b"")
        memory.fill([], BASE + 5 * PAGE_BYTES + 4)
        self.assertEqual(memory.generation(5), 0)
        self.assertNotIn(5, memory.written_pages())

    def test_blocks_spanning_pages_and_other_buffer_formats(self) -> None:
        memory = Rv32Memory(SIZE)
        self.addCleanup(memory.close)
        memory.load_bytes(BASE + PAGE_BYTES - 2, bytes((i & 0xFF) | 1 for i in range(2 * PAGE_BYTES + 4)))
        self.assertEqual(memory.written_pages(), [0, 1, 2, 3])
        self.assertEqual(list(memory.generations[:5]), [1, 1, 1, 1, 0])
        memory.fill(range(PAGE_BYTES // 4), BASE + 4 * PAGE_BYTES)  # exactamente una página
        self.assertEqual(list(memory.generations[:6]), [1, 1, 1, 1, 1, 0])
        memory.load_bytes(BASE + 8 * PAGE_BYTES, array("I", [0x44332211, 0x88776655]))
        self.assertEqual(memory.load8u(BASE + 8 * PAGE_BYTES), 0x11)
        self.assertEqual(memory.load32(BASE + 8 * PAGE_BYTES + 4), 0x88776655)
        memory.load_bytes(BASE, memoryview(b"\xFF"))
        self.assertEqual(memory.load8s(BASE), -1)

    def test_constructor_guards_and_close(self) -> None:
        for size, base in ((4096 + 1, BASE), (0, BASE), (-4096, BASE), (4096, 0xFFFFF000 + 4096), (4096, 100),
                           (4096, -4096), (1 << 32, 4096)):
            with self.assertRaises(ValueError, msg=f"{size=} {base=:#x}"):
                Rv32Memory(size, base=base)
        with mock.patch.object(sys, "byteorder", "big"):
            with self.assertRaises(RuntimeError):
                Rv32Memory(4096)
        top = Rv32Memory(4096, base=0xFFFFF000)  # base + size == 2³² se acepta
        self.addCleanup(top.close)
        top.store8(0xFFFFFFFF, 0x7F)
        self.assertEqual((top.limit, top.load8s(0xFFFFFFFF), top.page_of(0xFFFFFFFF)), (1 << 32, 0x7F, 0))
        memory = Rv32Memory(PAGE_BYTES, base=0)
        self.addCleanup(memory.close)
        memory.store32(0, 5)
        self.assertEqual(memory.load32(0), 5)
        memory.close()
        memory.close()
        with self.assertRaises(ValueError):
            memory.load32(0)
        # Una porción viva de las vistas impide cerrar el mmap; al soltarla, close() termina.
        held = Rv32Memory(PAGE_BYTES, base=0)
        self.addCleanup(held.close)
        piece = held.lanes[0:8]
        with self.assertRaises(BufferError):
            held.close()
        piece.release()
        held.close()
        with self.assertRaises(ValueError):
            held.load32(0)


class CodeLineMap(unittest.TestCase):
    def test_line_constants_and_map_sizes(self) -> None:
        self.assertEqual((LINE_SHIFT, LINE_BYTES, LINE_MASK), (4, 16, 15))
        self.assertEqual(PAGE_BYTES % LINE_BYTES, 0)   # una línea nunca cruza página
        self.assertGreaterEqual(LINE_BYTES, 4)         # un sw alineado nunca cruza línea
        memory = Rv32Memory(SIZE)
        self.addCleanup(memory.close)
        self.assertEqual((len(memory.code), len(memory.written)), (SIZE >> LINE_SHIFT, SIZE >> 12))
        self.assertFalse(any(memory.code) or any(memory.written))

    def test_mark_code_and_clear_code(self) -> None:
        memory = Rv32Memory(SIZE)
        self.addCleanup(memory.close)
        memory.mark_code(0x104, 4)                 # una palabra: solo su línea
        self.assertEqual([i for i, c in enumerate(memory.code) if c], [0x10])
        memory.mark_code(0x12C, 8)                 # 0x12C..0x133: dos líneas
        self.assertEqual([i for i, c in enumerate(memory.code) if c], [0x10, 0x12, 0x13])
        memory.mark_code(0x200, 0)                 # longitud 0: nada
        memory.mark_code(0x10F, 2)                 # desalineado: el byte 0x10F y el 0x110 (dos líneas; ciega 3)
        self.assertEqual([i for i, c in enumerate(memory.code) if c], [0x10, 0x11, 0x12, 0x13])
        memory.mark_code(SIZE - 4, 4)              # la última palabra de la RAM
        self.assertEqual([i for i, c in enumerate(memory.code) if c], [0x10, 0x11, 0x12, 0x13, (SIZE >> LINE_SHIFT) - 1])
        self.assertEqual(sum(memory.generations), 0)   # marcar no es escribir
        self.assertEqual(memory.written_pages(), [])
        memory.clear_code()
        self.assertFalse(any(memory.code))
        # Vaciar el mapa sube la generación de las páginas que tenían marcas (0 y la última), y solo de esas:
        # una caché construida sobre ellas (de este hart o de otro) deja de coincidir.
        self.assertEqual((memory.generation(0), memory.generation((SIZE >> 12) - 1), sum(memory.generations)), (1, 1, 2))
        self.assertEqual(memory.written_pages(), [])
        memory.clear_code()                        # sin marcas: no sube nada
        self.assertEqual(sum(memory.generations), 2)

    def test_misaligned_store_into_a_fresh_second_page_marks_it_written(self) -> None:
        # Hallazgo de la revisión ciega 1: la segunda página de un store que cruza página solo recibe esa
        # escritura; si no quedara marcada, el snapshot perdería sus bytes.
        for store, address in ((Rv32Memory.store32, BASE + 0x1000 - 2), (Rv32Memory.store16, BASE + 0x1000 - 1),
                               (Rv32Memory.store32, BASE + 0x4FFF)):
            with self.subTest(store=store.__name__, address=hex(address)):
                memory = Rv32Memory(SIZE)
                self.addCleanup(memory.close)
                store(memory, address, 0x11223344)
                first = (address - BASE) >> 12
                self.assertEqual(memory.written_pages(), [first, first + 1])
                restored = Rv32Memory(SIZE)
                self.addCleanup(restored.close)
                restored.restore_pages(memory.snapshot_pages())
                self.assertEqual(bytes(restored.lanes), bytes(memory.lanes))


if __name__ == "__main__":
    unittest.main()
