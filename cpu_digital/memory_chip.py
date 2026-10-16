"""Almacenamiento persistente disperso optimizado para chips de memoria no volátil."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from threading import RLock


DEFAULT_CHIP_CAPACITY_BYTES = 500_000_000  # 500 MB decimales
DEFAULT_CHIP_PAGE_BYTES = 4_096
_FORMAT_VERSION = 1


class NonVolatileMemoryChip:
    """Chip de memoria no volátil respaldado por un archivo SQLite disperso."""

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
        try:
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA synchronous = FULL")
            self._initialize_database()
        except Exception:
            self._db.close()
            raise

    @classmethod
    def open(cls, path: str | Path) -> NonVolatileMemoryChip:
        """Abre un chip existente detectando su capacidad y tamaño de página."""
        db_path = Path(path)
        if not db_path.is_file():
            raise FileNotFoundError(f"No existe el archivo de chip: {db_path}")
        with sqlite3.connect(db_path, timeout=10) as tmp_db:
            row = tmp_db.execute(
                "SELECT capacity_bytes, page_bytes FROM chip_info WHERE id = 1"
            ).fetchone()
            if row is None:
                raise ValueError("El archivo no contiene metadatos válidos en chip_info")
            capacity_bytes, page_bytes = int(row[0]), int(row[1])
        return cls(db_path, capacity_bytes=capacity_bytes, page_bytes=page_bytes)

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
            row = self._db.execute(
                "SELECT COALESCE(SUM(LENGTH(data)), 0) FROM chip_pages"
            ).fetchone()
            return int(row[0])

    def read(self, address: int, length: int) -> bytes:
        """Lee un rango de bytes en una sola consulta; las páginas ausentes valen cero."""
        self._check_range(address, length)
        if length == 0:
            return b""

        result = bytearray(length)
        first_page = address // self._page_bytes
        last_page = (address + length - 1) // self._page_bytes

        with self._lock:
            self._ensure_open()
            rows = self._db.execute(
                "SELECT page_index, data FROM chip_pages "
                "WHERE page_index BETWEEN ? AND ?",
                (first_page, last_page),
            ).fetchall()

            for page_index, page_data in rows:
                expected_len = self._page_length(page_index)
                if len(page_data) != expected_len:
                    raise sqlite3.DatabaseError(
                        f"Página {page_index} corrupta: longitud {len(page_data)} != {expected_len}"
                    )
                page_start_addr = page_index * self._page_bytes
                read_start = max(address, page_start_addr)
                read_end = min(address + length, page_start_addr + expected_len)
                chunk_size = read_end - read_start

                page_offset = read_start - page_start_addr
                output_offset = read_start - address
                result[output_offset : output_offset + chunk_size] = page_data[
                    page_offset : page_offset + chunk_size
                ]

        return bytes(result)

    def write(self, address: int, data: bytes | bytearray | memoryview) -> None:
        """Escribe bytes de forma transaccional, evitando lecturas en páginas completas."""
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
                upserts: list[tuple[int, bytes]] = []
                deletes: list[tuple[int]] = []

                while remaining:
                    page_index, page_offset = divmod(cursor, self._page_bytes)
                    page_len = self._page_length(page_index)
                    chunk_size = min(remaining, page_len - page_offset)
                    chunk = view[input_offset : input_offset + chunk_size]

                    if page_offset == 0 and chunk_size == page_len:
                        # Página completa: sin SELECT previo
                        if any(chunk):
                            upserts.append((page_index, bytes(chunk)))
                        else:
                            deletes.append((page_index,))
                    else:
                        # Página parcial: requiere fusionar con datos existentes
                        row = self._db.execute(
                            "SELECT data FROM chip_pages WHERE page_index = ?",
                            (page_index,),
                        ).fetchone()
                        if row is not None:
                            if len(row[0]) != page_len:
                                raise sqlite3.DatabaseError(
                                    f"Página {page_index} corrupta en base de datos"
                                )
                            page = bytearray(row[0])
                        else:
                            page = bytearray(page_len)

                        page[page_offset : page_offset + chunk_size] = chunk
                        if any(page):
                            upserts.append((page_index, bytes(page)))
                        elif row is not None:
                            deletes.append((page_index,))

                    cursor += chunk_size
                    input_offset += chunk_size
                    remaining -= chunk_size

                if upserts:
                    self._db.executemany(
                        "INSERT INTO chip_pages(page_index, data) VALUES (?, ?) "
                        "ON CONFLICT(page_index) DO UPDATE SET data = excluded.data",
                        upserts,
                    )
                if deletes:
                    self._db.executemany(
                        "DELETE FROM chip_pages WHERE page_index = ?", deletes
                    )

    def erase(self, address: int, length: int) -> None:
        """Borra el rango indicado optimizando páginas completas mediante DELETE por rango."""
        self._check_range(address, length)
        if length == 0:
            return

        with self._lock:
            self._ensure_open()
            with self._db:
                start_page, start_offset = divmod(address, self._page_bytes)
                end_addr = address + length
                end_page, end_offset = divmod(end_addr, self._page_bytes)

                # Caso 1: El borrado ocurre dentro de una única página
                if start_page == end_page:
                    self._erase_partial_page(start_page, start_offset, length)
                    return

                # Caso 2: Primera página parcial (si no empieza alineada)
                first_full_page = start_page
                if start_offset > 0:
                    first_chunk = self._page_length(start_page) - start_offset
                    self._erase_partial_page(start_page, start_offset, first_chunk)
                    first_full_page += 1

                # Caso 3: Última página parcial (si no termina alineada al final de página)
                last_full_page = end_page - 1
                if end_offset > 0 and end_addr < self._capacity_bytes:
                    self._erase_partial_page(end_page, 0, end_offset)
                elif end_addr == self._capacity_bytes and end_offset > 0:
                    last_full_page = end_page

                # Caso 4: Borrado masivo de todas las páginas completas intermedias
                if first_full_page <= last_full_page:
                    self._db.execute(
                        "DELETE FROM chip_pages WHERE page_index BETWEEN ? AND ?",
                        (first_full_page, last_full_page),
                    )

    def _erase_partial_page(self, page_index: int, offset: int, length: int) -> None:
        page_len = self._page_length(page_index)
        if offset == 0 and length == page_len:
            self._db.execute("DELETE FROM chip_pages WHERE page_index = ?", (page_index,))
            return

        row = self._db.execute(
            "SELECT data FROM chip_pages WHERE page_index = ?", (page_index,)
        ).fetchone()
        if row is not None:
            page = bytearray(row[0])
            page[offset : offset + length] = bytes(length)
            if any(page):
                self._db.execute(
                    "UPDATE chip_pages SET data = ? WHERE page_index = ?",
                    (bytes(page), page_index),
                )
            else:
                self._db.execute(
                    "DELETE FROM chip_pages WHERE page_index = ?", (page_index,)
                )

    def compact(self) -> None:
        """Compacta el archivo SQLite para devolver al sistema operativo el espacio borrado."""
        with self._lock:
            self._ensure_open()
            self._db.execute("VACUUM")

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

    def __len__(self) -> int:
        return self._capacity_bytes

    def __getitem__(self, key: int | slice) -> int | bytes:
        if isinstance(key, int):
            if key < 0:
                key += self._capacity_bytes
            return self.read(key, 1)[0]
        if isinstance(key, slice):
            start, stop, step = key.indices(self._capacity_bytes)
            if step != 1:
                raise ValueError("Solo se admiten slices contiguos (step=1)")
            return self.read(start, max(0, stop - start))
        raise TypeError("Índice debe ser int o slice")

    def __setitem__(
        self, key: int | slice, value: int | bytes | bytearray | memoryview
    ) -> None:
        if isinstance(key, int):
            if key < 0:
                key += self._capacity_bytes
            if type(value) is not int or not (0 <= value <= 255):
                raise ValueError("El valor para un índice entero debe ser un byte (0..255)")
            self.write(key, bytes([value]))
            return
        if isinstance(key, slice):
            start, stop, step = key.indices(self._capacity_bytes)
            if step != 1:
                raise ValueError("Solo se admiten slices contiguos (step=1)")
            view = memoryview(value).cast("B")
            if len(view) != max(0, stop - start):
                raise ValueError("La longitud de los datos no coincide con el tamaño del slice")
            self.write(start, view)
            return
        raise TypeError("Índice debe ser int o slice")

    def __enter__(self) -> NonVolatileMemoryChip:
        with self._lock:
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