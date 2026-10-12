/* Cabecera mínima para compilar Embench en freestanding. */
#ifndef RV32_MINI_STDLIB_H
#define RV32_MINI_STDLIB_H
#include <stddef.h>
int abs(int x);
void abort(void);
void exit(int status);
#endif
