"""Árbol de dispositivos (DTB) de la plataforma RV32 y arranque con DTB (RFC-EXP-00018, P1.11; H3.5).

``machine_dtb(machine)`` genera el blob FDT (Devicetree Specification v0.4 §5) que describe la plataforma de ``Machine``
con la topología del DTB de mini-rv32ima [R13]: RAM con la base y el tamaño de la memoria, un hart RV32IMA sin MMU
(``riscv,isa`` = "rv32ima", ``mmu-type`` = "riscv,none") con su controlador de interrupciones local, ``timebase-frequency``
= 1 MHz (un tic de ``mtime`` es 1 µs del invitado: con divisor 1, una instrucción por µs, la base de E1), el CLINT con MSI y
MTI del hart, el UART 8250 y el SYSCON con apagado (0x5555) y reinicio (0x7777); las direcciones salen de
``rv32_platform``. ``boot(machine, image, bootargs)`` aplica la convención de arranque de mini-rv32ima: imagen en la base
de la RAM, DTB (con ``bootargs`` en ``/chosen``) al final, ``a0`` = ``mhartid`` = 0, ``a1`` = dirección del DTB, ``pc`` en la
base y modo M. El blob es determinista: la misma configuración da los mismos bytes. Spec: ``.forja/h3/spec_dtb_arranque.md``.

Formato del blob: cabecera de 40 bytes (enteros de 32 bits big-endian), bloque de reservas alineado a 8 (solo el
terminador), bloque de estructura alineado a 4 (``FDT_BEGIN_NODE`` + nombre, ``FDT_PROP`` + longitud + desplazamiento del
nombre + valor, ``FDT_END_NODE``, ``FDT_END``; las propiedades antes que los hijos) y bloque de cadenas con los nombres de
propiedad sin repetir.
"""

from __future__ import annotations

import struct
from typing import NamedTuple

from .rv32_platform import CLINT_BASE, CLINT_SIZE, SYSCON_BASE, SYSCON_POWEROFF, SYSCON_REBOOT, UART_BASE, UART_SIZE, Machine

FDT_MAGIC = 0xD00DFEED
FDT_VERSION = 17
FDT_LAST_COMPATIBLE_VERSION = 16
FDT_BEGIN_NODE = 0x1
FDT_END_NODE = 0x2
FDT_PROP = 0x3
FDT_END = 0x9
HEADER_SIZE = 40
TIMEBASE_FREQUENCY = 1_000_000   # Hz de mtime: un tic = 1 µs del invitado
UART_CLOCK = 0x1000000           # la del DTB de mini-rv32ima: el 8250 de Linux exige la propiedad; el valor no importa
#                                  (el UART no emula el divisor de baudios)
SYSCON_WINDOW = 0x1000           # lo que el DTB declara (como mini-rv32ima); el SYSCON solo responde al desplazamiento 0
DEFAULT_BOOTARGS = "earlycon=uart8250,mmio,0x10000000,1000000 console=ttyS0"
CPU_PHANDLE = 1
INTC_PHANDLE = 2
SYSCON_PHANDLE = 4


class Node(NamedTuple):
    """Nodo del árbol: nombre (vacío en la raíz), propiedades en orden (nombre, valor en bytes) e hijos en orden."""

    name: str
    properties: list[tuple[str, bytes]]
    children: list["Node"]


def cells(*values: int) -> bytes:
    """Celdas de 32 bits big-endian (cada valor en [0, 2³²))."""
    return struct.pack(f">{len(values)}I", *values)


def text(*values: str) -> bytes:
    """Una cadena o una lista de cadenas, cada una terminada en NUL (ASCII)."""
    return b"".join(value.encode("ascii") + b"\0" for value in values)


def _pad4(data: bytes) -> bytes:
    return data + bytes(-len(data) % 4)


def build_fdt(root: Node) -> bytes:
    """Serializa ``root`` como blob FDT versión 17 (§5): cabecera, reservas (solo el terminador), estructura y cadenas."""
    names: dict[str, int] = {}
    strings = bytearray()
    structure = bytearray()

    def name_offset(name: str) -> int:
        if name not in names:
            names[name] = len(strings)
            strings.extend(text(name))
        return names[name]

    def emit(node: Node) -> None:
        if "/" in node.name or "\0" in node.name:
            raise ValueError(f"nombre de nodo inválido: {node.name!r}")
        structure.extend(cells(FDT_BEGIN_NODE) + _pad4(text(node.name)))
        for name, value in node.properties:
            structure.extend(cells(FDT_PROP, len(value), name_offset(name)) + _pad4(value))
        for child in node.children:
            emit(child)
        structure.extend(cells(FDT_END_NODE))

    emit(root)
    structure.extend(cells(FDT_END))
    reservations = bytes(16)                                  # (0, 0): el terminador
    off_rsvmap = HEADER_SIZE                                  # 40 ya está alineado a 8
    off_struct = off_rsvmap + len(reservations)               # múltiplo de 8, luego de 4
    off_strings = off_struct + len(structure)
    total = off_strings + len(strings)
    header = cells(FDT_MAGIC, total, off_struct, off_strings, off_rsvmap, FDT_VERSION, FDT_LAST_COMPATIBLE_VERSION, 0,
                   len(strings), len(structure))
    return header + reservations + bytes(structure) + bytes(strings)


