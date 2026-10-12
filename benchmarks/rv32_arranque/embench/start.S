# Arranque bare-metal de Embench: pila, main() y apagado por SYSCON (0x11100000 = 0x5555).
# main devuelve !correct (0 = verificado): se publica por el UART como «V0» o «V1» antes de apagar.
    .section .text.start, "ax"
    .globl _start
_start:
    la      sp, _stack_top
    call    main
    li      t0, 0x10000000
    li      t1, 'V'
    sw      t1, 0(t0)
    addi    a0, a0, '0'
    sw      a0, 0(t0)
    li      t1, '\n'
    sw      t1, 0(t0)
    li      t0, 0x11100000
    li      t1, 0x5555
    sw      t1, 0(t0)
1:  j       1b
