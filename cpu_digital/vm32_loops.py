"""Acelerador de bucles de VM32: avance rápido con semántica exacta.

Un bucle interno se compila una vez a una función Python que ejecuta sus
iteraciones sin la sobrecarga por instrucción del intérprete: sin checkpoint,
sin dispatch, sin traza. Su cuerpo va de la cabecera al primer salto que vuelve
a ella y puede contener saltos hacia delante, dentro del cuerpo (``if``/``else``,
``continue``) o fuera de él (salidas anticipadas). Llamadas, syscalls, saltos
hacia atrás distintos del de vuelta (bucles anidados) y las instrucciones no
soportadas lo dejan en el intérprete. El contrato es que el estado observable
(registros, banderas, memoria, pila, pc, gas, ciclos, contador de instrucciones
y última instrucción) sea **idéntico** al que dejaría el intérprete,
instrucción a instrucción:

* Cada instrucción se genera con la misma aritmética, los mismos redondeos a
  float32 y las mismas banderas que su manejador en ``TramoyaVM32``.
* El gas y el presupuesto de instrucciones se comprueban por iteración contra la
  vuelta más larga (la que no toma ningún salto hacia delante); si no alcanzan,
  la función devuelve el control en la cabecera y el intérprete agota el gas en
  la instrucción exacta.
* Antes de cada acceso a memoria, ``DIV``/``MOD``, ``FDIV``, ``FSQRT``, ``FTOI``,
  ``PUSH`` o ``POP`` se comprueba que no pueda fallar (rango, código, ROM,
  divisor cero, raíz negativa, conversión imposible, límites de la pila). Si
  pudiera, la función devuelve el control en esa instrucción con el estado
  parcial ya escrito, y el intérprete la ejecuta con su validación, su mensaje y
  su rollback. Ninguna instrucción se ejecuta dos veces y ninguna a medias.
* Un salto hacia delante tomado dentro del cuerpo fija la posición desde la que
  se sigue ejecutando (``skip``) y descuenta de la vuelta las instrucciones y el
  gas que salta; los bloques anteriores a ``skip`` no se ejecutan.

Solo se acelera cuando no hay traza, breakpoints ni interrupciones pendientes.
El resultado se verifica por fuzzing diferencial contra el intérprete
(``tests/test_vm32_loops.py``).
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from .vm32_isa import VM32_ISA, VMOpcode

if TYPE_CHECKING:  # pragma: no cover
    from .vm32 import TramoyaVM32

MAX_BODY_INSTRUCTIONS = 64

_JUMP_CONDITIONS = {
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

Jump = tuple[int, str, tuple[int, int, int]]  # pc, mnemónico, operandos


@dataclass(frozen=True, slots=True)
class CompiledLoop:
    header: int
    exit_pc: int
    length: int          # instrucciones de la vuelta sin saltos tomados (incluido el salto de vuelta)
    cost: int            # gas de esa vuelta
    backedge: Jump
    run: Callable[..., tuple[int, int, int, int]]
    exits: tuple[Jump, ...] = ()  # saltos que salen del bucle; run() devuelve 1 + su índice si fue el último


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

    def __init__(self, branches: bool) -> None:
        self.lines: list[str] = []
        self.indent = "        "
        # Con saltos internos, sn/sc acumulan las instrucciones y el gas saltados en la vuelta.
        self.branches = branches
        self.uses_stack = False

    def emit(self, line: str) -> None:
        self.lines.append(self.indent + line)

    def account(self, done: int, used: int) -> str:
        if self.branches:
            return f"steps += {done} - sn; used += {used} - sc"
        return f"steps += {done}; used += {used}"

    def guard(self, condition: str, pc: int, done: int, used: int) -> None:
        # Si la instrucción podría fallar, la ejecuta el intérprete.
        self.emit(f"if {condition}:")
        self.emit(f"    {self.account(done, used)}; pc = {pc}; break")

    def write_int(self, dest: int, expr: str, carry: str = "False", overflow: str = "False") -> None:
        # Equivale a _write_result: normaliza a int32 y fija Z/N (C y O según la operación).
        self.emit(f"v = {expr}")
        target = _dest(dest)
        if target is not None:
            self.emit(f"{target} = v")
        self.emit(f"Z = v == 0; N = v < 0; C = {carry}; O = {overflow}")

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
            self.guard("not (0 <= addr < mem_len)", pc, done, used)
            self.write_int(a, "mem[addr]")
        elif opcode == VMOpcode.STORE:
            e(f"addr = {_reg(b)} + ({c})")
            # Código (protegido o no) y fuera de RAM: lo resuelve el intérprete.
            self.guard("not (code_size <= addr < mem_len)", pc, done, used)
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
        elif opcode in {VMOpcode.DIV, VMOpcode.MOD}:
            # Equivale a _op_div/_op_mod con _quotient: trunca hacia cero; el divisor cero falla.
            _dest(a)
            e(f"l = {_reg(b)}; d = {_reg(c)}")
            self.guard("d == 0", pc, done, used)
            e("q = abs(l) // abs(d)")
            e("if (l < 0) != (d < 0):")
            e("    q = -q")
            e("raw = q" if opcode == VMOpcode.DIV else "raw = l - q * d")
            self.write_int(a, "s32(raw)", overflow="v != raw")
        elif opcode == VMOpcode.PUSH:
            # Equivale a _push: límite de pila y R15 = profundidad.
            self.uses_stack = True
            self.guard("len(stack) >= limit", pc, done, used)
            e(f"stack.append({_reg(a)}); r15 = len(stack)")
        elif opcode == VMOpcode.POP:
            self.uses_stack = True
            _dest(a)
            self.guard("not stack", pc, done, used)
            e("value = stack.pop(); r15 = len(stack)")
            self.write_int(a, "value")
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
        elif opcode == VMOpcode.FDIV:
            _dest(a)
            e(f"y = fun(upk({_reg(c)} & {M}))[0]")
            self.guard("y == 0.0", pc, done, used)
            e(f"x = fun(upk({_reg(b)} & {M}))[0]")
            self._write_float(a, "x / y")
        elif opcode == VMOpcode.FSQRT:
            _dest(a)
            e(f"x = fun(upk({_reg(b)} & {M}))[0]")
            self.guard("x < 0.0", pc, done, used)
            self._write_float(a, "sqrt(x)")
        elif opcode == VMOpcode.FTOI:
            # Equivale a _op_ftoi: NaN, infinito o fuera de int32 fallan en el intérprete.
            _dest(a)
            e(f"x = fun(upk({_reg(b)} & {M}))[0]")
            self.guard("x != x or x in (inf, -inf)", pc, done, used)
            e("raw = int(x)")
            self.guard("not -2147483648 <= raw <= 2147483647", pc, done, used)
            self.write_int(a, "raw")
        elif opcode == VMOpcode.FCMP:
            # Equivale a _op_fcmp: compara sin restar; con algún NaN, solo C.
            e(f"x = fun(upk({_reg(a)} & {M}))[0]; y = fun(upk({_reg(b)} & {M}))[0]")
            e("if x != x or y != y:")
            e("    Z = False; N = False; C = True; O = False")
            e("else:")
            e("    Z = x == y; N = x < y; C = False; O = False")
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
        # Como _float_to_reg: si el host lanza al desbordar float32, se redondea a infinito.
        e("    try:")
        e("        packed = fpk(value)")
        e("    except OverflowError:")
        e("        packed = fpk(inf if value > 0.0 else -inf)")
        e("    stored = fun(packed)[0]")
        if target is not None:
            e(f"    {target} = s32(uun(packed)[0])")
        e("    Z = stored == 0.0; N = stored < 0.0; C = False; O = stored in (inf, -inf)")


def compile_loop(vm: "TramoyaVM32", header: int) -> CompiledLoop | None:
    """Compila el bucle cuya cabecera es ``header``; None si no es acelerable."""
    program = vm.program
    if program is None or header % 4 or not 0 <= header < program.code_size:
        return None
    code_size = program.code_size
    body: list[tuple[int, VMOpcode, int, int, int]] = []
    pc = header
    while True:
        if pc >= code_size or len(body) > MAX_BODY_INSTRUCTIONS:
            return None
        spec, a, b, c = vm._fetch_decoded(pc)
        if spec is None:
            return None
        if spec.opcode in _JUMP_CONDITIONS:
            if a == header:
                break
            # Solo saltos hacia delante a una instrucción válida; hacia atrás sería un bucle anidado.
            if not (pc < a < code_size and a % 4 == 0):
                return None
        body.append((pc, spec.opcode, a, b, c))
        pc += 4
    if not body:
        return None
    backedge_pc, backedge_op = pc, spec.opcode
    backedge: Jump = (backedge_pc, spec.mnemonic, (a, b, c))

    n = len(body)
    prefix = [0]  # gas de las primeras i instrucciones del cuerpo
    for _, opcode, *_ in body:
        prefix.append(prefix[-1] + VM32_ISA[int(opcode)].cost)
    cost = prefix[n] + VM32_ISA[int(backedge_op)].cost
    length = n + 1
    exit_pc = backedge_pc + 4

    # Saltos internos: posición del salto -> posición de destino (n es el salto de vuelta).
    internal = {i: (a - header) // 4 for i, (_, opcode, a, _, _) in enumerate(body)
                if opcode in _JUMP_CONDITIONS and a <= backedge_pc}
    # Bloques: empiezan en cada destino y tras cada salto interno; se protegen con
    # `if skip <= inicio` cuando algún salto anterior puede saltarlos.
    starts = sorted({0} | {t for t in internal.values() if t < n} | {i + 1 for i in internal if i + 1 < n})
    emitter = _Emitter(branches=bool(internal))
    exits: list[Jump] = []
    try:
        for block, start in enumerate(starts):
            end = starts[block + 1] if block + 1 < len(starts) else n
            guarded = any(i < start < t for i, t in internal.items())
            if guarded:
                emitter.emit(f"if skip <= {start}:")
                emitter.indent += "    "
                emitted = len(emitter.lines)
            for index in range(start, end):
                pc, opcode, a, b, c = body[index]
                condition = _JUMP_CONDITIONS.get(opcode)
                if condition is None:
                    emitter.instruction(pc, opcode, a, b, c, index, prefix[index])
                    continue
                head = "" if opcode == VMOpcode.JMP else f"if {condition}: "
                if index in internal:
                    target = internal[index]
                    emitter.emit(f"{head}skip = {target}; sn += {target - index - 1}; "
                                 f"sc += {prefix[target] - prefix[index + 1]}")
                else:
                    exits.append((pc, VM32_ISA[int(opcode)].mnemonic, (a, b, c)))
                    emitter.emit(f"{head}{emitter.account(index + 1, prefix[index + 1])}; "
                                 f"pc = {a}; last = {len(exits)}; break")
            if guarded:
                if len(emitter.lines) == emitted:
                    emitter.emit("pass")
                emitter.indent = emitter.indent[:-4]
    except _Unsupported:
        return None
    condition = _JUMP_CONDITIONS[backedge_op]

    prologue = ["    last = 0"]
    epilogue = ["    " + "; ".join(f"r[{i}] = r{i}" for i in range(1, 15))]
    if emitter.uses_stack:
        prologue.append("    stack = vm._stack; limit = vm.config.stack_limit")
        epilogue.append("    r[15] = r15")
    source = "\n".join([
        "def run(vm, r, f, mem, mem_len, code_size, gas, budget):",
        "    " + "; ".join(f"r{i} = r[{i}]" for i in range(1, 16)),
        "    Z = f['Z']; N = f['N']; C = f['C']; O = f['O']",
        "    steps = 0; used = 0; pc = 0",
        *prologue,
        "    while True:",
        f"        if gas - used < {cost} or budget - steps < {length}:",
        f"            pc = {header}; break",
        *(["        skip = 0; sn = 0; sc = 0"] if internal else []),
        *emitter.lines,
        f"        {emitter.account(length, cost)}",
        f"        if {condition}:",
        "            continue",
        f"        pc = {exit_pc}; break",
        *epilogue,
        "    f['Z'] = Z; f['N'] = N; f['C'] = C; f['O'] = O",
        "    return pc, steps, used, last",
    ])
    namespace = {
        "s32": _signed32,
        "fpk": _F32.pack,
        "fun": _F32.unpack,
        "upk": _U32.pack,
        "uun": _U32.unpack,
        "inf": float("inf"),
        "sqrt": math.sqrt,
    }
    # El código generado sale del bytecode ya decodificado, no de texto externo.
    exec(compile(source, f"<bucle {header}>", "exec"), namespace)
    return CompiledLoop(header, exit_pc, length, cost, backedge, namespace["run"], tuple(exits))


def _signed32(value: int) -> int:
    return ((value + 0x80000000) & 0xFFFFFFFF) - 0x80000000


__all__ = ["CompiledLoop", "MAX_BODY_INSTRUCTIONS", "compile_loop"]
