"""RAM contigua del hart RV32 de RFC-EXP-00018 (P1.1 enmendado el 2026-10-10; P1.6 refinado el 2026-10-11).

Un ``mmap`` anónimo que el sistema operativo materializa por páginas de 4 KiB al tocarlas,
con tres vistas ``memoryview`` sobre el mismo búfer (``lanes``: bytes; ``words_u``: palabras
sin signo; ``words_i``: palabras con signo) y tres estados por encima del búfer:

- ``written`` (un byte por página de 4 KiB): 1 si la página recibió alguna escritura desde la
  creación o el último ``reset``. Es lo que el snapshot necesita: guarda solo las páginas escritas
  en el formato paginado v2 de VM32 (páginas de 4 096 palabras, listas de enteros con signo
  recortadas por la derecha, vacías omitidas), lo que reproduce el contenido byte a byte mientras
  toda escritura pase por esta clase o por sus vistas marcando la página, porque los ``mmap``
  nuevos son cero (invariante: byte ≠ 0 ⇒ página escrita). Restaurar marca las páginas cubiertas
  por los datos restaurados, no el conjunto original.
- ``code`` (un byte por línea de ``LINE_BYTES`` = 16 bytes): 1 si alguna palabra de la línea entró
  en una caché derivada del hart (decodificación o bloque compilado) desde el último
  ``clear_code``. Lo marcan el hart al decodificar (por su espejo del mapa) y el compilador de
  bloques (``mark_code``) antes de usar la palabra cacheada; ``clear_code`` desmarca y sube la generación de las páginas que
  tenían marcas, de modo que ninguna caché construida antes (de este hart o de otro que comparta
  la memoria) sobrevive al vaciado.
- ``generations`` (un entero de 64 bits por página, monótono: nunca baja): lo que las cachés
  comparan para saber si siguen válidas (P1.6). Sube con los stores del invitado que tocan una
  línea marcada en ``code`` (todas las páginas que el store toca), con toda escritura del host
  (``load_bytes``, ``fill``, ``restore_pages``) y con ``reset`` en las páginas que borra. Un store
  a una línea sin código no la sube: no puede dejar obsoleta ninguna caché y solo marca
  ``written``. Antes del refinamiento todo store subía la generación, y los datos que compartían
  página con el código invalidaban sus bloques en cada escritura (Embench empaquetado: 2,66× →
  3,38× el intérprete).

Los tres son estado derivado del contenido y de las escrituras, no estado arquitectónico: no
entran en el snapshot (``code`` lo vacía el hart junto con sus cachés). El inventario de P2.5 lo
declara en los atributos de clase ``STATE_FIELDS``, ``DERIVED_FIELDS``, ``CONFIG_FIELDS``,
``STATS_FIELDS`` y ``HARNESS_FIELDS`` (H2.1): el contenido (el búfer y sus vistas) entra en el snapshot
del hart como páginas v2; las generaciones nunca se restauran (suben), ``written`` se recalcula y ``code``
se vacía.

Semántica (RISC-V ISA Manual no privilegiado v20260120, §2.6): memoria direccionada por bytes,
little-endian; las cargas de 8 y 16 bits extienden con signo (``load8s``, ``load16s``) o con
ceros (``load8u``, ``load16u``); las escrituras guardan los bits bajos de ``value``. Los accesos
desalineados se resuelven aquí, sin trap (decisión de P1.1). Las direcciones fuera de la RAM
lanzan :class:`AccessFault` sin tocar nada: el bus de la plataforma decide si son MMIO o un
fallo de acceso (causas 5 y 7 de ``mcause``).

El hart y los bloques compilados usan los atributos públicos (``base``, ``limit``, ``lanes``,
``words_u``, ``words_i``, ``generations``, ``written``, ``code``) directamente en su camino
caliente; su contrato es el de los métodos de esta clase y se comprueba con fuzzing diferencial.
Quien escribe por las vistas debe poner ``written[página] = 1`` en cada página que toque (el
snapshot solo mira esas páginas, y una marca olvidada perdería esos bytes en silencio) e
incrementar ``generations[página]`` si alguna línea tocada está marcada en ``code`` (si no, una
caché ejecutaría una palabra obsoleta).

Dominios: las direcciones llegan ya enmascaradas a [0, 2³²); ``load32``, ``load16u`` y ``load8u``
devuelven enteros en [0, 2^ancho); ``load16s`` y ``load8s`` devuelven el valor con signo como
entero Python (negativo sin enmascarar: el hart lo convierte a 32 bits). Las escrituras aceptan
cualquier entero y guardan sus bits bajos; un valor que no sea entero lanza ``TypeError`` antes
de tocar la generación.
"""

