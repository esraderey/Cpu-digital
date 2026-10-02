"""Coste del acelerador de bucles con código automodificable (``protect_code=False``).

Cada escritura en código invalida los bucles compilados. Para cada carga mide con y
sin acelerador (``VMConfig.accelerate_loops``) el tiempo mediano, el gas, los intentos
de compilación (llamadas a ``compile_loop``) y las instrucciones aceleradas, y comprueba
que el estado final es idéntico. La razón de tiempos es la CPU del host por unidad de
gas con acelerador frente a sin él: el gas es el mismo.

Las cargas adversarias escriben en su código justo después de que el bucle se compile,
para que el trabajo de compilar se pierda:

* ``cada vuelta``: el bucle escribe en su propio cuerpo en cada vuelta;
* ``cada 33 vueltas``: lo mínimo para que la cabecera vuelva a calentarse;
* ``cuerpo caro``: como la anterior, con un cuerpo de 64 instrucciones que nunca se
  ejecutan (la cabecera sale del bucle nada más entrar) pero sí se compilan (~8 ms);
* ``intento fallido``: el mismo cuerpo con una syscall al final, que no compila.

Las otras dos son el uso legítimo, un parche por ronda:

* ``parche por ronda``: 2 000 vueltas del bucle parcheado;
* ``parche, 8 bucles``: ocho bucles de 400 vueltas en cada ronda. Lo que cada uno
  ejecuta compilado paga su compilación antes de que el siguiente se caliente, así que
  los ocho vuelven a compilar en cada ronda sin esperar.

El coste de compilar en programas que no escriben en su código lo mide
``benchmarks/benchmark_compilacion.py``.

Uso (desde la raíz del repositorio):
    python benchmarks/benchmark_automodificable.py [--rondas 5] [--escala 1]
"""

from __future__ import annotations

import argparse
import platform
import statistics
import sys
import time

from cpu_digital import vm32
from cpu_digital.vm32 import TramoyaVM32, VMConfig
from cpu_digital.vm32_assembler import Program32, VM32Assembler

GAS = 10**9

EVERY_ITERATION = """.code
    MOVI R9, {n}
    LEA R4, OBJ
L:
    STORE R0, [R4+2]
    ADDI R1, R1, 1
OBJ:
    NOP
    SUBI R9, R9, 1
    JNZ L
    HALT
"""

EVERY_PERIOD = """.code
    MOVI R8, {n}
    LEA R4, OBJ
OUT:
    MOVI R9, {period}
L:
    SUBI R9, R9, 1
    JNZ L
    STORE R0, [R4+2]
OBJ:
    NOP
    SUBI R8, R8, 1
    JNZ OUT
    HALT
"""


def expensive_body(rounds: int, last: str = "FDIV R1, R2, R3") -> str:
    lines = [".code", f"    MOVI R8, {rounds}", "    LEA R4, OBJ", "    MOVI R9, 33", "H:", "    JMP T",
             *["    FDIV R1, R2, R3"] * 62, f"    {last}", "    JNZ H", "T:", "    SUBI R9, R9, 1", "    JNZ H",
             "    STORE R0, [R4+2]", "OBJ:", "    NOP", "    MOVI R9, 33", "    SUBI R8, R8, 1", "    JNZ H", "    HALT"]
    return "\n".join(lines)


def several_loops(rounds: int, loops: int = 8, iterations: int = 400) -> str:
    lines = [".code", f"    MOVI R8, {rounds}", "    LEA R4, OBJ", "OUT:"]
    for index in range(loops):
        lines += [f"    MOVI R9, {iterations}", f"L{index}:", f"    ADDI R1, R1, {index + 1}",
                  "    SUBI R9, R9, 1", f"    JNZ L{index}"]
    lines += ["    STORE R0, [R4+2]", "OBJ:", "    NOP", "    SUBI R8, R8, 1", "    JNZ OUT", "    HALT"]
    return "\n".join(lines)


def workloads(scale: int) -> dict[str, str]:
    return {
        "cada vuelta": EVERY_ITERATION.format(n=3000 * scale),
        "cada 33 vueltas": EVERY_PERIOD.format(n=1000 * scale, period=33),
        "cuerpo caro": expensive_body(1000 * scale),
        "intento fallido": expensive_body(1000 * scale, last="SYSCALL 1"),
        "parche por ronda": EVERY_PERIOD.format(n=30 * scale, period=2000),
        "parche, 8 bucles": several_loops(20 * scale),
    }


def execute(program: Program32, accelerate: bool) -> tuple[float, int, tuple, TramoyaVM32]:
    """Devuelve tiempo, intentos de compilación, estado final y la VM."""
    attempts = 0
    real = vm32.compile_loop

    def counting(vm: TramoyaVM32, header: int):
        nonlocal attempts
        attempts += 1
        return real(vm, header)

    vm = TramoyaVM32(VMConfig(memory_words=4096, gas_limit=GAS, trace_size=0, protect_code=False,
                              accelerate_loops=accelerate))
    vm.load_program(program)
    vm32.compile_loop = counting
    try:
        started = time.perf_counter()
        result = vm.run()
        elapsed = time.perf_counter() - started
    finally:
        vm32.compile_loop = real
    return elapsed, attempts, (result, vm.registers, dict(vm.flags), vm.snapshot_bytes()), vm


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(description="Coste del acelerador de bucles con código automodificable")
    parser.add_argument("--rondas", type=int, default=5, help="Ejecuciones por carga y modo (se toma la mediana)")
    parser.add_argument("--escala", type=int, default=1, help="Multiplica las vueltas de cada carga")
    args = parser.parse_args()
    if args.rondas < 1 or args.escala < 1:
        parser.error("Los parámetros deben ser positivos")

    print(f"Python {platform.python_version()} · {platform.system()} {platform.machine()} · rondas={args.rondas}\n")
    print(f"{'carga':18} {'gas':>9} {'instr.':>9} {'compila':>8} {'aceleradas':>11} "
          f"{'sin (ms)':>9} {'con (ms)':>9} {'con/sin':>8}")
    for name, source in workloads(args.escala).items():
        program = VM32Assembler().assemble(source, source_name=f"{name}.tasm").program
        times: dict[bool, list[float]] = {False: [], True: []}
        states = {}
        for accelerate in (False, True):
            for _ in range(args.rondas):
                elapsed, attempts, states[accelerate], vm = execute(program, accelerate)
                times[accelerate].append(elapsed)
        if states[False] != states[True] or states[False][0].state != "HALTED":
            print(f"{name:18} ERROR: el estado con acelerador difiere del intérprete o el programa no termina")
            return 1
        result = states[True][0]
        plain, fast = statistics.median(times[False]), statistics.median(times[True])
        print(f"{name:18} {GAS - result.gas_remaining:>9,} {result.instructions:>9,} {attempts:>8,} "
              f"{vm.accelerated_instructions:>11,} {plain * 1000:>9.1f} {fast * 1000:>9.1f} {fast / plain:>7.2f}×")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
