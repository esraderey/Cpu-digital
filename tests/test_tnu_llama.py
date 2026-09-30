"""Anclas de extremo a extremo: llama2.tasm sobre el TNU frente a dos oráculos.

* Oráculo diferencial: ``reference_generate`` (mismos núcleos, mismo orden) debe
  dar exactamente los mismos tokens que el programa invitado.
* Oráculo independiente: un llama2.c ingenuo en doble precisión, escrito aquí sin
  usar ``build_rom`` ni los núcleos TNU, debe coincidir en los tokens voraces.
"""

from __future__ import annotations

import math
import struct
import unittest
from pathlib import Path

from cpu_digital import tnu_models as tm
from cpu_digital.tnu import ROM_BASE, TensorROM, TramoyaNeuralUnit
from cpu_digital.vm32 import TramoyaVM32, VMConfig
from cpu_digital.vm32_assembler import VM32Assembler

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"
PROGRAM = VM32Assembler().assemble(
    (ROOT / "vm_programs" / "llama2.tasm").read_text(encoding="utf-8"), "llama2.tasm"
).program
CAPS = frozenset({"io", "memory", "npu"})
if not hasattr(math, "sumprod"):  # el resto de VM32 soporta 3.10; el TNU exige 3.12
    raise unittest.SkipTest("El TNU requiere Python 3.12 o superior (math.sumprod)")

CHECKPOINT, TOKENIZER = tm.synthetic_checkpoint()


def make_vm(rom: TensorROM, inputs: list[int], **config) -> TramoyaVM32:
    config.setdefault("gas_limit", 10**9)
    config.setdefault("trace_size", 0)
    vm = TramoyaVM32(VMConfig(capabilities=CAPS, **config), npu=TramoyaNeuralUnit(rom))
    vm.load_program(PROGRAM, inputs=inputs)
    return vm


def generated(vm: TramoyaVM32) -> list[int]:
    count = vm.read_memory(PROGRAM.symbols["NGEN"])
    return list(vm.memory_slice(vm.read_memory(PROGRAM.symbols["TOKENS_PTR"]), count))


def text(vm: TramoyaVM32) -> str:
    return "".join(str(value) for value in vm.result().output)


def naive_generate(checkpoint: bytes, steps: int) -> list[int]:
    """llama2.c voraz en doble precisión, leído directamente del checkpoint."""
    dim, hidden, layers, heads, kv_heads, vocab, seq_len = struct.unpack_from("<7i", checkpoint)
    hs = dim // heads
    kv_dim = kv_heads * hs
    weights = struct.unpack_from(f"<{(len(checkpoint) - 28) // 4}f", checkpoint, 28)
    offsets = {}
    cursor = 0
    for name, size in (
        ("emb", vocab * dim), ("rms_att", layers * dim), ("wq", layers * dim * dim),
        ("wk", layers * dim * kv_dim), ("wv", layers * dim * kv_dim), ("wo", layers * dim * dim),
        ("rms_ffn", layers * dim), ("w1", layers * dim * hidden), ("w2", layers * hidden * dim),
        ("w3", layers * dim * hidden), ("rms_final", dim),
    ):
        offsets[name] = cursor
        cursor += size

    def matmul(base: int, x: list[float], n: int, d: int) -> list[float]:
        return [sum(weights[base + i * n + j] * x[j] for j in range(n)) for i in range(d)]

    def rmsnorm(x: list[float], base: int) -> list[float]:
        ss = 1.0 / math.sqrt(sum(v * v for v in x) / len(x) + 1e-5)
        return [weights[base + j] * (ss * x[j]) for j in range(len(x))]

    key_cache = [[[0.0] * kv_dim for _ in range(seq_len)] for _ in range(layers)]
    value_cache = [[[0.0] * kv_dim for _ in range(seq_len)] for _ in range(layers)]
    token, out = tm.BOS, []
    for pos in range(steps):
        x = list(weights[offsets["emb"] + token * dim:offsets["emb"] + (token + 1) * dim])
        for layer in range(layers):
            xb = rmsnorm(x, offsets["rms_att"] + layer * dim)
            q = matmul(offsets["wq"] + layer * dim * dim, xb, dim, dim)
            k = matmul(offsets["wk"] + layer * dim * kv_dim, xb, dim, kv_dim)
            v = matmul(offsets["wv"] + layer * dim * kv_dim, xb, dim, kv_dim)
            for i in range(0, dim, 2):
                freq = 1.0 / (10000.0 ** ((i % hs) / hs))
                c, s = math.cos(pos * freq), math.sin(pos * freq)
                for vec in ((q, k) if i < kv_dim else (q,)):
                    vec[i], vec[i + 1] = vec[i] * c - vec[i + 1] * s, vec[i] * s + vec[i + 1] * c
            key_cache[layer][pos], value_cache[layer][pos] = k, v
            for h in range(heads):
                kvh = h // (heads // kv_heads)
                qh = q[h * hs:(h + 1) * hs]
                scores = [
                    sum(qh[i] * key_cache[layer][t][kvh * hs + i] for i in range(hs)) / math.sqrt(hs)
                    for t in range(pos + 1)
                ]
                peak = max(scores)
                exps = [math.exp(sc - peak) for sc in scores]
                total = sum(exps)
                for i in range(hs):
                    xb[h * hs + i] = sum(exps[t] / total * value_cache[layer][t][kvh * hs + i] for t in range(pos + 1))
            x = [a + b for a, b in zip(x, matmul(offsets["wo"] + layer * dim * dim, xb, dim, dim))]
            xb = rmsnorm(x, offsets["rms_ffn"] + layer * dim)
            hb = matmul(offsets["w1"] + layer * dim * hidden, xb, dim, hidden)
            hb2 = matmul(offsets["w3"] + layer * dim * hidden, xb, dim, hidden)
            hb = [a / (1.0 + math.exp(-a)) * b for a, b in zip(hb, hb2)]
            x = [a + b for a, b in zip(x, matmul(offsets["w2"] + layer * hidden * dim, hb, hidden, dim))]
        x = rmsnorm(x, offsets["rms_final"])
        logits = matmul(offsets["emb"], x, dim, vocab)
        token = max(range(vocab), key=logits.__getitem__)
        if token == tm.BOS:
            break
        out.append(token)
    return out


