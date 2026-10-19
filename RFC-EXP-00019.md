# RFC-EXP-00019 — Núcleo nativo en Rust para el hart RV32: un ejecutor rápido que el hart Python certifica

| Campo | Valor |
|---|---|
| Identificador | RFC-EXP-00019 |
| Estado | PROPUESTO (RFC de investigación abierto por decisión del usuario del 2026-10-12, RFC-EXP-00018 §5 «Hoja de ruta»); ningún experimento ejecutado salvo la prueba de humo del toolchain [L4] |
| Fecha | 2026-10-17 |
| Firmado | OMRI |
| Relacionado | RFC-EXP-00018 (P1 hart, P2 arnés bajo corte, P2.3 hash, P2.5 inventario, P5 núcleo nativo, E4, E8, «Hoja de ruta»); RFC-EXP-00015 (§32 criterios para abandonar el estado experimental) |
| Expedición | ¿Cómo superar el techo de velocidad del hart RV32 en Python integrando un núcleo en Rust **sin** que el proyecto deje de ser «Python puro como especificación ejecutable», sin perder el gas exacto, el determinismo ni la invariante S, y con una validación más estricta que la del resto del repositorio? |

**Regla de tinta.** **HECHO** marca afirmaciones con fuente externa [R#] o con lectura o medida del repositorio y de la máquina del proyecto [L#]; **PROPUESTA** marca diseño nuestro; **CONJETURA** marca estimaciones sin medir, incluidas las de coste y rendimiento. Nunca se mezclan sin marca. Lo no localizado en el barrido se dice como tal. Las referencias escritas como «RFC-EXP-00018 [R#]» o «RFC-EXP-00018 [L#]» remiten a las listas de ese documento, no a las de este.

**Regla de validación reforzada (solo para la parte Rust).** Todo lo que ejecute código nativo pasa por la escalera de §3 N4 completa antes de considerarse integrado, y su revisión tiene tres ciegas sucesivas en vez de dos, una de ellas de un revisor que solo mira `unsafe`, la frontera ABI y los enteros. El resto del proyecto sigue con la disciplina de siempre (forja, maestranza, dos ciegas, oráculo).

## 1. Resumen

El hart RV32 de RFC-EXP-00018 ejecuta hoy 2,2–2,4 M instr/s interpretando y 7,2–7,7 M con bloques compilados (HECHO [L1]); con las 65 M de instrucciones que mini-rv32ima contó hasta el shell de Linux nommu (HECHO [L1]), el arranque se estima entre 15 y 60 s (CONJETURA, RFC-EXP-00018 §5). Los intérpretes nativos de la misma clase rinden 40–400 MIPS (HECHO, RFC-EXP-00018 §2.3 y sus referencias RFC-EXP-00018 [R17], [R22] y [R23]): dos órdenes de magnitud. RFC-EXP-00018 P5 ya propone un núcleo nativo opcional cuya ejecución certifica el hart Python por ventanas; aquí se concreta ese núcleo **en Rust**, se fija la frontera con Python (un `cdylib` con ABI C cargado por `ctypes`, sin dependencias nuevas), se define la certificación por fotogramas y, sobre todo, se exige una escalera de validación propia: tipos y lints que prohíben `unsafe` fuera de la frontera, equivalencia instrucción a instrucción con el hart Python, invariante S también en el núcleo, fuzzing diferencial, Miri y Kani sobre el código Rust, mutación medida sobre el certificador, cadena de suministro vacía y compilación reproducible con carga condicionada al hash. El hart Python no pierde su papel: sigue siendo la especificación ejecutable y el único juez; el núcleo Rust es un ejecutor rápido verificado a posteriori, opcional, y el proyecto funciona igual sin él.

Seis experimentos (EN1–EN6) con cifra asesina deciden si el núcleo entra: si un intérprete Rust de RV32I no supera en 5× a los bloques Python en esta máquina, o si certificar CoreMark y Embench no cabe en la CI, o si el certificador deja vivos más del 5 % de los mutantes del núcleo, la propuesta muere o se rediseña.

## 2. Estado del arte (mapa breve; detalle y verificación en el Anexo A)

### 2.1 Punto de partida (HECHO, repositorio)

- El hart Python implementa RV32I y los contadores Zicntr con comprobar-y-confirmar, dos contadores (gas del host e `instret`), horizonte de eventos por bloque, caché de decodificación y bloques compilados invalidados por generación de página con mapa de líneas de código de 16 bytes (H2.0), y una plataforma mínima (UART, SYSCON, CLINT) [L1][L2]. 2 372 líneas en siete módulos, 135 pruebas propias, especificaciones y revisiones en `.forja/h1` y `.forja/h2` [L2].
- Medidas: CoreMark rv32i 2,24/2,44/1,96 M instr/s interpretado en Python 3.13/3.12/3.10, bloques con horizonte 3,21×/3,18×/3,52×; Embench (14 benchmarks) 3,30–3,37× de media geométrica; Linux nommu hasta el shell cuesta 64,9 M de instrucciones en mini-rv32ima [L1].
- P5 de RFC-EXP-00018 propone un núcleo en C certificado por ventanas y lo condiciona a E1 (cumplido: viven P5 y la meta de H3) y E8 (pendiente); el usuario marcó el 2026-10-12 que la velocidad del hart se trata en un RFC de investigación propio, con la cifra «cómoda» para un shell interactivo y para Doom como objetivo que fija él [L1].
- La invariante S (`run(B1); snapshot; restore; run(B2) ≡ run(B1+B2)`), el hash incremental de estado (P2.3) y la puerta de inventario (P2.5) están propuestos en RFC-EXP-00018 P2 y son requisito de H2.1–H2.2 [L1]; este RFC los asume como la base sobre la que certificar.

### 2.2 Emuladores RISC-V en Rust y máquinas deterministas con gas (HECHO [R#])

| Proyecto | Qué es | Dato relevante | Fuente |
|---|---|---|---|
| ckb-vm (Nervos) | VM RISC-V en Rust, RV64IMC(B), con un intérprete en Rust y un intérprete en ensamblador («ASM mode») | Los ciclos consumidos son el gas de la cadena; el intérprete Rust se declara para desarrollo y el ASM es el de producción; macro-op fusion da «un salto de rendimiento muy bueno» | [R1][R2][R3] |
| PolkaVM (Parity) | VM RISC-V de usuario en Rust para contratos y JAM | Objetivos: misma entrada ⇒ misma salida; «gas metering barato, determinista y razonablemente preciso»; el medidor asíncrono cuesta ~5 % y el síncrono ~20 % en su benchmark; el README avisa de que es trabajo en curso | [R4][R5][R6] |
| RISC Zero zkVM | Emulador RV32IM en Rust que ejecuta invitados compilados al target `riscv32im-risc0-zkvm-elf` | El target de Rust «se implementa con un emulador software llamado zkVM»; el ejecutor divide la ejecución en segmentos y registra el estado de cada uno | [R7][R8] |
| Cartesi Machine | Emulador RV64IMAZicsr_Zifencei (C++) con todo el estado mapeado en el espacio de direcciones | «El contenido del espacio de direcciones define por completo la máquina»; raíz de Merkle del estado para disputas; snapshots | [R9][R10] |
| rvemu (d0iasm), riscv-rust (takahirox) | Emuladores RISC-V en Rust (RV64G; Linux y xv6) | Sin cifras de rendimiento publicadas en el barrido; `FENCE`, `ECALL` y atomicidad incompletos en rvemu | [R11][R12] |
| mini-rv32ima | Emulador RV32IMA+Zifencei+Zicsr en ~400 líneas de C que arranca Linux nommu | Es la referencia de E1 de RFC-EXP-00018; no se localizó un port a Rust en el barrido (cota) | [R13] |

Lectura: hay precedentes serios de RISC-V en Rust con ejecución determinista y gas exacto como requisito de consenso (ckb-vm en producción, PolkaVM, RISC Zero, Cartesi), y ninguno publica una certificación de un motor rápido por otro lento y normativo: la novedad de P5 se mantiene.

### 2.3 La frontera Python ↔ nativo (HECHO)

- `ctypes` está en la biblioteca estándar; `CDLL`/`WinDLL` liberan el GIL durante la llamada y lo readquieren después; `PyDLL` no lo libera y sirve solo para la API C de Python [R14].
- PyO3 ofrece el ABI estable (`abi3`, `abi3-pyXY`) para una sola rueda que sirva a varias versiones de CPython; la compilación libre de hilos de 3.13 no carga ruedas `abi3`, sí `abi3t`; maturin es la herramienta de construcción recomendada; `abi3` renuncia a optimizaciones que dependen de la versión exacta [R15][R16].
- Medido en la máquina del proyecto (HECHO [L4]): una llamada `ctypes` a una función Rust trivial cuesta 435 ns frente a 89 ns la misma suma en una función Python; una frontera por instrucción multiplicaría por cinco el coste del intérprete actual, así que la frontera tiene que ser por ventana de millones de instrucciones.

### 2.4 Herramientas de validación de Rust (HECHO)

- **Lints de `unsafe`**: `#![forbid(unsafe_code)]` o `deny` convierten todo bloque `unsafe` en error; `forbid` además impide relajarlo después [R17]. Desde Rust 1.82 los atributos `no_mangle`, `export_name` y `link_section` se escriben `#[unsafe(...)]` porque pueden causar comportamiento indefinido sin bloque `unsafe` [R18]; medido [L4]: con `forbid(unsafe_code)` un `#[no_mangle]` desnudo es un error de compilación.
- **Pánicos en la frontera**: desde Rust 1.81 un pánico que escapa de una función `extern "C"` aborta el proceso en vez de desenrollar hacia C (antes era comportamiento indefinido); quien necesite desenrollar usa `extern "C-unwind"` [R19][R20].
- **Miri**: intérprete que detecta comportamiento indefinido (accesos fuera de rango, uso tras liberar, datos sin inicializar, alineación, invariantes de tipo, y de forma experimental reglas de alias y carreras); «si Miri encuentra UB, el código es incorrecto; si no lo encuentra, quizá haya que probar más entradas» [R21][R22].
- **Kani**: comprobador de modelos acotado de AWS para Rust (MIR → CBMC) con arneses que parecen tests pero cuantifican sobre todas las entradas dentro de la cota; contratos de función y de bucle para ir más allá de la cota; usado en la campaña de verificación de la biblioteca estándar [R23].
- **Fuzzing**: cargo-fuzz (libFuzzer, nightly) con `arbitrary` para entradas estructuradas y sanitizadores [R24].
- **Cadena de suministro**: cargo-audit (avisos RustSec publicados), cargo-deny (licencias y avisos), cargo-vet (auditorías humanas registradas; Mozilla rechaza en CI lo que `cargo vet` no acepta) [R25].
- **Reproducibilidad**: `SOURCE_DATE_EPOCH` para las marcas de tiempo, `--remap-path-prefix` para que la ruta de construcción no entre en el binario (fue la única diferencia entre dos `librustc_driver.so`), `--locked`/`--offline`/`--frozen` y `cargo vendor` para fijar dependencias, `rust-toolchain.toml` para fijar la versión del compilador [R26][R27][R28][R29].
- **Enteros**: en el perfil `release` el desbordamiento envuelve en complemento a dos; `-C overflow-checks` (RFC 1535) activa la comprobación sin arrastrar `debug-assertions` [R30].
- **Windows**: el target `x86_64-pc-windows-msvc` necesita el enlazador de MSVC (Visual Studio Build Tools) o un `lld-link` que acepte sus opciones [R31].

### 2.5 El toolchain de la máquina del proyecto (HECHO [L4], 2026-10-17)

`rustc 1.90.0` y `cargo 1.90.0` (canal `stable-x86_64-pc-windows-msvc`), Visual Studio 2022 Build Tools con SDK de Windows 10.0.26100, LLD 21.1.0 de LLVM, caché local de crates de 717 MB. El `link.exe` de Git (`E:\Git\usr\bin`) precede al de MSVC en el `PATH`; fijando `linker = "C:/Program Files/LLVM/bin/lld-link.exe"` en `.cargo/config.toml` un `cdylib` con `panic = "abort"`, `lto`, `codegen-units = 1` y `overflow-checks = true` compila y enlaza en 1,7 s (9 728 bytes) y `ctypes.CDLL` lo carga y llama. No hay que instalar nada para los experimentos EN1–EN3 y EN5; Miri y cargo-fuzz exigen el canal nightly, que no está instalado (EN4 pide permiso antes).

### 2.6 Frontera del barrido

No se localizó: un port de mini-rv32ima a Rust; cifras de MIPS publicadas de rvemu ni de riscv-rust; ninguna comparación publicada de coste por llamada `ctypes` frente a PyO3 (se midió solo `ctypes` [L4]); ningún esquema publicado en el que un motor rápido sea certificado por reejecución de ventanas en un motor lento normativo (coincide con la cota de RFC-EXP-00018 §2.6).

## 3. Propuestas

### N1. Qué es el núcleo Rust y qué no es (PROPUESTA)

1. Un **ejecutor rápido** de la semántica del hart Python de RFC-EXP-00018 P1 (RV32I + Zicntr en su primera versión; M, A, Zicsr y traps cuando H3 los tenga en Python), con la misma tarifa de gas, el mismo horizonte, la misma plataforma mínima (UART, SYSCON, CLINT) y las mismas libertades fijadas (P1.8). No añade nada que el hart Python no haga; una instrucción que el Python trata como ilegal, el núcleo también.
2. **Opcional y silencioso en su ausencia**: `cpu_digital/rv32_native.py` busca el binario, comprueba su hash y su autoprueba (N4 V7) y, si algo falla, el proyecto sigue en Python puro con el motivo registrado. La rueda de PyPI sigue siendo Python puro y no lleva el binario.
3. **El hart Python es la especificación ejecutable y el único juez.** El núcleo Rust nunca certifica nada; se le certifica (N3). Las pruebas de conformidad (riscv-tests, ACT4, invariante S) se ejecutan contra el Python; el núcleo las pasa por equivalencia.
4. **Determinismo por construcción**: sin coma flotante del host, sin hilos, sin reloj, sin asignación dinámica durante la ejecución (la RAM la aporta Python), enteros siempre con `wrapping_*`/`checked_*` explícitos, sin `as` que trunque sin anotación revisada.

### N2. La frontera: ABI C mínima por ventana, memoria compartida, sin dependencias (PROPUESTA)

1. **Un `cdylib` con ABI C** (`#[repr(C)]`, `extern "C"`), cargado con `ctypes.CDLL` (libera el GIL durante la llamada [R14]; la disciplina es que Python no toca la RAM compartida mientras el núcleo corre). Sin PyO3 ni maturin en la primera versión: cero dependencias nuevas de Python y la rueda intacta; se reconsidera si la frontera domina el coste (§4).
2. **Memoria compartida sin copia**: Python pasa en cada llamada el puntero y el tamaño del `mmap` de `Rv32Memory` (`ctypes.addressof` sobre el búfer) y el núcleo lo usa solo durante esa llamada; nunca guarda el puntero. La RAM, las páginas escritas y el snapshot siguen siendo de Python.
3. **Funciones** (≤ 10): `rv32n_version() -> u32`, `rv32n_abi_hash() -> u64` (hash de la definición del ABI, comprobado al cargar), `rv32n_run(estado*, ram*, ram_len, dispositivos*, gas) -> parada`, `rv32n_step(...)` (una instrucción; **solo para la validación V1**, no para producción), `rv32n_dirty(...)` (mapa de bits de páginas escritas en la ventana), `rv32n_selftest() -> u32`. El `estado` es una `struct` C con registros, `pc`, `instret`, CSR y la reserva LR/SC (campo presente desde el principio, P2.5), espejo exacto del inventario del snapshot Python.
4. **Nada escapa como excepción**: `panic = "abort"` y, además, todo error previsible vuelve como código de parada; el núcleo no puede entrar en pánico por una entrada del invitado (lo prueban V3 y V4). Los pánicos en `extern "C"` abortan desde 1.81 [R19]: eso es una red, no el mecanismo de error.
5. **Una llamada por ventana** (W ≥ 2²⁰ instrucciones; EN2 elige W): con 435 ns por llamada [L4], la frontera cuesta < 0,1 % a 10 MIPS.

### N3. Certificación por ventanas (PROPUESTA; refina RFC-EXP-00018 P5)

1. El núcleo ejecuta la ventana i desde el estado S_i y entrega un **fotograma** F_i = (S_{i+1}, páginas escritas en la ventana con su imagen posterior, entradas no deterministas consumidas con su `instret`, bytes emitidos por el UART, motivo de parada, hash P2.3 de S_{i+1}).
2. El hart Python **reejecuta cada ventana** desde S_i con el mismo gas y compara su resultado con F_i bit a bit (estado, páginas, salidas, parada, hash). Las ventanas son independientes y se certifican en P procesos. Si todas coinciden, por inducción la ejecución nativa entera es la de la especificación Python.
3. Dos modos: **completo** (toda ventana certificada; obligatorio en CI para CoreMark, Embench, riscv-tests y la invariante S) y **muestreado** (una de cada k ventanas; solo como prueba de humo declarada, nunca como garantía: SMARTS estima medias, no universales, RFC-EXP-00018 [R40]).
4. La **puerta de inventario** (P2.5) cierra el agujero de la inducción: cada campo del estado nativo figura en la `struct` del ABI y en el snapshot Python, o en la lista blanca de cachés derivadas con su regla de invalidación. Un campo nuevo sin clasificar pone la CI en rojo.
5. **Modo diagnóstico**: con `rv32n_step` y el hash P2.3 por instrucción, la primera divergencia se localiza sin bisección.

### N4. Escalera de validación reforzada (PROPUESTA; obligatoria para integrar)

| Peldaño | Qué exige | Evidencia que se presenta | Quién la verifica |
|---|---|---|---|
| V0 Tipos y lints | `#![deny(unsafe_code)]` en toda la crate salvo `ffi.rs` (≤ 60 líneas, cada `unsafe` con su comentario de invariante); `#![deny(warnings)]` y clippy pedante en CI; `overflow-checks = true` también en `release`; `panic = "abort"`; sin `as` que trunque sin `#[allow]` justificado; sin coma flotante; cero crates externas | `cargo clippy` y `cargo build` limpios con la configuración versionada | CI y revisor Rust |
| V1 Equivalencia instrucción a instrucción | `rv32n_step` frente al hart Python sobre ≥ 10⁶ programas aleatorios (los generadores de las pruebas de H1 más uno propio del revisor), riscv-tests (H2.3), CoreMark y Embench completos comparando el hash P2.3 **en cada instrucción** | 0 divergencias; recuento de instrucciones y clases cubiertas | Oráculo matemático independiente |
| V2 Invariante S con el núcleo | Cortes de gas arbitrarios y sesgados en la frontera de ventana; `run(B1); fotograma; restore en Python; run(B2)` ≡ ejecución seguida, con y sin certificación | ≥ 1 000 cortes por programa, 0 divergencias (E4 extendido) | Arnés de H2.2 |
| V3 Fuzzing | (a) Interno: cargo-fuzz con `arbitrary` sobre decodificador y ejecutor, sanitizadores, 24 h sin pánico ni UB; (b) diferencial: programas hostiles generados desde Python (fuera de RAM, desalineados, código automodificable, MMIO) contra el hart Python | Corpus versionado; 0 fallos; cobertura de ramas del núcleo ≥ 95 % | Revisor ciego 2 |
| V4 Miri y Kani | Miri sobre toda la suite Rust (0 UB, incluidas las reglas de alias experimentales); arneses Kani que **demuestran** las guardas de memoria (ningún acceso fuera de la RAM para cualquier dirección y ancho), la extensión de signo, el desbordamiento de `pc` y la decodificación `decode(encode(x)) = x` sobre los campos | Salidas literales; lista de arneses | Revisor Rust |
| V5 Mutación medida sobre el certificador | ≥ 50 mutantes del núcleo (signo, guardas ±1, gas, orden de escritura, `rd = x0`, envolvente del `pc`) ejecutados **solo con la certificación por ventanas** (no con las pruebas unitarias): ≥ 95 % muertos; los vivos se clasifican (equivalentes o huecos del fotograma) | Tabla de mutantes | Revisor ciego 3 |
| V6 Cadena y reproducibilidad | Cero dependencias externas (`cargo vet` trivial); `rust-toolchain.toml` fijado (1.90.0); `Cargo.lock` versionado; `--locked --offline`; dos construcciones en rutas distintas con `--remap-path-prefix` y `SOURCE_DATE_EPOCH` dan el mismo sha256 en Windows y Linux; el hash por (toolchain, target) queda versionado | Hashes y comandos literales | CI |
| V7 Puerta de carga | `rv32_native.py` solo carga el binario si su sha256 es el versionado para su toolchain y si `rv32n_abi_hash` y `rv32n_selftest` (CoreMark it10 nativo certificado por Python en < 10 s) pasan; si no, Python puro con el motivo en el registro, nunca en silencio | Prueba que simula cada fallo | Suite Python |

Además: **tres revisiones ciegas sucesivas** (la tercera, de un revisor que solo mira `unsafe`, ABI, vidas de punteros, alias con los búferes de `ctypes`, pánicos y conversiones de enteros) y un oráculo matemático, todas bajo aislamiento; se integra solo cuando la última no encuentra nada nuevo. Los peldaños V3(a) y V4 necesitan el canal nightly (Miri, cargo-fuzz): se piden con permiso y corren en una tarea de CI aparte y programada, pero su verde es requisito para declarar el núcleo «certificado».

### N5. Integración en el repositorio (PROPUESTA)

- `native/rv32n/`: `Cargo.toml` (sin dependencias), `rust-toolchain.toml`, `.cargo/config.toml` (enlazador `lld-link` en Windows), `src/{lib.rs, core.rs, decode.rs, mem.rs, devices.rs, ffi.rs}`, `fuzz/`, `kani/`, `tests/`.
- `cpu_digital/rv32_native.py`: carga condicionada (N4 V7), `struct` del ABI con `ctypes.Structure`, `Machine.run(..., native=True)` que delega por ventanas y certifica según el modo.
- `benchmarks/benchmark_rv32_native.py`: EN1–EN3 y EN6 con los veredictos.
- CI: los trabajos actuales no cambian (Python puro); se añade `native` (Ubuntu y Windows: `cargo build --release --locked --offline`, `cargo test`, clippy, reproducibilidad, certificación completa de CoreMark y Embench) y `native-nightly` programado (Miri, cargo-fuzz).
- Nada se tira: el compilador de bloques Python sigue siendo la vía rápida del modo Python puro.

### N6. Coste y riesgos (CONJETURA salvo donde se indica)

- Coste: 1 500–2 500 líneas de Rust (núcleo RV32I ~800, dispositivos ~200, FFI ~60, pruebas, fuzz y arneses Kani ~800) y ~600 de Python (cargador, fotogramas, certificador, benchmark); 4–6 semanas-persona, de las que la mitad son la escalera N4.
- Rendimiento esperado: 50–200 MIPS en intérprete Rust optimizado (CONJETURA a partir de los intérpretes C del mapa de RFC-EXP-00018 [R17], [R22] y [R23], y de que el rastreo de páginas escritas por store cuesta); EN1 lo mide.
- Riesgos: **R-A** dos implementaciones que divergen (mitigado por N3: la divergencia se detecta por ventana y se localiza por instrucción); **R-B** seguridad de la frontera (punteros a `mmap` que Python puede cerrar: el núcleo nunca retiene el puntero y Python no cierra la memoria mientras hay una llamada en curso, probado en V3); **R-C** toolchain de Windows frágil (`link.exe` de Git; mitigado con `.cargo/config.toml` y el hash por toolchain) [L4]; **R-D** herramientas solo nightly (Miri, cargo-fuzz) en una máquina donde no se instala nada sin permiso; **R-E** el GIL se libera durante la llamada [R14]: la disciplina de hilo único se hace cumplir en `rv32_native.py`; **R-F** que la ganancia real no compense la segunda implementación: EN1 lo decide con cifra.

## 4. Alternativas consideradas

- **Núcleo en C (P5 tal como está escrito en RFC-EXP-00018).** Mismo diseño de certificación y coste parecido, pero sin lints que prohíban lo inseguro, sin Miri ni Kani y con comportamiento indefinido por desbordamiento con signo; descartado a favor de Rust por la escalera N4. Sigue siendo la alternativa si el toolchain Rust dejara de estar disponible.
- **Módulo de extensión con PyO3/maturin.** Integración más estrecha y menos marshalling, pero añade una dependencia de construcción a la rueda y ata a versiones de CPython (`abi3` lo mitiga, `abi3t` para la libre de hilos [R15]); se descarta en la primera versión porque la frontera por ventana hace irrelevante el coste por llamada [L4] y porque la rueda debe seguir siendo Python puro. Se reabre si EN2 muestra que el fotograma domina.
- **Compilar el hart Python con mypyc o Cython.** Una sola implementación, pero rendimiento típico de pocas veces (CONJETURA; no medido), toolchain de C y ninguna herramienta formal; no alcanza el orden de magnitud buscado.
- **PyPy.** No está en la máquina del proyecto (HECHO, RFC-EXP-00018 [L7]) y es una restricción del usuario.
- **Núcleo en WebAssembly ejecutado por un runtime.** Añade una dependencia de ejecución; descartado.
- **Hacer normativo el núcleo Rust y retirar el hart Python.** Descartado: destruye la identidad del proyecto («Python puro como especificación») y el argumento de certificación, que necesita un juez independiente del ejecutor.

## 5. Riesgos, costes y experimentos asesinos

Cada experimento nombra la cifra que mata la propuesta; si se alcanza, se para, se documenta con las medidas literales y se presenta. Todos corren bajo aislamiento (Job Object) y con la carga de la máquina comprobada antes y después.

- **EN1 — Prototipo mínimo (1 semana, ~700 líneas de Rust).** Intérprete RV32I + Zicntr con la plataforma mínima, cargado por `ctypes` sobre el `mmap` de `Rv32Memory`; CoreMark rv32i debe dar `instret` = 8 505 937 y los cuatro CRC del hart Python [L1]; se miden MIPS en esta máquina y el coste de la frontera. **Mata** la propuesta si el núcleo no supera en 5× a los bloques Python (es decir, < 36 M instr/s) o si la frontera cuesta más del 20 %.
- **EN2 — Fotogramas y certificación (sobre EN1).** Ventanas de 2²⁰ a 2²⁴ instrucciones; volumen del fotograma (páginas escritas) y tiempo de reejecución en Python con 1 y 4 procesos sobre CoreMark y los 14 de Embench. **Mata** N3 si la certificación completa de CoreMark más Embench no cabe en 20 minutos de CI con 4 procesos o si los fotogramas degeneran en snapshots completos (> 10 % de la RAM por ventana), que es el criterio E8 de RFC-EXP-00018 trasladado.
- **EN3 — Invariante S con el núcleo (sobre H2.2).** 1 000 cortes por programa en la frontera de ventana y dentro de ella (parada por gas y continuación en Python). **Mata** N1–N3 si aparece una divergencia que solo se cure con checkpoint por instrucción en el núcleo o con un coste que dependa de la partición.
- **EN4 — Escalera de validación (1–2 semanas; nightly con permiso).** Miri limpio, arneses Kani demostrados, 24 h de fuzz sin fallo, ≥ 50 mutantes con ≥ 95 % muertos **por la certificación**. **Mata** N3 (obliga a rediseñar el fotograma) si el certificador deja vivos más del 5 % de los mutantes no equivalentes.
- **EN5 — Reproducibilidad y cadena.** Dos construcciones en rutas distintas en Windows y en Linux con el mismo toolchain fijado dan el mismo sha256; `cargo vet` sin nada que auditar. **Mata** la carga condicionada al hash (N4 V7) si los binarios no son reproducibles en los corredores de CI; la alternativa sería firmar artefactos, que no se propone aquí.
- **EN6 — Arranque de Linux nommu (después de H3).** El objetivo real: tiempo de arranque hasta el shell en nativo frente a Python, certificado por completo (65 M de instrucciones son 16–62 ventanas de 2²²). La cifra «cómoda» para el shell y para Doom la fija el usuario; el experimento la mide, no la inventa.

**Riesgos transversales.** R-G: la escalera N4 puede costar más que el núcleo; se acepta porque es el precio de una segunda implementación. R-H: el canal nightly puede romper Miri o cargo-fuzz entre versiones; se fija también la fecha del nightly. R-I: una ventaja de 50× haría tentador relajar la certificación a muestreo en producción; este RFC lo prohíbe fuera de la prueba de humo. R-J: el núcleo RV32I de la primera versión queda atrás cuando H3 añada M, A, Zicsr y traps al Python; la regla es que el núcleo nunca va por delante del Python y que la certificación falla en rojo si el Python ejecuta algo que el núcleo no (parada «no implementado» en nativo ⇒ vuelta a Python para esa ventana).

**Preguntas abiertas.** ¿Qué W minimiza coste total (EN2)? ¿Conviene que el núcleo reporte también el hash P2.3 por instrucción en modo diagnóstico, o basta por ventana? ¿Se versiona algún binario para los usuarios sin toolchain, o solo se construye en CI? ¿Qué cifra es «cómoda» para el shell y para Doom?

## 6. Referencias

**Fuentes externas [R#]** (abiertas el 2026-10-17; las citas entre comillas son literales o traducción directa):

- [R1] docs.rs, `ckb-vm` 0.24.x: «CKB VM has 2 different modes: Rust interpreter mode and ASM mode». https://docs.rs/crate/ckb-vm/latest
- [R2] All About Circuits, «CKB-VM as a RISC-V Instruction Set: Inspiration, Design and Benefits»: intérprete optimizado en ensamblador desplegado en la capa 1 de Nervos CKB; macro-op fusion. https://www.allaboutcircuits.com/industry-articles/ckb-vm-as-a-risc-v-instruction-set-inspiration-design-and-benefits/
- [R3] Nervos docs, «CKB-VM»: ciclos como medida de coste de la ejecución. https://docs.nervos.org/docs/ckb-fundamentals/ckb-vm
- [R4] koute/polkavm (GitHub): «a general purpose user-level RISC-V based virtual machine»; objetivos de diseño (determinismo, gas metering) y aviso de trabajo en curso. https://github.com/koute/polkavm
- [R5] Polkadot Forum, «Announcing PolkaVM»: gas metering asíncrono ~5 % y síncrono ~20 % en su benchmark. https://forum.polkadot.network/t/announcing-polkavm-a-new-risc-v-based-vm-for-smart-contracts-and-possibly-more/3811
- [R6] JAM docs, «PVM»: coste fijo por instrucción y límite de peso. https://jam-docs.onrender.com/basics/pvm
- [R7] rustc platform support, `riscv32im-risc0-zkvm-elf`: «the target is implemented as a software emulator called the zkVM». https://doc.rust-lang.org/rustc/platform-support/riscv32im-risc0-zkvm-elf.html
- [R8] risc0/risc0 (GitHub): zkVM RV32IM, ejecutor por segmentos. https://github.com/risc0/risc0
- [R9] Cartesi docs, «Cartesi Machine»: «The contents of the address space therefore completely define the Cartesi Machine»; RV64IMAZicsr_Zifencei. https://docs.cartesi.io/cartesi-machine/
- [R10] Cartesi docs, arquitectura: emulador fuera de cadena con raíz de Merkle del estado. https://docs.cartesi.io/cartesi-machine/target/architecture/
- [R11] docs.rs, `rvemu`: RV64G; «FENCE, ECALL, and EBREAK don't do anything for now». https://docs.rs/crate/rvemu/0.0.5
- [R12] xrs-os/riscv-rust (GitHub, espejo de takahirox): Linux y xv6; crate `riscv_emu_rust`. https://github.com/xrs-os/riscv-rust
- [R13] cnlohr/mini-rv32ima (GitHub): «single-file header … implementing rv32ima/Zifencei+Zicsr»; arranca Linux nommu. https://github.com/cnlohr/mini-rv32ima/
- [R14] Referencia de ctypes (CDLL frente a PyDLL): «the Python GIL is released before calling any function exported by these libraries, and reaquired afterwards»; PyDLL no lo libera. https://svn.python.org/projects/ctypes/trunk/ctypes/docs/manual/reference.html (comportamiento vigente en https://docs.python.org/3/library/ctypes.html)
- [R15] PyO3, guía «Building and Distribution» (0.29): `abi3`/`abi3-pyXY`; la compilación libre de hilos de CPython no carga ruedas `abi3`, sí `abi3t`. https://docs.rs/crate/pyo3/0.29.2/source/guide/src/building-and-distribution.md
- [R16] PyO3, guía «Building and Distribution» (0.15): maturin hace elecciones de opinión y necesita poca configuración. https://pyo3.rs/v0.15.0/building_and_distribution
- [R17] Rust Reference, «Diagnostic attributes» y `std::forbid`: `forbid` = `deny` y además impide relajar el lint; `unsafe_code` es `allow` por defecto. https://doc.rust-lang.org/reference/attributes/diagnostics.html y https://doc.rust-lang.org/std/attribute.forbid.html
- [R18] Rust Edition Guide, «Unsafe attributes»: `no_mangle`, `export_name`, `link_section` se marcan `unsafe(...)` desde 1.82; obligatorio en la edición 2024. https://doc.rust-lang.org/edition-guide/rust-2024/unsafe-attributes.html
- [R19] Rust 1.81.0, notas de la versión (RELEASES.md): abortar en pánicos no capturados en funciones `extern "C"`. https://git.dreamy.place/mirrors/rust/plain/RELEASES.md?h=1.81.0 (anuncio: https://blog.rust-lang.org/2024/09/05/Rust-1.81.0.html)
- [R20] Rust Reference, «Panic»: desenrollar con el ABI equivocado es comportamiento indefinido; `extern "C-unwind"`. https://doc.rust-lang.org/reference/panic.html
- [R21] rust-lang/miri (README): «an Undefined Behavior detection tool for Rust»; lista de lo que detecta; «when Miri does not find UB, then you may just have to test more inputs». https://github.com/rust-lang/miri
- [R22] POPL 2026, «Miri: Practical Undefined Behavior Detection for Rust». https://popl26.sigplan.org/details/POPL-2026-popl-research-papers/50/Miri-Practical-Undefined-Behavior-Detection-for-Rust
- [R23] «Kani: A Model Checker for Rust» (arXiv 2607.01504; ASE 2026): MIR → CBMC, arneses, contratos, campaña de la biblioteca estándar. https://arxiv.org/abs/2607.01504
- [R24] Trail of Bits, «cargo-fuzz»: libFuzzer, nightly, `arbitrary`, sanitizadores. https://trailofbits.com/skills/cargo-fuzz/
- [R25] LWN, «Cargo-vet» (Mozilla): auditorías registradas; CI que rechaza lo no auditado. https://lwn.net/Articles/897437
- [R26] Reproducible Builds, «Rust»: `SOURCE_DATE_EPOCH`, metadatos, rutas. https://reproducible-builds.org/docs/rust/
- [R27] rust-lang/rust #112098: «librustc_driver.so not reproducible», la ruta de construcción era la única diferencia; arreglado con `--remap-path-prefix`. https://github.com/rust-lang/rust/issues/112098
- [R28] Cargo, `cargo vendor` y opciones `--locked`, `--offline`, `--frozen`. https://doc.rust-lang.org/cargo/commands/cargo-vendor.html
- [R29] rustup, «Overrides»: orden de precedencia y `rust-toolchain.toml`. https://rust-lang.github.io/rustup/overrides.html
- [R30] RFC 1535, «stable-overflow-checks»: `-C overflow-checks` sin `debug-assertions`. https://www.rustmax.net/library/rfcs/1535-stable-overflow-checks
- [R31] rust-lang/rust, INSTALL.md: las construcciones MSVC necesitan Visual Studio Build Tools (el enlazador de Microsoft) y un SDK de Windows reciente. https://rust.googlesource.com/rust/+/HEAD/INSTALL.md

**Lecturas y medidas del proyecto [L#]:**

- [L1] `RFC-EXP-00018.md` (P1, P2, P5, E1, E3, E8, «Hoja de ruta», [L8]–[L10]) y `VM32.md` (secciones H0, H1, H2): cifras de E1, E3, Embench y H2.0.
- [L2] `cpu_digital/rv32_{isa,decode,alu,mem,hart,platform,blocks}.py`, `tests/test_rv32_*.py`, `.forja/h1/`, `.forja/h2/`: el hart Python, sus especificaciones y revisiones.
- [L3] `pyproject.toml` (paquete Python puro, `dependencies = ["tramoya==1.5.3"]`) y `.github/workflows/ci.yml` (lint, pruebas en 3.10–3.13 en Linux, 3.13 en Windows informativo).
- [L4] Prueba de humo del 2026-10-17 en la máquina del proyecto (Anexo B): toolchain, construcción del `cdylib`, lint de `no_mangle`, coste de la llamada `ctypes`.

## Anexo A — Mapa breve: método y verificación

Barrido de 21 consultas web el 2026-10-17 (modo estándar) sobre cuatro sectores: emuladores RISC-V en Rust y VMs deterministas con gas; frontera Python↔nativo; herramientas de validación de Rust; reproducibilidad y cadena de suministro. Cada [R#] cita el texto que sostiene la afirmación. Verificación prevista antes de dar el RFC por cerrado: un revisor ciego abre cada URL y confirma o tacha la cita; un segundo revisor ataca el argumento de certificación (N3), la frontera (N2) y la escalera (N4) sin ver al primero. Sus informes se adjuntan en `.forja/rfc19/`.

Cotas del barrido (§2.6): port de mini-rv32ima a Rust, cifras de rvemu/riscv-rust, coste `ctypes` frente a PyO3, certificación de un motor rápido por uno normativo.

## Anexo B — Prueba de humo del toolchain (HECHO [L4], 2026-10-17)

```
rustup show                         → stable-x86_64-pc-windows-msvc (active, default); rustc 1.90.0; cargo 1.90.0
where link.exe lld-link.exe         → E:\Git\usr\bin\link.exe  (el de Git tapa al de MSVC)
                                      C:\Program Files\LLVM\bin\lld-link.exe (LLD 21.1.0)
Visual Studio 2022 BuildTools, SDK  → presentes (Windows Kits 10.0.19041.0 y 10.0.26100.0)

Cargo.toml: crate-type = ["cdylib"]; profile.release: opt-level 3, codegen-units 1, lto, panic "abort", overflow-checks true
Intento 1: #![forbid(unsafe_code)] + #[no_mangle]
  → error: declaration of a `no_mangle` function (atributo inseguro desde 1.82)
Intento 2: #![deny(unsafe_code)]; #[allow(unsafe_code)] #[unsafe(no_mangle)] pub extern "C" fn humo_suma_mod32(a: u32, b: u32) -> u32 { a.wrapping_add(b) }
           .cargo/config.toml: [target.x86_64-pc-windows-msvc] linker = "C:/Program Files/LLVM/bin/lld-link.exe"
  → cargo build --release --offline: Finished in 1.73s; humo.dll = 9 728 bytes
ctypes.CDLL(humo.dll).humo_suma_mod32(0xFFFFFFFF, 2) → 0x1
Coste de una llamada ctypes (5 rondas de 300 000): 434–436 ns; la misma suma en una función Python: 89 ns
```

Consecuencias de diseño: N2.5 (frontera por ventana), N4 V0 (`deny(unsafe_code)` con `unsafe` solo en la frontera y atributos `unsafe(no_mangle)`), N5 (`.cargo/config.toml` con `lld-link` versionado), R-C.
