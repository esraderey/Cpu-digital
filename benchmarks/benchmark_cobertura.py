"""Cobertura y aceleración del acelerador de bucles de VM32 en código de juego.

Para cada carga mide con y sin acelerador (``VMConfig.accelerate_loops``) las
instrucciones, la cobertura (``accelerated_instructions / instructions``), el
tiempo mediano y la aceleración, y comprueba que el estado final es idéntico
(resultado, registros, banderas y ``snapshot_bytes``).

Para el juego (``vm_programs/juego_raycaster.tasm``) añade:

* desglose por fase (rangos de las etiquetas de fase del programa);
* desglose por bucle: instrucciones del bucle más interno que las contiene y
  motivos por los que el acelerador no lo compila;
* estimación de cobertura con extensiones del acelerador, simulando sus reglas
  (calentamiento, espera entre compilaciones, entrada por la cabecera, vueltas
  completas) sobre la traza de PCs del intérprete. La simulación del acelerador
  actual debe reproducir exactamente la cobertura real; si no, el informe lo
  marca.

Uso (desde la raíz del repositorio):
    python benchmarks/benchmark_cobertura.py [--frames 20] [--rondas 3] [--json informe.json]
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import statistics
import struct
import sys
import time
from array import array
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from cpu_digital.vm32 import LOOP_COOLDOWN, LOOP_COOLDOWN_PER_INSTRUCTION, LOOP_WARMUP, TramoyaVM32, VMConfig
from cpu_digital.vm32_assembler import Program32, VM32Assembler
from cpu_digital.vm32_isa import VM32_ISA
from cpu_digital.vm32_isa import VMOpcode as Op
from cpu_digital.vm32_loops import MAX_BODY_INSTRUCTIONS, compile_loop

ROOT = Path(__file__).resolve().parents[1]
GAME = ROOT / "vm_programs" / "juego_raycaster.tasm"
MODELS = ROOT / "models"
PHASES = ("INICIO", "MOVER_JUGADOR", "FISICA", "COLISIONES", "RENDER_MUROS", "RENDER_SPRITES", "MINIMAPA")
GAS = 4 * 10**9

JUMPS = frozenset({Op.JMP, Op.JZ, Op.JNZ, Op.JNEG, Op.JPOS, Op.JC, Op.JNC, Op.JLT, Op.JGT})
# Lo que el acelerador de la rama acelerador-bucles (P2-lite) sabe generar.
P2_LITE = frozenset({
    Op.NOP, Op.MOV, Op.MOVI, Op.LEA, Op.LOAD, Op.STORE, Op.ADD, Op.ADDI, Op.SUB, Op.SUBI, Op.MUL, Op.MULI,
    Op.AND, Op.OR, Op.XOR, Op.NOT, Op.SHL, Op.SHR, Op.CMP, Op.CMPI, Op.TEST,
    Op.FADD, Op.FSUB, Op.FMUL, Op.FABS, Op.ITOF,
})
CATEGORY = {
    Op.DIV: "division", Op.MOD: "division", Op.PUSH: "pila", Op.POP: "pila",
    Op.CALLR: "llamada_indirecta", Op.RET: "retorno", Op.SYSCALL: "syscall",
    Op.FDIV: "fpu", Op.FSQRT: "fpu", Op.FTOI: "fpu", Op.FCMP: "fpu",
    Op.MULH: "arit64", Op.ADDX: "arit64", Op.SUBX: "arit64",
}
REGISTERS = {"none": (), "reg": (0,), "reg_imm": (0,), "reg_target": (0,), "reg_reg": (0, 1),
             "reg_mem": (0, 1), "reg_reg_imm": (0, 1), "reg_reg_reg": (0, 1, 2)}
NO_DESTINATION = frozenset({Op.STORE, Op.CMP, Op.CMPI, Op.TEST, Op.NOP})
# Extensiones acumulativas del acelerador, por riesgo creciente (tras ellas se
# informa la cota de P2: todo salvo syscalls e instrucciones de sistema, también
# fuera de bucles). "vuelta" admite varios saltos a la cabecera: el cuerpo llega
# hasta el último dentro del tope de 64 instrucciones y los anteriores son continue.
_STEPS = (
    ("+ saltos hacia delante", "adelante"),
    ("+ salidas anticipadas", "salida"),
    ("+ DIV/MOD", "division"),
    ("+ PUSH/POP", "pila"),
    ("+ FCMP/FTOI/FDIV/FSQRT", "fpu"),
    ("+ varios saltos de vuelta", "vuelta"),
    ("+ llamadas a hojas", "llamada"),
    ("+ bucles anidados", "anidado"),
    ("+ cuerpos de más de 64", "largo"),
)
EXTENSIONS: tuple[tuple[str, frozenset[str]], ...] = tuple(
    (name, frozenset(category for _, category in _STEPS[:index + 1])) for index, (name, _) in enumerate(_STEPS)
)
SYSTEM_OPCODES = frozenset({Op.SYSCALL, Op.INT, Op.IRET, Op.EI, Op.DI, Op.SETIV, Op.YIELD, Op.BREAK, Op.HALT,
                            Op.SPAWN, Op.SWITCH, Op.FRET})


# ── Cargas ──────────────────────────────────────────────────────────────────


@dataclass
class Workload:
    name: str
    program: Program32
    inputs: tuple[int, ...] = ()
    capabilities: frozenset[str] | None = None
    setup: Callable[[TramoyaVM32], None] | None = None
    npu_factory: Callable[[], object] | None = None
    note: str = ""


def assemble(source: str, name: str) -> Program32:
    return VM32Assembler().assemble(source, name).program


def mac_workload(iterations: int = 20_000) -> Workload:
    source = f""".code
    MOVI R1, 100000
    MOVI R2, 200000
    MOVI R9, {iterations}
