"""Benchmark reproducible del Chip Neuronal Tramoya (TNU).

Experimentos (criterio de muerte entre paréntesis):
  E1  FDOT dentro de la VM, contando el dispatch (≥ 20 M MAC/s)
  E2  stories260K: tok/s y determinismo con la misma semilla (≥ 20 tok/s, tokens idénticos)
  E3  stories15M Q8 (≥ 1 tok/s); también f32 como referencia
  E4  snapshot con la ROM de stories15M conectada (< 100 ms)
  G   calibración de gas: µs por unidad de gas de cada operación frente al intérprete

Los modelos se leen de ``models/`` (descarga manual; ver VM32.md). Sin modelos se
omiten E2/E3/E4 y se indica.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from array import array
from pathlib import Path

from cpu_digital import tnu_models as tm
from cpu_digital.tnu import ROM_BASE, TNU_GAS_DIVISOR, TensorROM, TramoyaNeuralUnit
from cpu_digital.vm32 import TramoyaVM32, VMConfig
from cpu_digital.vm32_assembler import VM32Assembler

ROOT = Path(__file__).resolve().parents[1]
CAPS = frozenset({"io", "memory", "npu", "introspection"})
MODELS = ROOT / "models"
LLAMA = VM32Assembler().assemble((ROOT / "vm_programs" / "llama2.tasm").read_text(encoding="utf-8"), "llama2.tasm").program


def assemble(source: str):
    return VM32Assembler().assemble(source).program


def new_vm(rom: TensorROM | None = None, **config) -> TramoyaVM32:
    config.setdefault("gas_limit", 4 * 10**9)
    config.setdefault("trace_size", 0)
    config.setdefault("memory_words", 1 << 21)
    return TramoyaVM32(VMConfig(capabilities=CAPS, **config), npu=TramoyaNeuralUnit(rom))


def timed_run(vm: TramoyaVM32) -> float:
    started = time.perf_counter()
    vm.run()
    elapsed = time.perf_counter() - started
    result = vm.result()
    if result.state != "HALTED":
        raise RuntimeError(f"La VM terminó en {result.state}: {result.fault}")
    return elapsed


def loop_program(body: str, iterations: int, vl: int, vr: int = 1, vs: int = 0) -> str:
    return f""".code
    MOVI R10, {vl}
    MOVI R11, {vr}
    MOVI R12, {vs}
    VCFG R10, R11, R12
    MOVI R1, 8192
    MOVI R2, 16384
    MOVI R3, 24576
    MOVI R4, {ROM_BASE}
    MOVI R6, {tm.f32_bits(0.5)}
    MOVI R9, {iterations}
LOOP:
{body}
    SUBI R9, R9, 1
    JNZ LOOP
    HALT
"""


def fill_ram(vm: TramoyaVM32, words: int = 3 * 8192) -> None:
    values = array("f", ((index % 97) / 97.0 - 0.5 for index in range(words)))
    vm._memory.write_bytes(8192, values.tobytes())


def best_of(rounds: int, build) -> tuple[float, TramoyaVM32]:
    best = None
    for _ in range(rounds):
        vm = build()
        elapsed = timed_run(vm)
        if best is None or elapsed < best[0]:
            best = (elapsed, vm)
    return best


def interpreter_rate(rounds: int) -> dict[str, float]:
    program = assemble(".code\n    MOVI R9, 100000\nL:\n    SUBI R9, R9, 1\n    JNZ L\n    HALT")

    def build():
        vm = new_vm()
        vm.load_program(program)
        return vm

    elapsed, vm = best_of(rounds, build)
    instructions = vm.result().instructions
    gas = 4 * 10**9 - vm.result().gas_remaining
    return {"instr_per_s": instructions / elapsed, "us_per_gas": elapsed / gas * 1e6}


def scalar_mac_rate(rounds: int) -> float:
    """MAC float32 con LOAD/FMUL/FADD en el invitado (sin TNU)."""
    program = assemble(""".code
    MOVI R1, 8192
    MOVI R2, 16384
    MOVI R9, 4096
