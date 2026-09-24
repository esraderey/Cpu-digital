"""Tramoya VM32: máquina virtual RISC de 32 bits, segura y embebible."""

from __future__ import annotations

import json
import math
import struct
import zlib
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from tramoya import Machine, MachineBuilder, MachineError

from .memory import PagedMemory
from .memory_chip import NonVolatileMemoryChip
from .vm32_assembler import Program32, signed32
from .vm32_isa import VM32_ISA, VMInstruction, VMOpcode


SNAPSHOT_MAGIC = b"TVMS32\x01"
SNAPSHOT_VERSION = 2
DEFAULT_MEMORY_WORDS = 1_048_576
MAX_MEMORY_WORDS = 16_777_216
MAX_SNAPSHOT_RAW_BYTES = 256 * 1024 * 1024
MAX_MEMORY_CHIP_TRANSFER_BYTES = 4_096
RUNNABLE_STATES = frozenset({"READY", "RUNNING", "PAUSED", "WAITING"})
FINAL_STATES = frozenset({"HALTED", "FAULTED"})


class VMRuntimeError(RuntimeError):
    pass


class _ExecutionFault(Exception):
    pass


class _HaltSignal(Exception):
    def __init__(self, code: int):
        self.code = code


class _WaitSignal(Exception):
    pass


class _PauseSignal(Exception):
    pass


@dataclass(slots=True)
class _FiberContext:
    """Contexto de una fibra cooperativa."""
    fiber_id: int
    pc: int
    registers: list[int]
    flags: dict[str, bool]
    stack: list[int]
    state: str  # "READY", "RUNNING", "FINISHED"
    parent_id: int


@dataclass(frozen=True, slots=True)
class VMConfig:
    memory_words: int = DEFAULT_MEMORY_WORDS
    gas_limit: int = 1_000_000
    stack_limit: int = 8_192
    interrupt_depth: int = 32
    trace_size: int = 4_096
    output_limit: int = 1_000_000
    fiber_limit: int = 64
    protect_code: bool = True
    capabilities: frozenset[str] = frozenset({"io", "introspection", "random", "memory"})

    def __post_init__(self) -> None:
        if not 256 <= self.memory_words <= MAX_MEMORY_WORDS:
            raise ValueError(f"memory_words debe estar entre 256 y {MAX_MEMORY_WORDS}")
        if (
            self.gas_limit < 1
            or self.stack_limit < 1
            or self.interrupt_depth < 1
            or self.trace_size < 0
            or self.output_limit < 1
            or self.fiber_limit < 1
        ):
            raise ValueError("Los límites de recursos no son válidos")


@dataclass(frozen=True, slots=True)
class VMTraceEntry:
    sequence: int
    pc: int
    opcode: str
    operands: tuple[int, int, int]
    cycles: int
    gas_remaining: int
    registers: tuple[int, ...]
    flags: tuple[bool, bool, bool, bool]
    detail: str

    def format(self) -> str:
        z, n, c, o = self.flags
        a, b, c_arg = self.operands
        return (
            f"#{self.sequence:05d} PC={self.pc:08X} {self.opcode:<8} "
            f"{a:>11},{b:>11},{c_arg:>11} | "
            f"R1={self.registers[1]:>11} R2={self.registers[2]:>11} "
            f"Z={int(z)} N={int(n)} C={int(c)} O={int(o)} "
            f"GAS={self.gas_remaining} | {self.detail}"
        )


@dataclass(frozen=True, slots=True)
class VMResult:
    state: str
    exit_code: int | None
    fault: str | None
    wait_reason: str | None
    pause_reason: str | None
    instructions: int
    cycles: int
    gas_remaining: int
    output: tuple[int | str, ...]

    @property
    def ok(self) -> bool:
        return self.state == "HALTED" and self.exit_code == 0


@dataclass(frozen=True, slots=True)
class _Syscall:
    number: int
    name: str
    handler: Callable[["TramoyaVM32"], None]
    capability: str | None


