"""Anclas de ``rv32_platform``: UART, SYSCON, CLINT, bus (ventanas, anchos, fallos) y ``Machine``."""

from __future__ import annotations

import unittest
from array import array

from cpu_digital.rv32_hart import Stop
from cpu_digital.rv32_isa import (
    CAUSE_BREAKPOINT,
    CAUSE_ILLEGAL_INSTRUCTION,
    CAUSE_LOAD_ACCESS_FAULT,
    CAUSE_STORE_ACCESS_FAULT,
    CSR_TIME,
    Kind,
    encode,
)
from cpu_digital.rv32_mem import AccessFault
from cpu_digital.rv32_platform import (
    CLINT_BASE,
    CLINT_MTIME,
    CLINT_MTIMECMP,
    SYSCON_BASE,
    UART_BASE,
    Bus,
    Clint,
    Machine,
    Uart,
)

BASE = 0x80000000
UART, SYSCON, MTIME, MTIMECMP = 10, 12, 11, 13   # registros base que usan los programas de prueba
EBREAK = encode(Kind.EBREAK)


def assemble(words: list[int]) -> bytes:
    return b"".join(word.to_bytes(4, "little") for word in words)


class Recorder:
    """Dispositivo falso: devuelve más de 32 bits en toda carga y anota lo que recibe en las escrituras."""

    def __init__(self) -> None:
        self.stores: list[tuple[int, int, int]] = []

    def load(self, offset: int, width: int) -> int:
        return 0x1_0000_00FF

    def store(self, offset: int, width: int, value: int) -> None:
        self.stores.append((offset, width, value))


