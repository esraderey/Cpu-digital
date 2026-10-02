"""Coste de compilar del acelerador de bucles en programas que no escriben en su código.

Compilar un bucle cuesta de ~0,3 ms (cuerpo de una instrucción) a ~8 ms (64 de FPU), y
es el programa quien decide cuántos bucles tiene. Para cada carga mide con y sin
acelerador (``VMConfig.accelerate_loops``) el tiempo mediano, el gas, los intentos de
compilación (llamadas a ``compile_loop``) y las instrucciones aceleradas, y comprueba que
el estado final es idéntico. La razón de tiempos es la CPU del host por unidad de gas con
acelerador frente a sin él: el gas es el mismo. Todo con la configuración por defecto
(``protect_code=True``) y sin traza.

Las cargas adversarias compilan cuerpos que no usan. Cada cabecera sale del cuerpo nada más
entrar (tres instrucciones por vuelta) y deja detrás un cuerpo que nunca se ejecuta pero sí
se compila; salvo que se diga otra cosa, de 64 instrucciones, 63 de ellas de FPU:

* ``K bucles``: K cabeceras distintas, cada una con las 33 vueltas que la calientan;
* ``vueltas justas``: cada cabecera da las vueltas que pagan la compilación anterior, de
  modo que todas compilan;
* ``pago interpretado``: entre cabecera y cabecera, un bucle que no compila ejecuta las
  instrucciones que pagan la espera;
* ``pago acelerado``: lo mismo con un bucle compilado, cuyas instrucciones cuentan doble. Es
  la carga que más compila por instrucción ejecutada;
* ``cadena``: paga la espera entrando en 30 bucles compilados que salen en su primera
  instrucción: no cuentan doble y al host le cuestan como interpretarlas;
* ``espera caliente``: tras cada cabecera, un bucle nuevo de dos instrucciones espera su
  turno para compilar, y cada vuelta suya lo comprueba;
* ``cuerpos cortos``: 200 cabeceras con cuerpos de 8 instrucciones;
* ``no compilan``: 200 cabeceras cuyo cuerpo son 62 saltos a la instrucción siguiente y una
  syscall, el intento fallido más caro que conocemos.

Las demás son uso legítimo y miden el precio de la cota:

* ``8 bucles``: ocho bucles de 400 vueltas uno detrás de otro, una ronda o veinte;
* ``60 bucles``: sesenta bucles de 300 vueltas, cada uno recorrido una sola vez.

Uso (desde la raíz del repositorio):
    python benchmarks/benchmark_compilacion.py [--rondas 5]
"""

from __future__ import annotations

import argparse
import platform
import statistics
import sys
import time

from cpu_digital import vm32
from cpu_digital.vm32 import LOOP_COOLDOWN, LOOP_COOLDOWN_PER_INSTRUCTION, TramoyaVM32, VMConfig
from cpu_digital.vm32_assembler import Program32, VM32Assembler

GAS = 10**9
EXPENSIVE = ["    FDIV R1, R2, R3"] * 63
# Instrucciones que aplaza la compilación de una cabecera cara (el salto, 63 de FPU y la vuelta).
WAIT = LOOP_COOLDOWN + LOOP_COOLDOWN_PER_INSTRUCTION * (len(EXPENSIVE) + 2)
LINKS = 30


def unused_body(index: int, visits: int, body: list[str]) -> list[str]:
    """Una cabecera que da ``visits`` vueltas saliendo del cuerpo nada más entrar."""
    return [f"    MOVI R9, {visits}", f"H{index}:", f"    JMP T{index}", *body, f"    JNZ H{index}",
            f"T{index}:", "    SUBI R9, R9, 1", f"    JNZ H{index}"]


def unused_bodies(count: int, visits: int = 33, body: list[str] = EXPENSIVE) -> str:
    lines = [".code"]
    for index in range(count):
        lines += unused_body(index, visits, body)
    lines.append("    HALT")
    return "\n".join(lines)


def failing_bodies(count: int) -> str:
    lines = [".code"]
    for index in range(count):
        body = [line for jump in range(62) for line in (f"    JZ N{index}_{jump}", f"N{index}_{jump}:")]
        lines += unused_body(index, 33, [*body, "    SYSCALL 1"])
    lines.append("    HALT")
    return "\n".join(lines)


def paid_bodies(count: int, pay: list[str], rounds: int) -> str:
    """Entre cabecera y cabecera llama a un bucle de pago de ``rounds`` vueltas."""
    lines = [".code", "    JMP MAIN", "PAY:", f"    MOVI R8, {rounds}", "PL:", *pay,
             "    SUBI R8, R8, 1", "    JNZ PL", "    RET", "MAIN:"]
    for index in range(count):
        lines += ["    CALL PAY", *unused_body(index, 33, EXPENSIVE)]
    lines.append("    HALT")
    return "\n".join(lines)


