"""Acelerador de bucles de VM32: avance rápido con semántica exacta.

Un bucle interno cuyo cuerpo es lineal (sin llamadas, syscalls ni saltos
salvo el de vuelta) se compila una vez a una función Python que ejecuta las
iteraciones sin la sobrecarga por instrucción del intérprete: sin checkpoint,
sin dispatch, sin traza. El contrato es que el estado observable (registros,
banderas, memoria, pc, gas, ciclos, contador de instrucciones) sea **idéntico**
al que dejaría el intérprete, instrucción a instrucción:

* Cada instrucción se genera con la misma aritmética, los mismos redondeos a
  float32 y las mismas banderas que su manejador en ``TramoyaVM32``.
* El gas y el presupuesto de instrucciones se comprueban por iteración; si no
  alcanzan, la función devuelve el control en la cabecera y el intérprete
  agota el gas en la instrucción exacta.
* Antes de cada acceso a memoria se comprueba el rango. Si el acceso podría
  fallar (o tocar código, o la ROM), la función devuelve el control en esa
  instrucción con el estado parcial ya escrito, y el intérprete la ejecuta con
  su validación, su mensaje y su rollback. Ninguna instrucción se ejecuta dos
  veces y ninguna a medias.

Solo se acelera cuando no hay traza, breakpoints ni interrupciones pendientes.
El resultado se verifica por fuzzing diferencial contra el intérprete
(``tests/test_vm32_loops.py``).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from .vm32_isa import VM32_ISA, VMOpcode

if TYPE_CHECKING:  # pragma: no cover
    from .vm32 import TramoyaVM32

MAX_BODY_INSTRUCTIONS = 64

_BACKEDGE_CONDITIONS = {
    VMOpcode.JMP: "True",
    VMOpcode.JZ: "Z",
    VMOpcode.JNZ: "not Z",
    VMOpcode.JNEG: "N",
    VMOpcode.JPOS: "not Z and not N",
    VMOpcode.JC: "C",
    VMOpcode.JNC: "not C",
    VMOpcode.JLT: "N != O",
    VMOpcode.JGT: "not Z and N == O",
}

_F32 = struct.Struct("f")
_U32 = struct.Struct("I")
CANONICAL_NAN = 0x7FC00000


@dataclass(frozen=True, slots=True)
class CompiledLoop:
    header: int
    exit_pc: int
    length: int          # instrucciones por iteración (incluido el salto de vuelta)
    cost: int            # gas por iteración
    backedge: tuple[int, str, tuple[int, int, int]]
    run: Callable[..., tuple[int, int, int]]


class _Unsupported(Exception):
    pass


def _reg(index: int) -> str:
    if not 0 <= index <= 15:
        raise _Unsupported(f"registro R{index}")
    return "0" if index == 0 else f"r{index}"


def _dest(index: int) -> str | None:
    if not 0 <= index <= 14:
        raise _Unsupported(f"destino R{index}")
    return None if index == 0 else f"r{index}"


class _Emitter:
    """Genera el cuerpo de la función a partir de las instrucciones decodificadas."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def emit(self, line: str) -> None:
        self.lines.append("        " + line)

    def write_int(self, dest: int, expr: str, carry: str = "False", overflow: str = "False") -> None:
        # Equivale a _write_result: normaliza a int32 y fija Z/N (C y O según la operación).
        self.emit(f"v = {expr}")
        target = _dest(dest)
        if target is not None:
            self.emit(f"{target} = v")
        self.emit(f"Z = v == 0; N = v < 0; C = {carry}; O = {overflow}")

    def bail(self, pc: int, done: int, used: int) -> None:
        self.emit(f"steps += {done}; used += {used}; pc = {pc}; break")

    def instruction(self, pc: int, opcode: VMOpcode, a: int, b: int, c: int, done: int, used: int) -> None:
        e = self.emit
        M = "4294967295"
        if opcode == VMOpcode.NOP:
            return
        if opcode in {VMOpcode.MOV}:
            self.write_int(a, _reg(b))
        elif opcode in {VMOpcode.MOVI, VMOpcode.LEA}:
            self.write_int(a, f"s32({b})")
        elif opcode == VMOpcode.LOAD:
            _dest(a)
            e(f"addr = {_reg(b)} + ({c})")
            e("if not (0 <= addr < mem_len):")
            e(f"    steps += {done}; used += {used}; pc = {pc}; break")
            self.write_int(a, "mem[addr]")
        elif opcode == VMOpcode.STORE:
            e(f"addr = {_reg(b)} + ({c})")
            # Código (protegido o no) y fuera de RAM: lo resuelve el intérprete.
            e("if not (code_size <= addr < mem_len):")
            e(f"    steps += {done}; used += {used}; pc = {pc}; break")
            e(f"mem[addr] = {_reg(a)}")
        elif opcode in {VMOpcode.ADD, VMOpcode.ADDI}:
            right = _reg(c) if opcode == VMOpcode.ADD else f"({c})"
            e(f"l = {_reg(b)}; rr = {right}; raw = l + rr; v = s32(raw)")
            e(f"O = (l >= 0) == (rr >= 0) and (v >= 0) != (l >= 0); C = ((l & {M}) + (rr & {M})) > {M}")
            self._assign_flags(a)
        elif opcode in {VMOpcode.SUB, VMOpcode.SUBI}:
            right = _reg(c) if opcode == VMOpcode.SUB else f"({c})"
            e(f"l = {_reg(b)}; rr = {right}; v = s32(l - rr)")
            e(f"O = (l >= 0) != (rr >= 0) and (v >= 0) != (l >= 0); C = (l & {M}) >= (rr & {M})")
            self._assign_flags(a)
        elif opcode in {VMOpcode.MUL, VMOpcode.MULI}:
            right = _reg(c) if opcode == VMOpcode.MUL else f"({c})"
            e(f"raw = {_reg(b)} * {right}")
            self.write_int(a, "s32(raw)", overflow="v != raw")
        elif opcode == VMOpcode.AND:
            self.write_int(a, f"{_reg(b)} & {_reg(c)}")
        elif opcode == VMOpcode.OR:
            self.write_int(a, f"{_reg(b)} | {_reg(c)}")
        elif opcode == VMOpcode.XOR:
            self.write_int(a, f"{_reg(b)} ^ {_reg(c)}")
        elif opcode == VMOpcode.NOT:
            self.write_int(a, f"~{_reg(b)}")
        elif opcode in {VMOpcode.SHL, VMOpcode.SHR}:
            if not 0 <= c <= 31:
                raise _Unsupported("desplazamiento inválido")
            e(f"v0 = {_reg(b)}")
            if opcode == VMOpcode.SHL:
                carry = f"bool({c} and ((v0 & {M}) >> {32 - c}) & 1)" if c else "False"
                e(f"raw = v0 << {c}")
                self.write_int(a, "s32(raw)", carry=carry, overflow="v != raw")
            else:
                carry = f"bool(((v0 & {M}) >> {c - 1}) & 1)" if c else "False"
                self.write_int(a, f"v0 >> {c}", carry=carry)
        elif opcode in {VMOpcode.CMP, VMOpcode.CMPI}:
            right = _reg(b) if opcode == VMOpcode.CMP else f"({b})"
            e(f"l = {_reg(a)}; rr = {right}; v = s32(l - rr)")
            e(f"O = (l >= 0) != (rr >= 0) and (v >= 0) != (l >= 0); C = (l & {M}) >= (rr & {M})")
            e("Z = v == 0; N = v < 0")
        elif opcode == VMOpcode.TEST:
            e(f"v = {_reg(a)} & {_reg(b)}")
            e("Z = v == 0; N = v < 0; C = False; O = False")
        elif opcode in {VMOpcode.FADD, VMOpcode.FSUB, VMOpcode.FMUL}:
            symbol = {VMOpcode.FADD: "+", VMOpcode.FSUB: "-", VMOpcode.FMUL: "*"}[opcode]
            e(f"x = fun(upk({_reg(b)} & {M}))[0]; y = fun(upk({_reg(c)} & {M}))[0]")
            self._write_float(a, f"x {symbol} y")
        elif opcode == VMOpcode.FABS:
            e(f"x = fun(upk({_reg(b)} & {M}))[0]")
            self._write_float(a, "abs(x)")
        elif opcode == VMOpcode.ITOF:
            self._write_float(a, f"float({_reg(b)})")
        else:
            raise _Unsupported(opcode.name)

    def _assign_flags(self, dest: int) -> None:
        target = _dest(dest)
        if target is not None:
            self.emit(f"{target} = v")
        self.emit("Z = v == 0; N = v < 0")

    def _write_float(self, dest: int, expr: str) -> None:
        # Equivale a _float_to_reg: bits del float32 en el registro y banderas del valor guardado.
        e = self.emit
        e(f"value = {expr}")
        target = _dest(dest)
        e("if value != value:")
        if target is not None:
            e(f"    {target} = {CANONICAL_NAN}")
        e("    Z = False; N = False; C = True; O = False")
        e("else:")
        e("    packed = fpk(value); stored = fun(packed)[0]")
        if target is not None:
            e(f"    {target} = s32(uun(packed)[0])")
        e("    Z = stored == 0.0; N = stored < 0.0; C = False; O = stored in (inf, -inf)")


