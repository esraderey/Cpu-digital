"""CPU educativa de 16 bits impulsada por una máquina de estados Tramoya."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from tramoya import Machine, MachineBuilder, MachineError

from .isa import ISA, Instruction, Opcode


ACTIVE_STATES = frozenset({"FETCH", "DECODE", "EXECUTE"})
TERMINAL_STATES = frozenset({"HALT", "FAULT"})

_ADDRESS_OPS = frozenset(
    {
        Opcode.LOAD,
        Opcode.ADD,
        Opcode.SUB,
        Opcode.MUL,
        Opcode.DIV,
        Opcode.MOD,
        Opcode.SAVE,
        Opcode.CMP,
        Opcode.AND,
        Opcode.OR,
        Opcode.XOR,
    }
)
_TARGET_OPS = frozenset(
    {Opcode.JMP, Opcode.JZ, Opcode.JNZ, Opcode.JNEG, Opcode.JPOS, Opcode.JLT, Opcode.JGT, Opcode.CALL}
)


@dataclass(frozen=True, slots=True)
class TraceEntry:
    sequence: int
    trigger: str
    source: str
    destination: str
    cycles: int
    pc: int
    ir: int | None
    instruction: str
    operand: int | None
    acc: int
    zero: bool
    negative: bool
    overflow: bool
    stack_depth: int
    detail: str

    def format(self) -> str:
        flags = f"Z={int(self.zero)} N={int(self.negative)} O={int(self.overflow)}"
        edge = f"{self.source:7} → {self.destination:7}"
        return (
            f"#{self.sequence:04d} C={self.cycles:04d} {edge} "
            f"PC={self.pc:03d} IR={str(self.ir):>3} ACC={self.acc:6d} "
            f"{flags} SP={self.stack_depth:02d} | {self.detail}"
        )


@dataclass(frozen=True, slots=True)
class RunResult:
    state: str
    cycles: int
    accumulator: int
    output: tuple[int | str, ...]
    fault: str | None
    pause_reason: str | None

    @property
    def ok(self) -> bool:
        return self.state == "HALT"


def _instruction(ctx: Mapping[str, Any]) -> Instruction | None:
    return ISA.get(ctx.get("ir"))


def _valid_address(ctx: Mapping[str, Any], value: Any) -> bool:
    return isinstance(value, int) and 0 <= value < len(ctx["memory"])


def _decode_error(ctx: Mapping[str, Any]) -> str | None:
    instruction = _instruction(ctx)
    if instruction is None:
        return f"Opcode desconocido: {ctx.get('ir')!r}"
    if instruction.operand != "none" and not _valid_address(ctx, ctx.get("pc")):
        return f"Falta el operando de {instruction.mnemonic} en PC={ctx.get('pc')}"
    return None


def _execution_error(ctx: Mapping[str, Any]) -> str | None:
    instruction = _instruction(ctx)
    if instruction is None:
        return f"Opcode desconocido: {ctx.get('ir')!r}"

    opcode = instruction.opcode
    operand = ctx.get("mar")
    if opcode in _ADDRESS_OPS and not _valid_address(ctx, operand):
        return f"Dirección de memoria inválida para {instruction.mnemonic}: {operand!r}"
    if opcode in _TARGET_OPS and not _valid_address(ctx, operand):
        return f"Destino de salto inválido para {instruction.mnemonic}: {operand!r}"

    if opcode in {Opcode.DIV, Opcode.MOD} and ctx["memory"][operand] == 0:
        return f"División entre cero en {instruction.mnemonic}"
    if opcode == Opcode.IN and not ctx["input"]:
        return "La instrucción IN no tiene datos disponibles"
    if opcode in {Opcode.POP, Opcode.RET} and not ctx["stack"]:
        return f"Desbordamiento inferior de pila en {instruction.mnemonic}"
    if opcode == Opcode.RET and not _valid_address(ctx, ctx["stack"][-1]):
        return f"Dirección de retorno inválida: {ctx['stack'][-1]!r}"
    if opcode in {Opcode.PUSH, Opcode.CALL} and len(ctx["stack"]) >= ctx["stack_limit"]:
        return f"Desbordamiento superior de pila en {instruction.mnemonic}"
    return None


class CPU:
    """Procesador acumulador de 16 bits con memoria unificada y pila."""

    def __init__(self, memory_size: int = 256, history_size: int = 512, stack_limit: int = 64):
        if not 16 <= memory_size <= 65_536:
            raise ValueError("memory_size debe estar entre 16 y 65536")
        if history_size < 0:
            raise ValueError("history_size no puede ser negativo")
        if stack_limit < 1:
            raise ValueError("stack_limit debe ser positivo")

        self.memory_size = memory_size
        self.history_size = history_size
        self.stack_limit = stack_limit
        self._trace: list[TraceEntry] = []
        self.machine = self._build_machine(self._fresh_context())

    @property
    def state(self) -> str:
        return self.machine.state

    @property
    def ctx(self) -> dict[str, Any]:
        return self.machine.ctx

    @property
    def trace(self) -> tuple[TraceEntry, ...]:
        return tuple(self._trace)

    @property
    def halted(self) -> bool:
        return self.state == "HALT"

    @property
    def faulted(self) -> bool:
        return self.state == "FAULT"

    @property
    def paused(self) -> bool:
        return self.state == "PAUSED"

    def _fresh_context(self) -> dict[str, Any]:
        return {
            "memory": [0] * self.memory_size,
            "pc": 0,
            "acc": 0,
            "ir": None,
            "mar": None,
            "flags": {"Z": True, "N": False, "O": False},
            "stack": [],
            "stack_limit": self.stack_limit,
            "input": [],
            "output": [],
            "cycles": 0,
            "program_start": 0,
            "program_size": 0,
            "fault": None,
            "halt_reason": None,
            "pause_reason": None,
            "resume_state": "FETCH",
            "last_operation": "CPU reiniciada",
            "last_transition": None,
        }

    def _build_machine(self, context: dict[str, Any]) -> Machine:
        builder = MachineBuilder("FETCH")
        builder.add_states("FETCH", "DECODE", "EXECUTE", "PAUSED", "HALT", "FAULT")

        # El orden importa: Tramoya elige la primera guardia aprobada.
        builder.transition("tick", "FETCH", "FAULT")
        builder.transition("tick", "FETCH", "DECODE")
        builder.transition("tick", "DECODE", "HALT")
        builder.transition("tick", "DECODE", "PAUSED")
        builder.transition("tick", "DECODE", "FAULT")
        builder.transition("tick", "DECODE", "EXECUTE")
        builder.transition("tick", "EXECUTE", "FAULT")
        builder.transition("tick", "EXECUTE", "FETCH")

        # Transiciones globales: ejemplo deliberado del comodín de Tramoya.
        builder.transition("pause", "*", "PAUSED")
        builder.transition("timeout", "*", "FAULT")

        builder.transition("resume", "PAUSED", "FETCH")
        builder.transition("resume", "PAUSED", "DECODE")
        builder.transition("resume", "PAUSED", "EXECUTE")

        @builder.guard("tick", "FETCH", "FAULT")
        def fetch_is_invalid(ctx: Mapping[str, Any]) -> bool:
            return not _valid_address(ctx, ctx.get("pc"))

        @builder.on("tick", "FETCH", "FAULT")
        def fault_during_fetch(ctx: dict[str, Any]) -> None:
            self._clock(ctx)
            self._set_fault(ctx, f"PC fuera de memoria: {ctx.get('pc')!r}")

        @builder.guard("tick", "FETCH", "DECODE")
        def fetch_is_valid(ctx: Mapping[str, Any]) -> bool:
            return _valid_address(ctx, ctx.get("pc"))

        @builder.on("tick", "FETCH", "DECODE")
        def fetch(ctx: dict[str, Any]) -> None:
            self._clock(ctx)
            address = ctx["pc"]
            ctx["ir"] = ctx["memory"][address]
            ctx["pc"] += 1
            ctx["mar"] = None
            instruction = _instruction(ctx)
            name = instruction.mnemonic if instruction else f"OP?({ctx['ir']})"
            ctx["last_operation"] = f"FETCH RAM[{address}] → IR ({name})"

        @builder.guard("tick", "DECODE", "HALT")
        def is_halt(ctx: Mapping[str, Any]) -> bool:
            return ctx.get("ir") == int(Opcode.HALT)

        @builder.on("tick", "DECODE", "HALT")
        def halt(ctx: dict[str, Any]) -> None:
            self._clock(ctx)
            ctx["halt_reason"] = "Instrucción HALT"
            ctx["last_operation"] = "HALT: programa finalizado"

        @builder.guard("tick", "DECODE", "PAUSED")
        def is_breakpoint_instruction(ctx: Mapping[str, Any]) -> bool:
            return ctx.get("ir") == int(Opcode.BREAK)

        @builder.on("tick", "DECODE", "PAUSED")
        def breakpoint_instruction(ctx: dict[str, Any]) -> None:
            self._clock(ctx)
            ctx["resume_state"] = "FETCH"
            ctx["pause_reason"] = "Instrucción BREAK"
            ctx["last_operation"] = "BREAK: ejecución pausada"

        @builder.guard("tick", "DECODE", "FAULT")
        def decode_is_invalid(ctx: Mapping[str, Any]) -> bool:
            return _decode_error(ctx) is not None

        @builder.on("tick", "DECODE", "FAULT")
        def fault_during_decode(ctx: dict[str, Any]) -> None:
            self._clock(ctx)
            self._set_fault(ctx, _decode_error(ctx) or "Error desconocido de decodificación")

        @builder.guard("tick", "DECODE", "EXECUTE")
        def decode_is_valid(ctx: Mapping[str, Any]) -> bool:
            instruction = _instruction(ctx)
            return (
                instruction is not None
                and instruction.opcode not in {Opcode.HALT, Opcode.BREAK}
                and _decode_error(ctx) is None
            )

        @builder.on("tick", "DECODE", "EXECUTE")
        def decode(ctx: dict[str, Any]) -> None:
            self._clock(ctx)
            instruction = _instruction(ctx)
            if instruction is None:  # Defensa adicional; la guardia ya lo impide.
                raise RuntimeError("No se puede decodificar un opcode desconocido")
            if instruction.operand == "none":
                ctx["mar"] = None
                ctx["last_operation"] = f"DECODE {instruction.mnemonic}"
            else:
                address = ctx["pc"]
                raw_operand = ctx["memory"][address]
                # Las palabras se almacenan con signo; direcciones y destinos
                # usan los mismos 16 bits interpretados como valor sin signo.
                ctx["mar"] = (
                    raw_operand & 0xFFFF
                    if instruction.operand in {"address", "target"}
                    else raw_operand
                )
                ctx["pc"] += 1
                ctx["last_operation"] = f"DECODE {instruction.mnemonic} operando={ctx['mar']}"

        @builder.guard("tick", "EXECUTE", "FAULT")
        def execute_is_invalid(ctx: Mapping[str, Any]) -> bool:
            return _execution_error(ctx) is not None

        @builder.on("tick", "EXECUTE", "FAULT")
        def fault_during_execute(ctx: dict[str, Any]) -> None:
            self._clock(ctx)
            self._set_fault(ctx, _execution_error(ctx) or "Error desconocido de ejecución")

        @builder.guard("tick", "EXECUTE", "FETCH")
        def execute_is_valid(ctx: Mapping[str, Any]) -> bool:
            return _execution_error(ctx) is None

        @builder.on("tick", "EXECUTE", "FETCH")
        def execute(ctx: dict[str, Any]) -> None:
            self._clock(ctx)
            self._execute_instruction(ctx)

        @builder.on("pause", "*", "PAUSED")
        def pause(ctx: dict[str, Any]) -> None:
            ctx.pop("_pause_requested", None)
            ctx["last_operation"] = f"PAUSA: {ctx.get('pause_reason') or 'solicitada'}"

        @builder.guard("pause", "*", "PAUSED")
        def pause_is_authorized(ctx: Mapping[str, Any]) -> bool:
            return bool(ctx.get("_pause_requested")) and ctx.get("resume_state") in ACTIVE_STATES

        @builder.on("timeout", "*", "FAULT")
        def timeout(ctx: dict[str, Any]) -> None:
            limit = ctx.get("timeout_limit")
            ctx.pop("_timeout_requested", None)
            self._set_fault(ctx, f"Límite de {limit} ciclos alcanzado")

        @builder.guard("timeout", "*", "FAULT")
        def timeout_is_authorized(ctx: Mapping[str, Any]) -> bool:
            return bool(ctx.get("_timeout_requested"))

        def is_resume_target(target: str):
            def guard(ctx: Mapping[str, Any]) -> bool:
                return ctx.get("resume_state") == target
            return guard

        def resumed(ctx: dict[str, Any]) -> None:
            ctx["pause_reason"] = None
            ctx["last_operation"] = f"Ejecución reanudada en {ctx.get('resume_state')}"

        for target in ("FETCH", "DECODE", "EXECUTE"):
            builder.guard("resume", "PAUSED", target)(is_resume_target(target))
            builder.on("resume", "PAUSED", target)(resumed)

        @builder.enter("HALT")
        def entered_halt(ctx: dict[str, Any]) -> None:
            ctx["fault"] = None

        @builder.enter("FAULT")
        def entered_fault(ctx: dict[str, Any]) -> None:
            ctx["halt_reason"] = None

        @builder.enter("PAUSED")
        def entered_pause(ctx: dict[str, Any]) -> None:
            ctx["pause_reason"] = ctx.get("pause_reason") or "Pausa solicitada"

        @builder.on_transition_handler
        def record_last_transition(trigger: str, source: str, destination: str, ctx: dict[str, Any]) -> None:
            ctx["last_transition"] = {
                "trigger": trigger,
                "source": source,
                "destination": destination,
            }

        @builder.observe
        def trace_transition(trigger: str, source: str, destination: str, ctx: dict[str, Any]) -> None:
            flags = ctx["flags"]
            instruction = _instruction(ctx)
            self._trace.append(
                TraceEntry(
                    sequence=len(self._trace) + 1,
                    trigger=trigger,
                    source=source,
                    destination=destination,
                    cycles=ctx["cycles"],
                    pc=ctx["pc"],
                    ir=ctx["ir"],
                    instruction=instruction.mnemonic if instruction else "?",
                    operand=ctx["mar"],
                    acc=ctx["acc"],
                    zero=flags["Z"],
                    negative=flags["N"],
                    overflow=flags["O"],
                    stack_depth=len(ctx["stack"]),
                    detail=ctx["last_operation"],
                )
            )

        return builder.build(ctx=context, history_size=self.history_size)

    @staticmethod
    def _clock(ctx: dict[str, Any]) -> None:
        ctx["cycles"] += 1

    @staticmethod
    def _set_fault(ctx: dict[str, Any], message: str) -> None:
        ctx["fault"] = message
        ctx["last_operation"] = f"FAULT: {message}"

    @staticmethod
    def _normalize(value: int) -> tuple[int, bool]:
        unsigned = value & 0xFFFF
        signed = unsigned - 0x10000 if unsigned >= 0x8000 else unsigned
        return signed, signed != value

    def _set_acc(self, ctx: dict[str, Any], value: int) -> None:
        normalized, overflow = self._normalize(value)
        ctx["acc"] = normalized
        ctx["flags"].update(Z=normalized == 0, N=normalized < 0, O=overflow)

    def _compare(self, ctx: dict[str, Any], value: int) -> None:
        result, overflow = self._normalize(ctx["acc"] - value)
        ctx["flags"].update(Z=result == 0, N=result < 0, O=overflow)

    @staticmethod
    def _quotient(dividend: int, divisor: int) -> int:
        quotient = abs(dividend) // abs(divisor)
        return -quotient if (dividend < 0) != (divisor < 0) else quotient

    def _execute_instruction(self, ctx: dict[str, Any]) -> None:
        instruction = _instruction(ctx)
        if instruction is None:
            raise RuntimeError("Opcode inválido alcanzó EXECUTE")
        opcode = instruction.opcode
        operand = ctx["mar"]
        memory = ctx["memory"]
        acc = ctx["acc"]

        if opcode == Opcode.NOP:
            pass
        elif opcode == Opcode.LOAD:
            self._set_acc(ctx, memory[operand])
        elif opcode == Opcode.LOADI:
            self._set_acc(ctx, operand)
        elif opcode == Opcode.ADD:
            self._set_acc(ctx, acc + memory[operand])
        elif opcode == Opcode.ADDI:
            self._set_acc(ctx, acc + operand)
        elif opcode == Opcode.SUB:
            self._set_acc(ctx, acc - memory[operand])
        elif opcode == Opcode.SUBI:
            self._set_acc(ctx, acc - operand)
        elif opcode == Opcode.MUL:
            self._set_acc(ctx, acc * memory[operand])
        elif opcode == Opcode.MULI:
            self._set_acc(ctx, acc * operand)
        elif opcode == Opcode.DIV:
            self._set_acc(ctx, self._quotient(acc, memory[operand]))
        elif opcode == Opcode.MOD:
            divisor = memory[operand]
            quotient = self._quotient(acc, divisor)
            self._set_acc(ctx, acc - quotient * divisor)
        elif opcode == Opcode.SAVE:
            memory[operand] = acc
        elif opcode == Opcode.CMP:
            self._compare(ctx, memory[operand])
        elif opcode == Opcode.CMPI:
            self._compare(ctx, operand)
        elif opcode == Opcode.JMP:
            ctx["pc"] = operand
        elif opcode == Opcode.JZ and ctx["flags"]["Z"]:
            ctx["pc"] = operand
        elif opcode == Opcode.JNZ and not ctx["flags"]["Z"]:
            ctx["pc"] = operand
        elif opcode == Opcode.JNEG and ctx["flags"]["N"]:
            ctx["pc"] = operand
        elif opcode == Opcode.JPOS and not ctx["flags"]["Z"] and not ctx["flags"]["N"]:
            ctx["pc"] = operand
        elif opcode == Opcode.JLT and ctx["flags"]["N"] != ctx["flags"]["O"]:
            ctx["pc"] = operand
        elif opcode == Opcode.JGT and not ctx["flags"]["Z"] and ctx["flags"]["N"] == ctx["flags"]["O"]:
            ctx["pc"] = operand
        elif opcode == Opcode.OUT:
            ctx["output"].append(acc)
        elif opcode == Opcode.OUTC:
            ctx["output"].append(chr(acc & 0xFF))
        elif opcode == Opcode.IN:
            self._set_acc(ctx, ctx["input"].pop(0))
        elif opcode == Opcode.PUSH:
            ctx["stack"].append(acc)
        elif opcode == Opcode.POP:
            self._set_acc(ctx, ctx["stack"].pop())
        elif opcode == Opcode.CALL:
            ctx["stack"].append(ctx["pc"])
            ctx["pc"] = operand
        elif opcode == Opcode.RET:
            ctx["pc"] = ctx["stack"].pop()
        elif opcode == Opcode.AND:
            self._set_acc(ctx, (acc & 0xFFFF) & (memory[operand] & 0xFFFF))
        elif opcode == Opcode.OR:
            self._set_acc(ctx, (acc & 0xFFFF) | (memory[operand] & 0xFFFF))
        elif opcode == Opcode.XOR:
            self._set_acc(ctx, (acc & 0xFFFF) ^ (memory[operand] & 0xFFFF))
        elif opcode == Opcode.NOT:
            self._set_acc(ctx, ~(acc & 0xFFFF))
        elif opcode == Opcode.SHL:
            self._set_acc(ctx, acc << 1)
        elif opcode == Opcode.SHR:
            self._set_acc(ctx, acc >> 1)

        ctx["last_operation"] = f"EXECUTE {instruction.mnemonic}"
        if operand is not None:
            ctx["last_operation"] += f" {operand}"
        ctx["last_operation"] += f" → ACC={ctx['acc']}"

    def reset(self, clear_memory: bool = True) -> None:
        memory = [0] * self.memory_size if clear_memory else list(self.ctx["memory"])
        context = self._fresh_context()
        context["memory"] = memory
        self.machine.reset(clear_ctx=True)
        self.machine.ctx.update(context)
        self._trace.clear()

    def load_program(
        self,
        words: Sequence[int],
        start: int = 0,
        inputs: Iterable[int] = (),
        clear_memory: bool = True,
    ) -> None:
        if start < 0 or start + len(words) > self.memory_size:
            raise ValueError(
                f"El programa [{start}, {start + len(words)}) no cabe en {self.memory_size} palabras"
            )
        self.reset(clear_memory=clear_memory)
        normalized = [self._normalize(int(word))[0] for word in words]
        self.ctx["memory"][start:start + len(normalized)] = normalized
        self.ctx["pc"] = start
        self.ctx["program_start"] = start
        self.ctx["program_size"] = len(normalized)
        self.ctx["input"] = [self._normalize(int(value))[0] for value in inputs]
        self.ctx["last_operation"] = f"Programa de {len(normalized)} palabras cargado en {start}"

    def step(self) -> str:
        if self.state not in ACTIVE_STATES:
            return self.state
        return self.machine.trigger("tick")

    def run(self, max_cycles: int = 10_000, breakpoints: Iterable[int] = ()) -> RunResult:
        if max_cycles < 1:
            raise ValueError("max_cycles debe ser positivo")
        breakpoint_set = set(breakpoints)
        deadline = self.ctx["cycles"] + max_cycles

        while self.state in ACTIVE_STATES:
            if self.state == "FETCH" and self.ctx["pc"] in breakpoint_set:
                self.pause(f"Breakpoint en PC={self.ctx['pc']}")
                break
            if self.ctx["cycles"] >= deadline:
                self.machine.trigger(
                    "timeout",
                    timeout_limit=max_cycles,
                    _timeout_requested=True,
                )
                break
            self.step()
        return self.result()

    def result(self) -> RunResult:
        return RunResult(
            state=self.state,
            cycles=self.ctx["cycles"],
            accumulator=self.ctx["acc"],
            output=tuple(self.ctx["output"]),
            fault=self.ctx["fault"],
            pause_reason=self.ctx["pause_reason"],
        )

    def pause(self, reason: str = "Pausa solicitada") -> str:
        if self.state not in ACTIVE_STATES:
            return self.state
        return self.machine.trigger(
            "pause",
            resume_state=self.state,
            pause_reason=reason,
            _pause_requested=True,
        )

    def resume(self) -> str:
        if self.state != "PAUSED":
            return self.state
        return self.machine.trigger("resume")

    def undo(self) -> str:
        try:
            state = self.machine.undo()
        except MachineError:
            raise
        return state

    def snapshot(self) -> str:
        return self.machine.to_json()

    def restore(self, snapshot: str) -> None:
        previous = self.machine.to_dict()
        try:
            data = json.loads(snapshot)
            self._validate_snapshot(data)
            self.machine.load_dict(data)
            self._validate_context()
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, MachineError):
            self.machine.load_dict(previous)
            raise
        self._trace.clear()

    def save_snapshot(self, path: str | Path) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(self.snapshot(), encoding="utf-8")
        return destination

    def load_snapshot(self, path: str | Path) -> None:
        self.restore(Path(path).read_text(encoding="utf-8"))

    def _validate_snapshot(self, data: Any) -> None:
        """Valida el snapshot antes de tocar la máquina (load_dict no ve el contexto CPU)."""
        if not isinstance(data, dict):
            return  # load_dict rechaza lo que no sea un dict
        if "initial" in data and data["initial"] != "FETCH":
            raise ValueError("Estado inicial inválido en snapshot")
        ctx = data.get("ctx", {})
        if isinstance(ctx, dict):
            self._check_context(ctx, data.get("state"))
        history = data.get("history", [])
        if isinstance(history, list):
            for entry in history:
                if isinstance(entry, dict) and isinstance(entry.get("ctx", {}), dict):
                    self._check_context(entry.get("ctx", {}), entry.get("state"), history=True)

    def _validate_context(self) -> None:
        self._check_context(self.ctx, self.state)

    def _check_context(self, ctx: Mapping[str, Any], state: Any, *, history: bool = False) -> None:
        def int_list(name: str, low: int | None, high: int | None) -> list[int]:
            values = ctx.get(name)
            if not isinstance(values, list) or not all(
                type(item) is int and (low is None or low <= item) and (high is None or item <= high)
                for item in values
            ):
                raise ValueError(f"{name} inválida en snapshot")
            return values

        if len(int_list("memory", -32768, 32767)) != self.memory_size:
            raise ValueError("El snapshot no corresponde al tamaño de memoria de esta CPU")
        pc = ctx.get("pc")
        if history:
            if type(pc) is not int or pc < 0:
                raise ValueError("PC inválido en snapshot")
        elif not _valid_address(ctx, pc) and state not in TERMINAL_STATES:
            raise ValueError("PC inválido en snapshot")
        acc = ctx.get("acc")
        if type(acc) is not int or not -32768 <= acc <= 32767:
            raise ValueError("Acumulador inválido en snapshot")
        flags = ctx.get("flags")
        if not isinstance(flags, dict) or set(flags) != {"Z", "N", "O"} or not all(
            type(value) is bool for value in flags.values()
        ):
            raise ValueError("Banderas inválidas en snapshot")
        if type(ctx.get("stack_limit")) is not int or ctx["stack_limit"] != self.stack_limit:
            raise ValueError("stack_limit del snapshot no corresponde a esta CPU")
        if len(int_list("stack", -32768, 65535)) > self.stack_limit:
            raise ValueError("Pila excede el límite configurado")
        int_list("input", None, None)
        output = ctx.get("output")
        if not isinstance(output, list) or not all(type(item) is int or isinstance(item, str) for item in output):
            raise ValueError("Salida inválida en snapshot")
        cycles = ctx.get("cycles")
        if type(cycles) is not int or cycles < 0:
            raise ValueError("Ciclos inválidos en snapshot")
        if ctx.get("resume_state") not in ACTIVE_STATES:
            raise ValueError("resume_state inválido en snapshot")

    def diagram_mermaid(self) -> str:
        return self.machine.to_mermaid()

    def diagram_dot(self, title: str = "cpu_digital") -> str:
        return self.machine.to_dot(title)

    def format_registers(self) -> str:
        flags = self.ctx["flags"]
        instruction = _instruction(self.ctx)
        return (
            f"STATE={self.state} PC={self.ctx['pc']} ACC={self.ctx['acc']} "
            f"IR={self.ctx['ir']}({instruction.mnemonic if instruction else '?'}) "
            f"MAR={self.ctx['mar']} Z={int(flags['Z'])} N={int(flags['N'])} "
            f"O={int(flags['O'])} SP={len(self.ctx['stack'])} CYCLES={self.ctx['cycles']}"
        )

    def format_memory(self, start: int = 0, count: int = 16) -> str:
        if start < 0 or count < 1 or start >= self.memory_size:
            raise ValueError("Rango de memoria inválido")
        end = min(start + count, self.memory_size)
        rows = []
        for row_start in range(start, end, 8):
            values = self.ctx["memory"][row_start:min(row_start + 8, end)]
            rows.append(f"{row_start:04X}: " + " ".join(f"{value & 0xFFFF:04X}" for value in values))
        return "\n".join(rows)
