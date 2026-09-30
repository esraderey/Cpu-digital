"""Herramientas de host para modelos tipo llama2.c sobre el TNU.

* Convierte un checkpoint de llama2.c (formato legado, float32) y su
  ``tokenizer.bin`` en una imagen de ROM TNU (float32 o Q8 por fila).
* Codifica prompts con el BPE de llama2.c (en el host; ver RFC §P5).
* ``reference_generate`` encadena los mismos núcleos de ``tnu`` en el mismo orden
  que ``vm_programs/llama2.tasm``: es el oráculo diferencial del programa
  invitado (mismos tokens, bit a bit).

Formato de la ROM (palabras little-endian; direcciones absolutas en la ventana
``ROM_BASE``): cabecera de 64 palabras, tensores y tabla de vocabulario. Cada
pieza del vocabulario se guarda como una cadena terminada en cero con un punto
de código por palabra, lista para la syscall ``print_string``.
"""

from __future__ import annotations

import math
import random
import struct
from array import array
from dataclasses import dataclass
from pathlib import Path

from . import tnu
from .tnu import ROM_BASE, TensorROM

ROM_MAGIC = int.from_bytes(b"TNU1", "little")
ROM_VERSION = 1
HEADER_WORDS = 64
QUANT_F32 = 0
QUANT_Q8 = 1
BOS = 1

# Índices de la cabecera (compartidos con vm_programs/llama2.tasm).
H_MAGIC, H_VERSION, H_QUANT = 0, 1, 2
H_DIM, H_HIDDEN, H_LAYERS, H_HEADS, H_KV_HEADS, H_VOCAB, H_SEQ = 3, 4, 5, 6, 7, 8, 9
H_HEAD_SIZE, H_KV_DIM, H_KV_MUL, H_INV_SQRT_HS = 10, 11, 12, 13
H_TOK_EMB, H_RMS_ATT, H_WQ, H_WK, H_WV, H_WO, H_RMS_FFN = 16, 17, 18, 19, 20, 21, 22
H_W1, H_W2, H_W3, H_RMS_FINAL, H_WCLS, H_VOCAB_TABLE = 23, 24, 25, 26, 27, 28
H_WQ_STRIDE, H_WK_STRIDE, H_WV_STRIDE, H_WO_STRIDE = 32, 33, 34, 35
H_W1_STRIDE, H_W2_STRIDE, H_W3_STRIDE = 36, 37, 38

_MATRICES = ("wq", "wk", "wv", "wo", "w1", "w2", "w3")


@dataclass(frozen=True, slots=True)
class Llama2Config:
    dim: int
    hidden_dim: int
    n_layers: int
    n_heads: int
    n_kv_heads: int
    vocab_size: int
    seq_len: int

    @property
    def head_size(self) -> int:
        return self.dim // self.n_heads

    @property
    def kv_dim(self) -> int:
        return self.n_kv_heads * self.head_size

    def matrix_shape(self, name: str) -> tuple[int, int]:
        """(filas, columnas) de cada matriz por capa, como en ``matmul`` de llama2.c."""
        return {
            "wq": (self.dim, self.dim),
            "wk": (self.kv_dim, self.dim),
            "wv": (self.kv_dim, self.dim),
            "wo": (self.dim, self.dim),
            "w1": (self.hidden_dim, self.dim),
            "w2": (self.dim, self.hidden_dim),
            "w3": (self.hidden_dim, self.dim),
        }[name]


