"""Anclas del compilador de bloques (H1): equivalencia con el intérprete, salidas lentas, horizonte, CoreMark."""

from __future__ import annotations

import contextlib
import random
import unittest
from pathlib import Path
from unittest import mock

from cpu_digital import rv32_alu as alu
from cpu_digital import rv32_blocks
from cpu_digital.rv32_blocks import MAX_BLOCK, compile_block
from cpu_digital.rv32_decode import decode
from cpu_digital.rv32_hart import MAX_BLOCKS, MAX_RECOMPILATIONS, Rv32Hart, Stop
from cpu_digital.rv32_isa import CAUSE_INSTRUCTION_ACCESS_FAULT, CAUSE_INSTRUCTION_MISALIGNED, CAUSE_LOAD_ACCESS_FAULT, Kind, encode
from cpu_digital.rv32_mem import LINE_MASK, LINE_SHIFT, Rv32Memory
from cpu_digital.rv32_platform import UART_BASE, Machine
from tests.test_rv32_hart import BASE, CODE, DATA, RAM, Reference, assemble, random_program

ROOT = Path(__file__).resolve().parents[1]
COREMARK = ROOT / "benchmarks" / "rv32_arranque" / "bin" / "coremark_rv32i_zicsr_it10.bin"
COREMARK_INSTRUCTIONS = 8_505_936 + 1
EBREAK = encode(Kind.EBREAK)


def state(hart: Rv32Hart, memory: Rv32Memory) -> tuple:
    return (list(hart.regs), hart.pc, hart.instret, bytes(memory.lanes))


@contextlib.contextmanager
def recording_blocks():
    """Envuelve ``compile_block`` para recordar los bytes de cada bloque al compilarlo (comprobación de I)."""
    recorded: dict[int, tuple[object, bytes]] = {}
    original = rv32_blocks.compile_block

    def wrapper(hart, pc, gas_per_instruction=1, keep_source=True):
        block = original(hart, pc, gas_per_instruction, keep_source)
        if block is not None:
            offset = pc - hart.base
            recorded[id(block)] = (block, bytes(hart.memory.lanes[offset:offset + 4 * block.length]))
        return block

    with mock.patch.object(rv32_blocks, "compile_block", wrapper):
        yield recorded


def check_invariants(hart: Rv32Hart, memory: Rv32Memory, recorded: dict[int, tuple[object, bytes]]) -> list[str]:
    """Invariantes I y W de la spec de P1.6 sobre el estado actual: toda palabra cacheada con generación vigente
    (decodificación o bloque) tiene su línea marcada y coincide con la memoria; byte ≠ 0 ⇒ página escrita."""
    errors: list[str] = []
    gens, code, words = memory.generations, memory.code, memory.words_u
    for page, (generation, entries) in hart.decode_cache.items():
        if gens[page] != generation:
            continue
        for index, decoded in enumerate(entries):
            if decoded is None:
                continue
            offset = (page << 12) + (index << 2)
            if not code[offset >> LINE_SHIFT]:
                errors.append(f"decodificación sin marca en {offset:#x}")
            if decode(words[offset >> 2]) != decoded:
                errors.append(f"decodificación obsoleta en {offset:#x}")
    for pc, block in hart.blocks.items():
        if block is None or any(gens[p] != g for p, g in zip(block.pages, block.generations)):
            continue
        offset = pc - hart.base
        if any(not code[(offset + 4 * i) >> LINE_SHIFT] for i in range(block.length)):
            errors.append(f"bloque sin marca en {pc:#x}")
        record = recorded.get(id(block))
        if record is None or record[0] is not block:
            errors.append(f"bloque sin registro en {pc:#x}")
        elif record[1] != bytes(memory.lanes[offset:offset + 4 * block.length]):
            errors.append(f"bloque obsoleto en {pc:#x}")
    for page in range(len(memory.written)):
        if not memory.written[page] and any(memory.lanes[page << 12:(page + 1) << 12]):
            errors.append(f"bytes distintos de cero en la página {page} sin marcar como escrita")
    return errors


def snapshot_round_trips(memory: Rv32Memory) -> bool:
    restored = Rv32Memory(memory.size)
    try:
        restored.restore_pages(memory.snapshot_pages())
        return bytes(restored.lanes) == bytes(memory.lanes)
    finally:
        restored.close()


def run_modes(image: bytes, gas: int, chunks: random.Random | None = None, horizon: bool = True) -> list[tuple]:
    """Estado final con intérprete puro y con bloques (y gas restante, parada, instrucciones en bloques y
    generación de la página 0); comprueba los invariantes I y W y la ida y vuelta del snapshot."""
    results = []
    for accelerate in (False, True):
        memory = Rv32Memory(RAM)
        memory.load_bytes(BASE, image)
        hart = Rv32Hart(memory)
        hart.accelerate = accelerate
        hart.horizon = horizon
        hart.warmup = 1
        with recording_blocks() as recorded:
            if chunks is None:
                stop = hart.run(gas)
                remaining = hart.gas
            else:
                # Cortes de gas aleatorios (solo con horizonte: sin él, el gas no es exacto por diseño).
                stop = Stop("gas")
                remaining = gas
                cuts = 0
                while remaining > 0 and stop.reason == "gas":
                    chunk = min(remaining, chunks.randrange(1, 25))
                    stop = hart.run(chunk)
                    remaining -= chunk - hart.gas
                    cuts += 1
                    if cuts % 4 == 0:
                        errors = check_invariants(hart, memory, recorded)
                        if errors:
                            raise AssertionError(errors)
            errors = check_invariants(hart, memory, recorded)
        if errors or not snapshot_round_trips(memory):
            raise AssertionError(errors or ["el snapshot no reproduce la memoria"])
        results.append((state(hart, memory), remaining, stop, hart.block_instructions, memory.generation(0)))
        memory.close()
    return results


def hostile_layout(words: list[int], rng: random.Random, base_offset: int, span: int) -> list[int]:
    """Apunta x10 y x11 a ``CODE + base_offset`` y redirige los stores con esas bases a ``[0, span)`` bytes de
    ahí: así los programas aleatorios escriben de verdad sobre el código (y sus líneas) en vez de repartir los
    desplazamientos por 2 KiB de datos."""
    words = list(words)
    words[0] = encode(Kind.LUI, 10, imm=alu.to_signed(CODE & 0xFFFFF000))
    words[1] = encode(Kind.ADDI, 10, 10, imm=base_offset)
    words[2] = encode(Kind.LUI, 11, imm=alu.to_signed(CODE & 0xFFFFF000))
    words[3] = encode(Kind.ADDI, 11, 11, imm=base_offset)
    for index in range(4, len(words)):
        kind, _, rs1, rs2, _, _ = decode(words[index])
        if kind in (Kind.SB, Kind.SH, Kind.SW) and rs1 in (10, 11):
            words[index] = encode(Kind(kind), 0, rs1, rs2, imm=rng.randrange(0, span))
    return words


def loop_program(rng: random.Random, body_length: int, iterations: int) -> list[int]:
    """Cuerpo aleatorio sin ebreak repetido ``iterations`` veces con un contador en x20."""
    body = random_program(rng, body_length)[:-1]  # quita el ebreak final
    prologue = body[:4]
    inner = [word for word in body[4:] if (word & 0x7F) not in (0x6F, 0x67)]  # sin jal/jalr en el cuerpo
    words = prologue + [encode(Kind.ADDI, 20, 0, imm=iterations)]
    loop_start = len(words)
    words += inner
    words.append(encode(Kind.ADDI, 20, 20, imm=-1))
    words.append(encode(Kind.BNE, 0, 20, 0, imm=-4 * (len(words) - loop_start)))
    words.append(encode(Kind.EBREAK))
    return words


