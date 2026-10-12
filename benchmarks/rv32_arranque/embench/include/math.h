/* Cabecera mínima para compilar Embench en freestanding. No hay implementación: los benchmarks que
   usan coma flotante no se enlazan en rv32i (sin compiler-rt ni libm para riscv32). */
#ifndef RV32_MINI_MATH_H
#define RV32_MINI_MATH_H
double sqrt(double x);
double cos(double x);
double sin(double x);
double pow(double x, double y);
double fabs(double x);
double exp(double x);
double log(double x);
double floor(double x);
double ceil(double x);
#endif
