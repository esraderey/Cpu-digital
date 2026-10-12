"""E2 de RFC-EXP-00018: carriles de bytes sobre la memoria paginada y anillo de deshacer.

Mide, sin tocar el núcleo, lo que costaría en el hart RV32 (P1.1 y P1.10) leer y
escribir bytes y medias palabras sobre el almacén actual (``PagedMemory``: páginas
``array('i')`` de 4 096 palabras) frente al acceso por palabra, y lo que añade un
anillo de deshacer. Dos vías para los carriles:

* ``desplazar`` (la que describe el texto de P1.1): leer la palabra y extraer el
  byte con desplazamiento y máscara; escribir con lectura-modificación-escritura.
* ``vista``: una ``memoryview`` de formato ``B`` por página, sin copia y sobre el
  mismo ``array``, indexada por byte; las palabras alineadas usan una vista ``I``.

Cada acceso comprueba que la dirección cae en la RAM y resta la base
``0x80000000``; lo demás es el carril. Las páginas de la región están
materializadas y sus vistas cacheadas (el hart consultará la caché con ``get`` y
materializará al vuelo; aquí una página ausente lanza ``KeyError``). Tres medidas:

1. coste por acceso de cada operación en un bucle de llamadas sobre datos
   aleatorios, bruto y neto de un bucle con un método vacío de la misma aridad;
2. ``memcpy`` por palabras, ``memcpy`` por bytes y ``strcpy`` en un intérprete
   mínimo (tabla de manejadores, gas y recuento por instrucción, guarda de x0, un
   salto por vuelta), en ns por instrucción; le faltan la decodificación desde
   memoria y los traps, así que los sobrecostes relativos son cotas superiores;
3. los mismos programas con dos anillos ``deque(maxlen=4096)``: uno que guarda
   ``(dirección, valor anterior)`` solo en las escrituras a memoria, y el que haría
   falta para deshacer N instrucciones, con una entrada ``(pc, destino, valor
   anterior)`` por instrucción (registro o palabra de memoria; los saltos solo pc).

Criterios del RFC (§5, E2): P1.1 muere si el acceso por byte cuesta más de 1,5× el
acceso por palabra (se juzgan las dos vías por separado, con el cociente neto, que
es el más severo, y frente a la palabra alineada por vista ``I``, el camino que
tendrá el hart, no el ``PagedMemory[...]`` actual); el anillo se retira si encarece
memcpy/strcpy más del 25 % (se juzga el anillo que deshace instrucciones; el de solo
escrituras se informa). Un veredicto a menos de 0,1× o de 3 puntos del umbral se
marca «en el umbral»: el ruido de la medida es de ±0,05× y ±3 puntos. Antes de
medir, cada operación se contrasta con un modelo ``bytearray``: direcciones al azar,
los cuatro desplazamientos, los cruces de página, los límites de la RAM y valores
negativos o de más de 32 bits (``--verificar`` solo hace eso).

Con ``--plana`` se mide además la alternativa que adoptó P1.1 el 2026-10-10: la RAM
entera en un ``mmap`` anónimo (el sistema operativo materializa cada página de 4 KiB al
tocarla) con tres vistas ``B``/``I``/``i`` y un mapa de bits de páginas escritas, sin
páginas en diccionario; sus accesos se comparan con los de las vistas por página.

Uso (desde la raíz del repositorio):
    python benchmarks/benchmark_rv32_memoria.py [--rondas 7] [--accesos 200000] [--plana] [--json informe.json]
    python benchmarks/benchmark_rv32_memoria.py --verificar
"""

from __future__ import annotations

import argparse
import json
import math
import mmap
import platform
import random
import statistics
import sys
import time
from collections import deque
from pathlib import Path
from typing import Callable

from cpu_digital.memory import PAGE_WORDS, PagedMemory

BASE = 0x80000000          # la RAM del hart empieza aquí (RFC-EXP-00018, P1.11)
PAGE_SHIFT = 14            # 4 096 palabras = 16 KiB por página
PAGE_MASK = (1 << PAGE_SHIFT) - 1
REGION_BYTES = 1 << 17     # 128 KiB tocados por el microbanco
RING_SIZE = 4_096
MEMORY_WORDS = 1 << 16     # 256 KiB de RAM lógica bastan para la región
LANE_THRESHOLD = 1.5       # «más de 1,5×» mata P1.1
RING_THRESHOLD = 0.25      # «más del 25 %» retira el anillo
LANE_MARGIN = 0.1          # a menos de esto del umbral, el veredicto se marca «en el umbral»
RING_MARGIN = 0.03
GAS = 1_000_000_000        # gas del intérprete mínimo: cabe en un dígito de CPython, como un gas real
LANE_SHIFT = (0, 8, 16, 24)
LANE_KEEP = tuple(0xFFFFFFFF ^ (0xFF << shift) for shift in LANE_SHIFT)  # máscaras precalculadas


class AccessFault(Exception):
    pass