class BlocksEqualInterpreter(unittest.TestCase):
    def test_random_programs_and_loops(self) -> None:
        rng = random.Random(404)
        accelerated_total = 0
        code_line_hits = {"página": 0, "línea": 0}
        for index in range(300):
            if index % 3 == 0:
                words = loop_program(rng, rng.randrange(12, 60), rng.randrange(2, 9))
            else:
                words = random_program(rng, rng.randrange(20, 200))
            layout = "datos"
            if index % 5 == 4:
                # Stores sobre el propio programa (desde 0x40 hasta su final): código automodificable al azar, que
                # el intérprete y los bloques deben ver igual.
                layout = "página"
                words = hostile_layout(words, rng, 0x40, max(64, 4 * len(words) - 0x40))
            elif index % 7 == 3:
                # Stores sobre las últimas líneas de código y los 64 bytes de datos contiguos (fin − 64 … fin + 64):
                # la frontera línea/código que distingue el refinamiento de P1.6.
                layout = "línea"
                words = hostile_layout(words, rng, 4 * len(words) - 64, 128)
            image = assemble(words)
            gas = rng.choice((600, 2000, rng.randrange(5, 300)))
            with self.subTest(program=index, gas=gas):
                plain, fast = run_modes(image, gas, chunks=rng if index % 2 else None)
                self.assertEqual(fast[0], plain[0])
                self.assertEqual(fast[1], plain[1])
                self.assertEqual(fast[2], plain[2])
                accelerated_total += fast[3]
                if layout != "datos" and fast[4] > 1:   # algún store subió la generación de la página del código
                    code_line_hits[layout] += 1
        self.assertGreater(accelerated_total, 10_000)
        # Las dos disposiciones hostiles ejercitan de verdad stores sobre líneas de código ya decodificadas
        # (hallazgo de la revisión ciega 1: con los desplazamientos originales no caía ninguno).
        self.assertGreater(code_line_hits["página"], 5, code_line_hits)
        self.assertGreater(code_line_hits["línea"], 5, code_line_hits)

    def test_without_horizon_only_the_gas_differs(self) -> None:
        rng = random.Random(7)
        overshoots = 0
        for index in range(40):
            words = loop_program(rng, 30, 6)
            image = assemble(words)
            plain, fast = run_modes(image, 50, horizon=False)
            with self.subTest(program=index):
                if plain[2].reason == "gas":
                    # El intérprete para en la instrucción 50 exacta; sin horizonte el bloque puede rebasarla.
                    self.assertEqual(plain[0][2], 50)
                    self.assertGreaterEqual(fast[0][2], 50)
                    self.assertLessEqual(fast[1], 0)
                    overshoots += fast[0][2] > 50
                else:
                    self.assertEqual(fast[0], plain[0])
                    self.assertEqual(fast[2], plain[2])
        self.assertGreater(overshoots, 0)


class SlowExits(unittest.TestCase):
    def make(self, words: list[int], ram: int = 1 << 20) -> Machine:
        machine = Machine(ram_bytes=ram)
        self.addCleanup(machine.close)
        machine.load_flat(assemble(words))
        machine.hart.accelerate = True
        machine.hart.warmup = 1
        return machine

    def test_mmio_load_and_store_leave_the_block_and_the_interpreter_does_the_io(self) -> None:
        words = [
            encode(Kind.LUI, 5, imm=alu.to_signed(UART_BASE)), encode(Kind.ADDI, 6, 0, imm=65),
            encode(Kind.ADDI, 7, 0, imm=1), encode(Kind.SW, 0, 5, 6, imm=0),  # «A» por el UART: salida lenta
            encode(Kind.ADDI, 7, 7, imm=1), encode(Kind.LBU, 8, 5, imm=5),      # LSR: salida lenta
            encode(Kind.ADDI, 7, 7, imm=1), encode(Kind.EBREAK),
        ]
        machine = self.make(words)
        stop = machine.run(100)
        hart = machine.hart
        self.assertEqual(stop.reason, "ebreak")
        self.assertEqual((machine.uart_text, hart.regs[7], hart.regs[8], hart.instret), ("A", 3, 0x60, 7))
        self.assertEqual(hart.compilations, 1)
        self.assertEqual(hart.block_instructions, 3)  # lui, addi, addi; el sw sale al intérprete y la cola se interpreta

    def test_misaligned_access_out_of_range_and_jalr(self) -> None:
        words = [encode(Kind.ADDI, 6, 6, imm=1), encode(Kind.LW, 7, 5, imm=0), encode(Kind.ADDI, 6, 6, imm=1),
                 encode(Kind.EBREAK)]
        machine = self.make(words)
        hart = machine.hart
        hart.regs[5] = DATA + 1
        machine.memory.store32(DATA, 0x11223344)
        machine.memory.store32(DATA + 4, 0x55667788)
        stop = machine.run(100)
        self.assertEqual((stop.reason, hart.regs[6], hart.regs[7]), ("ebreak", 2, 0x88112233))
        machine = self.make(words)
        hart = machine.hart
        hart.regs[5] = BASE + (1 << 20)
        stop = machine.run(100)
        self.assertEqual(stop, Stop("fallo", CAUSE_LOAD_ACCESS_FAULT, BASE + (1 << 20)))
        self.assertEqual((hart.regs[6], hart.pc, hart.instret, hart.gas), (1, CODE + 4, 1, 99))
        words = [encode(Kind.ADDI, 6, 6, imm=1), encode(Kind.JALR, 1, 5, imm=2), encode(Kind.EBREAK)]
        machine = self.make(words)
        hart = machine.hart
        hart.regs[5] = CODE
        stop = machine.run(100)
        self.assertEqual(stop, Stop("fallo", CAUSE_INSTRUCTION_MISALIGNED, CODE + 2))
        self.assertEqual((hart.regs[6], hart.regs[1], hart.pc, hart.instret), (1, 0, CODE + 4, 1))
        # jalr con bit 0 en el destino: el bloque lo descarta y sigue dentro del bloque (§2.5).
        words = [encode(Kind.ADDI, 6, 6, imm=1), encode(Kind.JALR, 1, 5, imm=1), encode(Kind.EBREAK)]
        machine = self.make(words)
        hart = machine.hart
        hart.regs[5] = CODE + 8
        self.assertEqual(machine.run(100), Stop("ebreak", 3, 0))
        self.assertEqual((hart.regs[6], hart.regs[1], hart.pc, hart.instret, hart.block_instructions), (1, CODE + 8, CODE + 8, 2, 2))

    def test_wide_loads_at_the_end_of_the_ram_leave_the_block(self) -> None:
        # Las guardas son «o <= size − ancho»: lh/lhu en el último byte y lw en los tres últimos salen al
        # intérprete, que falla con la dirección; lb en el último byte es válida.
        for kind, back, expected in ((Kind.LH, 1, "fallo"), (Kind.LHU, 1, "fallo"), (Kind.LW, 2, "fallo"), (Kind.LW, 3, "fallo"),
                                     (Kind.LW, 1, "fallo"), (Kind.LB, 1, "ebreak"), (Kind.LBU, 1, "ebreak"), (Kind.LH, 2, "ebreak"),
                                     (Kind.LW, 4, "ebreak"), (Kind.LB, 0, "fallo"), (Kind.LBU, 0, "fallo"), (Kind.LH, 0, "fallo"),
                                     (Kind.LW, 0, "fallo")):
            with self.subTest(kind=kind.name, back=back):
                words = [encode(Kind.ADDI, 6, 6, imm=1), encode(kind, 7, 5, imm=0), encode(Kind.ADDI, 6, 6, imm=1), encode(Kind.EBREAK)]
                machine = self.make(words, ram=1 << 16)
                hart = machine.hart
                address = BASE + (1 << 16) - back
                hart.regs[5] = address
                machine.memory.store8(BASE + (1 << 16) - 1, 0x80)
                stop = machine.run(100)
                self.assertEqual(stop.reason, expected)
                if expected == "fallo":
                    self.assertEqual(stop, Stop("fallo", CAUSE_LOAD_ACCESS_FAULT, address))
                    self.assertEqual((hart.regs[6], hart.regs[7], hart.pc, hart.instret), (1, 0, CODE + 4, 1))
                else:
                    self.assertEqual((hart.regs[6], hart.instret), (2, 3))
                    if kind == Kind.LB:
                        self.assertEqual(hart.regs[7], 0xFFFFFF80)
                    elif kind == Kind.LBU:
                        self.assertEqual(hart.regs[7], 0x80)
        # Stores en o == size (justo tras la RAM) y en el último hueco válido.
        for kind, back, expected in ((Kind.SB, 0, "fallo"), (Kind.SH, 0, "fallo"), (Kind.SW, 0, "fallo"), (Kind.SH, 1, "fallo"),
                                     (Kind.SW, 2, "fallo"), (Kind.SB, 1, "ebreak"), (Kind.SH, 2, "ebreak"), (Kind.SW, 4, "ebreak")):
            with self.subTest(kind=kind.name, back=back):
                words = [encode(Kind.ADDI, 6, 6, imm=1), encode(kind, 0, 5, 7, imm=0), encode(Kind.ADDI, 6, 6, imm=1), encode(Kind.EBREAK)]
                machine = self.make(words, ram=1 << 16)
                hart = machine.hart
                address = BASE + (1 << 16) - back
                hart.regs[5], hart.regs[7] = address, 0x11223344
                stop = machine.run(100)
                if expected == "fallo":
                    self.assertEqual(stop, Stop("fallo", 7, address))
                    self.assertEqual((hart.regs[6], hart.pc, hart.instret), (1, CODE + 4, 1))
                else:
                    self.assertEqual((stop.reason, hart.regs[6], hart.instret), ("ebreak", 2, 3))
                    self.assertEqual(machine.memory.load8u(address), 0x44)
        # Umbrales exactos de la extensión de signo dentro de un bloque.
        for kind, value, expected in ((Kind.LH, 0x7FFF, 0x7FFF), (Kind.LH, 0x8000, 0xFFFF8000), (Kind.LH, 0xFFFF, 0xFFFFFFFF),
                                      (Kind.LHU, 0x8000, 0x8000), (Kind.LB, 0x7F, 0x7F), (Kind.LB, 0x80, 0xFFFFFF80),
                                      (Kind.LB, 0xFF, 0xFFFFFFFF), (Kind.LBU, 0x80, 0x80)):
            with self.subTest(kind=kind.name, value=hex(value)):
                machine = self.make([encode(Kind.ADDI, 6, 6, imm=1), encode(kind, 7, 5, imm=0), encode(Kind.EBREAK)])
                machine.hart.regs[5] = DATA
                machine.memory.store32(DATA, value)
                self.assertEqual(machine.run(100).reason, "ebreak")
                self.assertEqual((machine.hart.regs[7], machine.hart.block_instructions), (expected, 2))

    def test_constant_misaligned_branch_targets_are_left_to_the_interpreter(self) -> None:
        # Un salto con destino constante desalineado no entra en el bloque: el intérprete produce el fallo
        # y el estado coincide con el del intérprete puro.
        for word in (encode(Kind.BEQ, 0, 0, 0, imm=6), encode(Kind.JAL, 0, imm=6), encode(Kind.BNE, 0, 6, 0, imm=-6)):
            with self.subTest(word=hex(word)):
                words = [encode(Kind.ADDI, 6, 6, imm=1), word, encode(Kind.EBREAK)]
                machine = self.make(words)
                hart = machine.hart
                memory = machine.memory
                block = compile_block(hart, CODE)
                self.assertEqual(block.length, 1)
                stop = machine.run(100)
                target = (CODE + 4 + 6) if word != words[1] or (word >> 31) == 0 else CODE + 4 - 6
                self.assertEqual(stop, Stop("fallo", CAUSE_INSTRUCTION_MISALIGNED, target))
                self.assertEqual((hart.regs[6], hart.pc, hart.instret, hart.block_instructions), (1, CODE + 4, 1, 1))
                plain, fast = run_modes(memory.read_bytes(BASE, 12), 100)
                self.assertEqual(fast[:3], plain[:3])


