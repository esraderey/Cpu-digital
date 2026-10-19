"""Ejemplo: un incrementador binario sobre ``PersistentTuringMachine``.

Uso::

    python -m cpu_digital.ejemplo_turing [ruta_del_chip.sqlite]

Sin argumento, la cinta se guarda en un archivo temporal que se borra al terminar.
Importar este módulo no ejecuta nada.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from cpu_digital.memory_chip import NonVolatileMemoryChip
from cpu_digital.turing_machine import PersistentTuringMachine

# (estado_actual, leído): (nuevo_estado, escribir, dirección)
INCREMENTER_TRANSITIONS = {
    # 1. Avanzar hasta el final del número binario
    ("q_seek_end", "0"): ("q_seek_end", "0", "R"),
    ("q_seek_end", "1"): ("q_seek_end", "1", "R"),
    ("q_seek_end", "_"): ("q_add_carry", "_", "L"),

    # 2. Propagar el acarreo hacia la izquierda
    ("q_add_carry", "1"): ("q_add_carry", "0", "L"),  # 1 + 1 = 0 con acarreo 1
    ("q_add_carry", "0"): ("q_halt", "1", "S"),       # 0 + 1 = 1, fin del acarreo
    ("q_add_carry", "_"): ("q_halt", "1", "S"),       # Desbordamiento (ej. 111 -> 1000)
}


def ejecutar(ruta_chip: Path, entrada: str = "1011") -> None:
    """Carga ``entrada`` en la cinta, ejecuta el incrementador e imprime el resultado."""
    with NonVolatileMemoryChip(ruta_chip) as chip:
        tm = PersistentTuringMachine(
            chip,
            transitions=INCREMENTER_TRANSITIONS,
            initial_state="q_seek_end",
            accept_states={"q_halt"},
        )

        # Cargar la entrada en la posición lógica 0..len(entrada)-1
        tm.load_input(entrada, start_index=0)
        print(f"Cinta inicial : {tm.read_window(-1, 5)}")

        # Ejecutar hasta detenerse
        tm.run()

        print(f"Cinta final   : {tm.read_window(-1, 5)}")
        print(f"Estado final  : {tm.state} (Aceptado: {tm.is_accepted})")
        print(f"Pasos totales : {tm.steps}")
        print(f"Páginas usadas: {chip.allocated_pages} ({chip.allocated_bytes} bytes)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Incrementador binario sobre una máquina de Turing persistente."
    )
    parser.add_argument(
        "ruta",
        nargs="?",
        type=Path,
        help="archivo SQLite del chip (por defecto, uno temporal que se borra al terminar)",
    )
    args = parser.parse_args(argv)
    if args.ruta is not None:
        ejecutar(args.ruta)
    else:
        with tempfile.TemporaryDirectory(prefix="turing_tape_") as carpeta:
            ejecutar(Path(carpeta) / "turing_tape.sqlite")
    return 0


if __name__ == "__main__":
    sys.exit(main())
