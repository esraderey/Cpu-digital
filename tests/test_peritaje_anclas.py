"""Anclas de regresión de los hallazgos confirmados por el peritaje del commit fb38417."""

from __future__ import annotations

import http.client
import json
import struct
import tempfile
import threading
import time
import unittest
import zlib
from pathlib import Path

from tramoya import MachineError

from cpu_digital import CPU, Opcode
from cpu_digital.assembler import Assembler, AssemblyError
from cpu_digital.memory_chip import NonVolatileMemoryChip
from cpu_digital.ui_server import ConsoleHTTPServer, ConsoleSession
from cpu_digital.vm32 import SNAPSHOT_MAGIC, TramoyaVM32, VMConfig, VMRuntimeError
from cpu_digital.vm32_assembler import Program32, VM32Assembler, VMAssemblyError


def program(source: str):
    return VM32Assembler().assemble(source).program


def unpack(raw: bytes) -> dict:
    return json.loads(zlib.decompress(raw[len(SNAPSHOT_MAGIC):]))


def pack(payload: dict) -> bytes:
    return SNAPSHOT_MAGIC + zlib.compress(json.dumps(payload).encode("utf-8"))


def core_state(vm: TramoyaVM32) -> tuple:
    return (
        vm.pc,
        vm.registers,
        dict(vm.flags),
        vm.stack,
        tuple(vm._input),
        vm.output,
        tuple(vm._pending_interrupts),
        dict(vm._interrupt_vectors),
        tuple(vm._interrupt_stack),
        vm._interrupts_enabled,
        vm._heap_ptr,
        vm.result().gas_remaining,
        {
            fid: (f.pc, tuple(f.registers), tuple(f.stack), f.state, tuple(sorted(f.flags.items())), f.parent_id)
            for fid, f in vm._fibers.items()
        },
        vm._current_fiber,
    )


class ConsoleOriginTests(unittest.TestCase):
    """PER-SEG-001: la consola solo obedece a su propio origen."""

    SOURCE = (
        ".code\n.entry _start\n_start:\n MOVI R1, 0\n LEA R2, D\n MOVI R3, 3\n SYSCALL 10\n HALT\n"
        ".data\nD: .WORD 80, 87, 78\n"
    )

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.chip_path = Path(self.tmp.name) / "chip.sqlite"
        self.server = ConsoleHTTPServer(("127.0.0.1", 0), memory_chip_path=self.chip_path)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def request(self, method: str, path: str, body: str | None = None, headers: dict | None = None) -> int:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body, headers or {})
        response = conn.getresponse()
        response.read()
        conn.close()
        return response.status

    def local(self, **extra: str) -> dict:
        return {"Host": f"127.0.0.1:{self.port}", "Content-Type": "application/json", **extra}

    def chip_bytes(self) -> bytes:
        self.server.session.close()
        with NonVolatileMemoryChip(self.chip_path) as chip:
            return chip.read(0, 3)

    def test_foreign_host_is_rejected(self) -> None:
        body = json.dumps({"source": self.SOURCE})
        self.assertEqual(self.request("POST", "/api/load", body, self.local(Host="evil.example")), 403)
        self.assertEqual(self.request("GET", "/api/snapshot", headers={"Host": "evil.example"}), 403)
        self.assertEqual(self.request("GET", "/api/state", headers={"Host": f"evil.example:{self.port}"}), 403)

    def test_foreign_origin_is_rejected(self) -> None:
        body = json.dumps({"source": self.SOURCE})
        status = self.request("POST", "/api/load", body, self.local(Origin="http://evil.example"))
        self.assertEqual(status, 403)

    def test_simple_cross_site_content_type_is_rejected(self) -> None:
        body = json.dumps({"source": self.SOURCE})
        headers = self.local(**{"Content-Type": "text/plain;charset=UTF-8"})
        self.assertEqual(self.request("POST", "/api/load", body, headers), 415)
        self.assertEqual(self.request("POST", "/api/action", json.dumps({"action": "run"}), headers), 415)
        self.assertEqual(self.chip_bytes(), b"\x00\x00\x00")

    def test_same_origin_console_keeps_working(self) -> None:
        body = json.dumps({"source": self.SOURCE})
        origin = f"http://127.0.0.1:{self.port}"
        self.assertEqual(self.request("POST", "/api/load", body, self.local(Origin=origin)), 200)
        self.assertEqual(self.request("POST", "/api/action", json.dumps({"action": "run"}), self.local()), 200)
        self.assertEqual(self.request("GET", "/api/state", headers={"Host": f"localhost:{self.port}"}), 200)
        self.assertEqual(self.chip_bytes(), b"PWN")