L:
    LOAD R3, [R1]
    LOAD R4, [R2]
    FMUL R5, R3, R4
    FADD R6, R6, R5
    ADDI R1, R1, 1
    ADDI R2, R2, 1
    SUBI R9, R9, 1
    JNZ L
    HALT
"""

    def seed(vm: TramoyaVM32) -> None:
        for offset in range(iterations):
            for base, value in ((100000, 0.5 + offset % 7), (200000, 1.25 - offset % 5)):
                vm.write_memory(base + offset, struct.unpack("<i", struct.pack("<f", value))[0])

    return Workload("mac_escalar", assemble(source, "mac.tasm"), setup=seed, note=f"{iterations} vueltas")


def demo_workloads() -> list[Workload]:
    inputs = {"entrada": (7, 8)}
    loads = []
    for path in sorted((ROOT / "vm_programs").glob("*.tasm")):
        if path.stem in {"llama2", GAME.stem}:
            continue
        loads.append(Workload(f"demo:{path.stem}", assemble(path.read_text(encoding="utf-8"), path.name),
                              inputs=inputs.get(path.stem, ())))
    return loads


def llama_workload(tokens: int = 20) -> Workload | None:
    if not (MODELS / "stories260K.bin").exists() or not (MODELS / "tok512.bin").exists():
        return None
    from cpu_digital import tnu_models as tm
    from cpu_digital.tnu import TensorROM, TramoyaNeuralUnit

    rom = TensorROM(tm.build_rom((MODELS / "stories260K.bin").read_bytes(), (MODELS / "tok512.bin").read_bytes()))
    program = assemble((ROOT / "vm_programs" / "llama2.tasm").read_text(encoding="utf-8"), "llama2.tasm")
    return Workload("llama2_260K", program, inputs=(tokens, 0, 1, 0), capabilities=frozenset({"io", "memory", "npu"}),
                    npu_factory=lambda: TramoyaNeuralUnit(rom), note=f"{tokens} tokens, voraz")


def game_workload(path: Path, frames: int) -> Workload:
    return Workload(path.stem, assemble(path.read_text(encoding="utf-8"), path.name), inputs=(frames,),
                    note=f"{frames} frames")


# ── Ejecución y medición ────────────────────────────────────────────────────


def new_vm(load: Workload, accelerate: bool, vm_class: type[TramoyaVM32] = TramoyaVM32) -> TramoyaVM32:
    config = VMConfig(gas_limit=GAS, trace_size=0, accelerate_loops=accelerate,
                      **({"capabilities": load.capabilities} if load.capabilities else {}))
    npu = load.npu_factory() if load.npu_factory else None
    vm = vm_class(config, npu=npu) if npu is not None else vm_class(config)
    vm.load_program(load.program, inputs=load.inputs)
    if load.setup:
        load.setup(vm)
    return vm


def fingerprint(vm: TramoyaVM32) -> tuple:
    result = vm.result()
    return (result, vm.pc, vm.registers, dict(vm.flags), vm.stack, vm.snapshot_bytes())


@dataclass
class Measure:
    name: str
    note: str
    state: str
    instructions: int
    accelerated: int
    seconds_plain: float
    seconds_fast: float
    identical: bool

    @property
    def coverage(self) -> float:
        return self.accelerated / self.instructions if self.instructions else 0.0

    @property
    def speedup(self) -> float:
        return self.seconds_plain / self.seconds_fast if self.seconds_fast else 0.0


def measure(load: Workload, rounds: int) -> Measure:
    times: dict[bool, list[float]] = {False: [], True: []}
    prints: dict[bool, tuple] = {}
    accelerated = 0
    for _ in range(rounds):
        for accelerate in (False, True):  # alternar reparte el ruido entre ambos modos
            vm = new_vm(load, accelerate)
            started = time.perf_counter()
            vm.run()
            times[accelerate].append(time.perf_counter() - started)
            if accelerate not in prints:
                prints[accelerate] = fingerprint(vm)
            if accelerate:
                accelerated = vm.accelerated_instructions
    result = prints[False][0]
    return Measure(load.name, load.note, result.state, result.instructions, accelerated,
                   statistics.median(times[False]), statistics.median(times[True]), prints[False] == prints[True])


# ── Análisis de bucles sobre la traza del intérprete ────────────────────────


class TracingVM(TramoyaVM32):
    """Intérprete que registra el PC de cada instrucción ejecutada."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.pcs = array("l")

    def _execute_one(self) -> None:
        self.pcs.append(self._pc)
        super()._execute_one()