class SelfModifyingCode(unittest.TestCase):
    def test_store_into_own_page_exits_and_recompiles(self) -> None:
        # El bloque escribe sobre su instrucción siguiente: debe ejecutar la palabra nueva.
        words = [encode(Kind.ADDI, 6, 6, imm=1), encode(Kind.SW, 0, 10, 5, imm=8), encode(Kind.ADDI, 1, 0, imm=1),
                 encode(Kind.EBREAK)]
        memory = Rv32Memory(RAM)
        self.addCleanup(memory.close)
        memory.load_bytes(BASE, assemble(words))
        hart = Rv32Hart(memory)
        hart.accelerate = True
        hart.warmup = 1
        hart.regs[10] = CODE
        hart.regs[5] = encode(Kind.ADDI, 1, 0, imm=9)
        stop = hart.run(100)
        self.assertEqual((stop.reason, hart.regs[1], hart.regs[6], hart.instret), ("ebreak", 9, 1, 3))
        self.assertEqual(hart.block_instructions, 1)  # solo el addi antes del store

    def test_partial_stores_into_own_page_also_exit(self) -> None:
        # sb y sh sobre el byte 2 de «addi x1, x0, 1» (0x00100093) lo convierten en «addi x1, x0, 9».
        for store, value in ((encode(Kind.SB, 0, 10, 5, imm=10), 0x90), (encode(Kind.SH, 0, 10, 5, imm=10), 0x0090),
                             (encode(Kind.SH, 0, 10, 5, imm=9), 0x9000)):
            with self.subTest(store=hex(store)):
                words = [encode(Kind.ADDI, 6, 6, imm=1), store, encode(Kind.ADDI, 1, 0, imm=1), encode(Kind.EBREAK)]
                memory = Rv32Memory(RAM)
                self.addCleanup(memory.close)
                memory.load_bytes(BASE, assemble(words))
                hart = Rv32Hart(memory)
                hart.accelerate = True
                hart.warmup = 1
                hart.regs[10], hart.regs[5] = CODE, value
                stop = hart.run(100)
                self.assertEqual((stop.reason, hart.regs[1], hart.regs[6], hart.instret, hart.block_instructions), ("ebreak", 9, 1, 3, 1))
                self.assertEqual(memory.load32(CODE + 8), 0x00900093)

    def test_halfword_store_crossing_into_a_page_with_compiled_code(self) -> None:
        # El bloque de la página 0 escribe sh en +0x1FFF: el byte alto cae en la página 2, donde vive un
        # bloque ya compilado (rd 2 → 3). La escritura cruza página: sale al intérprete, que marca las dos.
        page0 = [encode(Kind.JAL, 0, imm=0x2000), encode(Kind.BNE, 0, 4, 0, imm=16), encode(Kind.SH, 0, 10, 5, imm=0x7FF),
                 encode(Kind.ADDI, 4, 0, imm=1), encode(Kind.JAL, 0, imm=-16), encode(Kind.EBREAK)]
        page2 = [encode(Kind.ADDI, 2, 0, imm=2), encode(Kind.JALR, 0, 11, imm=0)]
        image = assemble(page0) + bytes(0x2000 - 4 * len(page0)) + assemble(page2)
        for warmup in (1, 2):
            with self.subTest(warmup=warmup):
                memory = Rv32Memory(RAM)
                self.addCleanup(memory.close)
                memory.load_bytes(BASE, image)
                hart = Rv32Hart(memory)
                hart.accelerate = True
                hart.warmup = warmup
                hart.regs[10], hart.regs[11], hart.regs[5] = CODE + 0x1800, CODE + 4, 0x9300
                stop = hart.run(100)
                self.assertEqual((stop.reason, hart.regs[2], hart.regs[3], hart.regs[4], hart.instret), ("ebreak", 2, 2, 1, 11))
                self.assertEqual((memory.load32(CODE + 0x2000), memory.generation(1) > 0, memory.generation(2) > 0), (0x00200193, True, True))

    def test_two_page_block_storing_into_its_own_second_page_exits(self) -> None:
        # El bloque ocupa 0xFF8..0x1004 (páginas 0 y 1) y escribe sobre su propia tercera instrucción, que
        # está en la página 1: la guarda de página propia debe cubrir las dos páginas.
        words = [encode(Kind.ADDI, 1, 1, imm=1), encode(Kind.SW, 0, 10, 5, imm=0), encode(Kind.ADDI, 2, 0, imm=2), encode(Kind.EBREAK)]
        image = bytes(0xFF8) + assemble(words)
        memory = Rv32Memory(RAM)
        self.addCleanup(memory.close)
        memory.load_bytes(BASE, image)
        hart = Rv32Hart(memory)
        hart.accelerate = True
        hart.warmup = 1
        hart.reset(CODE + 0xFF8)
        hart.regs[10], hart.regs[5] = CODE + 0x1000, encode(Kind.ADDI, 3, 0, imm=2)
        stop = hart.run(100)
        self.assertEqual((stop.reason, hart.regs[1], hart.regs[2], hart.regs[3], hart.instret, hart.block_instructions),
                         ("ebreak", 1, 0, 2, 3, 1))
        self.assertEqual(hart.blocks[CODE + 0xFF8].pages, (0, 1))

    LOOP_WITH_STORE = [encode(Kind.ADDI, 5, 5, imm=1), encode(Kind.SW, 0, 9, 5, imm=0), encode(Kind.ADDI, 6, 6, imm=1),
                       encode(Kind.ADDI, 7, 7, imm=1), encode(Kind.BNE, 0, 5, 8, imm=-16), encode(Kind.EBREAK)]

    def loop_hart(self, target: int) -> Rv32Hart:
        memory = Rv32Memory(RAM)
        self.addCleanup(memory.close)
        memory.load_bytes(BASE, assemble(self.LOOP_WITH_STORE))
        hart = Rv32Hart(memory)
        hart.accelerate = True
        hart.warmup = 1
        hart.regs[9], hart.regs[8] = target, 100
        return hart

    def test_recompilation_is_capped_per_pc(self) -> None:
        # Bucle que escribe sobre la línea de su propio código (0x10..0x1F contiene el bne y el ebreak):
        # cada vuelta sale al intérprete, que invalida, y el bloque se recompila. Tras MAX_RECOMPILATIONS
        # recompilaciones el pc se interpreta, con el mismo resultado.
        image = assemble(self.LOOP_WITH_STORE)
        plain, fast = run_modes(image, 10_000)
        self.assertEqual(fast[:3], plain[:3])
        hart = self.loop_hart(CODE + 0x18)
        self.assertEqual(hart.run(10_000).reason, "ebreak")
        self.assertEqual((hart.regs[5], hart.regs[6], hart.regs[7], hart.instret), (100, 100, 100, 500))
        self.assertEqual((hart.compilations, hart.recompilations), (1 + MAX_RECOMPILATIONS, {CODE: MAX_RECOMPILATIONS}))
        self.assertIsNone(hart.blocks[CODE])
        self.assertEqual(hart.block_instructions, 1 + MAX_RECOMPILATIONS)   # un addi por entrada antes del sw
        self.assertEqual(hart.memory.generation(0), 1 + 100)               # la carga y los 100 stores sobre código
        hart.reset()
        self.assertEqual((hart.blocks, hart.recompilations), ({}, {}))

    def test_data_store_in_the_code_page_keeps_the_block(self) -> None:
        # El mismo bucle con sus datos en una línea sin código de la misma página (0x100): el bloque no sale,
        # no se invalida y ejecuta las 100 vueltas enteras (refinamiento de P1.6; antes recompilaba 8 veces y
        # quedaba interpretado).
        hart = self.loop_hart(CODE + 0x100)
        self.assertEqual(hart.run(10_000).reason, "ebreak")
        self.assertEqual((hart.regs[5], hart.regs[6], hart.regs[7], hart.instret), (100, 100, 100, 500))
        self.assertEqual((hart.compilations, hart.recompilations, hart.block_instructions), (1, {}, 500))
        self.assertIsNotNone(hart.blocks[CODE])
        memory = hart.memory
        self.assertEqual((memory.generation(0), memory.load32(CODE + 0x100), memory.written_pages()), (1, 100, [0]))
        self.assertEqual([i for i, c in enumerate(memory.code) if c], [0, 1])   # las líneas del bloque y del ebreak

    def test_not_taken_branch_fallthrough_is_a_control_point(self) -> None:
        # Con warmup 2, la primera vuelta se interpreta entera; la caída del beq (nunca tomado) debe
        # contar como llegada al punto CODE + 8, de modo que en la segunda vuelta ya hay dos bloques.
        words = [encode(Kind.ADDI, 6, 6, imm=1), encode(Kind.BEQ, 0, 0, 5, imm=8), encode(Kind.ADDI, 7, 7, imm=1),
                 encode(Kind.ADDI, 20, 20, imm=-1), encode(Kind.BNE, 0, 20, 0, imm=-16), encode(Kind.EBREAK)]
        memory = Rv32Memory(RAM)
        self.addCleanup(memory.close)
        memory.load_bytes(BASE, assemble(words))
        hart = Rv32Hart(memory)
        hart.accelerate = True
        hart.warmup = 2
        hart.regs[5], hart.regs[20] = 1, 5
        self.assertEqual(hart.run(100).reason, "ebreak")
        self.assertEqual((hart.regs[6], hart.regs[7], hart.instret), (5, 5, 25))
        self.assertEqual((hart.compilations, hart.block_instructions), (2, 20))   # vueltas 2–5 enteras en bloques
        self.assertEqual(sorted(pc for pc, block in hart.blocks.items() if block is not None), [CODE, CODE + 8])

    def test_block_cache_is_bounded_and_flushes_whole(self) -> None:
        # 12 bloques distintos (saltos encadenados) con un tope de 4 entradas: la caché nunca pasa del tope,
        # se vacía entera al llenarse y el resultado es el del intérprete.
        words = []
        for i in range(12):
            words += [encode(Kind.ADDI, 6, 6, imm=1), encode(Kind.JAL, 0, imm=4)]   # bloque de 2: addi + jal a la caída
        words += [encode(Kind.ADDI, 20, 20, imm=-1), encode(Kind.BNE, 0, 20, 0, imm=-4 * 25), encode(Kind.EBREAK)]
        image = assemble(words)
        plain, fast = run_modes(image, 10_000)
        self.assertEqual(fast[:3], plain[:3])
        memory = Rv32Memory(RAM)
        self.addCleanup(memory.close)
        memory.load_bytes(BASE, image)
        hart = Rv32Hart(memory)
        hart.accelerate = True
        hart.warmup = 1
        hart.max_blocks = 4
        hart.regs[20] = 3
        self.assertEqual(hart.run(10_000).reason, "ebreak")
        self.assertEqual((hart.regs[6], hart.instret), (36, 3 * 26))
        self.assertLessEqual(len(hart.blocks), 4)
        self.assertLessEqual(len(hart.block_hits), 4)
        self.assertGreater(hart.cache_flushes, 0)
        self.assertGreaterEqual(hart.compilations, 13)   # al menos una compilación por punto de control
        self.assertEqual(MAX_BLOCKS, 4096)
        hart.reset()
        self.assertEqual(hart.cache_flushes, 0)

    def test_attach_memory_and_reset_drop_the_blocks(self) -> None:
        memory = Rv32Memory(RAM)
        self.addCleanup(memory.close)
        memory.load_bytes(BASE, assemble([encode(Kind.ADDI, 1, 0, imm=1), encode(Kind.EBREAK)]))
        hart = Rv32Hart(memory)
        hart.accelerate = True
        hart.warmup = 1
        self.assertEqual((hart.run(10).reason, hart.regs[1], hart.compilations), ("ebreak", 1, 1))
        other = Rv32Memory(RAM)
        self.addCleanup(other.close)
        other.load_bytes(BASE, assemble([encode(Kind.ADDI, 1, 0, imm=25), encode(Kind.EBREAK)]))   # misma generación (1)
        hart.attach_memory(other)
        self.assertEqual((hart.blocks, hart.block_hits, hart.recompilations), ({}, {}, {}))
        self.assertEqual(hart.compilations, 1)   # las estadísticas acumuladas las limpia reset, no attach_memory
        hart.reset()
        self.assertEqual(hart.compilations, 0)
        self.assertEqual((hart.run(10).reason, hart.regs[1]), ("ebreak", 25))
        self.assertEqual(memory.load32(BASE), encode(Kind.ADDI, 1, 0, imm=1))
        hart.reset()
        self.assertEqual((hart.blocks, hart.block_hits), ({}, {}))

    def test_block_spanning_two_pages_is_invalidated_by_a_store_into_its_second_page(self) -> None:
        # X ocupa 0xFF8..0x1004 (páginas 0 y 1); un sw del bloque de la página 0 cambia su tercera
        # instrucción (en la página 1): X debe recompilarse aunque la página 0 no haya cambiado.
        x_block = [encode(Kind.ADDI, 1, 1, imm=1), encode(Kind.ADDI, 1, 1, imm=1), encode(Kind.ADDI, 2, 0, imm=2), encode(Kind.JALR, 0, 11, imm=0)]
        page0 = [encode(Kind.JAL, 0, imm=0xFF8), encode(Kind.BNE, 0, 4, 0, imm=16), encode(Kind.SW, 0, 10, 5, imm=0),
                 encode(Kind.ADDI, 4, 0, imm=1), encode(Kind.JAL, 0, imm=-16), encode(Kind.EBREAK)]
        image = assemble(page0) + bytes(0xFF8 - 4 * len(page0)) + assemble(x_block)
        memory = Rv32Memory(RAM)
        self.addCleanup(memory.close)
        memory.load_bytes(BASE, image)
        hart = Rv32Hart(memory)
        hart.accelerate = True
        hart.warmup = 1
        hart.regs[10], hart.regs[11], hart.regs[5] = CODE + 0x1000, CODE + 4, encode(Kind.ADDI, 3, 0, imm=2)
        stop = hart.run(100)
        self.assertEqual((stop.reason, hart.regs[1], hart.regs[2], hart.regs[3], hart.regs[4], hart.instret), ("ebreak", 4, 2, 2, 1, 15))
        self.assertEqual(hart.blocks[CODE + 0xFF8].pages, (0, 1))
        self.assertEqual(hart.compilations, 5)   # jal, X, bne, sw-addi-jal; X otra vez tras el parche

    def test_store_from_another_page_invalidates_the_block(self) -> None:
        # Página 0: bloque que salta a la página 1; la página 1 reescribe una instrucción de la 0 y
        # vuelve con jalr (x11 = CODE) mientras x9 != x12.
        page1 = CODE + 0x1000
        words0 = [encode(Kind.ADDI, 6, 6, imm=1), encode(Kind.ADDI, 7, 0, imm=1), encode(Kind.JAL, 1, imm=page1 - (CODE + 8))]
        words1 = [encode(Kind.SW, 0, 10, 5, imm=4), encode(Kind.ADDI, 9, 9, imm=1), encode(Kind.BEQ, 0, 9, 12, imm=8),
                  encode(Kind.JALR, 0, 11, imm=0), encode(Kind.EBREAK)]
        image = assemble(words0) + bytes(0x1000 - 4 * len(words0)) + assemble(words1)
        memory = Rv32Memory(RAM)
        self.addCleanup(memory.close)
        memory.load_bytes(BASE, image)
        hart = Rv32Hart(memory)
        hart.accelerate = True
        hart.warmup = 1
        hart.regs[10] = CODE
        hart.regs[11] = CODE
        hart.regs[5] = encode(Kind.ADDI, 7, 0, imm=5)  # sustituye «addi x7, x0, 1» de la página 0
        hart.regs[12] = 3
        stop = hart.run(1000)
        self.assertEqual(stop.reason, "ebreak")
        self.assertEqual((hart.regs[6], hart.regs[7], hart.regs[9]), (3, 5, 3))
        self.assertGreaterEqual(hart.compilations, 2)


