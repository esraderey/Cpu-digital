"""CLI para compilar, ejecutar y depurar programas Tramoya VM32."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tramoya import MachineError

from cpu_digital.vm32 import VMConfig, VMRuntimeError, TramoyaVM32
from cpu_digital.vm32_assembler import (
    Program32,
    VM32Assembler,
    VMAssemblyError,
    VMAssemblyResult,
    disassemble_vm32,
)


ROOT = Path(__file__).resolve().parent
DEMOS = {
    "hola": ROOT / "vm_programs" / "hola.tasm",
    "factorial": ROOT / "vm_programs" / "factorial_10.tasm",
    "fibonacci": ROOT / "vm_programs" / "fibonacci_20.tasm",
    "array": ROOT / "vm_programs" / "array_sum.tasm",
    "entrada": ROOT / "vm_programs" / "entrada.tasm",
    "interrupcion": ROOT / "vm_programs" / "interrupcion.tasm",
    "proteccion": ROOT / "vm_programs" / "proteccion.tasm",
}


def _integer(value: str) -> int:
    try:
        return int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Entero inválido: {value}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tramoya-vm",
        description="VM RISC de 32 bits segura y embebible, controlada por Tramoya",
    )
    parser.add_argument("program", nargs="?", type=Path, help="Fuente .tasm o bytecode .tvm")
    parser.add_argument("--demo", choices=sorted(DEMOS), help="Programa incluido")
    parser.add_argument("--compile", type=Path, help="Escribe bytecode .tvm")
    parser.add_argument("--no-run", action="store_true", help="Solo compila/inspecciona")
    parser.add_argument("--input", nargs="*", type=_integer, default=[], help="Entradas para read_int")
    parser.add_argument("--memory-words", type=int, default=VMConfig().memory_words)
    parser.add_argument("--gas", type=int, default=1_000_000)
    parser.add_argument("--stack-limit", type=int, default=8_192)
    parser.add_argument("--trace-buffer", type=int, default=4_096, help="0 desactiva la instrumentación")
    parser.add_argument("--output-limit", type=int, default=1_000_000)
    parser.add_argument("--max-instructions", type=int, default=None, help="Límite adicional de esta ejecución")
    parser.add_argument("--deny-capability", action="append", default=[], help="Deshabilita io/random/memory/etc.")
    parser.add_argument("--breakpoint", action="append", default=[], help="Dirección o etiqueta")
    parser.add_argument("--vector", action="append", default=[], help="Configura VECTOR=ETIQUETA")
    parser.add_argument("--interrupt", action="append", type=_integer, default=[], help="Encola una interrupción")
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--listing", action="store_true")
    parser.add_argument("--disassemble", action="store_true")
    parser.add_argument("--diagram", action="store_true", help="Imprime el ciclo de vida Mermaid")
    parser.add_argument("--debug", action="store_true", help="Depurador interactivo")
    parser.add_argument("--save-snapshot", type=Path)
    parser.add_argument("--load-snapshot", type=Path)
    return parser


def _load_program(path: Path, *, max_words: int) -> tuple[Program32, VMAssemblyResult | None]:
    if path.suffix.lower() == ".tvm":
        return Program32.load(path), None
    source = path.read_text(encoding="utf-8")
    assembled = VM32Assembler().assemble(source, source_name=str(path), max_words=max_words)
    return assembled.program, assembled


def _resolve(value: str, symbols: dict[str, int]) -> int:
    key = value.upper()
    if key in symbols:
        return symbols[key]
    try:
        return int(value, 0)
    except ValueError as exc:
        raise ValueError(f"Símbolo o dirección desconocida: {value}") from exc


def _resolve_breakpoints(values: list[str], symbols: dict[str, int]) -> set[int]:
    return {_resolve(value, symbols) for value in values}


def _configure_vectors(vm: TramoyaVM32, definitions: list[str], symbols: dict[str, int]) -> None:
    for definition in definitions:
        if "=" not in definition:
            raise ValueError(f"Vector inválido: {definition}; usa N=ETIQUETA")
        vector_text, target_text = definition.split("=", 1)
        vector = int(vector_text, 0)
        vm.set_interrupt_vector(vector, _resolve(target_text, symbols))


def _format_output(values: tuple[int | str, ...]) -> str:
    if not values:
        return "(sin salida)"
    if all(isinstance(value, str) for value in values):
        return repr("".join(values))
    return ", ".join(repr(value) for value in values)


def _print_result(vm: TramoyaVM32) -> None:
    result = vm.result()
    print("\n=== VM32 ===")
    print(vm.format_registers())
    print(
        f"Instrucciones={result.instructions} Ciclos={result.cycles} "
        f"Gas={result.gas_remaining} Salida={_format_output(result.output)}"
    )
    if result.exit_code is not None:
        print(f"Exit code: {result.exit_code}")
    if result.fault:
        print(f"Fallo: {result.fault}")
    if result.wait_reason:
        print(f"Espera: {result.wait_reason}")
    if result.pause_reason:
        print(f"Pausa: {result.pause_reason}")


def _debugger(vm: TramoyaVM32, breakpoints: set[int], max_instructions: int | None) -> None:
    print("Depurador VM32. Usa 'help' para ver comandos.")
    print(vm.format_registers())
    while True:
        try:
            command = input("vm32> ").strip().split()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not command:
            continue
        name, *args = command
        name = name.lower()
        try:
            if name in {"q", "quit", "salir"}:
                break
            if name in {"h", "help", "ayuda"}:
                print(
                    "step|s, run|r, regs, mem INICIO [N], stack, trace [N], "
                    "input VALORES..., irq N, resume, wake, save ARCHIVO, diagram, quit"
                )
            elif name in {"s", "step"}:
                if vm.state == "PAUSED":
                    vm.resume()
                vm.step()
                print(vm.format_registers())
            elif name in {"r", "run"}:
                vm.run(max_instructions=max_instructions, breakpoints=breakpoints)
                _print_result(vm)
            elif name in {"regs", "state"}:
                print(vm.format_registers())
            elif name in {"mem", "memory"}:
                start = int(args[0], 0)
                count = int(args[1], 0) if len(args) > 1 else 16
                values = vm.memory_slice(start, count)
                for offset in range(0, len(values), 8):
                    row = values[offset:offset + 8]
                    print(f"{start + offset:08X}: " + " ".join(f"{value & 0xFFFFFFFF:08X}" for value in row))
            elif name == "stack":
                print(vm.stack)
            elif name == "trace":
                count = int(args[0], 0) if args else 10
                for entry in vm.trace[-count:]:
                    print(entry.format())
            elif name == "input":
                vm.provide_input(*(int(value, 0) for value in args))
                print(vm.state)
            elif name == "irq" and args:
                vm.request_interrupt(int(args[0], 0))
            elif name == "resume":
                print(vm.resume())
            elif name == "wake":
                print(vm.wake())
            elif name == "save" and args:
                print(f"Snapshot: {vm.save_snapshot(args[0])}")
            elif name == "diagram":
                print(vm.lifecycle_mermaid())
            else:
                print("Comando desconocido. Usa 'help'.")
        except (ValueError, OSError, MachineError, VMRuntimeError) as exc:
            print(f"Error: {exc}")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    args = build_parser().parse_args(argv)

    try:
        capabilities = set(VMConfig().capabilities) - set(args.deny_capability)
        config = VMConfig(
            memory_words=args.memory_words,
            gas_limit=args.gas,
            stack_limit=args.stack_limit,
            trace_size=args.trace_buffer,
            output_limit=args.output_limit,
            capabilities=frozenset(capabilities),
        )
        vm = TramoyaVM32(config)
        assembled: VMAssemblyResult | None = None
        symbols: dict[str, int] = {}

        if args.load_snapshot:
            vm = TramoyaVM32.from_snapshot_bytes(
                args.load_snapshot.read_bytes(),
                trace_size=args.trace_buffer,
                capabilities=frozenset(capabilities),
            )
            symbols = dict(vm.program.symbols) if vm.program else {}
            if args.input:
                vm.provide_input(*args.input)
        else:
            path = args.program or DEMOS[args.demo or "hola"]
            program, assembled = _load_program(path, max_words=args.memory_words)
            symbols = dict(program.symbols)
            vm.load_program(program, inputs=args.input)
            print(
                f"Programa: {path}\nCódigo: {program.code_size} palabras | "
                f"Datos: {program.data_size} | Entrada: {program.entry:08X}"
            )
            if args.compile:
                print(f"Bytecode: {program.save(args.compile)}")

        if args.listing:
            if assembled is None:
                print("El listado fuente solo está disponible al ensamblar .tasm")
            else:
                print("\n=== LISTADO VM32 ===")
                print(assembled.format_listing())
        if args.disassemble and vm.program:
            print("\n=== DESENSAMBLADO VM32 ===")
            print("\n".join(disassemble_vm32(vm.program)))
        if args.diagram:
            print("\n=== CICLO DE VIDA TRAMOYA ===")
            print(vm.lifecycle_mermaid())

        _configure_vectors(vm, args.vector, symbols)
        for vector in args.interrupt:
            vm.request_interrupt(vector)
        breakpoints = _resolve_breakpoints(args.breakpoint, symbols)

        if not args.no_run:
            if args.debug:
                _debugger(vm, breakpoints, args.max_instructions)
            else:
                vm.run(max_instructions=args.max_instructions, breakpoints=breakpoints)
            _print_result(vm)
            if args.trace:
                print("\n=== TRAZA VM32 ===")
                for entry in vm.trace:
                    print(entry.format())
        if args.save_snapshot:
            print(f"Snapshot: {vm.save_snapshot(args.save_snapshot)}")

        result = vm.result()
        if result.state == "FAULTED":
            return 1
        return int(result.exit_code or 0) & 0xFF if result.state == "HALTED" else 0
    except (VMAssemblyError, VMRuntimeError, ValueError, OSError, MachineError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