class GuestMatchesOracles(unittest.TestCase):
    def test_guest_equals_differential_reference(self) -> None:
        for quant in ("f32", "q8"):
            rom = TensorROM(tm.build_rom(CHECKPOINT, TOKENIZER, quant=quant))
            for inverse_temperature, seed in ((0.0, 1), (1.25, 99)):
                with self.subTest(quant=quant, invt=inverse_temperature):
                    vm = make_vm(rom, [18, tm.f32_bits(inverse_temperature), seed, 0])
                    result = vm.run()
                    self.assertEqual(result.state, "HALTED", result.fault)
                    reference = tm.reference_generate(
                        rom, steps=18, inverse_temperature=inverse_temperature, seed=seed
                    )
                    self.assertEqual(generated(vm), reference)
                    self.assertTrue(reference)
                    pieces, _ = tm.read_tokenizer(TOKENIZER, 40)
                    self.assertEqual(text(vm), tm.decode_tokens(reference, pieces))

    def test_greedy_f32_matches_independent_naive_llama(self) -> None:
        rom = TensorROM(tm.build_rom(CHECKPOINT, TOKENIZER, quant="f32"))
        vm = make_vm(rom, [12, 0, 1, 0])
        vm.run()
        self.assertEqual(generated(vm), naive_generate(CHECKPOINT, 12))

    def test_same_seed_same_tokens_and_seed_matters(self) -> None:
        rom = TensorROM(tm.build_rom(CHECKPOINT, TOKENIZER))
        runs = []
        for seed in (5, 5, 6):
            vm = make_vm(rom, [16, tm.f32_bits(1.0), seed, 0])
            vm.run()
            runs.append((generated(vm), text(vm)))
        self.assertEqual(runs[0], runs[1])
        self.assertNotEqual(runs[0][0], runs[2][0])

    def test_snapshot_mid_generation_resumes_identically(self) -> None:
        rom = TensorROM(tm.build_rom(CHECKPOINT, TOKENIZER))
        inputs = [16, tm.f32_bits(1.0), 3, 0]
        full = make_vm(rom, inputs)
        full.run()
        partial = make_vm(rom, inputs)
        partial.run(max_instructions=2_500)
        self.assertEqual(partial.state, "PAUSED")
        snapshot = partial.snapshot_bytes()
        self.assertLess(len(snapshot), 60_000)
        resumed = TramoyaVM32.from_snapshot_bytes(snapshot, capabilities=CAPS, npu=TramoyaNeuralUnit(rom))
        resumed.run()
        self.assertEqual(generated(resumed), generated(full))
        self.assertEqual(text(resumed), text(full))
        self.assertEqual(resumed.result().gas_remaining, full.result().gas_remaining)

    def test_prompt_tokens_are_forced_then_generation_continues(self) -> None:
        rom = TensorROM(tm.build_rom(CHECKPOINT, TOKENIZER))
        prompt = [tm.BOS, 7, 9, 11]
        vm = make_vm(rom, [10, 0, 1, len(prompt), *prompt])
        vm.run()
        tokens = generated(vm)
        self.assertEqual(tokens[:3], [7, 9, 11])
        self.assertEqual(tokens, tm.reference_generate(rom, steps=10, prompt=prompt))

    def test_invalid_prompt_token_exits_with_code_2(self) -> None:
        rom = TensorROM(tm.build_rom(CHECKPOINT, TOKENIZER))
        result = make_vm(rom, [5, 0, 1, 2, tm.BOS, 40]).run()
        self.assertEqual((result.state, result.exit_code), ("HALTED", 2))

    def test_negative_zero_inverse_temperature_is_greedy_like_reference(self) -> None:
        # Ancla G4-D3: el invitado comparaba los bits y trataba -0.0 como muestreo.
        rom = TensorROM(tm.build_rom(CHECKPOINT, TOKENIZER))
        vm = make_vm(rom, [12, tm.f32_bits(-0.0), 5, 0])
        vm.run()
        self.assertEqual(generated(vm), tm.reference_generate(rom, steps=12, inverse_temperature=-0.0, seed=5))
        self.assertEqual(generated(vm), tm.reference_generate(rom, steps=12))

    def test_unknown_rom_exits_with_code_3(self) -> None:
        result = make_vm(TensorROM(bytes(256)), [5, 0, 1, 0]).run()
        self.assertEqual((result.state, result.exit_code), ("HALTED", 3))

    def test_gas_exhaustion_mid_generation_faults_on_a_prefix(self) -> None:
        rom = TensorROM(tm.build_rom(CHECKPOINT, TOKENIZER))
        full = make_vm(rom, [16, 0, 1, 0])
        full.run()
        used = 10**9 - full.result().gas_remaining
        starved = make_vm(rom, [16, 0, 1, 0], gas_limit=used // 2)
        result = starved.run()
        self.assertEqual(result.state, "FAULTED")
        self.assertIn("Gas agotado", result.fault)
        partial = generated(starved)
        self.assertEqual(partial, generated(full)[:len(partial)])

    def test_rom_header_and_shared_q8_classifier(self) -> None:
        rom = TensorROM(tm.build_rom(CHECKPOINT, TOKENIZER, quant="q8"))
        self.assertEqual(rom.word(tm.H_MAGIC), tm.ROM_MAGIC)
        self.assertEqual(rom.word(tm.H_QUANT), tm.QUANT_Q8)
        self.assertEqual(rom.word(tm.H_WCLS), rom.word(tm.H_TOK_EMB))
        self.assertGreaterEqual(rom.word(tm.H_TOK_EMB), ROM_BASE + tm.HEADER_WORDS)

    def test_checkpoint_size_must_match_header(self) -> None:
        with self.assertRaises(ValueError):
            tm.read_checkpoint(CHECKPOINT[:-4])


@unittest.skipUnless((MODELS / "stories260K.bin").exists(), "modelo real no descargado (models/ fuera de git)")
class RealModelSmoke(unittest.TestCase):
    def test_stories260k_generates_20_tokens_equal_to_reference(self) -> None:
        checkpoint = (MODELS / "stories260K.bin").read_bytes()
        tokenizer = (MODELS / "tok512.bin").read_bytes()
        rom = TensorROM(tm.build_rom(checkpoint, tokenizer))
        pieces, scores = tm.read_tokenizer(tokenizer, 512)
        prompt = tm.encode("Once upon a time", pieces, scores)
        self.assertEqual(tm.decode_tokens(prompt[1:], pieces), "Once upon a time")
        vm = make_vm(rom, [20, 0, 1, len(prompt), *prompt])
        result = vm.run()
        self.assertEqual(result.state, "HALTED", result.fault)
        tokens = generated(vm)
        self.assertEqual(len(tokens), 20)  # un token siguiente por paso; los 4 primeros, forzados
        self.assertEqual(tokens[:4], prompt[1:])
        self.assertEqual(tokens, tm.reference_generate(rom, steps=20, prompt=prompt))
        self.assertTrue(text(vm).startswith("Once upon a time"))


if __name__ == "__main__":
    unittest.main()
