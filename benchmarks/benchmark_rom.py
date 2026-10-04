"""Bucles que leen la ROM del TNU con LOAD, con y sin acelerador de bucles.

El código compilado lee la ROM del TNU (desde ``ROM_BASE``, con la capacidad ``npu`` y un
``TramoyaNeuralUnit`` con ROM) con la misma regla que el intérprete. Hasta el 2026-10-02, la
guarda de cada LOAD devolvía el control al intérprete en cualquier dirección fuera de la RAM,
también en esas lecturas legales: el bucle se compilaba, pero cada entrada se detenía en la
lectura y, con ella al principio del cuerpo, el bucle tardaba de 1,16 a 1,27 veces lo que sin
acelerador (Python 3.13.2, Windows x64).

Para cada carga mide con y sin acelerador (``VMConfig.accelerate_loops``) el tiempo mediano,
las instrucciones y las aceleradas, cuenta las entradas al código compilado y cuántas
devolvieron el control sin avanzar ninguna instrucción o a mitad del cuerpo, y comprueba que
el estado final es idéntico. Las cargas:

* ``revisor``: el programa del hallazgo, que suma la misma palabra en cada vuelta;
* ``producto, w en la N.ª``: producto escalar de una fila de pesos ``w`` por un vector ``x``
  de la RAM, con ``w`` leído en la primera, la segunda o la quinta instrucción del cuerpo;

cada una con los datos en la ROM y, como control, en la RAM. Con los modelos (``--modelos``), añade
``vm_programs/llama2.tasm`` con stories260K: cuántos bucles compila y cuántas lecturas de la
ROM con LOAD hace, en total y dentro de bucles compilados.

Uso (desde la raíz del repositorio):
    python benchmarks/benchmark_rom.py [--rondas 5] [--vueltas 20000] [--modelos models] [--tokens 20]
"""

from __future__ import annotations

import argparse
import platform
import statistics
import sys
import time
from array import array
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from cpu_digital.tnu import ROM_BASE, TensorROM, TramoyaNeuralUnit
from cpu_digital.vm32 import TramoyaVM32, VMConfig
from cpu_digital.vm32_assembler import Program32, VM32Assembler
from cpu_digital.vm32_isa import VMOpcode

ROOT = Path(__file__).resolve().parents[1]
CAPS = frozenset({"io", "memory", "npu"})
GAS = 4 * 10**9
X = 8192        # vector x, siempre en la RAM
COPY = 65536    # copia de los datos de la ROM en la RAM, para el control
# Producto escalar: R1 recorre x, R2 recorre w y R6 acumula.
STEPS = {
    "x": "LOAD R3, [R1]",
    "w": "LOAD R4, [R2]",
    "mul": "FMUL R5, R3, R4",
    "acc": "FADD R6, R6, R5",
    "next_x": "ADDI R1, R1, 1",
    "next_w": "ADDI R2, R2, 1",
}
ORDERS = {
    "1.ª": ("w", "x", "mul", "acc", "next_x", "next_w"),
    "2.ª": ("x", "w", "mul", "acc", "next_x", "next_w"),
    # w se lee para la vuelta siguiente: la primera multiplica x[0] por R4 = 0.
    "5.ª": ("x", "mul", "acc", "next_x", "w", "next_w"),
}


@dataclass
class Workload:
    name: str
    data: str                       # "ROM" o "RAM": dónde lee el bucle lo que está en la ROM
    program: Program32
    rom: TensorROM
    inputs: tuple[int, ...] = ()
    vector: array | None = None     # x, que se escribe en X


def assemble(source: str, name: str) -> Program32:
    return VM32Assembler().assemble(source, name).program


def floats(count: int, seed: int) -> array:
    return array("f", (((index * seed) % 97) / 97.0 - 0.5 for index in range(count)))


def reviewer(address: int, iterations: int) -> Program32:
    source = "\n".join([".code", f"    MOVI R2, {address}", f"    MOVI R9, {iterations}", "L:",
                        "    LOAD R1, [R2]", "    ADD R3, R3, R1", "    SUBI R9, R9, 1", "    JNZ L", "    HALT"])
    return assemble(source, "revisor.tasm")