def read_checkpoint(data: bytes) -> tuple[Llama2Config, dict[str, memoryview]]:
    """Separa un checkpoint legado de llama2.c en tensores float32 (vistas sin copia)."""
    if len(data) < 28:
        raise ValueError("Checkpoint truncado")
    values = struct.unpack_from("<7i", data)
    shared = values[5] > 0
    config = Llama2Config(values[0], values[1], values[2], values[3], values[4], abs(values[5]), values[6])
    if min(values[:5]) < 1 or config.seq_len < 1 or config.dim % config.n_heads or config.n_heads % config.n_kv_heads:
        raise ValueError("Cabecera de checkpoint inválida")
    floats = memoryview(data)[28:].cast("f") if (len(data) - 28) % 4 == 0 else None
    if floats is None:
        raise ValueError("Checkpoint con tamaño no alineado a float32")
    c, layers = config, config.n_layers
    sizes = [
        ("tok_emb", c.vocab_size * c.dim),
        ("rms_att", layers * c.dim),
        ("wq", layers * c.dim * c.dim),
        ("wk", layers * c.dim * c.kv_dim),
        ("wv", layers * c.dim * c.kv_dim),
        ("wo", layers * c.dim * c.dim),
        ("rms_ffn", layers * c.dim),
        ("w1", layers * c.dim * c.hidden_dim),
        ("w2", layers * c.hidden_dim * c.dim),
        ("w3", layers * c.dim * c.hidden_dim),
        ("rms_final", c.dim),
        ("freq_cis", c.seq_len * c.head_size),
    ]
    if not shared:
        sizes.append(("wcls", c.vocab_size * c.dim))
    tensors: dict[str, memoryview] = {}
    cursor = 0
    for name, size in sizes:
        tensors[name] = floats[cursor:cursor + size]
        cursor += size
    if cursor != len(floats):
        raise ValueError(f"El checkpoint no coincide con su cabecera ({cursor} ≠ {len(floats)} floats)")
    del tensors["freq_cis"]
    tensors.setdefault("wcls", tensors["tok_emb"])
    return config, tensors


def read_tokenizer(data: bytes, vocab_size: int) -> tuple[list[bytes], list[float]]:
    pieces: list[bytes] = []
    scores: list[float] = []
    cursor = 4  # max_token_length
    for _ in range(vocab_size):
        score, length = struct.unpack_from("<fi", data, cursor)
        cursor += 8
        if length < 0 or cursor + length > len(data):
            raise ValueError("Tokenizer truncado o corrupto")
        pieces.append(data[cursor:cursor + length])
        scores.append(score)
        cursor += length
    return pieces, scores


def display_piece(piece: bytes) -> str:
    """Texto de una pieza: ``<0xXX>`` es un byte crudo; el resto, UTF-8."""
    if len(piece) == 6 and piece.startswith(b"<0x") and piece.endswith(b">"):
        try:
            return chr(int(piece[3:5], 16))
        except ValueError:
            pass
    return piece.decode("utf-8", errors="replace")


def _quantize_rows(values: memoryview, rows: int, cols: int) -> bytes:
    """Bloque Q8 de una matriz: bytes (q + 128) fila a fila y luego una escala float32 por fila."""
    quantized = bytearray()
    scales = array("f")
    for row in range(rows):
        levels, scale = tnu.quantize(values[row * cols:(row + 1) * cols])
        quantized += levels
        scales.append(scale)
    return bytes(quantized) + scales.tobytes()


