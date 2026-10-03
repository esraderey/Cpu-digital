# RFC-EXP-00017 — Regiones con bucles anidados y unidad de dibujo para Tramoya VM32

| Campo | Valor |
|---|---|
| Identificador | RFC-EXP-00017 |
| Estado | PROPUESTO; E1 ejecutado el 2026-10-02 (viven P1b y P2) |
| Fecha | 2026-10-02 |
| Firmado | OMRI |
| Relacionado | RFC-EXP-00016 (§P2-lite: pasos 2 y 3 de la decisión sobre P2; §P3, variante raycaster), RFC EXP 00015 (Tramoya VM32) |
| Expedición | ¿Qué le falta a Tramoya VM32 para ejecutar más rápido código de juego como el raycaster de referencia, una vez acotado el coste de compilar del acelerador? ¿Qué aportaría un segundo núcleo «matemático»? |

**Regla de tinta.** En este documento, **HECHO** marca afirmaciones con fuente [R#], medición local [L#] o lectura del código del repositorio (con su ruta); **PROPUESTA** marca diseño nuestro; **CONJETURA** marca extrapolaciones sin medir. Nunca se mezclan sin marca. Las estimaciones de tiempo de §3 son CONJETURA aunque se calculen con costes medidos.

## 1. Resumen

Con el acelerador, el raycaster de referencia corre a 13,8 frames/s. El 59,6 % del tiempo se va en el 14,3 % de las instrucciones, las que siguen en el intérprete (3,40 µs cada una; compiladas, 0,39 µs), y el 33,0 %, en cuatro bucles de dibujo ya compilados (HECHO [L6]). Un núcleo matemático no ataca ninguna de las dos: aunque la coma flotante no costara nada, el juego iría como mucho 1,19× más rápido (HECHO [L6]). **P1** extiende el compilador de bucles a varios saltos de vuelta y a regiones con bucles anidados y cuerpos de hasta 256 instrucciones; en la simulación, el intérprete pasa de 260 222 instrucciones a 60 878. **P2** añade instrucciones de dibujo que ejecuta el host: un relleno con paso y una columna de textura escalada con transparencia. Estimamos ≈1,7× con P1, ≈1,4× con P2 y ≈3,4× con las dos (CONJETURA, como la simulación).

## 2. Estado del arte

### 2.1 Dónde se va hoy el tiempo (HECHO [L6] salvo marca)

Medido con `benchmarks/benchmark_dibujo.py --rondas 5`, nuevo en este RFC, sobre `vm_programs/juego_raycaster.tasm` (sha256 `3fab6dca…ea26`, el programa congelado de RFC-EXP-00016) con 20 frames y el `cpu_digital` de `1972ef9`.

- 1 816 417 instrucciones; con el acelerador, 1,451 s (72,6 ms por frame, 13,8 frames/s). En tres ejecuciones, el total varió entre 1,451 y 1,482 s, y los cocientes de este documento, en ±0,01. Sin acelerador, el juego tarda 3,2× más (RFC-EXP-00016 [L4] y [L5]).
- Mezcla: memoria (`LOAD`, `STORE`) 38,5 %, aritmética y lógica entera 34,7 %, saltos 19,5 %, coma flotante 6,0 % y movimientos 1,2 %.
- Reparto del tiempo:

| Dónde | Instrucciones | Tiempo | µs por instrucción |
|---|---|---|---|
| Bucles compilados | 85,7 % | 40,4 % | 0,39 |
| Intérprete | 14,3 % | 59,6 % | 3,40 |

- Los cuatro bucles de dibujo, ya compilados, suman el 33,0 % del tiempo: la columna de téxeles de los sprites (`RS_PIX`, 12,5 %) y los rellenos de techo, suelo y muro (`RM_CEIL`, 7,6 %; `RM_FL`, 7,2 %; `RM_WL`, 5,6 %). En 20 frames hacen 9 600 rellenos (399 344 filas, 31,6 µs por relleno) y 2 087 columnas de sprite (108 786 téxeles, 1 710 ns por téxel).
- De las 260 222 instrucciones interpretadas, 202 703 (el 78 %) son de la fase `render_muros`, y 45 785, de `render_sprites`.
- La coma flotante es el 6,0 % de las instrucciones, pero ~15,8 % del tiempo, porque buena parte está en código interpretado (estimación con el coste medio de cada modo). Aunque no costara nada, el juego iría como mucho 1,19× más rápido (ley de Amdahl).

### 2.2 Qué deja instrucciones en el intérprete (HECHO, `cpu_digital/vm32_loops.py` y el juego)

- El acelerador compila un bucle desde su cabecera hasta el primer salto que vuelve a ella, con un cuerpo de hasta 64 instrucciones (`MAX_BODY_INSTRUCTIONS`), saltos hacia delante internos o de salida y ningún otro salto hacia atrás. Comprueba el gas y el presupuesto una vez por vuelta contra la vuelta más larga, y si una instrucción pudiera fallar, devuelve el control al intérprete en ella.
- `RM_COL`, el bucle por columna de `render_muros`, tiene 83 instrucciones y contiene cuatro bucles: el DDA (`RM_DDA`) y los rellenos `RM_CEIL`, `RM_WL` y `RM_FL`. Lo bloquean a la vez su longitud y los bucles anidados. Sus tres bloques fríos (`RM_XZ`, `RM_YZ` y `RM_NEAR`) están detrás del salto de vuelta y regresan al cuerpo con un salto hacia atrás.
- `RM_DDA` tiene dos saltos de vuelta, a 5 y a 12 instrucciones de su cabecera. El acelerador compila hasta el primero, así que cada paso en x sale al intérprete y vuelve a entrar. Cuesta 0,78 µs por instrucción compilada, el doble de la media (HECHO [L6]).
- En los sprites, `RS_STRIPE` (7 instrucciones) se compila, pero su camino visible (`RS_VIS`, el bucle `RS_PIX` y `JMP RS_ADV`) está detrás del salto de vuelta y regresa a la mitad del cuerpo. Un modelo por intervalos [cabecera, último salto de vuelta] no puede expresarlo: haría falta uno de bucles naturales sobre el grafo de control. `RS_SPR`, el bucle por sprite (100 instrucciones), contiene ese camino.

### 2.3 Fuentes externas

- **HECHO [R1].** El raycasting de lodev hace un cálculo por columna de la pantalla, no por píxel («only a calculation has to be done for every vertical line of the screen»). La columna se dibuja con una línea vertical de un color (`verLine`) o, con texturas, avanzando la coordenada de textura con un paso fijo por píxel (`texPos += step`). Son las dos operaciones de P2.
- **HECHO [R2].** PICO-8 ofrece como primitivas del runtime el relleno de rectángulos (`RECTFILL`) y `SSPR`, que estira un rectángulo de la hoja de sprites hasta un rectángulo de destino en la pantalla; la transparencia que fija `PALT` la respetan `SPR`, `SSPR`, `MAP` y `TLINE`.
- **HECHO [R3].** En CPython, el GIL asegura que un solo hilo ejecute bytecode Python a la vez. Desde Python 3.13 puede desactivarse, pero solo en una compilación configurada con `--disable-gil`.
- **No cubierto.** No se barrió la literatura sobre compilación de bucles anidados y regiones en compiladores JIT (de trazas o de métodos). El diseño de P1 se apoya solo en el código existente y en las mediciones locales.

## 3. Propuestas

Orden de construcción (PROPUESTA): P0, P1a, P1b y P2, con los experimentos de §5 antes de cada paso. P1 va primero porque acelera cualquier programa sin cambiarlo; P2 exige que el programa use instrucciones nuevas.

### P0. Prerrequisitos (PROPUESTA)

1. `benchmarks/benchmark_dibujo.py`, incluido en este RFC, es la herramienta de medida de P1 y P2: reproduce todas las cifras [L6] y comprueba que la simulación del acelerador actual coincide con la medición.
2. El cambio que acelera los `LOAD` de la ROM del TNU, aún sin fusionar, toca el mismo generador de código: P1 parte de él.
3. `benchmarks/benchmark_tnu.py` mide la línea base del intérprete con un bucle que el acelerador compila (HECHO: `interpreter_rate` usa la configuración por defecto, con `accelerate_loops=True`), así que su columna «×intérprete» sale inflada. La calibración del gas de P2 (E4) necesita esa línea base corregida, con el acelerador desactivado.
4. Independiente de este RFC: el acceso a memoria por página en el código generado (paso 1 de la decisión de RFC-EXP-00016) dio un 15 % más de velocidad con un prototipo (HECHO, RFC-EXP-00016 [L4]). Si se construye antes, P1 lo hereda.

### P1. Varios saltos de vuelta, bucles anidados y cuerpos largos (PROPUESTA)

Ataca §2.2: por `RM_COL` y el DDA, la fase `render_muros` aporta el 78 % de las instrucciones interpretadas. Son los pasos 2 y 3 de la decisión de RFC-EXP-00016, en dos entregas.

**P1a. Varios saltos de vuelta.** El cuerpo llega hasta el último salto a la cabecera dentro del tope actual de 64 instrucciones, y los anteriores se generan como `continue`, con la contabilidad de pasos y de gas de su camino. La comprobación por vuelta no cambia, porque un `continue` anticipado recorre un camino más corto que la vuelta más larga. Riesgo bajo: es el mismo generador con un caso más.

**P1b. Regiones con bucles anidados y cuerpos de hasta 256 instrucciones.**

*Forma de la región.* Desde una cabecera caliente H, con el mismo calentamiento y la misma espera de compilación de hoy, la región es el intervalo [H, B], donde B es el último salto a H dentro del tope de 256 instrucciones. Cada salto hacia atrás del intervalo con destino T > H define un bucle interior [T, último salto a T]. La región se compila solo si:

1. los bucles forman una familia anidada de intervalos: dos cualesquiera son disjuntos o uno contiene al otro;
2. cada bucle tiene una sola entrada: ningún salto desde fuera de él cae en su interior, salvo en su cabecera;
3. todo salto que sale de un bucle interior va hacia delante, a una posición del bucle que lo contiene (se genera como `skip` del nivel de fuera y `break` de los de dentro), o fuera de la región (salida al intérprete, como hoy);
4. la profundidad es como mucho 3; el juego usa 2.

Todo lo demás (saltos hacia atrás a algo que no es una cabecera, un `continue` hacia una cabecera exterior desde un bucle interior, llamadas, syscalls) deja la cabecera sin región, y sus bucles interiores siguen compilándose por separado, como hoy.

*Generación (variante A, estructurada).* Cada bucle se genera como un `while True:` con el esquema actual de bloques y `skip` en su nivel, y los interiores se anidan en el código del exterior. Cada salida de la región escribe los registros y devuelve el control con su pc y su última instrucción. CPython rechaza más de unos 20 bloques anidados estáticamente (HECHO [L7]); la profundidad 3 necesita cuatro: tres `while` y el `try` de la FPU.

*Puntos seguros.* Con bucles anidados, una vuelta exterior ya no tiene un coste acotado. El gas y el presupuesto se comprueban al empezar cada vuelta de cada bucle, contra el camino más largo desde esa cabecera hasta el siguiente punto seguro: la próxima cabecera que alcance (la suya al dar la vuelta, la de un bucle interior o la de uno exterior) o una salida de la región. Ese camino no tiene ciclos y se calcula al compilar. Si no alcanzan, el control vuelve al intérprete en esa cabecera con el estado exacto, y el intérprete agota el gas en la instrucción exacta, como hoy. La contabilidad se cierra en cada punto seguro, y cada punto de entrega declara su última instrucción observable: el salto de vuelta, la instrucción anterior a una cabecera interior a la que se llega sin saltar o el salto de salida.

*Variante B (despachador de bloques).* Una variable de bloque y una cadena de comparaciones por bloque básico admiten cualquier grafo de control reducible, incluido el camino fuera de línea de los sprites, a cambio de un despacho por bloque. Solo se construye si E3 descarta A, y solo si B no pasa de 0,6 µs por instrucción en `RM_COL`.

*Coste de compilar.* Se mantiene la regla actual: cada intento aplaza el siguiente 1 024 instrucciones, más 256 por instrucción de la región (22 272 para `RM_COL`). El tope de 256 instrucciones acota el código generado y la espera, y la simulación ya modela esa espera.

*Verificación.* Fuzzing diferencial de al menos 1 000 programas con bucles anidados (profundidad 1 a 3, saltos internos y de salida en cada nivel, barridos de gas y de presupuesto, pausas); anclas que cuentan compilaciones; mutantes del código generado; el juego con y sin acelerador con estado idéntico, incluido `snapshot_bytes` y también pausado a mitad de frame; y la simulación de `benchmark_cobertura.py`, exacta frente al acelerador real, con anclas nuevas en `CoverageSimulationAnchors`.

*Ganancia (CONJETURA, simulada [L6]).* El simulador de cobertura coincide hoy instrucción a instrucción con el acelerador real (HECHO [L6]); las filas siguientes aplican sus reglas extendidas.

| Acelerador | Cobertura | Interpretadas | Juego (estimado) |
|---|---|---|---|
| Actual (HECHO [L6]) | 85,67 % | 260 222 | 1,451 s |
| + P1a | 88,27 % | 213 064 | ~1,312 s (1,11×) |
| + P1a y bucles anidados, con cuerpos de 64 | 88,41 % | 210 582 | ~1,305 s |
| + P1a y cuerpos largos, sin bucles anidados | 88,27 % | 213 064 | ~1,312 s |
| + P1a y P1b | 96,65 % | 60 878 | ~0,863 s (1,68×; ~23 frames/s) |

- P1a solo compila más del DDA: 43 600 instrucciones de `RM_SX` (el paso en x) y 3 558 de `RM_DDA`.
- Los bucles anidados y los cuerpos largos solo valen juntos, porque `RM_COL` necesita las dos cosas.
- Con P1b, las interpretadas de `render_muros` bajan de 202 703 a 5 841; las de `colisiones`, de 6 086 a 4 748; las del minimapa, de 2 428 a 1 284; las de `render_sprites` siguen en 45 785.
- Supuesto de la estimación: las 199 344 instrucciones que pasan a compilarse cuestan lo que cuesta de media una compilada hoy (0,39 µs). Es optimista, porque `RM_COL` tiene `FDIV`, `FTOI` y más puntos seguros que un bucle simple; es pesimista, porque desaparecen las entradas y salidas del DDA, que hoy duplican su coste por instrucción.

*Límites.* P1b no cubre `render_sprites` (§2.2): lo abordan P2 o la variante B. Todo se mide en un solo juego.

*Coste.* ~400–700 líneas de Python y de pruebas, sobre todo en `vm32_loops.py` y `tests/test_vm32_loops.py` (CONJETURA).

### P2. Unidad de dibujo: relleno con paso y columna de textura escalada (PROPUESTA)

Ataca el 33,0 % de §2.1. Esos bucles ya están compilados, y abaratar la instrucción compilada no basta: aun con el acceso por página (0,31 µs por instrucción, RFC-EXP-00016 [L4]), un relleno ejecuta 7 instrucciones por cada 4 filas, ~0,54 µs por fila (CONJETURA, aritmética), frente a 22 ns en el host. RFC-EXP-00016 §P3 proponía, para el raycaster, un comando `COL(x, altura, tex, u)` en su lista de dibujo: el invitado hace el DDA y el host escala la columna. P2 lo convierte en instrucciones síncronas, sin la lista de dibujo ni el modo por frames de aquel RFC.

*Coste medido en el host (HECHO [L6]).* Prototipos en Python sobre las páginas de `PagedMemory` (slices con paso), comprobados contra una implementación palabra a palabra: relleno, 0,53 µs + 22 ns por fila (hoy, 31,6 µs por relleno); columna escalada con transparencia, 1,68 µs + 185 ns por téxel (hoy, 1 710 ns por téxel). Los prototipos no validan, no registran el deshacer y no cobran gas.

*ISA.* Capacidad nueva `draw`, fuera de la configuración por defecto, y sin chip: no depende del TNU, que exige Python ≥ 3.12 y un `TramoyaNeuralUnit` conectado (HECHO, `VM32.md`). Tres instrucciones con opcodes del bloque libre 68–98 (HECHO, `cpu_digital/vm32_isa.py`):

| Instrucción | Semántica |
|---|---|
| `DCFG Rp, Rs` | P ← Rp, el paso de destino en palabras (≥ 1), y S ← Rs, el paso de textura en 16.16 sin signo. Es estado del núcleo, como VL, VR y VS: entra en el snapshot solo si no es cero y se revierte con el registro de deshacer. |
| `FILL Rd, Rn, Rv` | `mem[Rd + k·P] ← Rv` para k = 0 … Rn−1. Con Rn = 0 no escribe y cobra el gas base. |
| `BLIT Rd, Ra, Rn` | Para k = 0 … Rn−1: `t ← mem[(Ra + k·S) >> 16]` (desplazamiento lógico; lee como `LOAD`) y, si t ≠ 0, `mem[Rd + k·P] ← t`. Es la secuencia `SHR`/`LOAD`/`JZ`/`STORE`/`ADD` de `RS_PIX`, salvo que falla si el acumulador desborda 32 bits. |

Ninguna modifica registros ni banderas. El téxel 0 es transparente, como en el juego.

*Validación, gas y atomicidad.* Siguen el orden del TNU: permisos, parámetros (P ≥ 1 y 0 ≤ Rn ≤ 2²⁰, el tope propuesto), gas, lectura, cálculo y una sola escritura.

- El destino [Rd, Rd + (Rn−1)·P] debe caer entero en la RAM. Falla si toca la ROM o código protegido.
- Con `protect_code=False`, si la escritura toca código, se invalida la caché de decodificación y se descartan los bucles compilados, como en `_tnu_store` (HECHO, `cpu_digital/vm32.py`).
- `BLIT` falla si el rango de téxeles se solapa con el de destino; así puede leer todo antes de escribir.
- Antes de escribir se guardan los valores previos de las Rn palabras en el registro de deshacer.
- Gas: 2 + ⌈unidades/64⌉, con una unidad por fila en `FILL` y dos en `BLIT`. Es la propuesta inicial; la fija E4.

*En el acelerador.* `FILL` y `BLIT` se compilan dentro de bucles y regiones desde el primer día, como llamadas a la misma función del host, y solo con clases exactas: si la memoria o las capacidades son subclases, o la VM sustituye algún método que el código generado reproduce, ejecuta el intérprete. Como su gas depende de Rn, cada una es además un punto seguro: antes de ejecutarla, el código generado calcula su gas y comprueba que el gas y el presupuesto alcancen para ella y para el camino más largo hasta el siguiente punto seguro. Si no alcanzan, o si la validación pudiera fallar, entrega el control al intérprete en ella. Sin esto, una `FILL` dentro de `RM_COL` impediría compilarlo con P1b, y las dos propuestas no se sumarían.

*Variante del juego.* `vm_programs/juego_raycaster_dibujo.tasm` es una copia que sustituye `RM_CEIL`, `RM_WL` y `RM_FL` por `FILL` de 4·g filas, con el mismo redondeo a múltiplos de 4 y el mismo exceso por debajo del framebuffer, y `RS_PIX` por un `BLIT` con el doble de filas que pares, que escribe exactamente lo mismo que el bucle; añade un `DCFG` al empezar cada sprite. El original no se toca. Prueba dorada: las mismas salidas (`SYSCALL 1`) y el mismo framebuffer, incluidas las 3 filas que el suelo puede escribir por debajo, al terminar cada uno de los 20 frames.

*Ganancia (CONJETURA).* Con los costes del prototipo más el despacho de una instrucción interpretada (3,40 µs) por relleno y por columna, el juego pasa de 1,451 a ~1,048 s (1,38×; ~19 frames/s). Si el dibujo no costara nada, el tope sería 1,49× (HECHO [L6], Amdahl sobre el tiempo medido). La estimación es optimista porque el prototipo no valida, no registra el deshacer y no cobra gas.

*Coste.* ~250–400 líneas, más la copia del juego y sus pruebas (CONJETURA).

### P1 y P2 juntas (CONJETURA)

Con P1b, los rellenos quedan dentro de `RM_COL` compilado y su despacho cuesta el de una instrucción compilada; las columnas de sprite siguen en el intérprete. Con los mismos supuestos, el juego pasa de 1,451 a ~0,432 s (3,36×; ~46 frames/s). Es la cifra menos fiable del documento, porque suma dos estimaciones optimistas.

## 4. Alternativas consideradas

- **Núcleo matemático escalar (coprocesador de coma flotante).** Cae: aunque la coma flotante no costara nada, el juego iría como mucho 1,19× más rápido (HECHO [L6]). Además, un coprocesador seguiría pagando el despacho de cada instrucción, que es lo que domina (3,40 µs interpretada frente a 0,39 µs compilada). **Residuo**: la cota de Amdahl sobre el tiempo, en `benchmark_dibujo.py`.
- **Segundo núcleo en paralelo, en otro hilo del host.** Cae: con el GIL, dos hilos no ejecutan bytecode Python a la vez, y quitarlo exige una compilación especial de CPython (HECHO [R3]). Dos núcleos sobre la misma memoria exigirían además una planificación determinista; hoy las fibras son cooperativas, no paralelas (HECHO, `VM32.md`). Con el dibujo repartido por columnas entre dos núcleos, el tope sería 2× (CONJETURA). Sin residuo.
- **Unidad de dibujo dentro del TNU, con `VCFG`.** Cae: heredaría Python ≥ 3.12 y el chip conectado, y sobrecargaría el sentido de VL, VR y VS. **Residuo**: el estado de configuración en el núcleo, que solo entra en el snapshot si no es cero, pasa a `DCFG`.
- **Framebuffer por columnas y `VCOPY`.** Cae: `VCOPY` copia, pero no rellena ni escala; exige `npu`, y cambiar la disposición del framebuffer cambia el contrato con quien lo muestra. **Residuo**: `FILL` con P = 1 cubre también los rellenos contiguos.
- **Solo bucles anidados o solo cuerpos largos.** Cae por la simulación: por separado no pasan del 88,41 % (§P1). **Residuo**: P1a, que sí vale sola.
- **P2 completo de RFC-EXP-00016 (traductor de bloques).** Sigue aplazado: con P1b, el 96,65 % de las instrucciones del juego estaría en bucles compilados (CONJETURA, simulada).

## 5. Riesgos, costes y experimentos asesinos

Riesgos principales:

- Divergencias de contabilidad en las regiones anidadas: gas, presupuesto y última instrucción en cada punto de entrega (P1b).
- Más tiempo de compilación por región (P1b).
- Una política de gas que cobre de menos frente al coste del host (P2).
- Menos simplicidad didáctica en la ISA (P2), como `XFORM` en RFC-EXP-00016 §P3.
- Todo se mide en un solo juego (las dos).

Experimentos, por coste ascendente:

- **E1 (minutos; P1b y P2). Ejecutado el 2026-10-02: VIVEN.** Repetir [L6] con Python 3.12.6 y 3.10.11. **Muere P1b** si en alguno el intérprete gasta menos del 35 % del tiempo, y **muere P2** si los bucles de dibujo bajan del 20 %. Resultado (HECHO [L6]): el intérprete gasta el 59,4 % del tiempo en 3.12.6 y el 61,4 % en 3.10.11, y los bucles de dibujo, el 33,2 % y el 31,4 %. Las estimaciones quedan en 1,39× y 1,36× para P2, 1,67× y 1,73× para P1, y 3,35× y 3,37× para las dos.
- **E2 (horas; P2).** Prototipo de `DCFG`, `FILL` y `BLIT` en una subclase de laboratorio, con validación, deshacer y gas, y la copia del juego. **Muere** si el juego no gana al menos 1,2× o si difiere alguna salida o el framebuffer de algún frame.
- **E3 (1–2 días; P1b).** Prototipo de la variante A solo para `RM_COL`, fuera del acelerador del repositorio, con el juego completo. **Muere** si el juego no gana al menos 1,3× o si el estado difiere. Si A no puede expresar `RM_COL`, se prueba B, que **muere** por encima de 0,6 µs por instrucción en `RM_COL`.
- **E4 (1 día; P2).** Calibración del gas de `FILL` y `BLIT` frente al intérprete con el acelerador desactivado (P0.3), en formas adversas: una fila, pasos que cruzan páginas, téxeles todos transparentes y destino al final de la RAM. **Muere** la política si ninguna de la forma 2 + ⌈unidades/k⌉ deja el coste por unidad de gas de todas las formas por debajo de 1,1× el del intérprete sin cobrar más de 2× a las del juego.
- **E5 (1–2 semanas; P1a y P1b).** Implementación completa con la verificación de §P1 y `benchmarks/benchmark_compilacion.py` repetido con regiones de hasta 256 instrucciones. **Muere** si aparece una divergencia que no se corrija sin volver a la profundidad 1 o a cuerpos de 64, o si el peor adversario de compilación pasa de 2× (hoy, 1,3×; RFC-EXP-00016 [L5]).

Preguntas abiertas: si `BLIT` debe admitir un color transparente distinto de 0, como `PALT` [R2]; si conviene una variante horizontal (P = 1) para otros juegos; si la variante B merece la pena para los sprites después de P2; y el tope de filas por instrucción.

## 6. Referencias

Fuentes externas consultadas el 2026-10-02.

- [R1] L. Vandevenne, «Raycasting», Lode's Computer Graphics Tutorial. https://lodev.org/cgtutor/raycasting.html
- [R2] Lexaloffle, «PICO-8 User Manual», v0.2.7. https://www.lexaloffle.com/dl/docs/pico-8_manual.html
- [R3] Python Software Foundation, «Glossary», entrada «global interpreter lock» (página de Python 3.14.8). https://docs.python.org/3/glossary.html
- [L6] `benchmarks/benchmark_dibujo.py --rondas 5` con el `cpu_digital` de `1972ef9`. Python 3.13.2 sobre Windows x64 (Ryzen 5 5600G), 2026-10-02, con el proceso fijado a un núcleo lógico, prioridad alta y la máquina en reposo. Los recuentos y la simulación son deterministas; los tiempos son la mediana de 5 rondas. Para E1, el mismo script con `--rondas 3` en Python 3.12.6 y 3.10.11, en la misma máquina y el mismo día.
- [L7] Límite de bloques anidados de CPython: compilar con `compile()` un programa de 21 `while` anidados da `SyntaxError: too many statically nested blocks` en Python 3.10.11; en 3.13.2, con 25. Comprobación local, 2026-10-02.
- RFC-EXP-00016 [L4] y [L5]: mediciones de la decisión sobre P2 y del coste de compilar, citadas con esas etiquetas.