L:
    LOAD R3, [R1]
    LOAD R4, [R2]
    FMUL R5, R3, R4
    FADD R6, R6, R5
    ADDI R1, R1, 1
    ADDI R2, R2, 1
    SUBI R9, R9, 1
    JNZ L
    HALT""")

    def build():
        vm = new_vm()
        vm.load_program(program)
        fill_ram(vm)
        return vm

    elapsed, _ = best_of(rounds, build)
    return 4096 / elapsed


def e1_fdot(rounds: int) -> dict[str, float]:
    results = {}
    for vl, iterations in ((4096, 400), (288, 2000)):
        program = assemble(loop_program("    FDOT R5, R1, R2", iterations, vl))

        def build(program=program):
            vm = new_vm()
            vm.load_program(program)
            fill_ram(vm)
            return vm

        elapsed, _ = best_of(rounds, build)
        results[f"fdot_{vl}_mmac_s"] = vl * iterations / elapsed / 1e6
    weights = TensorROM(array("f", ((i % 89) / 89.0 for i in range(288 * 288))).tobytes())
    program = assemble(loop_program("    MATVEC R3, R4, R1", 100, 288, 288))

    def build_matvec():
        vm = new_vm(weights)
        vm.load_program(program)
        fill_ram(vm)
        return vm

    elapsed, _ = best_of(rounds, build_matvec)
    results["matvec_288_ms"] = elapsed / 100 * 1e3
    results["matvec_288_mmac_s"] = 288 * 288 * 100 / elapsed / 1e6
    return results


def gas_calibration(rounds: int, us_per_gas_interpreter: float) -> dict[str, dict[str, float]]:
    rom = TensorROM(array("f", ((i % 89) / 89.0 - 0.3 for i in range(288 * 768))).tobytes())
    q8_rom = TensorROM(
        tm._quantize_rows(memoryview(array("f", ((i % 89) / 89.0 - 0.3 for i in range(288 * 768)))), 768, 288)
    )
    cases = {
        "VADD": ("    VADD R3, R1, R2", 4096, 1, None),
        "VSCALE": ("    VSCALE R3, R1, R6", 4096, 1, None),
        "VEXP": ("    VEXP R3, R1", 4096, 1, None),
        "VSILU": ("    VSILU R3, R1", 4096, 1, None),
        "VSOFTMAX": ("    VSOFTMAX R3, R1", 4096, 1, None),
        "RMSNORM": ("    RMSNORM R3, R1, R2", 4096, 1, None),
        "ROPE": ("    MOVI R7, 7\n    MOVI R8, 48\n    ROPE R3, R7, R8", 4032, 1, None),
        "FDOT": ("    FDOT R5, R1, R2", 4096, 1, None),
        "MATVEC": ("    MATVEC R3, R4, R1", 288, 768, rom),
        "MATTV": ("    MATTV R3, R4, R1", 288, 16, rom),
        "QMATVEC": ("    MOVI R5, 30000\n    VQUANT R5, R1\n    QMATVEC R3, R4, R5", 288, 768, q8_rom),
        "VARGMAX": ("    VARGMAX R5, R1", 4096, 1, None),
    }
    table = {}
    for name, (body, vl, vr, rom_case) in cases.items():
        iterations = 50
        program = assemble(loop_program(body, iterations, vl, vr))
        body_program = assemble(loop_program("    NOP", iterations, vl, vr))

        def build(program=program, rom_case=rom_case):
            vm = new_vm(rom_case)
            vm.load_program(program)
            fill_ram(vm)
            return vm

        elapsed, vm = best_of(rounds, build)
        gas = 4 * 10**9 - vm.result().gas_remaining
        empty = new_vm(rom_case)
        empty.load_program(body_program)
        empty.run()
        loop_gas = 4 * 10**9 - empty.result().gas_remaining
        op_gas = (gas - loop_gas) / iterations
        op_seconds = (elapsed - loop_gas * us_per_gas_interpreter / 1e6) / iterations
        table[name] = {
            "gas_por_instr": op_gas,
            "us_por_instr": op_seconds * 1e6,
            "us_por_gas": op_seconds * 1e6 / op_gas,
            "relacion_con_interprete": (op_seconds * 1e6 / op_gas) / us_per_gas_interpreter,
        }
    return table


def generate(rom: TensorROM, steps: int, inverse_temperature: float, seed: int) -> tuple[float, list[int], str, int]:
    vm = new_vm(rom)
    vm.load_program(LLAMA, inputs=[steps, tm.f32_bits(inverse_temperature), seed, 0])
    elapsed = timed_run(vm)
    count = vm.read_memory(LLAMA.symbols["NGEN"])
    tokens = list(vm.memory_slice(vm.read_memory(LLAMA.symbols["TOKENS_PTR"]), count))
    text = "".join(str(value) for value in vm.result().output)
    return elapsed, tokens, text, vm.result().instructions


def e2_stories260k(steps: int) -> dict[str, object] | None:
    if not (MODELS / "stories260K.bin").exists():
        return None
    rom = TensorROM(tm.build_rom((MODELS / "stories260K.bin").read_bytes(), (MODELS / "tok512.bin").read_bytes()))
    greedy_s, greedy, text, instructions = generate(rom, steps, 0.0, 1)
    first_s, first, _, _ = generate(rom, steps, 1.0, 42)
    second_s, second, _, _ = generate(rom, steps, 1.0, 42)
    return {
        "tokens": len(greedy),
        "tok_s_voraz": len(greedy) / greedy_s,
        "tok_s_muestreo": [len(first) / first_s, len(second) / second_s],
        "instr_por_token": instructions / max(1, len(greedy)),
        "misma_semilla_identica": first == second,
        "texto": text[:160],
    }


def e3_stories15m(steps: int, quants: list[str]) -> dict[str, object] | None:
    if not (MODELS / "stories15M.bin").exists():
        return None
    checkpoint = (MODELS / "stories15M.bin").read_bytes()
    tokenizer = (MODELS / "tokenizer.bin").read_bytes()
    report = {}
    for quant in quants:
        started = time.perf_counter()
        rom = TensorROM(tm.build_rom(checkpoint, tokenizer, quant=quant))
        build_s = time.perf_counter() - started
        elapsed, tokens, text, instructions = generate(rom, steps, 0.0, 1)
        report[quant] = {
            "rom_palabras": rom.words,
            "rom_build_s": build_s,
            "tokens": len(tokens),
            "tok_s": len(tokens) / elapsed,
            "instr_por_token": instructions / max(1, len(tokens)),
            "texto": text[:160],
        }
    return report


def e4_snapshot() -> dict[str, float] | None:
    if not (MODELS / "stories15M.bin").exists():
        return None
    rom = TensorROM(tm.build_rom((MODELS / "stories15M.bin").read_bytes(), (MODELS / "tokenizer.bin").read_bytes()))

    def measure(budget: int | None, attached: bool) -> dict[str, float]:
        vm = new_vm(rom)
        vm.load_program(LLAMA, inputs=[256, 0, 1, 0])
        if budget:
            vm.run(max_instructions=budget)  # a mitad de la generación, con la caché KV poblada
        if not attached:
            vm.attach_npu(None)
        samples, restores = [], []
        snapshot = b""
        for _ in range(5):
            started = time.perf_counter()
            snapshot = vm.snapshot_bytes()
            samples.append(time.perf_counter() - started)
            started = time.perf_counter()
            TramoyaVM32.from_snapshot_bytes(
                snapshot, capabilities=CAPS, npu=TramoyaNeuralUnit(rom) if attached else None
            )
            restores.append(time.perf_counter() - started)
        return {
            "ram_asignada_palabras": vm.allocated_memory_words,
            "snapshot_ms": statistics.median(samples) * 1e3,
            "restore_ms": statistics.median(restores) * 1e3,
            "snapshot_bytes": len(snapshot),
        }

    # El criterio E4 mide la ROM: se compara el mismo estado de RAM con y sin ROM conectada.
    loaded = measure(None, True)
    middle = measure(20_000, True)
    middle_without_rom = measure(20_000, False)
    return {
        "rom_palabras": rom.words,
        "snapshot_ms": loaded["snapshot_ms"],
        "recien_cargado": loaded,
        "mitad_con_rom": middle,
        "mitad_sin_rom": middle_without_rom,
        "aporte_rom_ms": middle["snapshot_ms"] - middle_without_rom["snapshot_ms"],
        "aporte_rom_bytes": middle["snapshot_bytes"] - middle_without_rom["snapshot_bytes"],
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(description="Benchmark del chip TNU (E1–E4 y calibración de gas)")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--steps-260k", type=int, default=256)
    parser.add_argument("--steps-15m", type=int, default=128)
    parser.add_argument("--quant-15m", nargs="+", default=["q8", "f32"], choices=["q8", "f32"])
    parser.add_argument("--skip-models", action="store_true", help="Solo E1 y la calibración de gas")
    parser.add_argument("--json", type=Path, help="Guarda el informe completo en JSON")
    args = parser.parse_args()

    report: dict[str, object] = {
        "python": sys.version.split()[0],
        "plataforma": f"{platform.system()} {platform.machine()}",
        "divisor_gas": TNU_GAS_DIVISOR,
    }
    interpreter = interpreter_rate(args.rounds)
    report["interprete"] = interpreter
    report["mac_escalar_por_s"] = scalar_mac_rate(args.rounds)
    report["E1"] = e1_fdot(args.rounds)
    report["gas"] = gas_calibration(args.rounds, interpreter["us_per_gas"])
    if not args.skip_models:
        report["E2"] = e2_stories260k(args.steps_260k)
        report["E3"] = e3_stories15m(args.steps_15m, args.quant_15m)
        report["E4"] = e4_snapshot()

    print(f"Python {report['python']} · {report['plataforma']}")
    print(f"Intérprete: {interpreter['instr_per_s']:,.0f} instr/s · {interpreter['us_per_gas']:.2f} µs/gas")
    print(f"MAC escalar en el invitado (LOAD/FMUL/FADD): {report['mac_escalar_por_s']:,.0f} MAC/s")
    e1 = report["E1"]
    verdict = "PASA" if e1["fdot_4096_mmac_s"] >= 20 else "MUERE"
    print(f"E1 FDOT 4096: {e1['fdot_4096_mmac_s']:.1f} M MAC/s · FDOT 288: {e1['fdot_288_mmac_s']:.1f} M MAC/s · "
          f"MATVEC 288×288: {e1['matvec_288_ms']:.2f} ms ({e1['matvec_288_mmac_s']:.1f} M MAC/s)  [{verdict}]")
    print("Gas: operación · gas/instr · µs/instr · µs/gas · ×intérprete")
    for name, row in report["gas"].items():
        print(f"  {name:<9} {row['gas_por_instr']:>8.0f} {row['us_por_instr']:>10.1f} "
              f"{row['us_por_gas']:>8.3f} {row['relacion_con_interprete']:>6.2f}")
    for key in ("E2", "E3", "E4"):
        if key not in report:
            continue
        if report[key] is None:
            print(f"{key}: omitido (faltan modelos en models/)")
            continue
        print(f"{key}: {json.dumps(report[key], ensure_ascii=False)}")
    if report.get("E2"):
        e2 = report["E2"]
        ok = e2["tok_s_voraz"] >= 20 and e2["misma_semilla_identica"]
        print(f"E2 veredicto: {'PASA' if ok else 'MUERE'} ({e2['tok_s_voraz']:.1f} tok/s)")
    if report.get("E3") and "q8" in report["E3"]:
        print(f"E3 veredicto: {'PASA' if report['E3']['q8']['tok_s'] >= 1 else 'MUERE'} "
              f"({report['E3']['q8']['tok_s']:.2f} tok/s Q8)")
    if report.get("E4"):
        e4 = report["E4"]
        print(f"E4 veredicto: {'PASA' if e4['snapshot_ms'] < 100 else 'MUERE'} "
              f"({e4['snapshot_ms']:.1f} ms con la ROM de {e4['rom_palabras']:,} palabras; "
              f"aporte de la ROM a mitad de generación: {e4['aporte_rom_ms']:+.1f} ms, "
              f"{e4['aporte_rom_bytes']:+d} B; total a mitad: {e4['mitad_con_rom']['snapshot_ms']:.0f} ms, "
              "dominado por zlib-9 de la caché KV en RAM)")
    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Informe: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