class LaneMemory:
    """Capa de bytes sobre ``PagedMemory``: una vista por página, sin copia."""

    __slots__ = ("memory", "limit", "lanes", "words_u", "words_i", "ring")

    def __init__(self, memory: PagedMemory) -> None:
        if memory.page_words != PAGE_WORDS or PAGE_WORDS * 4 != 1 << PAGE_SHIFT:
            raise ValueError("El microbanco supone páginas de 4 096 palabras (16 KiB)")
        self.memory = memory
        self.limit = BASE + len(memory) * 4
        self.lanes: dict[int, memoryview] = {}
        self.words_u: dict[int, memoryview] = {}
        self.words_i: dict[int, memoryview] = {}
        self.ring: deque[tuple[int, ...]] = deque(maxlen=RING_SIZE)

    def materialize(self, first: int, count: int) -> None:
        """Materializa las páginas de ``count`` bytes desde ``first`` y cachea sus vistas."""
        for page in range((first - BASE) >> PAGE_SHIFT, ((first - BASE + count - 1) >> PAGE_SHIFT) + 1):
            base = page * PAGE_WORDS
            # PagedMemory solo materializa una página al escribir un valor distinto de cero;
            # se deja la primera palabra como estaba.
            previous = self.memory[base]
            self.memory[base] = 1
            self.memory[base] = previous
            view = self.memory.view_bytes(base, PAGE_WORDS)
            self.lanes[page] = view
            self.words_u[page] = view.cast("I")
            self.words_i[page] = view.cast("i")

    # ── Suelos: métodos vacíos con la aridad de una carga y de una escritura ──

    def nothing_load(self, addr: int) -> None:
        return None

    def nothing_store(self, addr: int, value: int) -> None:
        return None

    # ── Palabra alineada (vista ``I``) ──

    def lw(self, addr: int) -> int:
        if addr & 3 or not BASE <= addr < self.limit:
            raise AccessFault(addr)
        o = addr - BASE
        return self.words_u[o >> PAGE_SHIFT][(o & PAGE_MASK) >> 2]

    def sw(self, addr: int, value: int) -> None:
        if addr & 3 or not BASE <= addr < self.limit:
            raise AccessFault(addr)
        o = addr - BASE
        self.words_u[o >> PAGE_SHIFT][(o & PAGE_MASK) >> 2] = value & 0xFFFFFFFF

    # ── Palabra por el camino actual del intérprete (PagedMemory; devuelve sin signo) ──

    def lw_paged(self, addr: int) -> int:
        if addr & 3 or not BASE <= addr < self.limit:
            raise AccessFault(addr)
        return self.memory[(addr - BASE) >> 2] & 0xFFFFFFFF

    def sw_paged(self, addr: int, value: int) -> None:
        if addr & 3 or not BASE <= addr < self.limit:
            raise AccessFault(addr)
        self.memory[(addr - BASE) >> 2] = value

    # ── Byte y media palabra por vista ``B`` ──

    def lbu(self, addr: int) -> int:
        if not BASE <= addr < self.limit:
            raise AccessFault(addr)
        o = addr - BASE
        return self.lanes[o >> PAGE_SHIFT][o & PAGE_MASK]

    def lb(self, addr: int) -> int:
        if not BASE <= addr < self.limit:
            raise AccessFault(addr)
        o = addr - BASE
        b = self.lanes[o >> PAGE_SHIFT][o & PAGE_MASK]
        return b - 256 if b > 127 else b

    def sb(self, addr: int, value: int) -> None:
        if not BASE <= addr < self.limit:
            raise AccessFault(addr)
        o = addr - BASE
        self.lanes[o >> PAGE_SHIFT][o & PAGE_MASK] = value & 0xFF

    def lhu(self, addr: int) -> int:
        if not BASE <= addr < self.limit - 1:
            raise AccessFault(addr)
        o = addr - BASE
        i = o & PAGE_MASK
        if i == PAGE_MASK:  # cruza de página: dos accesos de byte
            return self.lbu(addr) | (self.lbu(addr + 1) << 8)
        page = self.lanes[o >> PAGE_SHIFT]
        return page[i] | (page[i + 1] << 8)

    def lh(self, addr: int) -> int:
        if not BASE <= addr < self.limit - 1:
            raise AccessFault(addr)
        o = addr - BASE
        i = o & PAGE_MASK
        if i == PAGE_MASK:
            h = self.lbu(addr) | (self.lbu(addr + 1) << 8)
        else:
            page = self.lanes[o >> PAGE_SHIFT]
            h = page[i] | (page[i + 1] << 8)
        return h - 65536 if h > 32767 else h

    def sh(self, addr: int, value: int) -> None:
        if not BASE <= addr < self.limit - 1:
            raise AccessFault(addr)
        o = addr - BASE
        i = o & PAGE_MASK
        if i == PAGE_MASK:
            self.sb(addr, value)
            self.sb(addr + 1, value >> 8)
            return
        page = self.lanes[o >> PAGE_SHIFT]
        page[i] = value & 0xFF
        page[i + 1] = (value >> 8) & 0xFF

    def lw_unaligned(self, addr: int) -> int:
        if not BASE <= addr < self.limit - 3:
            raise AccessFault(addr)
        o = addr - BASE
        i = o & PAGE_MASK
        if i > PAGE_MASK - 3:
            return (self.lbu(addr) | (self.lbu(addr + 1) << 8)
                    | (self.lbu(addr + 2) << 16) | (self.lbu(addr + 3) << 24))
        return int.from_bytes(self.lanes[o >> PAGE_SHIFT][i:i + 4], "little")

    # ── Byte por desplazamiento sobre la palabra (vistas ``i``/``I``): la vía del texto de P1.1 ──

    def lbu_shift(self, addr: int) -> int:
        if not BASE <= addr < self.limit:
            raise AccessFault(addr)
        o = addr - BASE
        word = self.words_i[o >> PAGE_SHIFT][(o & PAGE_MASK) >> 2]
        return (word >> ((o & 3) << 3)) & 0xFF

    def lb_shift(self, addr: int) -> int:
        if not BASE <= addr < self.limit:
            raise AccessFault(addr)
        o = addr - BASE
        word = self.words_i[o >> PAGE_SHIFT][(o & PAGE_MASK) >> 2]
        b = (word >> ((o & 3) << 3)) & 0xFF
        return b - 256 if b > 127 else b

    def sb_shift(self, addr: int, value: int) -> None:
        if not BASE <= addr < self.limit:
            raise AccessFault(addr)
        o = addr - BASE
        page = self.words_u[o >> PAGE_SHIFT]
        i = (o & PAGE_MASK) >> 2
        lane = o & 3
        page[i] = (page[i] & LANE_KEEP[lane]) | ((value & 0xFF) << LANE_SHIFT[lane])

    # ── Escrituras con anillo: (dirección, valor anterior) ──

    def sw_ring(self, addr: int, value: int) -> None:
        if addr & 3 or not BASE <= addr < self.limit:
            raise AccessFault(addr)
        o = addr - BASE
        page = self.words_u[o >> PAGE_SHIFT]
        i = (o & PAGE_MASK) >> 2
        self.ring.append((addr, page[i]))
        page[i] = value & 0xFFFFFFFF

    def sb_ring(self, addr: int, value: int) -> None:
        if not BASE <= addr < self.limit:
            raise AccessFault(addr)
        o = addr - BASE
        page = self.lanes[o >> PAGE_SHIFT]
        i = o & PAGE_MASK
        self.ring.append((addr, page[i]))
        page[i] = value & 0xFF

    # ── Escrituras del anillo que deshace instrucciones: (pc, dirección, valor anterior) ──

    def sw_undo(self, pc: int, addr: int, value: int) -> None:
        if addr & 3 or not BASE <= addr < self.limit:
            raise AccessFault(addr)
        o = addr - BASE
        page = self.words_u[o >> PAGE_SHIFT]
        i = (o & PAGE_MASK) >> 2
        self.ring.append((pc, addr, page[i]))
        page[i] = value & 0xFFFFFFFF

    def sb_undo(self, pc: int, addr: int, value: int) -> None:
        if not BASE <= addr < self.limit:
            raise AccessFault(addr)
        o = addr - BASE
        page = self.lanes[o >> PAGE_SHIFT]
        i = o & PAGE_MASK
        self.ring.append((pc, addr, page[i]))
        page[i] = value & 0xFF