class CheckpointCostTests(unittest.TestCase):
    """PER-REC-002: el coste por instrucción no depende del estado acumulado."""

    LOOP = ".code\n.entry _start\n_start:\nBUCLE: JMP BUCLE\n"

    @staticmethod
    def per_instruction(vm: TramoyaVM32, count: int = 3000) -> float:
        best = float("inf")
        for _ in range(3):
            start = time.perf_counter()
            vm.run(max_instructions=count)
            best = min(best, time.perf_counter() - start)
        return best / count

    def test_large_input_queue_does_not_slow_every_instruction(self) -> None:
        base = TramoyaVM32(VMConfig(trace_size=0))
        base.load_program(program(self.LOOP))
        loaded = TramoyaVM32(VMConfig(trace_size=0))
        loaded.load_program(program(self.LOOP), inputs=[1] * 300_000)
        self.assertLess(self.per_instruction(loaded), self.per_instruction(base) * 5)

    def test_deep_stacks_do_not_slow_every_instruction(self) -> None:
        source = (
            ".code\n.entry _start\n_start:\n MOVI R7, 8000\nPUSHL: PUSH R7\n SUBI R7, R7, 1\n"
            " CMPI R7, 0\n JPOS PUSHL\n SPAWN F\n MOV R2, R1\n SWITCH R2\nBUCLE: JMP BUCLE\n"
            "F: MOVI R7, 8000\nPUSHF: PUSH R7\n SUBI R7, R7, 1\n CMPI R7, 0\n JPOS PUSHF\n"
            " MOVI R8, 0\n SWITCH R8\n JMP F\n"
        )
        base = TramoyaVM32(VMConfig(trace_size=0))
        base.load_program(program(self.LOOP))
        assembled = program(source)
        deep = TramoyaVM32(VMConfig(trace_size=0))
        deep.load_program(assembled)
        deep.run(max_instructions=200_000, breakpoints={assembled.symbols["BUCLE"]})
        self.assertEqual(deep.pc, assembled.symbols["BUCLE"])
        self.assertEqual(len(deep.stack), 8000)
        self.assertLess(self.per_instruction(deep), self.per_instruction(base) * 5)