def build_rom(checkpoint: bytes, tokenizer: bytes, *, quant: str = "f32") -> bytes:
    """Imagen de ROM TNU para un checkpoint de llama2.c y su tokenizer."""
    if quant not in {"f32", "q8"}:
        raise ValueError("quant debe ser 'f32' o 'q8'")
    config, tensors = read_checkpoint(checkpoint)
    pieces, _ = read_tokenizer(tokenizer, config.vocab_size)
    q8 = quant == "q8"
    if q8 and (config.dim % 4 or config.hidden_dim % 4):
        raise ValueError("Q8 requiere dim y hidden_dim múltiplos de 4")
    header = [0] * HEADER_WORDS
    chunks: list[bytes] = []
    cursor = HEADER_WORDS

    def place(blob: bytes) -> int:
        nonlocal cursor
        address = ROM_BASE + cursor
        chunks.append(blob)
        cursor += len(blob) // 4
        return address

    def matrix(name: str, header_index: int, stride_index: int) -> None:
        rows, cols = config.matrix_shape(name)
        per_layer = rows * cols
        view = tensors[name]
        if q8:
            blob = b"".join(
                _quantize_rows(view[layer * per_layer:(layer + 1) * per_layer], rows, cols)
                for layer in range(config.n_layers)
            )
            header[stride_index] = rows * cols // 4 + rows
        else:
            blob = view.tobytes()
            header[stride_index] = per_layer
        header[header_index] = place(blob)

    if q8:
        header[H_TOK_EMB] = place(_quantize_rows(tensors["tok_emb"], config.vocab_size, config.dim))
        shared = tensors["wcls"] is tensors["tok_emb"]
        header[H_WCLS] = (
            header[H_TOK_EMB] if shared else place(_quantize_rows(tensors["wcls"], config.vocab_size, config.dim))
        )
    else:
        header[H_TOK_EMB] = place(tensors["tok_emb"].tobytes())
    header[H_RMS_ATT] = place(tensors["rms_att"].tobytes())
    matrix("wq", H_WQ, H_WQ_STRIDE)
    matrix("wk", H_WK, H_WK_STRIDE)
    matrix("wv", H_WV, H_WV_STRIDE)
    matrix("wo", H_WO, H_WO_STRIDE)
    header[H_RMS_FFN] = place(tensors["rms_ffn"].tobytes())
    matrix("w1", H_W1, H_W1_STRIDE)
    matrix("w2", H_W2, H_W2_STRIDE)
    matrix("w3", H_W3, H_W3_STRIDE)
    header[H_RMS_FINAL] = place(tensors["rms_final"].tobytes())
    if not q8:
        shared = tensors["wcls"] is tensors["tok_emb"]
        header[H_WCLS] = header[H_TOK_EMB] if shared else place(tensors["wcls"].tobytes())

    strings = array("i")
    table = array("i")
    strings_base = cursor + config.vocab_size
    for piece in pieces:
        table.append(ROM_BASE + strings_base + len(strings))
        strings.extend(ord(char) for char in display_piece(piece))
        strings.append(0)
    header[H_VOCAB_TABLE] = place(table.tobytes())
    place(strings.tobytes())

    header[H_MAGIC] = ROM_MAGIC
    header[H_VERSION] = ROM_VERSION
    header[H_QUANT] = QUANT_Q8 if q8 else QUANT_F32
    header[H_DIM:H_SEQ + 1] = [
        config.dim, config.hidden_dim, config.n_layers, config.n_heads,
        config.n_kv_heads, config.vocab_size, config.seq_len,
    ]
    header[H_HEAD_SIZE] = config.head_size
    header[H_KV_DIM] = config.kv_dim
    header[H_KV_MUL] = config.n_heads // config.n_kv_heads
    header[H_INV_SQRT_HS] = struct.unpack("<i", struct.pack("<f", 1.0 / math.sqrt(config.head_size)))[0]
    return array("i", header).tobytes() + b"".join(chunks)


def encode(text: str, pieces: list[bytes], scores: list[float], *, bos: bool = True) -> list[int]:
    """BPE de llama2.c: prefijo de espacio, respaldo por bytes y fusiones por puntuación."""
    lookup = {piece: index for index, piece in enumerate(pieces)}
    tokens: list[int] = [BOS] if bos else []
    if text:
        tokens.append(lookup[b" "])
    for char in text:
        encoded = char.encode("utf-8")
        if encoded in lookup:
            tokens.append(lookup[encoded])
        else:
            tokens.extend(byte + 3 for byte in encoded)
    while True:
        best = (-1e10, -1, -1)
        for index in range(len(tokens) - 1):  # como llama2.c, BOS también participa
            merged = lookup.get(pieces[tokens[index]] + pieces[tokens[index + 1]])
            if merged is not None and scores[merged] > best[0]:
                best = (scores[merged], merged, index)
        if best[1] == -1:
            return tokens
        tokens[best[2]:best[2] + 2] = [best[1]]


def decode_tokens(tokens: list[int], pieces: list[bytes], first: int = BOS) -> str:
    """Texto que imprime el invitado para ``tokens`` generados tras ``first``."""
    out = []
    previous = first
    for token in tokens:
        text = display_piece(pieces[token])
        if previous == BOS and text.startswith(" "):
            text = text[1:]
        out.append(text)
        previous = token
    return "".join(out)


# ── Referencia de host (oráculo diferencial) ──────────────────────────────

class _Rom:
    def __init__(self, rom: TensorROM) -> None:
        self.rom = rom
        self.header = [rom.word(index) for index in range(HEADER_WORDS)]
        if self.header[H_MAGIC] != ROM_MAGIC or self.header[H_VERSION] != ROM_VERSION:
            raise ValueError("La ROM no es una imagen TNU de llama2")

    def floats(self, address: int, count: int) -> memoryview:
        return self.rom.bytes_view(address - ROM_BASE, count).cast("f")

    def raw(self, address: int, count: int) -> memoryview:
        return self.rom.bytes_view(address - ROM_BASE, count)


