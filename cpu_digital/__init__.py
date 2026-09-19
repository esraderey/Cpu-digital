"""CPU Digital: un procesador educativo orquestado por Tramoya."""

from .assembler import Assembler, AssemblyError, AssemblyResult
from .cpu import CPU, RunResult, TraceEntry
from .isa import ISA, Instruction, Opcode
from .vm32 import VMConfig, VMResult, VMRuntimeError, TramoyaVM32
from .vm32_assembler import Program32, VM32Assembler, VMAssemblyError, VMAssemblyResult
from .vm32_isa import VM32_ISA, VMInstruction, VMOpcode

__all__ = [
    "Assembler",
    "AssemblyError",
    "AssemblyResult",
    "CPU",
    "ISA",
    "Instruction",
    "Opcode",
    "RunResult",
    "TraceEntry",
    "Program32",
    "TramoyaVM32",
    "VM32Assembler",
    "VM32_ISA",
    "VMAssemblyError",
    "VMAssemblyResult",
    "VMConfig",
    "VMInstruction",
    "VMOpcode",
    "VMResult",
    "VMRuntimeError",
]