class Horizon(unittest.TestCase):
    def test_block_does_not_enter_without_gas_for_its_cost(self) -> None:
        words = [encode(Kind.ADDI, 6, 6, imm=1)] * 10 + [encode(Kind.JAL, 0, imm=-40)]
        memory = Rv32Memory(RAM)
        self.addCleanup(memory.close)
        memory.load_bytes(BASE, assemble(words))
        hart = Rv32Hart(memory)
        hart.accelerate = True
        hart.warmup = 1
        self.assertEqual(hart.run(11), Stop("gas"))           # con warmup = 1 la primera vuelta ya va en bloque
        self.assertEqual((hart.instret, hart.gas, hart.regs[6], hart.pc), (11, 0, 10, CODE))
        self.assertEqual(hart.block_instructions, 11)
        self.assertEqual(hart.run(25), Stop("gas"))           # 2 vueltas compiladas (22) + 3 interpretadas
        self.assertEqual((hart.instret, hart.gas, hart.regs[6], hart.pc), (36, 0, 33, CODE + 12))
        self.assertEqual(hart.block_instructions, 33)
        block = hart.blocks[CODE]
        self.assertEqual((block.length, block.cost), (11, 11))

    def test_chunks_equal_one_run(self) -> None:
        rng = random.Random(99)
        words = loop_program(rng, 40, 20)
        image = assemble(words)
        whole, = run_modes(image, 3000)[1:]
        chunked, = run_modes(image, 3000, chunks=rng)[1:]
        self.assertEqual(whole[:3], chunked[:3])