class FlatMemory:
    """RAM contigua en un ``mmap`` anónimo con vistas ``B``/``I``/``i`` y mapa de bits de 4 KiB escritas.

    Es el diseño de P1.1 enmendado: sin páginas en diccionario, sin caché derivada (las
    vistas son función del búfer) y sin cruces de página. Exige host little-endian.
    """

    __slots__ = ("buffer", "lanes", "words_u", "words_i", "written", "limit", "size")

    def __init__(self, size: int) -> None:
        if sys.byteorder != "little":
            raise RuntimeError("Los carriles nativos exigen un host little-endian")
        if size <= 0 or size & 0xFFF:
            raise ValueError("El tamaño de la RAM debe ser un múltiplo positivo de 4 KiB")
        self.size = size
        self.buffer = mmap.mmap(-1, size)
        self.lanes = memoryview(self.buffer)
        self.words_u = self.lanes.cast("I")
        self.words_i = self.lanes.cast("i")
        self.written = bytearray(size >> 12)
        self.limit = BASE + size

    def close(self) -> None:
        self.words_i.release()
        self.words_u.release()
        self.lanes.release()
        self.buffer.close()

    def nothing_load(self, addr: int) -> None:
        return None

    def nothing_store(self, addr: int, value: int) -> None:
        return None

    def lw(self, addr: int) -> int:
        if addr & 3 or not BASE <= addr < self.limit:
            raise AccessFault(addr)
        return self.words_u[(addr - BASE) >> 2]

    def sw(self, addr: int, value: int) -> None:
        if addr & 3 or not BASE <= addr < self.limit:
            raise AccessFault(addr)
        o = addr - BASE
        self.written[o >> 12] = 1
        self.words_u[o >> 2] = value & 0xFFFFFFFF

    def lbu(self, addr: int) -> int:
        if not BASE <= addr < self.limit:
            raise AccessFault(addr)
        return self.lanes[addr - BASE]

    def lb(self, addr: int) -> int:
        if not BASE <= addr < self.limit:
            raise AccessFault(addr)
        b = self.lanes[addr - BASE]
        return b - 256 if b > 127 else b

    def sb(self, addr: int, value: int) -> None:
        if not BASE <= addr < self.limit:
            raise AccessFault(addr)
        o = addr - BASE
        self.written[o >> 12] = 1
        self.lanes[o] = value & 0xFF

    def lhu(self, addr: int) -> int:
        if not BASE <= addr < self.limit - 1:
            raise AccessFault(addr)
        o = addr - BASE
        return self.lanes[o] | (self.lanes[o + 1] << 8)

    def lh(self, addr: int) -> int:
        if not BASE <= addr < self.limit - 1:
            raise AccessFault(addr)
        o = addr - BASE
        h = self.lanes[o] | (self.lanes[o + 1] << 8)
        return h - 65536 if h > 32767 else h

    def sh(self, addr: int, value: int) -> None:
        if not BASE <= addr < self.limit - 1:
            raise AccessFault(addr)
        o = addr - BASE
        self.written[o >> 12] = 1
        self.written[(o + 1) >> 12] = 1
        self.lanes[o] = value & 0xFF
        self.lanes[o + 1] = (value >> 8) & 0xFF

    def lw_unaligned(self, addr: int) -> int:
        if not BASE <= addr < self.limit - 3:
            raise AccessFault(addr)
        o = addr - BASE
        return int.from_bytes(self.lanes[o:o + 4], "little")

    def written_pages(self) -> list[int]:
        return [index for index, flag in enumerate(self.written) if flag]


def verify_flat(flat: FlatMemory, seed: int = 4096) -> int:
    """Contrasta ``FlatMemory`` con un ``bytearray`` y comprueba la propiedad del mapa de bits."""
    rng = random.Random(seed)
    size = flat.size
    model = bytearray(size)
    checks = 0

    def check(condition: bool, what: str) -> None:
        nonlocal checks
        if not condition:
            raise AssertionError(what)
        checks += 1

    def value() -> int:
        kind = rng.randrange(3)
        if kind == 0:
            return rng.getrandbits(32)
        if kind == 1:
            return rng.getrandbits(32) - (1 << 32)
        return rng.getrandbits(40)

    def reads(addr: int) -> None:
        o = addr - BASE
        check(flat.lbu(addr) == model[o], f"lbu en {addr:#x}")
        check(flat.lb(addr) == int.from_bytes(model[o:o + 1], "little", signed=True), f"lb en {addr:#x}")
        check(flat.lw(addr & ~3) == int.from_bytes(model[o & ~3:(o & ~3) + 4], "little"), f"lw en {addr:#x}")
        if o + 1 < size:
            check(flat.lhu(addr) == int.from_bytes(model[o:o + 2], "little"), f"lhu en {addr:#x}")
            check(flat.lh(addr) == int.from_bytes(model[o:o + 2], "little", signed=True), f"lh en {addr:#x}")
        if o + 3 < size:
            check(flat.lw_unaligned(addr) == int.from_bytes(model[o:o + 4], "little"), f"lw_unaligned en {addr:#x}")

    hot = [rng.randrange(size >> 12) for _ in range(8)]  # unas pocas páginas concentran las escrituras
    for _ in range(20_000):
        page = rng.choice(hot) if rng.random() < 0.8 else rng.randrange(size >> 12)
        addr = BASE + (page << 12) + rng.randrange(4096)
        o = addr - BASE
        kind = rng.randrange(4)
        if kind == 0:
            reads(addr)
        elif kind == 1:
            v = value()
            flat.sb(addr, v)
            model[o] = v & 0xFF
            checks += 1
        elif kind == 2 and o + 1 < size:
            v = value()
            flat.sh(addr, v)
            model[o:o + 2] = (v & 0xFFFF).to_bytes(2, "little")
            checks += 1
        else:
            v = value()
            flat.sw(addr & ~3, v)
            model[o & ~3:(o & ~3) + 4] = (v & 0xFFFFFFFF).to_bytes(4, "little")
            checks += 1
    for addr in (BASE, BASE + 1, BASE + 2, BASE + 3, flat.limit - 4, flat.limit - 3, flat.limit - 2, flat.limit - 1):
        reads(addr)
    for addr in (BASE - 4, BASE - 1, flat.limit, flat.limit + 4, -1):
        for op in (flat.lw, flat.lb, flat.lbu, flat.lh, flat.lhu, flat.lw_unaligned):
            try:
                op(addr)
            except AccessFault:
                checks += 1
            else:
                raise AssertionError(f"{op.__name__} no falló en {addr:#x}")
        for op in (flat.sw, flat.sb, flat.sh):
            try:
                op(addr, 1)
            except AccessFault:
                checks += 1
            else:
                raise AssertionError(f"{op.__name__} no falló en {addr:#x}")
    for addr in (BASE + 1, flat.limit - 2):
        try:
            flat.lw(addr)
        except AccessFault:
            checks += 1
        else:
            raise AssertionError("lw aceptó una dirección desalineada")
    check(bytes(flat.lanes) == bytes(model), "la RAM entera difiere del modelo")
    # Propiedad del snapshot: toda página con algún byte distinto de cero está marcada.
    marked = set(flat.written_pages())
    for page in range(size >> 12):
        if any(model[page << 12:(page + 1) << 12]):
            check(page in marked, f"página {page} escrita y sin marcar")
    check(len(marked) <= len(hot) + 20_000 and len(marked) > 0, "el mapa de bits no tiene páginas escritas")
    return checks


# ── Oráculo: modelo bytearray little-endian ──

