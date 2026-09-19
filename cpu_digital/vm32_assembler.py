"""Ensamblador, enlazador y formato binario para Tramoya VM32."""

from __future__ import annotations

import ast
import json
import re
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .vm32_isa import VM32_ISA, VMInstruction, VMOpcode, vm32_instruction


MAGIC = b"TVM2"
FORMAT_VERSION = 1
_HEADER = struct.Struct("<4sHHIIIII")
_WORD = struct.Struct("<i")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LABEL = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):")
_SYMBOL_EXPR = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*)(?:\s*([+-])\s*((?:0[xX][0-9a-fA-F]+)|(?:0[bB][01]+)|(?:\d+)))?$"
)
_REGISTER = re.compile(r"^[Rr](\d+)$")
DEFAULT_ASSEMBLY_WORD_LIMIT = 1_048_576


class VMAssemblyError(ValueError):
    def __init__(self, message: str, line: int | None = None):
        self.line = line
        super().__init__((f"Línea {line}: " if line is not None else "") + message)


def signed32(value: int) -> int:
    unsigned = value & 0xFFFFFFFF
    return unsigned - 0x100000000 if unsigned >= 0x80000000 else unsigned


@dataclass(frozen=True, slots=True)
class VMListingLine:
    line: int
    address: int
    words: tuple[int, ...]
    source: str


