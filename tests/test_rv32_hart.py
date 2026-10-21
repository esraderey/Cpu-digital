"""Anclas del hart RV32 (H1): diferencial con un intérprete de referencia, bordes, fallos, CSR y CoreMark."""

from __future__ import annotations

import random
import unittest
from pathlib import Path

from cpu_digital import rv32_alu as alu
from cpu_digital import rv32_isa
from cpu_digital.rv32_decode import decode
from cpu_digital.rv32_hart import GAS_PER_INSTRUCTION, Rv32Hart, Stop
from cpu_digital.rv32_isa import (
    CAUSE_BREAKPOINT,
    CAUSE_ECALL_FROM_M,
    CAUSE_ECALL_FROM_U,
    CAUSE_ILLEGAL_INSTRUCTION,
    CAUSE_INSTRUCTION_ACCESS_FAULT,
    CAUSE_INSTRUCTION_MISALIGNED,
    CAUSE_LOAD_ACCESS_FAULT,
    CAUSE_STORE_ACCESS_FAULT,
    CSR_CYCLE,
    CSR_INSTRET,
    CSR_TIME,
    Kind,
    encode,
)
from cpu_digital.rv32_mem import Rv32Memory
from cpu_digital.rv32_platform import Bus, Clint, Machine

ROOT = Path(__file__).resolve().parents[1]
COREMARK = ROOT / "benchmarks" / "rv32_arranque" / "bin" / "coremark_rv32i_zicsr_it10.bin"
# Recuento de E1 con mini-rv32ima instrumentado (8 505 936) más la escritura en SYSCON que apaga la
# máquina: mini-rv32ima sale de su bucle en esa escritura sin contarla; el hart la confirma y la cuenta.
COREMARK_INSTRUCTIONS = 8_505_936 + 1
BASE = 0x80000000
RAM = 1 << 16          # 64 KiB: código en la primera página, datos en la segunda y tercera
CODE = BASE
DATA = BASE + 0x1000
M = 0xFFFFFFFF
M64 = (1 << 64) - 1
CLINT_BASE = 0x11000000   # mapa de la plataforma (spec de H2 y H3.4): msip +0, mtimecmp +0x4000, mtime +0xBFF8


class Reference:
    """Intérprete de referencia: decodificación y aritmética compartidas; control, memoria, CSR, privilegio y traps
    propios (H3.1), escritos desde ``.forja/h3/spec_zicsr_traps.md`` sin importar la legalización ni los manejadores del
    hart. ``count`` son las instrucciones retiradas (``instret``) y ``charged`` el gas (las retiradas más las entradas en
    trap, que cobran una instrucción)."""

    MSTATUS_KEEP = 0x8 | 0x80 | 0x1800 | 0x20000 | 0x200000      # MIE, MPIE, MPP, MPRV, TW
    MISA = 0x40000000 | 1 << 0 | 1 << 8 | 1 << 12 | 1 << 20      # RV32 con A (H3.3), I, M (H3.2) y U

    def __init__(self, image: bytes, size: int = RAM, clint: bool = False, divisor: int = 1) -> None:
        self.mem = bytearray(size)
        self.mem[:len(image)] = image
        self.regs = [0] * 32
        self.pc = BASE
        self.count = 0
        self.charged = 0
        self.priv = 3
        self.csr = {0x300: 0, 0x304: 0, 0x305: 0, 0x340: 0, 0x341: 0, 0x342: 0, 0x343: 0}
        self.reservation: int | None = None              # H3.3: palabra reservada por LR.W
        self.stop: Stop | None = None
        # H3.4 (spec .forja/h3/spec_interrupciones_wfi.md): CLINT en CLINT_BASE con msip (+0), mtimecmp (+0x4000) y mtime
        # (+0xBFF8) = (instret mod 2⁶⁴) // divisor; sin CLINT, mip = 0 y WFI no tiene evento posible.
        self.clint = clint
        self.divisor = divisor
        self.mtimecmp = 0
        self.msip = 0
        self.taken: list[tuple[int, int]] = []           # bitácora de las pruebas: (código, privilegio) de cada interrupción
        self.sleeps = 0                                  # WFI que adelantaron instret más allá de sí mismas

    def mtime(self) -> int:
        return (self.count & M64) // self.divisor

    def mip(self) -> int:
        if not self.clint:
            return 0
        return (0x8 if self.msip else 0) | (0x80 if self.mtime() >= self.mtimecmp else 0)

    def device(self, address: int, width: int) -> int | None:
        """Desplazamiento del acceso en la ventana del CLINT (entero dentro de ella), o None."""
        offset = address - CLINT_BASE
        return offset if self.clint and 0 <= offset and offset + width <= 0x10000 else None

    def device_load(self, offset: int, width: int) -> int:
        # Cada registro responde a los accesos que empiezan dentro de él; el resto de la ventana lee 0.
        for start, size, value in ((0, 4, self.msip), (0x4000, 8, self.mtimecmp), (0xBFF8, 8, self.mtime())):
            if start <= offset < start + size:
                return (value >> 8 * (offset - start)) & ((1 << 8 * width) - 1)
        return 0

    def device_store(self, offset: int, width: int, value: int) -> None:
        mask = (1 << 8 * width) - 1
        if 0 <= offset < 4:                              # msip: solo el bit 0 tiene estado
            shift = 8 * offset
            self.msip = ((self.msip & ~(mask << shift)) | (value & mask) << shift) & 1
        elif 0x4000 <= offset < 0x4008:
            shift = 8 * (offset - 0x4000)
            self.mtimecmp = ((self.mtimecmp & ~(mask << shift)) | (value & mask) << shift) & M64

    # ── CSR según el mapa de la spec ──

    def csr_read(self, number: int) -> int | None:
        """Valor del CSR, o None si no existe en el mapa."""
        if number in self.csr:
            return self.csr[number]
        if number in (0xC00, 0xC02, 0xB00, 0xB02):
            return self.count & M
        if number in (0xC80, 0xC82, 0xB80, 0xB82):
            return (self.count >> 32) & M
        if number == 0xC01:
            return self.mtime() & M
        if number == 0xC81:
            return (self.mtime() >> 32) & M
        if number == 0x301:
            return self.MISA
        if number == 0x306:
            return 0b111
        if number == 0x344:
            return self.mip()
        if number in (0x310, 0xF11, 0xF12, 0xF13, 0xF14, 0xF15):
            return 0
        if 0xB03 <= number <= 0xB1F or 0xB83 <= number <= 0xB9F or 0x323 <= number <= 0x33F:
            return 0
        if 0xC03 <= number <= 0xC1F or 0xC83 <= number <= 0xC9F:
            return 0
        return None

    def csr_write(self, number: int, value: int) -> None:
        if number == 0x300:
            value &= self.MSTATUS_KEEP
            if (value >> 11) & 3 in (1, 2):
                value &= ~0x1800
        elif number == 0x304:
            value &= 0x888
        elif number == 0x305:
            value &= ~2 & M
        elif number == 0x341:
            value &= ~3 & M
        if number in self.csr:
            self.csr[number] = value & M                 # el resto del mapa ignora la escritura

    def trap(self, reason: str, cause: int, tval: int) -> bool:
        """Entra en el manejador (y sigue) o, si su destino no está en la RAM, se detiene sin escribir nada. En modo
        vectorizado las interrupciones (bit 31 de la causa) van a base + 4 × código; las excepciones, a la base."""
        target = self.csr[0x305] & ~3 & M
        if self.csr[0x305] & 1 and cause >> 31:
            target = (target + 4 * (cause & 0x7FFFFFFF)) & M
        if not 0 <= target - BASE <= len(self.mem) - 4:
            self.stop = Stop(reason, cause, tval)
            return False
        status = self.csr[0x300]
        self.csr[0x341] = self.pc & ~3 & M
        self.csr[0x342] = cause
        self.csr[0x343] = tval & M
        self.csr[0x300] = (status & ~(0x8 | 0x80 | 0x1800)) | (0x80 if status & 0x8 else 0) | (self.priv << 11)
        self.priv = 3
        self.reservation = None                          # el trap pierde la reserva (política R-9)
        self.pc = target
        self.charged += 1
        return True

    def atomic(self, k: Kind, rd: int, rs1: int, rs2: int) -> int | None:
        """Extensión A desde la spec de H3.3: alineación y RAM antes de nada; LR reserva; SC escribe solo con la reserva
        de esa palabra (y siempre la pierde); AMO lee, escribe op(viejo, rs2) y devuelve el viejo."""
        address = self.regs[rs1]
        is_lr = k == Kind.LR_W
        if address & 3:
            raise LookupError(4 if is_lr else 6, address)
        if not 0 <= address - BASE <= len(self.mem) - 4:
            raise LookupError(CAUSE_LOAD_ACCESS_FAULT if is_lr else CAUSE_STORE_ACCESS_FAULT, address)
        offset = address - BASE
        old = int.from_bytes(self.mem[offset:offset + 4], "little")
        if is_lr:
            self.reservation = address
            return old
        if k == Kind.SC_W:
            success = self.reservation == address
            self.reservation = None
            if success:
                self.mem[offset:offset + 4] = self.regs[rs2].to_bytes(4, "little")
            return 0 if success else 1
        value = self.regs[rs2]
        signed_old, signed_value = alu.to_signed(old), alu.to_signed(value)
        new = {Kind.AMOSWAP_W: value, Kind.AMOADD_W: (old + value) & M, Kind.AMOXOR_W: old ^ value,
               Kind.AMOAND_W: old & value, Kind.AMOOR_W: old | value,
               Kind.AMOMIN_W: old if signed_old < signed_value else value,
               Kind.AMOMAX_W: old if signed_old > signed_value else value,
               Kind.AMOMINU_W: min(old, value), Kind.AMOMAXU_W: max(old, value)}[k]
        self.store(address, 4, new)
        return old

    def load(self, address: int, width: int, signed: bool) -> int:
        offset = address - BASE
        if not 0 <= offset <= len(self.mem) - width:
            device = self.device(address, width)
            if device is None:
                raise LookupError(CAUSE_LOAD_ACCESS_FAULT, address)
            value = self.device_load(device, width)
            if signed and value >> (8 * width - 1):
                value -= 1 << (8 * width)
            return value & M
        return int.from_bytes(self.mem[offset:offset + width], "little", signed=signed) & M

    def store(self, address: int, width: int, value: int) -> None:
        offset = address - BASE
        if not 0 <= offset <= len(self.mem) - width:
            device = self.device(address, width)
            if device is None:
                raise LookupError(CAUSE_STORE_ACCESS_FAULT, address)
            self.device_store(device, width, value)
            return
        if self.reservation is not None and address < self.reservation + 4 and self.reservation < address + width:
            self.reservation = None                      # un store del hart sobre la palabra reservada la pierde
        self.mem[offset:offset + width] = (value & ((1 << (8 * width)) - 1)).to_bytes(width, "little")

    def step(self) -> bool:
        """Una instrucción retirada o una entrada en trap (las dos cobran 1 de gas); False si se detiene."""
        pc = self.pc
        if pc & 3:
            self.stop = Stop("fallo", CAUSE_INSTRUCTION_MISALIGNED, pc)   # solo un pc puesto por el host: sin trap
            return False
        # H3.4: en cada frontera, antes de la instrucción, la interrupción pendiente, habilitada en mie y tomable (U, o M con
        # mstatus.MIE) de mayor prioridad (MEI > MSI > MTI) entra como trap con mepc = pc y mtval = 0.
        pending = self.mip() & self.csr[0x304]
        if pending and (self.priv == 0 or self.csr[0x300] & 0x8):
            code = 11 if pending & 0x800 else 3 if pending & 0x8 else 7
            priv = self.priv
            entered = self.trap("interrupcion", 1 << 31 | code, 0)
            if entered:
                self.taken.append((code, priv))
            return entered
        if not 0 <= pc - BASE <= len(self.mem) - 4:
            return self.trap("fallo", CAUSE_INSTRUCTION_ACCESS_FAULT, pc)
        word = int.from_bytes(self.mem[pc - BASE:pc - BASE + 4], "little")
        kind, rd, rs1, rs2, imm, raw = decode(word)
        k = Kind(kind)
        regs = self.regs
        next_pc = pc + 4
        result: int | None = None
        try:
            if k == Kind.LUI:
                result = imm & M
            elif k == Kind.AUIPC:
                result = (pc + imm) & M
            elif k == Kind.JAL:
                next_pc = (pc + imm) & M
                result = (pc + 4) & M
            elif k == Kind.JALR:
                next_pc = (regs[rs1] + imm) & M & ~1
                result = (pc + 4) & M
            elif k in alu.BRANCHES:
                if alu.BRANCHES[k](regs[rs1], regs[rs2]):
                    next_pc = (pc + imm) & M
            elif k in (Kind.LB, Kind.LBU, Kind.LH, Kind.LHU, Kind.LW):
                width = {Kind.LB: 1, Kind.LBU: 1, Kind.LH: 2, Kind.LHU: 2, Kind.LW: 4}[k]
                result = self.load((regs[rs1] + imm) & M, width, k in (Kind.LB, Kind.LH))
            elif k in (Kind.SB, Kind.SH, Kind.SW):
                self.store((regs[rs1] + imm) & M, {Kind.SB: 1, Kind.SH: 2, Kind.SW: 4}[k], regs[rs2])
            elif k in (Kind.ADDI, Kind.SLTI, Kind.SLTIU, Kind.XORI, Kind.ORI, Kind.ANDI, Kind.SLLI, Kind.SRLI, Kind.SRAI):
                result = alu.FUNCTIONS[k](regs[rs1], imm & M)
            elif k in alu.FUNCTIONS:                     # RV32I y, desde H3.2, M (la ALU de H1 pasó oráculo)
                result = alu.FUNCTIONS[k](regs[rs1], regs[rs2])
            elif k in (Kind.FENCE, Kind.FENCE_I):
                pass
            elif k == Kind.ECALL:
                return self.trap("ecall", CAUSE_ECALL_FROM_U if self.priv == 0 else CAUSE_ECALL_FROM_M, 0)
            elif k == Kind.EBREAK:
                return self.trap("ebreak", CAUSE_BREAKPOINT, 0)
            elif k == Kind.MRET:
                if self.priv != 3:
                    raise LookupError(CAUSE_ILLEGAL_INSTRUCTION, raw)
                status = self.csr[0x300]
                back = (status >> 11) & 3
                status = (status & ~(0x8 | 0x1800)) | 0x80 | (0x8 if status & 0x80 else 0)
                if back != 3:
                    status &= ~0x20000
                self.csr[0x300] = status
                self.priv = back
                self.reservation = None                  # MRET pierde la reserva
                next_pc = self.csr[0x341]
            elif k == Kind.WFI:
                if self.priv == 0 and self.csr[0x300] & 0x200000:
                    raise LookupError(CAUSE_ILLEGAL_INSTRUCTION, raw)       # TW = 1 en U
                enabled = self.csr[0x304]
                if not self.mip() & enabled:             # con una pendiente habilitada en mie, un NOP
                    threshold = self.mtimecmp * self.divisor
                    if not (self.clint and enabled & 0x80 and threshold <= M64):
                        self.stop = Stop("wfi")          # sin evento posible: nada se confirma
                        return False
                    # MTIP llega cuando instret mod 2⁶⁴ alcanza mtimecmp × divisor (aún no: no está pendiente).
                    wake = self.count + threshold - (self.count & M64)
                    self.sleeps += wake > self.count + 1
                    self.count = max(self.count + 1, wake) - 1   # el +1 común de abajo retira la propia WFI
            elif k in (Kind.LR_W, Kind.SC_W, Kind.AMOSWAP_W, Kind.AMOADD_W, Kind.AMOXOR_W, Kind.AMOAND_W, Kind.AMOOR_W,
                       Kind.AMOMIN_W, Kind.AMOMAX_W, Kind.AMOMINU_W, Kind.AMOMAXU_W):
                result = self.atomic(k, rd, rs1, rs2)
            elif k in (Kind.CSRRW, Kind.CSRRS, Kind.CSRRC, Kind.CSRRWI, Kind.CSRRSI, Kind.CSRRCI):
                immediate = k in (Kind.CSRRWI, Kind.CSRRSI, Kind.CSRRCI)
                operand = rs1 if immediate else regs[rs1]
                swap = k in (Kind.CSRRW, Kind.CSRRWI)
                writes = swap or rs1 != 0
                old = self.csr_read(imm)
                user_hpm = 0xC03 <= imm <= 0xC1F or 0xC83 <= imm <= 0xC9F
                if old is None or (imm >> 8) & 3 > self.priv or (writes and imm >> 10 == 3) or (self.priv == 0 and user_hpm):
                    raise LookupError(CAUSE_ILLEGAL_INSTRUCTION, raw)
                if writes:
                    if swap:
                        self.csr_write(imm, operand)
                    elif k in (Kind.CSRRS, Kind.CSRRSI):
                        self.csr_write(imm, old | operand)
                    else:
                        self.csr_write(imm, old & ~operand & M)
                result = old
            else:
                raise LookupError(CAUSE_ILLEGAL_INSTRUCTION, raw)
            if next_pc & 3:
                raise LookupError(CAUSE_INSTRUCTION_MISALIGNED, next_pc)
        except LookupError as fault:
            return self.trap("fallo", fault.args[0], fault.args[1])
        if result is not None and rd:
            regs[rd] = result
        self.pc = next_pc
        self.count += 1
        self.charged += 1
        return True

    def run(self, steps: int) -> Stop:
        for _ in range(steps):
            if not self.step():
                return self.stop
        return Stop("gas")