def compile_loop(vm: "TramoyaVM32", header: int) -> CompiledLoop | None:
    """Compila el bucle cuya cabecera es ``header``; None si no es acelerable."""
    program = vm.program
    if program is None or header % 4 or not 0 <= header < program.code_size:
        return None
    body: list[tuple[int, VMOpcode, int, int, int]] = []
    pc = header
    backedge = None
    while True:
        if pc >= program.code_size or len(body) > MAX_BODY_INSTRUCTIONS:
            return None
        spec, a, b, c = vm._fetch_decoded(pc)
        if spec is None:
            return None
        if spec.opcode in _BACKEDGE_CONDITIONS and a == header:
            backedge = (pc, spec.opcode, (a, b, c))
            break
        body.append((pc, spec.opcode, a, b, c))
        pc += 4
    if not body:
        return None

    emitter = _Emitter()
    cost = VM32_ISA[int(backedge[1])].cost
    done = 0
    used = 0
    try:
        for pc, opcode, a, b, c in body:
            emitter.instruction(pc, opcode, a, b, c, done, used)
            done += 1
            used += VM32_ISA[int(opcode)].cost
    except _Unsupported:
        return None
    cost += used
    length = done + 1
    exit_pc = backedge[0] + 4
    condition = _BACKEDGE_CONDITIONS[backedge[1]]

    source = "\n".join([
        "def run(vm, r, f, mem, mem_len, code_size, gas, budget):",
        "    " + "; ".join(f"r{i} = r[{i}]" for i in range(1, 16)),
        "    Z = f['Z']; N = f['N']; C = f['C']; O = f['O']",
        "    steps = 0; used = 0; pc = 0",
        "    while True:",
        f"        if gas - used < {cost} or budget - steps < {length}:",
        f"            pc = {header}; break",
        *emitter.lines,
        f"        steps += {length}; used += {cost}",
        f"        if {condition}:",
        "            continue",
        f"        pc = {exit_pc}; break",
        "    " + "; ".join(f"r[{i}] = r{i}" for i in range(1, 15)),
        "    f['Z'] = Z; f['N'] = N; f['C'] = C; f['O'] = O",
        "    return pc, steps, used",
    ])
    namespace = {
        "s32": _signed32,
        "fpk": _F32.pack,
        "fun": _F32.unpack,
        "upk": _U32.pack,
        "uun": _U32.unpack,
        "inf": float("inf"),
    }
    # El código generado sale del bytecode ya decodificado, no de texto externo.
    exec(compile(source, f"<bucle {header}>", "exec"), namespace)
    return CompiledLoop(header, exit_pc, length, cost, (backedge[0], backedge[1].name, backedge[2]), namespace["run"])


def _signed32(value: int) -> int:
    return ((value + 0x80000000) & 0xFFFFFFFF) - 0x80000000


__all__ = ["CompiledLoop", "MAX_BODY_INSTRUCTIONS", "compile_loop"]