class Limits(unittest.TestCase):
    def compile_at(self, words: list[int], pc: int = CODE, image: bytes | None = None):
        memory = Rv32Memory(RAM)
        self.addCleanup(memory.close)
        memory.load_bytes(BASE, image if image is not None else assemble(words))
        hart = Rv32Hart(memory)
        return compile_block(hart, pc), hart

    def test_block_length_cap_and_page_crossing(self) -> None:
        self.assertEqual(MAX_BLOCK, 32)
        block, hart = self.compile_at([encode(Kind.ADDI, 1, 1, imm=1)] * 40)
        self.assertEqual((block.length, block.pages, block.cost), (32, (0,), 32))
        self.assertEqual(compile_block(hart, CODE + 128).length, 8)
        self.assertNotIn("run", block.run.__globals__)   # sin ciclo función ↔ espacio de nombres
        hart.accelerate = True
        hart.warmup = 1
        self.assertEqual(hart.run(40), Stop("gas"))
        self.assertEqual((hart.instret, hart.regs[1], hart.block_instructions, hart.compilations), (40, 40, 40, 2))
        image = bytes(0x1000 - 8) + assemble([encode(Kind.ADDI, 1, 1, imm=1)] * 6)
        block, _ = self.compile_at([], pc=CODE + 0x1000 - 8, image=image)
        self.assertEqual((block.length, block.pages), (6, (0, 1)))

    def test_illegal_first_instruction_is_not_compiled(self) -> None:
        block, _ = self.compile_at([0, encode(Kind.ADDI, 1, 1, imm=1)])
        self.assertIsNone(block)
        block, _ = self.compile_at([encode(Kind.ECALL)])
        self.assertIsNone(block)

    def test_x0_destination_and_self_loop(self) -> None:
        block, hart = self.compile_at([encode(Kind.ADDI, 0, 1, imm=1), encode(Kind.LUI, 0, imm=4096), encode(Kind.JAL, 0, imm=-8)])
        self.assertNotIn("r0", block.source)
        hart.accelerate = True
        hart.warmup = 1
        self.assertEqual(hart.run(99), Stop("gas"))
        self.assertEqual((hart.instret, hart.regs[0], hart.pc), (99, 0, CODE))
        block, hart = self.compile_at([encode(Kind.JAL, 0, imm=0)])
        self.assertEqual(block.length, 1)
        hart.accelerate = True
        hart.warmup = 1
        self.assertEqual(hart.run(50), Stop("gas"))
        self.assertEqual((hart.instret, hart.block_instructions), (50, 50))

    def test_last_word_of_ram(self) -> None:
        image = bytes(RAM - 4) + assemble([encode(Kind.ADDI, 1, 1, imm=1)])
        block, hart = self.compile_at([], pc=BASE + RAM - 4, image=image)
        self.assertEqual(block.length, 1)
        hart.accelerate = True
        hart.warmup = 1
        hart.pc = BASE + RAM - 4
        self.assertEqual(hart.run(10), Stop("fallo", CAUSE_INSTRUCTION_ACCESS_FAULT, BASE + RAM))
        self.assertEqual((hart.instret, hart.regs[1], hart.pc), (1, 1, BASE + RAM))

    def test_warmup_is_respected(self) -> None:
        # Bucle de 10 vueltas (3 instrucciones): con calentamiento w, las primeras w − 1 llegadas al pc se
        # interpretan y las demás van en bloque.
        words = [encode(Kind.ADDI, 6, 6, imm=1), encode(Kind.ADDI, 20, 20, imm=-1), encode(Kind.BNE, 0, 20, 0, imm=-8),
                 encode(Kind.EBREAK)]
        for warmup, expected in ((1, 30), (2, 27), (3, 24), (10, 3), (11, 0)):
            with self.subTest(warmup=warmup):
                memory = Rv32Memory(RAM)
                self.addCleanup(memory.close)
                memory.load_bytes(BASE, assemble(words))
                hart = Rv32Hart(memory)
                hart.accelerate = True
                hart.warmup = warmup
                hart.regs[20] = 10
                self.assertEqual(hart.run(1000).reason, "ebreak")
                self.assertEqual((hart.instret, hart.regs[6], hart.block_instructions, hart.compilations),
                                 (30, 10, expected, 1 if expected else 0))

    def test_each_control_point_compiles_once_per_generation(self) -> None:
        rng = random.Random(5)
        words = loop_program(rng, 30, 50)
        memory = Rv32Memory(RAM)
        self.addCleanup(memory.close)
        memory.load_bytes(BASE, assemble(words))
        hart = Rv32Hart(memory)
        hart.accelerate = True
        hart.warmup = 3
        hart.run(100_000)
        self.assertEqual(hart.compilations, sum(1 for block in hart.blocks.values() if block is not None))
        self.assertEqual(hart.recompilations, {})   # sin escrituras sobre el código, ninguna recompilación
        self.assertGreater(hart.block_instructions, 0)
        self.assertEqual({block.source for block in hart.blocks.values() if block is not None}, {""})   # fuente no retenida


