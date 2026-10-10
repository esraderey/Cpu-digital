# Mapa del Territorio — expedición RFC-EXP-00018 (v1, verificado)

Fecha: 2026-10-10. Toda afirmación lleva su nivel de evidencia (V2 = fuente abierta y leída; V3 = contrastada en dos fuentes, una primaria) y su referencia [R#], o [L#] si es lectura del repositorio local. Nada por debajo de V2. Los huecos de la frontera (§8) están redactados como cota del barrido, no como inexistencia.

Verificación (F3): un verificador ciego abrió las fuentes y buscó la cita literal. S1–S5: 73 anclas muestreadas (22–35 % de cada reporte, las 38 críticas incluidas), 73 confirmadas, 0 falsas. S6: 68 de 70 anclas verificadas, 63 confirmadas, 3 con cita truncada o reformulada (corregidas aquí) y 2 no verificables (retiradas del mapa). S1b: 3 anclas comprobadas por el director, 3 exactas.

## 0. Pregunta y definición operativa

Pregunta: ¿cómo acercar Tramoya VM32 lo más posible a un procesador real, aceptando como objetivo un procesador de hace unas cinco generaciones?

Definición operativa de «procesador real de hace cinco generaciones» (propuesta del director, ajustable): procesador de 32 bits de la clase 1988–1999 (MIPS R3000, i486, Pentium P5, ARM7TDMI/ARM920T) o su equivalente abierto moderno (RV32IMA con especificación privilegiada). Rasgos: ISA con compilador existente, modos de privilegio, traps, temporizador, MMU o protección de memoria, y capacidad de arrancar un sistema operativo real.

## 1. Punto de partida: Tramoya VM32 hoy (HECHO, lectura del repositorio)

- [L1] `VM32.md`, `README.md`: ISA propia de 79 instrucciones de formato fijo de 4 palabras de 32 bits (16 bytes por instrucción), 16 registros, memoria direccionada por palabra (1 048 576 palabras por defecto, 16 777 216 máximo), gas por instrucción, rollback por instrucción, snapshots paginados, capacidades del host, fibras cooperativas, FPU float32, coprocesador TNU de 18 instrucciones que ejecuta llama2.c. Rendimiento medido: ~0,55 M instr/s el intérprete, ~5,5 M instr/s en bucles compilados, 3,2× en el raycaster de referencia.
- [L2] `cpu_digital/vm32.py` (rama `main`): `_core_checkpoint` copia registros y una tupla de 19 campos en cada instrucción; la pila es una lista de Python fuera de la memoria y `R15` es solo su profundidad; el heap es un puntero que solo crece (`_sys_alloc`); las interrupciones solo se despachan en la fibra 0; no hay límite de reloj de pared, solo gas; `write_memory` lleva un journal para el rollback.
- [L3] `cpu_digital/memory.py`: memoria paginada de 4 096 palabras por página sobre `array('i')`, páginas materializadas bajo demanda, lectura por diccionario.
- [L4] `cpu_digital/vm32_loops.py`: el acelerador compila bucles calientes a funciones Python con la misma semántica que el intérprete; equivalencia verificada con fuzzing diferencial (1 200 bucles) y con el juego con y sin acelerador.
- [L5] `tests/`, `.github/workflows/ci.yml`: 220 pruebas unittest, CI en Python 3.10–3.13 con ruff; anclas de regresión por extensión.
- [L6] Saltos absolutos en el `.tvm` sin reubicaciones; cadenas a una palabra por carácter; syscall del chip expande cada byte a una palabra (`VM32.md`, `vm32_assembler.py`).

## 2. Sector S1 — ISAs reales de 32 bits y sus herramientas

### 2.1 RISC-V RV32 (la ISA mejor documentada del barrido)

- [V2] RV32I tiene 40 instrucciones únicas, todas de 32 bits fijos, con cuatro formatos núcleo R/I/S/U y dos variantes B/J; IALIGN=32 [R1].
- [V2] La especificación recomienda los opcodes custom-0..custom-3 para extensiones propias y promete que las futuras extensiones estándar no los usarán; los opcodes marcados «reservados» deben evitarse [R2]. «G» = IMAFD + Zicsr + Zifencei [R2].
- [V2] Privilegiada v1.13: tres niveles U/S/M; solo M es obligatorio; combinaciones admitidas M, M+U, M+S+U; enrutado flexible de traps [R3]. CSRs mínimos: `mtvec` siempre implementado; `mcause` y `mepc` se escriben al entrar en un trap; `MRET` retorna; sin S-mode no existen `medeleg`/`mideleg` [R4].
- [V2] Sv32: espacio virtual de 32 bits, páginas de 4 KiB, `satp` con el PPN de la tabla raíz, `SFENCE.VMA`; CSRs `sstatus`, `stvec`, `sepc`, `scause`, `stval` [R5].
- [V2] Toolchain Rust: `riscv32i/im/imc/imac/imafc-unknown-none-elf` son Tier 2 («garantizado que compila») [R6]. GCC y LLVM: pendiente de S1b.
- [V2] Conformidad: `riscv-tests` (unit tests autocomprobantes, suites rv32ui y rv32si, timeout = fallo) [R7]; `riscv-arch-test` con el framework ACT4, que sustituye a RISCOF (deprecado), calcula resultados esperados con el modelo de referencia Sail e incluye pruebas privilegiadas; la propia página aclara que no son pruebas de verificación completas [R8].
- [V3] Bibliotecas C: picolibc (derivada de newlib, licencias BSD, semihosting integrado, stdio que solo requiere get/put de un carácter) y musl tienen RV32 [R9][R10].

### 2.2 Compiladores para RISC-V

- [V2] GCC añadió RISC-V en la serie GCC 7 [R91]; acepta `-mabi=ilp32` y cadenas `-march=rv32…` con base i/e/g y extensiones (ejemplos literales `rv32g`, `rv32imaf`) [R92]. LLVM 9.0.0 (2019) declaró el objetivo RISC-V no experimental, con codegen completo RV32I y RV64I más MAFDC [R93]; RISCV, ARM, Mips y X86 figuran en la lista oficial de objetivos de LLVM (agosto 2025) [R94].

### 2.3 Las otras cuatro ISAs

| ISA | Especificación | Toolchain | Coprocesador | Conformidad pública | Nivel |
|---|---|---|---|---|---|
| ARMv4T (ARM7TDMI) / ARMv5TE | TRM DDI 0210C descargable («Non-Confidential», reproducción con permiso escrito); licencia del ARM ARM no abierta | GCC `-march=armv4t/armv5te`, `-mcpu=arm7tdmi`, `-marm/-mthumb` [R95]; Rust armv4t/thumbv4t/armv5te Tier 3 [R6]; LLVM oficial [R94] | hasta 16 coprocesadores; CP15 control, CP14 depuración, CP7–CP4 libres; CDP, LDC, STC, MRC, MCR [R96] | no buscada en el barrido | V2 |
| MIPS I / MIPS32 (R3000) | MIPS Open se cerró en 2019; MIPS declaró en 2021 el fin del desarrollo de la arquitectura [R97] | GCC `-march=mips1/mips32`, `r3000` [R98]; Rust Tier 3 [R6]; LLVM oficial [R94] | formatos R/I/J de 32 bits; COP2 y COP3 opcionales definidos por la implementación [R97]; CP0 sistema, hasta 4 coprocesadores [R11] | no localizada (consulta asesina ejecutada) [S1b] | V2 |
| IA-32 (i486 / Pentium) | Intel SDM descargable (versión 093, 2026-10-09), sin licencia declarada en la página [R99] | GCC `-march=i486`, `-march=pentium` (sin MMX) [R100]; Rust i586 Tier 2 [R6]; LLVM oficial [R94] | opcodes ESC 11011B para 80x87, bit EM [R12] | no localizada (consulta asesina ejecutada) [S1b] | V2 |
| m68k (68000–68030) | M68000PRM (Motorola 1992) descargable de NXP sin autenticación [R101] | GCC `-m68000/-m68020/-m68030`, `-mhard-float` [R102]; Rust Tier 3 [R6]; LLVM: no aparece en la lista oficial [R94] | línea F (1111) con cpID de 3 bits; cpBcc/cpDBcc/cpGEN/cpScc/cpTRAPcc en 68020/68030 [R101] | no localizada (consulta asesina ejecutada) [S1b] | V2 |

- [V3] picolibc y musl cubren ARM de 32 bits, MIPS, i386 y m68k [R9][R10].
- Conteos de instrucciones (cotas inferiores, recuento propio del explorador sobre los manuales, sin cifra oficial): ARM7TDMI ≥38 mnemónicos ARM y ≥33 Thumb más 14 saltos condicionales [R96]; m68k ≥81 mnemónicos [R101]. x86 y MIPS: no obtenidos.
- Cota del barrido: solo RISC-V tiene una suite de conformidad pública con modelo de referencia ejecutable (ACT4 + Sail) [R8]; para MIPS, IA-32 y m68k no se localizó ninguna pese a consultas dirigidas [S1b].

## 3. Sector S2 — Emuladores funcionales y rendimiento por técnica

| Técnica | Ejemplo | Cifra publicada | Nivel |
|---|---|---|---|
| ISS en Python alojado en un intérprete Python (RPython sin traducir) | Pydgin RISC-V | ~100 mil instr/s | V2 [R17] |
| Python puro, CPU de 8 bits | 6502 en Python | ≈0,5 MHz (CPython 3.9), ≈0,85 MHz (3.11), ≈2 MHz (PyPy) | V2 [R25] |
| RPython traducido a C, intérprete sin JIT | Pydgin RISC-V | 10 MIPS | V2 [R17] |
| Intérprete C/C++ optimizado (caché de instrucciones predecodificadas, TLB software, bucle desenrollado) | Spike | 40–200 MIPS (2016) | V2 [R17][R23] |
| Intérprete C con caché de bloques básicos | ISS JKU (DATE 2025) | hasta 406,97 MIPS, 1,65× Spike | V2 [R22] |
| RPython con JIT de metatrazas | Pydgin RISC-V / SMIPS | 90–750 MIPS / 87–761 MIPS | V2 [R17][R16] |
| Sail → RPython (AOT + JIT) | Pydrofoil | 233–255× el simulador C de Sail; 26,7× más lento que QEMU | V2 [R19] |
| Traducción dinámica (bloques traducidos y encadenados) | QEMU TCG | 240–1220 MIPS en ARMv5 | V2 [R15][R16] |
| Simulador detallado | gem5 atómico | 2–3 MIPS | V2 [R18] |

- [V3] Un intérprete RISC-V sin traducción dinámica pero con caché de predecodificación o de bloques rinde de decenas a ~400 MIPS [R17][R22].
- [V3] Un ISS en Python restringido con JIT de trazas supera en uno o dos órdenes de magnitud a simuladores de referencia en C no optimizados [R16][R19].
- [V2] mini-rv32ima: rv32ima + Zifencei + Zicsr (S/U parcial), CLINT y MMIO, ~400 líneas de C, ~450 CoreMark en un portátil (≈½ de QEMU); su compilación de riscv-tests figura como rota [R13]. rv32emu: intérprete más JIT por niveles, arranca un kernel Linux RV32, se prueba con RISCOF/arch-tests [R20]. riscv-rust: Rust a WebAssembly, ejecuta Linux y xv6 [R21].
- [V2] Técnicas de Pydgin: decodificador generado de máscaras de bits, una función Python por instrucción, pistas al JIT (PC constante → instrucción constante para código no automodificable), límite de traza subido de 6 000 a 400 000; RISC-V añadido en nueve días; ARMv5 y SMIPS en cuatro persona-mes [R16][R17][R18].
- [V2] QEMU, según los autores de Pydgin, solo mantiene el estado arquitectónico fiel por bloque, no por instrucción [R16].
- [V2] PyBoy (Game Boy en Python, etiquetas cython y pypy): ×124 a ×395 tiempo real; no publica instr/s [R24].
- Verificación en este sector: riscv-tests autocomprobantes [R7]; arranque de Linux/xv6 como criterio informal [R13][R21]; Pydgin se prueba con herramientas de Python antes de traducir [R16].

## 4. Sector S3 — Microarquitectura de la generación objetivo y modelos de tiempo

### 4.1 Los procesadores de la banda 1988–1999

| CPU (año) | Pipeline | Cachés | Otros | Nivel |
|---|---|---|---|---|
| MIPS R3000 (1988) | 5 etapas | externas, hasta 256 KB cada una | TLB en el coprocesador de control; FPU R3010 externa opcional | V2 [R29] |
| Intel 486 (1989) | 5 etapas de un ciclo | L1 unificada 8 KB | FPU integrada (salvo SX); 1 CPI ideal, ~1,8 medido | V2 [R27] |
| Pentium P5 (1993) | 2 pipelines en orden (U cualquier instrucción, V las simples), 5 etapas enteras, FPU de 8 | L1 8 KB código + 8 KB datos, 2 vías, write-back | predicción dinámica con BTB | V2 [R28] |
| ARM7TDMI (1994) | (3 etapas: no se obtuvo cita literal) | sin caché separada; von Neumann | ARM 32 bits + Thumb 16 bits | V2 [R31] |
| ARM920T / ARM9TDMI (~2000) | 5 etapas Harvard (Fetch, Decode, Execute, Memory, Write) | 16 KB + 16 KB, línea de 8 palabras | MMU ARMv4 | V2 [R30] |

- [V2] El pipeline RISC clásico IF/ID/EX/MEM/WB, con una instrucción por ciclo como objetivo, bypass para riesgos de datos y ranura de retardo para los saltos, es el modelo canónico (Hennessy y Patterson) [R32].

### 4.2 Coste y fidelidad de los modelos de tiempo

- [V2] gem5: AtomicSimpleCPU estima latencias de caché con accesos atómicos; TimingSimpleCPU se detiene en cada acceso; ambos funcionales en orden sin pipeline detallado; O3CPU es fuera de orden basado en el Alpha 21264 [R33][R34].
- [V2] Sodor: núcleos RISC-V educativos de 1, 2, 3 y 5 etapas; «1 etapa» equivale a un simulador de ISA; memoria scratchpad de un ciclo [R36]. Ripes: visualizador de pipeline RV32IMC/RV64IMC con efecto de la caché [R35].
- [V2] Escalera de coste (Sniper, SC'11): el modelo one-IPC cuenta instrucciones y simula la jerarquía de cachés pero supone predicción perfecta; el modelo de intervalos queda dentro del 25 % de error medio frente a hardware y es 2–3× más lento que one-IPC; activar la I-cache añade ~50 % de tiempo; Sniper llega a 2 MIPS; los simuladores industriales cycle-accurate van a 1–10 kHz [R39].
- [V3] Los simuladores académicos detallados (gem5, MARSS/PTLsim) rinden decenas a centenas de KIPS [R38][R39].
- [V2] ZSim: modelo simple IPC=1 más cachés hasta 90 MIPS por núcleo; OOO detallado 20 MIPS (ralentización ≈200×); valida dentro del 10 % de un Westmere; omite la ejecución en camino erróneo [R38].
- [V2] SMARTS: muestreo estadístico con 0,64 % de error en CPI y 35–60× de aceleración [R40]. Cachegrind: predictores «típicos de 2004», sin TLB ni especulación [R41].

## 5. Sector S4 — Software real que correría

- [V3] Linux mainline compila para RV32 sin MMU con el kernel en modo M: `ARCH_RV32I` depende de `NONPORTABLE`; `RISCV_M_MODE` depende de `!MMU` y selecciona `CLINT_TIMER`; con MMU el kernel va en modo S y usa SBI (`RISCV_SBI` si no es modo M) [R42][R43][R44]. mini-rv32ima lo arranca con CLINT, MMIO, UART y DTB [R13]; un registro de arranque muestra ≈15,75 MiB de RAM [R45]; un port a ESP32-P4 tarda ≈17,9 s de reset a shell [R46].
- [V2] Linux RV32 con MMU: defconfig `qemu_riscv32_virt` en Buildroot desde 2019, glibc mainline y kernel ≥5.4 [R44]; VexRiscv «linux balanced» (RV32IMA, MMU, modo supervisor) es compatible con Linux, Zephyr y FreeRTOS, 1,21 DMIPS/MHz y 2,27 CoreMark/MHz [R47].
- [V2] xv6-riscv upstream exige QEMU `riscv64-softmmu` [R48]; su mapa: UART0 en 0x10000000, VIRTIO0 en 0x10001000, CLINT en 0x02000000, PLIC en 0x0c000000, RAM desde 0x80000000 con 128 MiB [R49]. Existe un port comunitario rv32ia que corre en `qemu-system-riscv32` [R50].
- [V2] Zephyr elige base RV32I/RV32E/RV64I y activa M, A, F, D, C, Zicsr, Zifencei según el devicetree [R51]. FreeRTOS RISC-V exige definir `mtime`/`mtimecmp` o un temporizador propio [R52].
- [V2] uClinux: Linux sin MMU para 68k/ColdFire/ARM7TDMI, fusionado como opción NOMMU desde 2.5.46 [R53]. Linux x86: parches de 2025–2026 retiran el 486 (discrepancia 6.15/7.1 sin resolver) [R54].
- [V2] Cargas de referencia: CoreMark (2 000 B de datos, ≥10 s, E/S y tiempo por capa de portado, sin libc obligatoria) [R55]; Embench IoT (sin SO, libc mínima, ≤64 kB de código y de RAM, ~4 s) [R56]; doomgeneric (cinco funciones de plataforma, WAD) [R57]; Doom en un RV32I sobre FPGA a 15–20 FPS con ≈14 MB de datos [R58].

## 6. Sector S5 — Determinismo, gas, snapshots, reversibilidad y verificación

- [V3] Patrón común: registrar solo las entradas no deterministas y re-simular el resto (QEMU record/replay sobre icount; rr) [R59][R60].
- [V2] QEMU: record/replay exige icount; icount da ejecución determinista sin entradas externas; soporta RISC-V; el disco pasa por blkreplay; la depuración inversa no ejecuta hacia atrás: carga el snapshot más cercano y reejecuta hasta la instrucción buscada; exige al menos un snapshot [R59].
- [V2] rr: ancla a «retired conditional branches», un núcleo, búfer de syscalls; grabación <2× salvo make (7,85×); checkpoints por fork en <10 ms; no en ARM ni en nubes sin contadores [R60].
- [V2] wasmtime: fuel determinista pero más caro; epochs ~10 % de ralentización y no deterministas por basarse en reloj de pared; al agotarse, trap no reanudable o cesión asíncrona [R62]. EVM: coste fijo por opcode (ADD 3, MUL 5) y dinámico para almacenamiento [R63]. eBPF: verificador estático (DAG, poda de estados), límite de 1 millón de instrucciones desde 5.2 (antes 4k/128k), bucles acotados desde 5.3 [R64][R65].
- [V2] Snapshots: QEMU savevm/loadvm comparten la infraestructura de migración (VMState versionado, RAM iterativa, parada de cientos de ms) [R66]; CRIU usa soft-dirty en pagemap para volcados incrementales [R67]; gem5 guarda `cpt.<tick>` y restaura por defecto con CPU atómica [R68]; gdb «record full» guarda 200 000 instrucciones por defecto [R69].
- [V3] Verificar contra una referencia por prueba diferencial es el método común: riscv-dv cosimula con Spike, ovpsim, whisper y sail [R70]; TheHuzz usa modelo dorado (11 bugs, 8 nuevos) [R72]; prueba diferencial QEMU frente a 4 dispositivos ARM (2 774 649 flujos, 155 642 inconsistentes, 4 bugs de QEMU) [R73]. Cascade halla 37 bugs (28 CVE) en 5 CPU RISC-V sin modelo de referencia, por no terminación [R71].

## 7. Sector S6 — Extensiones vectoriales y neuronales; ISA frente a MMIO

- [V2] RVV: 32 registros v0–v31, `vl`/`vtype` por `vsetvli`; subconjuntos Zve* «pensados para procesadores embebidos»; Zve32x y Zve32f llevan todo el conjunto vectorial entero (la única excepción, `vmulh` en EEW=64, solo afecta a Zve64*); Zve32f limita FP a EEW=32; V completa admite elementos de 8 a 64 bits [R74]. El kernel Linux documenta Zve32x «as defined by version 1.0 of the RISC-V Vector extension manual» [R75]. [V3] Zve32f/Zve64f están implementadas en QEMU (parche de SiFive, enero de 2022) y Zve32x/Zve32f en LLVM, aunque LLVM las soporta solo con VLEN≥64 [R74][R76][R77].
- [V2] QEMU implementa RVV con helpers por macro que recorren elemento a elemento de `vstart` a `vl`; agrupar cargas unit-stride bajó un caso de 13,5 s a 1,5 s [R89][R90].
- [V2] Arm Helium (Armv8.1-M): Q0–Q7 solapados con el banco FP, ejecución por beats, elementos de 8 a 32 bits, registro de predicación VPR [R79][R80]. Intel MMX (1997): MM0–MM7 alias de x87, saturación; SSE (1999): XMM0–XMM7 independientes, FP simple [R81][R82].
- [V2] NPU reales: el SDK del Kendryte K210 expone el KPU como un bloque de registros volátiles con FIFO de configuración y DMA (en el archivo leído no aparece ninguna instrucción de CPU para él) [R83]; T-Head MME es una unidad matricial independiente que no reutiliza los registros vectoriales, con ocho registros 2D, `mmaqa`/`fmmacc` y >20 instrucciones de cuantización, tipos de int4 a fp32 [R84]; LLVM implementa en estado experimental un borrador 0.1 de extensiones matriciales RISC-V y la extensión de T-Head XTHeadVdot 1.0.0 [R77]; SiFive VCIX es un coprocesador vectorial por interfaz de instrucciones (LLVM 1.1.0) [R77]. El estado de ratificación de IME/AME no quedó verificado (anclas retiradas).
- [V2] ISA frente a MMIO (Chipyard): RoCC usa instrucciones custom y comparte recursos con la CPU pero exige toolchain propia; MMIO usa registros mapeados y la toolchain estándar con un driver; hasta cuatro aceleradores RoCC por núcleo [R88]. Solo una fuente: sin V3 en esta casilla.

## 8. Frontera: casillas abiertas (cota del barrido, con su consulta asesina ejecutada)

- F1. **Emulador de ISA real de 32 bits en Python puro con cifras publicadas**: no localizado. Las únicas cifras Python son RPython alojado (~100 KIPS) y un 6502 (~0,5–0,85 MHz CPython). Tramoya VM32 ya está por encima (0,55 M instr/s; 5,5 M en bucles compilados) pero con ISA propia [L1][R17][R25].
- F2. **ISA real bajo gas determinista + rollback atómico por instrucción + snapshots + capacidades en un solo sistema**: no localizado. Las piezas existen por separado: fuel de wasmtime, icount/record-replay y savevm de QEMU, gas de la EVM [R59][R62][R63]. Nadie del barrido las junta sobre RV32.
- F3. **Modelo de tiempo (one-IPC + caché + predictor) dentro de un emulador en Python**: no localizado; los costes medidos (intervalos 2–3× sobre one-IPC, I-cache +50 %) son de C++ [R39].
- F4. **Coprocesador vectorial/neuronal determinista bit a bit sobre una ISA real**: no localizado; las vías documentadas son extensión de ISA (RoCC, VCIX, custom-0..3) o dispositivo MMIO (KPU), sin literatura comparativa más allá de Chipyard [R2][R77][R83][R88].
- F5. **Emulador en Python que pase riscv-arch-test/ACT**: no localizado; los que lo intentan (rv32emu) son C [R20][R8].
- F6. **Linux nommu RV32 arrancado por un emulador en Python**: no localizado; en C se hace con ~400 líneas [R13]. Número de instrucciones hasta el prompt: no localizado en el barrido (ningún explorador halló la cifra) [S4].
- F7. **Cifra de sobrecoste de icount/record-replay de QEMU y del fuel de wasmtime**: no localizadas [S5].

## 9. No cubierto por la expedición

clang para `-march=armv4t`, `mips1`, `i486` y `m68020` (solo consta la lista de objetivos oficiales de LLVM); apertura formal de las especificaciones ARM/MIPS/x86/m68k; Unicorn, TinyEMU internos, Bochs/86Box; latencias por instrucción de cada CPU histórica; RAM mínima documentada de Linux RV32 nommu; newlib/libgloss; NEON y Arm SME; literatura ISA frente a MMIO más allá de Chipyard; formato en disco de savevm; DifuzzRTL de primera mano.

## Referencias (numeración provisional; fechas de consulta 2026-10-10)

[R1] RISC-V ISA Manual v20260120, Unprivileged, «RV32I». https://docs.riscv.org/reference/isa/v20260120/unpriv/rv32.html
[R2] Ídem, «RV32/64G Instruction Set Listings». https://docs.riscv.org/reference/isa/v20260120/unpriv/rv-32-64g.html
[R3] RISC-V Privileged, «Introduction». https://docs.riscv.org/reference/isa/v20260120/priv/priv-intro.html
[R4] Ídem, «Machine-Level ISA». https://docs.riscv.org/reference/isa/v20260120/priv/machine.html
[R5] Ídem, «Supervisor-Level ISA». https://docs.riscv.org/reference/isa/v20260120/priv/supervisor.html
[R6] Rust, «Platform Support». https://doc.rust-lang.org/rustc/platform-support.html
[R7] riscv-software-src/riscv-tests. https://github.com/riscv-software-src/riscv-tests
[R8] riscv-non-isa/riscv-arch-test (ACT4). https://github.com/riscv-non-isa/riscv-arch-test
[R9] picolibc/picolibc. https://github.com/picolibc/picolibc
[R10] musl, árbol `arch/`. https://git.musl-libc.org/cgit/musl/tree/arch
[R11] UNSW CS3231, «MIPS R2000/R3000». https://cgi.cse.unsw.edu.au/~cs3231/doc/mips.php
[R12] Intel 80386 Programmer's Reference Manual, §11.1 (copia Yale). https://flint.cs.yale.edu/cs422/readings/i386/s11_01.htm
[R13] cnlohr/mini-rv32ima. https://github.com/cnlohr/mini-rv32ima
[R14] F. Bellard, TinyEMU. https://bellard.org/tinyemu/
[R15] QEMU, «Translator Internals» (TCG). https://www.qemu.org/docs/master/devel/tcg.html
[R16] D. Lockhart, B. Ilbeyi, C. Batten, «Pydgin», ISPASS 2015. https://www.csl.cornell.edu/~cbatten/pdfs/lockhart-pydgin-ispass2015.pdf
[R17] B. Ilbeyi et al., «Pydgin for RISC-V», 2016. https://www.csl.cornell.edu/~cbatten/pdfs/ilbeyi-pydgin-riscv2016.pdf
[R18] PyPy blog, «Pydgin: Using RPython to Generate Fast ISS», 2015-03-13. https://pypy.org/posts/2015/03/pydgin-using-rpython-to-generate-fast-1514065178985838697.html
[R19] «Pydrofoil», arXiv 2503.04389, 2025-03-06. https://arxiv.org/html/2503.04389v1
[R20] sysprog21/rv32emu. https://github.com/sysprog21/rv32emu
[R21] takahirox/riscv-rust. https://github.com/takahirox/riscv-rust
[R22] JKU, «Fast Interpreter-Based ISS for Virtual Prototypes», DATE 2025. https://research.jku.at/en/publications/fast-interpreter-based-instruction-set-simulation-for-virtual-pro/
[R23] riscv-software-src/riscv-isa-sim (Spike). https://github.com/riscv-software-src/riscv-isa-sim
[R24] Baekalfen/PyBoy. https://github.com/Baekalfen/PyBoy
[R25] Hackaday.io, 6502 en Python, log 2023-03-21. https://hackaday.io/project/186956/log/216932
[R26] riscemu (Python). https://github.com/Joshy-R/riscemu
[R27] Wikipedia, «Intel 80486». https://en.wikipedia.org/wiki/Intel_80486
[R28] Wikipedia, «P5 (microarchitecture)». https://en.wikipedia.org/wiki/P5_(microarchitecture)
[R29] Wikipedia, «R3000». https://en.wikipedia.org/wiki/R3000
[R30] ARM920T TRM DDI 0151C (copia U. Waterloo). https://student.cs.uwaterloo.ca/~cs452/docs/ts7200/arm-920t.pdf
[R31] Wikipedia, «ARM7». https://en.wikipedia.org/wiki/ARM7
[R32] Wikipedia, «Classic RISC pipeline». https://en.wikipedia.org/wiki/Classic_RISC_pipeline
[R33] gem5, «SimpleCPU». https://www.gem5.org/documentation/general_docs/cpu_models/SimpleCPU
[R34] gem5, «O3CPU». https://www.gem5.org/documentation/general_docs/cpu_models/O3CPU
[R35] mortbopet/Ripes. https://github.com/mortbopet/Ripes
[R36] ucb-bar/riscv-sodor. https://github.com/ucb-bar/riscv-sodor
[R37] avadhpatel/marss. https://github.com/avadhpatel/marss
[R38] D. Sanchez, C. Kozyrakis, «ZSim», ISCA 2013. https://people.csail.mit.edu/sanchez/papers/2013.zsim.isca.pdf
[R39] T. Carlson, W. Heirman, L. Eeckhout, «Sniper», SC'11. https://www.comp.nus.edu.sg/~tcarlson/pdfs/carlson2011etloafsaapms.pdf
[R40] SMARTS (registro EPFL Infoscience). https://infoscience.epfl.ch/record/135578?ln=fr
[R41] Valgrind, «Cachegrind». https://valgrind.org/docs/manual/cg-manual.html
[R42] Linux, `arch/riscv/Kconfig` (master). https://raw.githubusercontent.com/torvalds/linux/master/arch/riscv/Kconfig
[R43] LKML, «Add RISC-V 32 NOMMU support», 2023-01. https://lkml.iu.edu/hypermail/linux/kernel/2301.2/04823.html
[R44] Buildroot: qemu_riscv32_nommu_virt_defconfig (2023-10), qemu_riscv32_virt (2019-01, 2020-09). https://buildroot.uclibc.org/lists/buildroot/2023-October/738858.html ; https://lists.buildroot.org/pipermail/buildroot/2019-January/240168.html ; https://lists.buildroot.org/pipermail/buildroot/2020-September/291097.html
[R45] HackMD, registro de arranque mini-rv32ima. https://hackmd.io/@sysprog/SkvKPqnH1g
[R46] Epiczhul/esp32p4-rv32ima. https://github.com/Epiczhul/esp32p4-rv32ima
[R47] SpinalHDL/VexRiscv. https://github.com/SpinalHDL/VexRiscv
[R48] mit-pdos/xv6-riscv. https://github.com/mit-pdos/xv6-riscv
[R49] xv6-riscv, `kernel/memlayout.h`. https://raw.githubusercontent.com/mit-pdos/xv6-riscv/riscv/kernel/memlayout.h
[R50] michaelengel/xv6-rv32. https://github.com/michaelengel/xv6-rv32
[R51] Zephyr, `arch/riscv/Kconfig.isa`. https://raw.githubusercontent.com/zephyrproject-rtos/zephyr/main/arch/riscv/Kconfig.isa
[R52] FreeRTOS forums, port RISC-V sin mtime. https://forums.freertos.org/t/port-freertos-on-riscv-cpu-without-mtime-and-mtime-cmp-registers/18874
[R53] Wikipedia, «μClinux». https://en.wikipedia.org/wiki/%CE%9CClinux
[R54] D. Gow, «Linux on i486»; The Register 2026-04-06. https://davidgow.net/linux/i486.html ; https://www.theregister.com/2026/04/06/patch_to_end_i486_support/
[R55] eembc/coremark. https://github.com/eembc/coremark
[R56] embench/embench-iot. https://github.com/embench/embench-iot
[R57] ozkl/doomgeneric. https://github.com/ozkl/doomgeneric
[R58] knazarov/doom-riscv; Gigazine 2026-07-22. https://git.knazarov.com/knazarov/doom-riscv ; https://www.gigazine.net/gsc_news/en/20260722-doom-on-risc-v
[R59] QEMU, «Record/replay». https://qemu.readthedocs.io/en/master/system/replay.html
[R60] R. O'Callahan et al., «Engineering Record and Replay for Deployability» (rr), arXiv 1705.05937. https://ar5iv.labs.arxiv.org/html/1705.05937
[R61] Antithesis, «How Antithesis works». https://antithesis.com/docs/introduction/how_antithesis_works/
[R62] Wasmtime, «Interrupting Wasm execution». https://docs.wasmtime.dev/examples-interrupting-wasm.html
[R63] ethereum.org, «Opcodes». https://ethereum.org/developers/docs/evm/opcodes/
[R64] docs.ebpf.io, «Verifier». https://docs.ebpf.io/linux/concepts/verifier/
[R65] docs.kernel.org, «eBPF verifier». https://docs.kernel.org/bpf/verifier.html
[R66] QEMU, «Migration». https://qemu.readthedocs.io/en/master/devel/migration/main.html
[R67] CRIU, «Memory changes tracking». https://criu.org/Memory_changes_tracking
[R68] gem5, «Checkpoints». https://www.gem5.org/documentation/general_docs/checkpoints/
[R69] GDB, «Process Record and Replay». https://sourceware.org/gdb/current/onlinedocs/gdb.html/Process-Record-and-Replay.html
[R70] chipsalliance/riscv-dv. https://github.com/chipsalliance/riscv-dv
[R71] F. Solt et al., «Cascade», USENIX Security 2024. https://www.usenix.org/conference/usenixsecurity24/presentation/solt
[R72] «TheHuzz», arXiv 2201.09941. https://arxiv.org/abs/2201.09941
[R73] «Differential testing of QEMU vs ARM devices», arXiv 2105.14273. https://arxiv.org/abs/2105.14273
[R74] riscvarchive/riscv-v-spec, `v-spec.adoc` (1.1-draft). https://raw.githubusercontent.com/riscvarchive/riscv-v-spec/master/v-spec.adoc
[R75] LKML 2024-05, Zve32x/RVV ratificada (extractos). https://lkml.iu.edu/hypermail/linux/kernel/2405.1/02008.html ; .../02010.html
[R76] qemu-riscv, parche Zve32f/Zve64f, 2022-01-17. https://lists.gnu.org/archive/html/qemu-riscv/2022-01/msg00297.html
[R77] LLVM, «RISC-V Usage». https://llvm.org/docs/RISCVUsage.html
[R78] riscv-non-isa/rvv-intrinsic-doc. https://github.com/riscv-non-isa/rvv-intrinsic-doc/blob/main/README.md
[R79] Arm Research, «Making Helium: Sudoku, registers and rabbits», 2019-02-21. https://developer.arm.com/community/arm-research/b/articles/posts/making-helium-sudoku-registers-and-rabbits
[R80] binutils-gdb, commit MVE. https://gnu.googlesource.com/binutils-gdb/+/ae66a8f19ef6bf2dc7369cf26073f34ddf7c175b%5E%21
[R81] Wikipedia, «MMX». https://en.wikipedia.org/wiki/MMX_(instruction_set)
[R82] Wikipedia, «Streaming SIMD Extensions». https://en.wikipedia.org/wiki/Streaming_SIMD_Extensions
[R83] Kendryte SDK, `kpu.h`. https://raw.githubusercontent.com/kendryte/kendryte-standalone-sdk/develop/lib/drivers/include/kpu.h
[R84] riscv.org, «XuanTie Matrix Multiply Extension», 2023-02-10. https://riscv.org/blog/xuantie-matrix-multiply-extension-instructions/
[R85]–[R87] retirados tras la verificación (cita no verificable o ausente): charter de IME, arXiv 2604.27808 (AME), ET-SoC-1.
[R88] Chipyard, «RoCC or MMIO». https://chipyard.readthedocs.io/en/stable/Customization/RoCC-or-MMIO.html
[R89] QEMU, `target/riscv/vector_internals.h`. https://qemu.googlesource.com/qemu/+/074dbce5fcce597e61ff6c44d2977458f0ba2f29/target/riscv/vector_internals.h
[R90] patchew, RFC «unit-stride load/store grouping», 2024-05-31. https://patchew.org/QEMU/20240531174504.281461-1-max.chou@sifive.com
[R91] GCC, «GCC 7 Release Series: Changes». https://gcc.gnu.org/gcc-7/changes.html
[R92] GCC, «RISC-V Options». https://gcc.gnu.org/onlinedocs/gcc/RISC-V-Options.html
[R93] LLVM, «LLVM 9.0.0 Release Notes», 2019-09-19. https://releases.llvm.org/9.0.0/docs/ReleaseNotes.html
[R94] LLVM, «Building LLVM with CMake» (lista de objetivos a agosto de 2025). https://llvm.org/docs/CMake.html
[R95] GCC, «ARM Options». https://gcc.gnu.org/onlinedocs/gcc/ARM-Options.html
[R96] ARM7TDMI r4p1 TRM, DDI 0210C (copia MIT). https://resenv.media.mit.edu/plug/ARM7TDMI_technical_reference.pdf
[R97] Wikipedia, «MIPS architecture». https://en.wikipedia.org/wiki/MIPS_architecture
[R98] GCC, «MIPS Options». https://gcc.gnu.org/onlinedocs/gcc/MIPS-Options.html
[R99] Intel, «Intel 64 and IA-32 Architectures Software Developer Manuals», 2026-10-09. https://www.intel.com/content/www/us/en/developer/articles/technical/intel-sdm.html
[R100] GCC, «x86 Options». https://gcc.gnu.org/onlinedocs/gcc/x86-Options.html
[R101] Motorola/NXP, M68000 Family Programmer's Reference Manual, 1992. https://www.nxp.com/docs/en/reference-manual/M68000PRM.pdf
[R102] GCC, «M680x0 Options». https://gcc.gnu.org/onlinedocs/gcc/M680x0-Options.html
