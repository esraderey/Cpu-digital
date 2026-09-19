"""Interfaz de línea de comandos para CPU Digital + Tramoya."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tramoya import MachineError

from cpu_digital import Assembler, AssemblyError, CPU
from cpu_digital.assembler import disassemble


ROOT = Path(__file__).resolve().parent
DEMOS = {
    "suma": ROOT / "programs" / "suma_15_25.asm",
    "ciclo": ROOT / "programs" / "suma_1_a_10.asm",
    "factorial": ROOT / "programs" / "factorial_5.asm",
    "subrutina": ROOT / "programs" / "subrutina.asm",
    "entrada": ROOT / "programs" / "entrada.asm",
    "hola": ROOT / "programs" / "hola.asm",
    "breakpoint": ROOT / "programs" / "breakpoint.asm",
}


def _integer(value: str) -> int:
    try:
        return int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Entero inválido: {value}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cpu-digital",
        description="Simulador de CPU de 16 bits orquestado por Tramoya 1.5.3",
    )
    parser.add_argument("program", nargs="?", type=Path, help="Programa ensamblador .asm")
    parser.add_argument("--demo", choices=sorted(DEMOS), default=None, help="Ejemplo incluido")
    parser.add_argument("--input", nargs="*", type=_integer, default=[], help="Valores para instrucciones IN")
    parser.add_argument("--max-cycles", type=int, default=10_000, help="Protección contra ciclos infinitos")
    parser.add_argument("--memory-size", type=int, default=256, help="Palabras de memoria")
    parser.add_argument("--breakpoint", action="append", default=[], help="Dirección o etiqueta donde pausar")
    parser.add_argument("--trace", action="store_true", help="Muestra la traza de microestados")
    parser.add_argument("--listing", action="store_true", help="Muestra el listado ensamblado")
    parser.add_argument("--disassemble", action="store_true", help="Muestra el programa desensamblado")
    parser.add_argument("--diagram", choices=("mermaid", "dot"), help="Imprime el diagrama de Tramoya")
    parser.add_argument("--debug", action="store_true", help="Abre el depurador interactivo")
    parser.add_argument("--save", type=Path, help="Guarda un snapshot JSON al finalizar")
    parser.add_argument("--load", type=Path, help="Restaura un snapshot JSON en vez de cargar un programa")
    return parser


def _resolve_breakpoints(values: list[str], symbols: dict[str, int]) -> set[int]:
    resolved: set[int] = set()
    for value in values:
        key = value.upper()
        if key in symbols:
            resolved.add(symbols[key])
            continue
        try:
            resolved.add(int(value, 0))
        except ValueError as exc:
            raise ValueError(f"Breakpoint desconocido: {value}") from exc
    return resolved


def _format_output(values: tuple[int | str, ...]) -> str:
    if not values:
        return "(sin salida)"
    if all(isinstance(value, str) for value in values):
        return repr("".join(values))
    return ", ".join(repr(value) for value in values)


def _print_result(cpu: CPU) -> None:
    result = cpu.result()
    print("\n=== RESULTADO ===")
    print(cpu.format_registers())
    print(f"Salida: {_format_output(result.output)}")
    if result.fault:
        print(f"Fallo: {result.fault}")
    if result.pause_reason:
        print(f"Pausa: {result.pause_reason}")


def _debugger(cpu: CPU, max_cycles: int, breakpoints: set[int]) -> None:
    print("Depurador Tramoya. Escribe 'help' para ver comandos.")
    print(cpu.format_registers())
    while True:
        try:
            command = input("cpu> ").strip().split()
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
                    "step|s, run|r, resume, regs, mem [inicio] [cantidad], "
                    "trace [cantidad], undo, save ARCHIVO, load ARCHIVO, diagram, quit"
                )
            elif name in {"s", "step", "paso"}:
                if cpu.paused:
                    cpu.resume()
                cpu.step()
                print(cpu.format_registers())
            elif name in {"r", "run", "correr"}:
                if cpu.paused:
                    cpu.resume()
                    cpu.step()  # Evita caer inmediatamente en el mismo breakpoint.
                cpu.run(max_cycles=max_cycles, breakpoints=breakpoints)
                _print_result(cpu)
            elif name == "resume":
                cpu.resume()
                print(cpu.format_registers())
            elif name in {"regs", "state", "estado"}:
                print(cpu.format_registers())
            elif name in {"mem", "memory", "memoria"}:
                start = int(args[0], 0) if args else 0
                count = int(args[1], 0) if len(args) > 1 else 16
                print(cpu.format_memory(start, count))
            elif name == "trace":
                count = int(args[0], 0) if args else 10
                for entry in cpu.trace[-count:]:
                    print(entry.format())
            elif name == "undo":
                cpu.undo()
                print(cpu.format_registers())
            elif name == "save" and args:
                print(f"Snapshot guardado: {cpu.save_snapshot(args[0])}")
            elif name == "load" and args:
                cpu.load_snapshot(args[0])
                print(cpu.format_registers())
            elif name == "diagram":
                print(cpu.diagram_mermaid())
            else:
                print("Comando desconocido. Usa 'help'.")
        except (ValueError, OSError, MachineError) as exc:
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
        cpu = CPU(memory_size=args.memory_size)
        symbols: dict[str, int] = {}
        words: tuple[int, ...] = ()

        if args.load:
            cpu.load_snapshot(args.load)
        else:
            program_path = args.program or DEMOS[args.demo or "suma"]
            source = program_path.read_text(encoding="utf-8")
            result = Assembler().assemble(source)
            symbols = dict(result.symbols)
            words = result.words
            cpu.load_program(words, inputs=args.input)
            print(f"Programa: {program_path}")
            print(f"Palabras: {len(words)} | Símbolos: {len(symbols)}")
            if args.listing:
                print("\n=== LISTADO ===")
                print(result.format_listing())
            if args.disassemble:
                print("\n=== DESENSAMBLADO ===")
                print("\n".join(disassemble(words, stop_at_halt=True)))

        if args.diagram:
            print(f"\n=== DIAGRAMA {args.diagram.upper()} ===")
            print(cpu.diagram_mermaid() if args.diagram == "mermaid" else cpu.diagram_dot())

        breakpoints = _resolve_breakpoints(args.breakpoint, symbols)
        if args.debug:
            _debugger(cpu, args.max_cycles, breakpoints)
        elif cpu.state not in {"HALT", "FAULT"}:
            cpu.run(max_cycles=args.max_cycles, breakpoints=breakpoints)

        _print_result(cpu)

        if args.trace:
            print("\n=== TRAZA ===")
            for entry in cpu.trace:
                print(entry.format())
        if args.save:
            print(f"Snapshot guardado: {cpu.save_snapshot(args.save)}")
        return 0 if not cpu.faulted else 1
    except (AssemblyError, ValueError, OSError, MachineError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