class CodeLineBoundary(unittest.TestCase):
    """Frontera línea/código del refinamiento de P1.6 dentro de los bloques."""

    def make(self, words: list[int]) -> tuple[Rv32Hart, Rv32Memory]:
        memory = Rv32Memory(RAM)
        self.addCleanup(memory.close)
        memory.load_bytes(BASE, assemble(words))
        hart = Rv32Hart(memory)
        hart.accelerate = True
        hart.warmup = 1
        return hart, memory

    def test_store_to_a_non_instruction_word_of_the_block_line_exits(self) -> None:
        # El bloque ocupa 0x0..0x8; el sw escribe en 0xC (el ebreak, aún no ejecutado, misma línea): sale al
        # intérprete, que sube la generación, y la palabra nueva se ejecuta.
        words = [encode(Kind.ADDI, 6, 6, imm=1), encode(Kind.SW, 0, 10, 5, imm=0xC), encode(Kind.ADDI, 1, 0, imm=1),
                 encode(Kind.EBREAK), encode(Kind.EBREAK)]
        hart, memory = self.make(words)
        hart.regs[10], hart.regs[5] = CODE, encode(Kind.ADDI, 2, 0, imm=5)
        stop = hart.run(100)
        self.assertEqual((stop.reason, hart.regs[1], hart.regs[2], hart.regs[6], hart.pc, hart.instret),
                         ("ebreak", 1, 5, 1, CODE + 0x10, 4))
        self.assertEqual((hart.block_instructions, hart.compilations, memory.generation(0)), (1, 1, 2))

    def test_halfword_store_crossing_a_line_inside_a_block(self) -> None:
        # sh en DATA + 0xF cruza de la línea 0x1000 a la 0x1010: la guarda del bloque lo manda al intérprete,
        # que lo escribe por la vía lenta; la generación solo sube si alguna de las dos líneas tiene código.
        words = [encode(Kind.ADDI, 6, 6, imm=1), encode(Kind.SH, 0, 10, 5, imm=0xF), encode(Kind.ADDI, 6, 6, imm=1),
                 encode(Kind.EBREAK)]
        for marked in (False, True):
            with self.subTest(marked=marked):
                hart, memory = self.make(words)
                if marked:
                    memory.mark_code(0x1010, 4)   # como si una instrucción de la línea 0x1010 estuviera cacheada
                hart.regs[10], hart.regs[5] = DATA, 0xBEEF
                stop = hart.run(100)
                self.assertEqual((stop.reason, hart.regs[6], hart.instret, hart.block_instructions), ("ebreak", 2, 3, 1))
                self.assertEqual((memory.load8u(DATA + 0xF), memory.load8u(DATA + 0x10)), (0xEF, 0xBE))
                self.assertEqual(memory.generation(1), 1 if marked else 0)
                self.assertEqual(memory.written_pages(), [0, 1])

    def test_stores_at_the_top_of_the_ram_stay_in_the_block(self) -> None:
        # Hallazgo de la ciega 3: las guardas «o <= size − ancho» de los stores generados no tenían cobertura en el
        # borde alto de la RAM (un «<» en vez de «<=» pasaba la suite, con el mismo estado final pero saliendo del bloque).
        words = [encode(Kind.ADDI, 6, 6, imm=1), encode(Kind.SW, 0, 10, 5, imm=-4), encode(Kind.SH, 0, 10, 5, imm=-6),
                 encode(Kind.SB, 0, 10, 5, imm=-7), encode(Kind.ADDI, 6, 6, imm=1), encode(Kind.EBREAK)]
        hart, memory = self.make(words)
        hart.regs[10], hart.regs[5] = BASE + RAM, 0x11223344
        stop = hart.run(100)
        self.assertEqual((stop.reason, hart.regs[6], hart.instret, hart.block_instructions), ("ebreak", 2, 5, 5))
        self.assertEqual((memory.load32(BASE + RAM - 4), memory.load16u(BASE + RAM - 6), memory.load8u(BASE + RAM - 7)),
                         (0x11223344, 0x3344, 0x44))
        self.assertEqual(memory.written_pages(), [0, (RAM >> 12) - 1])

    def test_host_write_over_a_compiled_block_invalidates_it(self) -> None:
        words = [encode(Kind.ADDI, 1, 0, imm=1), encode(Kind.ADDI, 2, 0, imm=2), encode(Kind.EBREAK)]
        hart, memory = self.make(words)
        self.assertEqual((hart.run(10).reason, hart.regs[1], hart.regs[2], hart.compilations), ("ebreak", 1, 2, 1))
        memory.load_bytes(CODE + 4, assemble([encode(Kind.ADDI, 2, 0, imm=9)]))   # el host reescribe el bloque
        hart.pc = CODE
        self.assertEqual((hart.run(10).reason, hart.regs[2], hart.compilations, hart.recompilations),
                         ("ebreak", 9, 2, {CODE: 1}))
        memory.fill([encode(Kind.ADDI, 2, 0, imm=4)], CODE + 4)
        hart.pc = CODE
        self.assertEqual((hart.run(10).reason, hart.regs[2], hart.compilations), ("ebreak", 4, 3))

    def test_generated_source_uses_the_code_map(self) -> None:
        words = [encode(Kind.SW, 0, 10, 5, imm=0), encode(Kind.SH, 0, 10, 5, imm=4), encode(Kind.SB, 0, 10, 5, imm=6),
                 encode(Kind.JAL, 0, imm=0)]
        hart, memory = self.make(words)
        block = compile_block(hart, CODE)
        self.assertEqual(block.length, 4)
        source = block.source
        self.assertIn("def run(regs, written=written, code=code, words=words, lanes=lanes):", source)
        self.assertEqual(source.count(f"not code[o >> {LINE_SHIFT}]"), 3)
        self.assertEqual(source.count("written[o >> 12] = 1"), 3)
        self.assertIn(f"o & {LINE_MASK} != {LINE_MASK}", source)
        self.assertNotIn("gens", source)
        self.assertEqual([i for i, c in enumerate(memory.code) if c], [0])   # compilar marca las líneas del bloque