def dot_product(order: tuple[str, ...], weights: int, iterations: int) -> Program32:
    lines = [".code", f"    MOVI R1, {X}", f"    MOVI R2, {weights}", f"    MOVI R9, {iterations}", "L:",
             *(f"    {STEPS[step]}" for step in order), "    SUBI R9, R9, 1", "    JNZ L", "    HALT"]
    return assemble("\n".join(lines), "producto.tasm")


def workloads(iterations: int) -> list[Workload]:
    weights = floats(iterations, 31)
    rom = TensorROM(weights.tobytes(), name="pesos")
    vector = floats(iterations, 17)
    loads = []
    for data, base in (("RAM", COPY), ("ROM", ROM_BASE)):
        loads.append(Workload("revisor", data, reviewer(base, iterations), rom))
    for position, order in ORDERS.items():
        for data, base in (("RAM", COPY), ("ROM", ROM_BASE)):
            loads.append(Workload(f"producto, w en la {position}", data, dot_product(order, base, iterations), rom,
                                  vector=vector))
    return loads


def llama_workload(models: Path, tokens: int) -> Workload | None:
    checkpoint, tokenizer = models / "stories260K.bin", models / "tok512.bin"
    if not checkpoint.exists() or not tokenizer.exists():
        return None
    from cpu_digital import tnu_models as tm

    rom = TensorROM(tm.build_rom(checkpoint.read_bytes(), tokenizer.read_bytes()), name=checkpoint.name)
    program = assemble((ROOT / "vm_programs" / "llama2.tasm").read_text(encoding="utf-8"), "llama2.tasm")
    return Workload(f"llama2_260K, {tokens} tokens", "ROM", program, rom, inputs=(tokens, 0, 1, 0))


def new_vm(load: Workload, accelerate: bool, vm_class: type[TramoyaVM32] = TramoyaVM32) -> TramoyaVM32:
    vm = vm_class(VMConfig(gas_limit=GAS, trace_size=0, accelerate_loops=accelerate, capabilities=CAPS),
                  npu=TramoyaNeuralUnit(load.rom))
    vm.load_program(load.program, inputs=load.inputs)
    if load.vector is not None:
        vm._memory.write_bytes(X, load.vector.tobytes())
    if load.data == "RAM":
        vm._memory.write_bytes(COPY, load.rom.bytes_view(0, load.rom.words))
    return vm


class CountingVM(TramoyaVM32):
    """Acelerador que anota los intentos de compilación y cómo termina cada entrada al código
    compilado (sobrescribe métodos que no cambian la semántica, así que sigue acelerando)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.attempts: list = []
        self.entries: Counter[str] = Counter()

    def _compiled_loop(self, header: int):
        tried = header in self._loops
        loop = super()._compiled_loop(header)
        if not tried and header in self._loops:
            self.attempts.append(loop)
        return loop

    def _run_loop(self, loop, budget: int) -> bool:
        advanced = super()._run_loop(loop, budget)
        if not advanced:
            self.entries["sin avanzar"] += 1
        elif loop.header < self._pc <= loop.backedge[0]:
            self.entries["a medias"] += 1  # una guarda devolvió el control dentro del cuerpo
        else:
            self.entries["completas"] += 1
        return advanced


class RomLoadVM(TramoyaVM32):
    """Intérprete que cuenta, por posición, los LOAD que leen la ROM."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.rom_loads: Counter[int] = Counter()

    def _read_rom_word(self, address: object) -> int:
        value = super()._read_rom_word(address)
        pc = self._pc - 4  # el intérprete avanza el pc antes de ejecutar la instrucción
        if self._fetch_decoded(pc)[0].opcode == VMOpcode.LOAD:  # y no una syscall que lee memoria
            self.rom_loads[pc] += 1
        return value


def fingerprint(vm: TramoyaVM32) -> tuple:
    result = vm.result()
    return (result, vm.pc, vm.registers, dict(vm.flags), vm.stack, vm.snapshot_bytes())


