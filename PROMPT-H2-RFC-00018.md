# Prompt de la fase H2 del RFC-EXP-00018 («Un hart RV32 para Tramoya»)

Para la próxima sesión. Léelo entero antes de tocar nada. Es la continuación de
`PROMPT-IMPLEMENTACION-RFC-00018.md`; sus reglas siguen vigentes y aquí solo se añaden o se
precisan.

## 0. Estado de partida (HECHO, 2026-10-11)

- Rama `rfc-00018-hart-rv32`. H0 (E1, E2) y H1 (E3) cerrados; el usuario crea el PR con «Create
  PR». Trabaja desde `main` actualizado o desde una rama nueva creada a partir de él una vez
  fusionado ese PR; si aún no está fusionado, desde `rfc-00018-hart-rv32`. Nunca desde
  `acelerador-bucles`.
- Módulos de H1 en `cpu_digital/`: `rv32_isa`, `rv32_decode`, `rv32_alu`, `rv32_mem`, `rv32_hart`,
  `rv32_platform`, `rv32_blocks` (2 300 líneas); pruebas `tests/test_rv32_*.py` y
  `tests/test_benchmark_rv32_{memoria,embench}.py`; experimentos `benchmarks/benchmark_rv32_{arranque,
  memoria,coremark,embench}.py`; binarios versionados con sha256 en `benchmarks/rv32_arranque/bin/`
  (CoreMark rv32i/rv32im, 14 de Embench); puertos bare-metal en `benchmarks/rv32_arranque/{coremark,
  embench}/`.
- E3: intérprete 2,24 / 2,44 / 1,96 M instr/s en Python 3.13 / 3.12 / 3.10; bloques con horizonte
  3,21× / 3,18× / 3,52× el intérprete; horizonte +1–3 %. Embench (14 benchmarks): media geométrica
  3,30× (1,79×–4,99×). P1 vive. Todo en `VM32.md`, sección «Hacia un hart RV32 … hito H0» y «H1».
- Revisiones: dos ciegas sucesivas y oráculo matemático por módulo (ISA/decode llegó a cuatro).
  Artefactos en `.forja/h1/` (specs, `revisor_h1.json`, `cierre.json`, gates). El cierre mecánico
  de forja dice RECHAZADA solo por cuatro avisos de alcance heredados del commit cd2dd78 del
  usuario; está explicado en el informe de H1.
- Hallazgo medido pendiente de decisión (PROPUESTA): el coste de la invalidación por página de
  P1.6. Con los datos en la misma página que el código, cuatro benchmarks de Embench caen a
  1,0–2,1× (huffbench 1,03×). Hay una investigación con prototipo medido en
  `.forja/h2/investigacion-p16.md` y su diff en `.forja/h2/prototipo-p16.diff`: con un mapa de
  líneas de código de 64 bytes la media geométrica de Embench empaquetado sube de 2,66× a 3,17×
  (huffbench 1,03× → 2,82×), sin coste en CoreMark ni en Embench alineado.
- Memoria de sesión que debes leer: `ejecuciones-aisladas`, `rfc-00018-implementacion-h1`,
  `rfc-00018-implementacion-h0`, `entorno-gates-vm32`.

## 1. Lectura obligatoria, en este orden

1. `RFC-EXP-00018.md` entero (P1, P2, §4, §5 con E4 y E5, Anexo A) y `RFC-EXP-00018-mapa.md`.
2. `VM32.md` (secciones «Memoria paginada», «Ciclo de vida Tramoya», «Hacia un hart RV32» con H0
   y H1), `CHANGELOG.md`.
3. `.forja/h1/spec_*.md` (las especificaciones vigentes de los siete módulos) y
   `.forja/h2/investigacion-p16.md`.
4. `cpu_digital/rv32_*.py` y `tests/test_rv32_*.py` enteros; `cpu_digital/vm32.py` solo para el
   formato de snapshot y el journal (`_core_checkpoint`, `snapshot`, `restore`) y `cpu_digital/memory.py`
   para el formato paginado v2.
5. `benchmarks/benchmark_rv32_coremark.py` y `benchmark_rv32_embench.py` (arnés de ejecución y
   verdictos).

La clase VM32 y sus pruebas siguen en verde siempre; no se toca ningún archivo preexistente de
`cpu_digital/` salvo los `rv32_*.py`.

## 2. Disciplina (la de H1, más lo aprendido)

