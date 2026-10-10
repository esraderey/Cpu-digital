# Prompt de implementación de RFC-EXP-00018 (hart RV32 para Tramoya)

Pégalo tal cual en una sesión nueva de Claude Code abierta en `D:\Cpu digital`.

---

Implementa el RFC-EXP-00018 («Un hart RV32 para Tramoya», estado PROPUESTO, firmado OMRI) siguiendo esta disciplina sin excepciones.

## 1. Antes de escribir una línea

1. Lee entero `RFC-EXP-00018.md` y después `RFC-EXP-00018-mapa.md`. Lee `VM32.md`, `cpu_digital/vm32.py`, `cpu_digital/memory.py` y `cpu_digital/vm32_loops.py` para conocer qué se reutiliza (memoria paginada, journal, snapshots, capacidades, arnés diferencial) y qué no se toca (la clase VM32 y sus 220+ pruebas deben seguir en verde siempre).
2. Comprueba `git status`, `git log` y la rama. Trabaja desde `main` actualizado o desde una rama nueva creada a partir de él, nunca desde `acelerador-bucles`, que está diez commits por detrás. No hagas commit ni push sin mi permiso explícito; cuando lo dé, usaré «Create PR».
3. Carga las skills `forja`, `maestranza`, `batuta`, `tests` y `logica-matematica`, y léelas antes de planificar. Si despliegas subagentes, `batuta` gobierna los contratos.
4. Herramientas disponibles en esta máquina: clang 21.1 con objetivo `riscv32` (compila los binarios de prueba con `--target=riscv32-unknown-elf -march=rv32i`/`rv32im` `-mabi=ilp32`); no hay GCC RISC-V, QEMU, Spike ni PyPy. No instales nada ni crees venvs nuevos: usa `.venv\Scripts\python.exe` y, para ruff, `C:\Python313\python.exe -m ruff` (0.8.6, la versión de la CI). El árbol de trabajo está en CRLF; usa la herramienta Edit, no heredocs, para tocar archivos existentes.

## 2. Orden de trabajo

Sigue los hitos del RFC (§3, «Orden de construcción») y no adelantes ninguno:

- H0: experimentos E1 y E2 sin tocar el núcleo. E1 instrumenta mini-rv32ima para contar las instrucciones del arranque de Linux nommu; E2 mide carriles de bytes sobre `array('i')` y el anillo de deshacer. Sus cifras fijan las expectativas de todo lo demás.
- H1: RV32I, bloques compilados, horizonte, UART; CoreMark y Embench compilados con clang. Cierra con E3.
- H2: riscv-tests, ACT4 versionado, invariante S, puerta de inventario. Cierra con E4 y E5.
- H3: M, A, Zicsr, traps M/U, CLINT, DTB; FreeRTOS, Zephyr y Linux nommu.
- H4 (P3), H5 (P4), H6 (P5) y H7 (fase 2) solo después, cada uno con su experimento.

Cada experimento del RFC (§5) tiene una cifra que mata la propuesta. Si una cifra se alcanza, paras, lo documentas con las mediciones literales y me lo presentas; no sigues construyendo por inercia ni rebajas el umbral.

## 3. Ciclo obligatorio por módulo (forja + maestranza)

Todo se construye como módulos nuevos dentro de `cpu_digital/` (p. ej. `rv32_isa.py`, `rv32_decode.py`, `rv32_alu.py`, `rv32_mem.py`, `rv32_csr.py`, `rv32_hart.py`, `rv32_blocks.py`, `rv32_platform.py`, `rv32_snapshot.py`, `rv32_timing.py`), cada uno con su archivo de pruebas. Para cada módulo, en este orden y sin saltar pasos:

1. **Especificar** con `forja`: criterios ejecutables extraídos del RFC y de la especificación RISC-V (cita la sección exacta del manual para cada semántica), riesgo clasificado, y la lista de qué módulos existentes toca (idealmente ninguno hasta la integración).
2. **Construir aislado**: el módulo no importa nada de VM32 salvo lo que el RFC manda reutilizar; sin efectos en el resto del paquete.
3. **Pasar la maestranza**: banco efímero con `scripts/banco.py --sin-venv` (compilar, importar, ruff, unittest, smoke). Se repara con arreglo mínimo y se vuelve a ejecutar hasta verde. Nada se da por bueno de palabra: cada «verde» va con el comando y su salida literal.
4. **Revisar las matemáticas** (siempre, en todo módulo con semántica numérica; ver §4).
5. **Revisión ciega**: un revisor independiente que solo ve la especificación y el diff, nunca el razonamiento del implementador. Los módulos difíciles (§5) pasan **dos revisiones independientes y sucesivas**: la segunda la hace otro revisor que no ha visto los hallazgos de la primera; solo se integra cuando la segunda no encuentra nada nuevo. Si la segunda encuentra algo, se corrige y se repite la segunda revisión.
6. **Integrar** con el diff mínimo y en un solo paso: conectar el módulo, ejecutar los gates completos del repo (`compileall`, ruff, `unittest discover -s tests` con las pruebas antiguas y las nuevas, y el fuzzing diferencial existente) y comprobar que la clase VM32 heredada no cambia ni un resultado.
7. **Definición de hecho**: compila, importa, pasa ruff, pasa sus pruebas y las de todo el repo en 3.13 y 3.10, tiene revisión (una o dos según §5), revisión matemática con oráculo independiente, y una sección «No cubierto» honesta. Sin eso no está hecho.

