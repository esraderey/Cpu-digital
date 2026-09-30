"""Benchmark reproducible del hot path de Tramoya VM32."""

from __future__ import annotations

import argparse
import statistics
import sys
import time

from cpu_digital import TramoyaVM32, VM32Assembler, VMConfig


SOURCE = """.code
LOOP:
    ADDI R1, R1, 1
    JMP LOOP
"""


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(description="Mide instrucciones por segundo en VM32")
    parser.add_argument("--instructions", type=int, default=200_000)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--trace", type=int, default=0, help="Tamaño del buffer de traza")
    parser.add_argument("--no-loops", action="store_true", help="Desactiva el acelerador de bucles")
    args = parser.parse_args()
    if args.instructions < 1 or args.rounds < 1 or args.trace < 0:
        parser.error("Los parámetros deben ser positivos; trace puede ser cero")

    program = VM32Assembler().assemble(SOURCE, source_name="benchmark.tasm").program
    rates: list[float] = []
    elapsed_values: list[float] = []
    allocated_bytes = 0
    for _ in range(args.rounds):
        vm = TramoyaVM32(
            VMConfig(
                gas_limit=max(1_000_000, args.instructions * 2),
                trace_size=args.trace,
                accelerate_loops=not args.no_loops,
            )
        )
        vm.load_program(program)
        started = time.perf_counter()
        vm.run(max_instructions=args.instructions)
        elapsed = time.perf_counter() - started
        executed = vm.result().instructions
        rates.append(executed / elapsed)
        elapsed_values.append(elapsed)
        allocated_bytes = vm.allocated_memory_bytes
        accelerated = vm.accelerated_instructions

    print(f"Rondas:             {args.rounds}")
    print(f"Instrucciones:      {args.instructions:,} por ronda")
    print(f"Traza:              {args.trace:,}")
    print(f"Bucles acelerados:  {'no' if args.no_loops else f'sí ({accelerated:,} instrucciones)'}")
    print(f"Mediana:            {statistics.median(rates):,.0f} instrucciones/s")
    print(f"Mejor:              {max(rates):,.0f} instrucciones/s")
    print(f"Tiempo mediano:     {statistics.median(elapsed_values):.6f} s")
    print(f"RAM lógica:         {VMConfig().memory_words * 4 / (1024 * 1024):.0f} MiB")
    print(f"RAM física VM32:    {allocated_bytes / 1024:.0f} KiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