class MachineTests(unittest.TestCase):
    def make(self, words: list[int], regs: dict[int, int] | None = None, time_divisor: int = 1) -> Machine:
        machine = Machine(ram_bytes=1 << 16, time_divisor=time_divisor)
        self.addCleanup(machine.close)
        machine.load_flat(assemble(words))
        machine.hart.regs[UART] = UART_BASE
        machine.hart.regs[SYSCON] = SYSCON_BASE
        machine.hart.regs[MTIME] = CLINT_BASE + CLINT_MTIME
        machine.hart.regs[MTIMECMP] = CLINT_BASE + CLINT_MTIMECMP
        for index, value in (regs or {}).items():
            machine.hart.regs[index] = value
        return machine

    def test_msip_register_keeps_only_bit_zero_under_partial_accesses(self) -> None:
        # H3.0: registro msip del CLINT en +0, 32 bits de los que solo el bit 0 tiene estado (WARL).
        from cpu_digital import rv32_platform
        msip_offset = rv32_platform.CLINT_MSIP
        machine = self.make([EBREAK])
        clint, bus, base = machine.clint, machine.bus, CLINT_BASE + msip_offset
        self.assertEqual(clint.msip, 0)
        bus.store(base, 4, 0xFFFFFFFF)
        self.assertEqual((clint.msip, bus.load(base, 4), bus.load(base, 2), bus.load(base, 1)), (1, 1, 1, 1))
        for offset in (1, 2, 3):                               # bytes 1–3: leen 0 y escribirlos no toca el bit 0
            self.assertEqual(bus.load(base + offset, 1), 0)
            bus.store(base + offset, 1, 0xFF)
            self.assertEqual(clint.msip, 1)
        bus.store(base + 2, 2, 0)
        bus.store(base + 3, 4, 0)                              # sobresale del registro: solo su byte 3, sin efecto
        self.assertEqual(clint.msip, 1)
        bus.store(base, 2, 0x0100)                             # bit 0 a cero en una escritura de media palabra
        self.assertEqual(clint.msip, 0)
        bus.store(base, 1, 0x03)
        self.assertEqual((clint.msip, bus.load(base, 4)), (1, 1))
        self.assertEqual((clint.mtimecmp, bus.load(CLINT_BASE + CLINT_MTIMECMP, 4)), (0, 0))   # mtimecmp intacto
        # Desde el invitado: sw, sb y lw sobre el registro.
        words = [encode(Kind.SW, 0, 14, 0, imm=0), encode(Kind.LW, 1, 14, imm=0), encode(Kind.ADDI, 2, 0, imm=5),
                 encode(Kind.SB, 0, 14, 2, imm=0), encode(Kind.LW, 3, 14, imm=0), encode(Kind.LBU, 4, 14, imm=1), EBREAK]
        machine = self.make(words, regs={14: base})
        machine.clint.msip = 1
        self.assertEqual(machine.run(20).reason, "ebreak")
        self.assertEqual((machine.hart.regs[1], machine.hart.regs[3], machine.hart.regs[4], machine.clint.msip), (0, 1, 0, 1))
        machine.load_flat(assemble([EBREAK]))
        self.assertEqual(machine.clint.msip, 0)                # load_flat deja la máquina limpia

    def test_uart_data_and_line_status(self) -> None:
        words = [encode(Kind.LBU, 1, UART, imm=5), encode(Kind.LBU, 2, UART, imm=0), encode(Kind.LBU, 3, UART, imm=5),
                 encode(Kind.LB, 4, UART, imm=0), encode(Kind.LBU, 5, UART, imm=0), encode(Kind.LW, 6, UART, imm=4),
                 encode(Kind.LB, 7, UART, imm=5), encode(Kind.SB, 0, UART, 8, imm=0), encode(Kind.SH, 0, UART, 9, imm=0),
                 encode(Kind.SW, 0, UART, 8, imm=4), EBREAK]
        machine = self.make(words, regs={8: 0x1F4, 9: 0x4142})
        machine.uart.feed("A")
        machine.uart.feed(b"\xc8")
        stop = machine.run(100)
        self.assertEqual(stop, Stop("ebreak", CAUSE_BREAKPOINT, 0))
        regs = machine.hart.regs
        self.assertEqual(regs[1], 0x61)                 # THRE, TEMT y DR: hay entrada
        self.assertEqual(regs[2], 0x41)                 # «A»
        self.assertEqual(regs[3], 0x61)                 # queda un byte
        self.assertEqual(regs[4], 0xFFFFFFC8)           # lb extiende el signo del byte del dispositivo
        self.assertEqual(regs[5], 0)                    # cola vacía
        self.assertEqual(regs[6], 0)                    # lw en +4: nada
        self.assertEqual(regs[7], 0x60)                 # sin entrada pendiente
        self.assertEqual(machine.uart.output, bytes((0xF4, 0x42)))   # sb y sh guardan el byte bajo; sw en +4, nada
        self.assertEqual(machine.uart_text, "\xf4B")
        self.assertEqual(machine.hart.instret, 10)

    def test_syscon_stops_after_confirming_the_store(self) -> None:
        words = [encode(Kind.ADDI, 1, 0, imm=1), encode(Kind.SW, 0, SYSCON, 2, imm=0), encode(Kind.ADDI, 1, 0, imm=2), EBREAK]
        for value, expected, instret in ((0x5555, Stop("apagado"), 2), (0x7777, Stop("reinicio"), 2),
                                         (0x12345555, Stop("ebreak", CAUSE_BREAKPOINT, 0), 3),
                                         (0, Stop("ebreak", CAUSE_BREAKPOINT, 0), 3)):
            machine = self.make(words, regs={2: value})
            stop = machine.run(10)
            self.assertEqual(stop, expected, hex(value))
            self.assertEqual((machine.hart.instret, machine.hart.gas, machine.hart.pc),
                             (instret, 10 - instret, BASE + 4 * instret), hex(value))
            self.assertEqual(machine.hart.regs[1], 1 if instret == 2 else 2)
        # El valor llega recortado al ancho: un sb con 0x5555 escribe 0x55 (no apaga) y un sh escribe 0x5555 (apaga).
        machine = self.make([encode(Kind.SB, 0, SYSCON, 2, imm=0), encode(Kind.SH, 0, SYSCON, 2, imm=0), EBREAK],
                            regs={2: 0x5555})
        self.assertEqual(machine.run(10), Stop("apagado"))
        self.assertEqual(machine.hart.instret, 2)
        # Leer SYSCON da 0; un sw en +4 cae fuera de la ventana de 4 bytes: fallo con la dirección.
        machine = self.make([encode(Kind.LW, 1, SYSCON, imm=0), encode(Kind.SW, 0, SYSCON, 2, imm=4), EBREAK], regs={1: 9, 2: 0x5555})
        self.assertEqual(machine.run(10), Stop("fallo", CAUSE_STORE_ACCESS_FAULT, SYSCON_BASE + 4))
        self.assertEqual(machine.hart.regs[1], 0)

    def test_clint_mtime_follows_instret_and_mtimecmp_is_64_bit(self) -> None:
        words = [encode(Kind.LH, 1, MTIME, imm=0), encode(Kind.LB, 2, MTIME, imm=0), encode(Kind.LBU, 3, MTIME, imm=0),
                 encode(Kind.LHU, 4, MTIME, imm=0), encode(Kind.LW, 5, MTIME, imm=0), encode(Kind.LW, 6, MTIME, imm=4),
                 encode(Kind.SW, 0, MTIMECMP, 7, imm=0), encode(Kind.SW, 0, MTIMECMP, 8, imm=4),
                 encode(Kind.LW, 9, MTIMECMP, imm=0), encode(Kind.LW, 14, MTIMECMP, imm=4), EBREAK]
        machine = self.make(words, regs={7: 0xDEADBEEF, 8: 0x12345678})
        machine.hart.instret = 0x8000
        self.assertEqual(machine.run(100).reason, "ebreak")
        regs = machine.hart.regs
        self.assertEqual(regs[1], 0xFFFF8000)      # lh de mtime = 0x8000: con signo
        self.assertEqual(regs[2], 0x01)            # lb de 0x8001
        self.assertEqual(regs[3], 0x02)            # lbu de 0x8002
        self.assertEqual(regs[4], 0x8003)          # lhu de 0x8003
        self.assertEqual(regs[5], 0x8004)          # lw de 0x8004
        self.assertEqual(regs[6], 0)               # mitad alta de mtime
        self.assertEqual(machine.clint.mtimecmp, 0x12345678DEADBEEF)
        self.assertEqual((regs[9], regs[14]), (0xDEADBEEF, 0x12345678))
        machine = self.make([encode(Kind.LB, 1, MTIME, imm=0), EBREAK])
        machine.hart.instret = 0x80
        machine.run(10)
        self.assertEqual(machine.hart.regs[1], 0xFFFFFF80)
        # Mitad alta y bytes interiores de mtime con instret ≥ 2³²; sub-palabras de mtimecmp.
        words = [encode(Kind.LW, 1, MTIME, imm=4), encode(Kind.LBU, 2, MTIME, imm=1), encode(Kind.LHU, 3, MTIME, imm=2),
                 encode(Kind.LHU, 4, MTIME, imm=6), encode(Kind.SB, 0, MTIMECMP, 7, imm=1), encode(Kind.SH, 0, MTIMECMP, 8, imm=6),
                 encode(Kind.LW, 5, MTIMECMP, imm=0), encode(Kind.LW, 6, MTIMECMP, imm=4), EBREAK]
        machine = self.make(words, regs={7: 0xAB, 8: 0xCDEF})
        machine.hart.instret = (0x1234 << 32) | 0x11223344
        self.assertEqual(machine.run(20).reason, "ebreak")
        regs = machine.hart.regs
        self.assertEqual(regs[1], 0x1234)                       # mitad alta tras la primera instrucción
        self.assertEqual(regs[2], 0x33)                         # byte 1 de 0x11223345
        self.assertEqual(regs[3], 0x1122)                       # bytes 2-3 de 0x11223346
        self.assertEqual(regs[4], 0x0000)                       # bytes 6-7 de la mitad alta 0x1234
        self.assertEqual(machine.clint.mtimecmp, 0xCDEF_0000_0000_AB00)
        self.assertEqual((regs[5], regs[6]), (0xAB00, 0xCDEF0000))

    def test_rdtime_and_mtime_share_the_divisor(self) -> None:
        words = [encode(Kind.CSRRS, 1, 0, imm=CSR_TIME), encode(Kind.LW, 2, MTIME, imm=0), EBREAK]
        machine = self.make(words, time_divisor=4)
        machine.hart.instret = 40
        machine.run(10)
        self.assertEqual((machine.hart.regs[1], machine.hart.regs[2]), (10, 10))   # 40 // 4 y 41 // 4
        self.assertEqual(machine.clint.time_divisor, machine.hart.time_divisor)
        machine.hart.time_divisor = 8                                  # un solo reloj: el CLINT lo sigue
        self.assertEqual((machine.clint.time_divisor, machine.clint.mtime), (8, 42 // 8))
        for bad in (0, -1, 2.5, True, "4"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                Machine(ram_bytes=1 << 16, time_divisor=bad)

    def test_unmapped_addresses_fault_with_the_address(self) -> None:
        for words, expected in (([encode(Kind.LW, 1, 14, imm=0)], Stop("fallo", CAUSE_LOAD_ACCESS_FAULT, 0x20000000)),
                                ([encode(Kind.SB, 0, 14, 1, imm=4)], Stop("fallo", CAUSE_STORE_ACCESS_FAULT, 0x20000004)),
                                ([encode(Kind.LBU, 1, UART, imm=0x100)], Stop("fallo", CAUSE_LOAD_ACCESS_FAULT, UART_BASE + 0x100)),
                                ([encode(Kind.LW, 1, 14, imm=-4)], Stop("fallo", CAUSE_LOAD_ACCESS_FAULT, 0x1FFFFFFC)),
                                # Un acceso que sobresale de la ventana por el final no se parte: falla con la dirección.
                                ([encode(Kind.LW, 1, UART, imm=0xFE)], Stop("fallo", CAUSE_LOAD_ACCESS_FAULT, UART_BASE + 0xFE)),
                                ([encode(Kind.SH, 0, UART, 1, imm=0xFF)], Stop("fallo", CAUSE_STORE_ACCESS_FAULT, UART_BASE + 0xFF))):
            machine = self.make(words + [EBREAK], regs={14: 0x20000000, 1: 5})
            self.assertEqual(machine.run(10), expected)
            self.assertEqual((machine.hart.instret, machine.hart.gas, machine.hart.pc), (0, 10, BASE))
            self.assertEqual(machine.hart.regs[1], 5)

    def test_load_flat_leaves_a_clean_machine(self) -> None:
        first = [encode(Kind.ADDI, 1, 0, imm=7), encode(Kind.SW, 0, 3, 1, imm=0), encode(Kind.SB, 0, UART, 1, imm=0),
                 encode(Kind.SW, 0, MTIMECMP, 1, imm=0), EBREAK]
        machine = self.make(first, regs={3: BASE + 0x2000})
        self.assertEqual(machine.run(10).reason, "ebreak")
        self.assertEqual((machine.memory.load32(BASE + 0x2000), machine.uart.output, machine.clint.mtimecmp), (7, b"\x07", 7))
        machine.uart.feed("zz")
        machine.load_flat(assemble([encode(Kind.ADDI, 2, 0, imm=3), EBREAK]))
        self.assertEqual((machine.hart.pc, machine.hart.instret, machine.hart.regs[1]), (BASE, 0, 0))
        self.assertFalse(any(machine.memory.code))                       # mapa de código vacío, con las cachés
        self.assertEqual(machine.memory.written_pages(), [0])            # solo la página de la imagen nueva
        self.assertEqual(machine.memory.load32(BASE + 0x2000), 0)
        self.assertEqual(machine.memory.load32(BASE + 8), 0)       # el resto de la imagen anterior se borró
        self.assertEqual((machine.uart.output, list(machine.uart.input), machine.clint.mtimecmp), (bytearray(), [], 0))
        pristine = Machine(ram_bytes=1 << 16)   # H2.1: el estado tras load_flat es el de una máquina nueva (divisor: test_rv32_snapshot)
        self.addCleanup(pristine.close)
        pristine.load_flat(assemble([encode(Kind.ADDI, 2, 0, imm=3), EBREAK]))
        self.assertEqual(machine.state_hash(), pristine.state_hash())
        self.assertEqual(machine.run(10).reason, "ebreak")
        self.assertEqual((machine.hart.regs[2], machine.hart.instret), (3, 1))
        machine.load_flat(b"")
        self.assertEqual(machine.run(10), Stop("fallo", CAUSE_ILLEGAL_INSTRUCTION, 0))
        with self.assertRaises(AccessFault):
            machine.load_flat(bytes((1 << 16) + 4))
        machine.load_flat(assemble([EBREAK]), BASE + 0x100)
        self.assertEqual((machine.hart.pc, machine.run(10).reason), (BASE + 0x100, "ebreak"))
        # Tamaño exacto de la RAM: cabe; un byte más, o un array de 1 100 palabras (4 400 bytes) en 4 KiB: no cabe,
        # y entonces no se borra nada (ni RAM, ni UART, ni hart).
        machine.load_flat(bytes(1 << 16))
        with self.assertRaises(AccessFault):
            machine.load_flat(bytes((1 << 16) + 1))
        small = Machine(ram_bytes=1 << 12)
        self.addCleanup(small.close)
        small.load_flat(assemble([encode(Kind.ADDI, 1, 0, imm=3), encode(Kind.SB, 0, UART, 1, imm=0), EBREAK]))
        small.hart.regs[UART] = UART_BASE
        self.assertEqual(small.run(10).reason, "ebreak")
        before = (bytes(small.memory.lanes), bytes(small.uart.output), small.hart.pc, small.hart.instret, small.hart.regs[1])
        with self.assertRaises(AccessFault):
            small.load_flat(memoryview(array("I", [1] * 1100)))
        self.assertEqual((bytes(small.memory.lanes), bytes(small.uart.output), small.hart.pc, small.hart.instret, small.hart.regs[1]), before)
        small.load_flat(array("I", [encode(Kind.ADDI, 2, 0, imm=9), EBREAK]))   # 8 bytes: cabe y corre
        self.assertEqual((small.run(10).reason, small.hart.regs[2]), ("ebreak", 9))


class BusTests(unittest.TestCase):
    def test_attach_validates_windows(self) -> None:
        bus = Bus()
        bus.attach(0x1000, 0x100, Uart())
        for base, size in ((0x10FF, 0x10), (0x0FFF, 0x2), (0x1000, 0x100), (0x1080, 0x10), (0x2000, 0), (0x2000, -1),
                           (-1, 0x10), (0xFFFFFFFF, 2), (1 << 32, 4)):
            with self.assertRaises(ValueError, msg=f"{base:#x} {size:#x}"):
                bus.attach(base, size, Uart())
        bus.attach(0x1100, 0x100, Uart())          # contigua, sin solape
        bus.attach(0xFFFFFFF0, 0x10, Uart())       # hasta el final del espacio
        with self.assertRaises(AccessFault):
            bus.load(0x1200, 4)
        with self.assertRaises(AccessFault):
            bus.store(0x0FFF, 1, 0)

    def test_bus_truncates_to_the_access_width(self) -> None:
        bus = Bus()
        device = Recorder()
        bus.attach(0x30000000, 0x100, device)
        self.assertEqual([bus.load(0x30000000, width) for width in (1, 2, 4)], [0xFF, 0xFF, 0xFF])
        bus.store(0x30000004, 1, 0x1FF)
        bus.store(0x30000008, 2, 0x12345)
        bus.store(0x3000000C, 4, 0x1_8000_0001)
        self.assertEqual(device.stores, [(4, 1, 0xFF), (8, 2, 0x2345), (12, 4, 0x80000001)])
        # Por el hart: lb extiende el signo del byte ya recortado y lw no deja más de 32 bits.
        machine = Machine(ram_bytes=1 << 16)
        self.addCleanup(machine.close)
        machine.bus.attach(0x30000000, 0x100, device)
        machine.load_flat(assemble([encode(Kind.LB, 1, 5, imm=0), encode(Kind.LH, 2, 5, imm=0), encode(Kind.LW, 3, 5, imm=0),
                                    encode(Kind.SW, 0, 6, 1, imm=0), EBREAK]))
        machine.hart.regs[5] = 0x30000000
        machine.hart.regs[6] = BASE + 0x1000
        self.assertEqual(machine.run(10).reason, "ebreak")
        self.assertEqual(machine.hart.regs[1:4], [0xFFFFFFFF, 0xFF, 0xFF])
        self.assertEqual(machine.memory.load32(BASE + 0x1000), 0xFFFFFFFF)

    def test_devices_alone(self) -> None:
        uart = Uart()
        self.assertEqual(uart.load(5, 1), 0x60)
        uart.feed(b"\x01\x02")
        self.assertEqual((uart.load(5, 1), uart.load(0, 1), uart.load(0, 4), uart.load(5, 1)), (0x61, 1, 2, 0x60))
        uart.store(0, 1, 0x141)
        uart.store(3, 1, 0x42)
        self.assertEqual(uart.text, "A")
        owner = Machine(ram_bytes=1 << 12, time_divisor=2)
        self.addCleanup(owner.close)
        clint = Clint(owner.hart)
        self.assertEqual((clint.mtime, clint.time_divisor), (0, 2))
        clint.hart.instret = 11
        self.assertEqual((clint.mtime, clint.load(CLINT_MTIME, 4), clint.load(CLINT_MTIME + 4, 4), clint.load(0, 4)), (5, 5, 0, 0))
        clint.store(CLINT_MTIMECMP + 4, 4, 1)
        clint.store(CLINT_MTIMECMP, 4, 0xFFFFFFFF)
        self.assertEqual((clint.mtimecmp, clint.load(CLINT_MTIMECMP, 4), clint.load(CLINT_MTIMECMP + 4, 4)), (0x1FFFFFFFF, 0xFFFFFFFF, 1))
        self.assertEqual((clint.load(CLINT_MTIMECMP + 3, 1), clint.load(CLINT_MTIMECMP + 4, 2), clint.load(CLINT_MTIMECMP + 8, 4)), (0xFF, 1, 0))
        clint.store(CLINT_MTIMECMP + 7, 1, 0x80)
        self.assertEqual(clint.mtimecmp, 0x80000001FFFFFFFF)


if __name__ == "__main__":
    unittest.main()