Un módulo que no pasa la maestranza o una revisión no se integra «para seguir avanzando»: se repara o se rechaza con evidencia.

## 4. Revisión de las matemáticas (obligatoria, con oráculo independiente)

El revisor matemático recalcula por su cuenta, con un script propio o a mano, y nunca se fía de las pruebas del implementador. Usa `logica-matematica` y `tests` (propiedades, diferencial, mutantes). Lista mínima:

- Aritmética int32 en complemento a dos: envolvimiento en todas las operaciones; comparaciones con y sin signo (`SLT`/`SLTU`, `BLT`/`BLTU`); desplazamientos que solo usan los 5 bits bajos del operando.
- Extensión M: `MUL`, `MULH`, `MULHU`, `MULHSU` contra el producto exacto de 64 bits; `DIV`/`DIVU`/`REM`/`REMU` con los casos de la especificación (división entre cero y desbordamiento de `INT_MIN / -1`) verificados uno a uno contra el texto del manual.
- Memoria: extracción de carriles de bytes y medias palabras con y sin signo (`LB`/`LBU`, `LH`/`LHU`), escrituras parciales que no toquen los demás bytes, accesos desalineados que cruzan dos palabras, y la equivalencia bit a bit con una memoria de referencia en `bytearray` bajo fuzzing.
- CSR y traps: campos WARL, valores de `mcause`, `mepc`, `mtval` y el orden fijo de las escrituras múltiples en la entrada al trap.
- Contadores: el gas del host y `instret`/`mcycle` del invitado se recalculan con el oráculo independiente del RFC (P2.4) sobre la bitácora; deben coincidir exactamente, también bajo cortes y restauraciones.
- Invariante S: `run(B1); snapshot; restore; run(B2)` idéntico bit a bit a `run(B1+B2)` en registros, CSR, memoria, gas, `instret` y salida, con cortes sesgados a LR/SC, entrada en trap, escritura de `mtimecmp` y `WFI`.
- Hash de estado: h(dirección, valor) por palabra, nunca solo el valor; demostrar con un contraejemplo que el intercambio de dos direcciones cambia el hash.
- Modelo de tiempo (P3): las ecuaciones del marcador de disponibilidad y el coste por bloque deben dar el mismo total con cualquier partición en bloques; prueba por igualdad exacta con el marcador dinámico del intérprete.
- Numérica del TNU (P4): producto escalar como `f32(fsum(f64·f64))` verificado contra un oráculo con `Fraction`; el polinomio de `exp` con cota de error declarada y medida.

Toda fórmula que aparezca en código o en documentación lleva al lado el caso límite que la prueba y el oráculo que la confirma.

## 5. Qué cuenta como módulo difícil (dos revisiones)

Decodificador y codificación RV32I/M/A/Zicsr; ALU y extensión M; capa de bytes y accesos desalineados; CSR, privilegio y traps; compilador de bloques con salida lenta única y volcado de registros; horizonte de eventos y dos contadores; snapshot, restauración e inventario de estado; hash incremental; modelo de tiempo; numérica del TNU; fotogramas del núcleo nativo. En la duda, es difícil. Son fáciles (una revisión): UART, generador de DTB, pegamento del CLI, documentación.

## 6. Reglas que no se negocian

- Python puro sin dependencias nuevas; clang solo para compilar binarios de prueba, que se versionan junto con sus firmas para que la CI no dependa de la toolchain.
- La clase VM32, su ISA, su gas y sus programas no cambian de comportamiento. Si algo del RFC exige tocarlos, me lo presentas antes con las cifras.
- No cambies la política de gas del TNU sin enseñarme antes la tabla de antes y después.
- Antes de dar por buena una cifra de tiempo, comprueba la carga de la máquina y repite la medición; los recuentos (instrucciones, compilaciones) son deterministas y no dependen de la carga.
- Documenta en `VM32.md` y `CHANGELOG.md` cada hito cerrado, con sus mediciones y su «No cubierto», en el mismo registro que los RFC: HECHO con fuente o medición, PROPUESTA y CONJETURA nunca mezclados.

## 7. Cómo reportas

Al cerrar cada módulo y cada hito: estado (cumplido, parcial o bloqueado), comandos ejecutados con su salida literal, qué revisores pasaron y qué encontraron, el resultado de la revisión matemática con su oráculo, qué quedó fuera y por qué. Si un experimento asesino mata una propuesta, el informe lo dice en la primera línea.

Empieza por H0: no toques el núcleo hasta tener las cifras de E1 y E2.
