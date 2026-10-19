/* riscv-arch-test 2.7.4 rv32i_m/I sobre el hart RV32 (H2.4): binario plano cargado en 0x80000000; código
   primero (.text.init con rvtest_entry_point), datos y firma en una página aparte. Sin pila. */
ENTRY(rvtest_entry_point)
SECTIONS
{
    . = 0x80000000;
    .text : { KEEP(*(.text.init)) *(.text .text.*) }
    .rodata : { *(.rodata .rodata.*) }
    . = ALIGN(0x1000);
    .data : { *(.data .data.* .sdata .sdata.*) }
    .data.string : { *(.data.string) }
    .bss : { *(.bss .bss.* .sbss .sbss.*) *(COMMON) }
    /DISCARD/ : { *(.comment) *(.riscv.attributes) *(.eh_frame) }
}
