/* Cabecera mínima para compilar Embench en freestanding: las aserciones no se evalúan. */
#ifndef RV32_MINI_ASSERT_H
#define RV32_MINI_ASSERT_H
#define assert(expression) ((void)0)
#endif