@dataclass
class Row:
    load: Workload
    instructions: int
    accelerated: int
    plain: float
    fast: float
    identical: bool
    entries: Counter[str] = field(default_factory=Counter)
    compiled: int = 0
    attempts: int = 0
    rom_loads: int = 0
    rom_loads_compiled: int = 0


def measure(load: Workload, rounds: int) -> Row:
    times: dict[bool, list[float]] = {False: [], True: []}
    prints: dict[bool, tuple] = {}
    for _ in range(rounds):
        for accelerate in (False, True):  # alternar reparte el ruido entre ambos modos
            vm = new_vm(load, accelerate)
            started = time.perf_counter()
            vm.run()
            times[accelerate].append(time.perf_counter() - started)
            prints.setdefault(accelerate, fingerprint(vm))
    # Los recuentos son deterministas: una ejecución aparte, sin cronometrar.
    counting = new_vm(load, True, CountingVM)
    counting.run()
    reading = new_vm(load, False, RomLoadVM)
    reading.run()
    compiled = [loop for loop in counting.attempts if loop is not None]
    inside = {pc for loop in compiled for pc in range(loop.header, loop.backedge[0] + 4, 4)}
    result = prints[False][0]
    return Row(load, result.instructions, counting.accelerated_instructions,
               statistics.median(times[False]), statistics.median(times[True]),
               prints[False] == prints[True] and result.state == "HALTED" and counting.result() == result,
               counting.entries, len(compiled), len(counting.attempts), sum(reading.rom_loads.values()),
               sum(count for pc, count in reading.rom_loads.items() if pc in inside))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(description="Bucles que leen la ROM del TNU con y sin acelerador de bucles")
    parser.add_argument("--rondas", type=int, default=5, help="Ejecuciones por carga y modo (se toma la mediana)")
    parser.add_argument("--vueltas", type=int, default=20_000, help="Vueltas de cada bucle sintético")
    parser.add_argument("--modelos", type=Path, default=ROOT / "models", help="Directorio de stories260K.bin y tok512.bin")
    parser.add_argument("--tokens", type=int, default=20, help="Tokens que genera llama2.tasm")
    args = parser.parse_args()
    if args.rondas < 1 or args.vueltas < 1 or args.tokens < 1:
        parser.error("rondas, vueltas y tokens deben ser positivos")

    loads = workloads(args.vueltas)
    llama = llama_workload(args.modelos, args.tokens)
    if llama is not None:
        loads.append(llama)
    print(f"Python {platform.python_version()} · {platform.system()} {platform.machine()} · "
          f"vueltas={args.vueltas:,} · rondas={args.rondas}\n")
    print(f"{'carga':28} {'datos':>5} {'instr.':>10} {'aceleradas':>11} {'entradas':>9} {'sin avanzar':>12} "
          f"{'a medias':>9} {'sin (ms)':>9} {'con (ms)':>9} {'con/sin':>8}")
    ok = True
    rows = []
    for load in loads:
        row = measure(load, args.rondas)
        rows.append(row)
        ok &= row.identical
        entries = row.entries
        print(f"{load.name:28} {load.data:>5} {row.instructions:>10,} {row.accelerated:>11,} "
              f"{sum(entries.values()):>9,} {entries['sin avanzar']:>12,} {entries['a medias']:>9,} "
              f"{row.plain * 1000:>9.1f} {row.fast * 1000:>9.1f} {row.fast / row.plain:>7.2f}×"
              f"{'' if row.identical else '  ERROR: el estado difiere o no termina'}")
    print("\nLecturas de la ROM con LOAD (intérprete) y bucles compilados (acelerador):")
    for row in rows:
        if row.load.data == "ROM":
            print(f"  {row.load.name:28} {row.rom_loads:>8,} lecturas, {row.rom_loads_compiled:>8,} dentro de bucles "
                  f"compilados · {row.compiled} de {row.attempts} intentos de compilación con éxito")
    if llama is None:
        print(f"\nllama2.tasm: omitido (faltan stories260K.bin y tok512.bin en {args.modelos})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
