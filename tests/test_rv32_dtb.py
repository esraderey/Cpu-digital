"""Anclas del DTB y del arranque con DTB (H3.5; spec ``.forja/h3/spec_dtb_arranque.md``): un lector de FDT propio, escrito
aquí desde la Devicetree Specification v0.4 §5 sin usar el generador, lee el blob y lo compara con el árbol de la spec."""

from __future__ import annotations

import hashlib
import struct
import unittest

from cpu_digital.rv32_dtb import DEFAULT_BOOTARGS, boot, machine_dtb
from cpu_digital.rv32_hart import Stop
from cpu_digital.rv32_isa import Kind, encode
from cpu_digital.rv32_platform import Machine

GOLDEN_SHA256 = "5132872781bd9959416051e8b41decfa690964bf641608a48d3ea998090a8ac6"   # DTB por defecto, 64 MiB (1 427 bytes)


def read_fdt(blob: bytes) -> tuple[dict[str, int], dict]:
    """Lector de FDT desde §5: devuelve la cabecera y el árbol como {"props": {nombre: bytes}, "order": [...],
    "children": {nombre: subárbol}, "child_order": [...]}; lanza AssertionError ante cualquier violación del formato."""
    assert len(blob) >= 40, "cabecera corta"
    fields = ("magic", "totalsize", "off_dt_struct", "off_dt_strings", "off_mem_rsvmap", "version", "last_comp_version",
              "boot_cpuid_phys", "size_dt_strings", "size_dt_struct")
    header = dict(zip(fields, struct.unpack(">10I", blob[:40])))
    assert header["magic"] == 0xD00DFEED, "magic"
    assert header["totalsize"] == len(blob), "totalsize"
    assert header["off_mem_rsvmap"] % 8 == 0 and header["off_dt_struct"] % 4 == 0, "alineación de los bloques"
    assert header["off_mem_rsvmap"] >= 40 and header["off_dt_struct"] >= header["off_mem_rsvmap"] + 16, "orden de los bloques"
    assert header["off_dt_strings"] == header["off_dt_struct"] + header["size_dt_struct"], "estructura y cadenas contiguas"
    assert header["off_dt_strings"] + header["size_dt_strings"] == len(blob), "las cadenas cierran el blob"
    offset = header["off_mem_rsvmap"]
    while True:                                                  # reservas: pares de 64 bits hasta (0, 0)
        address, size = struct.unpack(">QQ", blob[offset:offset + 16])
        offset += 16
        if address == 0 and size == 0:
            break
    strings = blob[header["off_dt_strings"]:header["off_dt_strings"] + header["size_dt_strings"]]

    def string_at(position: int) -> str:
        end = strings.index(b"\0", position)
        return strings[position:end].decode("ascii")

    position = header["off_dt_struct"]
    end_of_struct = header["off_dt_struct"] + header["size_dt_struct"]

    def token() -> int:
        nonlocal position
        assert position % 4 == 0 and position + 4 <= end_of_struct, "token fuera de la estructura"
        value = struct.unpack(">I", blob[position:position + 4])[0]
        position += 4
        return value

    def node() -> tuple[str, dict]:
        nonlocal position
        stop = blob.index(b"\0", position)
        name = blob[position:stop].decode("ascii")
        position = (stop + 1 + 3) & ~3
        tree = {"props": {}, "order": [], "children": {}, "child_order": []}
        seen_child = False
        while True:
            kind = token()
            if kind == 0x3:
                assert not seen_child, f"propiedad tras un hijo en {name!r}"
                length, name_offset = token(), token()
                prop = string_at(name_offset)
                assert prop not in tree["props"], f"propiedad repetida {prop}"
                tree["props"][prop] = blob[position:position + length]
                tree["order"].append(prop)
                position = (position + length + 3) & ~3
            elif kind == 0x1:
                seen_child = True
                child, subtree = node()
                assert child not in tree["children"], f"hijo repetido {child}"
                tree["children"][child] = subtree
                tree["child_order"].append(child)
            elif kind == 0x2:
                return name, tree
            elif kind == 0x4:
                continue
            else:
                raise AssertionError(f"token {kind:#x} inesperado")

    assert token() == 0x1, "la estructura empieza por FDT_BEGIN_NODE"
    root_name, root = node()
    assert root_name == "", "la raíz no tiene nombre"
    assert token() == 0x9 and position == end_of_struct, "FDT_END cierra la estructura"
    names = strings.split(b"\0")[:-1]
    assert strings.endswith(b"\0") and len(names) == len(set(names)), "cadenas terminadas en NUL y sin repetir"
    return header, root


def u32s(value: bytes) -> list[int]:
    assert len(value) % 4 == 0
    return list(struct.unpack(f">{len(value) // 4}I", value))


def strs(value: bytes) -> list[str]:
    assert value.endswith(b"\0")
    return value[:-1].decode("ascii").split("\0")


