/* Entorno propio para riscv-tests rv32mi sobre el hart RV32 (RFC-EXP-00018, H3.1; sustituye a env/p, que el zip de
   riscv-tests no trae porque es un submódulo). Un solo hart con modos M y U y sin modo S: trap_vector despacha a
   mtvec_handler si el test lo define y si no falla con TESTNUM | 1337; RVTEST_RV32M/RV64M ponen MPP = M antes del mret
   inicial (el test corre en M) y RVTEST_RV32U/RV64U lo dejan en U. Paso: 0x5555 en SYSCON (apagado); fallo: el número
   de test (TESTNUM = gp) por el UART y 0x7777 en SYSCON (reinicio), como en rv32ui. Sin PMP, satp, medeleg ni mideleg
   (no existen en la plataforma): no se inicializan. Las constantes de encoding.h que los tests usan se definen aquí. */
#ifndef RV32_HART_RISCV_TEST_MI_H
#define RV32_HART_RISCV_TEST_MI_H

#define TESTNUM gp

/* encoding.h (valores del manual privilegiado): solo lo que usan los tests de la lista. */
#define MSTATUS_SIE   0x00000002
#define MSTATUS_MIE   0x00000008
#define MSTATUS_SPIE  0x00000020
#define MSTATUS_MPIE  0x00000080
#define MSTATUS_SPP   0x00000100
#define MSTATUS_VS    0x00000600
#define MSTATUS_MPP   0x00001800
#define MSTATUS_FS    0x00006000
#define MSTATUS_MPRV  0x00020000
#define MSTATUS_SUM   0x00040000
#define MSTATUS_MXR   0x00080000
#define MSTATUS_TVM   0x00100000
#define MSTATUS_TW    0x00200000
#define MSTATUS_TSR   0x00400000
#define SSTATUS_SIE   MSTATUS_SIE
#define SSTATUS_SPIE  MSTATUS_SPIE
#define SSTATUS_SPP   MSTATUS_SPP
#define SSTATUS_FS    MSTATUS_FS
#define SSTATUS_VS    MSTATUS_VS
#define SSTATUS_SUM   MSTATUS_SUM
#define SSTATUS_MXR   MSTATUS_MXR
#define MIP_SSIP      (1 << 1)
#define MIP_MSIP      (1 << 3)
#define MIP_STIP      (1 << 5)
#define MIP_MTIP      (1 << 7)
#define MIP_SEIP      (1 << 9)
#define MIP_MEIP      (1 << 11)
#define PRV_U 0
#define PRV_S 1
#define PRV_M 3
#define CAUSE_MISALIGNED_FETCH     0x0
#define CAUSE_FETCH_ACCESS         0x1
#define CAUSE_ILLEGAL_INSTRUCTION  0x2
#define CAUSE_BREAKPOINT           0x3
#define CAUSE_MISALIGNED_LOAD      0x4
#define CAUSE_LOAD_ACCESS          0x5
#define CAUSE_MISALIGNED_STORE     0x6
#define CAUSE_STORE_ACCESS         0x7
#define CAUSE_USER_ECALL           0x8
#define CAUSE_SUPERVISOR_ECALL     0x9
#define CAUSE_MACHINE_ECALL        0xb

/* Nombres antiguos que algunos tests usan y que el ensamblador de clang puede no aceptar. */
#define scall ecall
#define sbreak ebreak

/* El cuerpo va en las macros RV32: los envoltorios rv32mi hacen «#define RVTEST_RV64M RVTEST_RV32M» (y lo mismo con
   RVTEST_RV64S), así que RV32 no puede definirse a través de RV64 sin volverse recursivo. */
#define RVTEST_RV32U .macro init; .endm
#define RVTEST_RV32M .macro init; li a0, MSTATUS_MPP; csrs mstatus, a0; .endm
#define RVTEST_RV32S RVTEST_RV32M
#define RVTEST_RV64U RVTEST_RV32U
#define RVTEST_RV64M RVTEST_RV32M
#define RVTEST_RV64S RVTEST_RV32M

/* mtvec_handler: si el test no lo define, link.ld lo PROVIDE a 0 (LLVM no admite el «.weak» de env/p seguido del
   «.global» del test). */
#define RVTEST_CODE_BEGIN                                                       \
        .section .text.init;                                                    \
        .align  6;                                                              \
        .globl _start;                                                          \
_start:                                                                         \
        j reset_vector;                                                         \
        .align 2;                                                               \
trap_vector:                                                                    \
        la t5, mtvec_handler;                                                   \
        beqz t5, 1f;                                                            \
        jr t5;                                                                  \
1:      ori TESTNUM, TESTNUM, 1337;                                             \
        li t5, 0x10000000;                                                      \
        sb TESTNUM, 0(t5);                                                      \
        li t5, 0x11100000;                                                      \
        li t6, 0x7777;                                                          \
        sw t6, 0(t5);                                                           \
2:      j 2b;                                                                   \
reset_vector:                                                                   \
        li TESTNUM, 0;                                                          \
        la t0, trap_vector;                                                     \
        csrw mtvec, t0;                                                         \
        csrwi mstatus, 0;                                                       \
        init;                                                                   \
        la t0, 1f;                                                              \
        csrw mepc, t0;                                                          \
        csrr a0, mhartid;                                                       \
        mret;                                                                   \
1:

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