class LoopTimingVM(TramoyaVM32):
    """Acelerador que mide el tiempo pasado dentro de los bucles compilados."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.loop_seconds = 0.0

    def _run_loop(self, loop, budget: int) -> bool:
        started = time.perf_counter()
        try:
            return super()._run_loop(loop, budget)
        finally:
            self.loop_seconds += time.perf_counter() - started


@dataclass
class Shape:
    """Bucle visto desde su cabecera, como lo recorre el acelerador."""

    header: int
    backedge: int
    body: int                               # instrucciones antes del salto de vuelta
    blockers: frozenset[str]
    calls: tuple[int, ...] = ()             # destinos de CALL en el cuerpo
    inner: tuple[int, ...] = ()             # cabeceras de bucles anidados


@dataclass
class Routine:
    start: int
    end: int                                # pc del último RET
    blockers: frozenset[str]
    calls: tuple[int, ...] = ()
    inner: tuple[int, ...] = ()


def decode(program: Program32) -> list[tuple[Op | None, int, int, int]]:
    words = program.words
    decoded = []
    for pc in range(0, program.code_size, 4):
        opcode, a, b, c = words[pc:pc + 4]
        try:
            decoded.append((Op(opcode), a, b, c))
        except ValueError:
            decoded.append((None, a, b, c))
    return decoded


def classify(code, pc: int, low: int, high: int, routine: bool = False) -> tuple[str | None, int | None]:
    """Categoría que impide a P2-lite acelerar la instrucción del tramo [low, high].

    Devuelve (None, None) si P2-lite la genera; con saltos y llamadas, además el destino.
    En una subrutina inlinada (``routine``), un RET es la salida hacia el llamador."""
    opcode, a, b, c = code[pc // 4]
    if opcode is None:
        return "sistema", None
    if opcode in JUMPS:
        if a < low:
            return "atras_externo", None
        if a > high:
            return "salida", None
        if a > pc:
            return "adelante", None
        if a == low and not routine:
            return "vuelta", None
        return "anidado", a
    if opcode == Op.CALL:
        return "llamada", a
    if opcode == Op.RET and routine:
        return None, None
    if opcode in CATEGORY:
        return CATEGORY[opcode], None
    if opcode not in P2_LITE:
        return "sistema", None
    operands = (a, b, c)
    if any(not 0 <= operands[i] <= 15 for i in REGISTERS[VM32_ISA[int(opcode)].form]):
        return "sistema", None
    if opcode not in NO_DESTINATION and a == 15:
        return "r15_destino", None
    if opcode in {Op.SHL, Op.SHR} and not 0 <= c <= 31:
        return "sistema", None
    return None, None


def body_blockers(code, low: int, high: int, end: int, routine: bool = False) -> tuple[set[str], list[int], list[int]]:
    blockers, calls, inner = set(), [], []
    for pc in range(low, end, 4):
        category, target = classify(code, pc, low, high, routine)
        if category is None:
            continue
        blockers.add(category)
        if category == "llamada":
            calls.append(target)
        elif category == "anidado":
            inner.append(target)
    return blockers, calls, inner


def scan_loop(code, header: int, code_size: int, limit: int = 4096, last: bool = False) -> Shape | None:
    """Cuerpo hasta el primer salto a la cabecera (el acelerador) o, con ``last``, hasta el
    último dentro del tope de 64 instrucciones (extensión "vuelta")."""
    pc = header
    while pc < code_size and (pc - header) // 4 < limit:
        opcode, a, _, _ = code[pc // 4]
        if opcode in JUMPS and a == header:
            break
        pc += 4
    else:
        return None
    if last:
        window = range(pc + 4, min(code_size, header + 4 * (MAX_BODY_INSTRUCTIONS + 1)), 4)
        pc = max((jump for jump in window if code[jump // 4][0] in JUMPS and code[jump // 4][1] == header), default=pc)
    blockers, calls, inner = body_blockers(code, header, pc, pc)
    if (pc - header) // 4 > MAX_BODY_INSTRUCTIONS:
        blockers.add("largo")
    if pc == header:
        blockers.add("vacio")
    return Shape(header, pc, (pc - header) // 4, frozenset(blockers), tuple(calls), tuple(sorted(set(inner))))


def scan_routine(code, start: int, code_size: int, limit: int = 512) -> Routine | None:
    """Subrutina inlinable: termina en el primer RET que ningún salto previo hacia delante rebasa."""
    reach = start
    pc = start
    while pc < code_size and (pc - start) // 4 < limit:
        opcode, a, _, _ = code[pc // 4]
        if opcode in JUMPS and a > pc:
            reach = max(reach, a)
        if opcode == Op.RET and pc >= reach:
            break
        pc += 4
    else:
        return None
    blockers, calls, inner = body_blockers(code, start, pc, pc + 4, routine=True)
    return Routine(start, pc, frozenset(blockers), tuple(calls), tuple(sorted(set(inner))))


@dataclass
class LoopAnalysis:
    program: Program32
    trace: array
    code: list = field(init=False)
    shapes: dict[int, Shape | None] = field(default_factory=dict)
    long_shapes: dict[int, Shape | None] = field(default_factory=dict)
    routines: dict[int, Routine | None] = field(default_factory=dict)
    real: dict[int, bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.code = decode(self.program)
        vm = TramoyaVM32(VMConfig(trace_size=0))
        vm.load_program(self.program)
        self._vm = vm

    def shape(self, header: int, last: bool = False) -> Shape | None:
        cache = self.long_shapes if last else self.shapes
        if header not in cache:
            cache[header] = scan_loop(self.code, header, self.program.code_size, last=last)
        return cache[header]

    def compiles_now(self, header: int) -> bool:
        if header not in self.real:
            self.real[header] = compile_loop(self._vm, header) is not None
        return self.real[header]

    def routine(self, start: int) -> Routine | None:
        if start not in self.routines:
            self.routines[start] = scan_routine(self.code, start, self.program.code_size)
        return self.routines[start]

    def region(self, header: int, allowed: frozenset[str] | None, seen: frozenset[int] = frozenset()) -> set[int] | None:
        """PCs que ejecutaría el bucle compilado, o None si no se compilaría.

        ``allowed`` None es el acelerador real (``compile_loop``); si no, el bucle se
        compila en la simulación cuando todos sus bloqueos están en ``allowed`` y sus
        bucles anidados y subrutinas llamadas también se compilan."""
        if allowed is not None and header not in seen:
            shape = self.shape(header, last="vuelta" in allowed)
            if shape is not None and shape.blockers <= allowed:
                region = self._extend(set(range(header, shape.backedge + 4, 4)), shape, allowed, seen | {header})
                if region is not None:
                    return region
        if self.compiles_now(header):
            return set(range(header, self.shape(header).backedge + 4, 4))
        return None

    def _extend(self, pcs: set[int], part: Shape | Routine, allowed: frozenset[str], seen: frozenset[int]) -> set[int] | None:
        for inner in part.inner:
            if self.region(inner, allowed, seen) is None:
                return None
        for target in part.calls:
            routine = self.routine(target)
            if routine is None or target in seen or not routine.blockers <= allowed:
                return None
            callee = self._extend(set(range(target, routine.end + 4, 4)), routine, allowed, seen | {target})
            if callee is None:
                return None
            pcs |= callee
        return pcs

    def simulate(self, allowed: frozenset[str] | None) -> array:
        """Marca (1) cada posición de la traza que ejecutaría el acelerador."""
        trace = self.trace
        code = self.code
        size = len(trace)
        marks = array("b", bytes(size))
        candidates: Counter[int] = Counter()
        regions: dict[int, set[int] | None] = {}
        # Espera entre compilaciones, como TramoyaVM32._compiled_loop: el reloj de pago son las
        # instrucciones ejecutadas (la posición en la traza) más las aceleradas que cuentan doble.
        cooldown_until = bonus = 0
        index = 0
        while index < size:
            pc = trace[index]
            if candidates[pc] >= LOOP_WARMUP:
                if pc not in regions and index + bonus >= cooldown_until:
                    region = regions[pc] = self.region(pc, allowed)
                    cooldown_until = (index + bonus + LOOP_COOLDOWN
                                      + LOOP_COOLDOWN_PER_INSTRUCTION * (len(region) if region is not None else 0))
                region = regions.get(pc)
                if region is not None:
                    end = index
                    while end < size and trace[end] in region:
                        end += 1
                    for position in range(index, end):
                        marks[position] = 1
                    if end - index >= len(region):  # tantas como tiene el bucle: cuentan doble
                        bonus += end - index
                    index = end
                    continue
            # Instrucción interpretada: un salto hacia atrás cuenta para su destino.
            if index + 1 < size:
                following = trace[index + 1]
                opcode = code[pc // 4][0]
                if following < pc + 4 and (opcode in JUMPS or opcode in {Op.CALL, Op.CALLR, Op.RET}):
                    candidates[following] += 1
            index += 1
        return marks

    def innermost(self) -> dict[int, int]:
        """Cabecera del bucle estático más interno que contiene cada PC de código."""
        ranges = []
        for pc in range(0, self.program.code_size, 4):
            opcode, a, _, _ = self.code[pc // 4]
            if opcode in JUMPS and a <= pc:
                ranges.append((a, pc))
        merged: dict[int, int] = {}
        for header, end in ranges:
            merged[header] = max(end, merged.get(header, end))
        owner = {}
        for header, end in sorted(merged.items(), key=lambda item: item[1] - item[0], reverse=True):
            for pc in range(header, end + 4, 4):
                owner[pc] = header
        return owner


def source_labels(source: str, program: Program32) -> dict[int, str]:
    names = {match.group(1).upper() for match in re.finditer(r"^\s*([A-Za-z_][A-Za-z0-9_]*):", source, re.M)}
    labels: dict[int, str] = {}
    for name, value in program.symbols.items():
        if name in names and 0 <= value < program.code_size and value % 4 == 0:
            labels.setdefault(value, name)
    return labels


def label_of(labels: dict[int, str], pc: int) -> str:
    best = max((address for address in labels if address <= pc), default=None)
    if best is None:
        return f"{pc}"
    return labels[best] if best == pc else f"{labels[best]}+{(pc - best) // 4}"


def phase_ranges(program: Program32) -> list[tuple[str, int, int]]:
    starts = [(name, program.symbols[name]) for name in PHASES if name in program.symbols]
    if not starts:
        return [("programa", 0, program.code_size)]
    starts.sort(key=lambda item: item[1])
    ranges = [("principal", 0, starts[0][1])]
    for index, (name, start) in enumerate(starts):
        end = starts[index + 1][1] if index + 1 < len(starts) else program.code_size
        ranges.append((name.lower(), start, end))
    return ranges


def analyze_game(load: Workload, measured: Measure, source: str) -> dict[str, object]:
    vm = new_vm(load, accelerate=False, vm_class=TracingVM)
    vm.run()
    trace = vm.pcs
    total = len(trace)
    analysis = LoopAnalysis(load.program, trace)
    now = analysis.simulate(None)
    simulated_now = sum(now)
    counts = Counter(trace)
    labels = source_labels(source, load.program)

    phases = []
    accelerated_by_pc: Counter[int] = Counter()
    for position, flag in enumerate(now):
        if flag:
            accelerated_by_pc[trace[position]] += 1
    for name, start, end in phase_ranges(load.program):
        executed = sum(n for pc, n in counts.items() if start <= pc < end)
        fast = sum(n for pc, n in accelerated_by_pc.items() if start <= pc < end)
        phases.append({"fase": name, "instrucciones": executed, "aceleradas": fast})

    owner = analysis.innermost()
    by_loop: Counter[int] = Counter()
    fast_by_loop: Counter[int] = Counter()
    for pc, n in counts.items():
        if pc in owner:
            by_loop[owner[pc]] += n
            fast_by_loop[owner[pc]] += accelerated_by_pc[pc]
    loops = []
    for header, executed in by_loop.most_common():
        shape = analysis.shape(header)
        loops.append({
            "cabecera": header, "etiqueta": label_of(labels, header), "instrucciones": executed,
            "aceleradas": fast_by_loop[header], "cuerpo": shape.body if shape else None,
            "compila": analysis.compiles_now(header) if shape else False,
            "bloqueos": sorted(shape.blockers) if shape else ["sin_vuelta"],
        })
    outside = total - sum(by_loop.values())

    estimates = [{"extension": "acelerador actual", "aceleradas": simulated_now}]
    for name, allowed in EXTENSIONS:
        estimates.append({"extension": name, "aceleradas": sum(analysis.simulate(allowed))})
    system = sum(n for pc, n in counts.items() if analysis.code[pc // 4][0] in SYSTEM_OPCODES)
    estimates.append({"extension": "cota P2 (todo salvo syscalls/sistema)", "aceleradas": total - system})

    # Coste por instrucción en este programa, de una ejecución acelerada que mide el tiempo
    # dentro de los bucles compilados (restar dos tiempos totales ruidosos es frágil). Las
    # interpretadas se calibran en esa misma ejecución: incluyen lo que añade run() al
    # buscar bucles, así que la fila del acelerador actual reproduce la medición.
    # Suponer para los bucles nuevos el coste medio de los ya acelerados es optimista.
    per_interpreted = measured.seconds_plain / total
    per_accelerated = None
    if measured.accelerated:
        timed = new_vm(load, accelerate=True, vm_class=LoopTimingVM)
        started = time.perf_counter()
        timed.run()
        elapsed = time.perf_counter() - started
        per_accelerated = timed.loop_seconds / timed.accelerated_instructions
        per_interpreted = (elapsed - timed.loop_seconds) / (total - timed.accelerated_instructions)
    for row in estimates:
        row["cobertura"] = row["aceleradas"] / total
        row["aceleracion_estimada"] = None
        if per_accelerated is not None:
            seconds = (total - row["aceleradas"]) * per_interpreted + row["aceleradas"] * per_accelerated
            row["aceleracion_estimada"] = measured.seconds_plain / seconds

    # Coherencia del clasificador con compile_loop: un bucle sin bloqueos debe compilar;
    # uno que compila con bloqueos de P2-lite indica una extensión ya implementada.
    unlocked = {}
    drift = []
    for header, compiles in analysis.real.items():
        shape = analysis.shape(header)
        if shape is None:
            continue
        if compiles and shape.blockers:
            unlocked[label_of(labels, header)] = sorted(shape.blockers)
        if not compiles and not shape.blockers:
            drift.append(label_of(labels, header))
    return {
        "instrucciones": total,
        "aceleradas_real": measured.accelerated,
        "aceleradas_simuladas": simulated_now,
        "simulacion_exacta": simulated_now == measured.accelerated,
        "fases": phases,
        "bucles": loops,
        "fuera_de_bucles": outside,
        "extensiones": estimates,
        "us_por_instruccion_interpretada": per_interpreted * 1e6,
        "us_por_instruccion_acelerada": per_accelerated * 1e6 if per_accelerated is not None else None,
        "compilan_con_bloqueos_de_p2_lite": unlocked,
        "deriva_del_clasificador": drift,
    }


# ── Informe ─────────────────────────────────────────────────────────────────


def print_measures(rows: list[Measure]) -> None:
    print(f"{'Carga':<22}{'instr.':>12}{'aceleradas':>12}{'cobertura':>11}{'sin acel.':>11}{'con acel.':>11}"
          f"{'aceler.':>9}  idéntico")
    for row in rows:
        print(f"{row.name:<22}{row.instructions:>12,}{row.accelerated:>12,}{row.coverage:>10.1%}"
              f"{row.seconds_plain:>10.3f}s{row.seconds_fast:>10.3f}s{row.speedup:>8.2f}×  "
              f"{'sí' if row.identical else 'NO'}{'  (' + row.note + ')' if row.note else ''}")


def print_game(report: dict[str, object], top: int) -> None:
    total = report["instrucciones"]
    print(f"\nJuego: {total:,} instrucciones; simulación del acelerador actual "
          f"{'EXACTA' if report['simulacion_exacta'] else 'NO COINCIDE'} "
          f"({report['aceleradas_simuladas']:,} frente a {report['aceleradas_real']:,} reales)")
    print(f"\n{'Fase':<18}{'instr.':>12}{'% total':>9}{'aceleradas':>12}{'cobertura':>11}")
    for row in report["fases"]:
        executed = row["instrucciones"]
        print(f"{row['fase']:<18}{executed:>12,}{executed / total:>8.1%}{row['aceleradas']:>12,}"
              f"{(row['aceleradas'] / executed if executed else 0):>10.1%}")
    print(f"\n{'Bucle (más interno)':<28}{'instr.':>11}{'% total':>9}{'acel.':>8}{'cuerpo':>8}  bloqueos")
    for row in report["bucles"][:top]:
        share = row["aceleradas"] / row["instrucciones"] if row["instrucciones"] else 0
        print(f"{row['etiqueta']:<28}{row['instrucciones']:>11,}{row['instrucciones'] / total:>8.1%}"
              f"{share:>7.0%}{row['cuerpo'] if row['cuerpo'] is not None else '-':>8}  "
              f"{', '.join(row['bloqueos']) or '(ninguno)'}")
    print(f"{'(fuera de bucles)':<28}{report['fuera_de_bucles']:>11,}{report['fuera_de_bucles'] / total:>8.1%}")
    print(f"\n{'Extensión (acumulativa, simulada)':<40}{'cobertura':>10}{'aceleración (optimista)':>25}")
    for row in report["extensiones"]:
        speed = row["aceleracion_estimada"]
        print(f"{row['extension']:<40}{row['cobertura']:>9.1%}{(f'{speed:.2f}×' if speed else '—'):>24}")
    fast = report["us_por_instruccion_acelerada"]
    print(f"\nCoste medido: {report['us_por_instruccion_interpretada']:.3f} µs por instrucción interpretada"
          + (f", {fast:.3f} µs por instrucción acelerada" if fast is not None else "")
          + ". La aceleración estimada supone ese coste también para los bucles nuevos.")
    if report["compilan_con_bloqueos_de_p2_lite"]:
        print("Compilan gracias a extensiones ya implementadas:", report["compilan_con_bloqueos_de_p2_lite"])
    if report["deriva_del_clasificador"]:
        print("AVISO: sin bloqueos según el clasificador pero compile_loop los rechaza:",
              report["deriva_del_clasificador"])


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(description="Cobertura del acelerador de bucles de VM32")
    parser.add_argument("--programa", type=Path, default=GAME, help="Juego .tasm (entrada: número de frames)")
    parser.add_argument("--frames", type=int, default=20, help="Frames del juego (entrada del programa)")
    parser.add_argument("--rondas", type=int, default=3, help="Ejecuciones por modo; se usa la mediana")
    parser.add_argument("--bucles", type=int, default=15, help="Bucles a listar en el desglose")
    parser.add_argument("--sin-referencias", action="store_true", help="Solo el juego")
    parser.add_argument("--json", type=Path, help="Guarda el informe completo en JSON")
    args = parser.parse_args()
    if args.frames < 1 or args.rondas < 1:
        parser.error("frames y rondas deben ser positivos")

    game = game_workload(args.programa, args.frames)
    loads = [game]
    if not args.sin_referencias:
        loads.append(mac_workload())
        loads.extend(demo_workloads())
        llama = llama_workload()
        if llama is not None:
            loads.append(llama)
    print(f"Python {sys.version.split()[0]} · {platform.system()} {platform.machine()} · "
          f"LOOP_WARMUP={LOOP_WARMUP} · espera={LOOP_COOLDOWN}+{LOOP_COOLDOWN_PER_INSTRUCTION}·L · "
          f"rondas={args.rondas}\n")
    rows = [measure(load, args.rondas) for load in loads]
    print_measures(rows)
    report = analyze_game(game, rows[0], args.programa.read_text(encoding="utf-8"))
    print_game(report, args.bucles)
    if args.json:
        payload = {
            "python": sys.version.split()[0],
            "plataforma": f"{platform.system()} {platform.machine()}",
            "loop_warmup": LOOP_WARMUP,
            "loop_cooldown": LOOP_COOLDOWN,
            "loop_cooldown_per_instruction": LOOP_COOLDOWN_PER_INSTRUCTION,
            "programa": args.programa.name,
            "frames": args.frames,
            "cargas": [{**row.__dict__, "cobertura": row.coverage, "aceleracion": row.speedup} for row in rows],
            "juego": report,
        }
        args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nInforme: {args.json}")
    return 0 if all(row.identical for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
