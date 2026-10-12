/* Cabecera mínima para compilar Embench en freestanding: printf no imprime (solo aparece en rutas
   de depuración de los benchmarks). */
#ifndef RV32_MINI_STDIO_H
#define RV32_MINI_STDIO_H
#include <stddef.h>
int printf(const char *format, ...);
int puts(const char *s);
int putchar(int c);
#endif
