"""Almacenamiento persistente disperso para chips de memoria no volátil."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from threading import RLock


DEFAULT_CHIP_CAPACITY_BYTES = 500_000_000  # 500 MB decimales
DEFAULT_CHIP_PAGE_BYTES = 4_096
_FORMAT_VERSION = 1


class NonVolatileMemoryChip:
    """Chip de memoria no volátil respaldado por un archivo SQLite disperso.

    La capacidad es direccionable, no se reserva al abrir el chip. Solo las
    páginas escritas ocupan espacio en el archivo, y las escrituras confirmadas
    sobreviven al cierre y a la reapertura del chip.
    """

    __slots__ = ("_path", "_capacity_bytes", "_page_bytes", "_db", "_lock")

    def __init__(
        self,
        path: str | Path,
        *,
        capacity_bytes: int = DEFAULT_CHIP_CAPACITY_BYTES,
        page_bytes: int = DEFAULT_CHIP_PAGE_BYTES,
    ) -> None:
        if type(capacity_bytes) is not int or capacity_bytes < 1:
            raise ValueError("capacity_bytes debe ser un entero positivo")
        if type(page_bytes) is not int or page_bytes < 1:
            raise ValueError("page_bytes debe ser un entero positivo")

        self._path = Path(path)
        self._capacity_bytes = capacity_bytes
        self._page_bytes = page_bytes
        self._lock = RLock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self._path, timeout=10, check_same_thread=False)
        self._db.execute("PRAGMA synchronous = FULL")
        try:
            self._initialize_database()
        except Exception:
            self._db.close()
            raise

    @property
    def path(self) -> Path:
        return self._path

    @property
    def capacity_bytes(self) -> int:
        return self._capacity_bytes

    @property
    def page_bytes(self) -> int:
        return self._page_bytes

    @property
    def allocated_pages(self) -> int:
        with self._lock:
            self._ensure_open()
            row = self._db.execute("SELECT COUNT(*) FROM chip_pages").fetchone()
            return int(row[0])

    @property
    def allocated_bytes(self) -> int:
        """Bytes de contenido almacenados; no incluye metadatos SQLite."""
        with self._lock:
            self._ensure_open()
            row = self._db.execute("SELECT COALESCE(SUM(LENGTH(data)), 0) FROM chip_pages").fetchone()
            return int(row[0])

    def read(self, address: int, length: int) -> bytes:
        """Lee bytes; las páginas nunca escritas se leen como cero."""
        self._check_range(address, length)
        result = bytearray(length)
        with self._lock:
            self._ensure_open()
            cursor = address
            output_offset = 0
            remaining = length
            while remaining:
                page_index, page_offset = divmod(cursor, self._page_bytes)
                chunk_size = min(remaining, self._page_length(page_index) - page_offset)
                row = self._db.execute(
                    "SELECT data FROM chip_pages WHERE page_index = ?", (page_index,)
                ).fetchone()
                if row is not None:
                    page = row[0]
                    result[output_offset:output_offset + chunk_size] = page[
                        page_offset:page_offset + chunk_size
                    ]
                cursor += chunk_size
                output_offset += chunk_size
                remaining -= chunk_size
        return bytes(result)

    def write(self, address: int, data: bytes | bytearray | memoryview) -> None:
        """Escribe bytes de forma transaccional y persistente."""
        try:
            view = memoryview(data).cast("B")
        except (TypeError, ValueError) as exc:
            raise TypeError("data debe ser una secuencia de bytes") from exc
        self._check_range(address, len(view))
        if not view:
            return

        with self._lock:
            self._ensure_open()
            with self._db:
                cursor = address
                input_offset = 0
                remaining = len(view)
                while remaining:
                    page_index, page_offset = divmod(cursor, self._page_bytes)
                    chunk_size = min(remaining, self._page_length(page_index) - page_offset)
                    row = self._db.execute(
                        "SELECT data FROM chip_pages WHERE page_index = ?", (page_index,)
                    ).fetchone()
                    page = bytearray(row[0]) if row is not None else bytearray(
                        self._page_length(page_index)
                    )
                    page[page_offset:page_offset + chunk_size] = view[
                        input_offset:input_offset + chunk_size
                    ]
                    if any(page):
                        self._db.execute(
                            "INSERT INTO chip_pages(page_index, data) VALUES (?, ?) "
                            "ON CONFLICT(page_index) DO UPDATE SET data = excluded.data",
                            (page_index, bytes(page)),
                        )
                    else:
                        self._db.execute(
                            "DELETE FROM chip_pages WHERE page_index = ?", (page_index,)
                        )
                    cursor += chunk_size
                    input_offset += chunk_size
                    remaining -= chunk_size

    def erase(self, address: int, length: int) -> None:
        """Borra el rango indicado; las áreas borradas vuelven a ser dispersas."""
        self._check_range(address, length)
        if length == 0:
            return
        with self._lock:
            self._ensure_open()
            with self._db:
                cursor = address
                remaining = length
                while remaining:
                    page_index, page_offset = divmod(cursor, self._page_bytes)
                    chunk_size = min(remaining, self._page_length(page_index) - page_offset)
                    row = self._db.execute(
                        "SELECT data FROM chip_pages WHERE page_index = ?", (page_index,)
                    ).fetchone()
                    if row is not None:
                        page = bytearray(row[0])
                        page[page_offset:page_offset + chunk_size] = bytes(chunk_size)
                        if any(page):
                            self._db.execute(
                                "UPDATE chip_pages SET data = ? WHERE page_index = ?",
                                (bytes(page), page_index),
                            )
                        else:
                            self._db.execute(
                                "DELETE FROM chip_pages WHERE page_index = ?", (page_index,)
                            )
                    cursor += chunk_size
                    remaining -= chunk_size

    def sync(self) -> None:
        """Fuerza el commit de cualquier transacción pendiente."""
        with self._lock:
            self._ensure_open()
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def __enter__(self) -> "NonVolatileMemoryChip":
        self._ensure_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _initialize_database(self) -> None:
        tables = {
            row[0]
            for row in self._db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if tables and "chip_info" not in tables:
            raise ValueError("El archivo existente no contiene un chip de memoria compatible")

        self._db.execute(
            "CREATE TABLE IF NOT EXISTS chip_info ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), "
            "format_version INTEGER NOT NULL, "
            "capacity_bytes INTEGER NOT NULL, "
            "page_bytes INTEGER NOT NULL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS chip_pages ("
            "page_index INTEGER PRIMARY KEY, data BLOB NOT NULL)"
        )
        row = self._db.execute(
            "SELECT format_version, capacity_bytes, page_bytes FROM chip_info WHERE id = 1"
        ).fetchone()
        if row is None:
            with self._db:
                self._db.execute(
                    "INSERT INTO chip_info(id, format_version, capacity_bytes, page_bytes) "
                    "VALUES (1, ?, ?, ?)",
                    (_FORMAT_VERSION, self._capacity_bytes, self._page_bytes),
                )
            return
        if row[0] != _FORMAT_VERSION:
            raise ValueError("Versión de chip de memoria no compatible")
        if row[1] != self._capacity_bytes or row[2] != self._page_bytes:
            raise ValueError("El chip existente tiene otra capacidad o tamaño de página")

    def _check_range(self, address: int, length: int) -> None:
        if type(address) is not int or type(length) is not int:
            raise TypeError("La dirección y la longitud deben ser enteros")
        if address < 0 or length < 0 or address + length > self._capacity_bytes:
            raise ValueError("Rango fuera de la capacidad del chip")

    def _page_length(self, page_index: int) -> int:
        return min(self._page_bytes, self._capacity_bytes - page_index * self._page_bytes)

    def _ensure_open(self) -> None:
        if self._db is None:
            raise RuntimeError("El chip de memoria está cerrado")


__all__ = ["DEFAULT_CHIP_CAPACITY_BYTES", "DEFAULT_CHIP_PAGE_BYTES", "NonVolatileMemoryChip"]