class DeviceTree(unittest.TestCase):
    def machine(self, ram: int = 1 << 20) -> Machine:
        machine = Machine(ram_bytes=ram)
        self.addCleanup(machine.close)
        return machine

    def test_the_default_tree_is_exactly_the_one_of_the_spec(self) -> None:
        header, root = read_fdt(machine_dtb(self.machine(64 << 20)))
        self.assertEqual((header["version"], header["last_comp_version"], header["boot_cpuid_phys"]), (17, 16, 0))
        self.assertEqual(root["order"], ["#address-cells", "#size-cells", "compatible", "model"])
        self.assertEqual((u32s(root["props"]["#address-cells"]), u32s(root["props"]["#size-cells"])), ([2], [2]))
        self.assertEqual(strs(root["props"]["compatible"]), ["riscv-minimal-nommu"])
        self.assertEqual(root["child_order"], ["chosen", "memory@80000000", "cpus", "soc"])
        self.assertEqual(strs(root["children"]["chosen"]["props"]["bootargs"]), [DEFAULT_BOOTARGS])
        memory = root["children"]["memory@80000000"]["props"]
        self.assertEqual((strs(memory["device_type"]), u32s(memory["reg"])), (["memory"], [0, 0x80000000, 0, 64 << 20]))
        cpus = root["children"]["cpus"]
        self.assertEqual([u32s(cpus["props"][key]) for key in ("#address-cells", "#size-cells", "timebase-frequency")],
                         [[1], [0], [1_000_000]])
        cpu = cpus["children"]["cpu@0"]
        self.assertEqual({key: (strs(value) if key not in ("reg", "phandle") else u32s(value)) for key, value in cpu["props"].items()},
                         {"device_type": ["cpu"], "reg": [0], "status": ["okay"], "compatible": ["riscv"], "riscv,isa": ["rv32ima"],
                          "mmu-type": ["riscv,none"], "phandle": [1]})
        intc = cpu["children"]["interrupt-controller"]["props"]
        self.assertEqual((u32s(intc["#interrupt-cells"]), intc["interrupt-controller"], strs(intc["compatible"]), u32s(intc["phandle"])),
                         ([1], b"", ["riscv,cpu-intc"], [2]))
        soc = root["children"]["soc"]
        self.assertEqual((u32s(soc["props"]["#address-cells"]), u32s(soc["props"]["#size-cells"]), strs(soc["props"]["compatible"]),
                          soc["props"]["ranges"]), ([2], [2], ["simple-bus"], b""))
        self.assertEqual(soc["child_order"], ["uart@10000000", "syscon@11100000", "poweroff", "reboot", "clint@11000000"])
        devices = soc["children"]
        self.assertEqual((strs(devices["uart@10000000"]["props"]["compatible"]), u32s(devices["uart@10000000"]["props"]["reg"])),
                         (["ns16850"], [0, 0x10000000, 0, 0x100]))
        self.assertEqual(u32s(devices["syscon@11100000"]["props"]["reg"]), [0, 0x11100000, 0, 0x1000])
        syscon_phandle = u32s(devices["syscon@11100000"]["props"]["phandle"])
        for name, value in (("poweroff", 0x5555), ("reboot", 0x7777)):
            props = devices[name]["props"]
            self.assertEqual((strs(props["compatible"]), u32s(props["regmap"]), u32s(props["offset"]), u32s(props["value"])),
                             ([f"syscon-{name}"], syscon_phandle, [0], [value]))
        clint = devices["clint@11000000"]["props"]
        self.assertEqual((strs(clint["compatible"]), u32s(clint["reg"]), u32s(clint["interrupts-extended"])),
                         (["sifive,clint0", "riscv,clint0"], [0, 0x11000000, 0, 0x10000], [2, 3, 2, 7]))

    def test_ram_and_bootargs_follow_the_machine_and_the_bytes_are_deterministic(self) -> None:
        small, other = self.machine(1 << 20), Machine(ram_bytes=3 << 20, base=0x80400000)
        self.addCleanup(other.close)
        _, root = read_fdt(machine_dtb(other, "console=ttyS0"))
        self.assertEqual(u32s(root["children"]["memory@80400000"]["props"]["reg"]), [0, 0x80400000, 0, 3 << 20])
        self.assertEqual(strs(root["children"]["chosen"]["props"]["bootargs"]), ["console=ttyS0"])
        self.assertEqual(machine_dtb(small), machine_dtb(self.machine(1 << 20)))
        self.assertEqual(hashlib.sha256(machine_dtb(self.machine(64 << 20))).hexdigest(), GOLDEN_SHA256)

    def test_boot_places_image_and_dtb_and_sets_a0_a1(self) -> None:
        machine = self.machine(1 << 20)
        image = bytes(range(256)) * 4
        machine.hart.regs[10], machine.uart.output[:] = 7, b"x"
        address = boot(machine, image)
        blob = machine_dtb(machine)
        self.assertEqual(address % 8, 0)
        self.assertLessEqual(address + len(blob), machine.memory.base + machine.memory.size)
        self.assertGreater(address + len(blob) + 8, machine.memory.base + machine.memory.size)   # lo más alto posible
        self.assertEqual(machine.memory.read_bytes(address, len(blob)), blob)
        self.assertEqual(machine.memory.read_bytes(machine.memory.base, len(image)), image)
        hart = machine.hart
        self.assertEqual((hart.regs[10], hart.regs[11], hart.pc, hart.privilege, bytes(machine.uart.output)),
                         (0, address, machine.memory.base, 3, b""))
        # La imagen puede leer el DTB por a1: un programa que carga la magic y apaga.
        program = [encode(Kind.LW, 5, 11, imm=0), encode(Kind.EBREAK)]
        boot(machine, b"".join(word.to_bytes(4, "little") for word in program))
        self.assertEqual(machine.run(10), Stop("ebreak", 3, 0))
        self.assertEqual(machine.hart.regs[5], int.from_bytes(struct.pack(">I", 0xD00DFEED), "little"))

    def test_an_image_that_overlaps_the_dtb_is_rejected_without_touching_the_machine(self) -> None:
        machine = self.machine(1 << 16)
        machine.hart.regs[10] = 7
        before = machine.snapshot()
        with self.assertRaises(ValueError):
            boot(machine, bytes((1 << 16) - 64))
        self.assertEqual(machine.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
