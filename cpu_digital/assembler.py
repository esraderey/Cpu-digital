"""Ensamblador de dos pasadas para la ISA de CPU Digital."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from .isa import ISA, Instruction, Opcode, instruction_for_mnemonic


_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LABEL = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):")
ADDRESS_SPACE = 0x10000  # la CPU direcciona 65536 palabras
_SYMBOL_EXPR = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*)(?:([+-])((?:0[xX][0-9a-fA-F]+)|(?:0[bB][01]+)|(?:\d+)))?$"
)


class AssemblyError(ValueError):
    """Error de ensamblado con ubicación en el código fuente."""

    def __init__(self, message: str, line: int | None = None):
        self.line = line
        prefix = f"Línea {line}: " if line is not None else ""
        super().__init__(prefix + message)


@dataclass(frozen=True, slots=True)
class ListingLine:
    line: int
    address: int
    words: tuple[int, ...]
    source: str


@dataclass(frozen=True, slots=True)
class AssemblyResult:
    words: tuple[int, ...]
    symbols: Mapping[str, int]
    listing: tuple[ListingLine, ...]

    def format_listing(self) -> str:
        rows: list[str] = []
        for item in self.listing:
            encoded = " ".join(f"{word & 0xFFFF:04X}" for word in item.words)
            rows.append(f"{item.address:04X}  {encoded:<14}  {item.source}")
        return "\n".join(rows)


@dataclass(frozen=True, slots=True)
class _Record:
    line: int
    address: int
    kind: str
    payload: object
    source: str


def _without_comment(line: str) -> str:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote:
            escaped = True
            continue
        if char in {"'", '"'}:
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
            continue
        if char in {";", "#"} and quote is None:
            return line[:index]
    return line


def _split_values(text: str) -> list[str]:
    values = [part.strip() for part in text.split(",")]
    if len(values) == 1 and "," not in text:
        values = text.split()
    return [value for value in values if value]


def _parse_string(text: str, line: int) -> str:
    try:
        value = ast.literal_eval(text.strip())
    except (SyntaxError, ValueError, TypeError, RecursionError, MemoryError) as exc:
        raise AssemblyError("Cadena inválida en .STRING", line) from exc
    if not isinstance(value, str):
        raise AssemblyError(".STRING requiere una cadena entre comillas", line)
    return value


def _literal(text: str) -> int | None:
    try:
        return int(text, 0)
    except ValueError:
        pass
    try:
        value = ast.literal_eval(text)
    except (SyntaxError, ValueError, TypeError, RecursionError, MemoryError):
        return None
    if isinstance(value, str) and len(value) == 1:
        return ord(value)
    return None


def _resolve(expression: str, symbols: Mapping[str, int], line: int) -> int:
    expression = expression.strip()
    direct = _literal(expression)
    if direct is not None:
        return direct

    match = _SYMBOL_EXPR.fullmatch(expression)
    if not match:
        raise AssemblyError(f"Expresión inválida: {expression!r}", line)
    name, operator, offset_text = match.groups()
    key = name.upper()
    if key not in symbols:
        raise AssemblyError(f"Símbolo no definido: {name}", line)
    value = symbols[key]
    if operator and offset_text:
        offset = int(offset_text, 0 if offset_text[:2].lower() in {"0x", "0b"} else 10)
        value = value + offset if operator == "+" else value - offset
    return value


def _check_extent(location: int, line: int) -> None:
    if location > ADDRESS_SPACE:
        raise AssemblyError(f"El programa excede el espacio de {ADDRESS_SPACE} palabras", line)


def _validate_word(value: int, line: int) -> int:
    if not -32768 <= value <= 65535:
        raise AssemblyError(f"Valor fuera de 16 bits: {value}", line)
    return value if value <= 32767 else value - 65536


class Assembler:
    """Ensamblador con etiquetas, constantes, datos, cadenas y `.ORG`."""

    def assemble(self, source: str) -> AssemblyResult:
        symbols: dict[str, int] = {}
        records: list[_Record] = []
        location = 0
        highest = 0

        for line_number, original in enumerate(source.splitlines(), 1):
            statement = _without_comment(original).strip()
            if not statement:
                continue

            label_match = _LABEL.match(statement)
            if label_match:
                name = label_match.group(1).upper()
                self._define(symbols, name, location, line_number)
                statement = statement[label_match.end():].strip()
                if not statement:
                    continue

            statement_parts = statement.split(maxsplit=1)
            head = statement_parts[0]
            tail = statement_parts[1].strip() if len(statement_parts) == 2 else ""
            directive = head.upper()

            if directive == ".EQU":
                parts = tail.replace(",", " ").split()
                if len(parts) != 2 or not _NAME.fullmatch(parts[0]):
                    raise AssemblyError("Uso: .EQU NOMBRE VALOR", line_number)
                value = _resolve(parts[1], symbols, line_number)
                self._define(symbols, parts[0].upper(), value, line_number)
                continue

            if directive == ".ORG":
                if not tail:
                    raise AssemblyError(".ORG requiere una dirección", line_number)
                new_location = _resolve(tail, symbols, line_number)
                if new_location < location:
                    raise AssemblyError(".ORG no puede retroceder ni solapar datos", line_number)
                if new_location < 0:
                    raise AssemblyError("La dirección de .ORG no puede ser negativa", line_number)
                _check_extent(new_location, line_number)
                location = new_location
                highest = max(highest, location)
                continue

            if directive == ".WORD":
                values = _split_values(tail)
                if not values:
                    raise AssemblyError(".WORD requiere al menos un valor", line_number)
                records.append(_Record(line_number, location, "word", values, original.strip()))
                location += len(values)
                _check_extent(location, line_number)
                highest = max(highest, location)
                continue

            if directive == ".STRING":
                value = _parse_string(tail, line_number)
                records.append(_Record(line_number, location, "string", value, original.strip()))
                location += len(value) + 1
                _check_extent(location, line_number)
                highest = max(highest, location)
                continue

            instruction = instruction_for_mnemonic(directive)
            if instruction is None:
                raise AssemblyError(f"Instrucción desconocida: {head}", line_number)

            operand = tail.rstrip(",").strip()
            if instruction.operand == "none" and operand:
                raise AssemblyError(f"{instruction.mnemonic} no recibe operandos", line_number)
            if instruction.operand != "none":
                if not operand or "," in operand:
                    raise AssemblyError(f"{instruction.mnemonic} requiere un operando", line_number)

            records.append(
                _Record(line_number, location, "instruction", (instruction, operand), original.strip())
            )
            location += instruction.words
            _check_extent(location, line_number)
            highest = max(highest, location)

        memory = [0] * highest
        listing: list[ListingLine] = []

        for record in records:
            encoded = self._encode(record, symbols)
            end = record.address + len(encoded)
            memory[record.address:end] = encoded
            listing.append(ListingLine(record.line, record.address, tuple(encoded), record.source))

        return AssemblyResult(tuple(memory), dict(symbols), tuple(listing))

    @staticmethod
    def _define(symbols: dict[str, int], name: str, value: int, line: int) -> None:
        if name in symbols:
            raise AssemblyError(f"Símbolo duplicado: {name}", line)
        symbols[name] = value

    @staticmethod
    def _encode(record: _Record, symbols: Mapping[str, int]) -> list[int]:
        if record.kind == "word":
            return [_validate_word(_resolve(value, symbols, record.line), record.line) for value in record.payload]

        if record.kind == "string":
            values = [ord(char) for char in record.payload]
            return [_validate_word(value, record.line) for value in [*values, 0]]

        instruction, operand = record.payload
        encoded = [int(instruction.opcode)]
        if instruction.operand != "none":
            value = _resolve(operand, symbols, record.line)
            if instruction.operand in {"address", "target"} and value < 0:
                raise AssemblyError("Una dirección no puede ser negativa", record.line)
            encoded.append(_validate_word(value, record.line))
        return encoded


def disassemble(words: Sequence[int], start: int = 0, stop_at_halt: bool = False) -> Iterable[str]:
    """Desensambla palabras; los datos desconocidos se muestran como `.WORD`."""

    pc = start
    while pc < len(words):
        address = pc
        opcode = words[pc]
        instruction: Instruction | None = ISA.get(opcode)
        if instruction is None:
            yield f"{address:04X}: .WORD {opcode}"
            pc += 1
            continue

        pc += 1
        if instruction.operand != "none":
            if pc >= len(words):
                yield f"{address:04X}: {instruction.mnemonic} <faltante>"
                break
            yield f"{address:04X}: {instruction.mnemonic} {words[pc]}"
            pc += 1
        else:
            yield f"{address:04X}: {instruction.mnemonic}"

        if stop_at_halt and instruction.opcode == Opcode.HALT:
            break