def verify(lanes: LaneMemory, region: int, size: int, seed: int = 2024) -> int:
    """Contrasta cada operación con un ``bytearray``; devuelve el número de comprobaciones.

    No usa ``assert``: las comprobaciones sobreviven a ``python -O``.
    """
    rng = random.Random(seed)
    model = bytearray(size)
    checks = 0
    end = region + size

    def check(condition: bool, what: str) -> None:
        nonlocal checks
        if not condition:
            raise AssertionError(what)
        checks += 1

    def region_bytes() -> bytes:
        return bytes(lanes.memory.view_bytes((region - BASE) >> 2, size >> 2))

    def random_value() -> int:
        kind = rng.randrange(4)
        if kind == 0:
            return rng.getrandbits(32)
        if kind == 1:
            return rng.getrandbits(32) - (1 << 32)  # negativo
        if kind == 2:
            return rng.getrandbits(40)              # más de 32 bits
        return rng.choice((0, 0x80, 0x8000, 0x80000000, 0xFF, 0xFFFF, 0xFFFFFFFF, -1, -129, -(1 << 31)))

    def write(kind: str, addr: int, value: int) -> None:
        nonlocal checks
        offset = addr - region
        if kind in ("sw", "sw_ring", "sw_paged"):
            getattr(lanes, kind)(addr, value)
            model[offset:offset + 4] = (value & 0xFFFFFFFF).to_bytes(4, "little")
        elif kind == "sw_undo":
            lanes.sw_undo(0, addr, value)
            model[offset:offset + 4] = (value & 0xFFFFFFFF).to_bytes(4, "little")
        elif kind in ("sb", "sb_shift", "sb_ring"):
            getattr(lanes, kind)(addr, value)
            model[offset] = value & 0xFF
        elif kind == "sb_undo":
            lanes.sb_undo(0, addr, value)
            model[offset] = value & 0xFF
        else:
            lanes.sh(addr, value)
            model[offset:offset + 2] = (value & 0xFFFF).to_bytes(2, "little")
        checks += 1

    def check_reads(addr: int) -> None:
        offset = addr - region
        word = offset & ~3
        unsigned_word = int.from_bytes(model[word:word + 4], "little")
        check(lanes.lw(addr & ~3) == unsigned_word, f"lw en {addr:#x}")
        check(lanes.lw_paged(addr & ~3) == unsigned_word, f"lw_paged en {addr:#x}")
        check(lanes.memory[(addr - BASE) >> 2] == int.from_bytes(model[word:word + 4], "little", signed=True),
              f"palabra con signo de PagedMemory en {addr:#x}")
        byte = model[offset]
        check(lanes.lbu(addr) == byte, f"lbu en {addr:#x}")
        check(lanes.lbu_shift(addr) == byte, f"lbu_shift en {addr:#x}")
        signed_byte = byte - 256 if byte > 127 else byte
        check(lanes.lb(addr) == signed_byte, f"lb en {addr:#x}")
        check(lanes.lb_shift(addr) == signed_byte, f"lb_shift en {addr:#x}")
        if offset + 1 < size:
            check(lanes.lhu(addr) == int.from_bytes(model[offset:offset + 2], "little"), f"lhu en {addr:#x}")
            check(lanes.lh(addr) == int.from_bytes(model[offset:offset + 2], "little", signed=True), f"lh en {addr:#x}")
        if offset + 3 < size:
            check(lanes.lw_unaligned(addr) == int.from_bytes(model[offset:offset + 4], "little"),
                  f"lw_unaligned en {addr:#x}")

    def expect_fault(op: Callable[..., object], *args: int) -> None:
        try:
            op(*args)
        except AccessFault:
            check(True, "")
        else:
            raise AssertionError(f"{op.__name__}{args} no falló")

    word_kinds = ("sw", "sw_ring", "sw_undo", "sw_paged")
    byte_kinds = ("sb", "sb_shift", "sb_ring", "sb_undo")
    # 1) Direcciones y valores al azar.
    for _ in range(20_000):
        if rng.random() < 0.45:
            check_reads(region + rng.randrange(size))
        else:
            kind = rng.choice(word_kinds + byte_kinds + ("sh",))
            if kind in word_kinds:
                addr = region + rng.randrange(0, size, 4)
            elif kind == "sh":
                addr = region + rng.randrange(size - 1)
            else:
                addr = region + rng.randrange(size)
            write(kind, addr, random_value())
    # 2) Bordes forzados: los cuatro últimos y los cuatro primeros bytes de cada página, y los de la región.
    edges: list[int] = []
    for page_start in range(region, end + 1, 1 << PAGE_SHIFT):
        edges.extend(a for a in range(page_start - 4, page_start + 4) if region <= a < end)
    for addr in edges:
        for kind in byte_kinds:
            write(kind, addr, random_value())
            check_reads(addr)
        if addr + 1 < end:
            write("sh", addr, random_value())
            check_reads(addr)
        if addr % 4 == 0:
            for kind in word_kinds:
                write(kind, addr, random_value())
                check_reads(addr)
    # 3) Toda la región, byte a byte, frente al modelo.
    check(region_bytes() == bytes(model), "la región entera difiere del modelo")
    # 4) Límites de la RAM: la última página se materializa para distinguir fallo de rango de página ausente.
    limit = lanes.limit
    lanes.materialize(limit - (1 << PAGE_SHIFT), 1 << PAGE_SHIFT)
    lanes.sw(limit - 4, 0x89ABCDEF)
    lanes.sh(limit - 2, 0x1234)
    lanes.sb(limit - 1, 0x5A)
    check(lanes.lb(limit - 1) == lanes.lb_shift(limit - 1) == 0x5A, "lb en el último byte de la RAM")
    check(lanes.lhu(limit - 2) == 0x5A34, "lhu en los dos últimos bytes de la RAM")
    check(lanes.lw_unaligned(limit - 4) == lanes.lw(limit - 4) == 0x5A34CDEF, "lw en la última palabra de la RAM")
    lanes.sb(limit - 1, 0x80)
    lanes.sb_undo(7, limit - 1, 0x01)
    check(lanes.ring[-1] == (7, limit - 1, 0x80) and lanes.lbu(limit - 1) == 0x01, "sb_undo en el último byte de la RAM")
    lanes.materialize(BASE, 1 << PAGE_SHIFT)
    lanes.sw(BASE, 0x11223344)
    lanes.sb(BASE, 0xAA)
    check(lanes.lw(BASE) == 0x112233AA and lanes.lb(BASE) == -86 and lanes.lbu_shift(BASE) == 0xAA,
          "accesos en la primera dirección de la RAM")
    check(RING_SIZE == 4096 and lanes.ring.maxlen == 4096, "el anillo no mide 4 096 entradas")
    before_faults = region_bytes()
    ring_before = len(lanes.ring)
    for addr in (BASE - 4, BASE - 1, limit, limit + 4, 0, 0xFFFFFFFF, -1):
        for op in (lanes.lw, lanes.lw_paged, lanes.lb, lanes.lbu, lanes.lb_shift, lanes.lbu_shift, lanes.lh, lanes.lhu,
                   lanes.lw_unaligned):
            expect_fault(op, addr)
        for op in (lanes.sw, lanes.sw_paged, lanes.sw_ring, lanes.sb, lanes.sb_shift, lanes.sb_ring, lanes.sh):
            expect_fault(op, addr, 1)
        expect_fault(lanes.sw_undo, 0, addr, 1)
        expect_fault(lanes.sb_undo, 0, addr, 1)
    for addr in (BASE + 1, BASE + 2, BASE + 3, limit - 3, limit - 2, limit - 1):
        for op in (lanes.lw, lanes.lw_paged):
            expect_fault(op, addr)
        for op in (lanes.sw, lanes.sw_paged, lanes.sw_ring):
            expect_fault(op, addr, 1)
        expect_fault(lanes.sw_undo, 0, addr, 1)
    expect_fault(lanes.lh, limit - 1)
    expect_fault(lanes.lhu, limit - 1)
    expect_fault(lanes.sh, limit - 1, 1)
    for addr in (limit - 3, limit - 2, limit - 1):
        expect_fault(lanes.lw_unaligned, addr)
    check(region_bytes() == before_faults, "una escritura fallida tocó la memoria")
    check(len(lanes.ring) == ring_before, "una escritura fallida anotó en el anillo")
    # 5) Los anillos guardan el valor anterior y deshacer en orden inverso restaura la memoria.
    lanes.ring.clear()
    before = region_bytes()
    operations: list[tuple[str, int]] = []
    for _ in range(RING_SIZE // 2):
        if rng.random() < 0.5:
            addr = region + rng.randrange(0, size, 4)
            lanes.sw_ring(addr, random_value())
            operations.append(("w", addr))
        else:
            addr = region + rng.randrange(size)
            lanes.sb_ring(addr, random_value())
            operations.append(("b", addr))
    check(len(lanes.ring) == len(operations), "el anillo no tiene una entrada por escritura")
    while lanes.ring:
        addr, previous = lanes.ring.pop()
        kind, expected_addr = operations.pop()
        check(addr == expected_addr, "el anillo no guarda la dirección escrita")
        (lanes.sw if kind == "w" else lanes.sb)(addr, previous)
    check(region_bytes() == before, "deshacer el anillo no restauró la memoria")
    lanes.ring.clear()
    old = lanes.lw(region)
    lanes.sw_ring(region, old ^ 0xA5A5A5A5)
    check(lanes.ring[-1] == (region, old), "sw_ring no guarda la palabra anterior")
    lanes.sb_ring(region + 5, 0x77)
    check(lanes.ring[-1] == (region + 5, model[5]), "sb_ring no guarda el byte anterior")
    lanes.sw_undo(1234, region + 8, 1)
    check(lanes.ring[-1] == (1234, region + 8, int.from_bytes(model[8:12], "little")), "sw_undo no guarda (pc, dirección, anterior)")
    lanes.sb_undo(99, region + 13, 2)
    check(lanes.ring[-1] == (99, region + 13, model[13]), "sb_undo no guarda (pc, dirección, anterior)")
    for _ in range(RING_SIZE + 10):
        lanes.sw_ring(region, 0)
    check(len(lanes.ring) == RING_SIZE, "el anillo no respeta su tamaño")
    return checks


# ── Intérprete mínimo: tabla de manejadores ──

Regs = list[int]
Handler = Callable[[Regs, LaneMemory, int, int, int, int, int], int]
MASK32 = 0xFFFFFFFF


def op_lw(regs: Regs, mem: LaneMemory, rd: int, rs1: int, rs2: int, imm: int, pc: int) -> int:
    value = mem.lw((regs[rs1] + imm) & MASK32)
    if rd:
        regs[rd] = value
    return pc + 1


def op_lbu(regs: Regs, mem: LaneMemory, rd: int, rs1: int, rs2: int, imm: int, pc: int) -> int:
    value = mem.lbu((regs[rs1] + imm) & MASK32)
    if rd:
        regs[rd] = value
    return pc + 1


def op_sw(regs: Regs, mem: LaneMemory, rd: int, rs1: int, rs2: int, imm: int, pc: int) -> int:
    mem.sw((regs[rs1] + imm) & MASK32, regs[rs2])
    return pc + 1


def op_sb(regs: Regs, mem: LaneMemory, rd: int, rs1: int, rs2: int, imm: int, pc: int) -> int:
    mem.sb((regs[rs1] + imm) & MASK32, regs[rs2])
    return pc + 1


def op_addi(regs: Regs, mem: LaneMemory, rd: int, rs1: int, rs2: int, imm: int, pc: int) -> int:
    if rd:
        regs[rd] = (regs[rs1] + imm) & MASK32
    return pc + 1


def op_bnez(regs: Regs, mem: LaneMemory, rd: int, rs1: int, rs2: int, imm: int, pc: int) -> int:
    return imm if regs[rs1] else pc + 1


# Anillo solo de escrituras a memoria.

def op_sw_ring(regs: Regs, mem: LaneMemory, rd: int, rs1: int, rs2: int, imm: int, pc: int) -> int:
    mem.sw_ring((regs[rs1] + imm) & MASK32, regs[rs2])
    return pc + 1


def op_sb_ring(regs: Regs, mem: LaneMemory, rd: int, rs1: int, rs2: int, imm: int, pc: int) -> int:
    mem.sb_ring((regs[rs1] + imm) & MASK32, regs[rs2])
    return pc + 1


# Anillo que deshace instrucciones: una entrada por instrucción.

def op_lw_undo(regs: Regs, mem: LaneMemory, rd: int, rs1: int, rs2: int, imm: int, pc: int) -> int:
    value = mem.lw((regs[rs1] + imm) & MASK32)
    mem.ring.append((pc, rd, regs[rd]))
    if rd:
        regs[rd] = value
    return pc + 1


def op_lbu_undo(regs: Regs, mem: LaneMemory, rd: int, rs1: int, rs2: int, imm: int, pc: int) -> int:
    value = mem.lbu((regs[rs1] + imm) & MASK32)
    mem.ring.append((pc, rd, regs[rd]))
    if rd:
        regs[rd] = value
    return pc + 1


def op_sw_undo(regs: Regs, mem: LaneMemory, rd: int, rs1: int, rs2: int, imm: int, pc: int) -> int:
    mem.sw_undo(pc, (regs[rs1] + imm) & MASK32, regs[rs2])
    return pc + 1


def op_sb_undo(regs: Regs, mem: LaneMemory, rd: int, rs1: int, rs2: int, imm: int, pc: int) -> int:
    mem.sb_undo(pc, (regs[rs1] + imm) & MASK32, regs[rs2])
    return pc + 1


def op_addi_undo(regs: Regs, mem: LaneMemory, rd: int, rs1: int, rs2: int, imm: int, pc: int) -> int:
    mem.ring.append((pc, rd, regs[rd]))
    if rd:
        regs[rd] = (regs[rs1] + imm) & MASK32
    return pc + 1


def op_bnez_undo(regs: Regs, mem: LaneMemory, rd: int, rs1: int, rs2: int, imm: int, pc: int) -> int:
    mem.ring.append((pc, -1, 0))
    return imm if regs[rs1] else pc + 1


Instruction = tuple[Handler, int, int, int, int]
A0, A1, A2, T0 = 10, 11, 12, 5


def program_memcpy(step: int, load: Handler, store: Handler, alu: Handler, branch: Handler) -> list[Instruction]:
    return [
        (load, T0, A1, 0, 0),
        (store, 0, A0, T0, 0),
        (alu, A0, A0, 0, step),
        (alu, A1, A1, 0, step),
        (alu, A2, A2, 0, -step),
        (branch, 0, A2, 0, 0),
    ]


def program_strcpy(load: Handler, store: Handler, alu: Handler, branch: Handler) -> list[Instruction]:
    return [
        (load, T0, A1, 0, 0),
        (store, 0, A0, T0, 0),
        (alu, A0, A0, 0, 1),
        (alu, A1, A1, 0, 1),
        (branch, 0, T0, 0, 0),
    ]


def run(program: list[Instruction], regs: Regs, mem: LaneMemory, gas: int = GAS) -> int:
    """Ejecuta hasta salir del programa o agotar el gas; devuelve las instrucciones ejecutadas."""
    pc = 0
    executed = 0
    end = len(program)
    while pc < end and gas > 0:
        handler, rd, rs1, rs2, imm = program[pc]
        pc = handler(regs, mem, rd, rs1, rs2, imm, pc)
        gas -= 1
        executed += 1
    return executed


# ── Medición ──

def measure(fn: Callable[[], object], rounds: int) -> tuple[float, float]:
    """Mínimo y mediana del tiempo de ``fn`` en segundos."""
    samples = []
    for _ in range(rounds):
        started = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - started)
    return min(samples), statistics.median(samples)