class AllModesAgainstReference(unittest.TestCase):
    """Pruebas de la revisión ciega 1 (matan mutantes que la suite anterior dejaba vivos): cada programa se compara
    con el intérprete de referencia sin cachés en seis modos (intérprete, bloques con warmup 1 y 2, y los dos con
    cortes de gas), con los invariantes I y W comprobados al terminar."""

    MODES = (("intérprete", False, 1, None), ("bloques", True, 1, None), ("bloques w2", True, 2, None),
             ("intérprete cortes", False, 1, 3), ("bloques cortes 3", True, 1, 3), ("bloques cortes 5", True, 1, 5))

    def hart_state(self, image: bytes, setup, gas: int, accelerate: bool, warmup: int = 1, cuts: int | None = None):
        memory = Rv32Memory(RAM)
        self.addCleanup(memory.close)
        memory.load_bytes(BASE, image)
        hart = Rv32Hart(memory)
        hart.accelerate = accelerate
        hart.warmup = warmup
        setup(hart.regs)
        with recording_blocks() as recorded:
            if cuts is None:
                stop = hart.run(gas)
            else:
                stop, left = Stop("gas"), gas
                while left > 0 and stop.reason == "gas":
                    chunk = min(left, cuts)
                    stop = hart.run(chunk)
                    left -= chunk - hart.gas
            self.assertEqual(check_invariants(hart, memory, recorded), [])
        self.assertTrue(snapshot_round_trips(memory))
        return (list(hart.regs), hart.pc, hart.instret, bytes(memory.lanes), stop), hart, memory

    def assert_all_modes(self, words: list[int], setup, gas: int = 400) -> tuple:
        image = assemble(words)
        reference = Reference(image)
        setup(reference.regs)
        stop = reference.run(gas)
        expected = (list(reference.regs), reference.pc, reference.count, bytes(reference.mem), stop)
        for name, accelerate, warmup, cuts in self.MODES:
            with self.subTest(modo=name):
                got, _, _ = self.hart_state(image, setup, gas, accelerate, warmup, cuts)
                self.assertEqual(got[:3], expected[:3])
                self.assertEqual(got[4], expected[4])
                self.assertEqual(got[3], expected[3])
        return expected

    def test_sb_patches_an_executed_instruction_in_a_line_that_is_not_the_first(self) -> None:
        # La línea 0x20..0x2F es la de índice 2 y la de índice 1 nunca se decodifica (mata «_sb consulta otra línea»).
        words = [encode(Kind.JAL, 0, imm=0x20)] + [EBREAK] * 7 + [
            encode(Kind.ADDI, 1, 1, imm=1),                    # 0x20
            encode(Kind.SB, 0, 10, 5, imm=2),                  # 0x24: parchea el byte 2 de 0x20 (imm 1 → 3)
            encode(Kind.ADDI, 20, 20, imm=-1),                 # 0x28
            encode(Kind.BNE, 0, 20, 0, imm=-12),               # 0x2C → 0x20
            EBREAK]

        def setup(regs):
            regs[10], regs[5], regs[20] = CODE + 0x20, 0x30, 2

        self.assertEqual(self.assert_all_modes(words, setup)[0][1], 4)

    def test_store_over_the_instruction_that_decodes_it_for_the_first_time(self) -> None:
        # Un sw que se pisa a sí mismo la primera vez que se decodifica (mata «marcar después de ejecutar»).
        words = [encode(Kind.JAL, 0, imm=0x20)] + [EBREAK] * 7 + [
            encode(Kind.SW, 0, 10, 5, imm=0),                  # 0x20 se escribe a sí mismo
            encode(Kind.ADDI, 20, 20, imm=-1),                 # 0x24
            encode(Kind.BNE, 0, 20, 0, imm=-8),                # 0x28 → 0x20
            EBREAK]

        def setup(regs):
            regs[10], regs[5], regs[20] = CODE + 0x20, encode(Kind.ADDI, 1, 1, imm=5), 2

        self.assertEqual(self.assert_all_modes(words, setup)[0][1], 5)

    def test_block_sb_on_the_last_byte_of_the_line_of_the_block(self) -> None:
        # El sb cae en el último byte de la línea del bloque y la línea siguiente aún no tiene código (mata «el sb
        # del bloque consulta la línea del byte siguiente»).
        words = [encode(Kind.JAL, 0, imm=0x20)] + [EBREAK] * 7 + [
            encode(Kind.ADDI, 20, 20, imm=-1),                 # 0x20
            encode(Kind.ADDI, 1, 1, imm=1),                    # 0x24
            encode(Kind.SB, 0, 11, 5, imm=3),                  # 0x28: byte 0x2F, rs2 del bne pasa de x0 a x16
            encode(Kind.BNE, 0, 20, 0, imm=-12),               # 0x2C → 0x20
            EBREAK]

        def setup(regs):
            regs[11], regs[5], regs[16], regs[20] = CODE + 0x2C, 0xFF, 1, 3

        self.assertEqual(self.assert_all_modes(words, setup)[0][1], 2)   # con el bne parcheado: dos vueltas

    def test_sb_on_the_last_byte_of_a_code_line_patches_the_branch_each_iteration(self) -> None:
        # Hallazgo de la revisión ciega 2: un sb sobre el último byte de la línea del bne (0x2F), con la línea 0x30
        # sin decodificar hasta la salida, alternando 0xFF y 0xFE en cada vuelta (rs2 del bne pasa de x0 a x16 y
        # vuelve). Con x20 = 4 y x16 = 2 el bucle debe dar cuatro vueltas y salir con x20 = 0; un _sb que consultara
        # la línea del byte siguiente dejaría el bne obsoleto en caché y saldría en la segunda vuelta.
        words = [encode(Kind.JAL, 0, imm=0x20)] + [EBREAK] * 7 + [
            encode(Kind.ADDI, 20, 20, imm=-1),                 # 0x20
            encode(Kind.XORI, 5, 5, imm=1),                    # 0x24: 0xFE ↔ 0xFF
            encode(Kind.SB, 0, 11, 5, imm=3),                  # 0x28: byte 0x2F
            encode(Kind.BNE, 0, 20, 0, imm=-12),               # 0x2C → 0x20
            EBREAK]

        def setup(regs):
            regs[11], regs[5], regs[16], regs[20] = CODE + 0x2C, 0xFE, 2, 4

        expected = self.assert_all_modes(words, setup)
        self.assertEqual((expected[0][20], expected[2]), (0, 17))

    def test_block_sees_marks_added_after_it_was_compiled(self) -> None:
        # La línea 0x40 se marca después de compilar el bloque del sw (mata «el bloque liga una copia del mapa»).
        words = [encode(Kind.JAL, 0, imm=0x20)] + [EBREAK] * 7 + [
            encode(Kind.SW, 0, 11, 5, imm=0),                  # 0x20 parchea 0x40
            encode(Kind.ADD, 5, 5, 6),                         # 0x24 siguiente parche: imm + 1
            encode(Kind.JAL, 0, imm=0x18),                     # 0x28 → 0x40
            EBREAK, EBREAK, EBREAK, EBREAK, EBREAK,            # 0x2C..0x3C
            encode(Kind.ADDI, 1, 1, imm=0),                    # 0x40 (se parchea)
            encode(Kind.ADDI, 20, 20, imm=-1),                 # 0x44
            encode(Kind.BNE, 0, 20, 0, imm=-0x28),             # 0x48 → 0x20
            EBREAK]

        def setup(regs):
            regs[11], regs[5], regs[6], regs[20] = CODE + 0x40, encode(Kind.ADDI, 1, 1, imm=1), 0x00100000, 3

        self.assertEqual(self.assert_all_modes(words, setup)[0][1], 6)

    def test_block_whose_first_instruction_is_the_last_word_of_a_line_patches_itself(self) -> None:
        # 0x2C es la última palabra de su línea (mata «compile_block no marca la línea de la primera instrucción»).
        words = [encode(Kind.JAL, 0, imm=0x2C)] + [EBREAK] * 10 + [
            encode(Kind.SW, 0, 10, 5, imm=0),                  # 0x2C se parchea a sí mismo
            encode(Kind.ADDI, 20, 20, imm=-1),                 # 0x30
            encode(Kind.BNE, 0, 20, 0, imm=-8),                # 0x34 → 0x2C
            EBREAK]

        def setup(regs):
            regs[10], regs[5], regs[20] = CODE + 0x2C, encode(Kind.ADDI, 1, 1, imm=5), 2

        self.assertEqual(self.assert_all_modes(words, setup)[0][1], 5)

    def test_store_over_an_odd_word_that_is_the_only_one_decoded_in_its_line(self) -> None:
        # La palabra 0x24 (índice 9) es la única decodificada de su línea (mata «marcar solo los índices pares»).
        words = [encode(Kind.JAL, 0, imm=0x24)] + [EBREAK] * 8 + [
            encode(Kind.SW, 0, 10, 5, imm=0),                  # 0x24 se parchea a sí mismo
            encode(Kind.ADDI, 20, 20, imm=-1),                 # 0x28
            encode(Kind.BNE, 0, 20, 0, imm=-8),                # 0x2C → 0x24
            EBREAK]

        def setup(regs):
            regs[10], regs[5], regs[20] = CODE + 0x24, encode(Kind.ADDI, 1, 1, imm=5), 2

        self.assertEqual(self.assert_all_modes(words, setup)[0][1], 5)

    def test_guest_stores_mark_their_pages_as_written_in_interpreter_and_blocks(self) -> None:
        # sw/sb/sh a páginas vírgenes (1, 2, 3) deben quedar en written y en el snapshot, desde el intérprete y
        # desde un bloque (mata los mutantes que olvidan written en cualquiera de los seis stores).
        words = [encode(Kind.SB, 0, 10, 5, imm=0), encode(Kind.SH, 0, 11, 5, imm=0), encode(Kind.SW, 0, 12, 5, imm=0),
                 encode(Kind.ADDI, 20, 20, imm=-1), encode(Kind.BNE, 0, 20, 0, imm=-16), EBREAK]

        def setup(regs):
            regs[10], regs[11], regs[12], regs[5], regs[20] = DATA, DATA + 0x1000, DATA + 0x2000, 0x11223344, 4

        image = assemble(words)
        for accelerate in (False, True):
            with self.subTest(accelerate=accelerate):
                got, hart, memory = self.hart_state(image, setup, 400, accelerate)
                self.assertEqual(got[4].reason, "ebreak")
                if accelerate:
                    self.assertGreater(hart.block_instructions, 10)
                self.assertEqual(memory.written_pages(), [0, 1, 2, 3])
                self.assertEqual(list(memory.generations[:4]), [1, 0, 0, 0])   # datos: solo la carga subió


