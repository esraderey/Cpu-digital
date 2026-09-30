"""Chip Neuronal Tramoya (TNU): coprocesador vectorial para VM32.

El chip aporta dos cosas al invitado:

* Una ROM de tensores inmutable, mapeada en la ventana ``[ROM_BASE, ROM_BASE +
  words)``, de solo lectura y fuera de los snapshots (el snapshot guarda solo su
  sha256).
* Los núcleos que ejecutan las instrucciones vectoriales TNU en el host. Son
  funciones puras sobre ``memoryview``: la VM valida rangos, cobra gas y
  confirma la escritura; aquí solo se calcula.

Semántica numérica declarada: los operandos son float32 IEEE 754; cada
resultado se calcula en doble precisión (``math.sumprod`` acumula con precisión
extendida) y se redondea una sola vez a float32 al guardarse. ``VADD``, ``VMUL``
y ``VSCALE`` coinciden bit a bit con ``FADD``/``FMUL`` salvo en la carga útil de un
NaN (las dos rutas convierten el NaN de doble a float32 de forma distinta);
``FDOT`` y ``MATVEC`` no coinciden con una cadena de ``FMUL``+``FADD`` porque
redondean una sola vez. Un resultado fuera del rango de float32 se guarda como
±inf, sin fallo.
"""

from __future__ import annotations

import hashlib
import math
import sys
from array import array
from itertools import repeat
from operator import add, mul, neg, truediv
from pathlib import Path
from typing import Sequence

if not hasattr(math, "sumprod"):  # pragma: no cover - depende de la versión de Python
    _sumprod = None
else:
    _sumprod = math.sumprod


ROM_BASE = 0x4000_0000
MAX_ROM_WORDS = (1 << 31) - ROM_BASE
MAX_TNU_LENGTH = 1 << 24
MAX_TNU_WORK = 1 << 26
TNU_GAS_DIVISOR = 64
TNU_ROW_UNITS = 8  # coste fijo por fila/columna de un producto matricial (llamada a sumprod)
Q8_OFFSET = 128
RMS_EPSILON = 1e-5
_EXP_CLAMP = 89.0  # exp(89) supera FLT_MAX: el float32 guardado es +inf.
_EXP_LIMIT = 709.0  # mayor argumento con exp finito en doble precisión
# Hasta este tamaño un operando repetido se pasa a lista: sumprod es ~1,6× más
# rápido con floats ya creados, a cambio de 32 B por elemento. Por encima se
# itera la vista directamente para acotar la memoria transitoria del host.
LIST_LIMIT = 1 << 16


class TensorROM:
    """Imagen de ROM inmutable con huella sha256.

    Se copia a ``bytes`` al construirse: el contenido que se usa es exactamente
    el que se ha medido con el hash, aunque el archivo cambie después.
    """

    __slots__ = ("_data", "_sha256", "_name", "_bytes", "_words", "_floats")

    def __init__(self, data: bytes | bytearray | memoryview, *, name: str = "<memoria>") -> None:
        if sys.byteorder != "little":  # pragma: no cover - plataformas soportadas
            raise RuntimeError("La ROM TNU requiere un host little-endian")
        payload = bytes(data)
        if not payload or len(payload) % 4:
            raise ValueError("La ROM TNU debe tener un tamaño positivo múltiplo de 4 bytes")
        if len(payload) // 4 > MAX_ROM_WORDS:
            raise ValueError(f"La ROM TNU excede {MAX_ROM_WORDS} palabras")
        self._data = payload
        self._sha256 = hashlib.sha256(payload).hexdigest()
        self._name = str(name)
        self._bytes = memoryview(payload)
        self._words = self._bytes.cast("i")
        self._floats = self._bytes.cast("f")

    @classmethod
    def from_file(cls, path: str | Path) -> "TensorROM":
        source = Path(path)
        return cls(source.read_bytes(), name=str(source))

    @property
    def sha256(self) -> str:
        return self._sha256

    @property
    def name(self) -> str:
        return self._name

    @property
    def size_bytes(self) -> int:
        return len(self._data)

    @property
    def words(self) -> int:
        return len(self._data) // 4

    def word(self, index: int) -> int:
        return self._words[index]

    def bytes_view(self, start_word: int, count: int) -> memoryview:
        return self._bytes[start_word * 4:(start_word + count) * 4]

    def reference(self) -> dict[str, object]:
        # Solo el nombre del archivo: el snapshot puede salir del host y la ruta
        # es informativa (nunca se abre al restaurar).
        return {"sha256": self._sha256, "size_bytes": len(self._data), "path": Path(self._name).name}