def assemble(words: list[int]) -> bytes:
    return b"".join(word.to_bytes(4, "little") for word in words)


def random_program(rng: random.Random, length: int) -> list[int]:
    """Programa aleatorio sin saltos hacia atrás: aritmética, memoria sobre la zona de datos, saltos adelante."""
    words = [encode(Kind.LUI, 10, imm=alu.to_signed(DATA & 0xFFFFF000)), encode(Kind.ADDI, 10, 10, imm=0),
             encode(Kind.LUI, 11, imm=alu.to_signed((DATA + 0x800) & 0xFFFFF000)), encode(Kind.ADDI, 11, 11, imm=0x7F0)]
    alu_kinds = [Kind.ADD, Kind.SUB, Kind.SLL, Kind.SLT, Kind.SLTU, Kind.XOR, Kind.SRL, Kind.SRA, Kind.OR, Kind.AND]
    imm_kinds = [Kind.ADDI, Kind.SLTI, Kind.SLTIU, Kind.XORI, Kind.ORI, Kind.ANDI]
    shift_kinds = [Kind.SLLI, Kind.SRLI, Kind.SRAI]
    loads = [Kind.LB, Kind.LBU, Kind.LH, Kind.LHU, Kind.LW]
    stores = [Kind.SB, Kind.SH, Kind.SW]
    while len(words) < length:
        roll = rng.random()
        rd = rng.choice((0, rng.randrange(1, 32)))
        if rd in (10, 11) and rng.random() < 0.9:
            rd = 12  # casi nunca se pisan los registros base: así la mayoría de programas llega al ebreak
        rs1 = rng.randrange(32)
        rs2 = rng.randrange(32)
        if roll < 0.30:
            words.append(encode(rng.choice(alu_kinds), rd, rs1, rs2))
        elif roll < 0.45:
            words.append(encode(rng.choice(imm_kinds), rd, rs1, imm=rng.randrange(-2048, 2048)))
        elif roll < 0.50:
            words.append(encode(rng.choice(shift_kinds), rd, rs1, imm=rng.randrange(32)))
        elif roll < 0.55:
            words.append(encode(rng.choice((Kind.LUI, Kind.AUIPC)), rd, imm=rng.randrange(-(1 << 19), 1 << 19) * 4096))
        elif roll < 0.68:
            base = rs1 if rng.random() < 0.015 else rng.choice((10, 11))  # a veces una base cualquiera: fallos
            words.append(encode(rng.choice(loads), rd, base, imm=rng.randrange(0, 0x7F0)))
        elif roll < 0.80:
            base = rs1 if rng.random() < 0.015 else rng.choice((10, 11))
            words.append(encode(rng.choice(stores), 0, base, rs2, imm=rng.randrange(0, 0x7F0)))
        elif roll < 0.90:
            remaining = length - len(words) - 1
            skip = rng.randrange(1, min(remaining, 8) + 1) if remaining > 1 else 1
            kind = rng.choice((Kind.BEQ, Kind.BNE, Kind.BLT, Kind.BGE, Kind.BLTU, Kind.BGEU))
            words.append(encode(kind, 0, rs1, rs2, imm=4 * skip))
        elif roll < 0.95:
            remaining = length - len(words) - 1
            skip = rng.randrange(1, min(remaining, 8) + 1) if remaining > 1 else 1
            words.append(encode(Kind.JAL, rd, imm=4 * skip))
        elif roll < 0.97:
            words.append(encode(Kind.CSRRS, rd, 0, imm=rng.choice((CSR_CYCLE, CSR_INSTRET, CSR_TIME))))
        elif roll < 0.995 and length - len(words) > 3:
            # auipc rT, 0; jalr rd, rT, 8 + 4·skip: salto indirecto hacia delante a una instrucción válida.
            temp = rng.randrange(12, 32)
            skip = rng.randrange(0, min(length - len(words) - 3, 6) + 1)
            words.append(encode(Kind.AUIPC, temp, imm=0))
            words.append(encode(Kind.JALR, rd, temp, imm=8 + 4 * skip))
        else:
            words.append(encode(Kind.JALR, rd, rs1, imm=rng.randrange(-16, 16)))  # destino cualquiera: fallo probable
    words.append(encode(Kind.EBREAK))
    return words


HANDLER = CODE + 0x800   # manejador de los programas con traps: salta la instrucción que atrapó (mepc + 4) y vuelve
CSR_POOL = (0x300, 0x300, 0x301, 0x304, 0x306, 0x310, 0x340, 0x340, 0x341, 0x342, 0x343, 0x344, 0xB00, 0xB02, 0xB80, 0xB82,
            0xB03, 0xB9F, 0x323, 0xC00, 0xC01, 0xC02, 0xC80, 0xC03, 0xC9F, 0xF11, 0xF14, 0xF15, 0x7C0, 0x302, 0x180, 0x320, 0x305)
CSR_KINDS = (Kind.CSRRW, Kind.CSRRS, Kind.CSRRC, Kind.CSRRWI, Kind.CSRRSI, Kind.CSRRCI)
M_KINDS = (Kind.MUL, Kind.MULH, Kind.MULHSU, Kind.MULHU, Kind.DIV, Kind.DIVU, Kind.REM, Kind.REMU)
EB = encode(Kind.EBREAK)
SRET = 0x10200073   # ilegal en la plataforma (sin modo S); WFI hacía este papel hasta H3.4, que la implementa
A_KINDS = (Kind.LR_W, Kind.SC_W, Kind.AMOSWAP_W, Kind.AMOADD_W, Kind.AMOXOR_W, Kind.AMOAND_W, Kind.AMOOR_W, Kind.AMOMIN_W,
           Kind.AMOMAX_W, Kind.AMOMINU_W, Kind.AMOMAXU_W)


def load_immediate(register: int, value: int) -> list[int]:
    """``li register, value`` en dos instrucciones (LUI + ADDI)."""
    high = (value + 0x800) & 0xFFFFF000
    low = alu.to_signed((value - high) & M)      # en [−2048, 2047]
    return [encode(Kind.LUI, register, imm=alu.to_signed(high)), encode(Kind.ADDI, register, register, imm=low)]


def handler_words() -> list[int]:
    return [encode(Kind.CSRRS, 31, 0, imm=0x341), encode(Kind.ADDI, 31, 31, imm=4), encode(Kind.CSRRW, 0, 31, imm=0x341),
            encode(Kind.MRET)]


def with_handler(words: list[int]) -> bytes:
    """Imagen con ``words`` desde CODE y el manejador en HANDLER (en medio, ceros: instrucciones ilegales)."""
    image = bytearray(assemble(words))
    if len(image) > HANDLER - CODE:
        raise ValueError("el programa pisa el manejador")
    image.extend(bytes(HANDLER - CODE - len(image)))
    return bytes(image + assemble(handler_words()))


def random_trap_program(rng: random.Random, length: int) -> list[int]:
    """Programa aleatorio de ``random_program`` con manejador instalado y, en lugar de un 15 % de sus instrucciones, CSR
    del mapa (legales, de solo lectura, sin privilegio o inexistentes) con las seis formas de Zicsr, ``ECALL``, ``EBREAK``,
    ilegales, ``MRET`` y el paso a modo U (``mepc`` ← la instrucción siguiente, MPP ← U, ``MRET``). x28–x31 son del
    andamiaje (el cuerpo puede pisarlos: el resultado sigue siendo determinista). Termina poniendo ``mtvec`` = 0 (modo
    compatible) y con ``EBREAK``."""
    body = random_program(rng, length)[:-1]
    index = 4                                    # tras el prólogo de random_program (bases x10 y x11)
    while index < len(body):
        if rng.random() < 0.15:
            roll = rng.random()
            if roll < 0.55:
                kind = rng.choice(CSR_KINDS)
                rd = rng.choice((0, rng.randrange(1, 28)))
                body[index] = encode(kind, rd, rng.randrange(32), imm=rng.choice(CSR_POOL))
            elif roll < 0.65:
                body[index] = encode(Kind.ECALL)
            elif roll < 0.72:
                body[index] = encode(Kind.EBREAK)
            elif roll < 0.80:
                body[index] = rng.choice((0x00000000, 0xFFFFFFFF, SRET))   # SRET: sin modo S, ilegal
            elif roll < 0.86:
                body[index] = encode(Kind.MRET)
            elif roll < 0.89:                    # H3.2: extensión M (sin excepciones posibles)
                body[index] = encode(rng.choice(M_KINDS), rng.choice((0, rng.randrange(1, 28))), rng.randrange(32), rng.randrange(32))
            elif roll < 0.92:                    # H3.3: extensión A sobre las bases de datos (x10, x11) o una cualquiera
                base = rng.choice((10, 10, 11, rng.randrange(32)))
                kind = rng.choice(A_KINDS)
                rd = rng.choice((0, rng.randrange(12, 28)))
                body[index] = encode(kind, rd, base, 0 if kind == Kind.LR_W else rng.randrange(32), imm=rng.randrange(4))
            elif roll < 0.95 and index + 4 <= len(body):    # H3.3: LR/SC sobre x10, a veces con un store en medio
                middle = encode(Kind.SW, 0, 10, 13, imm=rng.choice((0, 4))) if rng.random() < 0.4 else encode(Kind.ADDI, 13, 13, imm=1)
                body[index:index + 4] = [encode(Kind.LR_W, 12, 10), middle, encode(Kind.SC_W, 14, 10, 13),
                                         encode(Kind.ADD, 15, 15, 14)]
                index += 3
            elif index + 5 <= len(body):
                body[index:index + 5] = [encode(Kind.AUIPC, 28, imm=0), encode(Kind.ADDI, 28, 28, imm=20),
                                         encode(Kind.CSRRW, 0, 28, imm=0x341), encode(Kind.CSRRC, 0, 29, imm=0x300),
                                         encode(Kind.MRET)]
                index += 4
        index += 1
    prologue = load_immediate(30, HANDLER) + [encode(Kind.CSRRW, 0, 30, imm=0x305)] + load_immediate(29, 0x1800)
    return prologue + body + [encode(Kind.CSRRW, 0, 0, imm=0x305), encode(Kind.EBREAK)]


def hart_csrs(hart: Rv32Hart) -> dict[int, int]:
    return {0x300: hart.mstatus, 0x304: hart.mie, 0x305: hart.mtvec, 0x340: hart.mscratch, 0x341: hart.mepc,
            0x342: hart.mcause, 0x343: hart.mtval}


def make_hart(image: bytes, size: int = RAM) -> tuple[Rv32Hart, Rv32Memory]:
    memory = Rv32Memory(size)
    memory.load_bytes(BASE, image)
    hart = Rv32Hart(memory)
    return hart, memory


class DifferentialAgainstReference(unittest.TestCase):
    def check_program(self, words: list[int], rng: random.Random, cuts: bool) -> None:
        image = assemble(words)
        reference = Reference(image)
        reference_stop = reference.run(600)
        hart, memory = make_hart(image)
        self.addCleanup(memory.close)
        issued = 0
        if cuts:
            stop = Stop("gas")
            remaining = 600
            while remaining > 0 and stop.reason == "gas":
                chunk = min(remaining, rng.randrange(1, 25))
                stop = hart.run(chunk)
                issued += chunk
                remaining -= chunk
        else:
            issued = 600
            stop = hart.run(600)
        self.assertEqual(stop, reference_stop)
        self.assertEqual(hart.regs, reference.regs)
        self.assertEqual(hart.pc, reference.pc)
        self.assertEqual(hart.instret, reference.count)
        self.assertEqual(hart.gas, issued - reference.count)   # bitácora independiente: 1 de gas por confirmada
        self.assertEqual(bytes(memory.lanes), bytes(reference.mem))

    def test_random_programs(self) -> None:
        rng = random.Random(2026)
        reasons: dict[str, int] = {}
        for index in range(300):
            words = random_program(rng, rng.randrange(20, 200))
            image = assemble(words)
            expected = Reference(image).run(600)
            reasons[expected.reason] = reasons.get(expected.reason, 0) + 1
            with self.subTest(program=index):
                self.check_program(words, rng, cuts=index % 2 == 1)
        self.assertGreater(reasons.get("ebreak", 0), 100, reasons)
        self.assertGreater(reasons.get("fallo", 0), 20, reasons)


class TrapsAgainstReference(unittest.TestCase):
    """H3.1: CSR, traps síncronos, MRET y modo U frente a la referencia, con el intérprete y con bloques, con y sin
    cortes de gas (los traps cobran 1 de gas y no avanzan instret)."""

    MODES = ((False, 2, False), (False, 2, True), (True, 1, True), (True, 2, False), (True, 1, False))

    def check(self, image: bytes, rng: random.Random, gas: int, accelerate: bool, warmup: int, cuts: bool) -> Reference:
        reference = Reference(image)
        expected_stop = reference.run(gas)
        hart, memory = make_hart(image)
        self.addCleanup(memory.close)
        hart.accelerate, hart.warmup = accelerate, warmup
        consumed = 0
        stop = Stop("gas")
        remaining = gas
        while remaining > 0 and stop.reason == "gas":
            chunk = min(remaining, rng.randrange(1, 40)) if cuts else remaining
            stop = hart.run(chunk)
            consumed += chunk - hart.gas
            remaining -= chunk - hart.gas if stop.reason != "gas" else chunk
        self.assertEqual(stop, expected_stop)
        self.assertEqual(hart.regs, reference.regs)
        self.assertEqual((hart.pc, hart.instret, hart.privilege), (reference.pc, reference.count, reference.priv))
        self.assertEqual(hart_csrs(hart), reference.csr)
        self.assertEqual(hart.reservation, reference.reservation)
        self.assertEqual(consumed, reference.charged)
        self.assertEqual(bytes(memory.lanes), bytes(reference.mem))
        return reference

    def test_random_programs_with_csrs_traps_mret_and_user_mode(self) -> None:
        rng = random.Random(20261020)
        totals = {"traps": 0, "u": 0, "ilegal": 0, "ecall_u": 0, "stops": {}}
        for index in range(160):
            words = random_trap_program(rng, rng.randrange(20, 180))
            image = with_handler(words)
            gas = rng.choice((300, 900))
            for accelerate, warmup, cuts in self.MODES:
                with self.subTest(program=index, accelerate=accelerate, warmup=warmup, cuts=cuts):
                    self.check(image, rng, gas, accelerate, warmup, cuts)
            reference = Reference(image)                       # los totales, de una referencia aparte (no de un subTest)
            reference.run(gas)
            totals["traps"] += reference.charged - reference.count
            totals["u"] += reference.priv == 0
            totals["stops"][reference.stop.reason if reference.stop else "gas"] = totals["stops"].get(
                reference.stop.reason if reference.stop else "gas", 0) + 1
        self.assertGreater(totals["traps"], 2000, totals)       # los programas atrapan de verdad
        self.assertGreater(totals["u"], 15, totals)
        self.assertGreater(totals["stops"].get("gas", 0), 30, totals)
        self.assertGreater(totals["stops"].get("ebreak", 0), 10, totals)

    def test_compiled_hot_loops_that_trap_match_the_reference_in_every_mode(self) -> None:
        # Un bucle caliente (compilado con calentamiento 1 y 2) cuyas cargas se salen de la RAM en las últimas vueltas
        # (fallo de carga que el manejador salta) y otro con un ecall en cada vuelta: el trap lo produce el intérprete
        # tras la salida lenta o el corte del bloque, y todo coincide con la referencia.
        EBREAK = encode(Kind.EBREAK)
        prologue = load_immediate(30, HANDLER) + [encode(Kind.CSRRW, 0, 30, imm=0x305), encode(Kind.ADDI, 20, 0, imm=40)]
        loads = prologue + load_immediate(5, BASE + RAM - 80) + [
            encode(Kind.LW, 6, 5, imm=0), encode(Kind.ADD, 7, 7, 6), encode(Kind.ADDI, 5, 5, imm=4),
            encode(Kind.ADDI, 20, 20, imm=-1), encode(Kind.BNE, 0, 20, 0, imm=-16), encode(Kind.CSRRW, 0, 0, imm=0x305), EBREAK]
        calls = prologue + [encode(Kind.ADDI, 7, 7, imm=3), encode(Kind.ECALL), encode(Kind.XOR, 8, 8, 7),
                            encode(Kind.ADDI, 20, 20, imm=-1), encode(Kind.BNE, 0, 20, 0, imm=-16),
                            encode(Kind.CSRRW, 0, 0, imm=0x305), EBREAK]
        rng = random.Random(5)
        for name, words in (("cargas", loads), ("ecall", calls)):
            image = with_handler(words)
            for accelerate, warmup, cuts in self.MODES + ((True, 1, False),):
                with self.subTest(program=name, accelerate=accelerate, warmup=warmup, cuts=cuts):
                    reference = self.check(image, rng, 2000, accelerate, warmup, cuts)
                    self.assertEqual(reference.stop, Stop("ebreak", CAUSE_BREAKPOINT, 0))
                    self.assertGreater(reference.charged - reference.count, 15)   # vueltas que atrapan


# ── H3.4: interrupciones del CLINT y WFI (spec .forja/h3/spec_interrupciones_wfi.md) ──

VECTORS = CODE + 0x700   # tabla de mtvec vectorizado: base (excepciones), +12 (MSI) y +28 (MTI) saltan al manejador
MTIMECMP_PAGE = 0x11004  # lui de CLINT_BASE + 0x4000 (mtimecmp)
MTIME_PAGE = 0x1100C     # lui de CLINT_BASE + 0xC000; mtime está en −8


