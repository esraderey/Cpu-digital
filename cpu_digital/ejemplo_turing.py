from pathlib import Path
from memory_chip import NonVolatileMemoryChip
from turing_machine import PersistentTuringMachine

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

with NonVolatileMemoryChip(Path("turing_tape.sqlite")) as chip:
    tm = PersistentTuringMachine(
        chip,
        transitions=INCREMENTER_TRANSITIONS,
        initial_state="q_seek_end",
        accept_states={"q_halt"},
    )

    # Cargar "1011" en la posición lógica 0..3
    tm.load_input("1011", start_index=0)
    print(f"Cinta inicial : {tm.read_window(-1, 5)}")

    # Ejecutar hasta detenerse
    final_step = tm.run()

    print(f"Cinta final   : {tm.read_window(-1, 5)}")
    print(f"Estado final  : {tm.state} (Aceptado: {tm.is_accepted})")
    print(f"Pasos totales : {tm.steps}")
    print(f"Páginas usadas: {chip.allocated_pages} ({chip.allocated_bytes} bytes)")