def access_rows(lanes: LaneMemory, region: int, size: int, rounds: int, count: int) -> list[dict[str, object]]:
    rng = random.Random(7)
    aligned = [region + rng.randrange(0, size, 4) for _ in range(count)]
    anywhere = [region + rng.randrange(size - 4) for _ in range(count)]
    values = [rng.getrandbits(32) for _ in range(count)]
    aligned_pairs = list(zip(aligned, values))
    anywhere_pairs = list(zip(anywhere, values))

    def loads(fn: Callable[[int], object], addrs: list[int]) -> Callable[[], None]:
        def body() -> None:
            for addr in addrs:
                fn(addr)
        return body

    def stores(fn: Callable[[int, int], None], pairs: list[tuple[int, int]]) -> Callable[[], None]:
        def body() -> None:
            for addr, value in pairs:
                fn(addr, value)
        return body

    cases: list[tuple[str, str, Callable[[], None]]] = [
        ("vacío_carga", "bucle con un método vacío de un argumento", loads(lanes.nothing_load, aligned)),
        ("vacío_escritura", "bucle con un método vacío de dos argumentos", stores(lanes.nothing_store, aligned_pairs)),
        ("lw", "palabra alineada, vista I", loads(lanes.lw, aligned)),
        ("lw_paged", "palabra alineada, PagedMemory (camino actual)", loads(lanes.lw_paged, aligned)),
        ("lw_unaligned", "palabra en cualquier dirección, int.from_bytes", loads(lanes.lw_unaligned, anywhere)),
        ("lb", "byte con signo, vista B", loads(lanes.lb, anywhere)),
        ("lbu", "byte sin signo, vista B", loads(lanes.lbu, anywhere)),
        ("lb_shift", "byte con signo, desplazamiento", loads(lanes.lb_shift, anywhere)),
        ("lbu_shift", "byte sin signo, desplazamiento", loads(lanes.lbu_shift, anywhere)),
        ("lh", "media palabra con signo, vista B", loads(lanes.lh, anywhere)),
        ("lhu", "media palabra sin signo, vista B", loads(lanes.lhu, anywhere)),
        ("sw", "palabra alineada, vista I", stores(lanes.sw, aligned_pairs)),
        ("sw_paged", "palabra alineada, PagedMemory (camino actual)", stores(lanes.sw_paged, aligned_pairs)),
        ("sb", "byte, vista B", stores(lanes.sb, anywhere_pairs)),
        ("sb_shift", "byte, lectura-modificación-escritura", stores(lanes.sb_shift, anywhere_pairs)),
        ("sh", "media palabra, vista B", stores(lanes.sh, anywhere_pairs)),
        ("sw_ring", "palabra con anillo de escrituras", stores(lanes.sw_ring, aligned_pairs)),
        ("sb_ring", "byte con anillo de escrituras", stores(lanes.sb_ring, anywhere_pairs)),
        ("lw_repetido", "lw otra vez, para ver la deriva de la tanda", loads(lanes.lw, aligned)),
    ]
    rows = []
    for name, description, body in cases:
        best, median = measure(body, rounds)
        rows.append({
            "op": name,
            "descripcion": description,
            "ns_min": best / count * 1e9,
            "ns_mediana": median / count * 1e9,
        })
    return rows