- Skills: forja (construir con spec y gates), maestranza (`scripts/banco.py --sin-venv`), batuta
  (contratos de subagentes), tests, logica-matematica. Cada módulo: especificación con criterios
  ejecutables y citas del manual → construcción aislada → banco verde con comando y salida literal
  → revisión matemática con oráculo independiente → revisión ciega; **módulos difíciles: dos
  revisiones ciegas sucesivas, la segunda sin ver los hallazgos de la primera; se integra solo
  cuando la segunda no encuentra nada nuevo** (si encuentra algo, se corrige y se vuelve a
  revisar). Las desviaciones de esta regla se declaran en el informe, no se esconden.
- **Aislamiento obligatorio de toda ejecución** (el 2026-10-10 cinco revisores sin tope agotaron
  los 32 GB de RAM y mataron la sesión): todo comando que ejecute Python o LLVM va dentro de un Job
  Object de Windows con tope de memoria y tiempo. El envoltorio `aislar.py` vive en el scratchpad
  de la sesión anterior (`C:\Users\Raul\AppData\Local\Temp\claude\D--Cpu-digital\bc6661c6-…\scratchpad\
  aislar.py`); si no existe, recréalo (~150 líneas, ctypes): `CreateJobObjectW`,
  `SetInformationJobObject` con `JOB_OBJECT_LIMIT_JOB_MEMORY | JOB_OBJECT_LIMIT_PROCESS_MEMORY |
  JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, hijo creado suspendido (`CREATE_SUSPENDED`), asignado al job
  y reanudado; `--mem MB --tiempo S -- comando…`; sale 124 por tiempo y 137 por memoria; el
  ejecutable con ruta absoluta (`CreateProcess` no resuelve rutas relativas con «/»). Uso:
  `C:\Python313\python.exe aislar.py --mem 1500 --tiempo 600 -- "D:\Cpu digital\.venv\Scripts\python.exe" …`.
- Subagentes: **como mucho dos a la vez**; cada contrato exige el envoltorio en cada comando, un
  solo proceso Python por revisor, nada en segundo plano, nada de `timeout` de bash ni bucles
  `Monitor`/`sleep` como guarda, mutantes de uno en uno, sin bitácoras con copias de memoria por
  paso, RAM del hart ≤ 64 KiB salvo CoreMark (1 MiB), cerrar toda `Rv32Memory`/`Machine`. Si un
  revisor devuelve 124/137, lo anota y no repite.
- Gates de forja: `forja_gates.py correr --etiqueta <hito>_final` con **los mismos comandos del
  baseline** (G0 `.venv\Scripts\python.exe -m compileall -q cpu_digital tramoya_vm.py cpu_simulator.py
  tests benchmarks`; G1 `C:/Python313/python.exe -m ruff check cpu_digital tests benchmarks tramoya_vm.py`;
  G3 `.venv\Scripts\python.exe -m unittest discover -s tests`), envolviendo el propio
  `forja_gates.py` en `aislar.py`, no el comando interior (si el comando cambia, el cierre lo
  penaliza). Al empezar H2, **toma una instantánea de alcance nueva** (`forja_alcance.py snapshot`
  con los permitidos de H1 más `.forja/h2/`) para que el cierre no herede los avisos de H1.
- Archivos: el árbol de trabajo es CRLF en los preexistentes (`VM32.md`, `CHANGELOG.md`,
  `cpu_digital/vm32.py`…) y LF en los nuevos de H1; usa la herramienta Edit o un sustituidor
  exacto que conserve el fin de línea de cada archivo. **No uses heredocs de Git Bash con `\n` o
  `\\` dentro de código Python**: llegan como saltos reales. Escribe los scripts con la herramienta
  Write y ejecútalos.
- Descargas: solo con permiso explícito del usuario, a un directorio vacío del scratchpad,
  registrando URL, etiqueta/commit y sha256 en el informe y en los módulos que las usan. Nada se
  instala; no se crean venvs. Python 3.13.2 (`.venv`), 3.12.6 y 3.10.11 locales; ruff 0.8.6 con
  `C:\Python313\python.exe -m ruff`; clang/llvm 21.1 con objetivo riscv32 (sin GCC RISC-V, QEMU,
  Spike ni PyPy; sin compiler-rt ni libm para riscv32: `builtins_rv32i.c` y `builtins64_rv32i.c`
  cubren mul/div/shift de 32 y 64 bits; no hay soft-float).
- Medidas: comprueba la carga de la máquina antes y después (tres muestras de `LoadPercentage`), mínimo
  de ≥ 2 rondas, recuentos deterministas aparte de los tiempos; una medida con la máquina cargada
  «no vale» y se repite.
- Ni commit ni push sin permiso explícito del usuario; él usa «Create PR».

## 3. Orden de trabajo de H2

Cada paso cierra con sus pruebas en 3.13 y 3.10, banco, revisiones, documentación
(`VM32.md` «hito H2», `CHANGELOG.md`, RFC: Estado y E4/E5 «Ejecutado el…») y un informe al usuario
con el formato de §6. **Si una cifra asesina del RFC se alcanza, paras, lo documentas con las
mediciones literales y lo presentas; no sigues por inercia ni rebajas el umbral.**

### H2.0 Enmienda de P1.6: mapa de líneas de código (requiere «adelante» del usuario)

1. Presenta al usuario `.forja/h2/investigacion-p16.md` (problema, prototipo, cifras, alternativas,
   riesgos) y el texto de enmienda del RFC propuesto. Espera su decisión. Si aprueba:
2. Especificación forja `.forja/h2/spec_p16_lineas_codigo.md` a partir de la investigación
   (criterios: toda palabra decodificada o compilada está en una línea marcada antes de entrar en
   caché; un store a una línea marcada sube la generación de su página; un store a una línea no
   marcada solo marca la página como escrita; `written_pages`/snapshot desde `written`; las
   escrituras del host suben siempre la generación; `reset`/`attach_memory` vacían el mapa; invariante
   «byte ≠ 0 ⇒ página escrita»; equivalencia bit a bit intérprete/bloques/cortes con stores sobre
   código, sobre datos de la misma página y sobre datos de la misma línea).
3. Implementa sobre `rv32_mem.py`, `rv32_hart.py`, `rv32_blocks.py` siguiendo el diff del prototipo
   (`.forja/h2/prototipo-p16.diff`), con la misma disciplina que H1 (los tres son módulos difíciles).
   Actualiza las seis pruebas que fijaban generaciones por store de datos (`test_matches_the_model`,
   `test_out_of_range_and_boundary`, `test_generation_counts_every_store_on_each_page_touched`,
   `test_restore_validates_before_touching`, `test_snapshot_is_exact_and_v2_compatible`,
   `test_recompilation_is_capped_per_pc`) a la semántica nueva y añade las que prueban la frontera
   línea/código (store a la línea de un bloque, a la línea contigua, a la misma página sin código,
   `sh` que cruza línea, stores del host).
4. Cifras: Embench empaquetado (`--ld empaquetado --sin-instalar`) ≥ 3,0× de media geométrica y
   ningún benchmark por debajo de 1,5× salvo nsichneu (bloques de una o dos instrucciones); CoreMark
   y Embench alineado sin regresión (±3 %); `ud` queda como caso a explicar (sus datos comparten
   línea de 64 bytes con código: medir con líneas de 16 bytes antes de decidir el tamaño).
5. Decide y documenta el tamaño de línea (64 frente a 16 bytes) con las dos medidas.

### H2.1 Snapshot y restauración del hart (P2.5, inventario de estado)

- `Rv32Hart.snapshot()`/`restore()` y `Machine.snapshot()`/`restore()`: registros, `pc`, `instret`,
  gas pendiente no (lo fija el arnés), CSR implementados, reserva LR/SC (campo presente aunque H3
  la use), memoria con el formato paginado v2 desde las páginas escritas, UART (colas de entrada y
  salida), CLINT (`mtimecmp`), `accelerate`/`horizon`/`warmup` como configuración, nunca las cachés
  (decodificación, bloques, mapa de código): se declaran derivadas en el inventario (P2.5).
- Hash de estado (P2.3) estable entre ejecuciones, para el arnés de E4.
- Restaurar sobre un hart que ya ejecutó debe vaciar cachés y mapa de código; la memoria
  restaurada sube generaciones (ya es así en `restore_pages`).

### H2.2 Invariante S bajo cortes (E4)

- Arnés `benchmarks/benchmark_rv32_invariante.py` (o módulo de pruebas): `run(B1); snapshot;
  restore en una máquina nueva; run(B2)` frente a `run(B1 + B2)`: registros, CSR, memoria, `instret`,
  gas, UART, `Stop`. Cortes uniformes y **sesgados** a stores sobre código, salidas lentas de bloque,
  MMIO (UART, SYSCON), `WFI`/`mtimecmp` cuando existan (H3). 1 000 cortes por programa sobre
  CoreMark, los 14 de Embench y programas aleatorios con código automodificable; con y sin bloques.
- **Mata P1 (y P3)** una divergencia que solo se cure reintroduciendo el checkpoint por
  instrucción o un coste que dependa de la partición.

### H2.3 riscv-tests rv32ui (E5, primera parte)

- Pide permiso para descargar `riscv-software-src/riscv-tests` (fija el commit y el sha256 del
  zip). Compila `isa/rv32ui/*.S` con el ensamblador integrado de clang (`--target=riscv32-unknown-elf
  -march=rv32i_zicsr -mabi=ilp32`), con un entorno propio (`env/p` adaptado: `riscv_test.h` con
  `RVTEST_PASS`/`RVTEST_FAIL` escribiendo en SYSCON/UART, sin traps hasta H3; `link.ld` propio).
  Versiona los binarios y sus sha256 como con CoreMark. Lo que clang no ensamble (macros de GAS)
  se documenta con el error literal.
- Ejecuta cada prueba entera y **fragmentada** con K = 64 cortes con snapshot/restore entre cortes.
  Mutantes de restauración errónea (un campo presente pero mal restaurado; un bloque compilado no
  invalidado tras restaurar; mapa de código no vaciado), filtrados por vivacidad. **Mata P2** si el
  fragmentador detecta menos del 90 % de los mutantes vivos o si los cortes sesgados no superan a
  los uniformes.

### H2.4 ACT4 (riscv-arch-test), si cabe

- Pide permiso para descargar `riscv-non-isa/riscv-arch-test` (commit fijo). Solo `rv32i_m/I` con
  las firmas de referencia que trae el repositorio (no hay Sail). Si los macros no ensamblan con
  clang, documéntalo como no cubierto; no es bloqueante para H2.

### H2.5 Cierre

- `VM32.md` «hito H2», `CHANGELOG.md`, RFC (Estado: «H2 (E4, E5) ejecutado el …»; E4 y E5 con las
  cifras literales; enmienda de P1.6 si se aprobó); `.forja/h2/` con specs, `revisor_h2.json`,
  gates y cierre; actualiza la nota de memoria `rfc-00018-implementacion-h1` con una de H2 y la
  índice; informe final.

## 4. No negociables

- Python puro sin dependencias nuevas; clang solo para compilar binarios de prueba, versionados con
  sus firmas; las fuentes externas (CoreMark, Embench, riscv-tests) no se versionan, solo los puertos
  propios y los binarios.
- La clase VM32, su ISA, su gas y sus programas no cambian de comportamiento; la política de gas
  del TNU no se toca sin enseñar antes la tabla al usuario.
- La numeración de `Kind` es estable (prueba `test_kind_numbering_is_frozen`): H3 añade al final.
- `accelerate=False` sigue por defecto hasta que la invariante S esté probada con bloques (H2.2);
  entonces se propone activarlo por defecto, con la cifra.
- HECHO/PROPUESTA/CONJETURA nunca mezclados en los documentos.
- Nada de commit ni push sin permiso explícito; nada de instalaciones ni venvs; descargas solo con
  permiso y con sha256.

## 5. Trampas conocidas (resumen; detalle en la memoria)

- `forja_gates.py correr` ejecuta en cmd: `.venv\Scripts\python.exe` con barras invertidas.
- `banco.py` sobre `benchmarks/` necesita `--sin-copia --sin-venv` y `PYTHONPATH` con el
  site-packages del venv (`benchmark_tnu.py` lee `llama2.tasm` al importarse); el smoke va en
  PowerShell sin `&&`.
- `forja_oraculo.py`/`forja_alcance.py` necesitan `PYTHONUTF8=1`; el oráculo marca como sospechoso
  `except (AttributeError, OSError): pass` tras `sys.stdout.reconfigure` (justificar).
- Para 3.10/3.12: `PYTHONPATH="D:/Cpu digital/.venv/Lib/site-packages"` y `-m` desde la raíz.
- PowerShell 5.1 no tiene `&&`; `timeout` de Git Bash no mata procesos nativos.
- `ConsoleOriginTests` es intermitente en Windows (repetir).
- Un `Machine()` por defecto reserva 64 MiB de commit; en bucles usa RAM pequeña y `close()`.

## 6. Formato del informe

Estado (HECHO/PROPUESTA/CONJETURA), comandos con salida literal (gates, banco, benchmarks con la
carga de la máquina), revisores desplegados y sus hallazgos con lo corregido, revisión matemática
con oráculo, cifras frente a los umbrales del RFC con el veredicto, qué quedó fuera y por qué,
desviaciones de la disciplina declaradas.
