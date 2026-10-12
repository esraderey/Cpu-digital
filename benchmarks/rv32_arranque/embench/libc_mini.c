/* Mínimo de libc que los benchmarks enteros de Embench referencian además de lo que ya aporta
   ../coremark/support.c (memcpy, memmove, memset, memcmp, strlen). printf solo aparece en rutas de
   depuración: aquí no imprime nada. */
typedef unsigned int size_t;

int strcmp(const char *a, const char *b)
{
    while (*a && *a == *b) {
        a++;
        b++;
    }
    return (unsigned char)*a - (unsigned char)*b;
}

int strncmp(const char *a, const char *b, size_t n)
{
    for (; n; n--, a++, b++) {
        if (*a != *b)
            return (unsigned char)*a - (unsigned char)*b;
        if (!*a)
            return 0;
    }
    return 0;
}

char *strcpy(char *dest, const char *src)
{
    char *d = dest;
    while ((*d++ = *src++) != '\0') {
    }
    return dest;
}

char *strncpy(char *dest, const char *src, size_t n)
{
    size_t i = 0;
    for (; i < n && src[i]; i++)
        dest[i] = src[i];
    for (; i < n; i++)
        dest[i] = '\0';
    return dest;
}

char *strchr(const char *s, int c)
{
    for (;; s++) {
        if (*s == (char)c)
            return (char *)s;
        if (!*s)
            return 0;
    }
}

int abs(int x)
{
    return x < 0 ? -x : x;
}

int printf(const char *format, ...)
{
    (void)format;
    return 0;
}

int puts(const char *s)
{
    (void)s;
    return 0;
}

int putchar(int c)
{
    return c;
}

void abort(void)
{
    for (;;) {
    }
}

void exit(int status)
{
    (void)status;
    for (;;) {
    }
}
