/* riscv-tests rv32ui sobre el hart RV32 (H2.3): binario plano cargado en 0x80000000, con la
   disposición de env/p (código primero, datos en una página aparte). No hay pila: los tests
   no la usan. */
ENTRY(_start)
SECTIONS
{
    . = 0x80000000;
    .text : { KEEP(*(.text.init)) *(.text .text.*) }
    .rodata : { *(.rodata .rodata.* .srodata .srodata.*) }
    . = ALIGN(0x1000);
    .data : { *(.data .data.* .sdata .sdata.*) }
    .bss : { *(.bss .bss.* .sbss .sbss.*) *(COMMON) }
    /DISCARD/ : { *(.comment) *(.riscv.attributes) *(.eh_frame) }
}