def flat_rows(flat: FlatMemory, lanes: LaneMemory, region: int, size: int, rounds: int,
              count: int) -> list[dict[str, object]]:
    """Las mismas operaciones sobre la RAM contigua y sobre las vistas por página, en ns por acceso."""
    rng = random.Random(7)
    aligned = [region + rng.randrange(0, size, 4) for _ in range(count)]
    anywhere = [region + rng.randrange(size - 4) for _ in range(count)]
    values = [rng.getrandbits(32) for _ in range(count)]
    aligned_pairs = list(zip(aligned, values))
    anywhere_pairs = list(zip(anywhere, values))
    for addr in range(region, region + size, 4):
        word = lanes.lw(addr)
        flat.sw(addr, word)

    def loads(fn: Callable[[int], object], addrs: list[int]) -> Callable[[], None]:
        def body() -> None:
            for addr in addrs:
                fn(addr)
        return body

    def stores(fn: Callable[[int, int], None], pairs: list[tuple[int, int]]) -> Callable[[], None]:
        def body() -> None:
            for addr, value in pairs:
                fn(addr, value)
        return body

    cases = [
        ("vacío_carga", loads, aligned), ("lw", loads, aligned), ("lb", loads, anywhere), ("lbu", loads, anywhere),
        ("lh", loads, anywhere), ("lhu", loads, anywhere), ("lw_unaligned", loads, anywhere),
        ("vacío_escritura", stores, aligned_pairs), ("sw", stores, aligned_pairs), ("sb", stores, anywhere_pairs),
        ("sh", stores, anywhere_pairs),
    ]
    rows = []
    for name, maker, addrs in cases:
        method = {"vacío_carga": "nothing_load", "vacío_escritura": "nothing_store"}.get(name, name)
        flat_best, _ = measure(maker(getattr(flat, method), addrs), rounds)
        paged_best, _ = measure(maker(getattr(lanes, method), addrs), rounds)
        rows.append({"op": name, "ns_plana": flat_best / count * 1e9, "ns_paginas": paged_best / count * 1e9})
    by_op = {str(row["op"]): row for row in rows}
    for row in rows:
        floor_key = "vacío_escritura" if str(row["op"]) in ("sw", "sb", "sh", "vacío_escritura") else "vacío_carga"
        fl, fp = float(by_op[floor_key]["ns_plana"]), float(by_op[floor_key]["ns_paginas"])
        row["plana/paginas"] = float(row["ns_plana"]) / float(row["ns_paginas"])
        denominator = float(row["ns_paginas"]) - fp
        row["plana/paginas neto"] = (float(row["ns_plana"]) - fl) / denominator if denominator > 0 else math.inf
    return rows


