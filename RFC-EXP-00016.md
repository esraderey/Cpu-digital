# RFC-EXP-00016 — Pantalla determinista y aceleración por bloques para Tramoya VM32

| Campo | Valor |
|---|---|
| Identificador | RFC-EXP-00016 |
| Estado | PROPUESTO |
| Fecha | 2026-09-29 |
| Firmado | OMRI |
| Relacionado | RFC EXP 00015 (Tramoya VM32) |
| Expedición | ¿Cómo romper los límites de Tramoya VM32 (intérprete Python de ~0,36–0,4 M instr/s, consola por bloques con polling, gas total que se agota) para mostrar juegos simples, y si es posible 3D por software, con una memoria gráfica en CPU sin GPU? Secundario: uso con LLMs. |

**Regla de tinta.** En este documento, **HECHO** marca afirmaciones con fuente [R#] o medición local [L]; **PROPUESTA** marca diseño nuestro; **CONJETURA** marca extrapolaciones sin medir. Nunca se mezclan sin marca.

## 1. Resumen

La VM no necesita GPU: el cuello de botella es la sobrecarga del intérprete Python por instrucción, no el dibujo. Proponemos cuatro piezas en orden de construcción. **P1**: un modo de ejecución por frames con gas por frame, preempción sin fallo y reproducción exacta, cuya imagen se deriva de cuatro páginas de la memoria invitada (VRAM, paleta, sprites y lista de dibujo) y la rasteriza el host en microsegundos. **P2**: un traductor de bloques básicos a Python que conserva gas exacto, atomicidad y breakpoints mediante deoptimización por repetición. **P3**: 3D por software con la geometría en el invitado y el relleno de filas en el host, con una instrucción `XFORM`. **P4**: un banco de pruebas para LLMs con contraejemplos visuales. P1 y P3 son viables hoy sin acelerar la VM; P2 multiplica su margen.

## 2. Estado del arte

### 2.1 Punto de partida medido [L]

Mediciones locales del director sobre el repositorio (commit fb38417 más correcciones del peritaje), Python 3.13.2, Windows x64, bucle `ADDI`+`JMP`:

- **HECHO [L]**: VM32 ejecuta ~361 000 instr/s sin traza y ~205 000 con traza de 4096 entradas (la consola la activa por defecto).
- **HECHO [L]**: el tiempo se reparte en `_execute_one`, el dispatch por cadena `if/elif`, `run`, el checkpoint, `_add` y la consulta del estado Tramoya en cada vuelta; no hay un cuello único, sino sobrecarga de llamadas por instrucción.
- **HECHO [L]**: un intérprete mínimo con tabla de closures y la misma semántica esencial alcanza ~2,2 M instr/s (≈6×); traducir el bloque a código Python con `exec`, con flags al final y gas por bloque, alcanza ~14 M instr/s (≈40×, cota optimista sin fallos exactos ni rollback).
- **HECHO [L]**: en el host, rellenar un framebuffer de 160×120 bytes cuesta ~2 µs; un rectángulo de 60×40, ~11 µs; escribir 19 200 píxeles uno a uno en Python, ~3,2 ms.
- Consecuencia: a 30 fps, el invitado dispone hoy de ~12 000 instrucciones por frame sin traza y ~6 800 con traza.

### 2.2 Acelerar sin salir de Python

- **HECHO**: el intérprete tail-call de CPython 3.14 aporta un 1–5 % según la configuración [R1][R2]; el JIT copy-and-patch rinde en 3.13 «about as fast as» el intérprete especializado [R3] y en 3.15 un 4–12 % de media geométrica [R4].
- **HECHO**: el cambio de runtime sí da órdenes de magnitud: PyPy acelera 23× un programa numérico [R5]; un simulador de ISA en RPython pasa de ~10 MIPS a 90–750 MIPS con JIT [R6][R7].
- **HECHO**: un emulador reescrito de Python a Rust pasó de ~180 000 instr/s a 81 M instr/s [R8]; wasmtime-py da ~10× al sustituir una función Python por C compilado a WASM [R9]; Unicorn advierte que instrumentar cada instrucción degrada hasta 10× y recomienda hooks por bloque [R10].
- **Frontera**: no se localizó en el barrido ningún emulador sobre CPython que publique cifras de compilación a closures, superinstrucciones o traducción de bloques invitados a Python (consulta dirigida realizada); tampoco un coste fiable por llamada Python↔nativo. Las únicas cifras de ese espacio son las [L].

### 2.3 Memoria gráfica y render sin GPU

- **HECHO**: las consolas fantasía mapean una VRAM compacta en el espacio de direcciones y ofrecen primitivas de dibujo desde el runtime: PICO-8, 128×128 a 4 bpp en 0x6000 (8 KB) [R11]; TIC-80, 240×136 a 4 bpp con paleta RGB de 16 colores [R12]; WASM-4, 160×160 a 2 bpp en 6400 B [R13]; Varvara dibuja por puertos y evalúa su vector de pantalla 60 veces por segundo [R14].
- **HECHO**: el raycasting calcula una vez por columna, no por píxel [R15]; Wolfenstein 3D lanza 304 rayos y pinta 46 208 píxeles por frame, y daba 5 fps en un 286 a 6 MHz [R16]. tinyrenderer es un rasterizador por software de ~500 líneas [R17].
- **HECHO**: `getContext('2d', {willReadFrequently: true})` fuerza un canvas 2D por software [R18]; kitty y sixel aceptan píxeles crudos en terminal [R19][R20]; WebSocket evita la sobrecarga HTTP del polling [R21].

### 2.4 Ritmo por frames y presupuesto

- **HECHO**: PICO-8 no aborta un frame largo: baja de 30 a 15 fps y llama `_update` dos veces por frame visible, y expone el uso de CPU por frame [R11]; WASM-4 llama `update()` cada frame [R22].
- **HECHO**: el fuel de Wasmtime es determinista y provoca un trap al agotarse; las épocas son hasta 2–3× más rápidas pero no deterministas [R23]; en la EVM, agotar el gas revierte el estado [R24].
- **HECHO**: en la NES la NMI marca el inicio del vblank [R25]; `requestAnimationFrame` sigue el refresco de pantalla y se pausa en segundo plano [R26].
- **Frontera**: no se localizó en el barrido un modelo que combine presupuesto por frame, determinismo y snapshots reproducibles.

### 2.5 LLMs

- **HECHO**: el modelo de 260K parámetros de llama2.c (dim 64, 5 capas) corre a ~2,08 tokens/s en un 486 DX2-66 [R27] y emite un token cada ~8 minutos en un Commodore 64 con coma flotante por software [R28]; la cuantización int8 da 3× de velocidad [R29].
- **HECHO**: hay agentes LLM que generan código correcto para ISAs no vistas «within a few refinement steps» con retroalimentación [R30].
- **Frontera**: no se localizó en el barrido un trabajo con tasa de acierto medida de LLMs sobre un ensamblador RISC de juguete propio.

## 3. Propuestas

Las cuatro son **PROPUESTA** salvo donde se cita un hecho.

### P1. Frames deterministas derivados de la memoria (fusión de dos propuestas independientes)

Ataca la frontera de §2.4 y reinterpreta §2.3 para las garantías de VM32.

**Modelo temporal.**
- Nueva configuración `frame_gas` (G_f). `run_frame(entrada)` escribe la entrada del frame en memoria, recarga el gas de frame a G_f y ejecuta hasta la syscall `VSYNC` o hasta que G_f no cubra la siguiente instrucción.
- **Preempción blanda**: si el gas de frame no alcanza, la instrucción no se ejecuta, no hay fallo ni rollback, y la VM queda en `PAUSED` con motivo «frame largo» (sin estado nuevo en Tramoya). El frame lógico solo avanza en `VSYNC`, así que la línea temporal depende únicamente de (programa, registro de entradas por frame). El `gas_limit` total queda como techo de sesión.
- Nueva ruta en la comprobación de gas (hoy agotarlo es `_ExecutionFault`), que distinga gas de frame de gas total e incluya la entrada a interrupción.
- Introspección: syscall con el gas usado en el frame anterior (equivalente a `stat(1)` de PICO-8).
- **Reproducción y rebobinado**: `PagedMemory` marca páginas sucias (en `__setitem__` y `write_block`); cada K frames se guarda un keyframe que copia solo esas páginas (`array` a `array`) más registros, pila, fibras y colas. `snapshot_bytes()` queda fuera del camino caliente. Las syscalls con estado externo (`read_int`, chip SQLite) se registran en el log de entradas o se deshabilitan en modo frame.
- La traza se registra por frame (o se apaga en modo frame), porque su coste reduce el presupuesto de ~12 000 a ~6 800 instr/frame [L].

**Modelo gráfico.** Cuatro páginas alineadas, con base relativa a `memory_words` (por ejemplo, las cuatro últimas páginas):
- P0, pixel page: 160×120 a 4 bpp, 8 píxeles por palabra = 2400 palabras (cabe en una página de 4096).
- P1: paleta de 16 colores RGB24, palabra de entrada y contador de frame.
- P2: 256 sprites 8×8 a 4 bpp (8 palabras por sprite).
- P3: lista de dibujo de hasta 1024 comandos de 4 palabras (rect, spr, línea, tri, texto; mismo ancho que una instrucción).
- `VSYNC` (capacidad `display`): el host decodifica P0 y rasteriza P3 sobre un back buffer con asignación de slices y lo intercambia solo si termina. El gas de la lista se cobra contra el frame que termina, con un coste máximo por frame menor que G_f para que un frame nunca quede reintentándose indefinidamente.
- El framebuffer nunca entra en snapshots: se regenera a partir de P0–P3.

**Transporte y presentación sin GPU.**
- Web: `/api/frame` ejecuta hasta el siguiente `VSYNC` (no bloques de 2000 instrucciones) y devuelve 9600 B indexados, paleta y hash, o «sin cambios»; en una segunda iteración, WebSocket binario. El cliente pinta con `ImageData`, una LUT `Uint32Array`, `willReadFrequently: true` e `image-rendering: pixelated`, y fija el ritmo con `requestAnimationFrame`.
- CLI: medios bloques «▀» con color ANSI (160×60 celdas), emitiendo solo las celdas que cambian.

**Coste**: ~700 líneas de Python y ~150 de JavaScript, 1–2 semanas (CONJETURA). **Ganancia**: juegos 2D a 30 fps hoy con ~6 800–12 000 instrucciones de lógica por frame; desaparece el agotamiento del gas total como límite de partida; rebobinado y reproducción exacta de bugs.

### P2. Traductor de bloques con deoptimización por repetición

Ataca la frontera de §2.2: la cifra [L] de ~14 M instr/s existe, pero sin las garantías de VM32.

- Se traducen a funciones Python (`exec`) los bloques básicos y los bucles cerrados formados por instrucciones puras (MOV/MOVI, aritmética, CMP/TEST, LOAD/STORE, saltos). Terminan bloque: SYSCALL, INT/IRET/EI/DI/SETIV, fibras, YIELD, BREAK/HALT y escrituras a R15; PUSH/POP/CALL/RET en la versión 1, traducidos en la 1.5 (el código de juego es denso en llamadas).
- Registros como variables locales; flags calculadas solo si un salto o la salida del bloque las leen. Acceso a memoria por página con `page_words` real y camino lento para páginas no asignadas (leen 0).
- **Gas exacto**: al entrar se comprueba gas ≥ coste del bloque, instrucciones restantes ≥ longitud y ausencia de interrupción habilitada; en bucles, una vez por iteración con el coste constante del cuerpo. Si no se cumple, se usa el intérprete de referencia para ese tramo: el agotamiento cae en la misma instrucción que hoy. Con P1, el presupuesto es `min(gas_frame, gas_total)`.
- **Atomicidad por repetición**: límites, división por cero y validación de saltos lanzan `_Deopt` dentro del código compilado. Cada STORE anota (dirección, valor anterior). Ante `_Deopt` se deshacen los STORE, se descartan los locales y el intérprete de referencia repite desde la entrada del bloque o de la iteración (checkpoint por iteración en bucles). El fallo ocurre en la instrucción exacta, con su mensaje y rollback.
- Breakpoints como cabeceras de bloque (caché indexada por pc y huella de breakpoints). STORE al rango de código con `protect_code=False` invalida los bloques que lo cubren. Traza registrada por bloque (pc de entrada y longitud), para que el traductor funcione también en la consola.
- **Oráculo**: `snapshot_bytes()` determinista para comparar intérprete de referencia y traductor en fuzzing diferencial, con los casos límite de §4 (`INT32_MIN / -1`, desbordamiento de MULI) como semillas.

**Coste**: ~1500 líneas, 2–3 semanas, riesgo alto de divergencia mitigado por el oráculo (CONJETURA). **Ganancia**: 3–7 M instr/s en código de juego (8–20×), más cerca de 40× en bucles de relleno o raycasting (CONJETURA; la crítica cruzada estima más probable la mitad baja, 3–4 M).

### P3. 3D por software: geometría en el invitado, filas en el host

Ataca la frontera de §2.3: el 3D viable en máquinas lentas es el raycasting, y no se localizaron cifras de rasterizado de triángulos.

- Nueva instrucción `XFORM rd, rs, rt`: lee un vector 16.16 de 3 palabras en [rs] y una matriz 3×4 en [rt] y escribe x', y', z' en [rd]. Su redondeo es idéntico a la secuencia MULH/SHR equivalente, para que el resultado no dependa de usar o no la instrucción. Equivale a ~80 instrucciones simples; su gas es una **política declarada** (propuesta inicial: 40, la mitad del equivalente), no una equivalencia.
- El invitado transforma vértices, proyecta con DIV, descarta caras traseras con un producto cruzado entero y emite comandos `TRI(x0y0, x1y1, x2y2|color, clave_z)` en P3.
- En `VSYNC`, el host ordena de forma estable por (clave_z, índice de emisión) (pintor, sin z-buffer) y rasteriza con funciones de arista enteras y regla top-left, rellenando cada fila con un slice. Sin coma flotante en el host: resultado bit a bit idéntico entre plataformas. El host solo lee la memoria invitada, así que no necesita journal.
- Variante raycaster con comando `COL(x, altura, tex, u)`: el invitado hace el DDA y el host escala la columna. Requiere P2: se estiman 16–40 k instr/frame (CONJETURA).

**Coste**: ~300 líneas. **Ganancia**: una escena de 40 vértices y 60 triángulos cuesta ≈3500 instr/frame con `XFORM`, es decir, 30 fps sin acelerar la VM; sin ella, solo la transformación sube a ≈3200 instrucciones. Raster del host: 1–2 ms (CONJETURA).

### P4. Banco de pruebas para LLMs con contraejemplo visual (secundaria)

Ataca la frontera de §2.5.

- Cada tarea: especificación, registro de entradas y oráculo por propiedades sobre la RAM de P0–P3 (por ejemplo, «sprite 0 en x=40 en el frame 30») u hashes de frame.
- Ante un fallo, el arnés devuelve el primer frame divergente como ASCII 40×30 (esperado, obtenido y diferencias), los comandos de la lista de dibujo con el PC que escribió cada palabra por última vez (mapa de procedencia, solo en el arnés) y el gas por frame, más el keyframe N−1 de P1 para probar arreglos sin repetir la partida.
- Diseño experimental: ≥100 tareas o muestras emparejadas, la ISA documentada en el prompt, y un control con información equivalente (volcado de traza de la misma longitud) para separar el efecto de la localización del de «más texto».
- Inferencia sobre la VM: un modelo de 260K costaría ~4 M instrucciones por token, 11–20 s por token hoy (CONJETURA); es plausible como demostración, no como uso. La VM es más valiosa como sandbox determinista con gas y capacidades para código generado.

## 4. Alternativas consideradas

- **Núcleo nativo sin retrollamadas (WASM o C que sale a Python en cada instrucción impura).** Cae como apuesta principal: la memoria paginada de VM32 no se comparte tal cual con WASM; en C, `INT32_MIN / -1` y el desbordamiento con signo tienen comportamiento indefinido; la pila es una lista Python; y con primitivas de dibujo cada 20–30 instrucciones no supera a P2. **Residuo**: el microbenchmark de ida y vuelta con ctypes (llena el hueco de §2.2) y el catálogo de divergencias C↔Python como semillas del oráculo de P2.
- **Cambiar de runtime (PyPy) o reescribir en Rust sin más.** Descartado como propuesta: es refrito del mapa [R5][R8] y rompe el objetivo de mantener las syscalls y las garantías en Python.
- **Dibujo píxel a píxel desde el invitado.** Descartado como modo principal: 19 200 píxeles exceden el presupuesto de ~12 000 instrucciones por frame [L]. Se conserva como pixel page (P0) para efectos libres.
- **Estado `OVERRUN` nuevo en Tramoya para la preempción.** Sustituido por `PAUSED` con motivo, que ya existe.

## 5. Riesgos, costes y experimentos asesinos

Riesgos principales: divergencias sutiles de flags C/O entre intérprete y traductor (P2); ruptura de la reproducción por estado externo (P1); cambio de semántica del gas, que exige auditar denegaciones de servicio (P1); artefactos del pintor con triángulos que se cruzan y desbordamiento int32 en la proyección 16.16 (P3); `XFORM` resta simplicidad didáctica a la ISA (P3).

Experimentos numerados por coste ascendente; cada uno puede matar su propuesta:

- **E1 (P1, horas).** Raster del host en `VSYNC`: decodificar P0 a 4 bpp, 64 sprites y una lista de 200 comandos, más la codificación para el transporte. **Muere** si supera 10 ms/frame.
- **E2 (P1, 1–2 días).** Grabar 1000 frames con entradas y reproducirlos desde el keyframe inicial. **Muere** si algún hash de estado difiere, o si un keyframe incremental con 64 páginas sucias cuesta más de 3,3 ms.
- **E3 (P3, 1–2 días).** Cubo giratorio y escena de 60 triángulos. **Muere** si el invitado necesita más de 12 000 instr/frame, si el raster del host supera 8 ms/frame, o si `XFORM` no reduce al menos 10× el tiempo real del bloque de transformación frente a la secuencia equivalente.
- **E4 (residuo, horas).** Ida y vuelta vacía Python↔C con ctypes. Si cuesta menos de 20 µs, reabre la alternativa nativa.
- **E5 (P2, 1 semana).** Traductor con 12 opcodes y todas las reglas, medido en una mezcla de `ADDI`+`JMP`, relleno de memoria y bucle DDA. **Muere** si queda por debajo de 3 M instr/s, o si aparece una divergencia de `snapshot_bytes` en 10⁵ programas aleatorios que no se corrija sin cortar los bloques a 1–2 instrucciones.
- **E6 (P4, 1–2 semanas).** Mismo LLM, ≥100 tareas, retroalimentación «falló + código de salida» frente a «contraejemplo visual + PC» frente a control de información equivalente. **Muere** si la mejora sobre el control es menor de 10 pp o si la base ya supera el 90 %.

Preguntas abiertas: coste real de `putImageData` frente a `drawImage` a 30 fps; si 4 bpp y 16 colores bastan o conviene una paleta de 256; política definitiva de gas de `XFORM`.

## 6. Referencias

Todas consultadas el 2026-09-29. Las marcadas con † fueron confirmadas por el verificador ciego contra el texto de la fuente.

- [R1]† N. Elhage, «Performance of the Python 3.14 tail-call interpreter», 2025-03-09. https://blog.nelhage.com/post/cpython-tail-call/
- [R2] Python Software Foundation, «What's new in Python 3.14». https://docs.python.org/3/whatsnew/3.14.html
- [R3] PEP 744, «JIT Compilation», 2024-04-11. https://peps.python.org/pep-0744/
- [R4] PEP 836, 2026-07-02. https://peps.python.org/pep-0836/
- [R5] arXiv 2505.02346, comparativa de compiladores y runtimes Python, 2025-05-05. https://arxiv.org/pdf/2505.02346
- [R6]† B. Ilbeyi et al., «Pydgin for RISC-V», Cornell, 2016. https://www.csl.cornell.edu/~cbatten/pdfs/ilbeyi-pydgin-riscv2016.pdf
- [R7] PyPy blog, «Pydgin: using RPython to generate fast instruction-set simulators», 2015-03. https://pypy.org/posts/2015/03/pydgin-using-rpython-to-generate-fast-1514065178985838697.html
- [R8]† Pemu devlog, «Speed increase from 200 kHz to 81 MHz», 2023-12-29. https://cottonballs.itch.io/pemu/devlog/657380/speed-increase-from-200-khz-to-81-mhz
- [R9]† C. Wellons, nullprogram, 2026-01-01. https://nullprogram.com/blog/2026/01/01
- [R10] Unicorn Engine, FAQ. https://github.com/unicorn-engine/unicorn/blob/master/docs/FAQ.md
- [R11]† Lexaloffle, «PICO-8 manual». https://www.lexaloffle.com/dl/docs/pico-8_manual.html
- [R12] TIC-80 wiki, «RAM». https://github.com/nesbox/TIC-80/wiki/RAM
- [R13]† WASM-4, «Memory map». https://wasm4.org/docs/reference/memory
- [R14] XXIIVV, «Varvara». https://wiki.xxiivv.com/site/varvara.html
- [R15]† L. Vandevenne, «Raycasting». https://lodev.org/cgtutor/raycasting.html
- [R16]† F. Sanglard, «Game Engine Black Book: Wolfenstein 3D», cap. 8. https://wolfenstein3d.nl/blackbook8/
- [R17] D. Sokolov, «tinyrenderer». https://github.com/ssloy/tinyrenderer/wiki/Lesson-0:-getting-started
- [R18]† MDN, «HTMLCanvasElement.getContext()». https://developer.mozilla.org/en-US/docs/Web/API/HTMLCanvasElement/getContext
- [R19] K. Goyal, «Terminal graphics protocol» (kitty). https://sw.kovidgoyal.net/kitty/graphics-protocol/
- [R20] DEC, «VT3xx Graphics Programming», cap. 14 (sixel). https://www.vt100.net/docs/vt3xx-gp/chapter14.html
- [R21] web.dev, «WebSockets basics». https://web.dev/articles/websockets-basics
- [R22] WASM-4, «Functions». https://wasm4.org/docs/reference/functions
- [R23]† Wasmtime, `Config` (fuel y épocas). https://docs.rs/wasmtime/latest/wasmtime/struct.Config.html
- [R24] ethereum.org, «Gas and fees». https://ethereum.org/en/developers/docs/gas/
- [R25]† NESdev wiki, «NMI». https://www.nesdev.org/wiki/NMI
- [R26] MDN, «Window.requestAnimationFrame()». https://developer.mozilla.org/en-US/docs/Web/API/Window/requestAnimationFrame
- [R27]† Yeo Kheng Meng, «Llama2 LLM on DOS», 2025-04-15. https://yeokhengmeng.com/2025/04/llama2-llm-on-dos/
- [R28]† ytmytm, «llama2.c64». https://github.com/ytmytm/llama2.c64
- [R29] A. Karpathy, «llama2.c». https://github.com/karpathy/llama2.c
- [R30] arXiv 2603.08721, KernelCraft, 2026-02-10. https://arxiv.org/abs/2603.08721
- [L] Mediciones locales del director: `scratchpad/medir_vm.py` de la sesión de expedición, Python 3.13.2 sobre Windows x64, 2026-09-29.
