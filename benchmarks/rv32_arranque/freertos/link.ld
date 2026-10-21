/* Enlace del puerto propio de FreeRTOS (H3.5): todo en la RAM desde 0x80000000 (binario plano), 256 KiB, pila al final. */
OUTPUT_ARCH(riscv)
ENTRY(_start)
MEMORY { RAM (rwx) : ORIGIN = 0x80000000, LENGTH = 256K }
SECTIONS
{
    .text : { *(.text.start) *(.text .text.*) } > RAM
    .rodata : { *(.rodata .rodata.* .srodata .srodata.*) } > RAM
    .data : { *(.data .data.* .sdata .sdata.*) } > RAM
    .bss (NOLOAD) : ALIGN(16) { __bss_start = .; *(.bss .bss.* .sbss .sbss.* COMMON) . = ALIGN(16); __bss_end = .; } > RAM
    . = ALIGN(16);
    . += 8K;
    __stack_top = .;
}