def program_rows(lanes: LaneMemory, region: int, size: int, rounds: int) -> list[dict[str, object]]:
    half = size // 2
    src, dst = region, region + half
    rng = random.Random(11)
    for addr in range(src, src + half, 4):
        lanes.sw(addr, rng.getrandbits(32) | 0x01010101)  # sin bytes nulos: strcpy copia todo
    lanes.sb(src + half - 1, 0)
    cases: list[tuple[str, str, list[Instruction], int]] = [
        ("memcpy_w", "sin anillo", program_memcpy(4, op_lw, op_sw, op_addi, op_bnez), half),
        ("memcpy_w", "anillo: escrituras", program_memcpy(4, op_lw, op_sw_ring, op_addi, op_bnez), half),
        ("memcpy_w", "anillo: instrucciones", program_memcpy(4, op_lw_undo, op_sw_undo, op_addi_undo, op_bnez_undo), half),
        ("memcpy_b", "sin anillo", program_memcpy(1, op_lbu, op_sb, op_addi, op_bnez), half),
        ("memcpy_b", "anillo: escrituras", program_memcpy(1, op_lbu, op_sb_ring, op_addi, op_bnez), half),
        ("memcpy_b", "anillo: instrucciones", program_memcpy(1, op_lbu_undo, op_sb_undo, op_addi_undo, op_bnez_undo), half),
        ("strcpy", "sin anillo", program_strcpy(op_lbu, op_sb, op_addi, op_bnez), half),
        ("strcpy", "anillo: escrituras", program_strcpy(op_lbu, op_sb_ring, op_addi, op_bnez), half),
        ("strcpy", "anillo: instrucciones", program_strcpy(op_lbu_undo, op_sb_undo, op_addi_undo, op_bnez_undo), half),
        ("memcpy_w", "sin anillo (repetido)", program_memcpy(4, op_lw, op_sw, op_addi, op_bnez), half),
    ]
    expected = bytes(lanes.memory.view_bytes((src - BASE) >> 2, half >> 2))

    def runner(program: list[Instruction], length: int) -> Callable[[], int]:
        def body() -> int:
            regs = [0] * 32
            regs[A0], regs[A1], regs[A2] = dst, src, length
            lanes.ring.clear()
            return run(program, regs, lanes)
        return body

    rows = []
    for name, variant, program, length in cases:
        body = runner(program, length)
        executed = body()  # calentamiento y recuento de instrucciones
        best, median = measure(body, rounds)
        copied = bytes(lanes.memory.view_bytes((dst - BASE) >> 2, half >> 2))
        if copied != expected:
            raise AssertionError(f"{name} ({variant}) no copió la región entera")
        for addr in range(dst, dst + half, 4):
            lanes.sw(addr, 0)
        rows.append({
            "programa": name,
            "variante": variant,
            "instrucciones": executed,
            "ns_instr_min": best / executed * 1e9,
            "ns_instr_mediana": median / executed * 1e9,
            "s_min": best,
        })
    return rows


def verdicts(access: list[dict[str, object]], programs: list[dict[str, object]]) -> dict[str, object]:
    by_op = {str(row["op"]): float(row["ns_min"]) for row in access}
    floors = {"l": by_op["vacío_carga"], "s": by_op["vacío_escritura"]}

    def ratio(byte: str, word: str, net: bool) -> float:
        if not net:
            return by_op[byte] / by_op[word]
        floor = floors[byte[0]]
        denominator = by_op[word] - floor
        return (by_op[byte] - floor) / denominator if denominator > 0 else math.inf

    lanes: dict[str, float] = {}
    for byte, word in (("lb", "lw"), ("lbu", "lw"), ("sb", "sw"), ("lb_shift", "lw"), ("lbu_shift", "lw"),
                       ("sb_shift", "sw"), ("lh", "lw"), ("lhu", "lw"), ("sh", "sw"), ("lw_unaligned", "lw")):
        lanes[f"{byte}/{word}"] = ratio(byte, word, False)
        lanes[f"{byte}/{word} neto"] = ratio(byte, word, True)
    lanes["lb/lw_paged"] = ratio("lb", "lw_paged", False)
    lanes["sb/sw_paged"] = ratio("sb", "sw_paged", False)
    lanes["lw/lw_paged"] = ratio("lw", "lw_paged", False)
    lanes["lw_repetido/lw"] = ratio("lw_repetido", "lw", False)
    worst_view = max(lanes["lb/lw neto"], lanes["lbu/lw neto"], lanes["sb/sw neto"])
    worst_shift = max(lanes["lb_shift/lw neto"], lanes["lbu_shift/lw neto"], lanes["sb_shift/sw neto"])
    worst_half = max(lanes["lh/lw neto"], lanes["lhu/lw neto"], lanes["sh/sw neto"])

    base = {(str(row["programa"]), str(row["variante"])): float(row["ns_instr_min"]) for row in programs}
    seconds = {(str(row["programa"]), str(row["variante"])): float(row["s_min"]) for row in programs}
    ring: dict[str, float] = {}
    for name in ("memcpy_w", "memcpy_b", "strcpy"):
        plain = base[(name, "sin anillo")]
        ring[f"{name}: escrituras"] = base[(name, "anillo: escrituras")] / plain - 1
        ring[f"{name}: instrucciones"] = base[(name, "anillo: instrucciones")] / plain - 1
    worst_ring_stores = max(value for key, value in ring.items() if key.endswith("escrituras"))
    worst_ring_undo = max(value for key, value in ring.items() if key.endswith("instrucciones"))
    return {
        "carriles": lanes,
        "peor_vista_neto": worst_view,
        "peor_desplazamiento_neto": worst_shift,
        "peor_media_palabra_neto": worst_half,
        "p1_1_vista_sobrevive": worst_view <= LANE_THRESHOLD,
        "p1_1_desplazamiento_sobrevive": worst_shift <= LANE_THRESHOLD,
        "vista_en_el_umbral": abs(worst_view - LANE_THRESHOLD) < LANE_MARGIN,
        "desplazamiento_en_el_umbral": abs(worst_shift - LANE_THRESHOLD) < LANE_MARGIN,
        "anillo": ring,
        "peor_anillo_escrituras": worst_ring_stores,
        "peor_anillo_instrucciones": worst_ring_undo,
        "anillo_escrituras_se_conserva": worst_ring_stores <= RING_THRESHOLD,
        "anillo_deshacer_se_conserva": worst_ring_undo <= RING_THRESHOLD,
        "anillo_deshacer_en_el_umbral": abs(worst_ring_undo - RING_THRESHOLD) < RING_MARGIN,
        "deriva_programas": base[("memcpy_w", "sin anillo (repetido)")] / base[("memcpy_w", "sin anillo")] - 1,
        "memcpy_b_vs_w_por_instruccion": base[("memcpy_b", "sin anillo")] / base[("memcpy_w", "sin anillo")],
        "memcpy_b_vs_w_tiempo_total": seconds[("memcpy_b", "sin anillo")] / seconds[("memcpy_w", "sin anillo")],
    }


