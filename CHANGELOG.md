# Historial de cambios

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/).
Todavía no hay versiones publicadas: el paquete declara la versión 2.0.0 desde el
primer commit, así que los cambios se agrupan por fecha.

## [Sin publicar]

### 2026-10-02

- `RFC-EXP-00017.md` (propuesto): qué le falta al raycaster de referencia para
  ir más rápido. El 59,6 % del tiempo se va en las instrucciones que siguen en
  el intérprete y el 33,0 % en cuatro bucles de dibujo; un núcleo matemático
  daría como mucho 1,19×. Propone extender el compilador de bucles a varios
  saltos de vuelta y a regiones con bucles anidados y cuerpos largos, y una
  unidad de dibujo con un relleno con paso y una columna de textura escalada,
  y cinco experimentos que pueden descartarlas antes de construirlas; el
  primero, ya hecho en Python 3.12 y 3.10, no las descarta.
  `benchmarks/benchmark_dibujo.py` reproduce sus cifras.
- **Corrección:** el coste de compilar del acelerador de bucles queda acotado
  también en los programas que no escriben en su código. Antes compilaban cada
  bucle a sus 32 saltos y sin espera, así que el coste lo acotaba el tamaño del
  programa y no el gas: con muchos bucles caros de 33 vueltas, unas 40 veces más
  CPU del host con el mismo gas. Ahora cada intento de compilación aplaza
  siempre el siguiente (1 024 instrucciones ejecutadas, más 256 por instrucción
  del bucle). Al pagar esa espera cuentan doble las instrucciones de cada
  entrada al código compilado que ejecuta al menos tantas como tiene el bucle:
  un bucle que se usa se paga antes. Con programas adversarios que ejecutan
  justo lo que paga cada espera, el peor medido queda en 1,3×. La primera
  compilación tras cargar un programa sigue sin esperar, así que uno muy corto
  aún puede costar una compilación de más (~8 ms).
- La regla es la misma si el programa escribe en su código: desaparece la
  distinción que se introdujo el día anterior. Los que parchean su código una
  vez por ronda y tienen varios bucles calientes recuperan la aceleración
  (0,35× del intérprete frente a 0,76×).
- Precio: si un programa compila un bucle y apenas lo usa, los que se calientan
  después esperan en el intérprete hasta que esa compilación está pagada. En el
  raycaster de referencia, la cobertura con 20 frames pasa del 85,9 % al
  85,7 %, sin cambio medible en el tiempo.
- `benchmarks/benchmark_compilacion.py`: coste de compilar en programas que no
  escriben en su código, con y sin acelerador, contando los intentos de
  compilación. `benchmarks/benchmark_cobertura.py` simula también la espera.
- Pruebas: anclas que cuentan las compilaciones sin escrituras en código, otra
  de la simulación de cobertura y fuzzing diferencial de 80 programas con
  varios bucles; los fuzz comprueban además cada decisión de compilar contra la
  regla.

### 2026-10-01

- **Corrección:** la FPU escalar redondea a ±infinito al desbordar float32 aunque
  el Python anfitrión lance `OverflowError` al empaquetar el resultado. Así se
  comporta CPython 3.13.15, mientras que 3.13.2 todavía devolvía infinito. Antes,
  el intérprete fallaba con una excepción de host y el código compilado del
  acelerador la dejaba escapar de `run()`. Lo destapó la primera ejecución de la
  integración continua.
- **Corrección:** con `protect_code=False`, un bucle que escribía en su propio
  código recompilaba en cada vuelta sin acelerar nada: 34 veces más CPU del host
  con el mismo gas. Ahora una escritura en código reinicia también el
  calentamiento de las cabeceras y, desde la primera, cada intento de compilación
  aplaza el siguiente 1 024 instrucciones ejecutadas, más 256 por instrucción del
  bucle compilado. Ese caso queda en 1,02× y el peor programa adversario medido,
  en 1,4×. Los programas que no escriben en su código compilan igual que antes.
  Los que parchean su código una vez por ronda y tienen varios bucles calientes
  conservan menos aceleración que antes (0,76× del intérprete frente a 0,27×),
  porque sus bucles ya no se recompilan todos de inmediato.
- `benchmarks/benchmark_automodificable.py`: coste del acelerador con código
  automodificable, con y sin él, contando los intentos de compilación.