@dataclass(frozen=True, slots=True)
class Program32:
    words: tuple[int, ...]
    entry: int
    code_size: int
    data_size: int
    symbols: Mapping[str, int]
    source_name: str = "<memory>"

    def __post_init__(self) -> None:
        if self.code_size <= 0 or self.code_size % 4:
            raise ValueError("code_size debe ser positivo y múltiplo de 4")
        if self.data_size < 0 or self.code_size + self.data_size != len(self.words):
            raise ValueError("Tamaños de código/datos inconsistentes")
        if not 0 <= self.entry < self.code_size or self.entry % 4:
            raise ValueError("Punto de entrada inválido")

    def to_bytes(self) -> bytes:
        payload = b"".join(_WORD.pack(signed32(word)) for word in self.words)
        crc = zlib.crc32(payload) & 0xFFFFFFFF
        header = _HEADER.pack(
            MAGIC,
            FORMAT_VERSION,
            _HEADER.size,
            self.entry,
            self.code_size,
            self.data_size,
            len(self.words),
            crc,
        )
        metadata = json.dumps(
            {"symbols": dict(self.symbols), "source_name": self.source_name},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return header + struct.pack("<I", len(metadata)) + metadata + payload

    @classmethod
    def from_bytes(cls, data: bytes) -> "Program32":
        if len(data) < _HEADER.size + 4:
            raise ValueError("Bytecode truncado")
        magic, version, header_size, entry, code_size, data_size, count, expected_crc = _HEADER.unpack_from(data)
        if magic != MAGIC:
            raise ValueError("Firma de bytecode inválida")
        if version != FORMAT_VERSION or header_size != _HEADER.size:
            raise ValueError(f"Versión de bytecode no compatible: {version}")
        metadata_size = struct.unpack_from("<I", data, _HEADER.size)[0]
        payload_start = _HEADER.size + 4 + metadata_size
        expected_size = payload_start + count * _WORD.size
        if expected_size != len(data):
            raise ValueError("Longitud de bytecode inconsistente")
        try:
            metadata = json.loads(data[_HEADER.size + 4:payload_start].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Metadatos de bytecode inválidos") from exc
        payload = data[payload_start:]
        if zlib.crc32(payload) & 0xFFFFFFFF != expected_crc:
            raise ValueError("CRC de bytecode inválido")
        words = tuple(_WORD.unpack_from(payload, offset)[0] for offset in range(0, len(payload), 4))
        symbols = metadata.get("symbols", {})
        if not isinstance(symbols, dict) or not all(isinstance(k, str) and isinstance(v, int) for k, v in symbols.items()):
            raise ValueError("Tabla de símbolos inválida")
        return cls(
            words=words,
            entry=entry,
            code_size=code_size,
            data_size=data_size,
            symbols=symbols,
            source_name=str(metadata.get("source_name", "<bytecode>")),
        )

    def save(self, path: str | Path) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.to_bytes())
        return destination

    @classmethod
    def load(cls, path: str | Path) -> "Program32":
        return cls.from_bytes(Path(path).read_bytes())


@dataclass(frozen=True, slots=True)
class VMAssemblyResult:
    program: Program32
    listing: tuple[VMListingLine, ...]

    def format_listing(self) -> str:
        rows: list[str] = []
        for item in self.listing:
            encoded = " ".join(f"{word & 0xFFFFFFFF:08X}" for word in item.words)
            rows.append(f"{item.address:08X}  {encoded:<36}  {item.source}")
        return "\n".join(rows)


@dataclass(frozen=True, slots=True)
class _Symbol:
    section: str
    offset: int


@dataclass(frozen=True, slots=True)
class _Record:
    line: int
    section: str
    offset: int
    kind: str
    payload: object
    source: str


def _strip_comment(line: str) -> str:
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


def _split_operands(text: str) -> list[str]:
    if not text.strip():
        return []
    result: list[str] = []
    start = 0
    quote: str | None = None
    depth = 0
    escaped = False
    for index, char in enumerate(text):
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
        if quote is None:
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth < 0:
                    raise ValueError("Corchete de cierre inesperado")
            elif char == "," and depth == 0:
                result.append(text[start:index].strip())
                start = index + 1
    if quote or depth:
        raise ValueError("Comillas o corchetes sin cerrar")
    result.append(text[start:].strip())
    return result


def _literal(text: str) -> int | None:
    try:
        return int(text, 0)
    except ValueError:
        pass
    try:
        value = ast.literal_eval(text)
    except (SyntaxError, ValueError):
        return None
    if isinstance(value, str) and len(value) == 1:
        return ord(value)
    return None


def _resolve(expression: str, symbols: Mapping[str, int], line: int) -> int:
    direct = _literal(expression.strip())
    if direct is not None:
        return direct
    match = _SYMBOL_EXPR.fullmatch(expression.strip())
    if not match:
        raise VMAssemblyError(f"Expresión inválida: {expression!r}", line)
    name, operator, offset_text = match.groups()
    key = name.upper()
    if key not in symbols:
        raise VMAssemblyError(f"Símbolo no definido: {name}", line)
    value = symbols[key]
    if operator and offset_text:
        offset = int(offset_text, 0)
        value = value + offset if operator == "+" else value - offset
    return value


def _register(text: str, line: int) -> int:
    match = _REGISTER.fullmatch(text.strip())
    if not match or not 0 <= int(match.group(1)) <= 15:
        raise VMAssemblyError(f"Registro inválido: {text}", line)
    return int(match.group(1))


def _parse_memory(text: str, symbols: Mapping[str, int], line: int) -> tuple[int, int]:
    text = text.strip()
    if not text.startswith("[") or not text.endswith("]"):
        raise VMAssemblyError(f"Operando de memoria inválido: {text}", line)
    inner = text[1:-1].strip()
    register_match = re.match(r"^([Rr]\d+)(.*)$", inner)
    if not register_match:
        return 0, _resolve(inner, symbols, line)
    base = _register(register_match.group(1), line)
    remainder = register_match.group(2).strip()
    if not remainder:
        return base, 0
    if remainder[0] not in "+-":
        raise VMAssemblyError(f"Desplazamiento de memoria inválido: {text}", line)
    offset = _resolve(remainder[1:].strip(), symbols, line)
    return base, offset if remainder[0] == "+" else -offset


class VM32Assembler:
    """Ensamblador por secciones para el bytecode fijo de Tramoya VM32."""

    _COUNTS = {
        "none": 0,
        "reg": 1,
        "reg_reg": 2,
        "reg_imm": 2,
        "reg_reg_reg": 3,
        "reg_reg_imm": 3,
        "reg_mem": 2,
        "target": 1,
        "reg_target": 2,
        "imm": 1,
        "imm_target": 2,
    }

    def assemble(
        self,
        source: str,
        source_name: str = "<memory>",
        *,
        max_words: int = DEFAULT_ASSEMBLY_WORD_LIMIT,
    ) -> VMAssemblyResult:
        if not isinstance(max_words, int) or isinstance(max_words, bool) or max_words < 1:
            raise ValueError("max_words debe ser un entero positivo")
        section = "code"
        offsets = {"code": 0, "data": 0}
        raw_symbols: dict[str, _Symbol] = {}
        constants: dict[str, int] = {}
        records: list[_Record] = []
        entry_expression: str | None = None

        def reserve(target_section: str, count: int, line: int) -> None:
            if count < 0 or offsets["code"] + offsets["data"] + count > max_words:
                raise VMAssemblyError(
                    f"El programa excede el límite de {max_words} palabras",
                    line,
                )
            offsets[target_section] += count

        for line_number, original in enumerate(source.splitlines(), 1):
            statement = _strip_comment(original).strip()
            if not statement:
                continue
            label_match = _LABEL.match(statement)
            if label_match:
                name = label_match.group(1).upper()
                self._define(raw_symbols, constants, name, _Symbol(section, offsets[section]), line_number)
                statement = statement[label_match.end():].strip()
                if not statement:
                    continue

            parts = statement.split(maxsplit=1)
            head = parts[0].upper()
            tail = parts[1].strip() if len(parts) == 2 else ""

            if head in {".CODE", ".TEXT"}:
                section = "code"
                continue
            if head == ".DATA":
                section = "data"
                continue
            if head == ".ENTRY":
                if not tail:
                    raise VMAssemblyError(".ENTRY requiere una etiqueta", line_number)
                entry_expression = tail
                continue
            if head == ".EQU":
                equ = tail.replace(",", " ").split()
                if len(equ) != 2 or not _NAME.fullmatch(equ[0]):
                    raise VMAssemblyError("Uso: .EQU NOMBRE VALOR", line_number)
                value = _literal(equ[1])
                if value is None:
                    raise VMAssemblyError(".EQU requiere un literal numérico", line_number)
                self._define(raw_symbols, constants, equ[0].upper(), value, line_number)
                continue

            instruction = vm32_instruction(head)
            if instruction:
                if section != "code":
                    raise VMAssemblyError("Las instrucciones solo son válidas en .code", line_number)
                try:
                    operands = _split_operands(tail)
                except ValueError as exc:
                    raise VMAssemblyError(str(exc), line_number) from exc
                expected = self._COUNTS[instruction.form]
                if len(operands) != expected:
                    raise VMAssemblyError(
                        f"{instruction.mnemonic} requiere {expected} operando(s), recibió {len(operands)}",
                        line_number,
                    )
                records.append(
                    _Record(line_number, section, offsets[section], "instruction", (instruction, operands), original.strip())
                )
                reserve(section, 4, line_number)
                continue

            if section != "data":
                raise VMAssemblyError(f"Instrucción o directiva desconocida: {parts[0]}", line_number)
            if head == ".WORD":
                try:
                    values = _split_operands(tail)
                except ValueError as exc:
                    raise VMAssemblyError(str(exc), line_number) from exc
                if not values:
                    raise VMAssemblyError(".WORD requiere valores", line_number)
                records.append(_Record(line_number, section, offsets[section], "word", values, original.strip()))
                reserve(section, len(values), line_number)
            elif head == ".STRING":
                try:
                    value = ast.literal_eval(tail)
                except (SyntaxError, ValueError) as exc:
                    raise VMAssemblyError("Cadena inválida", line_number) from exc
                if not isinstance(value, str):
                    raise VMAssemblyError(".STRING requiere texto entre comillas", line_number)
                records.append(_Record(line_number, section, offsets[section], "string", value, original.strip()))
                reserve(section, len(value) + 1, line_number)
            elif head == ".SPACE":
                try:
                    count = _resolve(tail, constants, line_number)
                except VMAssemblyError as exc:
                    raise VMAssemblyError(".SPACE requiere una cantidad conocida no negativa", line_number) from exc
                if count < 0:
                    raise VMAssemblyError(".SPACE requiere una cantidad no negativa", line_number)
                records.append(_Record(line_number, section, offsets[section], "space", count, original.strip()))
                reserve(section, count, line_number)
            elif head == ".ALIGN":
                try:
                    alignment = _resolve(tail, constants, line_number)
                except VMAssemblyError as exc:
                    raise VMAssemblyError(".ALIGN requiere una potencia de dos conocida", line_number) from exc
                if alignment < 1 or alignment & (alignment - 1):
                    raise VMAssemblyError(".ALIGN requiere una potencia de dos", line_number)
                padding = (-offsets[section]) % alignment
                if padding:
                    records.append(_Record(line_number, section, offsets[section], "space", padding, original.strip()))
                    reserve(section, padding, line_number)
            else:
                raise VMAssemblyError(f"Directiva de datos desconocida: {parts[0]}", line_number)

        code_size = offsets["code"]
        if code_size == 0:
            raise VMAssemblyError("El programa no contiene código")
        symbols = dict(constants)
        for name, symbol in raw_symbols.items():
            symbols[name] = symbol.offset if symbol.section == "code" else code_size + symbol.offset

        if entry_expression is None:
            entry = symbols.get("_START", 0)
        else:
            entry = _resolve(entry_expression, symbols, 0)
        if not 0 <= entry < code_size or entry % 4:
            raise VMAssemblyError("El punto de entrada no es una instrucción válida")

        code = [0] * code_size
        data = [0] * offsets["data"]
        listing: list[VMListingLine] = []
        for record in records:
            encoded = self._encode(record, symbols, code_size)
            target = code if record.section == "code" else data
            target[record.offset:record.offset + len(encoded)] = encoded
            address = record.offset if record.section == "code" else code_size + record.offset
            listing.append(VMListingLine(record.line, address, tuple(encoded), record.source))

        program = Program32(
            words=tuple([*code, *data]),
            entry=entry,
            code_size=code_size,
            data_size=len(data),
            symbols=symbols,
            source_name=source_name,
        )
        return VMAssemblyResult(program, tuple(listing))

    @staticmethod
    def _define(
        raw_symbols: dict[str, _Symbol],
        constants: dict[str, int],
        name: str,
        value: _Symbol | int,
        line: int,
    ) -> None:
        if name in raw_symbols or name in constants:
            raise VMAssemblyError(f"Símbolo duplicado: {name}", line)
        if isinstance(value, _Symbol):
            raw_symbols[name] = value
        else:
            constants[name] = value

    def _encode(self, record: _Record, symbols: Mapping[str, int], code_size: int) -> list[int]:
        if record.kind == "word":
            return [signed32(_resolve(value, symbols, record.line)) for value in record.payload]
        if record.kind == "string":
            return [*[signed32(ord(char)) for char in record.payload], 0]
        if record.kind == "space":
            return [0] * int(record.payload)

        instruction, operands = record.payload
        encoded = [int(instruction.opcode), 0, 0, 0]
        form = instruction.form

        if form == "reg":
            encoded[1] = _register(operands[0], record.line)
        elif form == "reg_reg":
            encoded[1:3] = [_register(operand, record.line) for operand in operands]
        elif form == "reg_imm":
            encoded[1] = _register(operands[0], record.line)
            encoded[2] = signed32(_resolve(operands[1], symbols, record.line))
        elif form == "reg_reg_reg":
            encoded[1:4] = [_register(operand, record.line) for operand in operands]
        elif form == "reg_reg_imm":
            encoded[1] = _register(operands[0], record.line)
            encoded[2] = _register(operands[1], record.line)
            encoded[3] = signed32(_resolve(operands[2], symbols, record.line))
        elif form == "reg_mem":
            encoded[1] = _register(operands[0], record.line)
            encoded[2], encoded[3] = _parse_memory(operands[1], symbols, record.line)
            encoded[3] = signed32(encoded[3])
        elif form == "target":
            encoded[1] = self._target(operands[0], symbols, record.line, code_size)
        elif form == "reg_target":
            encoded[1] = _register(operands[0], record.line)
            encoded[2] = signed32(_resolve(operands[1], symbols, record.line))
        elif form == "imm":
            encoded[1] = signed32(_resolve(operands[0], symbols, record.line))
        elif form == "imm_target":
            encoded[1] = signed32(_resolve(operands[0], symbols, record.line))
            encoded[2] = self._target(operands[1], symbols, record.line, code_size)
        return encoded

    @staticmethod
    def _target(expression: str, symbols: Mapping[str, int], line: int, code_size: int) -> int:
        target = _resolve(expression, symbols, line)
        if not 0 <= target < code_size or target % 4:
            raise VMAssemblyError(f"Destino no ejecutable: {expression}", line)
        return target


def disassemble_vm32(program: Program32) -> list[str]:
    lines: list[str] = []
    for pc in range(0, program.code_size, 4):
        opcode, a, b, c = program.words[pc:pc + 4]
        spec: VMInstruction | None = VM32_ISA.get(opcode)
        name = spec.mnemonic if spec else f"OP_{opcode}"
        lines.append(f"{pc:08X}: {name:<8} {a:>11}, {b:>11}, {c:>11}")
    return lines
