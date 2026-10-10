/* Binario plano cargado en 0x80000000; .bss queda al final (la RAM del
   emulador arranca a cero) y la pila ocupa 32 KiB tras él. */
ENTRY(_start)
SECTIONS
{
    . = 0x80000000;
    .text : { KEEP(*(.text.start)) *(.text .text.*) }
    .rodata : { *(.rodata .rodata.* .srodata .srodata.*) }
    .data : { *(.data .data.* .sdata .sdata.*) }
    .bss : { __bss_start = .; *(.bss .bss.* .sbss .sbss.*) *(COMMON) __bss_end = .; }
    . = ALIGN(16);
    . = . + 0x8000;
    _stack_top = .;
    /DISCARD/ : { *(.comment) *(.riscv.attributes) *(.eh_frame) }
}
