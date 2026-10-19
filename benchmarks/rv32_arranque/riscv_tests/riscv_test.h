/* Entorno propio para riscv-tests rv32ui sobre el hart RV32 (RFC-EXP-00018, H2.3; sustituye a env/p):
   sin trap handler (los traps llegan en H3), un solo hart en modo M, paso por SYSCON (0x5555 apaga)
   y fallo escribiendo el número de test (TESTNUM = gp) por el UART y 0x7777 (reinicio) en SYSCON. */
#ifndef RV32_HART_RISCV_TEST_H
#define RV32_HART_RISCV_TEST_H

#define TESTNUM gp

#define RVTEST_RV32U
#define RVTEST_RV64U

#define RVTEST_CODE_BEGIN                                                       \
        .section .text.init;                                                    \
        .align  6;                                                              \
        .globl _start;                                                          \
_start:                                                                         \
        li TESTNUM, 0;

#define RVTEST_CODE_END                                                         \
        unimp

#define RVTEST_PASS                                                             \
        fence;                                                                  \
        li TESTNUM, 1;                                                          \
        li t5, 0x11100000;                                                      \
        li t6, 0x5555;                                                          \
        sw t6, 0(t5);                                                           \
1:      j 1b;

#define RVTEST_FAIL                                                             \
        fence;                                                                  \
1:      beqz TESTNUM, 1b;                                                       \
        li t5, 0x10000000;                                                      \
        sb TESTNUM, 0(t5);                                                      \
        li t5, 0x11100000;                                                      \
        li t6, 0x7777;                                                          \
        sw t6, 0(t5);                                                           \
1:      j 1b;

#define EXTRA_DATA

#define RVTEST_DATA_BEGIN                                                       \
        EXTRA_DATA                                                              \
        .align 4;                                                               \
        .global begin_signature;                                                \
begin_signature:

#define RVTEST_DATA_END                                                         \
        .align 4;                                                               \
        .global end_signature;                                                  \
end_signature:

#endif