def interrupt_handler_words(delta: int) -> list[int]:
    """Manejador en HANDLER: las excepciones saltan la instrucción (mepc + 4); MSI borra msip; MTI reprograma mtimecmp a
    mtime + ``delta`` (palabra baja, la alta a 0) o, con ``delta`` = 0, quita MTIE de mie. x29–x31 son suyos."""
    timer = ([encode(Kind.LUI, 30, imm=MTIME_PAGE << 12), encode(Kind.LW, 29, 30, imm=-8), encode(Kind.ADDI, 29, 29, imm=delta),
              encode(Kind.LUI, 30, imm=MTIMECMP_PAGE << 12), encode(Kind.SW, 0, 30, 29, imm=0), encode(Kind.SW, 0, 30, 0, imm=4)]
             if delta else
             [encode(Kind.ADDI, 30, 0, imm=0x80), encode(Kind.CSRRC, 0, 30, imm=0x304)] + [encode(Kind.ADDI, 0, 0, imm=0)] * 4)
    return ([encode(Kind.CSRRS, 31, 0, imm=0x342), encode(Kind.BLT, 0, 31, 0, imm=20),                  # 0, 4
             encode(Kind.CSRRS, 31, 0, imm=0x341), encode(Kind.ADDI, 31, 31, imm=4), encode(Kind.CSRRW, 0, 31, imm=0x341),
             encode(Kind.MRET),                                                                           # 8–20
             encode(Kind.ANDI, 31, 31, imm=15), encode(Kind.ADDI, 30, 0, imm=3), encode(Kind.BEQ, 0, 31, 30, imm=32)]   # 24–32
            + timer + [encode(Kind.MRET),                                                                 # 36–60
                       encode(Kind.LUI, 30, imm=CLINT_BASE), encode(Kind.SW, 0, 30, 0, imm=0), encode(Kind.MRET)])   # 64–72


def with_interrupt_handler(words: list[int], delta: int) -> bytes:
    """Imagen con ``words`` desde CODE, la tabla vectorizada en VECTORS y el manejador de interrupciones en HANDLER."""
    image = bytearray(assemble(words))
    if len(image) > VECTORS - CODE:
        raise ValueError("el programa pisa la tabla de vectores")
    image.extend(bytes(VECTORS - CODE - len(image)))
    table = [0] * 32
    for slot in (0, 3, 7):
        table[slot] = encode(Kind.JAL, 0, imm=HANDLER - (VECTORS + 4 * slot))
    image.extend(assemble(table))
    image.extend(bytes(HANDLER - CODE - len(image)))
    return bytes(image + assemble(interrupt_handler_words(delta)))


def random_interrupt_program(rng: random.Random, length: int, vectored: bool) -> list[int]:
    """Programa de ``random_program`` (sin su EBREAK) en el que un 22 % de las posiciones programa ``mtimecmp`` (a veces
    inalcanzable), escribe ``msip``, ``mie`` y ``mstatus.MIE``/``TW``, ejecuta ``WFI``, lee ``mip``/``time``/``mtime``,
    pasa a modo U o mete un bucle caliente (para que haya bloques que el plazo corte). x25–x31 son del andamiaje."""
    body = random_program(rng, length)[:-1]
    index = 4
    while index < len(body):
        if rng.random() < 0.22:
            roll = rng.random()
            room = len(body) - index
            if roll < 0.10:
                body[index] = encode(Kind.CSRRSI if rng.random() < 0.7 else Kind.CSRRCI, 0, 8, imm=0x300)   # MIE
            elif roll < 0.18 and room >= 2:
                op = rng.choice((Kind.CSRRW, Kind.CSRRS, Kind.CSRRS, Kind.CSRRC))
                body[index:index + 2] = [encode(Kind.ADDI, 28, 0, imm=rng.choice((0x8, 0x80, 0x88, 0x88, -1, 0))),   # −1: todos (mie se queda con 0x888)
                                         encode(op, 0, 28, imm=0x304)]
                index += 1
            elif roll < 0.30 and room >= 4:
                value = rng.choice((rng.randrange(0, 120), rng.randrange(0, 900), 0))
                high = encode(Kind.SW, 0, 28, 0, imm=4) if rng.random() < 0.9 else encode(Kind.SW, 0, 28, 29, imm=4)
                body[index:index + 4] = [encode(Kind.LUI, 28, imm=MTIMECMP_PAGE << 12), encode(Kind.ADDI, 29, 0, imm=value),
                                         encode(Kind.SW, 0, 28, 29, imm=0), high]
                index += 3
            elif roll < 0.38 and room >= 3:
                store = rng.choice((Kind.SW, Kind.SW, Kind.SB, Kind.SH))
                body[index:index + 3] = [encode(Kind.LUI, 28, imm=CLINT_BASE), encode(Kind.ADDI, 29, 0, imm=rng.choice((0, 1, 1, 3))),
                                         encode(store, 0, 28, 29, imm=rng.choice((0, 0, 0, 1)))]
                index += 2
            elif roll < 0.58 and room >= 6:              # MSI armada: MSIE, MIE y msip = 1
                body[index:index + 6] = [encode(Kind.ADDI, 28, 0, imm=0x8), encode(Kind.CSRRS, 0, 28, imm=0x304),
                                         encode(Kind.CSRRSI, 0, 8, imm=0x300), encode(Kind.LUI, 28, imm=CLINT_BASE),
                                         encode(Kind.ADDI, 29, 0, imm=1), encode(Kind.SW, 0, 28, 29, imm=0)]
                index += 5
            elif roll < 0.61:
                body[index] = encode(Kind.WFI)
            elif roll < 0.68 and room >= 9:              # WFI armada: MTIE y mtimecmp = mtime + d
                body[index:index + 9] = [encode(Kind.ADDI, 28, 0, imm=0x80), encode(Kind.CSRRS, 0, 28, imm=0x304),
                                         encode(Kind.LUI, 28, imm=MTIME_PAGE << 12), encode(Kind.LW, 29, 28, imm=-8),
                                         encode(Kind.ADDI, 29, 29, imm=rng.randrange(1, 60)),
                                         encode(Kind.LUI, 28, imm=MTIMECMP_PAGE << 12), encode(Kind.SW, 0, 28, 29, imm=0),
                                         encode(Kind.SW, 0, 28, 0, imm=4), encode(Kind.WFI)]
                index += 8
            elif roll < 0.72:
                body[index] = encode(Kind.CSRRS, rng.randrange(12, 25), 0, imm=rng.choice((0x344, 0xC01, 0xC81)))
            elif roll < 0.75 and room >= 2:
                body[index:index + 2] = [encode(Kind.LUI, 28, imm=MTIME_PAGE << 12), encode(Kind.LW, rng.randrange(12, 25), 28, imm=-8)]
                index += 1
            elif roll < 0.78 and room >= 2:
                body[index:index + 2] = [encode(Kind.LUI, 28, imm=0x200000), encode(Kind.CSRRS if rng.random() < 0.6 else Kind.CSRRC,
                                                                                     0, 28, imm=0x300)]   # TW
                index += 1
            elif roll < 0.89 and room >= 7:
                body[index:index + 7] = [encode(Kind.AUIPC, 28, imm=0), encode(Kind.ADDI, 28, 28, imm=28),
                                         encode(Kind.CSRRW, 0, 28, imm=0x341), encode(Kind.ADDI, 27, 0, imm=0x180),
                                         encode(Kind.SLLI, 27, 27, imm=4), encode(Kind.CSRRC, 0, 27, imm=0x300),
                                         encode(Kind.MRET)]                                  # a modo U
                index += 6
            elif room >= 4:
                body[index:index + 4] = [encode(Kind.ADDI, 26, 0, imm=rng.randrange(4, 40)), encode(Kind.ADDI, 25, 25, imm=1),
                                         encode(Kind.ADDI, 26, 26, imm=-1), encode(Kind.BNE, 0, 26, 0, imm=-8)]   # bucle caliente
                index += 3
        index += 1
    mtvec = VECTORS | 1 if vectored else HANDLER
    prologue = load_immediate(30, mtvec) + [encode(Kind.CSRRW, 0, 30, imm=0x305)]
    return prologue + body + [encode(Kind.CSRRW, 0, 0, imm=0x304), encode(Kind.CSRRW, 0, 0, imm=0x305), encode(Kind.EBREAK)]


def make_clint_hart(image: bytes, divisor: int = 1) -> tuple[Rv32Hart, Rv32Memory, Clint]:
    """Hart con la RAM de las pruebas y un bus con solo el CLINT (como en Machine, sin UART ni SYSCON)."""
    memory = Rv32Memory(RAM)
    memory.load_bytes(BASE, image)
    bus = Bus()
    hart = Rv32Hart(memory, bus, divisor)
    clint = Clint(hart)
    hart.interrupts = clint
    bus.attach(CLINT_BASE, 0x10000, clint)
    return hart, memory, clint


class InterruptsAgainstReference(unittest.TestCase):
    """H3.4: programas aleatorios con temporizador, msip, mie, mstatus.MIE, WFI, modo U y un manejador que reprograma el
    temporizador, frente a la referencia (que evalúa las interrupciones en **cada** frontera), en seis modos (intérprete y
    bloques, con y sin cortes de gas) y con divisores 1, 3 y 7."""

    MODES = ((False, 2, False), (False, 2, True), (True, 1, False), (True, 1, True), (True, 2, False), (True, 2, True))

    def check(self, image: bytes, rng: random.Random, gas: int, divisor: int, accelerate: bool, warmup: int,
              cuts: bool) -> Reference:
        reference = Reference(image, clint=True, divisor=divisor)
        expected_stop = reference.run(gas)
        hart, memory, clint = make_clint_hart(image, divisor)
        self.addCleanup(memory.close)
        hart.accelerate, hart.warmup = accelerate, warmup
        consumed = 0
        stop = Stop("gas")
        remaining = gas
        while remaining > 0 and stop.reason == "gas":
            chunk = min(remaining, rng.randrange(1, 40)) if cuts else remaining
            stop = hart.run(chunk)
            consumed += chunk - hart.gas
            remaining -= chunk - hart.gas
        self.assertEqual(stop, expected_stop)
        self.assertEqual(hart.regs, reference.regs)
        self.assertEqual((hart.pc, hart.instret, hart.privilege), (reference.pc, reference.count & M64, reference.priv))
        self.assertEqual(hart_csrs(hart), reference.csr)
        self.assertEqual((clint.mtimecmp, clint.msip), (reference.mtimecmp, reference.msip))
        self.assertEqual(hart.reservation, reference.reservation)
        self.assertEqual(consumed, reference.charged)
        self.assertEqual(bytes(memory.lanes), bytes(reference.mem))
        return reference

    def test_random_programs_with_timer_msip_wfi_and_user_mode(self) -> None:
        rng = random.Random(20261021)
        totals = {"msi": 0, "mti": 0, "u": 0, "vectored": 0, "sleeps": 0, "stops": {}}
        for index in range(150):
            vectored = rng.random() < 0.4
            delta = rng.choice((0, 7, 25, 60))
            image = with_interrupt_handler(random_interrupt_program(rng, rng.randrange(20, 150), vectored), delta)
            gas = rng.choice((400, 1500))
            divisor = (1, 3, 7)[index % 3]
            for accelerate, warmup, cuts in self.MODES:
                with self.subTest(program=index, divisor=divisor, accelerate=accelerate, warmup=warmup, cuts=cuts):
                    self.check(image, rng, gas, divisor, accelerate, warmup, cuts)
            census = Reference(image, clint=True, divisor=divisor)   # recuentos de una referencia aparte (no de un subTest)
            census.run(gas)
            totals["msi"] += sum(code == 3 for code, _ in census.taken)
            totals["mti"] += sum(code == 7 for code, _ in census.taken)
            totals["u"] += sum(priv == 0 for _, priv in census.taken)
            totals["sleeps"] += census.sleeps
            totals["vectored"] += vectored and bool(census.taken)
            reason = census.stop.reason if census.stop else "gas"
            totals["stops"][reason] = totals["stops"].get(reason, 0) + 1
        self.assertGreater(totals["msi"], 40, totals)
        self.assertGreater(totals["mti"], 150, totals)
        self.assertGreater(totals["u"], 20, totals)            # interrupciones tomadas en modo U
        self.assertGreater(totals["vectored"], 15, totals)
        self.assertGreater(totals["sleeps"], 40, totals)       # WFI que adelantan el reloj
        for reason in ("gas", "ebreak", "wfi"):
            self.assertGreater(totals["stops"].get(reason, 0), 5, totals)


STOP_HANDLER = (encode(Kind.CSRRW, 0, 0, imm=0x305), encode(Kind.EBREAK))   # pone mtvec = 0 y para con el EBREAK


