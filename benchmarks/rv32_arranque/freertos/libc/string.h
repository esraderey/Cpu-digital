/* string.h mínimo del puerto propio de FreeRTOS (H3.5): las funciones que el núcleo usa, definidas en support.c. */
#ifndef STRING_H
#define STRING_H
#include <stddef.h>
void * memset( void * destination, int value, size_t count );
void * memcpy( void * destination, const void * source, size_t count );
void * memmove( void * destination, const void * source, size_t count );
#endif