class TramoyaNeuralUnit:
    """Coprocesador TNU conectado a VM32 tras la capacidad ``npu``.

    No guarda estado mutable: la configuración vectorial (VL, VR, VS) es parte
    del núcleo de la VM, de modo que los snapshots y el rollback la cubren.
    """

    __slots__ = ("_rom",)

    def __init__(self, rom: TensorROM | None = None) -> None:
        if _sumprod is None:  # pragma: no cover - depende de la versión de Python
            raise RuntimeError("El TNU requiere Python 3.12 o superior (math.sumprod)")
        if rom is not None and not isinstance(rom, TensorROM):
            raise TypeError("rom debe ser TensorROM o None")
        self._rom = rom

    @property
    def rom(self) -> TensorROM | None:
        return self._rom


# ── Política de gas ─────────────────────────────────────────────────────
# Unidades de trabajo por elemento (o por MAC en productos). El gas extra de
# una instrucción TNU es ceil(unidades / TNU_GAS_DIVISOR), sumado a su coste
# base de la ISA. Calibración y cifras en RFC-EXP-00016 §P5.

def gas_extra(units: int) -> int:
    return -(-units // TNU_GAS_DIVISOR)


# ── Núcleos ─────────────────────────────────────────────────────────────

def vadd(a: Sequence[float], b: Sequence[float]) -> array:
    return array("f", map(add, a, b))


def vmul(a: Sequence[float], b: Sequence[float]) -> array:
    return array("f", map(mul, a, b))


def vscale(a: Sequence[float], scalar: float) -> array:
    return array("f", map(mul, a, repeat(scalar)))


def fdot(a: Sequence[float], b: Sequence[float]) -> float:
    return _sumprod(a, b)


def _reused(values: Sequence[float]) -> Sequence[float]:
    return list(values) if len(values) <= LIST_LIMIT else values


def matvec(weights: memoryview, rows: int, cols: int, stride: int, x: Sequence[float]) -> array:
    """out[r] = Σ_j W[r·stride + j] · x[j]."""
    xs = _reused(x)
    return array("f", [_sumprod(weights[r * stride:r * stride + cols], xs) for r in range(rows)])


def mattv(weights: memoryview, rows: int, cols: int, stride: int, a: Sequence[float]) -> array:
    """out[j] = Σ_r a[r] · W[r·stride + j] (producto transpuesto, suma ponderada de filas)."""
    coefficients = _reused(a)
    span = (rows - 1) * stride + 1
    return array("f", [_sumprod(coefficients, weights[j:j + span:stride]) for j in range(cols)])


def rmsnorm(a: Sequence[float], weight: Sequence[float]) -> array:
    scale = 1.0 / math.sqrt(_sumprod(a, a) / len(a) + RMS_EPSILON)
    return array("f", map(mul, weight, map(mul, a, repeat(scale))))


def softmax(a: Sequence[float]) -> array:
    peak = max(a)
    exps = array("d", map(math.exp, map(add, a, repeat(-peak))))
    total = math.fsum(exps)
    return array("f", map(truediv, exps, repeat(total)))


def vexp(a: Sequence[float]) -> array:
    return array("f", map(math.exp, map(min, a, repeat(_EXP_CLAMP))))


def silu(a: Sequence[float]) -> array:
    # exp(-x) se limita a 709 (finito en doble): x/(1+e^709) ya es 0 en float32,
    # así que para x muy negativo el resultado tiende a -0 como x·σ(x).
    denominators = map(add, repeat(1.0), map(math.exp, map(min, map(neg, a), repeat(_EXP_LIMIT))))
    return array("f", map(truediv, a, denominators))


def _rope_table(head_size: int, position: int) -> list[tuple[float, float]]:
    # Sin caché global: head_size/2 ángulos por instrucción es trabajo cobrado
    # (VL ≥ head_size) y no deja memoria retenida entre VMs.
    table = []
    for head_dim in range(0, head_size, 2):
        angle = position * (1.0 / (10000.0 ** (head_dim / head_size)))
        table.append((math.cos(angle), math.sin(angle)))
    return table


def rope(v: Sequence[float], position: int, head_size: int) -> array:
    """Rotación RoPE de llama2.c sobre pares consecutivos, cabeza a cabeza."""
    table = _rope_table(head_size, position)
    out = array("f", v)
    pairs_per_head = head_size // 2
    for pair in range(len(out) // 2):
        cos_value, sin_value = table[pair % pairs_per_head]
        v0 = out[2 * pair]
        v1 = out[2 * pair + 1]
        out[2 * pair] = v0 * cos_value - v1 * sin_value
        out[2 * pair + 1] = v0 * sin_value + v1 * cos_value
    return out


def argmax(a: Sequence[float]) -> int:
    """Primer índice del máximo; con NaN manda el orden de comparación de Python."""
    return max(range(len(a)), key=a.__getitem__)


def sample(probabilities: Sequence[float], coin: float) -> int:
    """Primer índice ``i`` con Σ_{≤i} p > ``coin`` (regla de llama2.c); si no hay, el último.

    Recorrido lineal con parada: vale para cualquier contenido (también negativos
    o NaN) y no materializa la suma acumulada.
    """
    total = 0.0
    for index, probability in enumerate(probabilities):
        total += probability
        if coin < total:
            return index
    return len(probabilities) - 1


def quantize(a: Sequence[float]) -> tuple[bytes, float]:
    """Q8 simétrico por vector: bytes sin signo con desplazamiento 128 y escala float32."""
    if not all(map(math.isfinite, a)):
        raise ValueError("Vector no cuantizable: contiene NaN o infinito")
    peak = max(map(abs, a))
    scale = array("f", [peak / 127.0])[0]
    if not scale:
        return bytes([Q8_OFFSET]) * len(a), 0.0
    levels = map(round, map(truediv, a, repeat(scale)))
    return bytes(map(add, map(max, map(min, levels, repeat(127)), repeat(-127)), repeat(Q8_OFFSET))), scale


def qmatvec(
    weights: memoryview,
    scales: Sequence[float],
    rows: int,
    cols: int,
    x_bytes: memoryview,
    x_scale: float,
) -> array:
    """out[r] = (Σ_j qW[r,j]·qx[j]) · sW[r] · sx con enteros exactos.

    Con q = u − 128: Σ qW·qx = Σ uW·qx − 128·Σ qx. Iterar bytes sin signo evita
    crear enteros negativos (CPython cachea 0..256), el doble de rápido.
    """
    signed = array("b", map(add, x_bytes, repeat(-Q8_OFFSET)))
    correction = Q8_OFFSET * sum(signed)
    xs = signed.tolist() if cols <= LIST_LIMIT else signed
    return array(
        "f",
        [
            (_sumprod(weights[r * cols:(r + 1) * cols], xs) - correction) * scales[r] * x_scale
            for r in range(rows)
        ],
    )


def qrow(row_bytes: memoryview, scale: float) -> array:
    return array("f", map(mul, map(add, row_bytes, repeat(-Q8_OFFSET)), repeat(scale)))


__all__ = [
    "MAX_ROM_WORDS",
    "MAX_TNU_LENGTH",
    "MAX_TNU_WORK",
    "Q8_OFFSET",
    "ROM_BASE",
    "TNU_GAS_DIVISOR",
    "TNU_ROW_UNITS",
    "TensorROM",
    "TramoyaNeuralUnit",
    "argmax",
    "fdot",
    "gas_extra",
    "mattv",
    "matvec",
    "qmatvec",
    "qrow",
    "quantize",
    "rmsnorm",
    "rope",
    "sample",
    "silu",
    "softmax",
    "vadd",
    "vexp",
    "vmul",
    "vscale",
]