from __future__ import annotations

import mmap
import sys
from array import array
from collections.abc import Iterable

PAGE_SHIFT = 12
PAGE_BYTES = 1 << PAGE_SHIFT
# Líneas del mapa de código (P1.6 refinado): 16 bytes. Deben dividir a la página (una línea nunca cruza
# página) y medir al menos una palabra (un «sw» alineado nunca cruza línea; un «sh» cruza línea si y
# solo si offset & LINE_MASK == LINE_MASK). El hart y los bloques importan LINE_SHIFT: es la única
# constante que fija el tamaño.
LINE_SHIFT = 4
LINE_BYTES = 1 << LINE_SHIFT
LINE_MASK = LINE_BYTES - 1
if PAGE_BYTES % LINE_BYTES or LINE_BYTES < 4:
    raise RuntimeError("LINE_BYTES debe dividir a PAGE_BYTES y medir al menos una palabra")
SNAPSHOT_PAGE_WORDS = 4_096           # formato v2 de VM32: 16 KiB por página de snapshot
DEFAULT_BASE = 0x80000000
MASK32 = 0xFFFFFFFF
INT32_MIN = -(1 << 31)
INT32_MAX = (1 << 31) - 1


class AccessFault(Exception):
    """Acceso fuera de la RAM; ``address`` es la dirección del invitado que lo causó."""

    def __init__(self, address: int) -> None:
        super().__init__(address)
        self.address = address