class InterruptsAndWfi(unittest.TestCase):
    """H3.4: anclas dirigidas de la spec (criterios 2–4): plazo exacto con intérprete y con bloques, quién toma la
    interrupción y cuál, modo vectorizado y compatible, y WFI (NOP, sueño hasta el plazo, parada y TW)."""

    def make(self, words: list[int], handler: tuple[int, ...] = STOP_HANDLER, divisor: int = 1, accelerate: bool = False,
             warmup: int = 2) -> tuple[Rv32Hart, Clint, bytes]:
        image = bytearray(assemble(words))
        image.extend(bytes(HANDLER - CODE - len(image)))
        image.extend(assemble(list(handler)))
        hart, memory, clint = make_clint_hart(bytes(image), divisor)
        self.addCleanup(memory.close)
        hart.accelerate, hart.warmup = accelerate, warmup
        return hart, clint, bytes(image)

    def test_the_timer_enters_at_the_exact_deadline_with_the_interpreter_and_with_blocks(self) -> None:
        # Bucle de 4 instrucciones (un bloque caliente) con mtimecmp = D: la interrupción entra cuando instret = D, en
        # cualquier posición del cuerpo; el manejador lee minstret (= D) y para. mepc, de la referencia.
        handler = (encode(Kind.CSRRS, 22, 0, imm=0xB02),) + STOP_HANDLER
        for divisor in (1, 3):
            for deadline in range(40, 56):              # tras el calentamiento: el bloque ya está compilado (warmup 2)
                words = (load_immediate(30, HANDLER) + [encode(Kind.CSRRW, 0, 30, imm=0x305),
                         encode(Kind.LUI, 28, imm=MTIMECMP_PAGE << 12), encode(Kind.ADDI, 29, 0, imm=deadline),
                         encode(Kind.SW, 0, 28, 29, imm=0), encode(Kind.SW, 0, 28, 0, imm=4), encode(Kind.ADDI, 28, 0, imm=0x80),
                         encode(Kind.CSRRS, 0, 28, imm=0x304), encode(Kind.CSRRSI, 0, 8, imm=0x300), encode(Kind.ADDI, 10, 0, imm=200),
                         encode(Kind.ADDI, 5, 5, imm=1), encode(Kind.ADDI, 6, 6, imm=3), encode(Kind.XOR, 7, 7, 6),
                         encode(Kind.BNE, 0, 5, 10, imm=-12), encode(Kind.EBREAK)])
                # Sin horizonte (horizon=False) un bloque puede rebasar el gas, pero no el plazo: el bucle lo impone.
                for accelerate, warmup, horizon in ((False, 2, True), (True, 1, True), (True, 2, True), (True, 1, False)):
                    with self.subTest(divisor=divisor, deadline=deadline, accelerate=accelerate, warmup=warmup, horizon=horizon):
                        hart, _, image = self.make(words, handler, divisor, accelerate, warmup)
                        hart.horizon = horizon
                        reference = Reference(image, clint=True, divisor=divisor)
                        self.assertEqual(reference.run(5000), Stop("ebreak", CAUSE_BREAKPOINT, 0))
                        self.assertEqual(hart.run(5000), Stop("ebreak", CAUSE_BREAKPOINT, 0))
                        self.assertEqual(hart.regs[22], deadline * divisor)
                        self.assertEqual((hart.mcause, hart.mepc, hart.mtval), (0x80000007, reference.csr[0x341], 0))
                        self.assertEqual(hart.regs, reference.regs)
                        if accelerate:
                            self.assertGreater(hart.block_instructions, 0)   # el bucle corrió en bloques hasta el plazo

    def test_who_takes_which_interrupt(self) -> None:
        # Al empezar run, con el estado puesto por el host: (privilegio, MIE, mie, msip, mtimecmp) → causa tomada o None.
        words = [encode(Kind.ADDI, 1, 1, imm=1)] * 4 + [encode(Kind.EBREAK)]
        cases = [
            ("M con MIE = 0 no la toma", 3, 0, 0x80, 0, 0, None),
            ("M con MIE = 1", 3, 1, 0x80, 0, 0, 7),
            ("U la toma siempre", 0, 0, 0x80, 0, 0, 7),
            ("mie sin el bit", 3, 1, 0x8, 0, 0, None),
            ("MSI antes que MTI", 3, 1, 0x88, 1, 0, 3),
            ("msip sin MSIE", 3, 1, 0x80, 1, 0, 7),
            ("solo MEIE: nada pendiente", 3, 1, 0x800, 1, 0, None),
            ("MTIP aún no", 3, 1, 0x88, 0, 10, None),
        ]
        for name, privilege, mie_bit, enabled, msip, mtimecmp, code in cases:
            with self.subTest(case=name):
                hart, clint, _ = self.make(words)
                hart.mtvec, hart.mie, hart.mstatus, hart.privilege = HANDLER, enabled, mie_bit << 3, privilege
                clint.msip, clint.mtimecmp = msip, mtimecmp
                self.assertEqual(hart.run(100), Stop("ebreak", CAUSE_BREAKPOINT, 0))
                if code is None:                              # el EBREAK del programa entró en el manejador
                    self.assertEqual((hart.mcause, hart.mepc, hart.regs[1]), (CAUSE_BREAKPOINT, CODE + 16, 4))
                else:                                         # entró antes de la primera instrucción
                    self.assertEqual((hart.mcause, hart.mepc, hart.mtval, hart.regs[1]), (1 << 31 | code, CODE, 0, 0))
                    self.assertEqual((hart.mstatus >> 11 & 3, hart.mstatus >> 7 & 1, hart.mstatus >> 3 & 1),
                                     (privilege, mie_bit, 0))
                    # Gas: la entrada (1) y la csrrw del manejador (1), que es la única instrucción retirada.
                    self.assertEqual((hart.privilege, hart.instret, 100 - hart.gas), (3, 1, 2))

    def test_vectored_and_compatible_modes(self) -> None:
        words = [encode(Kind.ADDI, 1, 1, imm=1), encode(Kind.EBREAK)]
        for code, msip, enabled in ((7, 0, 0x80), (3, 1, 0x8)):
            with self.subTest(vectored=code):
                hart, clint, _ = self.make(words)
                hart.mtvec, hart.mie, hart.mstatus = (HANDLER - 4 * code) | 1, enabled, 0x8   # base + 4 × código = HANDLER
                clint.msip = msip
                self.assertEqual(hart.run(100), Stop("ebreak", CAUSE_BREAKPOINT, 0))
                self.assertEqual((hart.mcause, hart.mepc, hart.pc), (1 << 31 | code, CODE, HANDLER + 4))
            with self.subTest(compatible=code):
                hart, clint, _ = self.make(words)
                hart.mtvec, hart.mie, hart.mstatus = 0x1, enabled, 0x8       # vectorizado con la base fuera de la RAM
                clint.msip = msip
                before = (hart.snapshot(), clint.msip, clint.mtimecmp)
                self.assertEqual(hart.run(100), Stop("interrupcion", 1 << 31 | code, 0))
                self.assertEqual((hart.snapshot(), clint.msip, clint.mtimecmp), before)   # nada escrito
                self.assertEqual(hart.gas, 100)                                         # ni gas cobrado
                self.assertEqual(hart.run(100), Stop("interrupcion", 1 << 31 | code, 0))   # y vuelve a parar igual
        hart, clint, _ = self.make(words)                     # destino vectorizado que se sale de la RAM: compatible
        hart.mtvec, hart.mie, hart.mstatus = (BASE + RAM - 4) | 1, 0x80, 0x8
        self.assertEqual(hart.run(100), Stop("interrupcion", 0x80000007, 0))
        hart, clint, _ = self.make(words)                     # sin gas para cobrarla: Stop("gas") sin entregarla
        hart.mtvec, hart.mie, hart.mstatus = HANDLER, 0x80, 0x8
        self.assertEqual(hart.run(0), Stop("gas"))
        self.assertEqual((hart.pc, hart.mcause, hart.gas), (CODE, 0, 0))

    def test_wfi(self) -> None:
        wfi = encode(Kind.WFI)
        read = encode(Kind.CSRRS, 1, 0, imm=0xB02)            # x1 ← minstret
        words = [wfi, read, encode(Kind.EBREAK)]
        with self.subTest(case="NOP con una pendiente habilitada y MIE = 0"):
            hart, clint, _ = self.make(words)
            hart.mie = 0x80                                   # mtimecmp = 0: MTIP pendiente
            self.assertEqual(hart.run(50), Stop("ebreak", CAUSE_BREAKPOINT, 0))
            self.assertEqual((hart.regs[1], hart.instret, 50 - hart.gas), (1, 2, 2))
        for divisor in (1, 7):
            with self.subTest(case="adelanta el reloj hasta el plazo sin gas", divisor=divisor):
                hart, clint, _ = self.make(words, divisor=divisor)
                hart.mie, clint.mtimecmp = 0x80, 1000
                self.assertEqual(hart.run(50), Stop("ebreak", CAUSE_BREAKPOINT, 0))
                self.assertEqual((hart.regs[1], hart.instret, 50 - hart.gas), (1000 * divisor, 1000 * divisor + 1, 2))
        with self.subTest(case="la interrupción que la despierta entra con mepc = pc + 4"):
            hart, clint, _ = self.make(words)
            hart.mtvec, hart.mie, hart.mstatus, clint.mtimecmp = HANDLER, 0x80, 0x8, 300
            self.assertEqual(hart.run(50), Stop("ebreak", CAUSE_BREAKPOINT, 0))
            # instret: la WFI lo deja en 300 y la csrrw del manejador lo sube a 301; x1 sin escribir (no se ejecutó).
            self.assertEqual((hart.mcause, hart.mepc, hart.instret, hart.regs[1]), (0x80000007, CODE + 4, 301, 0))
        for name, setup in (("sin nada en mie", lambda h, c: None),
                            ("solo MSIE y msip = 0", lambda h, c: setattr(h, "mie", 0x8)),
                            ("plazo que no cabe en la vuelta", lambda h, c: (setattr(h, "mie", 0x80),
                                                                             setattr(c, "mtimecmp", 1 << 62)))):
            with self.subTest(case=f"sin evento: {name}"):
                hart, clint, _ = self.make(words, divisor=7)
                setup(hart, clint)
                before = hart.snapshot()
                self.assertEqual(hart.run(50), Stop("wfi"))
                self.assertEqual((hart.snapshot(), hart.gas), (before, 50))   # nada confirmado, ni gas
                self.assertEqual(hart.run(50), Stop("wfi"))                  # la vuelve a ejecutar
        with self.subTest(case="hart sin CLINT"):
            hart, memory = make_hart(assemble(words))
            self.addCleanup(memory.close)
            hart.mie = 0x88
            self.assertEqual((hart.run(50), hart.pc, hart.gas), (Stop("wfi"), CODE, 50))
        # TW: con MTIE y mtimecmp = 1000 (sin nada pendiente, para que ninguna interrupción se adelante en U), WFI es
        # ilegal en U con TW = 1; en U con TW = 0 duerme hasta el plazo y la MTI (tomable en U) para en modo compatible;
        # en M, TW no cuenta: duerme y, con MIE = 0, sigue.
        tw_words = [wfi, encode(Kind.ADDI, 1, 1, imm=1), encode(Kind.EBREAK)]
        for privilege, tw, expected in ((0, 1, (Stop("fallo", CAUSE_ILLEGAL_INSTRUCTION, wfi), CODE, 0, 0)),
                                        (0, 0, (Stop("interrupcion", 0x80000007, 0), CODE + 4, 1000, 0)),
                                        (3, 1, (Stop("ebreak", CAUSE_BREAKPOINT, 0), CODE + 8, 1001, 1))):
            with self.subTest(case="TW", privilege=privilege, tw=tw):
                hart, clint, _ = self.make(tw_words)
                hart.privilege, hart.mstatus, hart.mie, clint.mtimecmp = privilege, tw << 21, 0x80, 1000
                self.assertEqual((hart.run(50), hart.pc, hart.instret, hart.regs[1]), expected)

    def test_the_gas_set_aside_for_the_deadline_comes_back(self) -> None:
        # Con un plazo vivo (MTIE, MIE y mtimecmp = 1000) el bucle aparta el gas que pasa del plazo; si run para antes por
        # otra causa (EBREAK en modo compatible, SYSCON), ese gas vuelve: solo se cobra lo ejecutado.
        words = [encode(Kind.ADDI, 1, 1, imm=1)] * 3 + [encode(Kind.EBREAK)]
        hart, clint, _ = self.make(words)
        hart.mie, hart.mstatus, clint.mtimecmp = 0x80, 0x8, 1000
        self.assertEqual(hart.run(5000), Stop("ebreak", CAUSE_BREAKPOINT, 0))
        self.assertEqual((hart.instret, hart.gas), (3, 4997))
        machine = Machine(ram_bytes=RAM)
        self.addCleanup(machine.close)
        machine.load_flat(assemble(load_immediate(12, 0x11100000) + load_immediate(13, 0x5555) + [encode(Kind.SW, 0, 12, 13, imm=0)]))
        machine.hart.mie, machine.hart.mstatus, machine.clint.mtimecmp = 0x80, 0x8, 1000
        self.assertEqual(machine.run(5000), Stop("apagado"))
        self.assertEqual((machine.hart.instret, machine.hart.gas), (5, 4995))

    def test_mtip_compares_the_wrapped_counter_inside_one_run(self) -> None:
        # WFI lleva instret a 2⁶⁴ − 5 (mtimecmp = 2⁶⁴ − 5) y el mismo run cruza 2⁶⁴: tras cruzar, mtime vuelve a 0 y MTIP
        # deja de estar pendiente aunque el contador local de run no se haya envuelto todavía.
        words = [encode(Kind.WFI), encode(Kind.CSRRS, 1, 0, imm=0x344)] + [encode(Kind.ADDI, 0, 0, imm=0)] * 8 + [
            encode(Kind.CSRRS, 2, 0, imm=0x344), encode(Kind.CSRRS, 3, 0, imm=0xC81), encode(Kind.EBREAK)]
        hart, clint, _ = self.make(words)
        hart.mie, clint.mtimecmp = 0x80, M64 - 4
        self.assertEqual(hart.run(100), Stop("ebreak", CAUSE_BREAKPOINT, 0))
        self.assertEqual((hart.regs[1], hart.regs[2], hart.regs[3]), (0x80, 0, 0))   # pendiente antes; después, no
        self.assertEqual(hart.instret, 6)                     # 2⁶⁴ − 5 + 11 retiradas tras la WFI, envuelto

    def test_writes_inside_a_run_move_the_deadline(self) -> None:
        # Un plazo calculado al empezar (mtimecmp = 1000) no vale tras escribir mtimecmp = 40 por MMIO, ni el «sin
        # plazo» tras habilitar MTIE en mie dentro del mismo run: la interrupción entra en el instret nuevo.
        handler = (encode(Kind.CSRRS, 22, 0, imm=0xB02),) + STOP_HANDLER
        loop = [encode(Kind.ADDI, 10, 0, imm=500), encode(Kind.ADDI, 5, 5, imm=1), encode(Kind.BNE, 0, 5, 10, imm=-4),
                encode(Kind.EBREAK)]
        mmio = [encode(Kind.LUI, 28, imm=MTIMECMP_PAGE << 12), encode(Kind.ADDI, 29, 0, imm=40), encode(Kind.SW, 0, 28, 29, imm=0)]
        enable = [encode(Kind.ADDI, 28, 0, imm=0x80), encode(Kind.CSRRS, 0, 28, imm=0x304)]
        for name, words, mie, mtimecmp in (("mtimecmp por MMIO", mmio + loop, 0x80, 1000), ("MTIE en mie", enable + loop, 0, 40)):
            for accelerate in (False, True):
                with self.subTest(case=name, accelerate=accelerate):
                    hart, clint, _ = self.make(words, handler, accelerate=accelerate, warmup=1)
                    hart.mtvec, hart.mie, hart.mstatus, clint.mtimecmp = HANDLER, mie, 0x8, mtimecmp
                    self.assertEqual(hart.run(5000), Stop("ebreak", CAUSE_BREAKPOINT, 0))
                    self.assertEqual((hart.regs[22], hart.mcause), (40, 0x80000007))


