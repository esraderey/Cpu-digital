"""Tramoya VM32: máquina virtual RISC de 32 bits, segura y embebible."""

from __future__ import annotations

import json
import math
import struct
import zlib
from array import array
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from tramoya import Machine, MachineBuilder, MachineError

from .memory import PagedMemory
from . import tnu
from .memory_chip import NonVolatileMemoryChip
from .tnu import MAX_TNU_LENGTH, MAX_TNU_WORK, ROM_BASE, TramoyaNeuralUnit
from .vm32_assembler import Program32, signed32
from .vm32_isa import TNU_FIRST_OPCODE, VM32_ISA, VMInstruction, VMOpcode
from .vm32_loops import CompiledLoop, compile_loop


SNAPSHOT_MAGIC = b"TVMS32\x01"
SNAPSHOT_VERSION = 2
DEFAULT_MEMORY_WORDS = 1_048_576
MAX_MEMORY_WORDS = 16_777_216
MAX_SNAPSHOT_RAW_BYTES = 256 * 1024 * 1024
MAX_MEMORY_CHIP_TRANSFER_BYTES = 4_096
MAX_ROPE_HEAD_SIZE = 256
LOOP_WARMUP = 32  # saltos hacia atrás a una cabecera antes de compilar su bucle (~0,4 ms)
_F32 = struct.Struct("f")
CANONICAL_NAN = 0x7FC00000
_U32 = struct.Struct("I")
RUNNABLE_STATES = frozenset({"READY", "RUNNING", "PAUSED", "WAITING"})
FINAL_STATES = frozenset({"HALTED", "FAULTED"})


def _is_int32(value: object) -> bool:
    return type(value) is int and -(1 << 31) <= value < (1 << 31)


class VMRuntimeError(RuntimeError):
    pass


class _ExecutionFault(VMRuntimeError):
    pass


class _HaltSignal(Exception):
    def __init__(self, code: int):
        self.code = code


class _WaitSignal(Exception):
    pass


