# RFC-EXP-00016 — Pantalla determinista y aceleración por bloques para Tramoya VM32

| Campo | Valor |
|---|---|
| Identificador | RFC-EXP-00016 |
| Estado | PROPUESTO (P1–P4); P5 IMPLEMENTADA y medida (2026-09-29) |
| Fecha | 2026-09-29 |
| Firmado | OMRI |
| Relacionado | RFC EXP 00015 (Tramoya VM32) |
| Expedición | ¿Cómo romper los límites de Tramoya VM32 (intérprete Python de ~0,36–0,4 M instr/s, consola por bloques con polling, gas total que se agota) para mostrar juegos simples, y si es posible 3D por software, con una memoria gráfica en CPU sin GPU? Secundario: uso con LLMs. |

**Regla de tinta.** En este documento, **HECHO** marca afirmaciones con fuente [R#] o medición local [L]; **PROPUESTA** marca diseño nuestro; **CONJETURA** marca extrapolaciones sin medir. Nunca se mezclan sin marca.

## 1. Resumen

La VM no necesita GPU: el cuello de botella es la sobrecarga del intérprete Python por instrucción, no el dibujo. Proponemos cuatro piezas en orden de construcción. **P1**: un modo de ejecución por frames con gas por frame, preempción sin fallo y reproducción exacta, cuya imagen se deriva de cuatro páginas de la memoria invitada (VRAM, paleta, sprites y lista de dibujo) y la rasteriza el host en microsegundos. **P2**: un traductor de bloques básicos a Python que conserva gas exacto, atomicidad y breakpoints mediante deoptimización por repetición. **P3**: 3D por software con la geometría en el invitado y el relleno de filas en el host, con una instrucción `XFORM`. **P4**: un banco de pruebas para LLMs con contraejemplos visuales. P1 y P3 son viables hoy sin acelerar la VM; P2 multiplica su margen. **P5**, añadida tras la expedición, ya está construida: el Chip Neuronal Tramoya (TNU), un coprocesador de 18 instrucciones vectoriales que ejecuta el host tras la capacidad `npu`, con la ROM de pesos fuera de los snapshots. Con él, un programa `.tasm` ejecuta llama2.c y genera texto de forma determinista a ~42 tok/s (stories260K) y ~3,5 tok/s (stories15M en int8) (HECHO [L2]).

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

**P2-lite, implementada (2026-09-29, rama `acelerador-bucles`; HECHO [L3]).** Antes de compilar bloques arbitrarios se construyó la versión restringida: solo bucles internos con cuerpo lineal y un único salto de vuelta, compilados tras 32 pasadas. La deoptimización no es por repetición sino por **entrega**: el código generado comprueba el rango antes de cada acceso a memoria y, si algo pudiera fallar, devuelve el control al intérprete en esa instrucción con el estado parcial ya escrito; el intérprete la ejecuta con su validación y su rollback, así que ninguna instrucción se ejecuta dos veces ni a medias. Gas y presupuesto se comprueban por vuelta. Medido: `ADDI`+`JMP` de 557 000 a 5,5 M instr/s (10×); producto punto escalar de 55 000 a 345 000 MAC/s (6,3×; 9,4× frente a la línea base anterior a las micro-optimizaciones). Cobertura en los demos del repositorio: 0 %, porque son programas de 5–145 instrucciones o tienen `SYSCALL`/`CALL` dentro del bucle; `llama2.tasm`: 0 % (sus bucles contienen `CALL` y ops TNU, y su tiempo es del TNU). El experimento de cobertura sobre código de juego sigue pendiente: P2-lite vive o muere por él. Verificación: fuzzing diferencial de 400 bucles aleatorios frente al intérprete (estado, gas, fallos, memoria idénticos).

Hallazgo colateral del fuzzing (HECHO [L3]): la FPU escalar no era determinista con NaN. Para `x * y` con dos NaN, CPython 3.13 devuelve la carga útil del segundo operando la primera vez y la del primero cuando la operación ya está especializada. Se corrigió guardando todo NaN como el canónico `0x7FC00000`, como hace RISC-V.

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
- Inferencia sobre la VM: un modelo de 260K costaría ~4 M instrucciones por token, 11–20 s por token hoy (CONJETURA); es plausible como demostración, no como uso. La VM es más valiosa como sandbox determinista con gas y capacidades para código generado. *P5 supera esta estimación: con el TNU, el mismo modelo cuesta ~2 000 instrucciones por token y corre a ~42 tok/s (HECHO [L2]).*

### P5. Chip Neuronal Tramoya (TNU): instrucciones vectoriales ejecutadas por el host — IMPLEMENTADA

Ataca la frontera de §2.5 y la conjetura de P4 sobre inferencia. Todo lo marcado [L2] se midió con `benchmarks/benchmark_tnu.py` (Python 3.13.2, Windows x64, 2026-09-29) sobre stories260K y stories15M de karpathy/tinyllamas con sus tokenizers.

**Punto de partida medido en el host (HECHO [L2]).**
- *float32:* `math.sumprod` sobre vistas `memoryview(página).cast('B').cast('f')`, sin copias. MATVEC 288×288 tarda 2,34 ms (35 M MAC/s) con ambos operandos como `memoryview`, y 1,47 ms (**56 M MAC/s**) si `x` se convierte antes a lista.
- *int8:* con una vista con signo `'b'`, 32–38 M MAC/s, porque CPython crea un objeto nuevo por cada entero negativo (solo cachea −5…256). Con bytes sin signo desplazados +128 y un término de corrección, **68 M MAC/s**.
- *Elemento a elemento:* `exp` vectorial a 10,8 M/s; suma vectorial a 14,3 M/s.
- *Dentro de la VM, sin TNU:* 346 000–363 000 instr/s (2,75–2,89 µs por unidad de gas, según la corrida) y 32 000 MAC/s con `LOAD`/`FMUL`/`FADD`.

**Diseño (PROPUESTA, construida).**
- *Conexión.* `TramoyaNeuralUnit` se conecta como el chip de memoria (`TramoyaVM32(..., npu=...)` o `attach_npu`) y exige la capacidad `npu`, que no viene por defecto. Sin ella, o sin chip, cualquier opcode 130–147 falla sin efectos. El chip no tiene estado mutable: la configuración vectorial (VL, VR, VS) vive en el núcleo de la VM, así que la cubren el registro de deshacer y los snapshots.
- *ISA.* `VCFG` (fija VL, VR y VS, al estilo `vsetvl`), `VCOPY`, `VADD`, `VMUL`, `VSCALE`, `FDOT`, `MATVEC`, `MATTV` (suma ponderada de filas, para la atención), `RMSNORM`, `VSOFTMAX`, `VEXP`, `VSILU`, `ROPE`, `VARGMAX`, `VSAMPLE`, `VQUANT`, `QMATVEC` y `QROW`. Los operandos son registros que contienen direcciones. VS permite recorrer la caché KV con paso `kv_dim` sin copiar cabezas. La tabla completa está en `VM32.md`.
- *ROM de pesos (punto 5 de la misión).* Ventana fija de solo lectura desde `0x40000000`, fuera de la RAM. `LOAD` y `print_string` la leen si hay `npu`; nada la escribe. `TensorROM` copia el archivo y calcula su sha256 al abrirlo. El snapshot guarda solo `{sha256, size_bytes, path}`. Para restaurar, el host tiene que conectar una ROM con la misma huella; la ruta del snapshot **nunca se abre**, porque un snapshot es entrada no confiable.
- *Memoria (punto 8).* `MAX_MEMORY_WORDS` no sube. Con la ROM aparte, la RAM de stories15M solo contiene la caché KV y las activaciones: como máximo 0,9 M palabras con 256 posiciones, y 188 416 palabras asignadas tras 80 000 instrucciones [L2]. La ROM ocupa 15,4 M palabras en f32 y 4,08 M en Q8 [L2].
- *Carga de pesos (punto 6).* En el host, `python -m cpu_digital.tnu_models CKPT TOK SALIDA [--quant q8]` y `--npu-rom` en el CLI. En el invitado, la syscall 12 `npu_info` y una cabecera de 64 palabras con las dimensiones, punteros absolutos y el paso por capa. Sin `.WORD` gigantes.
- *Numérica (puntos 1–4).*
  - Se calcula en doble precisión con **un solo redondeo** a float32 al guardar.
  - `VADD`, `VMUL` y `VSCALE` coinciden bit a bit con `FADD`/`FMUL` (verificado por un ancla).
  - `FDOT` y `MATVEC` acumulan con `sumprod`: son deterministas, pero no iguales a una cadena de `FMUL`+`FADD`.
  - Un desbordamiento da ±inf, sin fallo.
  - Q8: cuantización simétrica con escala float32 **por fila** en los pesos y por vector en las activaciones; los productos enteros son exactos.
  - Las filas en ROM nunca cruzan páginas, porque el buffer es contiguo. En RAM, una región que cruza páginas se reúne con una sola copia contigua: es el camino lento y se cobra como trabajo (ver gas).
- *Gas (punto 7), política declarada.*
  - Coste = coste base (1 para `VCFG`, 2 para el resto) + `ceil(unidades/64)`.
  - Unidades: una por MAC en los productos, más 8 por fila (`MATVEC`, `QMATVEC`) o columna (`MATTV`), que es el coste fijo de cada llamada a `sumprod`; en `MATVEC`/`MATTV`, además, al menos la región tocada, para no subcobrar pasos dispersos; de 1 a 5 por elemento en el resto.
  - Tope de 2²⁶ unidades por instrucción. El gas se comprueba y se cobra **antes** de leer o calcular.
  - Calibración final [L2], en µs por unidad de gas y relativa a los 2,89 µs/gas del intérprete en la misma corrida: VADD 0,85×, VSCALE 0,78×, VEXP 0,57×, VSILU 0,99×, VSOFTMAX 0,88×, RMSNORM 0,97×, ROPE 1,07×, FDOT 0,65×, MATVEC 0,43×, MATTV 0,56×, QMATVEC 0,33×, VARGMAX 0,69×. El peritaje midió además formas adversas: antes del término por fila, MATVEC con VL=1 llegaba a ~6,5×.
  - Ninguna operación medida cuesta al host más de ~1,1× lo que costaría el mismo gas interpretado. La propuesta inicial `1 + n/32` habría cobrado ~2× de más en FDOT.
- *Atomicidad.* Orden fijo: permisos → parámetros → tope → gas → lectura → cálculo → **una** escritura. Antes de escribir se guarda el contenido previo con `_log_undo`. Si la escritura toca código desprotegido, se invalida la caché de decodificación; si el destino es código protegido o la ROM, falla.
- *Tokenizer y muestreo (punto 9).* La codificación BPE del prompt va en el host (`tnu_models.encode`): es una búsqueda de fusiones por puntuación, difícil de acotar con gas y que se usa una sola vez. En el invitado quedan la decodificación de piezas (cadenas en la ROM, sin el espacio inicial tras BOS), el muestreo (xorshift32 propio + `VSAMPLE`, o `VARGMAX` a temperatura 0) y la parada en BOS.
- *Programa.* `vm_programs/llama2.tasm` orquesta capas, cabezas con GQA, caché KV, RoPE, FFN SwiGLU y muestreo. Coincide **bit a bit** con `tnu_models.reference_generate` (mismos núcleos, mismo orden). En modo voraz f32 coincide en tokens con un llama2.c ingenuo en doble precisión escrito aparte. Las anclas están en `tests/test_tnu_llama.py`.

**Experimentos (criterios de muerte fijados por la misión antes de medir).**

| # | Experimento | Criterio de muerte | Resultado [L2] | Veredicto |
|---|---|---|---|---|
| T1 (E1 de la misión) | FDOT dentro de la VM, contando el dispatch | < 20 M MAC/s | 29,4 M MAC/s con VL=4096; 12,0 M con VL=288; MATVEC 288×288 en 1,59 ms (52,2 M MAC/s) | VIVE |
| T2 (E2) | stories260K, 256 tokens | < 20 tok/s, o tokens distintos con la misma semilla | 41,6 tok/s voraz; 42,6 y 40,8 con muestreo; tokens idénticos; 1 990 instr/token | VIVE |
| T3 (E3) | stories15M int8, 128 tokens | < 1 tok/s | Q8: 3,53 tok/s; f32: 3,00 tok/s; ~2 100 instr/token | VIVE |
| T4 (E4) | snapshot con la ROM de 15M conectada | ≥ 100 ms | 0,7 ms recién cargado; a mitad de generación la ROM aporta **+0,3 ms** (ruido) y 109 B | VIVE en lo que mide |

**Matiz de T4 (HECHO [L2]).** A mitad de generación el snapshot tarda 234 ms con 90 112 palabras de RAM y 478 ms con 188 416, con o sin ROM conectada. De unos 231 ms perfilados, 226 los gasta `zlib` nivel 9 comprimiendo la caché KV, que es incompresible; con nivel 6 serían ~51 ms y con nivel 1, ~10 ms. Es una propiedad del formato de snapshot existente, no del TNU. Queda como PROPUESTA aparte (bajar el nivel de compresión o codificar las páginas en binario); este cambio no la aplica.

**Proyectado frente a medido.**
- *stories260K:* ~60 tok/s proyectados, 41,6 medidos. La proyección solo contaba MAC; a este tamaño pesan más la sobrecarga por fila (`sumprod` sobre filas de 8–172 elementos) y las ~2 000 instrucciones interpretadas por token (~5,5 ms).
- *stories15M:* ~2 tok/s proyectados en f32 y 3–4 en int8, frente a 3,00 y 3,53 medidos. La f32 supera la proyección gracias a pasar `x` como lista (56 frente a 35 M MAC/s). La int8 no llega a la relación 68/56 porque el clasificador y la reunión de la caché KV cuestan igual en ambos formatos.
- *Referencia externa:* un 486 DX2-66 da ≈ 2 tok/s con 260K [R27]; VM32+TNU en un PC actual es 20× más rápido.

**Límites honestos.**
- `math.sumprod` exige Python ≥ 3.12. El TNU lo comprueba al construirse; el resto de VM32 sigue funcionando en 3.10.
- `exp`, `cos`, `sin` y `pow` vienen de la libm del host. El determinismo está verificado en una sola plataforma; la igualdad bit a bit entre sistemas operativos es CONJETURA (el redondeo final a float32 suele absorber una diferencia de 1 ulp en doble).
- La escala Q8 por fila es más gruesa que los grupos de `runq.c`. Aun así, en los primeros 32 tokens voraces de stories15M, Q8 coincide con f32 [L2].
- Cuantizar la ROM Q8 de 15M tarda 3,6 s en Python puro [L2].

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
- [L3] Mediciones de P2-lite: `benchmarks/benchmark_vm32.py` (con y sin `--no-loops`) y el bucle MAC de `tests/test_vm32_loops.py`, Python 3.13.2 sobre Windows x64, 2026-09-29.
- [L2] Mediciones de P5: `benchmarks/benchmark_tnu.py` (reproducible; `--json` guarda el informe completo), Python 3.13.2 sobre Windows x64, 2026-09-29. Modelos stories260K y stories15M de https://huggingface.co/karpathy/tinyllamas; tokenizers de https://github.com/karpathy/llama2.c.
