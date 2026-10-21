/* Soporte mínimo del puerto propio de FreeRTOS (H3.5): UART en 0x10000000 (registro de datos en +0; el transmisor siempre
 * está libre), SYSCON en 0x11100000 (0x5555 apaga) y memset/memcpy/memmove por bytes. */
#include "support.h"

#define UART_DATA ( *( volatile uint8_t * ) 0x10000000UL )
#define SYSCON    ( *( volatile uint32_t * ) 0x11100000UL )

void uart_putc( char c )
{
    UART_DATA = ( uint8_t ) c;
}

void uart_puts( const char * text )
{
    while( *text )
    {
        uart_putc( *text++ );
    }
}

void uart_putu( uint32_t value )
{
    char digits[ 10 ];
    int count = 0;
    do
    {
        digits[ count++ ] = ( char ) ( '0' + value % 10 );
        value /= 10;
    } while( value );
    while( count )
    {
        uart_putc( digits[ --count ] );
    }
}

void syscon_poweroff( void )
{
    SYSCON = 0x5555;
    for( ; ; )
    {
    }
}

void * memset( void * destination, int value, size_t count )
{
    volatile uint8_t * bytes = destination;   /* volatile: que clang no lo convierta en una llamada a sí mismo */
    while( count-- )
    {
        *bytes++ = ( uint8_t ) value;
    }
    return destination;
}

void * memcpy( void * destination, const void * source, size_t count )
{
    volatile uint8_t * to = destination;
    const volatile uint8_t * from = source;
    while( count-- )
    {
        *to++ = *from++;
    }
    return destination;
}

void * memmove( void * destination, const void * source, size_t count )
{
    volatile uint8_t * to = destination;
    const volatile uint8_t * from = source;
    if( to < from )
    {
        while( count-- )
        {
            *to++ = *from++;
        }
    }
    else
    {
        to += count;
        from += count;
        while( count-- )
        {
            *--to = *--from;
        }
    }
    return destination;
}
