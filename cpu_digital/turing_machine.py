"""Máquina de Turing respaldada por NonVolatileMemoryChip."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Mapping

from .memory_chip import NonVolatileMemoryChip


class Direction(IntEnum):
    LEFT = -1
    STAY = 0
    RIGHT = 1


TransitionMap = Mapping[tuple[str, str], tuple[str, str, Direction | str]]


@dataclass(frozen=True)
class StepResult:
    step_number: int
    previous_state: str
    read_symbol: str
    written_symbol: str
    direction: Direction
    next_state: str
    head_position: int
    halted: bool


class PersistentTuringMachine:
    """Máquina de Turing cuya cinta reside en un NonVolatileMemoryChip."""

    def __init__(
        self,
        chip: NonVolatileMemoryChip,
        *,
        transitions: TransitionMap,
        initial_state: str,
        accept_states: set[str] | frozenset[str],
        reject_states: set[str] | frozenset[str] | None = None,
        blank_symbol: str = "_",
        head_position: int = 0,
    ) -> None:
        if len(blank_symbol.encode("ascii")) != 1:
            raise ValueError("blank_symbol debe ser un único carácter ASCII")
        if blank_symbol == "\x00":
            raise ValueError("Usa un carácter imprimible como '_' para blank_symbol")

        self._chip = chip
        self._blank = blank_symbol
        self._origin = chip.capacity_bytes // 2
        self._state = initial_state
        self._accept_states = frozenset(accept_states)
        self._reject_states = frozenset(reject_states or ())
        self._head = head_position
        self._steps = 0
        self._transitions = self._normalize_transitions(transitions)

    @property
    def state(self) -> str:
        return self._state

    @property
    def head_position(self) -> int:
        return self._head

    @property
    def steps(self) -> int:
        return self._steps

    @property
    def is_halted(self) -> bool:
        return self._state in self._accept_states or self._state in self._reject_states

    @property
    def is_accepted(self) -> bool:
        return self._state in self._accept_states

    def _to_physical_address(self, logical_index: int) -> int:
        address = self._origin + logical_index
        if not (0 <= address < self._chip.capacity_bytes):
            raise IndexError(f"Posición de cinta {logical_index} fuera de los límites del chip")
        return address

    def _encode_symbol(self, symbol: str) -> bytes:
        if len(symbol) != 1:
            raise ValueError(f"El símbolo debe tener longitud 1, recibido: {symbol!r}")
        if symbol == self._blank:
            return b"\x00"
        raw = symbol.encode("ascii")
        if raw == b"\x00":
            raise ValueError("El byte 0x00 está reservado para el símbolo blanco")
        return raw

    def _decode_byte(self, byte_val: int) -> str:
        if byte_val == 0:
            return self._blank
        return chr(byte_val)

    def _normalize_transitions(
        self, transitions: TransitionMap
    ) -> dict[tuple[str, str], tuple[str, str, Direction]]:
        normalized: dict[tuple[str, str], tuple[str, str, Direction]] = {}
        dir_map = {"L": Direction.LEFT, "R": Direction.RIGHT, "S": Direction.STAY}

        for (state, read_sym), (next_state, write_sym, move) in transitions.items():
            self._encode_symbol(read_sym)
            self._encode_symbol(write_sym)
            if isinstance(move, str):
                if move not in dir_map:
                    raise ValueError(f"Dirección inválida: {move!r}. Usa 'L', 'R' o 'S'")
                direction = dir_map[move]
            else:
                direction = Direction(move)
            normalized[(state, read_sym)] = (next_state, write_sym, direction)
        return normalized

    def read_cell(self, logical_index: int | None = None) -> str:
        """Lee el símbolo en la posición indicada (o bajo el cabezal actual)."""
        idx = self._head if logical_index is None else logical_index
        addr = self._to_physical_address(idx)
        byte_val = self._chip.read(addr, 1)[0]
        return self._decode_byte(byte_val)

    def write_cell(self, symbol: str, logical_index: int | None = None) -> None:
        """Escribe un símbolo en la posición indicada (o bajo el cabezal actual)."""
        idx = self._head if logical_index is None else logical_index
        addr = self._to_physical_address(idx)
        self._chip.write(addr, self._encode_symbol(symbol))

    def load_input(self, data: str, start_index: int = 0) -> None:
        """Escribe una cadena inicial en la cinta en una sola operación transaccional."""
        encoded = b"".join(self._encode_symbol(ch) for ch in data)
        addr = self._to_physical_address(start_index)
        self._chip.write(addr, encoded)

    def read_window(self, start_index: int, end_index: int) -> str:
        """Lee un segmento contiguo de la cinta [start_index, end_index]."""
        if end_index < start_index:
            return ""
        addr = self._to_physical_address(start_index)
        length = end_index - start_index + 1
        raw = self._chip.read(addr, length)
        return "".join(self._decode_byte(b) for b in raw)

    def step(self) -> StepResult:
        """Ejecuta un paso de cómputo según la tabla de transiciones."""
        if self.is_halted:
            return StepResult(
                step_number=self._steps,
                previous_state=self._state,
                read_symbol=self.read_cell(),
                written_symbol=self.read_cell(),
                direction=Direction.STAY,
                next_state=self._state,
                head_position=self._head,
                halted=True,
            )

        current_symbol = self.read_cell()
        key = (self._state, current_symbol)

        if key not in self._transitions:
            # Si no hay transición definida, pasa a un estado de rechazo implícito
            prev_state = self._state
            self._state = "HALT_REJECT"
            self._reject_states = self._reject_states | {"HALT_REJECT"}
            return StepResult(
                step_number=self._steps,
                previous_state=prev_state,
                read_symbol=current_symbol,
                written_symbol=current_symbol,
                direction=Direction.STAY,
                next_state=self._state,
                head_position=self._head,
                halted=True,
            )

        next_state, write_symbol, direction = self._transitions[key]
        if write_symbol != current_symbol:
            self.write_cell(write_symbol)

        prev_state = self._state
        self._head += int(direction)
        self._state = next_state
        self._steps += 1

        return StepResult(
            step_number=self._steps,
            previous_state=prev_state,
            read_symbol=current_symbol,
            written_symbol=write_symbol,
            direction=direction,
            next_state=self._state,
            head_position=self._head,
            halted=self.is_halted,
        )

    def run(self, max_steps: int = 10_000) -> StepResult:
        """Ejecuta la máquina hasta alcanzar un estado de parada o agotar max_steps."""
        if max_steps < 1:
            raise ValueError("max_steps debe ser positivo")
        result = self.step()
        while not result.halted and self._steps < max_steps:
            result = self.step()
        if not result.halted:
            raise RuntimeError(f"Límite de {max_steps} pasos alcanzado sin detenerse")
        return result
