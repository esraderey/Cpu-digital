"""E3 de RFC-EXP-00018: CoreMark rv32i sobre el hart en tres modos.

Ejecuta ``benchmarks/rv32_arranque/bin/coremark_rv32i_zicsr_it10.bin`` (compilado con clang,
sha256 versionado) con el intérprete comprobar-y-confirmar, con los bloques compilados sin
horizonte y con los bloques con horizonte (gas exacto), y mide instrucciones por segundo,
cobertura de los bloques y compilaciones. Los tres modos deben dar el mismo recuento de
instrucciones y los mismos CRC; con horizonte el gas es exacto.

Criterios del RFC (§5, E3): muere P1 si los bloques con horizonte no duplican al intérprete o si
el intérprete queda por debajo de 0,4 M instr/s; el horizonte se retira si cuesta más del 10 %
frente a los bloques sin él. Meta informativa: 5,5 M instr/s en los bucles internos de VM32.

Antes de dar por buena una cifra, comprobar la carga de la máquina y repetir: los tiempos son el
mínimo de ``--rondas`` (los recuentos son deterministas).

Uso (desde la raíz del repositorio):
    python benchmarks/benchmark_rv32_coremark.py [--rondas 3] [--binario ruta.bin] [--gas 20000000] [--json informe.json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import sys
import time
from pathlib import Path

from cpu_digital.rv32_platform import Machine

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BINARY = ROOT / "benchmarks" / "rv32_arranque" / "bin" / "coremark_rv32i_zicsr_it10.bin"
MODES = (("intérprete", False, True), ("bloques sin horizonte", True, False), ("bloques con horizonte", True, True))
INTERPRETER_MINIMUM = 400_000     # instr/s
BLOCKS_MINIMUM_RATIO = 2.0        # bloques con horizonte frente al intérprete
HORIZON_MAX_COST = 0.10           # frente a los bloques sin horizonte


def run_mode(image: bytes, accelerate: bool, horizon: bool, gas: int, ram: int) -> dict[str, object]:
    machine = Machine(ram_bytes=ram)
    try:
        machine.load_flat(image)
        machine.hart.accelerate = accelerate
        machine.hart.horizon = horizon
        started = time.perf_counter()
        stop = machine.run(gas)
        elapsed = time.perf_counter() - started
        hart = machine.hart
        text = machine.uart_text
        crc = {line.split(":")[0].strip(): line.split(":")[1].strip() for line in text.splitlines() if "crc" in line}
        return {
            "parada": stop.reason, "instrucciones": hart.instret, "segundos": elapsed,
            "instr_por_segundo": hart.instret / elapsed if elapsed else 0.0,
            "instrucciones_en_bloques": hart.block_instructions,
            "cobertura": hart.block_instructions / hart.instret if hart.instret else 0.0,
            "compilaciones": hart.compilations, "bloques": len([b for b in hart.blocks.values() if b is not None]),
            "gas_restante": hart.gas, "crc": crc, "validado": "Correct operation validated" in text,
        }
    finally:
        machine.close()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(description="E3 de RFC-EXP-00018: CoreMark rv32i en el hart")
    parser.add_argument("--rondas", type=int, default=3)
    parser.add_argument("--binario", type=Path, default=DEFAULT_BINARY)
    parser.add_argument("--gas", type=int, default=20_000_000)
    parser.add_argument("--ram", type=int, default=1 << 20)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    if args.rondas < 1 or args.gas < 1:
        parser.error("rondas y gas deben ser positivos")
    image = args.binario.read_bytes()
    print(f"Python {platform.python_version()} ({platform.system()} {platform.machine()}); {args.binario.name} "
          f"sha256 {hashlib.sha256(image).hexdigest()[:16]}…; rondas: {args.rondas}")
    results: dict[str, dict[str, object]] = {}
    for name, accelerate, horizon in MODES:
        rounds = [run_mode(image, accelerate, horizon, args.gas, args.ram) for _ in range(args.rondas)]
        best = min(rounds, key=lambda r: float(r["segundos"]))
        best = dict(best)
        best["segundos_mediana"] = statistics.median(float(r["segundos"]) for r in rounds)
        results[name] = best
        print(f"\n{name}: {best['instrucciones']:,} instrucciones, parada «{best['parada']}», "
              f"{best['segundos']:.2f} s (mediana {best['segundos_mediana']:.2f}), {best['instr_por_segundo'] / 1e6:.2f} M instr/s; "
              f"bloques: {best['cobertura']:.1%} de las instrucciones, {best['compilaciones']} compilaciones, "
              f"gas restante {best['gas_restante']:,}; CRC {'validados' if best['validado'] else best['crc']}")
    interpreter = results["intérprete"]
    no_horizon = results["bloques sin horizonte"]
    with_horizon = results["bloques con horizonte"]
    same_count = len({int(r["instrucciones"]) for r in results.values()}) == 1
    same_crc = len({json.dumps(r["crc"], sort_keys=True) for r in results.values()}) == 1
    ratio = float(with_horizon["instr_por_segundo"]) / float(interpreter["instr_por_segundo"])
    horizon_cost = float(no_horizon["segundos"]) / float(with_horizon["segundos"])
    horizon_cost = 1 / horizon_cost - 1 if horizon_cost else float("inf")
    verdict = {
        "mismo_recuento": same_count, "mismos_crc": same_crc,
        "interprete_instr_s": interpreter["instr_por_segundo"],
        "interprete_sobrevive": float(interpreter["instr_por_segundo"]) >= INTERPRETER_MINIMUM,
        "bloques_sobre_interprete": ratio, "bloques_sobreviven": ratio >= BLOCKS_MINIMUM_RATIO,
        "coste_horizonte": horizon_cost, "horizonte_se_conserva": horizon_cost <= HORIZON_MAX_COST,
        "gas_exacto_con_horizonte": int(with_horizon["gas_restante"]) >= 0 and int(with_horizon["instrucciones"]) == args.gas - int(with_horizon["gas_restante"]),
    }
    print(f"\nMismo recuento en los tres modos: {same_count}; mismos CRC: {same_crc}; gas exacto con horizonte: "
          f"{verdict['gas_exacto_con_horizonte']}")
    print(f"Intérprete: {float(interpreter['instr_por_segundo']) / 1e6:.2f} M instr/s → "
          f"{'sobrevive' if verdict['interprete_sobrevive'] else 'MUERE P1'} (umbral 0,4 M)")
    print(f"Bloques con horizonte / intérprete: {ratio:.2f}× → {'sobreviven' if verdict['bloques_sobreviven'] else 'MUERE P1'} (umbral 2×)")
    print(f"Coste del horizonte: {horizon_cost * 100:+.1f} % → {'se conserva' if verdict['horizonte_se_conserva'] else 'SE RETIRA'} (umbral 10 %)")
    if args.json:
        args.json.write_text(json.dumps({"python": platform.python_version(), "binario": args.binario.name,
                                         "rondas": args.rondas, "modos": results, "veredictos": verdict},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