class InstructionRollbackTests(unittest.TestCase):
    """PER-REC-002: el rediseño del checkpoint conserva la atomicidad por instrucción."""

    def assert_fault_is_atomic(self, vm: TramoyaVM32) -> None:
        before = core_state(vm)
        vm.step()
        self.assertEqual(vm.state, "FAULTED")
        after = core_state(vm)
        self.assertEqual(after, before)

    def test_call_to_invalid_target_undoes_push(self) -> None:
        vm = TramoyaVM32()
        vm.load_program(program(".code\nMOVI R1, 999\nPUSH R1\nCALLR R1\nHALT"))
        vm.step()
        vm.step()
        self.assert_fault_is_atomic(vm)

    def test_pop_into_read_only_register_restores_stack(self) -> None:
        vm = TramoyaVM32()
        vm.load_program(program(".code\nMOVI R1, 5\nPUSH R1\nPOP R15\nHALT"))
        vm.step()
        vm.step()
        self.assert_fault_is_atomic(vm)

    def test_host_syscall_side_effects_are_undone(self) -> None:
        assembled = program(".code\n_start: SPAWN W\nPUSH R1\nSYSCALL 101\nHALT\nW: FRET")

        def bad_handler(machine: TramoyaVM32) -> None:
            machine._sys_read_int()
            machine._push(77)
            machine._pop()
            machine._pop()
            machine.emit("x")
            machine.set_interrupt_vector(3, assembled.symbols["W"])
            machine.set_interrupt_vector(2, assembled.symbols["_start"])
            machine.request_interrupt(4)
            machine.provide_input(9, 10)
            raise RuntimeError("boom")

        vm = TramoyaVM32()
        vm.register_syscall(101, bad_handler)
        vm.load_program(assembled, inputs=[1, 2])
        vm.step()
        vm.step()
        vm.set_interrupt_vector(2, assembled.symbols["W"])
        self.assert_fault_is_atomic(vm)

    def test_external_interrupt_entry_is_undone(self) -> None:
        vm = TramoyaVM32()
        vm.load_program(program(".code\nNOP\nHALT"))
        vm.step()
        vm.request_interrupt(7)
        self.assert_fault_is_atomic(vm)
        self.assertEqual(tuple(vm._pending_interrupts), (7,))

    def test_fiber_switch_state_is_undone(self) -> None:
        assembled = program(".code\n_start: SPAWN W\nMOV R2, R1\nSWITCH R2\nHALT\nW: MOVI R1, 3\nSYSCALL 101\nHALT")

        def bad_handler(machine: TramoyaVM32) -> None:
            machine._finish_fiber()
            raise RuntimeError("boom")

        vm = TramoyaVM32()
        vm.register_syscall(101, bad_handler)
        vm.load_program(assembled)
        for _ in range(4):
            vm.step()
        self.assertEqual(vm._current_fiber, 1)
        self.assert_fault_is_atomic(vm)