- Pruebas: anclas que cuentan las compilaciones y fuzzing diferencial de 210
  bucles que parchean su propio código.

### 2026-09-30

- **Acelerador de bucles ampliado:** el cuerpo compilado admite saltos hacia
  delante (dentro del cuerpo y salidas anticipadas), `DIV`/`MOD`, `PUSH`/`POP`,
  `FCMP`, `FTOI`, `FDIV` y `FSQRT`, con las mismas guardas que el intérprete.
- **Corrección:** el acelerador ya no actúa si una subclase o la instancia
  sustituyen un manejador `_op_*` o un método del que depende el código
  generado. Antes lo ignoraba y dejaba un estado distinto del intérprete.
- `vm_programs/juego_raycaster.tasm`: raycaster de referencia con 16 entidades,
  colisiones, sprites y minimapa, escrito sin conocer el acelerador.
- `benchmarks/benchmark_cobertura.py`: cobertura y aceleración con y sin
  acelerador, desglose por fase y por bucle, y estimación de extensiones sobre la
  traza del intérprete. En el juego, la cobertura sube del 41,4 % al 85,9 % y la
  aceleración llega a 3,2×.
- Decisión sobre P2 en [RFC-EXP-00016](RFC-EXP-00016.md): P2 completo queda
  aplazado y se sigue ampliando P2-lite.
- Pruebas: anclas por extensión, fuzzing diferencial ampliado (800 bucles) y el
  juego con y sin acelerador.
- Preparación para GitHub:
  - licencia MIT, guía de contribución, código de conducta y política de seguridad;
  - este historial;
  - plantillas de issues y pull requests;
  - integración continua (lint y pruebas);
  - Dependabot para las acciones y `.gitattributes`;
  - metadatos completos en `pyproject.toml`.
- Eliminado `from tramoya import MachineBuilder.py`, un lanzador duplicado de
  `cpu_simulator.py`.

### 2026-09-29

- **Acelerador de bucles de VM32 (P2-lite):**
  - compila los bucles internos calientes a funciones Python con el mismo estado
    que el intérprete; `ADDI`+`JMP` pasa de ~0,56 a ~5,5 M instr/s;
  - opción `--no-loop-acceleration`;
  - micro-optimizaciones del intérprete (de ~0,40 a ~0,57 M instr/s);
  - los resultados NaN de la FPU se guardan como NaN canónico, para que sean
    deterministas.
- **Chip Neuronal Tramoya (TNU):**
  - 18 instrucciones vectoriales tras la capacidad `npu`, con la ROM de pesos
    fuera de los snapshots;
  - `vm_programs/llama2.tasm` ejecuta modelos de llama2.c (stories260K a ~42
    tok/s);
  - `benchmarks/benchmark_tnu.py`.
- **Auditoría de VM32 y de la consola:** anclas de regresión y correcciones sobre
  el origen de las peticiones a la consola, los costes, el rollback, la
  restauración de snapshots, los contratos de error, los límites de recursos, las
  comparaciones con signo y las fibras.
- [RFC-EXP-00016](RFC-EXP-00016.md): pantalla determinista y aceleración por
  bloques.

### 2026-09-23

- **VM32:**
  - FPU IEEE 754 de precisión simple (9 instrucciones);
  - aritmética de 64 bits (`MULH`, `ADDX`, `SUBX`);
  - fibras cooperativas (`SPAWN`, `SWITCH`, `FRET`).
- **Chip de memoria no volátil:**
  - 500 MB sobre SQLite, con syscalls 9–11;
  - integrado en el CLI y en la consola.
- Programa `vm_programs/prediccion_tendencia.tasm`.
- Plan para ampliar la RAM de VM32.

### 2026-09-19

- **Versión inicial.** CPU Digital 16:
  - CPU acumuladora con unidad de control Tramoya;
  - ensamblador, depurador, snapshots y diagramas.
- **Versión inicial.** Tramoya VM32:
  - runtime RISC de 32 bits con ISA fija de 4 palabras y ensamblador;
  - gas, capacidades, syscalls e interrupciones;
  - memoria paginada, snapshots y bytecode `.tvm`.
- Consola web local, pruebas y benchmark de VM32.
