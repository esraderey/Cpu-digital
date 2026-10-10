# Arranque bare-metal: pila, main() y apagado por SYSCON (0x11100000 = 0x5555).
    .section .text.start, "ax"
    .globl _start
_start:
    la      sp, _stack_top
    call    main
    li      t0, 0x11100000
    li      t1, 0x5555
    sw      t1, 0(t0)
1:  j       1b
