/* Objetivo propio para riscv-arch-test 2.7.4 (rv32i_m/I) sobre el hart RV32 (RFC-EXP-00018, H2.4): sin
   trap handler (los traps llegan en H3), un solo hart en modo M, parada por SYSCON (0x5555 apaga) y la
   región de firma delimitada por begin_signature / end_signature, que el driver lee de la RAM y compara
   con las firmas de referencia del repositorio. */
#ifndef _COMPLIANCE_MODEL_H
#define _COMPLIANCE_MODEL_H

#define RVMODEL_DATA_SECTION                                                    \
        .align 8; .global begin_regstate; begin_regstate:                       \
        .word 128;                                                              \
        .align 8; .global end_regstate; end_regstate:                           \
        .word 4;

#define RVMODEL_HALT                                                            \
        li t5, 0x11100000;                                                      \
        li t6, 0x5555;                                                          \
        sw t6, 0(t5);                                                           \
self_loop:  j self_loop;

#define RVMODEL_BOOT

#define RVMODEL_DATA_BEGIN                                                      \
  .align 4; .global begin_signature; begin_signature:

#define RVMODEL_DATA_END                                                        \
  .align 4; .global end_signature; end_signature:                               \
  RVMODEL_DATA_SECTION

#define RVMODEL_IO_INIT
#define RVMODEL_IO_WRITE_STR(_SP, _STR)
#define RVMODEL_IO_CHECK()
#define RVMODEL_IO_ASSERT_GPR_EQ(_SP, _R, _I)
#define RVMODEL_IO_ASSERT_SFPR_EQ(_F, _R, _I)
#define RVMODEL_IO_ASSERT_DFPR_EQ(_D, _R, _I)
#define RVMODEL_SET_MSW_INT
#define RVMODEL_CLEAR_MSW_INT
#define RVMODEL_CLEAR_MTIMER_INT
#define RVMODEL_CLEAR_MEXT_INT

#endif
