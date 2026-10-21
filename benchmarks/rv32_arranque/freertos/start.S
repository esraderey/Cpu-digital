/* Arranque del puerto propio de FreeRTOS (H3.5): pila, .bss a cero, mtvec al manejador de traps de FreeRTOS (modo directo)
 * y main. La convención de la plataforma (a0 = hartid, a1 = DTB) no se usa: FreeRTOS no lee el DTB. */
    .section .text.start
    .global _start
_start:
    la sp, __stack_top
    la t0, __bss_start
    la t1, __bss_end
1:  bgeu t0, t1, 2f
    sw zero, 0(t0)
    addi t0, t0, 4
    j 1b
2:  la t0, freertos_risc_v_trap_handler
    csrw mtvec, t0
    call main
3:  j 3b