class Rv32Memory:
    __slots__ = ("base", "size", "limit", "buffer", "lanes", "words_u", "words_i", "generations", "written", "code",
                 "_closed")
    # Inventario de estado (P2.5): cada slot está en exactamente una de estas listas; tests/test_rv32_snapshot.py lo
    # comprueba por introspección y fija la clasificación (un slot nuevo sin clasificar pone la suite en rojo).
    STATE_FIELDS = ("buffer", "lanes", "words_u", "words_i")   # el contenido: snapshot()["ram"]["pages"] (formato v2)
    DERIVED_FIELDS = {
        "generations": "monótonas y nunca restauradas: restore_pages (reset + escritura del host) y clear_code las suben, "
                       "así que ninguna caché construida antes de restaurar puede coincidir",
        "written": "se recalcula al restaurar: exactamente las páginas cubiertas por los datos restaurados (y ninguna tras reset)",
        "code": "lo vacía el hart con clear_code junto con sus cachés en restore, reset y attach_memory",
    }
    CONFIG_FIELDS = ("base", "size", "limit", "_closed")   # geometría y ciclo de vida: los decide el host
    STATS_FIELDS = ()
    HARNESS_FIELDS = ()

    def __init__(self, size: int, base: int = DEFAULT_BASE) -> None:
        if sys.byteorder != "little":
            raise RuntimeError("Los carriles nativos exigen un host little-endian")
        if array("I").itemsize != 4:
            raise RuntimeError("La plataforma no proporciona enteros de 4 bytes")
        if size <= 0 or size & (PAGE_BYTES - 1):
            raise ValueError("El tamaño de la RAM debe ser un múltiplo positivo de 4 KiB")
        if base & (PAGE_BYTES - 1) or base < 0 or base + size > (1 << 32):
            raise ValueError("La base debe estar alineada a 4 KiB y la RAM caber en 32 bits")
        self.base = base
        self.size = size
        self.limit = base + size
        self.buffer = mmap.mmap(-1, size)
        self.lanes = memoryview(self.buffer)
        self.words_u = self.lanes.cast("I")
        self.words_i = self.lanes.cast("i")
        # Generaciones de 64 bits: nunca desbordan. «Escrita» vive aparte, en ``written``.
        self.generations = array("Q", bytes(8 * (size >> PAGE_SHIFT)))
        self.written = bytearray(size >> PAGE_SHIFT)
        self.code = bytearray(size >> LINE_SHIFT)
        self._closed = False

    # ── Mapa de código y marcado de escrituras ──

    def mark_code(self, offset: int, length: int) -> None:
        """Marca como código las líneas de ``[offset, offset + length)`` (desplazamientos en la RAM, no
        necesariamente alineados). Lo llama el compilador de bloques antes de usar las palabras cacheadas
        (el hart marca al decodificar por su espejo del mapa). Con ``length <= 0`` no hace nada, ni siquiera
        comprobar el rango; con ``length > 0``, ``IndexError`` si el rango se sale de la RAM."""
        if length <= 0:
            return
        if offset < 0 or offset + length > self.size:
            raise IndexError(offset)
        code = self.code
        for line in range(offset >> LINE_SHIFT, ((offset + length - 1) >> LINE_SHIFT) + 1):
            code[line] = 1

    def clear_code(self) -> None:
        """Desmarca todas las líneas y sube la generación de cada página que tenía alguna marca: las cachés
        construidas sobre esas páginas (las de este hart, que ``Rv32Hart.attach_memory`` y ``Rv32Hart.reset``
        vacían a la vez, o las de cualquier otro hart que comparta la memoria) dejan de coincidir, así que
        vaciar el mapa nunca puede resucitar una palabra obsoleta. Coste: un recorrido del mapa (``find``, en C;
        0,23 ms para los 4 MiB de una RAM de 64 MiB) más una puesta a cero por página con código; no reasigna el
        mapa. Para los demás harts que compartan la memoria cada vaciado cuenta como una invalidación de sus
        bloques en esas páginas (y consume su tope de recompilaciones): compartir una memoria entre harts es
        correcto, pero no gratis."""
        code = self.code
        generations = self.generations
        lines_per_page = PAGE_BYTES >> LINE_SHIFT
        position = code.find(1)
        while position >= 0:
            page = position // lines_per_page
            generations[page] += 1
            start = page * lines_per_page
            code[start:start + lines_per_page] = bytes(lines_per_page)
            position = code.find(1, start + lines_per_page)

    def _touch(self, offset: int, width: int) -> None:
        """Escritura del invitado sobre ``[offset, offset + width)`` (``width`` ≤ 4 ≤ ``LINE_BYTES``: una o dos
        líneas, una o dos páginas): marca las páginas como escritas y sube la generación de todas ellas si
        alguna de las líneas tocadas está marcada como código."""
        first_page = offset >> PAGE_SHIFT
        last_page = (offset + width - 1) >> PAGE_SHIFT
        written = self.written
        written[first_page] = 1
        if last_page != first_page:
            written[last_page] = 1
        code = self.code
        first_line = offset >> LINE_SHIFT
        last_line = (offset + width - 1) >> LINE_SHIFT
        if code[first_line] or (last_line != first_line and code[last_line]):
            generations = self.generations
            generations[first_page] += 1
            if last_page != first_page:
                generations[last_page] += 1

    def _host_write(self, offset: int, length: int) -> None:
        """Escritura del host sobre ``[offset, offset + length)`` (``length`` > 0): marca las páginas como
        escritas y sube siempre su generación, porque puede ser código nuevo."""
        generations, written = self.generations, self.written
        for page in range(offset >> PAGE_SHIFT, ((offset + length - 1) >> PAGE_SHIFT) + 1):
            generations[page] += 1
            written[page] = 1

    def close(self) -> None:
        """Libera las vistas y cierra el ``mmap``; idempotente. Si queda una porción de ``lanes`` viva en
        otra parte, ``BufferError``: las vistas ya están liberadas y el ``mmap`` sigue abierto; cuando
        se suelte esa porción, un nuevo ``close()`` lo cierra."""
        if self._closed:
            return
        self.words_i.release()
        self.words_u.release()
        self.lanes.release()
        self.buffer.close()
        self._closed = True

    # ── Cargas ──

    def load32(self, address: int) -> int:
        offset = address - self.base
        if not 0 <= offset <= self.size - 4:
            raise AccessFault(address)
        if offset & 3:
            return int.from_bytes(self.lanes[offset:offset + 4], "little")
        return self.words_u[offset >> 2]

    def load16u(self, address: int) -> int:
        offset = address - self.base
        if not 0 <= offset <= self.size - 2:
            raise AccessFault(address)
        return self.lanes[offset] | (self.lanes[offset + 1] << 8)

    def load16s(self, address: int) -> int:
        offset = address - self.base
        if not 0 <= offset <= self.size - 2:
            raise AccessFault(address)
        half = self.lanes[offset] | (self.lanes[offset + 1] << 8)
        return half - 0x10000 if half > 0x7FFF else half

    def load8u(self, address: int) -> int:
        offset = address - self.base
        if not 0 <= offset < self.size:
            raise AccessFault(address)
        return self.lanes[offset]

    def load8s(self, address: int) -> int:
        offset = address - self.base
        if not 0 <= offset < self.size:
            raise AccessFault(address)
        byte = self.lanes[offset]
        return byte - 0x100 if byte > 0x7F else byte

    # ── Escrituras ──

    def store32(self, address: int, value: int) -> None:
        offset = address - self.base
        if not 0 <= offset <= self.size - 4:
            raise AccessFault(address)
        value &= MASK32   # un valor que no sea entero falla aquí, antes de tocar las marcas
        if offset & 3:
            self._touch(offset, 4)   # desalineada: puede cruzar línea y página
            self.lanes[offset:offset + 4] = value.to_bytes(4, "little")
            return
        self.written[offset >> PAGE_SHIFT] = 1
        if self.code[offset >> LINE_SHIFT]:
            self.generations[offset >> PAGE_SHIFT] += 1
        self.words_u[offset >> 2] = value

    def store16(self, address: int, value: int) -> None:
        offset = address - self.base
        if not 0 <= offset <= self.size - 2:
            raise AccessFault(address)
        low = value & 0xFF
        high = (value >> 8) & 0xFF
        self._touch(offset, 2)
        lanes = self.lanes
        lanes[offset] = low
        lanes[offset + 1] = high

    def store8(self, address: int, value: int) -> None:
        offset = address - self.base
        if not 0 <= offset < self.size:
            raise AccessFault(address)
        value &= 0xFF
        self.written[offset >> PAGE_SHIFT] = 1
        if self.code[offset >> LINE_SHIFT]:
            self.generations[offset >> PAGE_SHIFT] += 1
        self.lanes[offset] = value

    # ── Bloques: carga de programas y lecturas del host ──

    def load_bytes(self, address: int, data: bytes | bytearray | memoryview) -> None:
        """Escribe ``data`` desde ``address`` marcando sus páginas; ``AccessFault`` si no cabe."""
        view = memoryview(data).cast("B")
        offset = address - self.base
        if not 0 <= offset <= self.size - len(view):
            raise AccessFault(address)
        if not len(view):
            return
        self._host_write(offset, len(view))
        self.lanes[offset:offset + len(view)] = view

    def read_bytes(self, address: int, count: int) -> bytes:
        """``count`` bytes desde ``address``; ``AccessFault`` si el rango no cabe en la RAM."""
        if count < 0:
            raise ValueError("read_bytes exige un recuento no negativo")
        offset = address - self.base
        if not 0 <= offset <= self.size - count:
            raise AccessFault(address)
        return bytes(self.lanes[offset:offset + count])

    # ── Páginas, generaciones y snapshot ──

    def generation(self, page: int) -> int:
        """Generación de la página ``page`` (0 ≤ page < size >> 12; fuera de ese rango, ``IndexError``)."""
        if not 0 <= page < len(self.generations):
            raise IndexError(page)
        return self.generations[page]

    def page_of(self, address: int) -> int:
        """Índice de página de 4 KiB de una dirección de la RAM; ``AccessFault`` si no está en la RAM."""
        offset = address - self.base
        if not 0 <= offset < self.size:
            raise AccessFault(address)
        return offset >> PAGE_SHIFT

    def written_pages(self) -> list[int]:
        """Páginas de 4 KiB con alguna escritura desde la creación o el último ``reset`` (exacto)."""
        return [page for page, flag in enumerate(self.written) if flag]

    def snapshot_pages(self) -> list[list[object]]:
        """Páginas no vacías en el formato v2 de VM32: ``[[índice de 4 096 palabras, [enteros con signo]], ...]``."""
        encoded: list[list[object]] = []
        words_per_snapshot_page = SNAPSHOT_PAGE_WORDS
        pages_per_snapshot_page = (words_per_snapshot_page * 4) >> PAGE_SHIFT
        written = self.written
        total = len(written)
        for first in range(0, total, pages_per_snapshot_page):
            if not any(written[first:first + pages_per_snapshot_page]):
                continue
            start = (first << PAGE_SHIFT) >> 2
            values = self.words_i[start:start + words_per_snapshot_page].tolist()
            while values and values[-1] == 0:
                values.pop()
            if values:
                encoded.append([first // pages_per_snapshot_page, values])
        return encoded

    def restore_pages(self, pages: object) -> None:
        """Deja la RAM exactamente como la describe ``pages`` (formato v2); valida antes de tocar nada. Las
        páginas restauradas quedan escritas y con generación nueva; el mapa de código no se toca (lo vacía
        el hart al restaurar, junto con sus cachés)."""
        if not isinstance(pages, list):
            raise ValueError("Páginas de memoria inválidas")
        words_per_snapshot_page = SNAPSHOT_PAGE_WORDS
        maximum_page = (self.size - 1) // (words_per_snapshot_page * 4)
        seen: set[int] = set()
        validated: list[tuple[int, list[int]]] = []
        for record in pages:
            if not isinstance(record, list) or len(record) != 2:
                raise ValueError("Registro de página inválido")
            index, values = record
            if type(index) is not int or not 0 <= index <= maximum_page or index in seen:
                raise ValueError("Índice de página inválido o duplicado")
            if not isinstance(values, list):
                raise ValueError("Contenido de página inválido")
            values = list(values)   # copia antes de medir y validar: lo validado es lo que se escribe, aunque el objeto mienta
            if not 1 <= len(values) <= words_per_snapshot_page:   # sobre la copia: un __len__ falso no cuela más de 4 096 (ciega 4)
                raise ValueError("Contenido de página inválido")
            if index * words_per_snapshot_page * 4 + len(values) * 4 > self.size:
                raise ValueError("Página fuera del espacio de memoria")
            if not all(type(value) is int and INT32_MIN <= value <= INT32_MAX for value in values):
                raise ValueError("Valor de memoria fuera de int32")
            seen.add(index)
            validated.append((index, values))
        self.reset()
        for index, values in validated:
            start = index * words_per_snapshot_page
            self.words_i[start:start + len(values)] = array("i", values)
            self._host_write(start * 4, len(values) * 4)

    def reset(self) -> None:
        """Pone a cero las páginas escritas y las desmarca (``written`` sigue siendo exacto: ninguna página
        escrita). Sus generaciones suben (nunca bajan: una caché que no se limpie no puede confundir el
        contenido nuevo con el viejo). El mapa de código no se toca: una marca de más es segura, y lo
        vacía el hart junto con sus cachés."""
        zero = bytes(PAGE_BYTES)
        written = self.written
        generations = self.generations
        for page, flag in enumerate(written):
            if flag:
                self.lanes[page << PAGE_SHIFT:(page + 1) << PAGE_SHIFT] = zero
                generations[page] += 1
                written[page] = 0

    def fill(self, values: Iterable[int], address: int | None = None) -> None:
        """Escribe palabras consecutivas desde ``address`` (por defecto, la base)."""
        start = self.base if address is None else address
        offset = start - self.base
        words = array("I", [value & MASK32 for value in values])
        if offset & 3:
            raise ValueError("fill exige una dirección alineada a 4 bytes")
        if not 0 <= offset <= self.size - 4 * len(words):
            raise AccessFault(start)
        if not len(words):
            return
        self._host_write(offset, 4 * len(words))
        self.words_u[offset >> 2:(offset >> 2) + len(words)] = words


__all__ = ["AccessFault", "DEFAULT_BASE", "LINE_BYTES", "LINE_MASK", "LINE_SHIFT", "MASK32", "PAGE_BYTES", "PAGE_SHIFT",
           "Rv32Memory", "SNAPSHOT_PAGE_WORDS"]
