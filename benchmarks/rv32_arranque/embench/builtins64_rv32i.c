/* Rutinas de 64 bits que clang emite en rv32i (sin compiler-rt para riscv32): producto, división,
   resto y desplazamientos. Las de 32 bits están en ../coremark/builtins_rv32i.c. Escritas desde la
   definición; su oráculo son los verify_benchmark de Embench (aha-mont64 las usa a fondo). */
typedef unsigned int u32;
typedef int s32;
typedef unsigned long long u64;
typedef long long s64;

static u64 mul32x32(u32 x, u32 y)
{
    u32 xl = x & 0xFFFFu, xh = x >> 16, yl = y & 0xFFFFu, yh = y >> 16;
    u64 ll = (u64)(xl * yl);
    u64 lh = (u64)(xl * yh);
    u64 hl = (u64)(xh * yl);
    u64 hh = (u64)(xh * yh);
    return ll + (lh << 16) + (hl << 16) + (hh << 32);
}

u64 __muldi3(u64 a, u64 b)
{
    u32 a0 = (u32)a, a1 = (u32)(a >> 32), b0 = (u32)b, b1 = (u32)(b >> 32);
    u64 low = mul32x32(a0, b0);
    u32 cross = a0 * b1 + a1 * b0;   /* solo importan los 32 bits bajos del término alto */
    return low + ((u64)cross << 32);
}

u64 __udivmoddi4(u64 n, u64 d, u64 *rem)
{
    if (d == 0) {
        if (rem)
            *rem = n;
        return ~(u64)0;
    }
    u64 q = 0, r = 0;
    for (int i = 63; i >= 0; i--) {
        r = (r << 1) | ((n >> i) & 1u);
        if (r >= d) {
            r -= d;
            q |= (u64)1 << i;
        }
    }
    if (rem)
        *rem = r;
    return q;
}

u64 __udivdi3(u64 n, u64 d)
{
    return __udivmoddi4(n, d, 0);
}

u64 __umoddi3(u64 n, u64 d)
{
    u64 r;
    __udivmoddi4(n, d, &r);
    return r;
}

s64 __divdi3(s64 n, s64 d)
{
    if (d == 0)
        return -1;
    if (n == (s64)0x8000000000000000ull && d == -1)
        return n;
    u64 un = n < 0 ? (u64)(-n) : (u64)n;
    u64 ud = d < 0 ? (u64)(-d) : (u64)d;
    u64 q = __udivmoddi4(un, ud, 0);
    return ((n < 0) != (d < 0)) ? -(s64)q : (s64)q;
}

s64 __moddi3(s64 n, s64 d)
{
    if (d == 0)
        return n;
    if (n == (s64)0x8000000000000000ull && d == -1)
        return 0;
    u64 un = n < 0 ? (u64)(-n) : (u64)n;
    u64 ud = d < 0 ? (u64)(-d) : (u64)d;
    u64 r;
    __udivmoddi4(un, ud, &r);
    return n < 0 ? -(s64)r : (s64)r;
}

u64 __ashldi3(u64 a, int b)
{
    u32 lo = (u32)a, hi = (u32)(a >> 32);
    b &= 63;
    if (b == 0)
        return a;
    if (b >= 32)
        return (u64)(lo << (b - 32)) << 32;
    return ((u64)((hi << b) | (lo >> (32 - b))) << 32) | (u64)(lo << b);
}

u64 __lshrdi3(u64 a, int b)
{
    u32 lo = (u32)a, hi = (u32)(a >> 32);
    b &= 63;
    if (b == 0)
        return a;
    if (b >= 32)
        return (u64)(hi >> (b - 32));
    return ((u64)(hi >> b) << 32) | (u64)((lo >> b) | (hi << (32 - b)));
}

s64 __ashrdi3(s64 a, int b)
{
    u32 lo = (u32)a;
    s32 hi = (s32)((u64)a >> 32);
    b &= 63;
    if (b == 0)
        return a;
    if (b >= 32)
        return (s64)(hi >> (b - 32));   /* la extensión de signo del alto rellena el resto */
    return ((s64)(hi >> b) << 32) | (s64)(u64)((lo >> b) | ((u32)hi << (32 - b)));
}