class HostileProgramsAgainstReference(unittest.TestCase):
    """Fuzz hostil (de la revisión ciega 1, acotado): bucles que escriben sobre su propio código, sobre datos de su
    página y de su línea, con sb/sh/sw alineados o no y bases en los bordes de línea y de página; oráculo: el
    intérprete de referencia sin cachés, en los seis modos; invariantes I y W comprobados en cada ejecución."""

    OFFSETS = (0x20, 0x34, 0x60, 0xFC0, 0xFE0, 0xFF0, 0xFF8, 0x1FE0, RAM - 0xA0, RAM - 0x80)
    GAS = 3000

    @staticmethod
    def generate(rng: random.Random) -> tuple[bytes, dict[int, int]]:
        code_off = rng.choice(HostileProgramsAgainstReference.OFFSETS)
        gentle = rng.random() < 0.5   # parches válidos y alineados: los programas viven más
        n = rng.randrange(6, 26)
        body = []
        for _ in range(n):
            roll = rng.random()
            rd = rng.randrange(1, 5)
            if roll < 0.28:
                body.append(encode(rng.choice((Kind.ADDI, Kind.XORI, Kind.ORI)), rd, rng.randrange(1, 10), imm=rng.randrange(-64, 64)))
            elif roll < 0.38:
                body.append(encode(rng.choice((Kind.ADD, Kind.SUB, Kind.XOR)), rd, rng.randrange(1, 10), rng.randrange(1, 10)))
            elif roll < 0.72:
                kind = rng.choice((Kind.SW, Kind.SW, Kind.SW, Kind.SH, Kind.SB)) if gentle else rng.choice((Kind.SB, Kind.SH, Kind.SW))
                imm = rng.randrange(-12, 12) * 4 if (gentle and kind == Kind.SW) else rng.randrange(-48, 48)
                src = rng.choice((5, 6, 9, 5, 6, 9, 5, rng.randrange(1, 5))) if gentle else rng.randrange(1, 10)
                body.append(encode(kind, 0, rng.choice((10, 10, 11, 12)), src, imm=imm))
            elif roll < 0.82:
                body.append(encode(rng.choice((Kind.LB, Kind.LBU, Kind.LH, Kind.LHU, Kind.LW)), rd, rng.choice((10, 11, 12)), imm=rng.randrange(-48, 48)))
            elif roll < 0.92:
                body.append(encode(rng.choice((Kind.BEQ, Kind.BNE, Kind.BLT)), 0, rng.randrange(1, 5), rng.randrange(0, 5), imm=4 * rng.randrange(1, 4)))
            elif roll < 0.96:
                body.append(encode(Kind.JAL, 0, imm=4 * rng.randrange(1, 4)))
            elif roll < 0.98:
                body.append(encode(Kind.LUI, rd, imm=rng.randrange(-(1 << 19), 1 << 19) * 4096))
            else:
                body.append(encode(Kind.FENCE_I))
        body.append(encode(Kind.ADDI, 20, 20, imm=-1))
        body.append(encode(Kind.BNE, 0, 20, 0, imm=-4 * len(body)))
        body += [EBREAK] * 5   # los saltos hacia delante pueden salirse por el final: el relleno son ebreak
        image = bytearray(code_off + 4 * len(body))
        if code_off:
            image[0:4] = encode(Kind.JAL, 0, imm=code_off).to_bytes(4, "little")
            for off in range(4, min(code_off, 0x100), 4):
                image[off:off + 4] = EBREAK.to_bytes(4, "little")
        for i, word in enumerate(body):
            image[code_off + 4 * i:code_off + 4 * i + 4] = word.to_bytes(4, "little")
        end = code_off + 4 * len(body)
        anchors = (code_off, code_off + 4 * n // 2, end - 16, end, code_off + 0x10, code_off + 0x20, end + 8,
                   code_off + 4 * n // 2 + (0 if gentle else 2))
        if rng.random() < 0.12 and not gentle:
            anchors = (0x1000, 0x1000 - 8, RAM - 8, RAM - 3, 0x2000 - 2)
        regs = {10: CODE + rng.choice(anchors) + (0 if gentle else rng.choice((0, 0, 1, 2, 3))), 11: CODE + rng.choice(anchors),
                12: CODE + rng.choice(anchors) + (0 if gentle else rng.choice((0, 2))),
                5: encode(Kind.ADDI, 1, 1, imm=1), 6: encode(Kind.ADDI, 2, 2, imm=-3), 7: rng.getrandbits(32),
                8: EBREAK, 9: 0x13, 20: rng.randrange(2, 7)}
        for r in (1, 2, 3, 4):
            regs[r] = rng.getrandbits(32) if rng.random() < 0.3 else rng.randrange(0, 16)
        return bytes(image), regs

    def run_hart(self, image: bytes, regs: dict[int, int], accelerate: bool, warmup: int, cuts: int | None) -> tuple:
        memory = Rv32Memory(RAM)
        try:
            memory.load_bytes(BASE, image)
            hart = Rv32Hart(memory)
            hart.accelerate = accelerate
            hart.warmup = warmup
            for r, v in regs.items():
                hart.regs[r] = v
            loaded = sum(memory.generations)
            with recording_blocks() as recorded:
                if cuts is None:
                    stop = hart.run(self.GAS)
                else:
                    stop, left = Stop("gas"), self.GAS
                    while left > 0 and stop.reason == "gas":
                        chunk = min(left, cuts)
                        stop = hart.run(chunk)
                        left -= chunk - hart.gas
                errors = check_invariants(hart, memory, recorded)
            if not snapshot_round_trips(memory):
                errors.append("el snapshot no reproduce la memoria")
            final = (list(hart.regs), hart.pc, hart.instret, bytes(memory.lanes), stop)
            return final, errors, hart.block_instructions, sum(memory.generations) > loaded
        finally:
            memory.close()

    def test_hostile_programs_in_six_modes(self) -> None:
        rng = random.Random(20261012)
        stats = {"bloques": 0, "suben": 0, "stops": {}}
        for index in range(120):
            image, regs = self.generate(rng)
            reference = Reference(image)
            for r, v in regs.items():
                reference.regs[r] = v
            stop = reference.run(self.GAS)
            expected = (list(reference.regs), reference.pc, reference.count, bytes(reference.mem), stop)
            stats["stops"][stop.reason] = stats["stops"].get(stop.reason, 0) + 1
            for name, accelerate, warmup, cuts in AllModesAgainstReference.MODES + (("bloques cortes 7", True, 1, 7),):
                with self.subTest(program=index, modo=name):
                    got, errors, in_blocks, raised = self.run_hart(image, regs, accelerate, warmup, cuts)
                    self.assertEqual(errors, [])
                    self.assertEqual(got[:3], expected[:3])
                    self.assertEqual(got[4], expected[4])
                    self.assertEqual(got[3], expected[3])
                    if name == "bloques":
                        stats["bloques"] += in_blocks > 0
                    if name == "intérprete":
                        stats["suben"] += raised
        self.assertGreater(stats["bloques"], 60, stats)     # la mayoría ejecuta algo en bloques
        self.assertGreater(stats["suben"], 30, stats)       # muchos escriben sobre líneas de código
        self.assertGreater(stats["stops"].get("ebreak", 0), 20, stats)


class CoreMarkWithBlocks(unittest.TestCase):
    def test_coremark_counts_and_crc_with_and_without_horizon(self) -> None:
        if not COREMARK.is_file():
            self.skipTest("falta el binario versionado de CoreMark")
        image = COREMARK.read_bytes()
        for horizon in (True, False):
            machine = Machine(ram_bytes=1 << 20)
            self.addCleanup(machine.close)
            machine.load_flat(image)
            machine.hart.accelerate = True
            machine.hart.horizon = horizon
            stop = machine.run(20_000_000)
            with self.subTest(horizon=horizon):
                self.assertEqual(stop, Stop("apagado"))
                self.assertEqual(machine.hart.instret, COREMARK_INSTRUCTIONS)
                for line in ("[0]crclist       : 0xe714", "[0]crcmatrix     : 0x1fd7", "[0]crcstate      : 0x8e3a",
                             "[0]crcfinal      : 0xfcaf"):
                    self.assertIn(line, machine.uart_text)
                self.assertGreater(machine.hart.block_instructions / machine.hart.instret, 0.90)


if __name__ == "__main__":
    unittest.main()