def _label(value: float) -> str:
    return "sin medida fiable (denominador no positivo)" if math.isinf(value) else f"{value:.2f}×"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(description="E2 de RFC-EXP-00018: carriles de bytes y anillo de deshacer")
    parser.add_argument("--rondas", type=int, default=7)
    parser.add_argument("--accesos", type=int, default=200_000, help="accesos por ronda en el microbanco")
    parser.add_argument("--verificar", action="store_true", help="solo contrastar con el modelo bytearray")
    parser.add_argument("--plana", action="store_true", help="medir también la RAM contigua (mmap) frente a las vistas")
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    if args.rondas < 1 or args.accesos < 1:
        parser.error("rondas y accesos deben ser positivos")

    lanes = LaneMemory(PagedMemory(MEMORY_WORDS))
    region = BASE + (1 << 16)
    lanes.materialize(region, REGION_BYTES)
    checks = verify(lanes, region, REGION_BYTES)
    print(f"Verificación contra bytearray: {checks:,} comprobaciones OK")
    flat = FlatMemory(MEMORY_WORDS * 4)
    flat_checks = verify_flat(flat)
    print(f"Verificación de la RAM contigua: {flat_checks:,} comprobaciones OK")
    if args.verificar:
        flat.close()
        return 0
    rng = random.Random(3)
    for addr in range(region, region + REGION_BYTES, 4):
        lanes.sw(addr, rng.getrandbits(32))  # las cargas leen datos aleatorios, no ceros

    print(f"Python {platform.python_version()} ({platform.system()} {platform.machine()}); rondas: {args.rondas}")
    access = access_rows(lanes, region, REGION_BYTES, args.rondas, args.accesos)
    print(f"\nCoste por acceso ({args.accesos:,} accesos por ronda; ns, mínimo / mediana):")
    for row in access:
        print(f"  {row['op']:<16} {row['ns_min']:7.1f} / {row['ns_mediana']:7.1f}   {row['descripcion']}")
    programs = program_rows(lanes, region, REGION_BYTES, args.rondas)
    print("\nIntérprete mínimo (ns por instrucción, mínimo / mediana):")
    for row in programs:
        print(f"  {row['programa']:<9} {row['variante']:<22} {row['ns_instr_min']:7.1f} / "
              f"{row['ns_instr_mediana']:7.1f}   ({row['instrucciones']:,} instrucciones)")
    result = verdicts(access, programs)
    flat_table: list[dict[str, object]] = []
    if args.plana:
        flat_table = flat_rows(flat, lanes, region, REGION_BYTES, args.rondas, args.accesos)
        print("\nRAM contigua (mmap) frente a vistas por página (ns por acceso, mínimo; cociente bruto y neto):")
        for row in flat_table:
            print(f"  {row['op']:<16} {row['ns_plana']:7.1f} {row['ns_paginas']:8.1f}   "
                  f"{_label(float(row['plana/paginas']))} / {_label(float(row['plana/paginas neto']))}")
    flat.close()
    print("\nCocientes frente a la palabra alineada por vista I, el camino que tendrá el hart "
          "(bruto; «neto»: descontado el bucle con un método vacío):")
    for key, value in result["carriles"].items():
        print(f"  {key:<22} {_label(value):>8}")
    print("\nSobrecoste del anillo por instrucción (cota superior: el intérprete mínimo no decodifica ni atrapa):")
    for key, value in result["anillo"].items():
        print(f"  {key:<26} {value * 100:+6.1f} %")
    print(f"  deriva del caso base repetido: {result['deriva_programas'] * 100:+.1f} %")
    print(f"\nmemcpy por bytes frente a por palabras: {result['memcpy_b_vs_w_por_instruccion']:.2f}× por instrucción, "
          f"{result['memcpy_b_vs_w_tiempo_total']:.2f}× en tiempo total (ejecuta 4× más instrucciones)")
    near_lane = f" (en el umbral: a menos de {LANE_MARGIN:.2f} con ruido de ±0,05)"
    print(f"P1.1 tal como lo describe el RFC (leer-modificar-escribir, máscaras precalculadas): peor carril neto "
          f"{_label(result['peor_desplazamiento_neto'])} → "
          f"{'sobrevive' if result['p1_1_desplazamiento_sobrevive'] else 'MUERE'} (umbral {LANE_THRESHOLD}×)"
          f"{near_lane if result['desplazamiento_en_el_umbral'] else ''}")
    print(f"P1.1 con vistas de bytes por página: peor carril neto {_label(result['peor_vista_neto'])} → "
          f"{'sobrevive' if result['p1_1_vista_sobrevive'] else 'MUERE'}"
          f"{near_lane if result['vista_en_el_umbral'] else ''}; medias palabras "
          f"{_label(result['peor_media_palabra_neto'])}; lw desalineada "
          f"{_label(result['carriles']['lw_unaligned/lw neto'])} (informativo: el RFC la deja a la implementación)")
    print(f"Anillo que deshace instrucciones: {result['peor_anillo_instrucciones'] * 100:+.1f} % → "
          f"{'se conserva' if result['anillo_deshacer_se_conserva'] else 'SE RETIRA'} (umbral {RING_THRESHOLD:.0%})"
          f"{f' (en el umbral: a menos de {RING_MARGIN:.0%} con ruido de ±3 puntos)' if result['anillo_deshacer_en_el_umbral'] else ''}; "
          f"anillo solo de escrituras: {result['peor_anillo_escrituras'] * 100:+.1f} % → "
          f"{'se conserva' if result['anillo_escrituras_se_conserva'] else 'se retira'}")
    if args.json:
        args.json.write_text(json.dumps({
            "python": platform.python_version(),
            "plataforma": f"{platform.system()} {platform.machine()}",
            "rondas": args.rondas,
            "accesos": args.accesos,
            "accesos_filas": access,
            "programas": programs,
            "veredictos": result,
            "plana": flat_table,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