def chain_payment(count: int) -> str:
    """Cada ronda de pago recorre una cadena de bucles compilados contiguos: cada eslabón sale
    por un salto en su primera instrucción, así que cada entrada acelera una sola instrucción."""
    lines = [".code", "    JMP START"]
    for link in range(LINKS):
        lines += [f"C{link}:", f"    JMP C{link + 1}", f"    JNZ C{link}"]
    lines += [f"C{LINKS}:", "    RET",
              "PAY:", f"    MOVI R8, {WAIT // (LINKS + 4) + 1}", "PR:", "    CALL C0", "    SUBI R8, R8, 1", "    JNZ PR",
              "    RET", "START:"]
    for link in range(LINKS):  # calienta cada eslabón con 33 llamadas directas (saltos hacia atrás)
        lines += ["    MOVI R9, 33", f"W{link}:", f"    CALL C{link}", "    SUBI R9, R9, 1", f"    JNZ W{link}"]
    lines += ["    MOVI R8, 3000", "    CALL PR         ; hasta que toda la cadena está compilada y pagada"]
    for index in range(count):
        lines += ["    CALL PAY", *unused_body(index, 33, EXPENSIVE)]
    lines.append("    HALT")
    return "\n".join(lines)


def hot_wait(count: int) -> str:
    """Tras cada cabecera cara, un bucle nuevo de dos instrucciones da vueltas hasta pagarla."""
    lines = [".code"]
    for index in range(count):
        lines += [*unused_body(index, 33, EXPENSIVE), f"    MOVI R8, {WAIT // 2 + 600}", f"W{index}:",
                  "    SUBI R8, R8, 1", f"    JNZ W{index}"]
    lines.append("    HALT")
    return "\n".join(lines)


def several_loops(rounds: int, loops: int = 8, iterations: int = 400) -> str:
    lines = [".code", f"    MOVI R8, {rounds}", "OUT:"]
    for index in range(loops):
        lines += [f"    MOVI R9, {iterations}", f"L{index}:", f"    ADDI R1, R1, {index + 1}",
                  "    SUBI R9, R9, 1", f"    JNZ L{index}"]
    lines += ["    SUBI R8, R8, 1", "    JNZ OUT", "    HALT"]
    return "\n".join(lines)


def one_pass(loops: int = 60, iterations: int = 300) -> str:
    lines = [".code"]
    for index in range(loops):
        lines += [f"    MOVI R9, {iterations}", f"L{index}:",
                  *[f"    ADDI R{1 + k}, R{1 + k}, {index + k}" for k in range(4)],
                  "    SUBI R9, R9, 1", f"    JNZ L{index}"]
    lines.append("    HALT")
    return "\n".join(lines)


def workloads() -> dict[str, str]:
    never_compiles = ["    ADDX R7, R7, R7         ; el acelerador no la admite: este bucle no compila"]
    return {
        "10 bucles": unused_bodies(10),
        "50 bucles": unused_bodies(50),
        "200 bucles": unused_bodies(200),
        "vueltas justas": unused_bodies(20, visits=WAIT // 3 + 1),
        "pago interpretado": paid_bodies(20, never_compiles, WAIT // 3 + 1),
        "pago acelerado": paid_bodies(20, [], WAIT // 4 + 1),
        "cadena": chain_payment(30),
        "espera caliente": hot_wait(20),
        "cuerpos cortos": unused_bodies(200, body=EXPENSIVE[:7]),
        "no compilan": failing_bodies(200),
        "8 bucles, 1 ronda": several_loops(1),
        "8 bucles, 20 rondas": several_loops(20),
        "60 bucles, 1 pasada": one_pass(),
    }


def execute(program: Program32, accelerate: bool) -> tuple[float, int, tuple, TramoyaVM32]:
    """Devuelve tiempo, intentos de compilación, estado final y la VM."""
    attempts = 0
    real = vm32.compile_loop

    def counting(vm: TramoyaVM32, header: int):
        nonlocal attempts
        attempts += 1
        return real(vm, header)

    vm = TramoyaVM32(VMConfig(memory_words=max(4096, 2 * len(program.words)), gas_limit=GAS, trace_size=0,
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
    parser = argparse.ArgumentParser(description="Coste de compilar del acelerador de bucles sin código automodificable")
    parser.add_argument("--rondas", type=int, default=5, help="Ejecuciones por carga y modo (se toma la mediana)")
    args = parser.parse_args()
    if args.rondas < 1:
        parser.error("Las rondas deben ser positivas")

    print(f"Python {platform.python_version()} · {platform.system()} {platform.machine()} · "
          f"espera={LOOP_COOLDOWN}+{LOOP_COOLDOWN_PER_INSTRUCTION}·L · rondas={args.rondas}\n")
    print(f"{'carga':20} {'gas':>9} {'instr.':>9} {'compila':>8} {'aceleradas':>11} "
          f"{'sin (ms)':>9} {'con (ms)':>9} {'con/sin':>8}")
    for name, source in workloads().items():
        program = VM32Assembler().assemble(source, source_name=f"{name}.tasm").program
        times: dict[bool, list[float]] = {False: [], True: []}
        states = {}
        for accelerate in (False, True):
            for _ in range(args.rondas):
                elapsed, attempts, states[accelerate], vm = execute(program, accelerate)
                times[accelerate].append(elapsed)
        if states[False] != states[True] or states[False][0].state != "HALTED":
            print(f"{name:20} ERROR: el estado con acelerador difiere del intérprete o el programa no termina")
            return 1
        result = states[True][0]
        plain, fast = statistics.median(times[False]), statistics.median(times[True])
        print(f"{name:20} {GAS - result.gas_remaining:>9,} {result.instructions:>9,} {attempts:>8,} "
              f"{vm.accelerated_instructions:>11,} {plain * 1000:>9.1f} {fast * 1000:>9.1f} {fast / plain:>7.2f}×")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