class TramoyaVM32:
    """VM determinista para scripts, automatización y lógica embebida.

    R0 siempre vale cero. R1..R14 son registros generales. R15 expone la
    profundidad de la pila y es de solo lectura.
    """

    def __init__(
        self,
        config: VMConfig | None = None,
        *,
        memory_chip: NonVolatileMemoryChip | None = None,
    ):
        self.config = config or VMConfig()
        self.memory_chip = memory_chip
        self._trace: deque[VMTraceEntry] = deque(maxlen=self.config.trace_size)
        self._syscalls: dict[int, _Syscall] = {}
        self._program: Program32 | None = None
        self._pending_event: tuple[str, str | int | None] | None = None
        self._journal: dict[int, int] | None = None
        self._initialize_core()
        self.machine = self._build_lifecycle()
        self._install_builtin_syscalls()

    @property
    def state(self) -> str:
        return self.machine.state

    @property
    def pc(self) -> int:
        return self._pc

    @property
    def registers(self) -> tuple[int, ...]:
        self._sync_special_registers()
        return tuple(self._registers)

    @property
    def flags(self) -> Mapping[str, bool]:
        return dict(self._flags)

    @property
    def stack(self) -> tuple[int, ...]:
        return tuple(self._stack)

    @property
    def output(self) -> tuple[int | str, ...]:
        return tuple(self._output)

    @property
    def trace(self) -> tuple[VMTraceEntry, ...]:
        return tuple(self._trace)

    @property
    def program(self) -> Program32 | None:
        return self._program

    @property
    def allocated_memory_words(self) -> int:
        """Palabras físicas reservadas por la memoria paginada."""
        return self._memory.allocated_words

    @property
    def allocated_memory_bytes(self) -> int:
        return self._memory.allocated_bytes

    @property
    def allocated_memory_pages(self) -> int:
        return self._memory.allocated_pages

    def attach_memory_chip(self, chip: NonVolatileMemoryChip | None) -> None:
        """Conecta o desconecta un chip no volátil controlado por el host."""
        self.memory_chip = chip

    def _initialize_core(self) -> None:
        self._memory = PagedMemory(self.config.memory_words)
        self._registers = [0] * 16
        self._flags = {"Z": True, "N": False, "C": False, "O": False}
        self._pc = 0
        self._stack: list[int] = []
        self._input: deque[int] = deque()
        self._output: list[int | str] = []
        self._output_units = 0
        self._interrupt_vectors: dict[int, int] = {}
        self._pending_interrupts: deque[int] = deque()
        self._interrupt_stack: list[tuple[int, dict[str, bool], bool]] = []
        self._interrupts_enabled = True
        self._heap_ptr = 0
        self._random_state = 0x6D2B79F5
        self._instructions = 0
        self._cycles = 0
        self._gas_remaining = self.config.gas_limit
        self._trace.clear()
        self._trace_sequence = 0
        self._pending_event = None
        self._decode_cache: list[tuple[VMInstruction | None, int, int, int] | None] = []
        self._fibers: dict[int, _FiberContext] = {}
        self._current_fiber: int = 0
        self._next_fiber_id: int = 1
        self._sync_special_registers()

    def _fresh_lifecycle_context(self) -> dict[str, Any]:
        return {
            "program": None,
            "instructions": 0,
            "cycles": 0,
            "gas_remaining": self.config.gas_limit,
            "fault": None,
            "exit_code": None,
            "wait_reason": None,
            "pause_reason": None,
            "last_instruction": None,
            "last_transition": None,
        }

    def _build_lifecycle(self) -> Machine:
        builder = MachineBuilder("CREATED")
        builder.add_states("CREATED", "READY", "RUNNING", "PAUSED", "WAITING", "HALTED", "FAULTED")
        builder.transition("load", "*", "READY")
        builder.transition("start", "READY", "RUNNING")
        builder.transition("tick", "RUNNING", None)
        builder.transition("pause", "RUNNING", "PAUSED")
        builder.transition("wait", "RUNNING", "WAITING")
        builder.transition("halt", "RUNNING", "HALTED")
        builder.transition("fault", "*", "FAULTED")
        builder.transition("resume", "PAUSED", "RUNNING")
        builder.transition("wake", "WAITING", "RUNNING")

        @builder.guard("load", "*", "READY")
        def load_authorized(ctx: Mapping[str, Any]) -> bool:
            return bool(ctx.get("_load_authorized"))

        @builder.on("load", "*", "READY")
        def loaded(ctx: dict[str, Any]) -> None:
            ctx.pop("_load_authorized", None)
            ctx.update(
                instructions=0,
                cycles=0,
                gas_remaining=self._gas_remaining,
                fault=None,
                exit_code=None,
                wait_reason=None,
                pause_reason=None,
                last_instruction="Programa cargado",
            )

        @builder.on("tick", "RUNNING", None)
        def tick(ctx: dict[str, Any]) -> None:
            self._execute_one()
            self._sync_lifecycle(ctx)

        @builder.on("pause", "RUNNING", "PAUSED")
        def paused(ctx: dict[str, Any]) -> None:
            ctx["pause_reason"] = ctx.pop("_pause_reason", None) or "Pausa solicitada"

        @builder.on("wait", "RUNNING", "WAITING")
        def waiting(ctx: dict[str, Any]) -> None:
            ctx["wait_reason"] = ctx.pop("_wait_reason", None) or "Esperando evento"

        @builder.on("halt", "RUNNING", "HALTED")
        def halted(ctx: dict[str, Any]) -> None:
            ctx["exit_code"] = signed32(int(ctx.pop("_exit_code", 0)))
            ctx["wait_reason"] = None
            ctx["pause_reason"] = None

        @builder.guard("fault", "*", "FAULTED")
        def fault_authorized(ctx: Mapping[str, Any]) -> bool:
            return bool(ctx.get("_fault_authorized"))

        @builder.on("fault", "*", "FAULTED")
        def faulted(ctx: dict[str, Any]) -> None:
            ctx.pop("_fault_authorized", None)
            ctx["fault"] = ctx.pop("_fault_message", None) or "Fallo no especificado"
            ctx["exit_code"] = None

        @builder.on("resume", "PAUSED", "RUNNING")
        def resumed(ctx: dict[str, Any]) -> None:
            ctx["pause_reason"] = None

        @builder.on("wake", "WAITING", "RUNNING")
        def woke(ctx: dict[str, Any]) -> None:
            ctx["wait_reason"] = None

        @builder.on_transition_handler
        def transition_log(trigger: str, source: str, destination: str, ctx: dict[str, Any]) -> None:
            ctx["last_transition"] = {"trigger": trigger, "source": source, "destination": destination}

        # El contexto de ciclo de vida solo reemplaza valores de primer nivel;
        # shallow_ctx evita copias profundas por instrucción en el hot path.
        return builder.build(
            ctx=self._fresh_lifecycle_context(),
            history_size=128,
            shallow_ctx=True,
        )

    def _sync_lifecycle(self, ctx: dict[str, Any] | None = None) -> None:
        target = ctx if ctx is not None else self.machine.ctx
        target["instructions"] = self._instructions
        target["cycles"] = self._cycles
        target["gas_remaining"] = self._gas_remaining

    def load_program(self, program: Program32, inputs: Iterable[int] = ()) -> None:
        if len(program.words) > self.config.memory_words:
            raise ValueError(
                f"El programa usa {len(program.words)} palabras; la VM tiene {self.config.memory_words}"
            )
        self._initialize_core()
        self._program = program
        self._memory.write_block(0, program.words)
        self._build_decode_cache()
        self._pc = program.entry
        self._heap_ptr = len(program.words)
        self._input = deque(signed32(int(value)) for value in inputs)
        self.machine.trigger(
            "load",
            _load_authorized=True,
            program=program.source_name,
        )

    def reload(self, inputs: Iterable[int] = ()) -> None:
        if self._program is None:
            raise VMRuntimeError("No hay programa cargado")
        self.load_program(self._program, inputs=inputs)

    def _execute_one(self) -> None:
        if self._program is None:
            self._pending_event = ("fault", "No hay programa cargado")
            return

        pc_before = self._pc
        opcode_name = "?"
        operands = (0, 0, 0)
        cost = 0
        detail = ""
        snapshot = self._core_checkpoint()
        self._journal = {}

        try:
            if self._interrupts_enabled and self._pending_interrupts:
                if self._gas_remaining < 1:
                    raise _ExecutionFault("Gas agotado al despachar interrupción")
                vector = self._pending_interrupts.popleft()
                self._enter_interrupt(vector)
                self._cycles += 1
                self._gas_remaining -= 1
                opcode_name = "INT_ENTRY"
                operands = (vector, self._pc, 0)
                detail = f"Interrupción {vector} → {self._pc}"
                return

            self._validate_executable(self._pc)
            spec, a, b, c = self._fetch_decoded(self._pc)
            operands = (a, b, c)
            if spec is None:
                opcode = self._memory[self._pc]
                raise _ExecutionFault(f"Opcode VM32 desconocido: {opcode}")
            opcode_name = spec.mnemonic
            cost = spec.cost
            if self._gas_remaining < cost:
                raise _ExecutionFault(f"Gas agotado: {spec.mnemonic} requiere {cost}")

            self._pc += 4
            self._dispatch(spec.opcode, a, b, c)
            self._instructions += 1
            self._cycles += cost
            self._gas_remaining -= cost
            detail = "OK"
        except _HaltSignal as signal:
            self._instructions += 1
            self._cycles += cost
            self._gas_remaining -= cost
            self._pending_event = ("halt", signal.code)
            detail = f"HALT {signal.code}"
        except _WaitSignal as signal:
            self._instructions += 1
            self._cycles += cost
            self._gas_remaining -= cost
            self._pending_event = ("wait", str(signal))
            detail = str(signal)
        except _PauseSignal as signal:
            self._instructions += 1
            self._cycles += cost
            self._gas_remaining -= cost
            self._pending_event = ("pause", str(signal))
            detail = str(signal)
        except _ExecutionFault as exc:
            self._rollback_core(snapshot)
            self._pending_event = ("fault", str(exc))
            detail = f"FAULT: {exc}"
        except Exception as exc:
            self._rollback_core(snapshot)
            self._pending_event = ("fault", f"Excepción de host {type(exc).__name__}: {exc}")
            detail = f"FAULT HOST: {exc}"
        finally:
            self._journal = None
            self.machine.ctx["last_instruction"] = {
                "pc": pc_before,
                "opcode": opcode_name,
                "operands": list(operands),
                "detail": detail,
            }
            if self.config.trace_size:
                self._trace_sequence += 1
                self._trace.append(
                    VMTraceEntry(
                        sequence=self._trace_sequence,
                        pc=pc_before,
                        opcode=opcode_name,
                        operands=operands,
                        cycles=self._cycles,
                        gas_remaining=self._gas_remaining,
                        registers=tuple(self._registers),
                        flags=(self._flags["Z"], self._flags["N"], self._flags["C"], self._flags["O"]),
                        detail=detail,
                    )
                )

    def _core_checkpoint(self) -> tuple[Any, ...]:
        # El checkpoint se crea en cada instrucción; una tupla compacta y None
        # para colecciones vacías reduce asignaciones sin perder atomicidad.
        return (
            self._pc,
            self._registers.copy(),
            (self._flags["Z"], self._flags["N"], self._flags["C"], self._flags["O"]),
            self._stack.copy() if self._stack else None,
            tuple(self._input) if self._input else None,
            len(self._output),
            self._output_units,
            tuple(self._pending_interrupts) if self._pending_interrupts else None,
            self._interrupt_vectors.copy() if self._interrupt_vectors else None,
            [(pc, flags.copy(), enabled) for pc, flags, enabled in self._interrupt_stack]
            if self._interrupt_stack
            else None,
            self._interrupts_enabled,
            self._heap_ptr,
            self._random_state,
            self._instructions,
            self._cycles,
            self._gas_remaining,
            self._current_fiber,
            self._next_fiber_id,
            {fid: _FiberContext(f.fiber_id, f.pc, f.registers.copy(), dict(f.flags),
                                f.stack.copy(), f.state, f.parent_id)
             for fid, f in self._fibers.items()} if self._fibers else None,
        )

    def _rollback_core(self, snapshot: tuple[Any, ...]) -> None:
        if self._journal:
            for address, old_value in self._journal.items():
                self._memory[address] = old_value
        (
            self._pc,
            self._registers,
            flags,
            stack,
            input_values,
            output_length,
            self._output_units,
            pending_interrupts,
            interrupt_vectors,
            interrupt_stack,
            self._interrupts_enabled,
            self._heap_ptr,
            self._random_state,
            self._instructions,
            self._cycles,
            self._gas_remaining,
            current_fiber,
            next_fiber_id,
            fibers_snapshot,
        ) = snapshot
        self._flags = dict(zip(("Z", "N", "C", "O"), flags, strict=True))
        self._stack = [] if stack is None else stack
        self._input = deque(() if input_values is None else input_values)
        del self._output[output_length:]
        self._pending_interrupts = deque(() if pending_interrupts is None else pending_interrupts)
        self._interrupt_vectors = {} if interrupt_vectors is None else interrupt_vectors
        self._interrupt_stack = [] if interrupt_stack is None else interrupt_stack
        self._current_fiber = current_fiber
        self._next_fiber_id = next_fiber_id
        self._fibers = {} if fibers_snapshot is None else fibers_snapshot
        self._sync_special_registers()

    def _build_decode_cache(self) -> None:
        if self._program is None:
            self._decode_cache = []
            return
        self._decode_cache = []
        for pc in range(0, self._program.code_size, 4):
            opcode, a, b, c = self._memory.read_block(pc, 4)
            self._decode_cache.append((VM32_ISA.get(opcode), a, b, c))

    def _fetch_decoded(self, pc: int) -> tuple[VMInstruction | None, int, int, int]:
        cache_index = pc // 4
        cached = self._decode_cache[cache_index]
        if cached is None:
            opcode, a, b, c = self._memory.read_block(pc, 4)
            cached = (VM32_ISA.get(opcode), a, b, c)
            self._decode_cache[cache_index] = cached
        return cached

    def _dispatch(self, opcode: VMOpcode, a: int, b: int, c: int) -> None:
        if opcode == VMOpcode.NOP:
            return
        if opcode == VMOpcode.MOV:
            self._write_result(a, self._read_register(b))
        elif opcode == VMOpcode.MOVI:
            self._write_result(a, b)
        elif opcode == VMOpcode.LEA:
            self._write_result(a, b)
        elif opcode == VMOpcode.LOAD:
            self._write_result(a, self.read_memory(self._effective_address(b, c)))
        elif opcode == VMOpcode.STORE:
            self.write_memory(self._effective_address(b, c), self._read_register(a))
        elif opcode == VMOpcode.ADD:
            self._add(a, self._read_register(b), self._read_register(c))
        elif opcode == VMOpcode.ADDI:
            self._add(a, self._read_register(b), c)
        elif opcode == VMOpcode.SUB:
            self._subtract(a, self._read_register(b), self._read_register(c))
        elif opcode == VMOpcode.SUBI:
            self._subtract(a, self._read_register(b), c)
        elif opcode == VMOpcode.MUL:
            self._write_result(a, self._read_register(b) * self._read_register(c), overflow_check=True)
        elif opcode == VMOpcode.MULI:
            self._write_result(a, self._read_register(b) * c, overflow_check=True)
        elif opcode in {VMOpcode.DIV, VMOpcode.MOD}:
            dividend, divisor = self._read_register(b), self._read_register(c)
            if divisor == 0:
                raise _ExecutionFault("División entre cero")
            quotient = self._quotient(dividend, divisor)
            value = quotient if opcode == VMOpcode.DIV else dividend - quotient * divisor
            self._write_result(a, value)
        elif opcode == VMOpcode.CMP:
            self._set_sub_flags(self._read_register(a), self._read_register(b))
        elif opcode == VMOpcode.CMPI:
            self._set_sub_flags(self._read_register(a), b)
        elif opcode == VMOpcode.TEST:
            self._set_flags(self._read_register(a) & self._read_register(b))
        elif opcode == VMOpcode.AND:
            self._write_result(a, self._read_register(b) & self._read_register(c))
        elif opcode == VMOpcode.OR:
            self._write_result(a, self._read_register(b) | self._read_register(c))
        elif opcode == VMOpcode.XOR:
            self._write_result(a, self._read_register(b) ^ self._read_register(c))
        elif opcode == VMOpcode.NOT:
            self._write_result(a, ~self._read_register(b))
        elif opcode in {VMOpcode.SHL, VMOpcode.SHR}:
            if not 0 <= c <= 31:
                raise _ExecutionFault(f"Desplazamiento inválido: {c}")
            value = self._read_register(b)
            if opcode == VMOpcode.SHL:
                carry = bool(c and ((value & 0xFFFFFFFF) >> (32 - c)) & 1)
                self._write_result(a, value << c, carry=carry, overflow_check=True)
            else:
                carry = bool(c and ((value & 0xFFFFFFFF) >> (c - 1)) & 1)
                self._write_result(a, value >> c, carry=carry)
        elif opcode == VMOpcode.JMP:
            self._jump(a)
        elif opcode == VMOpcode.JZ and self._flags["Z"]:
            self._jump(a)
        elif opcode == VMOpcode.JNZ and not self._flags["Z"]:
            self._jump(a)
        elif opcode == VMOpcode.JNEG and self._flags["N"]:
            self._jump(a)
        elif opcode == VMOpcode.JPOS and not self._flags["Z"] and not self._flags["N"]:
            self._jump(a)
        elif opcode == VMOpcode.JC and self._flags["C"]:
            self._jump(a)
        elif opcode == VMOpcode.JNC and not self._flags["C"]:
            self._jump(a)
        elif opcode == VMOpcode.PUSH:
            self._push(self._read_register(a))
        elif opcode == VMOpcode.POP:
            self._write_result(a, self._pop())
        elif opcode == VMOpcode.CALL:
            self._push(self._pc)
            self._jump(a)
        elif opcode == VMOpcode.CALLR:
            target = self._read_register(a)
            self._push(self._pc)
            self._jump(target)
        elif opcode == VMOpcode.RET:
            self._jump(self._pop())
        elif opcode == VMOpcode.SYSCALL:
            # En run() el contexto Tramoya se sincroniza por bloque. Dar a una
            # syscall host el estado acumulado hasta la instrucción anterior.
            self._sync_lifecycle()
            self._invoke_syscall(a)
        elif opcode == VMOpcode.INT:
            self._enter_interrupt(a)
        elif opcode == VMOpcode.IRET:
            self._return_interrupt()
        elif opcode == VMOpcode.EI:
            self._interrupts_enabled = True
        elif opcode == VMOpcode.DI:
            self._interrupts_enabled = False
        elif opcode == VMOpcode.SETIV:
            if "interrupt_control" not in self.config.capabilities:
                raise _ExecutionFault("Capacidad interrupt_control no autorizada")
            try:
                self.set_interrupt_vector(a, b)
            except ValueError as exc:
                raise _ExecutionFault(str(exc)) from exc
        elif opcode == VMOpcode.YIELD:
            raise _WaitSignal("YIELD cooperativo")
        elif opcode == VMOpcode.BREAK:
            raise _PauseSignal("Instrucción BREAK")
        elif opcode == VMOpcode.HALT:
            # HALT indica terminación correcta. Para devolver otro código se
            # usa la syscall 0, que toma el valor explícito de R1.
            raise _HaltSignal(0)
        # ── FPU: punto flotante IEEE 754 single-precision ────────────
        elif opcode == VMOpcode.FADD:
            self._float_to_reg(a, self._float_from_reg(b) + self._float_from_reg(c))
        elif opcode == VMOpcode.FSUB:
            self._float_to_reg(a, self._float_from_reg(b) - self._float_from_reg(c))
        elif opcode == VMOpcode.FMUL:
            self._float_to_reg(a, self._float_from_reg(b) * self._float_from_reg(c))
        elif opcode == VMOpcode.FDIV:
            divisor = self._float_from_reg(c)
            if divisor == 0.0:
                raise _ExecutionFault("División flotante entre cero")
            self._float_to_reg(a, self._float_from_reg(b) / divisor)
        elif opcode == VMOpcode.FCMP:
            left, right = self._float_from_reg(a), self._float_from_reg(b)
            if math.isnan(left) or math.isnan(right):
                self._flags.update(Z=False, N=False, C=True, O=False)
            else:
                self._set_float_flags(left - right)
        elif opcode == VMOpcode.FTOI:
            fval = self._float_from_reg(b)
            if math.isnan(fval) or math.isinf(fval):
                raise _ExecutionFault("Conversión float→int de NaN o infinito")
            ival = int(fval)
            if not -(1 << 31) <= ival < (1 << 31):
                raise _ExecutionFault("Desbordamiento en conversión float→int")
            self._write_result(a, ival)
        elif opcode == VMOpcode.ITOF:
            self._float_to_reg(a, float(self._read_register(b)))
        elif opcode == VMOpcode.FABS:
            self._float_to_reg(a, abs(self._float_from_reg(b)))
        elif opcode == VMOpcode.FSQRT:
            fval = self._float_from_reg(b)
            if fval < 0.0:
                raise _ExecutionFault("Raíz cuadrada de número negativo")
            self._float_to_reg(a, math.sqrt(fval))
        # ── Aritmética extendida 64 bits ───────────────────────
        elif opcode == VMOpcode.MULH:
            full = self._read_register(b) * self._read_register(c)
            high = signed32((full >> 32) & 0xFFFFFFFF)
            self._write_result(a, high)
        elif opcode == VMOpcode.ADDX:
            left, right = self._read_register(b), self._read_register(c)
            carry_in = 1 if self._flags["C"] else 0
            raw = left + right + carry_in
            unsigned = (left & 0xFFFFFFFF) + (right & 0xFFFFFFFF) + carry_in
            result = signed32(raw)
            overflow = (left >= 0) == (right >= 0) and (result >= 0) != (left >= 0)
            self._write_register(a, result)
            self._set_flags(result, carry=unsigned > 0xFFFFFFFF, overflow=overflow)
        elif opcode == VMOpcode.SUBX:
            left, right = self._read_register(b), self._read_register(c)
            borrow_in = 0 if self._flags["C"] else 1
            raw = left - right - borrow_in
            result = signed32(raw)
            overflow = (left >= 0) != (right >= 0) and (result >= 0) != (left >= 0)
            borrow_unsigned = (left & 0xFFFFFFFF) >= ((right & 0xFFFFFFFF) + borrow_in)
            self._write_register(a, result)
            self._set_flags(result, carry=borrow_unsigned, overflow=overflow)
        # ── Fibras cooperativas ────────────────────────────────
        elif opcode == VMOpcode.SPAWN:
            self._spawn_fiber(a)
        elif opcode == VMOpcode.SWITCH:
            self._switch_fiber(self._read_register(a))
        elif opcode == VMOpcode.FRET:
            self._finish_fiber()

    def _effective_address(self, base_register: int, offset: int) -> int:
        return self._read_register(base_register) + offset

    def _read_register(self, index: int) -> int:
        if not 0 <= index <= 15:
            raise _ExecutionFault(f"Registro inválido: R{index}")
        # R0 y R15 se mantienen al escribir y al modificar la pila; no es
        # necesario resincronizar los 16 registros en cada lectura del hot path.
        return self._registers[index]

    def _write_register(self, index: int, value: int) -> None:
        if not 0 <= index <= 15:
            raise _ExecutionFault(f"Registro inválido: R{index}")
        if index == 0:
            return
        if index == 15:
            raise _ExecutionFault("R15/SP es de solo lectura")
        self._registers[index] = signed32(value)

    def _sync_special_registers(self) -> None:
        self._registers[0] = 0
        self._registers[15] = len(self._stack)

    def _write_result(
        self,
        register: int,
        value: int,
        *,
        carry: bool = False,
        overflow_check: bool = False,
    ) -> None:
        normalized = signed32(value)
        self._write_register(register, normalized)
        self._set_flags(normalized, carry=carry, overflow=overflow_check and normalized != value)

    def _set_flags(self, value: int, *, carry: bool = False, overflow: bool = False) -> None:
        normalized = signed32(value)
        self._flags.update(Z=normalized == 0, N=normalized < 0, C=carry, O=overflow)

    def _add(self, destination: int, left: int, right: int) -> None:
        raw = left + right
        unsigned = (left & 0xFFFFFFFF) + (right & 0xFFFFFFFF)
        result = signed32(raw)
        overflow = (left >= 0) == (right >= 0) and (result >= 0) != (left >= 0)
        self._write_register(destination, result)
        self._set_flags(result, carry=unsigned > 0xFFFFFFFF, overflow=overflow)

    def _subtract(self, destination: int, left: int, right: int) -> None:
        result = signed32(left - right)
        overflow = (left >= 0) != (right >= 0) and (result >= 0) != (left >= 0)
        self._write_register(destination, result)
        self._set_flags(result, carry=(left & 0xFFFFFFFF) >= (right & 0xFFFFFFFF), overflow=overflow)

    def _set_sub_flags(self, left: int, right: int) -> None:
        result = signed32(left - right)
        overflow = (left >= 0) != (right >= 0) and (result >= 0) != (left >= 0)
        self._set_flags(result, carry=(left & 0xFFFFFFFF) >= (right & 0xFFFFFFFF), overflow=overflow)

    @staticmethod
    def _quotient(dividend: int, divisor: int) -> int:
        quotient = abs(dividend) // abs(divisor)
        return -quotient if (dividend < 0) != (divisor < 0) else quotient

    # ── FPU: IEEE 754 single-precision ─────────────────────────────

    def _float_from_reg(self, index: int) -> float:
        """Interpreta un registro como float IEEE 754 single-precision."""
        bits = self._read_register(index) & 0xFFFFFFFF
        return struct.unpack('f', struct.pack('I', bits))[0]

    def _float_to_reg(self, dest: int, value: float) -> None:
        """Empaqueta un float en un registro y actualiza banderas flotantes."""
        bits = struct.unpack('I', struct.pack('f', value))[0]
        self._write_register(dest, signed32(bits))
        self._set_float_flags(value)

    def _set_float_flags(self, value: float) -> None:
        """Z=cero, N=negativo, C=NaN, O=infinito."""
        is_nan = math.isnan(value)
        self._flags.update(
            Z=value == 0.0 and not is_nan,
            N=value < 0.0 and not is_nan,
            C=is_nan,
            O=math.isinf(value),
        )

    def _push(self, value: int) -> None:
        if len(self._stack) >= self.config.stack_limit:
            raise _ExecutionFault("Desbordamiento superior de pila")
        self._stack.append(signed32(value))
        self._sync_special_registers()

    def _pop(self) -> int:
        if not self._stack:
            raise _ExecutionFault("Desbordamiento inferior de pila")
        value = self._stack.pop()
        self._sync_special_registers()
        return value

    def _validate_executable(self, address: int) -> None:
        if self._program is None or not 0 <= address < self._program.code_size or address % 4:
            raise _ExecutionFault(f"Dirección no ejecutable: {address}")

    def _jump(self, address: int) -> None:
        self._validate_executable(address)
        self._pc = address

    def read_memory(self, address: int) -> int:
        if not isinstance(address, int) or not 0 <= address < len(self._memory):
            raise _ExecutionFault(f"Lectura fuera de memoria: {address!r}")
        return self._memory[address]

    def write_memory(self, address: int, value: int) -> None:
        if not isinstance(address, int) or not 0 <= address < len(self._memory):
            raise _ExecutionFault(f"Escritura fuera de memoria: {address!r}")
        if self.config.protect_code and self._program is not None and address < self._program.code_size:
            raise _ExecutionFault(f"Intento de modificar código protegido en {address}")
        if self._journal is not None and address not in self._journal:
            self._journal[address] = self._memory[address]
        self._memory[address] = signed32(value)
        if self._program is not None and address < self._program.code_size:
            self._decode_cache[address // 4] = None

    def memory_slice(self, start: int, count: int) -> tuple[int, ...]:
        if start < 0 or count < 0 or start + count > len(self._memory):
            raise ValueError("Rango de memoria inválido")
        return self._memory.read_block(start, count)

    def get_register(self, index: int) -> int:
        """Lee un registro desde una integración de host/syscall."""
        try:
            return self._read_register(index)
        except _ExecutionFault as exc:
            raise VMRuntimeError(str(exc)) from exc

    def set_register(self, index: int, value: int, *, update_flags: bool = True) -> None:
        """Escribe un registro desde el host respetando R0 y R15."""
        try:
            if update_flags:
                self._write_result(index, int(value))
            else:
                self._write_register(index, int(value))
        except _ExecutionFault as exc:
            raise VMRuntimeError(str(exc)) from exc

    def emit(self, value: int | str) -> None:
        """Añade una salida controlada desde una syscall del host."""
        if not isinstance(value, (int, str)):
            raise TypeError("La salida debe ser int o str")
        normalized = signed32(value) if isinstance(value, int) else value
        units = len(normalized) if isinstance(normalized, str) else 1
        if self._output_units + units > self.config.output_limit:
            raise _ExecutionFault("Límite de salida excedido")
        self._output.append(normalized)
        self._output_units += units

    def wait_for_host(self, reason: str = "Esperando al host") -> None:
        """Suspende cooperativamente la VM desde una syscall."""
        raise _WaitSignal(reason)

    def terminate(self, exit_code: int = 0) -> None:
        """Finaliza la VM desde una syscall del host."""
        raise _HaltSignal(signed32(exit_code))

    def register_syscall(
        self,
        number: int,
        handler: Callable[["TramoyaVM32"], None],
        *,
        name: str | None = None,
        capability: str | None = None,
        replace: bool = False,
    ) -> None:
        if not 0 <= number <= 65_535:
            raise ValueError("Número de syscall fuera de rango")
        if number in self._syscalls and not replace:
            raise ValueError(f"La syscall {number} ya existe")
        self._syscalls[number] = _Syscall(number, name or f"syscall_{number}", handler, capability)

    def _invoke_syscall(self, number: int) -> None:
        syscall = self._syscalls.get(number)
        if syscall is None:
            raise _ExecutionFault(f"Syscall desconocida: {number}")
        if syscall.capability and syscall.capability not in self.config.capabilities:
            raise _ExecutionFault(f"Capacidad no autorizada: {syscall.capability}")
        syscall.handler(self)

    def _install_builtin_syscalls(self) -> None:
        self.register_syscall(0, lambda vm: vm._sys_exit(), name="exit")
        self.register_syscall(1, lambda vm: vm.emit(vm._read_register(1)), name="print_int", capability="io")
        self.register_syscall(2, lambda vm: vm.emit(chr(vm._read_register(1) & 0xFF)), name="print_char", capability="io")
        self.register_syscall(3, lambda vm: vm._sys_read_int(), name="read_int", capability="io")
        self.register_syscall(4, lambda vm: vm._write_result(1, len(vm._memory)), name="memory_size", capability="introspection")
        self.register_syscall(5, lambda vm: vm._write_result(1, vm._cycles), name="cycles", capability="introspection")
        self.register_syscall(6, lambda vm: vm._sys_random(), name="random", capability="random")
        self.register_syscall(7, lambda vm: vm._sys_print_string(), name="print_string", capability="io")
        self.register_syscall(8, lambda vm: vm._sys_alloc(), name="alloc", capability="memory")
        self.register_syscall(9, lambda vm: vm._sys_memory_chip_read(), name="memory_chip_read", capability="memory_chip")
        self.register_syscall(10, lambda vm: vm._sys_memory_chip_write(), name="memory_chip_write", capability="memory_chip")
        self.register_syscall(11, lambda vm: vm._sys_memory_chip_size(), name="memory_chip_size", capability="memory_chip")

    def _sys_exit(self) -> None:
        raise _HaltSignal(self._read_register(1))

    def _sys_read_int(self) -> None:
        if not self._input:
            self._pc -= 4
            raise _WaitSignal("Esperando entrada para read_int")
        self._write_result(1, self._input.popleft())

    def _sys_random(self) -> None:
        value = self._random_state & 0xFFFFFFFF
        value ^= (value << 13) & 0xFFFFFFFF
        value ^= value >> 17
        value ^= (value << 5) & 0xFFFFFFFF
        self._random_state = value & 0xFFFFFFFF
        self._write_result(1, self._random_state)

    def _sys_print_string(self) -> None:
        address = self._read_register(1)
        maximum = self._read_register(2)
        if maximum <= 0:
            maximum = 4096
        remaining_output = self.config.output_limit - self._output_units
        if remaining_output <= 0:
            raise _ExecutionFault("Límite de salida excedido")
        # Una syscall tiene coste de gas fijo; impedir que un R2 hostil fuerce
        # un escaneo arbitrario de toda la RAM en una sola instrucción.
        scan_limit = min(maximum, remaining_output + 1, 1_000_000)
        characters: list[str] = []
        for offset in range(scan_limit):
            value = self.read_memory(address + offset)
            if value == 0:
                self.emit("".join(characters))
                return
            if not 0 <= value <= 0x10FFFF:
                raise _ExecutionFault(f"Código Unicode inválido en memoria: {value}")
            characters.append(chr(value))
        if scan_limit < maximum:
            raise _ExecutionFault("Cadena excede el límite seguro de lectura o salida")
        raise _ExecutionFault("Cadena sin terminador dentro del límite")

    def _sys_alloc(self) -> None:
        count = self._read_register(1)
        if count <= 0:
            raise _ExecutionFault("alloc requiere un tamaño positivo en R1")
        if self._heap_ptr + count > len(self._memory):
            raise _ExecutionFault("Memoria insuficiente en alloc")
        address = self._heap_ptr
        self._heap_ptr += count
        self._write_result(1, address)

    def _sys_memory_chip_read(self) -> None:
        chip = self._require_memory_chip()
        offset = self._read_register(1)
        destination = self._read_register(2)
        count = self._read_memory_chip_transfer_count(self._read_register(3))
        if destination < 0 or destination + count > len(self._memory):
            raise _ExecutionFault("Destino de lectura del chip fuera de la RAM")
        try:
            data = chip.read(offset, count)
        except Exception as exc:
            raise _ExecutionFault(f"Lectura del chip fallida: {exc}") from exc
        for index, value in enumerate(data):
            self.write_memory(destination + index, value)
        self._write_result(1, count)

    def _sys_memory_chip_write(self) -> None:
        chip = self._require_memory_chip()
        offset = self._read_register(1)
        source = self._read_register(2)
        count = self._read_memory_chip_transfer_count(self._read_register(3))
        if source < 0 or source + count > len(self._memory):
            raise _ExecutionFault("Origen de escritura del chip fuera de la RAM")
        data = bytes(self.read_memory(source + index) & 0xFF for index in range(count))
        # La escritura es un efecto persistente del host; validar y preparar todos
        # los datos antes de confirmarla en el chip.
        self._write_result(1, count)
        try:
            chip.write(offset, data)
        except Exception as exc:
            raise _ExecutionFault(f"Escritura del chip fallida: {exc}") from exc

    def _sys_memory_chip_size(self) -> None:
        chip = self._require_memory_chip()
        self._write_result(1, chip.capacity_bytes)

    def _require_memory_chip(self) -> NonVolatileMemoryChip:
        if self.memory_chip is None:
            raise _ExecutionFault("No hay un chip de memoria conectado")
        return self.memory_chip

    @staticmethod
    def _read_memory_chip_transfer_count(count: int) -> int:
        if not 0 <= count <= MAX_MEMORY_CHIP_TRANSFER_BYTES:
            raise _ExecutionFault(
                f"La transferencia del chip debe estar entre 0 y {MAX_MEMORY_CHIP_TRANSFER_BYTES} bytes"
            )
        return count

    def provide_input(self, *values: int) -> str:
        self._input.extend(signed32(int(value)) for value in values)
        if self.state == "WAITING":
            self.machine.trigger("wake")
        return self.state

    def set_interrupt_vector(self, vector: int, address: int) -> None:
        if not 0 <= vector <= 255:
            raise ValueError(f"Vector de interrupción inválido: {vector}")
        try:
            self._validate_executable(address)
        except _ExecutionFault as exc:
            raise ValueError(str(exc)) from exc
        self._interrupt_vectors[vector] = address

    def request_interrupt(self, vector: int) -> None:
        if not 0 <= vector <= 255:
            raise ValueError("Vector de interrupción inválido")
        self._pending_interrupts.append(vector)

    def _enter_interrupt(self, vector: int) -> None:
        if len(self._interrupt_stack) >= self.config.interrupt_depth:
            raise _ExecutionFault("Profundidad máxima de interrupciones excedida")
        if vector not in self._interrupt_vectors:
            raise _ExecutionFault(f"Vector de interrupción {vector} no configurado")
        target = self._interrupt_vectors[vector]
        self._validate_executable(target)
        self._interrupt_stack.append((self._pc, dict(self._flags), self._interrupts_enabled))
        self._pc = target
        self._interrupts_enabled = False

    def _return_interrupt(self) -> None:
        if not self._interrupt_stack:
            raise _ExecutionFault("IRET sin interrupción activa")
        pc, flags, enabled = self._interrupt_stack.pop()
        self._validate_executable(pc)
        self._pc = pc
        self._flags = flags
        self._interrupts_enabled = enabled

    # ── Fibras cooperativas ────────────────────────────────────────

    def _spawn_fiber(self, address: int) -> None:
        """Crea una nueva fibra en la dirección dada. Devuelve su ID en R1."""
        if len(self._fibers) >= self.config.fiber_limit:
            raise _ExecutionFault(f"Límite de fibras alcanzado ({self.config.fiber_limit})")
        self._validate_executable(address)
        fiber_id = self._next_fiber_id
        self._next_fiber_id += 1
        fiber = _FiberContext(
            fiber_id=fiber_id,
            pc=address,
            registers=[0] * 16,
            flags={"Z": True, "N": False, "C": False, "O": False},
            stack=[],
            state="READY",
            parent_id=self._current_fiber,
        )
        self._fibers[fiber_id] = fiber
        self._write_result(1, fiber_id)

    def _switch_fiber(self, fiber_id: int) -> None:
        """Cambia el contexto de ejecución a la fibra indicada."""
        if fiber_id == self._current_fiber:
            return
        target = self._fibers.get(fiber_id)
        if target is None:
            raise _ExecutionFault(f"Fibra {fiber_id} no encontrada")
        if target.state == "FINISHED":
            raise _ExecutionFault(f"Fibra {fiber_id} ya terminó")
        self._save_fiber_context()
        self._load_fiber_context(fiber_id)

    def _finish_fiber(self) -> None:
        """Termina la fibra actual y regresa a la fibra padre."""
        if self._current_fiber == 0:
            raise _ExecutionFault("La fibra principal no puede terminar con FRET")
        fiber = self._fibers.get(self._current_fiber)
        parent_id = fiber.parent_id if fiber is not None else 0
        if fiber is not None:
            fiber.state = "FINISHED"
        parent = self._fibers.get(parent_id)
        if parent is None or parent.state == "FINISHED":
            parent_id = 0
            parent = self._fibers.get(parent_id)
        if parent is None:
            raise _ExecutionFault("No hay fibra padre a la cual regresar")
        self._load_fiber_context(parent_id)

    def _save_fiber_context(self) -> None:
        """Guarda el estado actual de ejecución en la fibra activa."""
        fiber = self._fibers.get(self._current_fiber)
        if fiber is None:
            fiber = _FiberContext(
                fiber_id=self._current_fiber,
                pc=self._pc,
                registers=self._registers.copy(),
                flags=dict(self._flags),
                stack=self._stack.copy(),
                state="READY",
                parent_id=0,
            )
            self._fibers[self._current_fiber] = fiber
        else:
            fiber.pc = self._pc
            fiber.registers = self._registers.copy()
            fiber.flags = dict(self._flags)
            fiber.stack = self._stack.copy()
            fiber.state = "READY"

    def _load_fiber_context(self, fiber_id: int) -> None:
        """Carga el estado de una fibra en el contexto de ejecución."""
        fiber = self._fibers[fiber_id]
        self._pc = fiber.pc
        self._registers = fiber.registers.copy()
        self._flags = dict(fiber.flags)
        self._stack = fiber.stack.copy()
        fiber.state = "RUNNING"
        self._current_fiber = fiber_id
        self._sync_special_registers()

    def step(self) -> str:
        if self.state == "READY":
            self.machine.trigger("start")
        if self.state != "RUNNING":
            return self.state
        self._pending_event = None
        self.machine.trigger("tick")
        self._apply_pending_event()
        return self.state

    def _apply_pending_event(self) -> None:
        if self._pending_event is None or self.state != "RUNNING":
            return
        event, payload = self._pending_event
        self._pending_event = None
        if event == "halt":
            self.machine.trigger("halt", _exit_code=int(payload or 0))
        elif event == "fault":
            self.machine.trigger(
                "fault",
                _fault_authorized=True,
                _fault_message=str(payload),
            )
        elif event == "wait":
            self.machine.trigger("wait", _wait_reason=str(payload))
        elif event == "pause":
            self.machine.trigger("pause", _pause_reason=str(payload))

    def run(
        self,
        max_instructions: int | None = None,
        breakpoints: Iterable[int] = (),
    ) -> VMResult:
        if self._program is None:
            raise VMRuntimeError("No hay programa cargado")
        if max_instructions is not None and max_instructions < 1:
            raise ValueError("max_instructions debe ser positivo")
        breakpoint_set = set(breakpoints)
        skip_breakpoint: int | None = None
        if self.state == "PAUSED":
            skip_breakpoint = self._pc
            self.resume()
        elif self.state == "WAITING":
            return self.result()
        elif self.state == "READY":
            self.machine.trigger("start")

        executed_at_start = self._instructions
        while self.state == "RUNNING":
            if self._pc in breakpoint_set and self._pc != skip_breakpoint:
                self.pause(f"Breakpoint en PC={self._pc}")
                break
            skip_breakpoint = None
            if max_instructions is not None and self._instructions - executed_at_start >= max_instructions:
                self.pause(f"Límite local de {max_instructions} instrucciones")
                break
            # El modo continuo conserva Tramoya para las transiciones de ciclo
            # de vida, pero evita una transición self-loop por instrucción. La
            # traza VM32 sigue registrando cada operación; step() mantiene el
            # camino instrumentado cuando se requiere depuración fina.
            self._pending_event = None
            self._execute_one()
            if self._pending_event is not None:
                self._sync_lifecycle()
            self._apply_pending_event()
        self._sync_lifecycle()
        return self.result()

    def pause(self, reason: str = "Pausa solicitada") -> str:
        if self.state == "RUNNING":
            self.machine.trigger("pause", _pause_reason=reason)
        return self.state

    def resume(self) -> str:
        if self.state == "PAUSED":
            self.machine.trigger("resume")
        return self.state

    def wake(self) -> str:
        if self.state == "WAITING":
            self.machine.trigger("wake")
        return self.state

    def result(self) -> VMResult:
        ctx = self.machine.ctx
        return VMResult(
            state=self.state,
            exit_code=ctx.get("exit_code"),
            fault=ctx.get("fault"),
            wait_reason=ctx.get("wait_reason"),
            pause_reason=ctx.get("pause_reason"),
            instructions=self._instructions,
            cycles=self._cycles,
            gas_remaining=self._gas_remaining,
            output=tuple(self._output),
        )

    def format_registers(self) -> str:
        self._sync_special_registers()
        rows = []
        for start in range(0, 16, 4):
            rows.append("  ".join(f"R{index:02}={self._registers[index]:11d}" for index in range(start, start + 4)))
        flags = " ".join(f"{name}={int(value)}" for name, value in self._flags.items())
        return f"STATE={self.state} PC={self._pc:08X} {flags}\n" + "\n".join(rows)

    def lifecycle_mermaid(self) -> str:
        return self.machine.to_mermaid()

    def available_controls(self) -> tuple[str, ...]:
        return tuple(trigger for trigger in self.machine.available_triggers if self.machine.can(trigger))

    def snapshot_bytes(self) -> bytes:
        if self._program is None:
            raise VMRuntimeError("No hay programa cargado")
        payload = {
            "version": SNAPSHOT_VERSION,
            "config": {
                "memory_words": self.config.memory_words,
                "gas_limit": self.config.gas_limit,
                "stack_limit": self.config.stack_limit,
                "interrupt_depth": self.config.interrupt_depth,
                "trace_size": self.config.trace_size,
                "protect_code": self.config.protect_code,
                "output_limit": self.config.output_limit,
                "fiber_limit": self.config.fiber_limit,
                "capabilities": sorted(self.config.capabilities),
            },
            "program": {
                "entry": self._program.entry,
                "code_size": self._program.code_size,
                "data_size": self._program.data_size,
                "symbols": dict(self._program.symbols),
                "source_name": self._program.source_name,
            },
            "lifecycle": self.machine.to_dict(),
            "core": {
                "memory_pages": self._memory.snapshot_pages(),
                "memory_page_words": self._memory.page_words,
                "registers": self._registers,
                "flags": self._flags,
                "pc": self._pc,
                "stack": self._stack,
                "input": list(self._input),
                "output": self._output,
                "output_units": self._output_units,
                "interrupt_vectors": self._interrupt_vectors,
                "pending_interrupts": list(self._pending_interrupts),
                "interrupt_stack": self._interrupt_stack,
                "interrupts_enabled": self._interrupts_enabled,
                "heap_ptr": self._heap_ptr,
                "random_state": self._random_state,
                "instructions": self._instructions,
                "cycles": self._cycles,
                "gas_remaining": self._gas_remaining,
                "fibers": [
                    {
                        "fiber_id": f.fiber_id,
                        "pc": f.pc,
                        "registers": f.registers,
                        "flags": f.flags,
                        "stack": f.stack,
                        "state": f.state,
                        "parent_id": f.parent_id,
                    }
                    for f in self._fibers.values()
                ],
                "current_fiber": self._current_fiber,
                "next_fiber_id": self._next_fiber_id,
            },
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return SNAPSHOT_MAGIC + zlib.compress(encoded, level=9)

    def restore_bytes(self, snapshot: bytes) -> None:
        if not snapshot.startswith(SNAPSHOT_MAGIC):
            raise ValueError("Firma de snapshot VM32 inválida")
        previous = self.snapshot_bytes() if self._program is not None else None
        try:
            payload = self._decode_snapshot(snapshot)
            self._apply_snapshot(payload)
        except Exception as exc:
            if previous is not None:
                payload = self._decode_snapshot(previous)
                self._apply_snapshot(payload)
            else:
                # Una restauración sobre una VM vacía también es atómica: si
                # load_dict alcanzó a mutar el ciclo de vida, volver a CREATED.
                self._initialize_core()
                self.machine = self._build_lifecycle()
            if isinstance(exc, (ValueError, MachineError)):
                raise
            raise ValueError("Contenido de snapshot VM32 inválido") from exc
        self._trace.clear()
        self._trace_sequence = 0

    @classmethod
    def from_snapshot_bytes(
        cls,
        snapshot: bytes,
        *,
        trace_size: int | None = None,
        capabilities: frozenset[str] | None = None,
        memory_chip: NonVolatileMemoryChip | None = None,
    ) -> "TramoyaVM32":
        """Crea una VM con la RAM y límites requeridos por el snapshot."""
        payload = cls._decode_snapshot_payload(snapshot, MAX_SNAPSHOT_RAW_BYTES)
        config_data = payload.get("config")
        if not isinstance(config_data, Mapping):
            raise ValueError("Snapshot sin configuración válida")
        defaults = VMConfig()
        embedded_capabilities = config_data.get("capabilities", defaults.capabilities)
        if capabilities is None:
            if not isinstance(embedded_capabilities, (list, tuple, set, frozenset)) or not all(
                isinstance(value, str) for value in embedded_capabilities
            ):
                raise ValueError("Capabilities inválidas en snapshot")
            capabilities = frozenset(embedded_capabilities)
        def config_integer(name: str, default: int | None = None) -> int:
            value = config_data.get(name, default)
            if type(value) is not int:
                raise ValueError(f"{name} inválido en snapshot")
            return value

        protect_code = config_data.get("protect_code", defaults.protect_code)
        if type(protect_code) is not bool:
            raise ValueError("protect_code inválido en snapshot")
        try:
            config = VMConfig(
                memory_words=config_integer("memory_words"),
                gas_limit=config_integer("gas_limit", defaults.gas_limit),
                stack_limit=config_integer("stack_limit", defaults.stack_limit),
                interrupt_depth=config_integer("interrupt_depth", defaults.interrupt_depth),
                trace_size=(
                    trace_size
                    if trace_size is not None
                    else config_integer("trace_size", defaults.trace_size)
                ),
                output_limit=config_integer("output_limit", defaults.output_limit),
                fiber_limit=config_integer("fiber_limit", defaults.fiber_limit),
                protect_code=protect_code,
                capabilities=capabilities,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Configuración inválida en snapshot") from exc
        vm = cls(config, memory_chip=memory_chip)
        vm._apply_snapshot(payload)
        vm._trace.clear()
        vm._trace_sequence = 0
        return vm

    def _decode_snapshot(self, snapshot: bytes) -> Mapping[str, Any]:
        maximum = min(
            self.config.memory_words * 16 + self.config.output_limit * 8 + 2_000_000,
            MAX_SNAPSHOT_RAW_BYTES,
        )
        return self._decode_snapshot_payload(snapshot, maximum)

    @staticmethod
    def _decode_snapshot_payload(snapshot: bytes, maximum: int) -> Mapping[str, Any]:
        if not snapshot.startswith(SNAPSHOT_MAGIC):
            raise ValueError("Firma de snapshot VM32 inválida")
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(snapshot[len(SNAPSHOT_MAGIC):], maximum + 1)
        if len(raw) > maximum or decompressor.unconsumed_tail or not decompressor.eof:
            raise ValueError("Snapshot VM32 excede el tamaño permitido o está truncado")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("JSON de snapshot VM32 inválido") from exc
        if not isinstance(payload, dict):
            raise ValueError("Estructura de snapshot VM32 inválida")
        return payload

    def _apply_snapshot(self, payload: Mapping[str, Any]) -> None:
        version = payload.get("version")
        if version not in {1, SNAPSHOT_VERSION}:
            raise ValueError("Versión de snapshot VM32 no compatible")
        config = payload.get("config")
        if not isinstance(config, Mapping) or config.get("memory_words") != self.config.memory_words:
            raise ValueError("El snapshot requiere otro tamaño de memoria")
        core = payload.get("core")
        program_data = payload.get("program")
        lifecycle = payload.get("lifecycle")
        if not isinstance(core, Mapping) or not isinstance(program_data, Mapping) or not isinstance(lifecycle, Mapping):
            raise ValueError("Estructura de snapshot incompleta")

        if version == 1:
            dense_memory = core.get("memory")
            if not isinstance(dense_memory, list) or len(dense_memory) != self.config.memory_words:
                raise ValueError("Memoria densa inválida en snapshot")
            if not all(type(value) is int and -(1 << 31) <= value < (1 << 31) for value in dense_memory):
                raise ValueError("Valor de memoria fuera de int32")
            memory = PagedMemory(self.config.memory_words)
            memory.write_block(0, dense_memory)
        else:
            page_words = core.get("memory_page_words", 4096)
            if type(page_words) is not int or not 256 <= page_words <= 65_536:
                raise ValueError("Tamaño de página inválido")
            memory = PagedMemory.from_snapshot(
                self.config.memory_words,
                core.get("memory_pages"),
                page_words=page_words,
            )

        registers = core.get("registers")
        if not isinstance(registers, list) or len(registers) != 16 or not all(
            type(value) is int and -(1 << 31) <= value < (1 << 31) for value in registers
        ):
            raise ValueError("Registros inválidos en snapshot")
        integer_fields = ("entry", "code_size", "data_size")
        if not all(type(program_data.get(field)) is int for field in integer_fields):
            raise ValueError("Metadatos de programa inválidos")
        total = int(program_data["code_size"]) + int(program_data["data_size"])
        if not 0 < total <= self.config.memory_words:
            raise ValueError("Tamaño de programa inválido")
        program = Program32(
            words=memory.read_block(0, total),
            entry=program_data["entry"],
            code_size=program_data["code_size"],
            data_size=program_data["data_size"],
            symbols=program_data["symbols"],
            source_name=program_data["source_name"],
        )

        flags = core.get("flags")
        if not isinstance(flags, Mapping) or set(flags) != {"Z", "N", "C", "O"} or not all(
            type(value) is bool for value in flags.values()
        ):
            raise ValueError("Banderas inválidas en snapshot")

        def int32_list(value: object, name: str) -> list[int]:
            if not isinstance(value, list) or not all(
                type(item) is int and -(1 << 31) <= item < (1 << 31) for item in value
            ):
                raise ValueError(f"{name} inválida en snapshot")
            return list(value)

        stack = int32_list(core.get("stack"), "Pila")
        input_values = int32_list(core.get("input"), "Entrada")
        output = core.get("output")
        if not isinstance(output, list) or not all(
            isinstance(value, str) or type(value) is int and -(1 << 31) <= value < (1 << 31)
            for value in output
        ):
            raise ValueError("Salida inválida en snapshot")

        interrupt_vectors_raw = core.get("interrupt_vectors")
        if not isinstance(interrupt_vectors_raw, Mapping):
            raise ValueError("Vectores de interrupción inválidos")
        try:
            interrupt_vectors = {int(key): int(value) for key, value in interrupt_vectors_raw.items()}
        except (TypeError, ValueError) as exc:
            raise ValueError("Vectores de interrupción inválidos") from exc
        pending_interrupts = int32_list(core.get("pending_interrupts"), "Cola de interrupciones")
        interrupt_stack_raw = core.get("interrupt_stack")
        if not isinstance(interrupt_stack_raw, list):
            raise ValueError("Pila de interrupciones inválida")
        interrupt_stack: list[tuple[int, dict[str, bool], bool]] = []
        for item in interrupt_stack_raw:
            if not isinstance(item, list) or len(item) != 3:
                raise ValueError("Contexto de interrupción inválido")
            pc, saved_flags, enabled = item
            if type(pc) is not int or not isinstance(saved_flags, Mapping) or set(saved_flags) != {"Z", "N", "C", "O"}:
                raise ValueError("Contexto de interrupción inválido")
            if not all(type(value) is bool for value in saved_flags.values()) or type(enabled) is not bool:
                raise ValueError("Contexto de interrupción inválido")
            interrupt_stack.append((pc, dict(saved_flags), enabled))

        scalar_names = ("pc", "output_units", "heap_ptr", "random_state", "instructions", "cycles", "gas_remaining")
        if not all(type(core.get(name)) is int for name in scalar_names):
            raise ValueError("Contadores inválidos en snapshot")
        if type(core.get("interrupts_enabled")) is not bool:
            raise ValueError("Estado de interrupciones inválido")

        self.machine.load_dict(lifecycle)
        self._program = program
        self._memory = memory
        self._registers = list(registers)
        self._flags = dict(flags)
        self._pc = int(core["pc"])
        self._stack = stack
        self._input = deque(input_values)
        self._output = list(output)
        self._output_units = int(core.get("output_units", sum(len(v) if isinstance(v, str) else 1 for v in self._output)))
        self._interrupt_vectors = interrupt_vectors
        self._pending_interrupts = deque(pending_interrupts)
        self._interrupt_stack = interrupt_stack
        self._interrupts_enabled = bool(core["interrupts_enabled"])
        self._heap_ptr = int(core["heap_ptr"])
        self._random_state = int(core["random_state"])
        self._instructions = int(core["instructions"])
        self._cycles = int(core["cycles"])
        self._gas_remaining = int(core["gas_remaining"])
        self._current_fiber = int(core.get("current_fiber", 0))
        self._next_fiber_id = int(core.get("next_fiber_id", 1))
        fibers_raw = core.get("fibers", [])
        self._fibers = {}
        if isinstance(fibers_raw, list):
            for fdata in fibers_raw:
                if isinstance(fdata, dict):
                    fid = int(fdata.get("fiber_id", 0))
                    self._fibers[fid] = _FiberContext(
                        fiber_id=fid,
                        pc=int(fdata.get("pc", 0)),
                        registers=list(fdata.get("registers", [0] * 16)),
                        flags=dict(fdata.get("flags", {"Z": True, "N": False, "C": False, "O": False})),
                        stack=list(fdata.get("stack", [])),
                        state=str(fdata.get("state", "READY")),
                        parent_id=int(fdata.get("parent_id", 0)),
                    )
        self._validate_restored_state()
        self._build_decode_cache()
        self._sync_special_registers()

    def _validate_restored_state(self) -> None:
        if self._program is None:
            raise ValueError("Snapshot sin programa")
        if self.state not in FINAL_STATES:
            self._validate_executable(self._pc)
        if len(self._stack) > self.config.stack_limit:
            raise ValueError("Pila excede el límite configurado")
        program_end = len(self._program.words)
        if not program_end <= self._heap_ptr <= len(self._memory):
            raise ValueError("Puntero de heap inválido")
        if not 0 <= self._gas_remaining <= self.config.gas_limit:
            raise ValueError("Gas inválido")
        if self._instructions < 0 or self._cycles < 0:
            raise ValueError("Contadores de ejecución inválidos")
        expected_output_units = sum(len(value) if isinstance(value, str) else 1 for value in self._output)
        if self._output_units != expected_output_units or not 0 <= self._output_units <= self.config.output_limit:
            raise ValueError("Salida excede el límite configurado")
        if any(not 0 <= vector <= 255 for vector in self._pending_interrupts):
            raise ValueError("Interrupción pendiente inválida")
        for vector, target in self._interrupt_vectors.items():
            if not 0 <= vector <= 255:
                raise ValueError("Vector de interrupción inválido")
            try:
                self._validate_executable(target)
            except _ExecutionFault as exc:
                raise ValueError(str(exc)) from exc
        if len(self._interrupt_stack) > self.config.interrupt_depth:
            raise ValueError("Pila de interrupciones excede el límite")
        if len(self._fibers) > self.config.fiber_limit:
            raise ValueError("Número de fibras excede el límite")
        for fiber in self._fibers.values():
            if fiber.state not in {"READY", "RUNNING", "FINISHED"}:
                raise ValueError(f"Estado de fibra inválido: {fiber.state}")
            if len(fiber.registers) != 16:
                raise ValueError("Registros de fibra inválidos")
            if len(fiber.stack) > self.config.stack_limit:
                raise ValueError("Pila de fibra excede el límite")

    def save_snapshot(self, path: str | Path) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.snapshot_bytes())
        return destination

    def load_snapshot(self, path: str | Path) -> None:
        self.restore_bytes(Path(path).read_bytes())
