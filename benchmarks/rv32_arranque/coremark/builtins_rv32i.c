/* Rutinas que clang emite para rv32i (sin extensión M): multiplicación y
   división de 32 bits por desplazamiento y suma/resta. Semántica RISC-V:
   DIV por cero da -1, DIVU por cero da 0xFFFFFFFF, REM por cero da el
   dividendo; INT_MIN / -1 da INT_MIN y su resto 0. */
typedef unsigned int u32;
typedef int s32;

u32 __mulsi3(u32 a, u32 b)
{
    u32 r = 0;
    while (b) {
        if (b & 1) r += a;
        a <<= 1;
        b >>= 1;
    }
    return r;
}

u32 __udivmodsi4(u32 n, u32 d, u32 *rem)
{
    u32 q = 0, r = 0;
    if (d == 0) { if (rem) *rem = n; return 0xFFFFFFFFu; }
    for (int i = 31; i >= 0; i--) {
        r = (r << 1) | ((n >> i) & 1);
        if (r >= d) { r -= d; q |= 1u << i; }
    }
    if (rem) *rem = r;
    return q;
}

u32 __udivsi3(u32 n, u32 d) { return __udivmodsi4(n, d, 0); }
u32 __umodsi3(u32 n, u32 d) { u32 r; __udivmodsi4(n, d, &r); return r; }

s32 __divsi3(s32 n, s32 d)
{
    if (d == 0) return -1;
    if (n == (s32)0x80000000 && d == -1) return n;
    u32 un = n < 0 ? 0u - (u32)n : (u32)n;
    u32 ud = d < 0 ? 0u - (u32)d : (u32)d;
    u32 q = __udivmodsi4(un, ud, 0);
    return ((n < 0) != (d < 0)) ? -(s32)q : (s32)q;
}

s32 __modsi3(s32 n, s32 d)
{
    if (d == 0) return n;
    if (n == (s32)0x80000000 && d == -1) return 0;
    u32 un = n < 0 ? 0u - (u32)n : (u32)n;
    u32 ud = d < 0 ? 0u - (u32)d : (u32)d;
    u32 r;
    __udivmodsi4(un, ud, &r);
    return n < 0 ? -(s32)r : (s32)r;
}
