/* Binario plano cargado en 0x80000000 con .data y .bss alineados a una página de 4 KiB: el código
   (y las constantes) no comparten página con los datos que el benchmark escribe. Es la disposición
   que un cargador con MMU da a cualquier programa; el link.ld de CoreMark (../coremark/link.ld)
   empaqueta los datos tras el código y mide el coste de la invalidación por página (P1.6). */
ENTRY(_start)
SECTIONS
{
    . = 0x80000000;
    .text : { KEEP(*(.text.start)) *(.text .text.*) }
    .rodata : { *(.rodata .rodata.* .srodata .srodata.*) }
    . = ALIGN(4096);
    .data : { *(.data .data.* .sdata .sdata.*) }
    .bss : { __bss_start = .; *(.bss .bss.* .sbss .sbss.*) *(COMMON) __bss_end = .; }
    . = ALIGN(16);
    . = . + 0x8000;
    _stack_top = .;
    /DISCARD/ : { *(.comment) *(.riscv.attributes) *(.eh_frame) }
}