class ConsoleConfigLimitTests(unittest.TestCase):
    """PER-REC-003: /api/load no acepta límites de recursos sin tope."""

    def test_unbounded_limits_are_rejected(self) -> None:
        session = ConsoleSession()
        for key in ("output_limit", "gas_limit", "stack_limit"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                session._config_from({"config": {key: 10**12}})

    def test_default_limits_are_accepted(self) -> None:
        config = ConsoleSession()._config_from({"config": {}})
        self.assertEqual(config.output_limit, VMConfig().output_limit)


class SnapshotRestoreAtomicityTests(unittest.TestCase):
    """PER-REC-004: un restore fallido no altera la VM y el snapshot propio siempre se relee."""

    def make_vm(self) -> TramoyaVM32:
        vm = TramoyaVM32(VMConfig(memory_words=256, output_limit=1, trace_size=0))
        vm.load_program(program(".code\nMOVI R1, 7\nHALT"), inputs=[-(2**31)] * 300_000)
        vm.step()
        return vm

    def test_own_snapshot_round_trips(self) -> None:
        vm = self.make_vm()
        vm.restore_bytes(vm.snapshot_bytes())
        self.assertEqual(vm.registers[1], 7)
        self.assertEqual(len(vm._input), 300_000)

    def test_rejected_snapshot_leaves_vm_untouched(self) -> None:
        vm = self.make_vm()
        payload = unpack(vm.snapshot_bytes())
        payload["core"]["input"] = []
        payload["core"]["registers"][1] = 1234567
        payload["core"]["heap_ptr"] = 0
        before = core_state(vm)
        with self.assertRaisesRegex(ValueError, "heap"):
            vm.restore_bytes(pack(payload))
        self.assertEqual(core_state(vm), before)


    def test_invalid_lifecycle_leaves_vm_untouched(self) -> None:
        vm = self.make_vm()
        payload = unpack(vm.snapshot_bytes())
        payload["lifecycle"]["state"] = "HALTED"
        payload["lifecycle"]["ctx"] = [1]
        before = (core_state(vm), vm.state, dict(vm.machine.ctx))
        with self.assertRaises((ValueError, MachineError)):
            vm.restore_bytes(pack(payload))
        self.assertEqual((core_state(vm), vm.state, dict(vm.machine.ctx)), before)

    def test_fiber_saved_at_end_of_code_round_trips(self) -> None:
        vm = TramoyaVM32()
        assembled = program(".code\n.entry _start\nW: NOP\nHALT\n_start: SPAWN W\nSWITCH R1")
        vm.load_program(assembled)
        vm.run(max_instructions=3)
        self.assertEqual(vm._fibers[0].pc, assembled.code_size)
        vm.restore_bytes(vm.snapshot_bytes())
        self.assertEqual(vm._current_fiber, 1)


class SnapshotTrustTests(unittest.TestCase):
    """PER-SEG-005: el host, no el snapshot, decide capacidades y límites."""

    def hostile_snapshot(self) -> bytes:
        vm = TramoyaVM32(VMConfig(capabilities=frozenset({"io"})))
        vm.load_program(program(".code\nHALT"))
        payload = unpack(vm.snapshot_bytes())
        payload["config"]["capabilities"] = ["io", "memory_chip", "interrupt_control"]
        payload["config"]["gas_limit"] = 10**15
        payload["core"]["gas_remaining"] = 10**15
        return pack(payload)

    def test_default_ignores_embedded_capabilities(self) -> None:
        vm = TramoyaVM32.from_snapshot_bytes(self.hostile_snapshot())
        self.assertNotIn("memory_chip", vm.config.capabilities)
        self.assertNotIn("interrupt_control", vm.config.capabilities)
        self.assertEqual(vm.config.capabilities, VMConfig().capabilities)

    def test_host_limits_bound_snapshot_limits(self) -> None:
        with self.assertRaisesRegex(ValueError, "gas_limit"):
            TramoyaVM32.from_snapshot_bytes(self.hostile_snapshot(), limits=VMConfig())

    def test_console_restore_applies_host_limits(self) -> None:
        session = ConsoleSession()
        # Otro tamaño de memoria obliga a la consola a usar from_snapshot_bytes.
        vm = TramoyaVM32(VMConfig(memory_words=4096))
        vm.load_program(program(".code\nHALT"))
        payload = unpack(vm.snapshot_bytes())
        payload["config"]["gas_limit"] = 10**15
        payload["core"]["gas_remaining"] = 10**15
        with self.assertRaisesRegex(ValueError, "gas_limit"):
            session.restore(pack(payload))


class SignedCompareTests(unittest.TestCase):
    """PER-LOG-006/007: JLT/JGT comparan con signo aunque la resta desborde."""

    VM_SOURCE = ".code\nMOVI R1, {a}\nMOVI R2, {b}\nCMP R1, R2\n{jump} SI\nMOVI R3, 0\nHALT\nSI: MOVI R3, 1\nHALT"

    def vm_branch(self, jump: str, a: int, b: int) -> int:
        vm = TramoyaVM32()
        vm.load_program(program(self.VM_SOURCE.format(a=a, b=b, jump=jump)))
        vm.run()
        return vm.registers[3]

    def test_vm32_signed_jumps(self) -> None:
        cases = [
            ("JLT", -(2**31), 1, 1), ("JLT", -5, 1, 1), ("JLT", 1, -(2**31), 0), ("JLT", 3, 3, 0),
            ("JGT", 2**31 - 1, -1, 1), ("JGT", 5, 1, 1), ("JGT", -(2**31), 1, 0), ("JGT", 3, 3, 0),
        ]
        for jump, a, b, expected in cases:
            with self.subTest(jump=jump, a=a, b=b):
                self.assertEqual(self.vm_branch(jump, a, b), expected)

    def test_vm32_signed_jumps_after_fcmp_with_infinity(self) -> None:
        one, inf, ninf = 0x3F800000, 0x7F800000, -8388608  # 1.0f, +inf, -inf
        for jump, a, b, expected in (("JLT", one, inf, 1), ("JGT", one, ninf, 1), ("JLT", inf, one, 0)):
            with self.subTest(jump=jump, a=a, b=b):
                vm = TramoyaVM32()
                vm.load_program(program(self.VM_SOURCE.replace("CMP", "FCMP").format(a=a, b=b, jump=jump)))
                vm.run()
                self.assertEqual(vm.registers[3], expected)

    def test_vm32_jneg_keeps_raw_sign_semantics(self) -> None:
        self.assertEqual(self.vm_branch("JNEG", -(2**31), 1), 0)

    def cpu_branch(self, jump: str, a: int, b: int) -> tuple:
        cpu = CPU()
        source = f"LOADI {a}\nCMPI {b}\n{jump} SI\nLOADI 0\nOUT\nHALT\nSI: LOADI 1\nOUT\nHALT\n"
        cpu.load_program(Assembler().assemble(source).words)
        return cpu.run().output

    def test_cpu16_signed_jumps(self) -> None:
        cases = [
            ("JLT", -30000, 30000, (1,)), ("JLT", -3, 3, (1,)), ("JLT", 30000, -30000, (0,)),
            ("JGT", 30000, -30000, (1,)), ("JGT", -30000, 30000, (0,)), ("JGT", 4, 4, (0,)),
        ]
        for jump, a, b, expected in cases:
            with self.subTest(jump=jump, a=a, b=b):
                self.assertEqual(self.cpu_branch(jump, a, b), expected)


class FiberInterruptTests(unittest.TestCase):
    """PER-LOG-008: las interrupciones pertenecen a la fibra principal."""

    def test_external_interrupt_waits_for_main_fiber(self) -> None:
        assembled = program(
            ".code\n_start: SPAWN W\nMOV R2, R1\nSWITCH R2\nHALT\n"
            "W: NOP\nNOP\nNOP\nFRET\nISR: MOV R1, R2\nSYSCALL 1\nIRET"
        )
        vm = TramoyaVM32()
        vm.load_program(assembled)
        vm.set_interrupt_vector(1, assembled.symbols["ISR"])
        vm.run(max_instructions=4)
        self.assertEqual(vm._current_fiber, 1)
        vm.request_interrupt(1)
        result = vm.run()
        self.assertTrue(result.ok)
        self.assertEqual(result.output, (1,))

    def test_iret_from_another_fiber_faults(self) -> None:
        source = (
            ".code\n_start: SPAWN W\nMOV R2, R1\nINT 1\nMOV R1, R2\nSYSCALL 1\nHALT\n"
            "ISR: SWITCH R2\nIRET\nW: {w}"
        )
        for w, state, output in (("IRET", "FAULTED", ()), ("FRET", "HALTED", (1,))):
            with self.subTest(w=w):
                assembled = program(source.format(w=w))
                vm = TramoyaVM32()
                vm.load_program(assembled)
                vm.set_interrupt_vector(1, assembled.symbols["ISR"])
                result = vm.run()
                self.assertEqual(result.state, state)
                self.assertEqual(result.output, output)

    def test_software_interrupt_inside_fiber_faults(self) -> None:
        assembled = program(".code\n_start: SPAWN W\nSWITCH R1\nHALT\nW: INT 1\nFRET\nISR: IRET")
        vm = TramoyaVM32()
        vm.load_program(assembled)
        vm.set_interrupt_vector(1, assembled.symbols["ISR"])
        result = vm.run()
        self.assertEqual(result.state, "FAULTED")
        self.assertIn("fibra principal", result.fault or "")


class FiberLimitTests(unittest.TestCase):
    """PER-LOG-009: fiber_limit cuenta fibras vivas, no las terminadas ni la principal."""

    SOURCE = ".code\n_start: MOVI R5, 100\nLOOP: SPAWN W\nMOV R2, R1\nSWITCH R2\nSUBI R5, R5, 1\nJNZ LOOP\nHALT\nW: FRET"

    def test_short_lived_fibers_do_not_exhaust_the_limit(self) -> None:
        vm = TramoyaVM32()
        vm.load_program(program(self.SOURCE))
        result = vm.run()
        self.assertTrue(result.ok, result.fault)
        self.assertEqual(vm.registers[5], 0)

    def test_live_fibers_still_respect_the_limit(self) -> None:
        vm = TramoyaVM32(VMConfig(fiber_limit=2))
        vm.load_program(program(".code\nSPAWN W\nSPAWN W\nSPAWN W\nHALT\nW: FRET"))
        result = vm.run()
        self.assertEqual(result.state, "FAULTED")
        self.assertIn("Límite de fibras", result.fault or "")


class FloatFlagTests(unittest.TestCase):
    """PER-LOG-010: las banderas FPU describen el float32 almacenado."""

    @staticmethod
    def bits(value: float) -> int:
        return struct.unpack("<i", struct.pack("<f", value))[0]

    def run_op(self, op: str, a: int, b: int) -> TramoyaVM32:
        vm = TramoyaVM32()
        vm.load_program(program(f".code\nMOVI R2, {a}\nMOVI R3, {b}\n{op} R1, R2, R3\nHALT"))
        vm.run()
        return vm

    def test_overflow_to_infinity_sets_o(self) -> None:
        vm = self.run_op("FMUL", self.bits(3.0e38), self.bits(10.0))
        self.assertEqual(vm.registers[1] & 0xFFFFFFFF, 0x7F800000)
        self.assertTrue(vm.flags["O"])

    def test_underflow_to_zero_sets_z(self) -> None:
        vm = self.run_op("FMUL", 1, 0x3E800000)
        self.assertEqual(vm.registers[1], 0)
        self.assertTrue(vm.flags["Z"])
        self.assertFalse(vm.flags["N"])

    def test_fcmp_equal_infinities(self) -> None:
        vm = TramoyaVM32()
        vm.load_program(program(".code\nMOVI R2, 0x7F800000\nMOVI R3, 0x7F800000\nFCMP R2, R3\nHALT"))
        vm.run()
        self.assertEqual(dict(vm.flags), {"Z": True, "N": False, "C": False, "O": False})

    def test_regular_results_keep_their_flags(self) -> None:
        vm = self.run_op("FSUB", self.bits(1.0), self.bits(3.0))
        self.assertEqual(dict(vm.flags), {"Z": False, "N": True, "C": False, "O": False})


class ReadIntAccountingTests(unittest.TestCase):
    """PER-LOG-011: esperar entrada no consume gas ni cuenta instrucciones."""

    def test_wait_then_input_matches_prefilled_input(self) -> None:
        waiting = TramoyaVM32()
        waiting.load_program(program(".code\nSYSCALL 3\nHALT"))
        waiting.run()
        self.assertEqual(waiting.state, "WAITING")
        waiting.provide_input(5)
        after = waiting.run()
        prefilled = TramoyaVM32()
        prefilled.load_program(program(".code\nSYSCALL 3\nHALT"), inputs=[5])
        expected = prefilled.run()
        self.assertEqual((after.instructions, after.gas_remaining), (expected.instructions, expected.gas_remaining))


class DivisionOverflowTests(unittest.TestCase):
    """PER-LOG-012: INT_MIN / -1 marca desbordamiento."""

    def test_div_int_min_by_minus_one_sets_o(self) -> None:
        vm = TramoyaVM32()
        vm.load_program(program(".code\nMOVI R1, 0x80000000\nMOVI R2, -1\nDIV R3, R1, R2\nHALT"))
        vm.run()
        self.assertEqual(vm.registers[3], -(2**31))
        self.assertTrue(vm.flags["O"])

    def test_regular_division_does_not_set_o(self) -> None:
        vm = TramoyaVM32()
        vm.load_program(program(".code\nMOVI R1, -7\nMOVI R2, 2\nDIV R3, R1, R2\nHALT"))
        vm.run()
        self.assertEqual(vm.registers[3], -3)
        self.assertFalse(vm.flags["O"])


class Cpu16RestoreValidationTests(unittest.TestCase):
    """PER-ROB-003: restore de la CPU16 rechaza contextos inválidos sin cambiar la CPU."""

    def base(self) -> tuple[CPU, dict]:
        cpu = CPU()
        cpu.load_program([Opcode.LOAD, 5, Opcode.HALT, 0, 0, 7])
        return cpu, json.loads(cpu.snapshot())

    def test_invalid_contexts_are_rejected_atomically(self) -> None:
        mutations = {
            "memoria no entera": lambda s: s["ctx"]["memory"].__setitem__(5, "x"),
            "banderas incompletas": lambda s: s["ctx"].__setitem__("flags", {}),
            "stack_limit ajeno": lambda s: s["ctx"].__setitem__("stack_limit", 10**6),
            "pila no entera": lambda s: s["ctx"].__setitem__("stack", ["x"]),
            "acc fuera de 16 bits": lambda s: s["ctx"].__setitem__("acc", 10**6),
            "historial envenenado": lambda s: s.__setitem__("history", [{"state": "FETCH", "ctx": {}}]),
            "estado inicial ajeno": lambda s: s.__setitem__("initial", "HALT"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                cpu, data = self.base()
                before = cpu.snapshot()
                mutate(data)
                with self.assertRaises(ValueError):
                    cpu.restore(json.dumps(data))
                self.assertEqual(cpu.snapshot(), before)

    def test_valid_snapshot_still_restores(self) -> None:
        cpu, data = self.base()
        cpu.restore(json.dumps(data))
        self.assertEqual(cpu.run().state, "HALT")
        self.assertEqual(cpu.ctx["acc"], 7)


class ErrorContractTests(unittest.TestCase):
    """PER-ROB-004: los errores de entrada salen con el tipo de error del dominio."""

    def test_vm32_assembler_errors(self) -> None:
        with self.assertRaises(VMAssemblyError):
            VM32Assembler().assemble(".code\nMOVI R1, {[]:1}\nHALT")

    def test_prefixed_offsets_keep_their_base(self) -> None:
        for suffix, expected in (("0x10", 16), ("0b101", 5), ("010", 10)):
            with self.subTest(suffix=suffix):
                assembled = program(f".code\nLEA R1, X+{suffix}\nX: HALT")
                self.assertEqual(assembled.words[2], assembled.symbols["X"] + expected)
                self.assertEqual(Assembler().assemble(f"A: NOP\nJMP A+{suffix}").words[-1], expected)

    def test_vm32_offset_is_decimal(self) -> None:
        assembled = program(".code\nLEA R1, X+010\nX: HALT")
        self.assertEqual(assembled.words[2], assembled.symbols["X"] + 10)

    def test_cpu16_assembler_errors(self) -> None:
        for source in (".ORG 0x7FFFFFFFFFFFFFFF\nHALT", ".ORG 0x10001\nHALT"):
            with self.subTest(source=source), self.assertRaises(AssemblyError):
                Assembler().assemble(source)
        self.assertEqual(Assembler().assemble("A: NOP\nJMP A+010").words[-1], 10)

    def test_bytecode_metadata_must_be_an_object(self) -> None:
        raw = program(".code\nHALT").to_bytes()
        header = 28
        size = struct.unpack_from("<I", raw, header)[0]
        for metadata in (b"[]", b"[" * 100_000):
            evil = raw[:header] + struct.pack("<I", len(metadata)) + metadata + raw[header + 4 + size:]
            with self.subTest(metadata=metadata[:4]), self.assertRaises(ValueError):
                Program32.from_bytes(evil)

    def test_public_memory_api_raises_runtime_error(self) -> None:
        vm = TramoyaVM32()
        vm.load_program(program(".code\nHALT"))
        for address in (0, 10**9):
            with self.subTest(address=address), self.assertRaises(VMRuntimeError):
                vm.write_memory(address, 1)


if __name__ == "__main__":
    unittest.main()