def platform_tree(ram_base: int, ram_size: int, bootargs: str = DEFAULT_BOOTARGS, isa: str = "rv32ima") -> Node:
    """El árbol de la plataforma (``.forja/h3/spec_dtb_arranque.md``) para una RAM de ``ram_size`` bytes en ``ram_base``."""
    if not 0 <= ram_base < 1 << 32 or not 0 < ram_size <= (1 << 32) - ram_base:
        raise ValueError("La RAM debe caber en el espacio de 32 bits")
    intc = Node("interrupt-controller", [("#interrupt-cells", cells(1)), ("interrupt-controller", b""),
                                         ("compatible", text("riscv,cpu-intc")), ("phandle", cells(INTC_PHANDLE))], [])
    cpu = Node("cpu@0", [("device_type", text("cpu")), ("reg", cells(0)), ("status", text("okay")), ("compatible", text("riscv")),
                         ("riscv,isa", text(isa)), ("mmu-type", text("riscv,none")), ("phandle", cells(CPU_PHANDLE))], [intc])
    cpus = Node("cpus", [("#address-cells", cells(1)), ("#size-cells", cells(0)),
                         ("timebase-frequency", cells(TIMEBASE_FREQUENCY))], [cpu])
    uart = Node(f"uart@{UART_BASE:x}", [("compatible", text("ns16850")), ("reg", cells(0, UART_BASE, 0, UART_SIZE)),
                                        ("clock-frequency", cells(UART_CLOCK))], [])
    syscon = Node(f"syscon@{SYSCON_BASE:x}", [("compatible", text("syscon")), ("reg", cells(0, SYSCON_BASE, 0, SYSCON_WINDOW)),
                                              ("phandle", cells(SYSCON_PHANDLE))], [])
    poweroff = Node("poweroff", [("compatible", text("syscon-poweroff")), ("regmap", cells(SYSCON_PHANDLE)), ("offset", cells(0)),
                                 ("value", cells(SYSCON_POWEROFF))], [])
    reboot = Node("reboot", [("compatible", text("syscon-reboot")), ("regmap", cells(SYSCON_PHANDLE)), ("offset", cells(0)),
                             ("value", cells(SYSCON_REBOOT))], [])
    clint = Node(f"clint@{CLINT_BASE:x}", [("compatible", text("sifive,clint0", "riscv,clint0")),
                                           ("reg", cells(0, CLINT_BASE, 0, CLINT_SIZE)),
                                           ("interrupts-extended", cells(INTC_PHANDLE, 3, INTC_PHANDLE, 7))], [])
    soc = Node("soc", [("#address-cells", cells(2)), ("#size-cells", cells(2)), ("compatible", text("simple-bus")), ("ranges", b"")],
               [uart, syscon, poweroff, reboot, clint])
    memory = Node(f"memory@{ram_base:x}", [("device_type", text("memory")),
                                            ("reg", cells(0, ram_base, ram_size >> 32, ram_size & 0xFFFFFFFF))], [])
    chosen = Node("chosen", [("bootargs", text(bootargs))], [])
    return Node("", [("#address-cells", cells(2)), ("#size-cells", cells(2)), ("compatible", text("riscv-minimal-nommu")),
                     ("model", text("cpu-digital,rv32"))], [chosen, memory, cpus, soc])


def machine_dtb(machine: Machine, bootargs: str = DEFAULT_BOOTARGS) -> bytes:
    """El DTB de la plataforma de ``machine`` (RAM con su base y tamaño)."""
    return build_fdt(platform_tree(machine.memory.base, machine.memory.size, bootargs))


def boot(machine: Machine, image: bytes | bytearray | memoryview, bootargs: str = DEFAULT_BOOTARGS) -> int:
    """Convención de arranque de mini-rv32ima: ``load_flat(image)`` (reinicio completo e imagen en la base de la RAM), el
    DTB en la dirección más alta de la RAM alineada a 8 que lo deja entero, ``a0`` = 0 (``mhartid``), ``a1`` = esa
    dirección; ``pc`` en la base y modo M (los deja ``load_flat``). ``ValueError`` sin tocar la máquina si la imagen y el
    DTB se solapan. Devuelve la dirección del DTB."""
    memory = machine.memory
    dtb = machine_dtb(machine, bootargs)
    address = (memory.base + memory.size - len(dtb)) & ~7
    if memoryview(image).nbytes > address - memory.base:
        raise ValueError("La imagen y el DTB no caben juntos en la RAM")
    machine.load_flat(image)
    memory.load_bytes(address, dtb)
    machine.hart.regs[10] = 0
    machine.hart.regs[11] = address
    return address


__all__ = ["DEFAULT_BOOTARGS", "FDT_MAGIC", "FDT_VERSION", "Node", "TIMEBASE_FREQUENCY", "boot", "build_fdt", "cells",
           "machine_dtb", "platform_tree", "text"]