class _RetryWaitSignal(_WaitSignal):
    """Espera que reintenta la instrucción: no completa, no cobra gas ni cuenta."""


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
    accelerate_loops: bool = True
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
        npu: TramoyaNeuralUnit | None = None,
    ):
        self.config = config or VMConfig()
        self.memory_chip = memory_chip
        self.npu: TramoyaNeuralUnit | None = None
        self.attach_npu(npu)
        self._trace: deque[VMTraceEntry] = deque(maxlen=self.config.trace_size)
        self._syscalls: dict[int, _Syscall] = {}
        self._program: Program32 | None = None
        self._pending_event: tuple[str, str | int | None] | None = None
        self._journal: dict[int, int] | None = None
        self._undo: list[Callable[[], None]] | None = None
        self._handlers = _handlers_for(type(self))
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

    def attach_npu(self, npu: TramoyaNeuralUnit | None) -> None:
        """Conecta o desconecta el coprocesador TNU (requiere además la capacidad ``npu``)."""
        if npu is not None and not isinstance(npu, TramoyaNeuralUnit):
            raise TypeError("npu debe ser TramoyaNeuralUnit o None")
        self.npu = npu

    @property
    def tnu_config(self) -> tuple[int, int, int]:
        """Configuración vectorial (VL, VR, VS); (0, 0, 0) si no se ejecutó VCFG."""
        return (self._vl, self._vr, self._vs)

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
        self._interrupt_stack: list[tuple[int, dict[str, bool], bool, int]] = []
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
        self._vl = self._vr = self._vs = 0
        self._last_instruction: tuple[int, str, tuple[int, int, int], str] | None = None
        # Acelerador de bucles: cabeceras vistas en saltos hacia atrás y sus
        # cuerpos compilados (None = no acelerable). Son cachés: no entran en
        # el snapshot ni en el checkpoint.
        self._loop_candidates: dict[int, int] = {}
        self._loops: dict[int, CompiledLoop | None] = {}
        self._accelerated_instructions = 0
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
        last = self._last_instruction
        if last is not None:
            pc, opcode_name, operands, detail = last
            target["last_instruction"] = {
                "pc": pc,
                "opcode": opcode_name,
                "operands": list(operands),
                "detail": detail,
            }

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
        self._undo = []

        try:
            # Las interrupciones externas solo se despachan en la fibra principal.
            if self._interrupts_enabled and self._pending_interrupts and self._current_fiber == 0:
                if self._gas_remaining < 1:
                    raise _ExecutionFault("Gas agotado al despachar interrupción")
                pending = self._pending_interrupts
                vector = pending.popleft()
                self._undo.append(lambda: pending.appendleft(vector))
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
            self._handlers[spec.opcode](self, a, b, c)
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
        except _RetryWaitSignal as signal:
            self._pending_event = ("wait", str(signal))
            detail = str(signal)
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
            self._undo = None
            # El diccionario para la consola se materializa en _sync_lifecycle,
            # no en cada instrucción del camino caliente.
            self._last_instruction = (pc_before, opcode_name, operands, detail)
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
        # El checkpoint se crea en cada instrucción y debe costar O(1): solo
        # escalares y referencias. Las colecciones se revierten con el registro
        # de deshacer (self._undo) que llenan las operaciones que las mutan.
        return (
            self._pc,
            self._registers.copy(),
            (self._flags["Z"], self._flags["N"], self._flags["C"], self._flags["O"]),
            self._stack,
            self._input,
            len(self._output),
            self._output_units,
            self._pending_interrupts,
            self._interrupt_vectors,
            self._interrupt_stack,
            self._interrupts_enabled,
            self._heap_ptr,
            self._random_state,
            self._instructions,
            self._cycles,
            self._gas_remaining,
            self._current_fiber,
            self._next_fiber_id,
            self._fibers,
        )

    def _rollback_core(self, snapshot: tuple[Any, ...]) -> None:
        if self._undo:
            for action in reversed(self._undo):
                action()
            self._undo.clear()
        if self._journal:
            for address, old_value in self._journal.items():
                self._memory[address] = old_value
        (
            self._pc,
            self._registers,
            flags,
            self._stack,
            self._input,
            output_length,
            self._output_units,
            self._pending_interrupts,
            self._interrupt_vectors,
            self._interrupt_stack,
            self._interrupts_enabled,
            self._heap_ptr,
            self._random_state,
            self._instructions,
            self._cycles,
            self._gas_remaining,
            self._current_fiber,
            self._next_fiber_id,
            self._fibers,
        ) = snapshot
        self._flags = dict(zip(("Z", "N", "C", "O"), flags, strict=True))
        del self._output[output_length:]
        self._sync_special_registers()

    def _log_undo(self, action: Callable[[], None]) -> None:
        if self._undo is not None:
            self._undo.append(action)

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
        # Tabla de manejadores por clase: una búsqueda en lugar de una cadena de
        # comparaciones. _execute_one la usa directamente; para extender la VM se
        # sobrescriben los métodos _op_*, no _dispatch.
        self._handlers[opcode](self, a, b, c)

    # ── Manejadores por opcode (misma semántica que la cadena original) ──

    def _op_nop(self, a: int, b: int, c: int) -> None:
        return

    def _op_mov(self, a: int, b: int, c: int) -> None:
        self._write_result(a, self._read_register(b))

    def _op_movi(self, a: int, b: int, c: int) -> None:
        self._write_result(a, b)

    def _op_lea(self, a: int, b: int, c: int) -> None:
        self._write_result(a, b)

    def _op_load(self, a: int, b: int, c: int) -> None:
        self._write_result(a, self.read_memory(self._effective_address(b, c)))

    def _op_store(self, a: int, b: int, c: int) -> None:
        self.write_memory(self._effective_address(b, c), self._read_register(a))

    def _op_add(self, a: int, b: int, c: int) -> None:
        self._add(a, self._read_register(b), self._read_register(c))

    def _op_addi(self, a: int, b: int, c: int) -> None:
        self._add(a, self._read_register(b), c)

    def _op_sub(self, a: int, b: int, c: int) -> None:
        self._subtract(a, self._read_register(b), self._read_register(c))

    def _op_subi(self, a: int, b: int, c: int) -> None:
        self._subtract(a, self._read_register(b), c)

    def _op_mul(self, a: int, b: int, c: int) -> None:
        self._write_result(a, self._read_register(b) * self._read_register(c), overflow_check=True)

    def _op_muli(self, a: int, b: int, c: int) -> None:
        self._write_result(a, self._read_register(b) * c, overflow_check=True)

    def _op_div(self, a: int, b: int, c: int) -> None:
        dividend, divisor = self._read_register(b), self._read_register(c)
        if divisor == 0:
            raise _ExecutionFault("División entre cero")
        self._write_result(a, self._quotient(dividend, divisor), overflow_check=True)

    def _op_mod(self, a: int, b: int, c: int) -> None:
        dividend, divisor = self._read_register(b), self._read_register(c)
        if divisor == 0:
            raise _ExecutionFault("División entre cero")
        quotient = self._quotient(dividend, divisor)
        self._write_result(a, dividend - quotient * divisor, overflow_check=True)

    def _op_cmp(self, a: int, b: int, c: int) -> None:
        self._set_sub_flags(self._read_register(a), self._read_register(b))

    def _op_cmpi(self, a: int, b: int, c: int) -> None:
        self._set_sub_flags(self._read_register(a), b)

    def _op_test(self, a: int, b: int, c: int) -> None:
        self._set_flags(self._read_register(a) & self._read_register(b))

    def _op_and(self, a: int, b: int, c: int) -> None:
        self._write_result(a, self._read_register(b) & self._read_register(c))

    def _op_or(self, a: int, b: int, c: int) -> None:
        self._write_result(a, self._read_register(b) | self._read_register(c))

    def _op_xor(self, a: int, b: int, c: int) -> None:
        self._write_result(a, self._read_register(b) ^ self._read_register(c))

    def _op_not(self, a: int, b: int, c: int) -> None:
        self._write_result(a, ~self._read_register(b))

    def _op_shl(self, a: int, b: int, c: int) -> None:
        if not 0 <= c <= 31:
            raise _ExecutionFault(f"Desplazamiento inválido: {c}")
        value = self._read_register(b)
        carry = bool(c and ((value & 0xFFFFFFFF) >> (32 - c)) & 1)
        self._write_result(a, value << c, carry=carry, overflow_check=True)

    def _op_shr(self, a: int, b: int, c: int) -> None:
        if not 0 <= c <= 31:
            raise _ExecutionFault(f"Desplazamiento inválido: {c}")
        value = self._read_register(b)
        carry = bool(c and ((value & 0xFFFFFFFF) >> (c - 1)) & 1)
        self._write_result(a, value >> c, carry=carry)

    def _op_jmp(self, a: int, b: int, c: int) -> None:
        self._jump(a)

    def _op_jz(self, a: int, b: int, c: int) -> None:
        if self._flags["Z"]:
            self._jump(a)

    def _op_jnz(self, a: int, b: int, c: int) -> None:
        if not self._flags["Z"]:
            self._jump(a)

    def _op_jneg(self, a: int, b: int, c: int) -> None:
        if self._flags["N"]:
            self._jump(a)

    def _op_jpos(self, a: int, b: int, c: int) -> None:
        flags = self._flags
        if not flags["Z"] and not flags["N"]:
            self._jump(a)

    def _op_jc(self, a: int, b: int, c: int) -> None:
        if self._flags["C"]:
            self._jump(a)

    def _op_jnc(self, a: int, b: int, c: int) -> None:
        if not self._flags["C"]:
            self._jump(a)

    def _op_jlt(self, a: int, b: int, c: int) -> None:
        flags = self._flags
        if flags["N"] != flags["O"]:
            self._jump(a)

    def _op_jgt(self, a: int, b: int, c: int) -> None:
        flags = self._flags
        if not flags["Z"] and flags["N"] == flags["O"]:
            self._jump(a)

    def _op_push(self, a: int, b: int, c: int) -> None:
        self._push(self._read_register(a))

    def _op_pop(self, a: int, b: int, c: int) -> None:
        self._write_result(a, self._pop())

    def _op_call(self, a: int, b: int, c: int) -> None:
        self._push(self._pc)
        self._jump(a)

    def _op_callr(self, a: int, b: int, c: int) -> None:
        target = self._read_register(a)
        self._push(self._pc)
        self._jump(target)

    def _op_ret(self, a: int, b: int, c: int) -> None:
        self._jump(self._pop())

    def _op_syscall(self, a: int, b: int, c: int) -> None:
        # En run() el contexto Tramoya se sincroniza por bloque. Dar a una
        # syscall host el estado acumulado hasta la instrucción anterior.
        self._sync_lifecycle()
        self._invoke_syscall(a)

    def _op_int(self, a: int, b: int, c: int) -> None:
        self._enter_interrupt(a)

    def _op_iret(self, a: int, b: int, c: int) -> None:
        self._return_interrupt()

    def _op_ei(self, a: int, b: int, c: int) -> None:
        self._interrupts_enabled = True

    def _op_di(self, a: int, b: int, c: int) -> None:
        self._interrupts_enabled = False

    def _op_setiv(self, a: int, b: int, c: int) -> None:
        if "interrupt_control" not in self.config.capabilities:
            raise _ExecutionFault("Capacidad interrupt_control no autorizada")
        try:
            self.set_interrupt_vector(a, b)
        except ValueError as exc:
            raise _ExecutionFault(str(exc)) from exc

    def _op_yield(self, a: int, b: int, c: int) -> None:
        raise _WaitSignal("YIELD cooperativo")

    def _op_break(self, a: int, b: int, c: int) -> None:
        raise _PauseSignal("Instrucción BREAK")

    def _op_halt(self, a: int, b: int, c: int) -> None:
        # HALT indica terminación correcta. Para devolver otro código se
        # usa la syscall 0, que toma el valor explícito de R1.
        raise _HaltSignal(0)

    # ── FPU: punto flotante IEEE 754 single-precision ────────────

    def _op_fadd(self, a: int, b: int, c: int) -> None:
        self._float_to_reg(a, self._float_from_reg(b) + self._float_from_reg(c))

    def _op_fsub(self, a: int, b: int, c: int) -> None:
        self._float_to_reg(a, self._float_from_reg(b) - self._float_from_reg(c))

    def _op_fmul(self, a: int, b: int, c: int) -> None:
        self._float_to_reg(a, self._float_from_reg(b) * self._float_from_reg(c))

    def _op_fdiv(self, a: int, b: int, c: int) -> None:
        divisor = self._float_from_reg(c)
        if divisor == 0.0:
            raise _ExecutionFault("División flotante entre cero")
        self._float_to_reg(a, self._float_from_reg(b) / divisor)

    def _op_fcmp(self, a: int, b: int, c: int) -> None:
        left, right = self._float_from_reg(a), self._float_from_reg(b)
        if math.isnan(left) or math.isnan(right):
            self._flags.update(Z=False, N=False, C=True, O=False)
        else:
            self._flags.update(Z=left == right, N=left < right, C=False, O=False)

    def _op_ftoi(self, a: int, b: int, c: int) -> None:
        fval = self._float_from_reg(b)
        if math.isnan(fval) or math.isinf(fval):
            raise _ExecutionFault("Conversión float→int de NaN o infinito")
        ival = int(fval)
        if not -(1 << 31) <= ival < (1 << 31):
            raise _ExecutionFault("Desbordamiento en conversión float→int")
        self._write_result(a, ival)

    def _op_itof(self, a: int, b: int, c: int) -> None:
        self._float_to_reg(a, float(self._read_register(b)))

    def _op_fabs(self, a: int, b: int, c: int) -> None:
        self._float_to_reg(a, abs(self._float_from_reg(b)))

    def _op_fsqrt(self, a: int, b: int, c: int) -> None:
        fval = self._float_from_reg(b)
        if fval < 0.0:
            raise _ExecutionFault("Raíz cuadrada de número negativo")
        self._float_to_reg(a, math.sqrt(fval))

    # ── Aritmética extendida 64 bits ───────────────────────

    def _op_mulh(self, a: int, b: int, c: int) -> None:
        full = self._read_register(b) * self._read_register(c)
        self._write_result(a, signed32((full >> 32) & 0xFFFFFFFF))

    def _op_addx(self, a: int, b: int, c: int) -> None:
        left, right = self._read_register(b), self._read_register(c)
        carry_in = 1 if self._flags["C"] else 0
        raw = left + right + carry_in
        unsigned = (left & 0xFFFFFFFF) + (right & 0xFFFFFFFF) + carry_in
        result = signed32(raw)
        overflow = (left >= 0) == (right >= 0) and (result >= 0) != (left >= 0)
        self._write_register(a, result)
        self._set_flags(result, carry=unsigned > 0xFFFFFFFF, overflow=overflow)

    def _op_subx(self, a: int, b: int, c: int) -> None:
        left, right = self._read_register(b), self._read_register(c)
        borrow_in = 0 if self._flags["C"] else 1
        raw = left - right - borrow_in
        result = signed32(raw)
        overflow = (left >= 0) != (right >= 0) and (result >= 0) != (left >= 0)
        borrow_unsigned = (left & 0xFFFFFFFF) >= ((right & 0xFFFFFFFF) + borrow_in)
        self._write_register(a, result)
        self._set_flags(result, carry=borrow_unsigned, overflow=overflow)

    # ── Fibras cooperativas ────────────────────────────────

    def _op_spawn(self, a: int, b: int, c: int) -> None:
        self._spawn_fiber(a)

    def _op_switch(self, a: int, b: int, c: int) -> None:
        self._switch_fiber(self._read_register(a))

    def _op_fret(self, a: int, b: int, c: int) -> None:
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
        flags = self._flags
        flags["Z"] = normalized == 0
        flags["N"] = normalized < 0
        flags["C"] = carry
        flags["O"] = overflow

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
        return _F32.unpack(_U32.pack(self._read_register(index) & 0xFFFFFFFF))[0]

    def _float_to_reg(self, dest: int, value: float) -> None:
        """Empaqueta un float en un registro y actualiza banderas flotantes.

        Un NaN se guarda siempre como el NaN canónico (0x7FC00000, como en
        RISC-V): la carga útil que propaga el host depende del orden de los
        operandos en el código máquina, y CPython lo cambia al especializar la
        operación, así que conservarla rompería el determinismo.
        """
        if value != value:
            self._write_register(dest, CANONICAL_NAN)
            self._set_float_flags(value)
            return
        packed = _F32.pack(value)
        self._write_register(dest, signed32(_U32.unpack(packed)[0]))
        # Las banderas describen el float32 realmente almacenado, no el double previo.
        self._set_float_flags(_F32.unpack(packed)[0])

    def _set_float_flags(self, value: float) -> None:
        """Z=cero, N=negativo, C=NaN, O=infinito."""
        is_nan = math.isnan(value)
        flags = self._flags
        flags["Z"] = value == 0.0 and not is_nan
        flags["N"] = value < 0.0 and not is_nan
        flags["C"] = is_nan
        flags["O"] = math.isinf(value)

    def _push(self, value: int) -> None:
        stack = self._stack
        if len(stack) >= self.config.stack_limit:
            raise _ExecutionFault("Desbordamiento superior de pila")
        stack.append(signed32(value))
        self._log_undo(stack.pop)
        self._sync_special_registers()

    def _pop(self) -> int:
        stack = self._stack
        if not stack:
            raise _ExecutionFault("Desbordamiento inferior de pila")
        value = stack.pop()
        self._log_undo(lambda: stack.append(value))
        self._sync_special_registers()
        return value

    def _validate_executable(self, address: int) -> None:
        if self._program is None or not 0 <= address < self._program.code_size or address % 4:
            raise _ExecutionFault(f"Dirección no ejecutable: {address}")

    def _jump(self, address: int) -> None:
        self._validate_executable(address)
        if address < self._pc:
            candidates = self._loop_candidates
            candidates[address] = candidates.get(address, 0) + 1
        self._pc = address

    def read_memory(self, address: int) -> int:
        if not isinstance(address, int) or not 0 <= address < len(self._memory):
            return self._read_rom_word(address)
        return self._memory[address]

    def _read_rom_word(self, address: object) -> int:
        # Camino frío: solo se llega aquí si la dirección no está en la RAM.
        rom = self._rom_if_enabled()
        if rom is not None and type(address) is int and ROM_BASE <= address < ROM_BASE + rom.words:
            return rom.word(address - ROM_BASE)
        raise _ExecutionFault(f"Lectura fuera de memoria: {address!r}")

    def _rom_if_enabled(self) -> tnu.TensorROM | None:
        if self.npu is None or "npu" not in self.config.capabilities:
            return None
        return self.npu.rom

    def write_memory(self, address: int, value: int) -> None:
        if not isinstance(address, int) or not 0 <= address < len(self._memory):
            if isinstance(address, int) and address >= ROM_BASE and self._rom_if_enabled() is not None:
                raise _ExecutionFault(f"La ROM TNU es de solo lectura: {address}")
            raise _ExecutionFault(f"Escritura fuera de memoria: {address!r}")
        if self.config.protect_code and self._program is not None and address < self._program.code_size:
            raise _ExecutionFault(f"Intento de modificar código protegido en {address}")
        if self._journal is not None and address not in self._journal:
            self._journal[address] = self._memory[address]
        self._memory[address] = signed32(value)
        if self._program is not None and address < self._program.code_size:
            self._decode_cache[address // 4] = None
            self._loops.clear()

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
        self.register_syscall(12, lambda vm: vm._sys_npu_info(), name="npu_info", capability="npu")

    def _sys_exit(self) -> None:
        raise _HaltSignal(self._read_register(1))

    def _sys_read_int(self) -> None:
        queue = self._input
        if not queue:
            self._pc -= 4
            raise _RetryWaitSignal("Esperando entrada para read_int")
        value = queue.popleft()
        self._log_undo(lambda: queue.appendleft(value))
        self._write_result(1, value)

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

    def _sys_npu_info(self) -> None:
        npu = self._require_npu()
        self._write_register(2, npu.rom.words if npu.rom is not None else 0)
        self._write_result(1, ROM_BASE)

    def _require_npu(self) -> TramoyaNeuralUnit:
        if self.npu is None:
            raise _ExecutionFault("No hay un chip TNU conectado")
        return self.npu

    @staticmethod
    def _read_memory_chip_transfer_count(count: int) -> int:
        if not 0 <= count <= MAX_MEMORY_CHIP_TRANSFER_BYTES:
            raise _ExecutionFault(
                f"La transferencia del chip debe estar entre 0 y {MAX_MEMORY_CHIP_TRANSFER_BYTES} bytes"
            )
        return count

    def provide_input(self, *values: int) -> str:
        normalized = [signed32(int(value)) for value in values]
        queue = self._input
        queue.extend(normalized)
        if normalized:
            self._log_undo(lambda: [queue.pop() for _ in normalized])
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
        vectors = self._interrupt_vectors
        if vector in vectors:
            previous = vectors[vector]
            self._log_undo(lambda: vectors.__setitem__(vector, previous))
        else:
            self._log_undo(lambda: vectors.pop(vector, None))
        vectors[vector] = address

    def request_interrupt(self, vector: int) -> None:
        if not 0 <= vector <= 255:
            raise ValueError("Vector de interrupción inválido")
        pending = self._pending_interrupts
        pending.append(vector)
        self._log_undo(pending.pop)

    def _enter_interrupt(self, vector: int) -> None:
        if self._current_fiber != 0:
            raise _ExecutionFault("Las interrupciones solo se atienden en la fibra principal")
        if len(self._interrupt_stack) >= self.config.interrupt_depth:
            raise _ExecutionFault("Profundidad máxima de interrupciones excedida")
        if vector not in self._interrupt_vectors:
            raise _ExecutionFault(f"Vector de interrupción {vector} no configurado")
        target = self._interrupt_vectors[vector]
        self._validate_executable(target)
        frames = self._interrupt_stack
        frames.append((self._pc, dict(self._flags), self._interrupts_enabled, self._current_fiber))
        self._log_undo(frames.pop)
        self._pc = target
        self._interrupts_enabled = False

    def _return_interrupt(self) -> None:
        frames = self._interrupt_stack
        if not frames:
            raise _ExecutionFault("IRET sin interrupción activa")
        if frames[-1][3] != self._current_fiber:
            raise _ExecutionFault("IRET desde una fibra distinta de la interrumpida")
        frame = frames.pop()
        self._log_undo(lambda: frames.append(frame))
        pc, flags, enabled, _fiber = frame
        self._validate_executable(pc)
        self._pc = pc
        self._flags = dict(flags)
        self._interrupts_enabled = enabled

    # ── Fibras cooperativas ────────────────────────────────────────

    def _spawn_fiber(self, address: int) -> None:
        """Crea una nueva fibra en la dirección dada. Devuelve su ID en R1."""
        live = sum(1 for f in self._fibers.values() if f.fiber_id != 0 and f.state != "FINISHED")
        if live >= self.config.fiber_limit:
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
        self._store_fiber(fiber)
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
        fibers = self._fibers
        fiber = fibers.get(self._current_fiber)
        parent_id = fiber.parent_id if fiber is not None else 0
        parent = fibers.get(parent_id)
        if parent is None or parent.state == "FINISHED":
            parent_id = 0
            parent = fibers.get(parent_id)
        if parent is None:
            raise _ExecutionFault("No hay fibra padre a la cual regresar")
        self._load_fiber_context(parent_id)
        if fiber is not None:
            # La fibra terminada se retira: no cuenta para fiber_limit y
            # SWITCH hacia ella falla como fibra inexistente.
            del fibers[fiber.fiber_id]
            self._log_undo(lambda: fibers.__setitem__(fiber.fiber_id, fiber))

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
            self._store_fiber(fiber)
        else:
            self._remember_fiber(fiber)
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
        self._remember_fiber(fiber)
        fiber.state = "RUNNING"
        self._current_fiber = fiber_id
        self._sync_special_registers()

    def _store_fiber(self, fiber: _FiberContext) -> None:
        fibers = self._fibers
        previous = fibers.get(fiber.fiber_id)
        fibers[fiber.fiber_id] = fiber
        if previous is None:
            self._log_undo(lambda: fibers.pop(fiber.fiber_id, None))
        else:
            self._log_undo(lambda: fibers.__setitem__(fiber.fiber_id, previous))

    def _remember_fiber(self, fiber: _FiberContext) -> None:
        # Los campos de una fibra se reasignan, nunca se mutan en sitio, así que
        # guardar las referencias basta para deshacer el cambio.
        saved = (fiber.pc, fiber.registers, fiber.flags, fiber.stack, fiber.state, fiber.parent_id)

        def restore() -> None:
            fiber.pc, fiber.registers, fiber.flags, fiber.stack, fiber.state, fiber.parent_id = saved

        self._log_undo(restore)

    # ── TNU: coprocesador vectorial ────────────────────────────────
    # Orden fijo en cada instrucción: permisos → parámetros → tope de trabajo →
    # gas → lectura de regiones → cálculo → una única escritura con deshacer.
    # Hasta la escritura no hay efectos; la escritura registra el contenido
    # previo en self._undo, así que cualquier fallo se revierte completo.

    def _execute_tnu(self, opcode: VMOpcode, a: int, b: int, c: int) -> None:
        if "npu" not in self.config.capabilities:
            raise _ExecutionFault("Capacidad npu no autorizada")
        self._require_npu()
        if opcode == VMOpcode.VCFG:
            self._tnu_configure(self._read_register(a), self._read_register(b), self._read_register(c))
            return
        vl = self._vl
        if not vl:
            raise _ExecutionFault("VCFG no configurado: VL = 0")
        reg = self._read_register

        # El destino se valida antes de cobrar y calcular: un destino inválido no
        # hace trabajo de host que luego el rollback devolvería como gas no gastado.
        if opcode in {VMOpcode.VADD, VMOpcode.VMUL}:
            destination = self._tnu_destination(reg(a), vl)
            self._tnu_charge(opcode, 2 * vl)
            left, right = self._tnu_floats(reg(b), vl), self._tnu_floats(reg(c), vl)
            kernel = tnu.vadd if opcode == VMOpcode.VADD else tnu.vmul
            self._tnu_store(destination, kernel(left, right))
        elif opcode == VMOpcode.VSCALE:
            destination = self._tnu_destination(reg(a), vl)
            self._tnu_charge(opcode, 2 * vl)
            self._tnu_store(destination, tnu.vscale(self._tnu_floats(reg(b), vl), self._float_from_reg(c)))
        elif opcode == VMOpcode.VCOPY:
            destination = self._tnu_destination(reg(a), vl)
            self._tnu_charge(opcode, vl)
            self._tnu_store(destination, bytes(self._tnu_bytes(reg(b), vl)))
        elif opcode == VMOpcode.FDOT:
            self._tnu_charge(opcode, vl)
            value = tnu.fdot(self._tnu_floats(reg(b), vl), self._tnu_floats(reg(c), vl))
            self._float_to_reg(a, array("f", [value])[0])
        elif opcode in {VMOpcode.MATVEC, VMOpcode.MATTV}:
            rows, stride = self._tnu_rows(), self._vs or vl
            span = (rows - 1) * stride + vl
            is_matvec = opcode == VMOpcode.MATVEC
            destination = self._tnu_destination(reg(a), rows if is_matvec else vl)
            # Se cobra lo que se toca: MAC más un coste fijo por producto (fila de
            # MATVEC, columna de MATTV) y, si el paso es disperso, la región entera.
            products = rows if is_matvec else vl
            self._tnu_charge(opcode, max(rows * vl + tnu.TNU_ROW_UNITS * products, span))
            weights = self._tnu_floats(reg(b), span)
            if is_matvec:
                result = tnu.matvec(weights, rows, vl, stride, self._tnu_floats(reg(c), vl))
            else:
                result = tnu.mattv(weights, rows, vl, stride, self._tnu_floats(reg(c), rows))
            self._tnu_store(destination, result)
        elif opcode == VMOpcode.RMSNORM:
            destination = self._tnu_destination(reg(a), vl)
            self._tnu_charge(opcode, 3 * vl)
            self._tnu_store(destination, tnu.rmsnorm(self._tnu_floats(reg(b), vl), self._tnu_floats(reg(c), vl)))
        elif opcode in {VMOpcode.VSOFTMAX, VMOpcode.VEXP, VMOpcode.VSILU}:
            destination = self._tnu_destination(reg(a), vl)
            self._tnu_charge(opcode, 5 * vl if opcode == VMOpcode.VSOFTMAX else 4 * vl)
            kernel = {VMOpcode.VSOFTMAX: tnu.softmax, VMOpcode.VEXP: tnu.vexp, VMOpcode.VSILU: tnu.silu}[opcode]
            self._tnu_store(destination, kernel(self._tnu_floats(reg(b), vl)))
        elif opcode == VMOpcode.ROPE:
            position, head_size = reg(b), reg(c)
            if position < 0:
                raise _ExecutionFault(f"Posición ROPE inválida: {position}")
            if not 2 <= head_size <= MAX_ROPE_HEAD_SIZE or head_size % 2 or vl % head_size:
                raise _ExecutionFault(f"Tamaño de cabeza ROPE inválido: {head_size} con VL={vl}")
            destination = self._tnu_destination(reg(a), vl)
            self._tnu_charge(opcode, 4 * vl)
            self._tnu_store(destination, tnu.rope(self._tnu_floats(destination, vl), position, head_size))
        elif opcode == VMOpcode.VARGMAX:
            self._tnu_charge(opcode, 2 * vl)
            self._write_result(a, tnu.argmax(self._tnu_floats(reg(b), vl)))
        elif opcode == VMOpcode.VSAMPLE:
            self._tnu_charge(opcode, 2 * vl)
            self._write_result(a, tnu.sample(self._tnu_floats(reg(b), vl), self._float_from_reg(c)))
        elif opcode == VMOpcode.VQUANT:
            self._tnu_require_q8(vl)
            destination = self._tnu_destination(reg(a), vl // 4 + 1)
            self._tnu_charge(opcode, 3 * vl)
            try:
                levels, scale = tnu.quantize(self._tnu_floats(reg(b), vl))
            except ValueError as exc:
                raise _ExecutionFault(str(exc)) from exc
            self._tnu_store(destination, levels + array("f", [scale]).tobytes())
        elif opcode == VMOpcode.QMATVEC:
            self._tnu_require_q8(vl)
            rows = self._tnu_rows()
            destination = self._tnu_destination(reg(a), rows)
            self._tnu_charge(opcode, rows * vl + tnu.TNU_ROW_UNITS * rows)
            matrix = self._tnu_bytes(reg(b), rows * vl // 4 + rows)
            vector = self._tnu_bytes(reg(c), vl // 4 + 1)
            result = tnu.qmatvec(
                matrix[:rows * vl], matrix[rows * vl:].cast("f"), rows, vl,
                vector[:vl], vector[vl:].cast("f")[0],
            )
            self._tnu_store(destination, result)
        elif opcode == VMOpcode.QROW:
            self._tnu_require_q8(vl)
            rows, row, base = self._tnu_rows(), reg(c), reg(b)
            if not 0 <= row < rows:
                raise _ExecutionFault(f"Fila Q8 fuera de rango: {row} (VR={rows})")
            destination = self._tnu_destination(reg(a), vl)
            self._tnu_charge(opcode, 2 * vl)
            # Se valida el rango de la matriz completa sin leerla: solo se tocan la
            # fila pedida y su escala, que es el trabajo cobrado.
            self._tnu_region(base, rows * vl // 4 + rows)
            row_bytes = self._tnu_bytes(base + row * vl // 4, vl // 4)
            scale = self._tnu_floats(base + rows * vl // 4 + row, 1)[0]
            self._tnu_store(destination, tnu.qrow(row_bytes, scale))
        else:
            raise _ExecutionFault(f"Opcode TNU no implementado: {int(opcode)}")

    def _tnu_configure(self, vl: int, vr: int, vs: int) -> None:
        if not 1 <= vl <= MAX_TNU_LENGTH or not 0 <= vr <= MAX_TNU_LENGTH or not 0 <= vs <= MAX_TNU_LENGTH:
            raise _ExecutionFault(f"VCFG inválido: VL={vl}, VR={vr}, VS={vs}")
        previous = (self._vl, self._vr, self._vs)

        def restore() -> None:
            self._vl, self._vr, self._vs = previous

        self._log_undo(restore)
        self._vl, self._vr, self._vs = vl, vr, vs

    def _tnu_rows(self) -> int:
        if self._vr < 1:
            raise _ExecutionFault("La operación de matriz requiere VR ≥ 1")
        return self._vr

    def _tnu_require_q8(self, vl: int) -> None:
        if vl % 4:
            raise _ExecutionFault(f"Las operaciones Q8 requieren VL múltiplo de 4 (VL={vl})")
        if self._vs:
            raise _ExecutionFault("Las operaciones Q8 requieren VS = 0")

    def _tnu_charge(self, opcode: VMOpcode, units: int) -> None:
        if units > MAX_TNU_WORK:
            raise _ExecutionFault(f"Trabajo TNU excede el límite por instrucción ({units} > {MAX_TNU_WORK})")
        extra = tnu.gas_extra(units)
        required = VM32_ISA[int(opcode)].cost + extra
        if self._gas_remaining < required:
            raise _ExecutionFault(f"Gas agotado: {opcode.name} requiere {required}")
        # El coste base lo cobra _execute_one al terminar; el checkpoint revierte ambos.
        self._gas_remaining -= extra
        self._cycles += extra

    def _tnu_region(self, address: int, count: int) -> tnu.TensorROM | None:
        """Valida una región de ``count`` palabras entera en RAM (devuelve None) o en ROM."""
        if 0 <= address and address + count <= len(self._memory):
            return None
        rom = self._rom_if_enabled()
        if rom is not None and ROM_BASE <= address and address + count <= ROM_BASE + rom.words:
            return rom
        raise _ExecutionFault(f"Región TNU fuera de RAM y ROM: [{address}, {address + count})")

    def _tnu_bytes(self, address: int, count: int) -> memoryview:
        """Región de ``count`` palabras en RAM o ROM (nunca a caballo entre ambas)."""
        rom = self._tnu_region(address, count)
        if rom is None:
            return self._memory.view_bytes(address, count)
        return rom.bytes_view(address - ROM_BASE, count)

    def _tnu_floats(self, address: int, count: int) -> memoryview:
        return self._tnu_bytes(address, count).cast("f")

    def _tnu_destination(self, address: int, count: int) -> int:
        """Valida un destino de ``count`` palabras: solo RAM y nunca código protegido."""
        if not (0 <= address and address + count <= len(self._memory)):
            if address + count > ROM_BASE and self._rom_if_enabled() is not None:
                raise _ExecutionFault(f"La ROM TNU es de solo lectura: destino {address}")
            raise _ExecutionFault(f"Destino TNU fuera de la RAM: [{address}, {address + count})")
        program = self._program
        if program is not None and address < program.code_size and self.config.protect_code:
            raise _ExecutionFault(f"Intento de modificar código protegido en {address}")
        return address

    def _tnu_store(self, address: int, payload: array | bytes) -> None:
        data = memoryview(payload).cast("B")
        count = len(data) // 4
        memory = self._memory
        self._tnu_destination(address, count)
        program = self._program
        touches_code = program is not None and address < program.code_size
        previous = bytes(memory.view_bytes(address, count))
        self._log_undo(lambda: memory.write_bytes(address, previous))
        memory.write_bytes(address, data)
        if touches_code:
            last = min(address + count, program.code_size) - 1
            for index in range(address // 4, last // 4 + 1):
                self._decode_cache[index] = None
            self._loops.clear()

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
        # El acelerador de bucles solo actúa sin traza, sin breakpoints y sin
        # interrupciones pendientes: en esos casos cada instrucción debe pasar
        # por el camino instrumentado.
        accelerate = self.config.accelerate_loops and not self.config.trace_size and not breakpoint_set
        candidates = self._loop_candidates
        while self.state == "RUNNING":
            if self._pc in breakpoint_set and self._pc != skip_breakpoint:
                self.pause(f"Breakpoint en PC={self._pc}")
                break
            skip_breakpoint = None
            if max_instructions is not None and self._instructions - executed_at_start >= max_instructions:
                self.pause(f"Límite local de {max_instructions} instrucciones")
                break
            if (
                accelerate
                and candidates.get(self._pc, 0) >= LOOP_WARMUP
                and not (self._interrupts_enabled and self._pending_interrupts)
            ):
                loop = self._compiled_loop(self._pc)
                if loop is not None:
                    budget = (
                        max_instructions - (self._instructions - executed_at_start)
                        if max_instructions is not None
                        else 1 << 62
                    )
                    if self._run_loop(loop, budget):
                        continue
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

    def _compiled_loop(self, header: int) -> CompiledLoop | None:
        try:
            return self._loops[header]
        except KeyError:
            compiled = compile_loop(self, header)
            self._loops[header] = compiled
            return compiled

    def _run_loop(self, loop: CompiledLoop, budget: int) -> bool:
        """Ejecuta iteraciones completas del bucle; False si no avanzó ninguna instrucción."""
        program = self._program
        pc, steps, used = loop.run(
            self, self._registers, self._flags, self._memory, len(self._memory),
            program.code_size if program is not None else 0, self._gas_remaining, budget,
        )
        if not steps:
            return False
        self._pc = pc
        self._instructions += steps
        self._cycles += used
        self._gas_remaining -= used
        self._accelerated_instructions += steps
        backedge_pc, mnemonic, operands = loop.backedge
        self._last_instruction = (backedge_pc, mnemonic, operands, "OK")
        return True

    @property
    def accelerated_instructions(self) -> int:
        """Instrucciones ejecutadas por el acelerador de bucles (ya contadas en instructions)."""
        return self._accelerated_instructions

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
        # Las claves TNU solo aparecen si hay estado o ROM: los snapshots de
        # programas sin TNU no cambian. La ROM nunca se copia, solo su huella.
        if self.tnu_config != (0, 0, 0):
            payload["core"]["tnu"] = {"vl": self._vl, "vr": self._vr, "vs": self._vs}
        if self.npu is not None and self.npu.rom is not None:
            payload["rom"] = self.npu.rom.reference()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return SNAPSHOT_MAGIC + zlib.compress(encoded, level=9)

    def restore_bytes(self, snapshot: bytes) -> None:
        """Restaura un snapshot de forma atómica: si falla, la VM no cambia."""
        payload = self._decode_snapshot_payload(snapshot, MAX_SNAPSHOT_RAW_BYTES)
        self._commit_snapshot(self._parse_snapshot(payload))

    @classmethod
    def from_snapshot_bytes(
        cls,
        snapshot: bytes,
        *,
        trace_size: int | None = None,
        capabilities: frozenset[str] | None = None,
        memory_chip: NonVolatileMemoryChip | None = None,
        limits: VMConfig | None = None,
        npu: TramoyaNeuralUnit | None = None,
        accelerate_loops: bool = True,
    ) -> "TramoyaVM32":
        """Crea una VM con la RAM que requiere el snapshot.

        Las capacidades las decide el host: sin ``capabilities`` se usan las de
        ``VMConfig()``, nunca las embebidas en el snapshot. Con ``limits``, cada
        límite de recursos del snapshot debe caber en el del host.
        """
        payload = cls._decode_snapshot_payload(snapshot, MAX_SNAPSHOT_RAW_BYTES)
        config_data = payload.get("config")
        if not isinstance(config_data, Mapping):
            raise ValueError("Snapshot sin configuración válida")
        defaults = VMConfig()
        if capabilities is None:
            capabilities = defaults.capabilities

        def config_integer(name: str, default: int | None = None) -> int:
            value = config_data.get(name, default)
            if type(value) is not int:
                raise ValueError(f"{name} inválido en snapshot")
            if limits is not None and name != "memory_words" and value > getattr(limits, name):
                raise ValueError(
                    f"{name} del snapshot ({value}) excede el límite del host ({getattr(limits, name)})"
                )
            return value

        protect_code = config_data.get("protect_code", defaults.protect_code)
        if type(protect_code) is not bool:
            raise ValueError("protect_code inválido en snapshot")
        if limits is not None and limits.protect_code and not protect_code:
            raise ValueError("El snapshot desactiva protect_code y el host lo exige")
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
                accelerate_loops=accelerate_loops,
                capabilities=frozenset(capabilities),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Configuración inválida en snapshot: {exc}") from exc
        vm = cls(config, memory_chip=memory_chip, npu=npu)
        vm._commit_snapshot(vm._parse_snapshot(payload))
        return vm

    @staticmethod
    def _decode_snapshot_payload(snapshot: bytes, maximum: int) -> Mapping[str, Any]:
        if not snapshot.startswith(SNAPSHOT_MAGIC):
            raise ValueError("Firma de snapshot VM32 inválida")
        decompressor = zlib.decompressobj()
        try:
            raw = decompressor.decompress(snapshot[len(SNAPSHOT_MAGIC):], maximum + 1)
        except zlib.error as exc:
            raise ValueError("Snapshot VM32 corrupto") from exc
        if len(raw) > maximum or decompressor.unconsumed_tail or not decompressor.eof:
            raise ValueError("Snapshot VM32 excede el tamaño permitido o está truncado")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError("JSON de snapshot VM32 inválido") from exc
        if not isinstance(payload, dict):
            raise ValueError("Estructura de snapshot VM32 inválida")
        return payload

    def _parse_snapshot(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Valida un snapshot decodificado contra esta VM sin modificarla."""
        try:
            return self._parse_snapshot_fields(payload)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("Contenido de snapshot VM32 inválido") from exc

    def _parse_snapshot_fields(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        config = self.config
        version = payload.get("version")
        if version not in {1, SNAPSHOT_VERSION}:
            raise ValueError("Versión de snapshot VM32 no compatible")
        snapshot_config = payload.get("config")
        if not isinstance(snapshot_config, Mapping) or snapshot_config.get("memory_words") != config.memory_words:
            raise ValueError("El snapshot requiere otro tamaño de memoria")
        core = payload.get("core")
        program_data = payload.get("program")
        lifecycle = payload.get("lifecycle")
        if not isinstance(core, Mapping) or not isinstance(program_data, Mapping) or not isinstance(lifecycle, dict):
            raise ValueError("Estructura de snapshot incompleta")

        if version == 1:
            dense_memory = core.get("memory")
            if not isinstance(dense_memory, list) or len(dense_memory) != config.memory_words:
                raise ValueError("Memoria densa inválida en snapshot")
            if not all(_is_int32(value) for value in dense_memory):
                raise ValueError("Valor de memoria fuera de int32")
            memory = PagedMemory(config.memory_words)
            memory.write_block(0, dense_memory)
        else:
            page_words = core.get("memory_page_words", 4096)
            if type(page_words) is not int or not 256 <= page_words <= 65_536:
                raise ValueError("Tamaño de página inválido")
            memory = PagedMemory.from_snapshot(config.memory_words, core.get("memory_pages"), page_words=page_words)

        def int32_list(value: object, name: str) -> list[int]:
            if not isinstance(value, list) or not all(_is_int32(item) for item in value):
                raise ValueError(f"{name} inválida en snapshot")
            return list(value)

        def flag_map(value: object, name: str) -> dict[str, bool]:
            if not isinstance(value, Mapping) or set(value) != {"Z", "N", "C", "O"} or not all(
                type(item) is bool for item in value.values()
            ):
                raise ValueError(f"{name} inválidas en snapshot")
            return dict(value)

        registers = int32_list(core.get("registers"), "Registros")
        if len(registers) != 16:
            raise ValueError("Registros inválidos en snapshot")
        if not all(type(program_data.get(field)) is int for field in ("entry", "code_size", "data_size")):
            raise ValueError("Metadatos de programa inválidos")
        total = program_data["code_size"] + program_data["data_size"]
        if not 0 < total <= config.memory_words:
            raise ValueError("Tamaño de programa inválido")
        symbols = program_data.get("symbols", {})
        if not isinstance(symbols, dict) or not all(
            isinstance(name, str) and type(value) is int for name, value in symbols.items()
        ):
            raise ValueError("Tabla de símbolos inválida en snapshot")
        source_name = program_data.get("source_name", "<snapshot>")
        if not isinstance(source_name, str):
            raise ValueError("Nombre de programa inválido en snapshot")
        program = Program32(
            words=memory.read_block(0, total),
            entry=program_data["entry"],
            code_size=program_data["code_size"],
            data_size=program_data["data_size"],
            symbols=dict(symbols),
            source_name=source_name,
        )

        def executable(address: object) -> bool:
            return type(address) is int and 0 <= address < program.code_size and address % 4 == 0

        flags = flag_map(core.get("flags"), "Banderas")
        stack = int32_list(core.get("stack"), "Pila")
        input_values = int32_list(core.get("input"), "Entrada")
        output = core.get("output")
        if not isinstance(output, list) or not all(isinstance(value, str) or _is_int32(value) for value in output):
            raise ValueError("Salida inválida en snapshot")

        vectors_raw = core.get("interrupt_vectors")
        if not isinstance(vectors_raw, Mapping):
            raise ValueError("Vectores de interrupción inválidos")
        interrupt_vectors: dict[int, int] = {}
        for key, target in vectors_raw.items():
            if not (type(key) is int or (isinstance(key, str) and key.isdigit())) or type(target) is not int:
                raise ValueError("Vectores de interrupción inválidos")
            vector = int(key)
            if not 0 <= vector <= 255:
                raise ValueError("Vector de interrupción inválido")
            if not executable(target):
                raise ValueError(f"Dirección no ejecutable: {target}")
            interrupt_vectors[vector] = target
        pending_interrupts = int32_list(core.get("pending_interrupts"), "Cola de interrupciones")
        if any(not 0 <= vector <= 255 for vector in pending_interrupts):
            raise ValueError("Interrupción pendiente inválida")

        frames_raw = core.get("interrupt_stack")
        if not isinstance(frames_raw, list) or len(frames_raw) > config.interrupt_depth:
            raise ValueError("Pila de interrupciones inválida o excede el límite")
        interrupt_stack: list[tuple[int, dict[str, bool], bool, int]] = []
        for item in frames_raw:
            # Los snapshots anteriores guardaban marcos de tres campos (fibra 0).
            if not isinstance(item, list) or len(item) not in {3, 4}:
                raise ValueError("Contexto de interrupción inválido")
            pc, saved_flags, enabled = item[:3]
            frame_fiber = item[3] if len(item) == 4 else 0
            if type(pc) is not int or type(enabled) is not bool or type(frame_fiber) is not int:
                raise ValueError("Contexto de interrupción inválido")
            interrupt_stack.append((pc, flag_map(saved_flags, "Banderas de interrupción"), enabled, frame_fiber))

        scalar_names = ("pc", "output_units", "heap_ptr", "random_state", "instructions", "cycles", "gas_remaining")
        if not all(type(core.get(name)) is int for name in scalar_names):
            raise ValueError("Contadores inválidos en snapshot")
        if type(core.get("interrupts_enabled")) is not bool:
            raise ValueError("Estado de interrupciones inválido")

        fibers_raw = core.get("fibers", [])
        # La fibra 0 (contexto de la principal) no cuenta para el límite.
        if not isinstance(fibers_raw, list) or len(fibers_raw) > config.fiber_limit + 1:
            raise ValueError("Fibras inválidas o exceden el límite")
        fibers: dict[int, _FiberContext] = {}
        for data in fibers_raw:
            if not isinstance(data, Mapping):
                raise ValueError("Fibra inválida en snapshot")
            fiber_id, fiber_pc, parent_id = data.get("fiber_id"), data.get("pc"), data.get("parent_id")
            state = data.get("state")
            if type(fiber_id) is not int or fiber_id < 0 or fiber_id in fibers:
                raise ValueError("Identificador de fibra inválido o duplicado")
            if type(parent_id) is not int or state not in {"READY", "RUNNING", "FINISHED"}:
                raise ValueError(f"Estado de fibra inválido: {fiber_id}")
            # Una fibra que cedió con SWITCH como última instrucción guarda pc == code_size.
            resumable = executable(fiber_pc) or fiber_pc == program.code_size
            if type(fiber_pc) is not int or (state != "FINISHED" and not resumable):
                raise ValueError(f"PC de fibra inválido: {fiber_id}")
            fiber_registers = int32_list(data.get("registers"), "Registros de fibra")
            fiber_stack = int32_list(data.get("stack"), "Pila de fibra")
            if len(fiber_registers) != 16 or len(fiber_stack) > config.stack_limit:
                raise ValueError(f"Contexto de fibra inválido: {fiber_id}")
            fibers[fiber_id] = _FiberContext(
                fiber_id=fiber_id,
                pc=fiber_pc,
                registers=fiber_registers,
                flags=flag_map(data.get("flags"), "Banderas de fibra"),
                stack=fiber_stack,
                state=state,
                parent_id=parent_id,
            )
        if sum(1 for f in fibers.values() if f.fiber_id != 0 and f.state != "FINISHED") > config.fiber_limit:
            raise ValueError("Fibras inválidas o exceden el límite")
        current_fiber = core.get("current_fiber", 0)
        next_fiber_id = core.get("next_fiber_id", 1)
        if type(current_fiber) is not int or (current_fiber != 0 and current_fiber not in fibers):
            raise ValueError("Fibra actual inválida en snapshot")
        if type(next_fiber_id) is not int or next_fiber_id <= max(fibers, default=0):
            raise ValueError("Siguiente identificador de fibra inválido")

        state_name = lifecycle.get("state")
        if state_name not in FINAL_STATES and not executable(core["pc"]):
            raise ValueError(f"Dirección no ejecutable: {core['pc']}")
        if len(stack) > config.stack_limit:
            raise ValueError("Pila excede el límite configurado")
        if not len(program.words) <= core["heap_ptr"] <= config.memory_words:
            raise ValueError("Puntero de heap inválido")
        if not 0 <= core["gas_remaining"] <= config.gas_limit:
            raise ValueError("Gas inválido")
        if core["instructions"] < 0 or core["cycles"] < 0:
            raise ValueError("Contadores de ejecución inválidos")
        expected_output_units = sum(len(value) if isinstance(value, str) else 1 for value in output)
        if core["output_units"] != expected_output_units or not 0 <= expected_output_units <= config.output_limit:
            raise ValueError("Salida excede el límite configurado")

        tnu_state = core.get("tnu", {"vl": 0, "vr": 0, "vs": 0})
        if not isinstance(tnu_state, Mapping) or set(tnu_state) != {"vl", "vr", "vs"}:
            raise ValueError("Estado TNU inválido en snapshot")
        tnu_config = (tnu_state["vl"], tnu_state["vr"], tnu_state["vs"])
        if not all(type(value) is int for value in tnu_config) or not (
            tnu_config == (0, 0, 0)
            or (
                1 <= tnu_config[0] <= MAX_TNU_LENGTH
                and 0 <= tnu_config[1] <= MAX_TNU_LENGTH
                and 0 <= tnu_config[2] <= MAX_TNU_LENGTH
            )
        ):
            raise ValueError("Configuración TNU inválida en snapshot")
        rom_reference = payload.get("rom")
        if rom_reference is not None:
            # El snapshot solo nombra la ROM; nunca se abre la ruta que trae. El
            # host conecta la ROM y aquí se exige que su huella coincida.
            if not isinstance(rom_reference, Mapping) or not isinstance(rom_reference.get("sha256"), str):
                raise ValueError("Referencia de ROM TNU inválida en snapshot")
            attached = self.npu.rom if self.npu is not None else None
            if attached is None or attached.sha256 != rom_reference["sha256"]:
                raise ValueError(f"El snapshot requiere la ROM TNU sha256={rom_reference['sha256']}")

        return {
            "lifecycle": lifecycle,
            "program": program,
            "memory": memory,
            "registers": registers,
            "flags": flags,
            "pc": core["pc"],
            "stack": stack,
            "input": input_values,
            "output": list(output),
            "output_units": expected_output_units,
            "interrupt_vectors": interrupt_vectors,
            "pending_interrupts": pending_interrupts,
            "interrupt_stack": interrupt_stack,
            "interrupts_enabled": core["interrupts_enabled"],
            "heap_ptr": core["heap_ptr"],
            "random_state": core["random_state"],
            "instructions": core["instructions"],
            "cycles": core["cycles"],
            "gas_remaining": core["gas_remaining"],
            "fibers": fibers,
            "current_fiber": current_fiber,
            "next_fiber_id": next_fiber_id,
            "tnu": tnu_config,
        }

    def _commit_snapshot(self, restored: Mapping[str, Any]) -> None:
        # load_dict es atómico y es el único paso que puede fallar: si lo hace,
        # todavía no se ha tocado ningún campo de la VM.
        try:
            self.machine.load_dict(restored["lifecycle"])
        except MachineError:
            raise
        except Exception as exc:
            raise ValueError("Ciclo de vida de snapshot inválido") from exc
        self._program = restored["program"]
        self._memory = restored["memory"]
        self._registers = restored["registers"]
        self._flags = restored["flags"]
        self._pc = restored["pc"]
        self._stack = restored["stack"]
        self._input = deque(restored["input"])
        self._output = restored["output"]
        self._output_units = restored["output_units"]
        self._interrupt_vectors = restored["interrupt_vectors"]
        self._pending_interrupts = deque(restored["pending_interrupts"])
        self._interrupt_stack = restored["interrupt_stack"]
        self._interrupts_enabled = restored["interrupts_enabled"]
        self._heap_ptr = restored["heap_ptr"]
        self._random_state = restored["random_state"]
        self._instructions = restored["instructions"]
        self._cycles = restored["cycles"]
        self._gas_remaining = restored["gas_remaining"]
        self._fibers = restored["fibers"]
        self._current_fiber = restored["current_fiber"]
        self._next_fiber_id = restored["next_fiber_id"]
        self._vl, self._vr, self._vs = restored["tnu"]
        self._pending_event = None
        self._build_decode_cache()
        # Las cachés del acelerador y la última instrucción describen el programa
        # anterior: el snapshot trae su propio código y su propio last_instruction.
        self._loops.clear()
        self._loop_candidates.clear()
        self._last_instruction = None
        self._sync_special_registers()
        self._trace.clear()
        self._trace_sequence = 0

    def save_snapshot(self, path: str | Path) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.snapshot_bytes())
        return destination

    def load_snapshot(self, path: str | Path) -> None:
        self.restore_bytes(Path(path).read_bytes())


_HANDLER_TABLES: dict[type, dict[int, Callable[..., None]]] = {}


def _handlers_for(cls: type) -> dict[int, Callable[..., None]]:
    """Tabla opcode → manejador de ``cls`` (respeta los ``_op_*`` sobrescritos)."""
    table = _HANDLER_TABLES.get(cls)
    if table is None:
        table = _HANDLER_TABLES[cls] = _build_handlers(cls)
    return table


def _build_handlers(cls: type) -> dict[int, Callable[..., None]]:
    table: dict[int, Callable[..., None]] = {}
    def tnu_handler(opcode: VMOpcode) -> Callable[..., None]:
        def handler(vm: TramoyaVM32, a: int, b: int, c: int) -> None:
            vm._execute_tnu(opcode, a, b, c)

        return handler

    for opcode in VMOpcode:
        if int(opcode) >= TNU_FIRST_OPCODE:
            table[opcode] = tnu_handler(opcode)
        else:
            table[opcode] = getattr(cls, f"_op_{opcode.name.lower()}")
    return table