def xorshift32(state: int) -> int:
    state ^= (state << 13) & 0xFFFFFFFF
    state ^= state >> 17
    state ^= (state << 5) & 0xFFFFFFFF
    return state & 0xFFFFFFFF


def f32_bits(value: float) -> int:
    return struct.unpack("<i", struct.pack("<f", value))[0]


def reference_generate(
    rom: TensorROM,
    *,
    steps: int,
    inverse_temperature: float = 0.0,
    seed: int = 1,
    prompt: list[int] | None = None,
) -> list[int]:
    """Tokens que genera ``llama2.tasm`` con las mismas entradas (0.0 = voraz)."""
    image = _Rom(rom)
    h = image.header
    dim, hidden, layers, heads = h[H_DIM], h[H_HIDDEN], h[H_LAYERS], h[H_HEADS]
    vocab, seq_len, hs, kv_dim, kv_mul = h[H_VOCAB], h[H_SEQ], h[H_HEAD_SIZE], h[H_KV_DIM], h[H_KV_MUL]
    q8 = h[H_QUANT] == QUANT_Q8
    inv_sqrt_hs = struct.unpack("<f", struct.pack("<i", h[H_INV_SQRT_HS]))[0]
    inverse_temperature = array("f", [inverse_temperature])[0]
    key_cache = array("f", bytes(4 * layers * seq_len * kv_dim))
    value_cache = array("f", bytes(4 * layers * seq_len * kv_dim))
    prompt = list(prompt or [BOS])
    state = seed & 0xFFFFFFFF or 1
    steps = steps if 0 < steps <= seq_len else seq_len

    def linear(name_index: int, stride_index: int, layer: int, x: array, rows: int, cols: int) -> array:
        base = h[name_index] + layer * h[stride_index]
        if q8:
            levels, scale = tnu.quantize(x)
            block = image.raw(base, rows * cols // 4 + rows)
            return tnu.qmatvec(block[:rows * cols], block[rows * cols:].cast("f"), rows, cols,
                               memoryview(levels), array("f", [scale])[0])
        return tnu.matvec(image.floats(base, rows * cols), rows, cols, cols, x)

    generated: list[int] = []
    token = prompt[0]
    for pos in range(steps):
        if q8:
            block = image.raw(h[H_TOK_EMB], vocab * dim // 4 + vocab)
            scale = block[vocab * dim:].cast("f")[token]
            x = tnu.qrow(block[token * dim:(token + 1) * dim], scale)
        else:
            x = array("f", image.floats(h[H_TOK_EMB] + token * dim, dim))
        for layer in range(layers):
            xb = tnu.rmsnorm(x, image.floats(h[H_RMS_ATT] + layer * dim, dim))
            q = linear(H_WQ, H_WQ_STRIDE, layer, xb, dim, dim)
            slot = (layer * seq_len + pos) * kv_dim
            key_cache[slot:slot + kv_dim] = linear(H_WK, H_WK_STRIDE, layer, xb, kv_dim, dim)
            value_cache[slot:slot + kv_dim] = linear(H_WV, H_WV_STRIDE, layer, xb, kv_dim, dim)
            q = tnu.rope(q, pos, hs)
            key_cache[slot:slot + kv_dim] = tnu.rope(key_cache[slot:slot + kv_dim], pos, hs)
            q = tnu.vscale(q, inv_sqrt_hs)
            keys = memoryview(key_cache)
            values = memoryview(value_cache)
            for head in range(heads):
                base = layer * seq_len * kv_dim + (head // kv_mul) * hs
                span = pos * kv_dim + hs
                att = tnu.matvec(keys[base:base + span], pos + 1, hs, kv_dim, q[head * hs:(head + 1) * hs])
                att = tnu.softmax(att)
                xb[head * hs:(head + 1) * hs] = tnu.mattv(values[base:base + span], pos + 1, hs, kv_dim, att)
            xb2 = linear(H_WO, H_WO_STRIDE, layer, xb, dim, dim)
            x = tnu.vadd(x, xb2)
            xb = tnu.rmsnorm(x, image.floats(h[H_RMS_FFN] + layer * dim, dim))
            hb = linear(H_W1, H_W1_STRIDE, layer, xb, hidden, dim)
            hb2 = linear(H_W3, H_W3_STRIDE, layer, xb, hidden, dim)
            hb = tnu.vmul(tnu.silu(hb), hb2)
            x = tnu.vadd(x, linear(H_W2, H_W2_STRIDE, layer, hb, dim, hidden))
        x = tnu.rmsnorm(x, image.floats(h[H_RMS_FINAL], dim))
        logits = linear(H_WCLS, H_WCLS, 0, x, vocab, dim)  # capa 0: el paso no se usa
        if pos < len(prompt) - 1:
            following = prompt[pos + 1]
        elif inverse_temperature == 0.0:
            following = tnu.argmax(logits)
        else:
            probabilities = tnu.softmax(tnu.vscale(logits, inverse_temperature))
            state = xorshift32(state)
            coin = float(state >> 8) * 2.0**-24
            following = tnu.sample(probabilities, coin)
        if following == BOS:
            break
        generated.append(following)
        token = following
    return generated


def synthetic_checkpoint(
    *,
    dim: int = 16,
    hidden_dim: int = 32,
    n_layers: int = 2,
    n_heads: int = 4,
    n_kv_heads: int = 2,
    vocab_size: int = 40,
    seq_len: int = 24,
    seed: int = 7,
) -> tuple[bytes, bytes]:
    """Checkpoint y tokenizer aleatorios pero deterministas, con la forma de llama2.c."""
    rng = random.Random(seed)
    config = Llama2Config(dim, hidden_dim, n_layers, n_heads, n_kv_heads, vocab_size, seq_len)
    count = (
        vocab_size * dim + n_layers * dim * 2
        + n_layers * (2 * dim * dim + 2 * dim * config.kv_dim + 3 * dim * hidden_dim)
        + dim + seq_len * config.head_size
    )
    weights = array("f", (rng.gauss(0.0, 0.5) for _ in range(count)))
    checkpoint = struct.pack("<7i", dim, hidden_dim, n_layers, n_heads, n_kv_heads, vocab_size, seq_len)
    pieces = [b"<unk>", b"<s>", b"</s>"] + [f" p{index}".encode() for index in range(3, vocab_size)]
    tokenizer = bytearray(struct.pack("<i", max(map(len, pieces))))
    for index, piece in enumerate(pieces):
        tokenizer += struct.pack("<fi", float(-index), len(piece)) + piece
    return checkpoint + weights.tobytes(), bytes(tokenizer)


def load_rom_file(path: str | Path) -> TensorROM:
    return TensorROM.from_file(path)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Construye una ROM TNU desde un checkpoint de llama2.c")
    parser.add_argument("checkpoint", type=Path, help="Checkpoint legado de llama2.c (.bin)")
    parser.add_argument("tokenizer", type=Path, help="tokenizer.bin / tok512.bin")
    parser.add_argument("output", type=Path, help="Archivo de ROM a escribir")
    parser.add_argument("--quant", choices=("f32", "q8"), default="f32")
    parser.add_argument("--prompt", help="Imprime los tokens de este prompt para --input del CLI")
    args = parser.parse_args(argv)
    checkpoint = args.checkpoint.read_bytes()
    tokenizer = args.tokenizer.read_bytes()
    image = build_rom(checkpoint, tokenizer, quant=args.quant)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(image)
    rom = TensorROM(image)
    print(f"ROM: {args.output} ({rom.words} palabras, sha256={rom.sha256})")
    if args.prompt is not None:
        config, _ = read_checkpoint(checkpoint)
        pieces, scores = read_tokenizer(tokenizer, config.vocab_size)
        tokens = encode(args.prompt, pieces, scores)
        print("Tokens:", len(tokens), *tokens)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BOS",
    "Llama2Config",
    "build_rom",
    "decode_tokens",
    "display_piece",
    "encode",
    "f32_bits",
    "load_rom_file",
    "read_checkpoint",
    "read_tokenizer",
    "reference_generate",
    "synthetic_checkpoint",
    "xorshift32",
]
