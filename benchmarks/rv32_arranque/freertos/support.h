/* Soporte mínimo del puerto propio de FreeRTOS (H3.5): UART 8250 de la plataforma, apagado por SYSCON y las tres funciones
 * de memoria que el núcleo usa (sin libc). */
#ifndef SUPPORT_H
#define SUPPORT_H

#include <stddef.h>
#include <stdint.h>

void uart_putc( char c );
void uart_puts( const char * text );
void uart_putu( uint32_t value );
void syscon_poweroff( void ) __attribute__( ( noreturn ) );
void * memset( void * destination, int value, size_t count );
void * memcpy( void * destination, const void * source, size_t count );
void * memmove( void * destination, const void * source, size_t count );

#endif /* SUPPORT_H */