class MExtension(unittest.TestCase):
    """H3.2: las ocho instrucciones de M en el intérprete y dentro de un bloque compilado, frente a un modelo con
    ``Fraction`` escrito aquí (tabla de división entre cero y desbordamiento de la ISA)."""

    @staticmethod
    def model(a: int, b: int) -> list[int]:
        from fractions import Fraction
        from math import trunc
        sa = a - (1 << 32) if a & 0x80000000 else a
        sb = b - (1 << 32) if b & 0x80000000 else b
        if sb == 0:
            div, rem = -1, sa
        elif sa == -(1 << 31) and sb == -1:
            div, rem = sa, 0                              # desbordamiento: cociente INT_MIN, resto 0
        else:
            div = trunc(Fraction(sa, sb))                 # división hacia cero
            rem = sa - sb * div                           # el resto lleva el signo del dividendo
        divu, remu = (M, a) if b == 0 else (a // b, a % b)
        return [(a * b) & M, ((sa * sb) >> 32) & M, ((sa * b) >> 32) & M, (a * b) >> 32, div & M, divu, rem & M, remu]

    def test_every_edge_of_the_division_table_in_the_interpreter_and_in_a_block(self) -> None:
        rng = random.Random(32)
        values = [0, 1, 2, 3, 7, M, M - 1, 0x80000000, 0x80000001, 0x7FFFFFFF, 0x7FFFFFFE, 0x10000, 0xFFFF0000]
        values += [rng.getrandbits(32) for _ in range(12)]
        words = [encode(kind, 3 + index, 1, 2) for index, kind in enumerate(M_KINDS)] + [encode(Kind.EBREAK)]
        compiled = 0
        for a in values:
            for b in values:
                expected = self.model(a, b)
                for accelerate in (False, True):
                    hart, memory = make_hart(assemble(words))
                    self.addCleanup(memory.close)
                    hart.accelerate, hart.warmup = accelerate, 1
                    hart.regs[1], hart.regs[2] = a, b
                    self.assertEqual(hart.run(20).reason, "ebreak")
                    self.assertEqual(hart.regs[3:11], expected, (hex(a), hex(b), accelerate))
                    compiled += hart.block_instructions
        self.assertEqual(compiled, len(values) ** 2 * len(M_KINDS))   # con bloques, las ocho dentro del bloque

    def test_rd_x0_is_not_written_and_misa_announces_m(self) -> None:
        words = [encode(kind, 0, 1, 2) for kind in M_KINDS] + [encode(Kind.CSRRS, 5, 0, imm=0x301), encode(Kind.EBREAK)]
        for accelerate in (False, True):
            hart, memory = make_hart(assemble(words))
            self.addCleanup(memory.close)
            hart.accelerate, hart.warmup = accelerate, 1
            hart.regs[1], hart.regs[2] = 0x80000000, M
            self.assertEqual(hart.run(20).reason, "ebreak")
            self.assertEqual((hart.regs[0], hart.regs[5] & (1 << 12)), (0, 1 << 12))


class AExtension(unittest.TestCase):
    """H3.3: LR/SC y AMO con la política de reserva de la plataforma (R-9), en el intérprete y con el store dentro de un
    bloque caliente; fallos sin confirmar nada."""

    def execute(self, words: list[int], regs: dict[int, int], accelerate: bool, gas: int = 200) -> tuple[Rv32Hart, Stop, Rv32Memory]:
        hart, memory = make_hart(assemble(words))
        self.addCleanup(memory.close)
        hart.accelerate, hart.warmup = accelerate, 1
        for index, value in regs.items():
            hart.regs[index] = value
        return hart, hart.run(gas), memory

    def word(self, memory: Rv32Memory, address: int) -> int:
        return memory.load32(address)

    def test_lr_sc_success_then_sc_without_reservation_fails(self) -> None:
        words = [encode(Kind.LR_W, 1, 10), encode(Kind.SC_W, 2, 10, 3), encode(Kind.SC_W, 4, 10, 5), encode(Kind.LR_W, 6, 10),
                 encode(Kind.SC_W, 7, 11, 5), encode(Kind.SC_W, 8, 10, 5), EB]
        for accelerate in (False, True):
            hart, stop, memory = self.execute(words, {10: DATA, 11: DATA + 4, 3: 0xABCD, 5: 0x5555}, accelerate)
            self.assertEqual(stop.reason, "ebreak")
            self.assertEqual((hart.regs[1], hart.regs[2], hart.regs[4]), (0, 0, 1))      # LR lee 0; SC bien; SC sin reserva falla
            self.assertEqual((hart.regs[6], hart.regs[7], hart.regs[8]), (0xABCD, 1, 1))  # SC a otra palabra falla y la pierde
            self.assertEqual((self.word(memory, DATA), self.word(memory, DATA + 4), hart.reservation), (0xABCD, 0, None))

    def test_every_store_that_touches_the_reserved_word_loses_it_also_from_a_compiled_block(self) -> None:
        # LR; un bucle caliente (compilado con calentamiento 1) hace el store tres veces; SC dice si la reserva sobrevivió.
        cases = [(encode(Kind.SB, 0, 10, 9, imm=0), True), (encode(Kind.SB, 0, 10, 9, imm=3), True),
                 (encode(Kind.SH, 0, 10, 9, imm=2), True), (encode(Kind.SW, 0, 10, 9, imm=0), True),
                 (encode(Kind.SW, 0, 10, 9, imm=-2), True), (encode(Kind.SH, 0, 10, 9, imm=3), True),   # desalineados que la cruzan
                 (encode(Kind.SW, 0, 10, 9, imm=4), False), (encode(Kind.SB, 0, 10, 9, imm=-1), False),  # contiguas: sigue
                 (encode(Kind.SW, 0, 10, 9, imm=16), False), (encode(Kind.AMOADD_W, 0, 11, 9), True)]
        for store, loses in cases:
            words = [encode(Kind.LR_W, 1, 10), encode(Kind.ADDI, 20, 0, imm=3), encode(Kind.ADDI, 5, 5, imm=1), store,
                     encode(Kind.ADDI, 20, 20, imm=-1), encode(Kind.BNE, 0, 20, 0, imm=-12), encode(Kind.SC_W, 2, 10, 3), EB]
            for accelerate in (False, True):
                with self.subTest(store=hex(store), accelerate=accelerate):
                    hart, stop, memory = self.execute(words, {10: DATA + 8, 11: DATA + 8, 9: 0x11, 3: 0x77}, accelerate)
                    self.assertEqual(stop.reason, "ebreak")
                    self.assertEqual(hart.regs[2], 1 if loses else 0)
                    if accelerate and store != encode(Kind.AMOADD_W, 0, 11, 9):
                        self.assertGreater(hart.block_instructions, 0)            # el bucle sí corre en bloques

    def test_amo_results_and_their_effect_on_the_reservation(self) -> None:
        values = [(5, 3), (0x80000000, 1), (M, 2), (0x7FFFFFFF, 0x80000000), (0, M)]
        expected = {Kind.AMOSWAP_W: lambda a, b: b, Kind.AMOADD_W: lambda a, b: (a + b) & M, Kind.AMOXOR_W: lambda a, b: a ^ b,
                    Kind.AMOAND_W: lambda a, b: a & b, Kind.AMOOR_W: lambda a, b: a | b,
                    Kind.AMOMIN_W: lambda a, b: a if alu.to_signed(a) < alu.to_signed(b) else b,
                    Kind.AMOMAX_W: lambda a, b: a if alu.to_signed(a) > alu.to_signed(b) else b,
                    Kind.AMOMINU_W: min, Kind.AMOMAXU_W: max}
        for kind, model in expected.items():
            for old, operand in values:
                with self.subTest(kind=kind.name, old=hex(old), operand=hex(operand)):
                    words = [encode(Kind.SW, 0, 10, 4, imm=0), encode(Kind.LR_W, 6, 11), encode(kind, 5, 10, 5), EB]
                    hart, stop, memory = self.execute(words, {10: DATA, 11: DATA + 4, 4: old, 5: operand}, False)
                    self.assertEqual((hart.regs[5], self.word(memory, DATA)), (old, model(old, operand)))   # rd = rs2: el viejo
                    self.assertEqual(hart.reservation, DATA + 4)                       # AMO a otra palabra: la reserva sigue
        hart, stop, memory = self.execute([encode(Kind.LR_W, 6, 10), encode(Kind.AMOSWAP_W, 0, 10, 5), EB], {10: DATA, 5: 1}, False)
        self.assertEqual(hart.reservation, None)                                       # AMO a la palabra reservada: la pierde

    def test_faults_commit_nothing(self) -> None:
        isa = rv32_isa
        for kind in A_KINDS:
            for address, cause_load, cause_store in ((DATA + 1, 4, 6), (DATA + 2, 4, 6), (BASE + RAM, 5, 7), (BASE - 4, 5, 7),
                                                     (BASE + RAM - 2, 4, 6)):
                with self.subTest(kind=kind.name, address=hex(address)):
                    word = encode(kind, 5, 10, 0 if kind == Kind.LR_W else 6)
                    hart, stop, memory = self.execute([word, EB], {10: address, 5: 0x99, 6: 7}, False)
                    cause = cause_load if kind == Kind.LR_W else cause_store
                    self.assertEqual(stop, Stop("fallo", cause, address))
                    self.assertEqual((hart.regs[5], hart.reservation, hart.instret, bytes(memory.lanes[0x1000:0x1010])),
                                     (0x99, None, 0, bytes(16)))
        # En una ventana MMIO (el UART) los atómicos son fallos de acceso.
        machine = Machine(ram_bytes=RAM)
        self.addCleanup(machine.close)
        machine.load_flat(assemble([encode(Kind.AMOOR_W, 5, 10, 6), EB]))
        machine.hart.regs[10] = 0x10000000
        self.assertEqual(machine.run(10), Stop("fallo", isa.CAUSE_STORE_ACCESS_FAULT, 0x10000000))
        self.assertEqual(bytes(machine.uart.output), b"")

    def test_atomics_on_the_clint_window_fault_and_touch_nothing(self) -> None:
        # Segunda ciega de H3.2 + H3.3 (ciega 5): los atómicos nunca llegan a un dispositivo, tampoco al CLINT (msip, mtimecmp
        # y mtime en su dirección real): fallo de acceso con mtval = dirección, en M y en U, y msip/mtimecmp intactos.
        for address in (CLINT_BASE, CLINT_BASE + 0x4000, CLINT_BASE + 0xBFF8):
            for kind in (Kind.LR_W, Kind.SC_W, Kind.AMOSWAP_W, Kind.AMOADD_W):
                for privilege in (3, 0):
                    with self.subTest(address=hex(address), kind=kind.name, privilege=privilege):
                        machine = Machine(ram_bytes=RAM)
                        self.addCleanup(machine.close)
                        machine.load_flat(assemble([encode(kind, 5, 10, 0 if kind == Kind.LR_W else 6), EB]))
                        machine.hart.regs[10], machine.hart.regs[5], machine.hart.regs[6] = address, 0x99, 0xFFFFFFFF
                        machine.hart.privilege = privilege
                        machine.clint.msip, machine.clint.mtimecmp = 1, 123
                        cause = CAUSE_LOAD_ACCESS_FAULT if kind == Kind.LR_W else CAUSE_STORE_ACCESS_FAULT
                        self.assertEqual(machine.run(10), Stop("fallo", cause, address))
                        self.assertEqual((machine.hart.regs[5], machine.hart.reservation, machine.hart.instret),
                                         (0x99, None, 0))
                        self.assertEqual((machine.clint.msip, machine.clint.mtimecmp), (1, 123))

    def test_amo_and_sc_rewrite_an_instruction_of_a_hot_loop(self) -> None:
        # Segunda ciega de H3.2 + H3.3 (ciega 5): un AMOADD (o un LR/ADD/SC) suma 1 al inmediato de la ADDI que abre el bucle en
        # cada vuelta; la vuelta siguiente ejecuta siempre la instrucción vigente, también con el bucle compilado: x6 = 1 + … + 10.
        head = load_immediate(10, CODE + 4 * 5) + load_immediate(11, 1 << 20) + [encode(Kind.ADDI, 20, 0, imm=10)]
        assert len(head) == 5                                    # la ADDI x6 que se reescribe es la sexta palabra (CODE + 20)
        bodies = {"amo": [encode(Kind.ADDI, 6, 6, imm=1), encode(Kind.AMOADD_W, 12, 10, 11), encode(Kind.ADDI, 20, 20, imm=-1),
                          encode(Kind.BNE, 0, 20, 0, imm=-12), EB],
                  "lr/sc": [encode(Kind.ADDI, 6, 6, imm=1), encode(Kind.LR_W, 13, 10), encode(Kind.ADD, 13, 13, 11),
                            encode(Kind.SC_W, 14, 10, 13), encode(Kind.ADDI, 20, 20, imm=-1), encode(Kind.BNE, 0, 20, 0, imm=-20), EB]}
        for name, body in bodies.items():
            image = assemble(head + body)
            reference = Reference(image)
            self.assertEqual(reference.run(500), Stop("ebreak", CAUSE_BREAKPOINT, 0))
            self.assertEqual(reference.regs[6], 55)
            for accelerate, warmup in ((False, 2), (True, 1), (True, 2)):
                with self.subTest(body=name, accelerate=accelerate, warmup=warmup):
                    hart, memory = make_hart(image)
                    self.addCleanup(memory.close)
                    hart.accelerate, hart.warmup = accelerate, warmup
                    self.assertEqual(hart.run(500), Stop("ebreak", CAUSE_BREAKPOINT, 0))
                    self.assertEqual((hart.regs, hart.pc, hart.instret), (reference.regs, reference.pc, reference.count))
                    self.assertEqual(bytes(memory.lanes), bytes(reference.mem))

    def test_lr_replaces_and_reset_attach_and_load_flat_lose_the_reservation(self) -> None:
        hart, stop, memory = self.execute([encode(Kind.LR_W, 1, 10), encode(Kind.LR_W, 2, 11), EB], {10: DATA, 11: DATA + 8}, False)
        self.assertEqual(hart.reservation, DATA + 8)
        hart.reset()
        self.assertIsNone(hart.reservation)
        hart.reservation = DATA
        hart.attach_memory(memory)
        self.assertIsNone(hart.reservation)
        machine = Machine(ram_bytes=RAM)
        self.addCleanup(machine.close)
        machine.hart.reservation = DATA
        machine.load_flat(assemble([EB]))
        self.assertIsNone(machine.hart.reservation)

    def test_restore_marks_the_line_of_the_reservation_again(self) -> None:
        words = [encode(Kind.LR_W, 1, 10), encode(Kind.ADDI, 20, 0, imm=3), encode(Kind.ADDI, 5, 5, imm=1),
                 encode(Kind.SW, 0, 10, 9, imm=0), encode(Kind.ADDI, 20, 20, imm=-1), encode(Kind.BNE, 0, 20, 0, imm=-12),
                 encode(Kind.SC_W, 2, 10, 3), EB]
        source, stop, _ = self.execute(words, {10: DATA, 9: 1, 3: 2}, False, gas=1)        # solo el LR
        state = source.snapshot()
        self.assertEqual(state["hart"]["reservation"], DATA)
        memory = Rv32Memory(RAM)
        self.addCleanup(memory.close)
        target = Rv32Hart(memory)
        target.accelerate, target.warmup = True, 1
        target.restore(state)
        self.assertEqual(memory.code[(DATA - BASE) >> 4], 1)                           # la línea de la reserva, marcada
        self.assertEqual(target.run(100).reason, "ebreak")
        self.assertEqual(target.regs[2], 1)                                            # el store del bloque la perdió
        self.assertGreater(target.block_instructions, 0)


class ZicsrAndTraps(unittest.TestCase):
    """Anclas dirigidas de H3.1: el mapa de CSR fila a fila, la entrada en trap y su orden, MRET, modo U, modo compatible
    y atomicidad."""

    def run_words(self, words: list[int], gas: int = 200, regs: dict[int, int] | None = None, handler: bool = True,
                  setup=None) -> tuple[Rv32Hart, Stop, Rv32Memory]:
        image = with_handler(words) if handler else assemble(words)
        hart, memory = make_hart(image)
        self.addCleanup(memory.close)
        if handler:
            hart.mtvec = HANDLER
        for index, value in (regs or {}).items():
            hart.regs[index] = value
        if setup:
            setup(hart)
        return hart, hart.run(gas), memory

    def test_every_row_of_the_csr_map(self) -> None:
        # Programa: x5 = todo unos; x1 ← csr (con instret = 2); x2 ← csr y csr ← x5 (instret = 3); x3 ← csr (instret = 4).
        # (csr, x1, x2, x3) para los escribibles o de escritura ignorada; (csr, x1, None, None) si escribir es ilegal.
        rows = [(0x300, 0, 0, 0x221888), (0x301, 0x40101101, 0x40101101, 0x40101101), (0x304, 0, 0, 0x888),
                (0x305, 0, 0, 0xFFFFFFFD), (0x306, 7, 7, 7), (0x310, 0, 0, 0), (0x323, 0, 0, 0), (0x33F, 0, 0, 0),
                (0x340, 0, 0, M), (0x341, 0, 0, 0xFFFFFFFC), (0x342, 0, 0, M), (0x343, 0, 0, M), (0x344, 0, 0, 0),
                (0xB00, 2, 3, 4), (0xB02, 2, 3, 4), (0xB80, 0, 0, 0), (0xB82, 0, 0, 0), (0xB03, 0, 0, 0), (0xB9F, 0, 0, 0),
                (0xC00, 2, None, None), (0xC01, 2, None, None), (0xC02, 2, None, None), (0xC80, 0, None, None),
                (0xC81, 0, None, None), (0xC82, 0, None, None), (0xC03, 0, None, None), (0xC9F, 0, None, None),
                (0xF11, 0, None, None), (0xF12, 0, None, None), (0xF13, 0, None, None), (0xF14, 0, None, None),
                (0xF15, 0, None, None)]
        for csr, first, second, third in rows:
            with self.subTest(csr=hex(csr)):
                words = [encode(Kind.ADDI, 5, 0, imm=-1), encode(Kind.ADDI, 6, 0, imm=0), encode(Kind.CSRRS, 1, 0, imm=csr),
                         encode(Kind.CSRRW, 2, 5, imm=csr), encode(Kind.CSRRS, 3, 0, imm=csr), encode(Kind.EBREAK)]
                hart, stop, _ = self.run_words(words, handler=False)
                self.assertEqual(hart.regs[1], first)
                if second is None:                                         # número de solo lectura: escribir es ilegal
                    self.assertEqual(stop, Stop("fallo", CAUSE_ILLEGAL_INSTRUCTION, words[3]))
                    self.assertEqual((hart.regs[2], hart.pc, hart.instret), (0, CODE + 12, 3))   # nada confirmado
                else:
                    self.assertEqual(stop.reason, "ebreak")
                    self.assertEqual((hart.regs[2], hart.regs[3]), (second, third))
        for csr in (0x7C0, 0x302, 0x303, 0x320, 0x180, 0x7A0, 0x3A0, 0x3B0, 0x100, 0xC20, 0xB20, 0x345, 0xF16):
            with self.subTest(csr=hex(csr)):
                words = [encode(Kind.CSRRS, 1, 0, imm=csr), encode(Kind.EBREAK)]
                hart, stop, _ = self.run_words(words, handler=False)
                self.assertEqual(stop, Stop("fallo", CAUSE_ILLEGAL_INSTRUCTION, words[0]))

    def test_read_csr_agrees_with_csrrs_for_every_number_in_m_and_u(self) -> None:
        # Rv32Hart.read_csr es la lectura del host con las reglas de la instrucción (oráculo 1 de H3, mutante
        # read_csr_sin_check_hpm_user): para los 4 096 números en M y en U, read_csr lanza el fallo ilegal exactamente
        # cuando CSRRS x1, csr, x0 se detiene con él (modo compatible) y, si no, devuelve lo que la instrucción lee.
        from cpu_digital.rv32_hart import _Halt
        hart, memory = make_hart(b"")
        self.addCleanup(memory.close)
        legal = {3: 0, 0: 0}
        for privilege in (3, 0):
            for csr in range(4096):
                word = encode(Kind.CSRRS, 1, 0, imm=csr)
                memory.store32(CODE, word)
                hart.pc, hart.instret, hart.regs[1], hart.privilege = CODE, 0, 0xDEAD, privilege
                stop = hart.run(1)
                hart.pc, hart.instret = CODE, 0
                with self.subTest(privilege=privilege, csr=hex(csr)):
                    try:
                        value = hart.read_csr(csr, word)
                    except _Halt as halt:
                        self.assertEqual((halt.cause, halt.value), (CAUSE_ILLEGAL_INSTRUCTION, word))
                        self.assertEqual(stop, Stop("fallo", CAUSE_ILLEGAL_INSTRUCTION, word))
                    else:
                        self.assertEqual(stop, Stop("gas"))
                        self.assertEqual(value, hart.regs[1])
                        legal[privilege] += 1
        # Del mapa de la spec: en M, 6 contadores de usuario + 58 hpmcounter3–31(h) + mcycle/minstret(h) (4) + 87
        # mhpmcounter3–31(h)/mhpmevent3–31 + 11 de estado y control (mstatus, misa, mie, mtvec, mcounteren, mstatush,
        # mscratch, mepc, mcause, mtval, mip) + 5 de identificación = 171; en U, solo cycle, time e instret (y sus h).
        self.assertEqual(legal, {3: 171, 0: 6})

    def test_the_six_zicsr_forms_and_their_write_rules(self) -> None:
        words = [encode(Kind.ADDI, 5, 0, imm=0x0F0), encode(Kind.CSRRW, 1, 5, imm=0x340),     # mscratch = 0xF0, x1 = 0
                 encode(Kind.CSRRS, 2, 0, imm=0x340), encode(Kind.ADDI, 6, 0, imm=0x00F),
                 encode(Kind.CSRRS, 3, 6, imm=0x340),                                          # | 0x0F → 0xFF, x3 = 0xF0
                 encode(Kind.CSRRC, 4, 5, imm=0x340),                                          # & ~0xF0 → 0x0F, x4 = 0xFF
                 encode(Kind.CSRRWI, 7, 0x1F, imm=0x340), encode(Kind.CSRRSI, 8, 0x0, imm=0xC00),   # uimm 0: lee sin escribir
                 encode(Kind.CSRRCI, 9, 0x3, imm=0x340), encode(Kind.CSRRS, 10, 0, imm=0x340),   # 0x1F & ~3 = 0x1C
                 encode(Kind.CSRRW, 0, 5, imm=0x340), encode(Kind.CSRRS, 11, 0, imm=0x340),     # rd = x0 no lee: escribe
                 encode(Kind.CSRRW, 12, 12, imm=0x340), encode(Kind.CSRRS, 13, 0, imm=0x340), encode(Kind.EBREAK)]
        hart, stop, _ = self.run_words(words, handler=False, regs={12: 0x55})
        self.assertEqual(stop.reason, "ebreak")
        regs = hart.regs
        self.assertEqual((regs[1], regs[2], regs[3], regs[4], regs[7], regs[9], regs[10], regs[11], regs[12], regs[13]),
                         (0, 0xF0, 0xF0, 0xFF, 0x0F, 0x1F, 0x1C, 0xF0, 0xF0, 0x55))
        self.assertEqual(regs[8], 7)                                                           # cycle en la 8.ª
        # CSRRS/CSRRC con rs1 ≠ x0 escriben aunque el valor sea 0: sobre un CSR de solo lectura, ilegal.
        for word in (encode(Kind.CSRRS, 1, 6, imm=0xC00), encode(Kind.CSRRC, 1, 6, imm=0xF14),
                     encode(Kind.CSRRSI, 1, 1, imm=0xC00), encode(Kind.CSRRWI, 1, 0, imm=0xF11)):
            hart, stop, _ = self.run_words([word], handler=False, regs={6: 0})
            self.assertEqual(stop, Stop("fallo", CAUSE_ILLEGAL_INSTRUCTION, word), hex(word))
        for word in (encode(Kind.CSRRS, 1, 0, imm=0xC00), encode(Kind.CSRRC, 1, 0, imm=0xF14), encode(Kind.CSRRSI, 1, 0, imm=0xF11)):
            hart, stop, _ = self.run_words([word, encode(Kind.EBREAK)], handler=False)
            self.assertEqual(stop.reason, "ebreak", hex(word))

    def test_trap_entry_writes_the_six_fields_and_charges_one_instruction(self) -> None:
        isa = rv32_isa   # constantes de H3 leídas del módulo al ejecutar (sin el cambio, falla la conducta, no el import)
        far = BASE + RAM
        cases = [  # (palabras, gas, mepc, mcause, mtval, instret, x1)
            ([encode(Kind.ECALL)], 1, CODE, CAUSE_ECALL_FROM_M, 0, 0, 0),
            ([encode(Kind.ADDI, 1, 0, imm=3), encode(Kind.EBREAK)], 2, CODE + 4, CAUSE_BREAKPOINT, 0, 1, 3),
            ([0xFFFFFFFF], 1, CODE, CAUSE_ILLEGAL_INSTRUCTION, 0xFFFFFFFF, 0, 0),
            ([encode(Kind.LW, 1, 5, imm=0)], 1, CODE, CAUSE_LOAD_ACCESS_FAULT, far, 0, 0),
            ([encode(Kind.SW, 0, 5, 5, imm=4)], 1, CODE, isa.CAUSE_STORE_ACCESS_FAULT, far + 4, 0, 0),
            ([encode(Kind.JAL, 1, imm=6)], 1, CODE, CAUSE_INSTRUCTION_MISALIGNED, CODE + 6, 0, 0),    # rd no se escribe
            ([encode(Kind.JALR, 1, 5, imm=0)], 2, far, CAUSE_INSTRUCTION_ACCESS_FAULT, far, 1, CODE + 4),   # jalr sí retira
        ]
        for words, gas, mepc, cause, tval, retired, x1 in cases:
            with self.subTest(cause=cause, word=hex(words[0])):
                hart, stop, _ = self.run_words(words, gas=gas, regs={5: far},
                                               setup=lambda h: setattr(h, "mstatus", isa.MSTATUS_MIE | isa.MSTATUS_TW))
                self.assertEqual(stop, Stop("gas"))
                self.assertEqual((hart.mepc, hart.mcause, hart.mtval), (mepc, cause, tval))
                self.assertEqual(hart.mstatus, isa.MSTATUS_MPIE | isa.MSTATUS_MPP | isa.MSTATUS_TW)   # MIE → MPIE, MPP = M
                self.assertEqual((hart.privilege, hart.pc, hart.instret, hart.gas, hart.regs[1]), (isa.PRIV_M, HANDLER, retired, 0, x1))

    def test_mret_returns_to_mpp_and_user_mode_is_restricted(self) -> None:
        isa = rv32_isa   # constantes de H3 leídas del módulo al ejecutar (sin el cambio, falla la conducta, no el import)
        # mepc ← user; MPP ← U; MPIE ← 1; mret → en U: csrr mstatus (ilegal), ecall (causa 8), mret (ilegal), cycle (legal).
        words = load_immediate(5, CODE + 0x40) + [encode(Kind.CSRRW, 0, 5, imm=0x341), encode(Kind.ADDI, 6, 0, imm=0x80),
                                                  encode(Kind.CSRRS, 0, 6, imm=0x300), encode(Kind.MRET)]
        words += [encode(Kind.ADDI, 0, 0, imm=0)] * (16 - len(words))
        words += [encode(Kind.CSRRS, 1, 0, imm=0x300), encode(Kind.ECALL), encode(Kind.MRET), encode(Kind.CSRRS, 2, 0, imm=0xC00),
                  encode(Kind.CSRRS, 3, 0, imm=0xC03), encode(Kind.CSRRS, 4, 0, imm=0xB00), encode(Kind.EBREAK)]
        causes = []
        hart, stop, memory = self.run_words(words, gas=6)
        self.assertEqual((stop, hart.privilege, hart.pc), (Stop("gas"), isa.PRIV_U, CODE + 0x40))
        self.assertEqual(hart.mstatus & (isa.MSTATUS_MIE | isa.MSTATUS_MPIE | isa.MSTATUS_MPP), isa.MSTATUS_MPIE | isa.MSTATUS_MIE)
        for expected_cause, expected_pc in ((CAUSE_ILLEGAL_INSTRUCTION, CODE + 0x40), (CAUSE_ECALL_FROM_U, CODE + 0x44),
                                            (CAUSE_ILLEGAL_INSTRUCTION, CODE + 0x48), (CAUSE_ILLEGAL_INSTRUCTION, CODE + 0x50),
                                            (CAUSE_ILLEGAL_INSTRUCTION, CODE + 0x54)):
            stop = hart.run(1)                                   # el trap (1 de gas)
            causes.append((hart.mcause, hart.mepc, hart.privilege, (hart.mstatus >> 11) & 3))
            self.assertEqual(causes[-1], (expected_cause, expected_pc, isa.PRIV_M, isa.PRIV_U), causes)
            self.assertEqual(hart.mstatus & isa.MSTATUS_MIE, 0)
            stop = hart.run(4)                                   # el manejador: mepc + 4 y mret de vuelta a U
            self.assertEqual(hart.privilege, isa.PRIV_U)
            if expected_pc == CODE + 0x48:
                self.assertEqual(hart.run(1), Stop("gas"))       # cycle sí es legible en U (mcounteren)
                self.assertEqual((hart.regs[2], hart.privilege), (hart.instret - 1, isa.PRIV_U))
        self.assertEqual((hart.run(1), hart.mcause, hart.mepc), (Stop("gas"), CAUSE_BREAKPOINT, CODE + 0x58))   # ebreak en U
        # MRET desde M a M conserva MPRV; a U lo borra.
        for mpp, mprv_after in ((3, isa.MSTATUS_MPRV), (0, 0)):
            words = load_immediate(5, CODE + 0x20) + [encode(Kind.CSRRW, 0, 5, imm=0x341), encode(Kind.MRET)]
            hart, stop, _ = self.run_words(words, gas=4, setup=lambda h, mpp=mpp: setattr(h, "mstatus", isa.MSTATUS_MPRV | mpp << 11))
            self.assertEqual((hart.privilege, hart.pc, hart.mstatus & isa.MSTATUS_MPRV), (mpp, CODE + 0x20, mprv_after))
            self.assertEqual(hart.mstatus & isa.MSTATUS_MPP, 0)    # MPP ← U tras MRET

    def test_compat_mode_stops_without_writing_anything_and_a_trap_loop_consumes_gas(self) -> None:
        for mtvec in (0, 1, BASE + RAM, BASE - 4, 0x00000800):
            with self.subTest(mtvec=hex(mtvec)):
                hart, stop, _ = self.run_words([encode(Kind.ECALL)], handler=False, setup=lambda h, v=mtvec: setattr(h, "mtvec", v))
                self.assertEqual(stop, Stop("ecall", CAUSE_ECALL_FROM_M, 0))
                self.assertEqual((hart.mepc, hart.mcause, hart.mtval, hart.mstatus, hart.pc, hart.gas, hart.instret),
                                 (0, 0, 0, 0, CODE, 200, 0))
        # mtvec apunta a una instrucción ilegal: cada entrada en trap cobra 1 de gas y run termina por gas.
        hart, stop, _ = self.run_words([0x00000000], handler=False, gas=50, setup=lambda h: setattr(h, "mtvec", CODE))
        self.assertEqual((stop, hart.instret, hart.gas, hart.mepc, hart.mcause), (Stop("gas"), 0, 0, CODE, CAUSE_ILLEGAL_INSTRUCTION))
        # En modo vectorizado las excepciones van a la base.
        hart, stop, _ = self.run_words([encode(Kind.ECALL)], gas=1, setup=lambda h: setattr(h, "mtvec", HANDLER | 1))
        self.assertEqual((hart.pc, hart.mcause), (HANDLER, CAUSE_ECALL_FROM_M))

    def test_a_failing_instruction_commits_nothing(self) -> None:
        isa = rv32_isa   # constantes de H3 leídas del módulo al ejecutar (sin el cambio, falla la conducta, no el import)
        # CSR sin privilegio (en U) o inexistente: ni el CSR ni rd; salto desalineado: ni rd ni pc; carga fuera: ni rd.
        for words, privilege in (([encode(Kind.CSRRW, 1, 5, imm=0x340)], isa.PRIV_U), ([encode(Kind.CSRRW, 1, 5, imm=0x7C0)], isa.PRIV_M),
                                 ([encode(Kind.CSRRS, 1, 5, imm=0xF14)], isa.PRIV_M), ([encode(Kind.JALR, 1, 5, imm=2)], isa.PRIV_M),
                                 ([encode(Kind.LW, 1, 6, imm=0)], isa.PRIV_M), ([encode(Kind.CSRRS, 1, 0, imm=0xC03)], isa.PRIV_U)):
            with self.subTest(word=hex(words[0]), privilege=privilege):
                def setup(hart, privilege=privilege):
                    hart.privilege = privilege
                    hart.mscratch = 0x77
                hart, stop, memory = self.run_words(words, gas=1, regs={1: 0x1234, 5: CODE + 0x10, 6: 0x10}, setup=setup)
                self.assertEqual((hart.regs[1], hart.mscratch, hart.instret, hart.pc), (0x1234, 0x77, 0, HANDLER))
                self.assertEqual(hart.mepc, CODE)

    def test_trap_entry_and_mret_lose_the_reservation(self) -> None:
        # Política de la plataforma (H3.3 la completa con LR/SC): la entrada en trap y MRET pierden la reserva.
        hart, stop, _ = self.run_words([encode(Kind.ECALL)], gas=1, setup=lambda h: setattr(h, "reservation", DATA))
        self.assertEqual((hart.pc, hart.reservation), (HANDLER, None))
        words = load_immediate(5, CODE + 0x20) + [encode(Kind.CSRRW, 0, 5, imm=0x341), encode(Kind.ADDI, 0, 0, imm=0), encode(Kind.MRET)]
        hart, stop, _ = self.run_words(words, gas=4)
        hart.reservation = DATA                                        # puesta justo antes del mret
        self.assertEqual((hart.run(1), hart.pc, hart.reservation), (Stop("gas"), CODE + 0x20, None))
        hart, stop, _ = self.run_words([encode(Kind.ECALL)], handler=False, setup=lambda h: setattr(h, "reservation", DATA))
        self.assertEqual((stop.reason, hart.reservation), ("ecall", DATA))   # modo compatible: no se escribe nada

    def test_mip_reads_the_clint(self) -> None:
        isa = rv32_isa   # constantes de H3 leídas del módulo al ejecutar (sin el cambio, falla la conducta, no el import)
        words = [encode(Kind.CSRRS, 1, 0, imm=0x344), encode(Kind.CSRRW, 0, 5, imm=0x344), encode(Kind.CSRRS, 2, 0, imm=0x344),
                 encode(Kind.EBREAK)]
        for msip, mtimecmp, expected in ((0, 0, isa.MIP_MTIP), (1, 0, isa.MIP_MTIP | isa.MIP_MSIP), (1, 1 << 40, isa.MIP_MSIP),
                                         (0, 1 << 40, 0), (0, 2, 0)):
            with self.subTest(msip=msip, mtimecmp=mtimecmp):
                machine = Machine(ram_bytes=RAM)
                self.addCleanup(machine.close)
                machine.load_flat(assemble(words))
                machine.clint.msip, machine.clint.mtimecmp = msip, mtimecmp
                machine.hart.regs[5] = M                               # escribir mip no cambia nada
                self.assertEqual(machine.run(10).reason, "ebreak")
                self.assertEqual((machine.hart.regs[1], machine.hart.regs[2]), (expected, expected if mtimecmp != 2 else isa.MIP_MTIP))


class SingleInstructions(unittest.TestCase):
    def run_words(self, words: list[int], gas: int = 100, regs: dict[int, int] | None = None) -> tuple[Rv32Hart, Stop]:
        hart, memory = make_hart(assemble(words))
        self.addCleanup(memory.close)
        for index, value in (regs or {}).items():
            hart.regs[index] = value
        stop = hart.run(gas)
        return hart, stop

    def test_x0_is_never_written(self) -> None:
        hart, stop = self.run_words([encode(Kind.ADDI, 0, 0, imm=5), encode(Kind.LUI, 0, imm=0x1000),
                                     encode(Kind.JAL, 0, imm=4), encode(Kind.LW, 0, 10, imm=0), encode(Kind.EBREAK)],
                                    regs={10: DATA})
        self.assertEqual(stop.reason, "ebreak")
        self.assertEqual(hart.regs[0], 0)
        self.assertEqual(hart.instret, 4)

    def test_jalr_clears_bit_zero_and_misaligned_targets_fault(self) -> None:
        hart, stop = self.run_words([encode(Kind.JALR, 1, 5, imm=1), encode(Kind.EBREAK), encode(Kind.EBREAK)],
                                    regs={5: CODE + 8})
        self.assertEqual((stop.reason, hart.pc, hart.regs[1]), ("ebreak", CODE + 8, CODE + 4))
        hart, stop = self.run_words([encode(Kind.JALR, 1, 5, imm=2)], regs={5: CODE})
        self.assertEqual(stop, Stop("fallo", CAUSE_INSTRUCTION_MISALIGNED, CODE + 2))
        self.assertEqual((hart.pc, hart.regs[1], hart.instret, hart.gas), (CODE, 0, 0, 100))
        hart, stop = self.run_words([encode(Kind.BEQ, 0, 0, 0, imm=6)])
        self.assertEqual(stop, Stop("fallo", CAUSE_INSTRUCTION_MISALIGNED, CODE + 6))
        hart, stop = self.run_words([encode(Kind.JAL, 1, imm=10)])
        self.assertEqual(stop, Stop("fallo", CAUSE_INSTRUCTION_MISALIGNED, CODE + 10))
        self.assertEqual(hart.regs[1], 0)

    def test_loads_and_stores(self) -> None:
        words = [
            encode(Kind.SW, 0, 10, 5, imm=0), encode(Kind.LB, 1, 10, imm=3), encode(Kind.LBU, 2, 10, imm=3),
            encode(Kind.LH, 3, 10, imm=2), encode(Kind.LHU, 4, 10, imm=2), encode(Kind.LW, 6, 10, imm=0),
            encode(Kind.SB, 0, 10, 7, imm=4), encode(Kind.SH, 0, 10, 7, imm=6), encode(Kind.LW, 8, 10, imm=4),
            encode(Kind.LW, 9, 10, imm=1), encode(Kind.SW, 0, 10, 5, imm=9), encode(Kind.LW, 12, 10, imm=8),
            encode(Kind.EBREAK),
        ]
        hart, stop = self.run_words(words, regs={10: DATA, 5: 0x80FF7F01, 7: 0xAABBCCDD})
        self.assertEqual(stop.reason, "ebreak")
        self.assertEqual(hart.regs[1], 0xFFFFFF80)
        self.assertEqual(hart.regs[2], 0x80)
        self.assertEqual(hart.regs[3], 0xFFFF80FF)
        self.assertEqual(hart.regs[4], 0x80FF)
        self.assertEqual(hart.regs[6], 0x80FF7F01)
        self.assertEqual(hart.regs[8], 0xCCDD00DD)
        self.assertEqual(hart.regs[9], 0xDD80FF7F)       # lw desalineada
        self.assertEqual(hart.regs[12], 0xFF7F0100)      # sw desalineada en +9 escribió 01 7F FF 80 en 9..12

    def test_sltiu_with_negative_immediate_and_shifts(self) -> None:
        hart, stop = self.run_words([encode(Kind.SLTIU, 1, 5, imm=-1), encode(Kind.SLTI, 2, 5, imm=-1),
                                     encode(Kind.SRAI, 3, 6, imm=31), encode(Kind.SRLI, 4, 6, imm=31),
                                     encode(Kind.SLL, 7, 6, 8), encode(Kind.EBREAK)],
                                    regs={5: 0xFFFFFFFE, 6: 0x80000000, 8: 33})
        self.assertEqual(hart.regs[1:5], [1, 1, 0xFFFFFFFF, 1])  # −2 < −1 con signo; 0xFFFFFFFE < 0xFFFFFFFF sin signo
        self.assertEqual(hart.regs[7], 0)

    def test_faults_leave_state_untouched(self) -> None:
        cases = [
            ([encode(Kind.LW, 1, 5, imm=0)], {5: BASE + RAM}, Stop("fallo", CAUSE_LOAD_ACCESS_FAULT, BASE + RAM)),
            ([encode(Kind.LW, 1, 5, imm=0)], {5: BASE + RAM - 2}, Stop("fallo", CAUSE_LOAD_ACCESS_FAULT, BASE + RAM - 2)),
            ([encode(Kind.SB, 0, 5, 6, imm=-1)], {5: BASE, 6: 7}, Stop("fallo", CAUSE_STORE_ACCESS_FAULT, BASE - 1)),
            ([0x00000000], {}, Stop("fallo", CAUSE_ILLEGAL_INSTRUCTION, 0)),
            ([0x043100B3], {}, Stop("fallo", CAUSE_ILLEGAL_INSTRUCTION, 0x043100B3)),   # OP con funct7 = 2 (H3.2: MUL es legal)
            ([encode(Kind.ECALL)], {}, Stop("ecall", CAUSE_ECALL_FROM_M, 0)),
            ([encode(Kind.EBREAK)], {}, Stop("ebreak", CAUSE_BREAKPOINT, 0)),
            ([encode(Kind.JALR, 0, 5, imm=0)], {5: BASE + RAM}, Stop("fallo", CAUSE_INSTRUCTION_ACCESS_FAULT, BASE + RAM)),
        ]
        for words, regs, expected in cases:
            with self.subTest(expected=expected):
                hart, memory = make_hart(assemble(words))
                self.addCleanup(memory.close)
                for index, value in regs.items():
                    hart.regs[index] = value
                before = (list(hart.regs), bytes(memory.lanes), list(memory.generations))
                stop = hart.run(50)
                self.assertEqual(stop, expected)
                after = (list(hart.regs), bytes(memory.lanes), list(memory.generations))
                self.assertEqual(before, after)
                if expected.cause == CAUSE_INSTRUCTION_ACCESS_FAULT:
                    self.assertEqual((hart.pc, hart.instret, hart.gas), (BASE + RAM, 1, 49))  # el jalr sí se confirmó
                else:
                    self.assertEqual((hart.pc, hart.instret, hart.gas), (CODE, 0, 50))

    def test_csr_counters(self) -> None:
        words = [encode(Kind.ADDI, 1, 0, imm=1), encode(Kind.CSRRS, 2, 0, imm=CSR_INSTRET),
                 encode(Kind.CSRRS, 3, 0, imm=CSR_CYCLE), encode(Kind.CSRRSI, 4, 0, imm=CSR_TIME),
                 encode(Kind.CSRRC, 5, 0, imm=0xC82), encode(Kind.CSRRS, 0, 0, imm=CSR_CYCLE),
                 encode(Kind.CSRRS, 6, 0, imm=0x7C0)]   # H3.1: mstatus ya existe; 0x7C0 (personalizado) no está en el mapa
        hart, stop = self.run_words(words)
        self.assertEqual(stop, Stop("fallo", CAUSE_ILLEGAL_INSTRUCTION, words[-1]))
        self.assertEqual(hart.regs[2:6], [1, 2, 3, 0])
        self.assertEqual(hart.instret, 6)
        # Mitades altas: con instret ≥ 2³² las lecturas bajas envuelven y las altas valen 1.
        words = [encode(Kind.CSRRS, 1, 0, imm=0xC82), encode(Kind.CSRRS, 2, 0, imm=CSR_INSTRET),
                 encode(Kind.CSRRS, 3, 0, imm=0xC80), encode(Kind.CSRRS, 4, 0, imm=0xC81),
                 encode(Kind.CSRRS, 5, 0, imm=CSR_TIME), encode(Kind.EBREAK)]
        hart, memory = make_hart(assemble(words))
        self.addCleanup(memory.close)
        hart.instret = (1 << 32) + 5
        self.assertEqual(hart.run(10).reason, "ebreak")
        self.assertEqual(hart.regs[1:6], [1, 6, 1, 1, 9])
        self.assertEqual(hart.instret, (1 << 32) + 10)
        hart = Rv32Hart(memory, time_divisor=4)
        hart.instret = (1 << 34) + 7
        self.assertEqual(hart.run(10).reason, "ebreak")
        # instreth en 2³⁴+7 → 4; instret en +8 → 8; cycleh en +9 → 4; timeh en +10 → (2³⁴+10)//4 >> 32 = 1;
        # time en +11 → (2³⁴+11)//4 = 2³² + 2 → 2.
        self.assertEqual(hart.regs[1:6], [4, 8, 4, 1, 2])
        with self.assertRaises(ValueError):
            Rv32Hart(memory, time_divisor=0)
        # csrrci con uimm = 0 es una lectura legal; fence y fence.i se ejecutan sin efecto y cuentan.
        words = [encode(Kind.FENCE, 0, 0, imm=0x0FF), encode(Kind.FENCE_I), encode(Kind.CSRRCI, 7, 0, imm=CSR_INSTRET),
                 encode(Kind.FENCE, 0, 0, imm=0x033), encode(Kind.ADDI, 1, 0, imm=1), encode(Kind.EBREAK)]
        hart, stop = self.run_words(words)
        self.assertEqual((stop.reason, hart.regs[7], hart.regs[1], hart.instret), ("ebreak", 2, 1, 5))
        for word in (encode(Kind.CSRRW, 1, 2, imm=CSR_CYCLE), encode(Kind.CSRRS, 1, 2, imm=CSR_CYCLE),
                     encode(Kind.CSRRSI, 1, 3, imm=CSR_INSTRET), encode(Kind.CSRRWI, 1, 0, imm=CSR_CYCLE)):
            hart, stop = self.run_words([word])
            self.assertEqual(stop, Stop("fallo", CAUSE_ILLEGAL_INSTRUCTION, word), hex(word))

    def test_gas_is_exact(self) -> None:
        words = [encode(Kind.ADDI, 1, 1, imm=1)] * 40 + [encode(Kind.EBREAK)]
        hart, stop = self.run_words(words, gas=10)
        self.assertEqual((stop.reason, hart.instret, hart.gas, hart.regs[1], hart.pc), ("gas", 10, 0, 10, CODE + 40))
        self.assertEqual(hart.run(0), Stop("gas"))
        stop = hart.run(1000)
        self.assertEqual((stop.reason, hart.instret, hart.gas, hart.regs[1]), ("ebreak", 40, 1000 - 30 * GAS_PER_INSTRUCTION, 40))

    def test_self_modifying_code_is_seen_immediately(self) -> None:
        # Escribe sobre la instrucción siguiente (misma página) y sobre otra página.
        words = [encode(Kind.SW, 0, 10, 5, imm=8), encode(Kind.ADDI, 0, 0, imm=0),
                 encode(Kind.ADDI, 1, 0, imm=1), encode(Kind.SW, 0, 11, 6, imm=0), encode(Kind.JALR, 0, 11, imm=0)]
        image = assemble(words) + bytes(0x1000 - 4 * len(words)) + assemble([encode(Kind.ADDI, 2, 0, imm=2), encode(Kind.EBREAK)])
        hart, memory = make_hart(image)
        self.addCleanup(memory.close)
        hart.regs[10] = CODE
        hart.regs[5] = encode(Kind.ADDI, 1, 0, imm=9)        # sustituye «addi x1, x0, 1»
        hart.regs[11] = CODE + 0x1000
        hart.regs[6] = encode(Kind.ADDI, 2, 0, imm=7)        # sustituye «addi x2, x0, 2» en la otra página
        stop = hart.run(100)
        self.assertEqual((stop.reason, hart.regs[1], hart.regs[2]), ("ebreak", 9, 7))
        # Segunda ejecución tras reset (caché vacía) con otros parches: mismo mecanismo, otro resultado.
        regs = hart.regs
        hart.reset(CODE)
        self.assertIs(hart.regs, regs)   # reset limpia la misma lista
        hart.regs[10], hart.regs[5], hart.regs[11], hart.regs[6] = CODE, encode(Kind.ADDI, 1, 0, imm=4), CODE + 0x1000, encode(Kind.ADDI, 2, 0, imm=3)
        stop = hart.run(100)
        self.assertEqual((stop.reason, hart.regs[1], hart.regs[2]), ("ebreak", 4, 3))

    def test_overwriting_an_executed_instruction_is_seen_on_the_next_pass(self) -> None:
        # Criterio 6: la instrucción parcheada ya se ejecutó (está en la caché de decodificación) y un
        # salto hacia atrás vuelve a pasar por ella; en la misma página y en otra página, con y sin cortes.
        same_page = [
            encode(Kind.ADDI, 1, 1, imm=1),            # 0: primera pasada +1; segunda pasada, parcheada, +9
            encode(Kind.BNE, 0, 2, 0, imm=8),          # 4: en la segunda pasada x2 ≠ 0 → salta al ebreak
            encode(Kind.JAL, 0, imm=8),                # 8: primera pasada: sigue en 16
            encode(Kind.EBREAK),                       # 12
            encode(Kind.SW, 0, 10, 5, imm=0),          # 16: parchea la palabra 0
            encode(Kind.ADDI, 2, 0, imm=1),            # 20
            encode(Kind.JALR, 0, 11, imm=0),           # 24: vuelve a 0
        ]
        other_page = same_page[:4] + [encode(Kind.JALR, 0, 12, imm=0)]   # 16: salta a la página siguiente
        far = [encode(Kind.SW, 0, 10, 5, imm=0), encode(Kind.ADDI, 2, 0, imm=1), encode(Kind.JALR, 0, 11, imm=0)]
        patch = encode(Kind.ADDI, 1, 1, imm=9)                             # 0x00908093
        self.assertEqual((patch, same_page[0]), (0x00908093, 0x00108093))
        # Variantes del parche de la misma página: sw entera, sb del byte 2, sh alineada y sh desalineada
        # (vía lenta, cruza una palabra): todas dejan la palabra 0x00908093.
        patches = ((encode(Kind.SW, 0, 10, 5, imm=0), patch), (encode(Kind.SB, 0, 10, 5, imm=2), 0x90),
                   (encode(Kind.SH, 0, 10, 5, imm=2), 0x0090), (encode(Kind.SH, 0, 10, 5, imm=1), 0x9080))
        # Confirmadas: misma página 6 + 2; otra página 7 + 2 (el jalr de ida a la otra página).
        images = [(f"misma página, parche {i}", assemble(same_page[:4] + [store] + same_page[5:]), value, 8)
                  for i, (store, value) in enumerate(patches)]
        images.append(("otra página", assemble(other_page) + bytes(0x1000 - 4 * len(other_page)) + assemble(far), patch, 9))
        for name, image, value, instret in images:
            for cuts in (None, 1, 3):
                with self.subTest(name, cuts=cuts):
                    hart, memory = make_hart(image)
                    self.addCleanup(memory.close)
                    hart.regs[10], hart.regs[11], hart.regs[12], hart.regs[5] = CODE, CODE, CODE + 0x1000, value
                    stop = Stop("gas")
                    if cuts is None:
                        stop = hart.run(100)
                    else:
                        while stop.reason == "gas":
                            stop = hart.run(cuts)
                    self.assertEqual((stop.reason, hart.regs[1], hart.regs[2], hart.instret), ("ebreak", 10, 1, instret))
                    self.assertEqual(memory.load32(CODE), patch)

    def test_store_crossing_into_the_next_page_invalidates_its_decoded_code(self) -> None:
        # sh en +0xFFF escribe el byte 0 de una instrucción ya ejecutada de la página 1 (rd 2 → 3).
        page0 = [encode(Kind.JAL, 0, imm=0x1000), encode(Kind.BNE, 0, 4, 0, imm=16), encode(Kind.SH, 0, 10, 5, imm=0x7FF),
                 encode(Kind.ADDI, 4, 0, imm=1), encode(Kind.JAL, 0, imm=-16), encode(Kind.EBREAK)]
        page1 = [encode(Kind.ADDI, 2, 0, imm=2), encode(Kind.JALR, 0, 11, imm=0)]
        self.assertEqual(page1[0], 0x00200113)
        image = assemble(page0) + bytes(0x1000 - 4 * len(page0)) + assemble(page1)
        for cuts in (None, 1, 2):
            with self.subTest(cuts=cuts):
                hart, memory = make_hart(image)
                self.addCleanup(memory.close)
                hart.regs[10], hart.regs[11], hart.regs[5] = CODE + 0x800, CODE + 4, 0x9300
                stop = Stop("gas")
                if cuts is None:
                    stop = hart.run(100)
                else:
                    while stop.reason == "gas":
                        stop = hart.run(cuts)
                self.assertEqual((stop.reason, hart.regs[2], hart.regs[3], hart.regs[4], hart.instret), ("ebreak", 2, 2, 1, 11))
                self.assertEqual(memory.load32(CODE + 0x1000), 0x00200193)
                self.assertEqual(memory.load8u(CODE + 0xFFF), 0)

    def test_fetch_below_the_base_faults_without_executing_the_last_page(self) -> None:
        # Hallazgo bloqueante de la revisión ciega 1: la «página −1» coincidía con el centinela de la caché
        # y el fetch en base − 4 ejecutaba la última palabra de la RAM, además con un resultado distinto
        # según los cortes de gas.
        words = [encode(Kind.LUI, 5, imm=alu.to_signed(BASE)), encode(Kind.ADDI, 5, 5, imm=-4), encode(Kind.JALR, 0, 5, imm=0)]
        image = assemble(words) + bytes(RAM - 4 * len(words) - 4) + assemble([encode(Kind.ADDI, 6, 0, imm=55)])
        expected = Stop("fallo", CAUSE_INSTRUCTION_ACCESS_FAULT, BASE - 4)
        for cuts in (None, [3, 100], [1, 1, 1, 100], [2, 1, 100]):
            with self.subTest(cuts=cuts):
                hart, memory = make_hart(image)
                self.addCleanup(memory.close)
                if cuts is None:
                    stop = hart.run(100)
                else:
                    for chunk in cuts:
                        stop = hart.run(chunk)
                        if stop.reason != "gas":
                            break
                self.assertEqual(stop, expected)
                self.assertEqual((hart.regs[6], hart.pc, hart.instret), (0, BASE - 4, 3))
        for pc in (BASE - 4, BASE - 4096, BASE - 8192, BASE - 4 * 1024 * 1024, 0, 4, BASE + RAM, 0xFFFFFFFC):
            with self.subTest(pc=pc):
                hart, memory = make_hart(image)
                self.addCleanup(memory.close)
                hart.pc = pc
                self.assertEqual(hart.run(10), Stop("fallo", CAUSE_INSTRUCTION_ACCESS_FAULT, pc))
                self.assertEqual((hart.regs[6], hart.pc, hart.instret, hart.gas), (0, pc, 0, 10))

    def test_pc_wraps_at_the_top_of_the_address_space(self) -> None:
        memory = Rv32Memory(4096, base=0xFFFFF000)
        self.addCleanup(memory.close)
        memory.load_bytes(0xFFFFFFF8, assemble([encode(Kind.ADDI, 1, 0, imm=1), encode(Kind.ADDI, 2, 0, imm=2)]))
        hart = Rv32Hart(memory)
        hart.reset(0xFFFFFFF8)
        self.assertEqual(hart.run(10), Stop("fallo", CAUSE_INSTRUCTION_ACCESS_FAULT, 0))
        self.assertEqual((hart.regs[1], hart.regs[2], hart.pc, hart.instret, hart.gas), (1, 2, 0, 2, 8))
        hart.reset(0xFFFFFFFC)
        self.assertEqual(hart.run(1), Stop("gas"))
        self.assertEqual((hart.pc, hart.instret), (0, 1))
        self.assertEqual(hart.run(5), Stop("fallo", CAUSE_INSTRUCTION_ACCESS_FAULT, 0))
        # Enlaces en la última palabra: jal/jalr escriben (pc + 4) & M = 0 y auipc toma el pc sin envolver.
        memory.load_bytes(0xFFFFFFF4, assemble([encode(Kind.EBREAK), encode(Kind.AUIPC, 2, imm=0), encode(Kind.JAL, 1, imm=-8)]))
        hart.reset(0xFFFFFFF8)
        self.assertEqual(hart.run(10), Stop("ebreak", 3, 0))
        self.assertEqual((hart.regs[1], hart.regs[2], hart.pc, hart.instret), (0, 0xFFFFFFF8, 0xFFFFFFF4, 2))
        memory.load_bytes(0xFFFFFFFC, assemble([encode(Kind.JALR, 3, 4, imm=0)]))
        hart.reset(0xFFFFFFFC)
        hart.regs[4] = 0xFFFFFFF4
        self.assertEqual(hart.run(10), Stop("ebreak", 3, 0))
        self.assertEqual((hart.regs[3], hart.pc), (0, 0xFFFFFFF4))

    def test_attach_memory_switches_the_mirrors_and_empties_the_cache(self) -> None:
        hart, memory = make_hart(assemble([encode(Kind.ADDI, 1, 0, imm=1), encode(Kind.EBREAK)]))
        self.addCleanup(memory.close)
        self.assertEqual((hart.run(10).reason, hart.regs[1]), ("ebreak", 1))
        other = Rv32Memory(1 << 14, base=0x40000000)
        self.addCleanup(other.close)
        other.load_bytes(0x40000000, assemble([encode(Kind.ADDI, 1, 0, imm=2), encode(Kind.SW, 0, 0, 1, imm=0), encode(Kind.EBREAK)]))
        hart.attach_memory(other)
        hart.reset()
        self.assertEqual((hart.pc, hart.base, hart.size, hart.words_u is other.words_u, hart.gens is other.generations),
                         (0x40000000, 0x40000000, 1 << 14, True, True))
        self.assertEqual(hart.run(10), Stop("fallo", CAUSE_STORE_ACCESS_FAULT, 0))
        self.assertEqual((hart.regs[1], hart.instret), (2, 1))
        self.assertEqual(memory.load32(BASE), encode(Kind.ADDI, 1, 0, imm=1))   # la memoria antigua no se tocó

    def test_run_rejects_misaligned_pc_and_negative_gas(self) -> None:
        hart, memory = make_hart(assemble([encode(Kind.EBREAK)]))
        self.addCleanup(memory.close)
        hart.pc = CODE + 2
        self.assertEqual(hart.run(10), Stop("fallo", CAUSE_INSTRUCTION_MISALIGNED, CODE + 2))
        self.assertEqual((hart.gas, hart.pc, hart.instret), (10, CODE + 2, 0))   # sin cobrar, como las demás paradas
        self.assertEqual(hart.run(0), Stop("gas"))
        self.assertEqual(hart.gas, 0)
        with self.assertRaises(ValueError):
            hart.run(-1)


class CodeLineMapInTheHart(unittest.TestCase):
    """Refinamiento de P1.6: solo los stores sobre líneas con código decodificado suben la generación."""

    def run_with_cuts(self, hart: Rv32Hart, cuts: int | None, gas: int) -> Stop:
        if cuts is None:
            return hart.run(gas)
        stop = Stop("gas")
        while stop.reason == "gas":
            stop = hart.run(cuts)
        return stop

    def test_data_stores_in_the_code_page_keep_the_decode_cache(self) -> None:
        # Bucle de 20 vueltas que escribe sw/sb/sh sobre datos de su propia página (línea 0x800, sin código):
        # la generación no cambia, la lista de decodificación es la misma y el resultado es el de referencia.
        words = [encode(Kind.ADDI, 1, 1, imm=1), encode(Kind.SW, 0, 10, 1, imm=0), encode(Kind.SB, 0, 10, 1, imm=4),
                 encode(Kind.SH, 0, 10, 1, imm=6), encode(Kind.ADDI, 20, 20, imm=-1), encode(Kind.BNE, 0, 20, 0, imm=-20),
                 encode(Kind.EBREAK)]
        image = assemble(words)
        reference = Reference(image)
        reference.regs[10], reference.regs[20] = CODE + 0x800, 20
        expected = reference.run(1000)
        self.assertEqual(expected.reason, "ebreak")
        for cuts in (None, 1, 7):
            with self.subTest(cuts=cuts):
                hart, memory = make_hart(image)
                self.addCleanup(memory.close)
                hart.regs[10], hart.regs[20] = CODE + 0x800, 20
                self.assertEqual(hart.run(3), Stop("gas"))            # las tres primeras ya están decodificadas
                entry = hart.decode_cache[0]
                stop = self.run_with_cuts(hart, cuts, 1000)
                self.assertEqual(stop, expected)
                self.assertEqual((hart.regs, hart.pc, hart.instret), (reference.regs, reference.pc, reference.count))
                self.assertEqual(bytes(memory.lanes), bytes(reference.mem))
                self.assertEqual(memory.generation(0), 1)             # solo la carga de la imagen
                self.assertIs(hart.decode_cache[0], entry)            # la misma entrada: nada se descartó
                self.assertEqual(memory.written_pages(), [0])
                self.assertEqual([i for i, c in enumerate(memory.code) if c], [0, 1])   # 7 instrucciones: 2 líneas

    def test_store_into_a_code_line_is_conservative_and_lazy_decoding_sees_later_words(self) -> None:
        patch = encode(Kind.ADDI, 3, 0, imm=7)
        # (a) sw sobre la palabra aún no ejecutada (0xC, un ebreak) de una línea con instrucciones ya decodificadas:
        # sube la generación (sobreaproximación) y la palabra nueva se ejecuta.
        words = [encode(Kind.ADDI, 1, 0, imm=1), encode(Kind.SW, 0, 10, 5, imm=0xC), encode(Kind.ADDI, 2, 0, imm=2),
                 encode(Kind.EBREAK), encode(Kind.EBREAK)]
        for cuts in (None, 1, 2):
            with self.subTest("misma línea", cuts=cuts):
                hart, memory = make_hart(assemble(words))
                self.addCleanup(memory.close)
                hart.regs[10], hart.regs[5] = CODE, patch
                stop = self.run_with_cuts(hart, cuts, 100)
                self.assertEqual((stop.reason, hart.regs[1], hart.regs[2], hart.regs[3], hart.pc, hart.instret),
                                 ("ebreak", 1, 2, 7, CODE + 0x10, 4))
                self.assertEqual(memory.generation(0), 2)   # la carga y el store sobre una línea de código
        # (b) sw sobre una línea sin ninguna palabra decodificada (0x10..0x1F) que se ejecuta después: la
        # decodificación es perezosa y ve la palabra nueva sin que la generación cambie.
        words = [encode(Kind.ADDI, 1, 0, imm=1), encode(Kind.SW, 0, 10, 5, imm=0x14), encode(Kind.ADDI, 2, 0, imm=2),
                 encode(Kind.JAL, 0, imm=8), encode(Kind.EBREAK), encode(Kind.EBREAK), encode(Kind.EBREAK)]
        for cuts in (None, 1, 2):
            with self.subTest("línea sin decodificar", cuts=cuts):
                hart, memory = make_hart(assemble(words))
                self.addCleanup(memory.close)
                hart.regs[10], hart.regs[5] = CODE, patch
                stop = self.run_with_cuts(hart, cuts, 100)
                self.assertEqual((stop.reason, hart.regs[3], hart.pc, hart.instret), ("ebreak", 7, CODE + 0x18, 5))
                self.assertEqual(memory.generation(0), 1)   # solo la carga: la línea 0x10 no tenía código
                self.assertEqual([i for i, c in enumerate(memory.code) if c], [0, 1])

    def test_halfword_store_crossing_from_a_data_line_into_a_code_line_invalidates(self) -> None:
        # La instrucción de 0x10 («addi x1, x1, 1») ya se ejecutó; un sh en 0xF (vía lenta: cruza línea) escribe
        # 0x00 en el dato 0xF y 0x13 en el byte 0 de la instrucción, que pasa a «addi x0, x1, 1».
        words = [encode(Kind.JAL, 0, imm=0x10), 0, 0, 0,
                 encode(Kind.ADDI, 1, 1, imm=1),            # 0x10
                 encode(Kind.BNE, 0, 2, 0, imm=0x18),       # 0x14 → 0x2C
                 encode(Kind.SH, 0, 10, 5, imm=0xF),        # 0x18
                 encode(Kind.ADDI, 2, 0, imm=1),            # 0x1C
                 encode(Kind.JAL, 0, imm=-16),              # 0x20 → 0x10
                 encode(Kind.EBREAK), encode(Kind.EBREAK),  # 0x24, 0x28
                 encode(Kind.EBREAK)]                       # 0x2C
        self.assertEqual(words[4], 0x00108093)
        for cuts in (None, 1, 3):
            with self.subTest(cuts=cuts):
                hart, memory = make_hart(assemble(words))
                self.addCleanup(memory.close)
                hart.regs[10], hart.regs[5] = CODE, 0x1300
                stop = self.run_with_cuts(hart, cuts, 100)
                self.assertEqual((stop.reason, hart.regs[1], hart.regs[2], hart.pc, hart.instret), ("ebreak", 1, 1, CODE + 0x2C, 8))
                self.assertEqual(memory.load32(CODE + 0x10), 0x00108013)
                self.assertEqual((memory.load8u(CODE + 0xF), memory.generation(0)), (0, 2))

    def test_host_writes_invalidate_decoded_code(self) -> None:
        hart, memory = make_hart(assemble([encode(Kind.ADDI, 1, 0, imm=1), encode(Kind.EBREAK)]))
        self.addCleanup(memory.close)
        self.assertEqual((hart.run(10).reason, hart.regs[1]), ("ebreak", 1))
        memory.load_bytes(CODE, assemble([encode(Kind.ADDI, 1, 0, imm=2)]))   # el host reescribe la instrucción ejecutada
        self.assertEqual(memory.generation(0), 2)
        hart.pc = CODE
        self.assertEqual((hart.run(10).reason, hart.regs[1]), ("ebreak", 2))
        memory.fill([encode(Kind.ADDI, 1, 0, imm=3)], CODE)
        hart.pc = CODE
        self.assertEqual((hart.run(10).reason, hart.regs[1], memory.generation(0)), ("ebreak", 3, 3))

    def test_a_store_over_the_instruction_being_decoded_for_the_first_time_is_seen(self) -> None:
        # Reproductor del oráculo matemático (mata el mutante «marcar después de ejecutar»): el sw de 0x20 se
        # reescribe a sí mismo en su primera ejecución como «addi x6, x6, 100»; las dos vueltas siguientes deben
        # ejecutar la palabra nueva. Resultado calculado a mano: x5 = 3, x6 = 200, x24 = 0, instret = 20.
        patch = encode(Kind.ADDI, 6, 6, imm=100)
        self.assertEqual(patch, 0x06430313)
        words = [encode(Kind.ADDI, 24, 0, imm=3), encode(Kind.AUIPC, 10, imm=0), encode(Kind.ADDI, 10, 10, imm=0x1C),
                 encode(Kind.LUI, 25, imm=0x06430000), encode(Kind.ADDI, 25, 25, imm=0x313),
                 encode(Kind.ADDI, 0, 0, imm=0), encode(Kind.ADDI, 0, 0, imm=0), encode(Kind.ADDI, 0, 0, imm=0),
                 encode(Kind.SW, 0, 10, 25, imm=0),            # 0x20: se sobrescribe a sí mismo
                 encode(Kind.ADDI, 5, 5, imm=1),               # 0x24
                 encode(Kind.ADDI, 24, 24, imm=-1),            # 0x28
                 encode(Kind.BNE, 0, 24, 0, imm=-12),          # 0x2C → 0x20
                 encode(Kind.EBREAK)]
        for accelerate, cuts in ((False, None), (False, 1), (False, 3), (True, None), (True, 3), (True, 7)):
            with self.subTest(accelerate=accelerate, cuts=cuts):
                hart, memory = make_hart(assemble(words))
                self.addCleanup(memory.close)
                hart.accelerate = accelerate
                hart.warmup = 1
                stop = self.run_with_cuts(hart, cuts, 100)
                self.assertEqual((stop.reason, hart.regs[5], hart.regs[6], hart.regs[24], hart.instret), ("ebreak", 3, 200, 0, 20))
                self.assertEqual(memory.load32(CODE + 0x20), patch)

    def test_misaligned_stores_crossing_into_fresh_pages_mark_them_written(self) -> None:
        # Hallazgo de la ciega 1 y del oráculo: un sw desalineado en 0x1FFE y un sh en 0x4FFF tocan páginas
        # vírgenes (2 y 5) que deben quedar escritas, o el snapshot perdería sus bytes.
        words = [encode(Kind.SW, 0, 10, 5, imm=0), encode(Kind.SH, 0, 11, 5, imm=0), encode(Kind.EBREAK)]
        for accelerate in (False, True):
            with self.subTest(accelerate=accelerate):
                hart, memory = make_hart(assemble(words))
                self.addCleanup(memory.close)
                hart.accelerate = accelerate
                hart.warmup = 1
                hart.regs[10], hart.regs[11], hart.regs[5] = CODE + 0x1FFE, CODE + 0x4FFF, 0x11223344
                self.assertEqual(hart.run(10).reason, "ebreak")
                self.assertEqual(memory.written_pages(), [0, 1, 2, 4, 5])
                self.assertEqual((memory.load32(CODE + 0x1FFE), memory.load16u(CODE + 0x4FFF)), (0x11223344, 0x3344))
                restored = Rv32Memory(RAM)
                self.addCleanup(restored.close)
                restored.restore_pages(memory.snapshot_pages())
                self.assertEqual(bytes(restored.lanes), bytes(memory.lanes))

    def test_two_harts_sharing_a_memory_stay_coherent(self) -> None:
        # Hallazgo de las dos revisiones: crear (o reiniciar) un segundo hart sobre la misma memoria vacía el
        # mapa de código; como clear_code sube la generación de las páginas marcadas, las cachés del primero
        # dejan de coincidir y el parche que escribe el segundo se ve.
        memory = Rv32Memory(RAM)
        self.addCleanup(memory.close)
        memory.load_bytes(CODE, assemble([encode(Kind.ADDI, 1, 1, imm=1), encode(Kind.EBREAK)]))
        memory.load_bytes(CODE + 0x1000, assemble([encode(Kind.SW, 0, 10, 5, imm=0), encode(Kind.EBREAK)]))
        for accelerate in (False, True):
            with self.subTest(accelerate=accelerate):
                first = Rv32Hart(memory)
                first.accelerate = accelerate
                first.warmup = 1
                first.reset(CODE)
                memory.load_bytes(CODE, assemble([encode(Kind.ADDI, 1, 1, imm=1)]))
                self.assertEqual((first.run(10).reason, first.regs[1]), ("ebreak", 1))
                before = memory.generation(0)
                second = Rv32Hart(memory)                        # vacía el mapa: la página 0 sube de generación
                self.assertEqual(memory.generation(0), before + 1)
                second.pc = CODE + 0x1000
                second.regs[10], second.regs[5] = CODE, encode(Kind.ADDI, 1, 1, imm=5)
                self.assertEqual(second.run(10).reason, "ebreak")
                first.pc = CODE
                first.regs[1] = 0
                self.assertEqual((first.run(10).reason, first.regs[1]), ("ebreak", 5))
                # Y al revés: el primero vuelve a cachear, el segundo se reinicia y parchea otra vez.
                second.reset(CODE + 0x1000)
                second.regs[10], second.regs[5] = CODE, encode(Kind.ADDI, 1, 1, imm=9)
                self.assertEqual(second.run(10).reason, "ebreak")
                first.pc = CODE
                first.regs[1] = 0
                self.assertEqual((first.run(10).reason, first.regs[1]), ("ebreak", 9))

    def test_reset_and_attach_memory_clear_the_code_map(self) -> None:
        hart, memory = make_hart(assemble([encode(Kind.ADDI, 1, 0, imm=1), encode(Kind.EBREAK)]))
        self.addCleanup(memory.close)
        self.assertEqual(hart.run(10).reason, "ebreak")
        self.assertEqual([i for i, c in enumerate(memory.code) if c], [0])
        hart.reset()
        self.assertFalse(any(memory.code))
        self.assertEqual(memory.generation(0), 2)                          # la carga y el vaciado del mapa
        self.assertEqual(hart.run(10).reason, "ebreak")                   # vuelve a marcar al decodificar
        self.assertEqual([i for i, c in enumerate(memory.code) if c], [0])
        other = Rv32Memory(RAM)
        self.addCleanup(other.close)
        other.mark_code(0x40, 4)
        hart.attach_memory(other)
        self.assertFalse(any(other.code))                                  # attach_memory vacía el mapa de la nueva
        self.assertEqual([i for i, c in enumerate(memory.code) if c], [0])   # y no toca el de la antigua
        self.assertIs(hart.code, other.code)
        self.assertIs(hart.written, other.written)


class CoreMarkOnTheHart(unittest.TestCase):
    def test_coremark_rv32i_runs_to_poweroff_with_the_e1_count(self) -> None:
        self.assertTrue(COREMARK.is_file(), "falta el binario versionado de CoreMark (benchmarks/rv32_arranque/bin)")
        machine = Machine(ram_bytes=1 << 20)
        self.addCleanup(machine.close)
        machine.load_flat(COREMARK.read_bytes())
        stop = machine.run(20_000_000)
        self.assertEqual(stop, Stop("apagado"))
        self.assertEqual(machine.hart.instret, COREMARK_INSTRUCTIONS)
        text = machine.uart_text
        for line in ("[0]crclist       : 0xe714", "[0]crcmatrix     : 0x1fd7", "[0]crcstate      : 0x8e3a",
                     "[0]crcfinal      : 0xfcaf", "Total ticks      : 8458371"):
            self.assertIn(line, text)

    def test_coremark_rv32im_runs_to_poweroff_with_the_e1_count_in_both_modes(self) -> None:
        # H3.2: E1 contó 333 625,3 instrucciones por iteración y 23 855 fijas (diferencia entre 10 y 20 iteraciones); el
        # hart cuenta además el store a SYSCON que apaga: 10 × 333 625,3 + 23 855 + 1.
        binary = COREMARK.parent / "coremark_rv32im_zicsr_it10.bin"
        hashes = set()
        for accelerate in (False, True):
            machine = Machine(ram_bytes=1 << 20)
            self.addCleanup(machine.close)
            machine.load_flat(binary.read_bytes())
            machine.hart.accelerate = accelerate
            self.assertEqual(machine.run(20_000_000), Stop("apagado"))
            self.assertEqual(machine.hart.instret, 3_336_253 + 23_855 + 1)
            text = machine.uart_text
            for line in ("[0]crclist       : 0xe714", "[0]crcmatrix     : 0x1fd7", "[0]crcstate      : 0x8e3a",
                         "[0]crcfinal      : 0xfcaf", "-march=rv32im_zicsr"):
                self.assertIn(line, text)
            hashes.add(machine.state_hash())
        self.assertEqual(len(hashes), 1)


if __name__ == "__main__":
    unittest.main()
