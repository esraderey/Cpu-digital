/* Soporte de placa de Embench para el hart RV32 de RFC-EXP-00018 (y mini-rv32ima): el disparador
   lee el CSR cycle (que en estos emuladores cuenta instrucciones) y publica la diferencia por el
   UART de 0x10000000 como «ticks=N». Escrito desde el contrato de support.h; no copia código de
   Embench. */
#include "support.h"

static unsigned int start_cycles;

static void uart_putc(char c)
{
    *(volatile unsigned int *)0x10000000u = (unsigned char)c;
}

static void uart_puts(const char *s)
{
    while (*s)
        uart_putc(*s++);
}

static void uart_put_uint(unsigned int value)
{
    char buffer[11];
    int i = 10;
    buffer[i] = '\0';
    do {
        buffer[--i] = (char)('0' + value % 10u);
        value /= 10u;
    } while (value);
    uart_puts(buffer + i);
}

void initialise_board(void)
{
}

void __attribute__((noinline)) start_trigger(void)
{
    __asm__ volatile("csrr %0, cycle" : "=r"(start_cycles) : : "memory");
}

void __attribute__((noinline)) stop_trigger(void)
{
    unsigned int now;
    __asm__ volatile("csrr %0, cycle" : "=r"(now) : : "memory");
    uart_puts("ticks=");
    uart_put_uint(now - start_cycles);
    uart_putc('\n');
}
