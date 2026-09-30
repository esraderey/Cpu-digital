"""Memoria paginada de palabras int32 para Tramoya VM32."""

from __future__ import annotations

from array import array
from collections.abc import Iterable, Sequence


PAGE_WORDS = 4_096
INT32_MIN = -(1 << 31)
INT32_MAX = (1 << 31) - 1


def _int32(value: int) -> int:
    unsigned = int(value) & 0xFFFFFFFF
    return unsigned - 0x100000000 if unsigned >= 0x80000000 else unsigned


class PagedMemory:
    """Espacio lógico grande que solo materializa páginas escritas.

    Las páginas usan ``array('i')``: cuatro bytes físicos por palabra en las
    plataformas soportadas. Las lecturas de páginas no asignadas devuelven cero.
    """

    __slots__ = ("_size", "_page_words", "_pages")

    def __init__(self, size: int, page_words: int = PAGE_WORDS):
        if size < 1 or page_words < 1:
            raise ValueError("El tamaño de memoria y página debe ser positivo")
        if array("i").itemsize != 4:
            raise RuntimeError("La plataforma no proporciona arrays int32 nativos")
        self._size = int(size)
        self._page_words = int(page_words)
        self._pages: dict[int, array[int]] = {}

    def __len__(self) -> int:
        return self._size

    @property
    def page_words(self) -> int:
        return self._page_words

    @property
    def allocated_pages(self) -> int:
        return len(self._pages)

    @property
    def allocated_words(self) -> int:
        return sum(len(page) for page in self._pages.values())

    @property
    def allocated_bytes(self) -> int:
        return self.allocated_words * 4

    def _check_address(self, address: int) -> int:
        if not isinstance(address, int) or isinstance(address, bool) or not 0 <= address < self._size:
            raise IndexError(f"Dirección fuera de memoria: {address!r}")
        return address

    def _new_page(self, page_index: int) -> array[int]:
        remaining = self._size - page_index * self._page_words
        return array("i", [0]) * min(self._page_words, remaining)

    def __getitem__(self, key: int | slice) -> int | list[int]:
        if isinstance(key, slice):
            if key.step not in (None, 1):
                raise ValueError("La memoria solo admite slices contiguos")
            start = 0 if key.start is None else key.start
            stop = self._size if key.stop is None else key.stop
            return list(self.read_block(start, stop - start))
        address = self._check_address(key)
        page_index, offset = divmod(address, self._page_words)
        page = self._pages.get(page_index)
        return 0 if page is None else int(page[offset])

    def __setitem__(self, key: int | slice, value: int | Sequence[int]) -> None:
        if isinstance(key, slice):
            if key.step not in (None, 1):
                raise ValueError("La memoria solo admite slices contiguos")
            start = 0 if key.start is None else key.start
            stop = self._size if key.stop is None else key.stop
            values = list(value) if not isinstance(value, int) else [value]
            if stop - start != len(values):
                raise ValueError("La asignación debe conservar el tamaño del slice")
            self.write_block(start, values)
            return
        address = self._check_address(key)
        normalized = _int32(int(value))
        page_index, offset = divmod(address, self._page_words)
        page = self._pages.get(page_index)
        if page is None:
            if normalized == 0:
                return
            page = self._new_page(page_index)
            self._pages[page_index] = page
        page[offset] = normalized

    def read_block(self, start: int, count: int) -> tuple[int, ...]:
        if not isinstance(start, int) or not isinstance(count, int) or start < 0 or count < 0 or start + count > self._size:
            raise ValueError("Rango de memoria inválido")
        result: list[int] = []
        address = start
        remaining = count
        while remaining:
            page_index, offset = divmod(address, self._page_words)
            chunk_size = min(remaining, self._page_words - offset)
            page = self._pages.get(page_index)
            if page is None:
                result.extend([0] * chunk_size)
            else:
                result.extend(page[offset:offset + chunk_size])
            address += chunk_size
            remaining -= chunk_size
        return tuple(result)

    def write_block(self, start: int, values: Iterable[int]) -> None:
        normalized = [_int32(value) for value in values]
        if start < 0 or start + len(normalized) > self._size:
            raise ValueError("Rango de memoria inválido")
        address = start
        cursor = 0
        while cursor < len(normalized):
            page_index, offset = divmod(address, self._page_words)
            chunk_size = min(len(normalized) - cursor, self._page_words - offset)
            chunk = normalized[cursor:cursor + chunk_size]
            page = self._pages.get(page_index)
            if page is None and any(chunk):
                page = self._new_page(page_index)
                self._pages[page_index] = page
            if page is not None:
                page[offset:offset + chunk_size] = array("i", chunk)
            address += chunk_size
            cursor += chunk_size

    def view_bytes(self, start: int, count: int) -> memoryview:
        """Bytes nativos de ``count`` palabras (formato ``'B'``).

        Sin copia si el rango cae en una sola página asignada; si cruza páginas
        o toca páginas no asignadas, devuelve una copia contigua. La vista sin
        copia refleja escrituras posteriores: el llamador debe consumirla antes
        de escribir.
        """
        if not isinstance(start, int) or not isinstance(count, int) or start < 0 or count < 0 or start + count > self._size:
            raise ValueError("Rango de memoria inválido")
        page_index, offset = divmod(start, self._page_words)
        if offset + count <= self._page_words:
            page = self._pages.get(page_index)
            if page is None:
                return memoryview(bytes(4 * count))
            return memoryview(page).cast("B")[4 * offset:4 * (offset + count)]
        result = bytearray(4 * count)
        address = start
        cursor = 0
        while cursor < count:
            page_index, offset = divmod(address, self._page_words)
            chunk_size = min(count - cursor, self._page_words - offset)
            page = self._pages.get(page_index)
            if page is not None:
                result[4 * cursor:4 * (cursor + chunk_size)] = memoryview(page).cast("B")[
                    4 * offset:4 * (offset + chunk_size)
                ]
            address += chunk_size
            cursor += chunk_size
        return memoryview(result)

    def write_bytes(self, start: int, data: bytes | bytearray | memoryview) -> None:
        """Escribe palabras dadas como bytes nativos; no materializa páginas con ceros."""
        view = memoryview(data).cast("B")
        if len(view) % 4:
            raise ValueError("Los datos deben ocupar palabras completas")
        count = len(view) // 4
        if not isinstance(start, int) or start < 0 or start + count > self._size:
            raise ValueError("Rango de memoria inválido")
        address = start
        cursor = 0
        while cursor < count:
            page_index, offset = divmod(address, self._page_words)
            chunk_size = min(count - cursor, self._page_words - offset)
            chunk = view[4 * cursor:4 * (cursor + chunk_size)]
            page = self._pages.get(page_index)
            if page is None and chunk.tobytes().count(0) != len(chunk):
                page = self._new_page(page_index)
                self._pages[page_index] = page
            if page is not None:
                memoryview(page).cast("B")[4 * offset:4 * (offset + chunk_size)] = chunk
            address += chunk_size
            cursor += chunk_size

    def snapshot_pages(self) -> list[list[object]]:
        """Devuelve páginas no vacías, recortadas por la derecha."""
        encoded: list[list[object]] = []
        for page_index, page in sorted(self._pages.items()):
            values = page.tolist()
            while values and values[-1] == 0:
                values.pop()
            if values:
                encoded.append([page_index, values])
        return encoded

    @classmethod
    def from_snapshot(
        cls,
        size: int,
        pages: object,
        *,
        page_words: int = PAGE_WORDS,
    ) -> "PagedMemory":
        if not isinstance(pages, list):
            raise ValueError("Páginas de memoria inválidas")
        memory = cls(size, page_words=page_words)
        seen: set[int] = set()
        maximum_page = (size - 1) // page_words
        for record in pages:
            if not isinstance(record, list) or len(record) != 2:
                raise ValueError("Registro de página inválido")
            page_index, values = record
            if type(page_index) is not int or not 0 <= page_index <= maximum_page or page_index in seen:
                raise ValueError("Índice de página inválido o duplicado")
            if not isinstance(values, list) or not 1 <= len(values) <= page_words:
                raise ValueError("Contenido de página inválido")
            start = page_index * page_words
            if start + len(values) > size:
                raise ValueError("Página fuera del espacio de memoria")
            if not all(type(value) is int and INT32_MIN <= value <= INT32_MAX for value in values):
                raise ValueError("Valor de memoria fuera de int32")
            memory.write_block(start, values)
            seen.add(page_index)
        return memory
