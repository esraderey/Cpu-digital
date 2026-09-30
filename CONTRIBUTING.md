# Cómo contribuir

Gracias por querer mejorar CPU Digital. Este documento resume cómo preparar el
entorno, qué se comprueba antes de aceptar un cambio y qué convenciones sigue el
proyecto.

## Entorno

Hace falta Python 3.10 o posterior (el chip TNU exige 3.12 o posterior).

```powershell
git clone https://github.com/esraderey/Cpu-digital.git
cd Cpu-digital
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m pip install ruff==0.8.6
```

En Linux o macOS usa `.venv/bin/python` en lugar de `.\.venv\Scripts\python.exe`.

## Antes de abrir un pull request

Ejecuta lo mismo que la integración continua:

```powershell
.\.venv\Scripts\python.exe -m ruff check cpu_digital tests benchmarks tramoya_vm.py
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Ambos deben terminar sin errores. Si un test ya fallaba antes de tu cambio, dilo en
el pull request en lugar de desactivarlo.

## Reglas del proyecto

- **Sin dependencias nuevas.** El único requisito de ejecución es `tramoya`. Todo
  lo demás usa la biblioteca estándar.
- **Determinismo.** Mismo programa, mismas entradas y misma configuración deben
  dar el mismo estado, la misma salida, el mismo gas y el mismo snapshot.
- **Atomicidad por instrucción.** Una instrucción que falla no deja efectos:
  registros, banderas, memoria, pila, E/S y gas vuelven al estado anterior.
- **Un fallo, un test ancla.** Antes de corregir un error, añade un test que lo
  reproduzca y falle; después aplica el arreglo y comprueba que pasa.
- **No debilites el oráculo.** No se aceptan tests desactivados, `skip`, asserts
  relajados ni excepciones silenciadas para obtener verde.
- **Acelerador de bucles.** `cpu_digital/vm32_loops.py` debe dejar exactamente el
  mismo estado observable que el intérprete. Todo cambio debe pasar
  `tests/test_vm32_loops.py`, y si el acelerador admite una instrucción o una
  construcción nueva, amplía el fuzzing diferencial para cubrirla.
- **Seguridad.** Respeta el modelo de capacidades, el gas y la validación en
  tiempo de ejecución descritos en [VM32.md](VM32.md). Un snapshot o un bytecode
  son entradas no confiables.

## Estilo

- Código, comentarios, mensajes de error y documentación en español, siguiendo el
  estilo del archivo que modifiques.
- Comentarios breves que expliquen el porqué, no el qué.
- Si cambias el comportamiento de la VM, actualiza [VM32.md](VM32.md) o el
  [README](README.md). Los RFC siguen su regla de tinta: **HECHO** (con fuente o
  medición reproducible), **PROPUESTA** y **CONJETURA**, sin mezclarlas.
- Acompaña las cifras de rendimiento con el script que las reproduce
  (`benchmarks/`), la versión de Python y la plataforma.

## Issues y pull requests

- Usa las plantillas de issue para errores y propuestas.
- Un pull request debe resolver un solo problema. Describe qué cambia, por qué y
  cómo lo verificaste.
- Para vulnerabilidades, sigue [SECURITY.md](SECURITY.md) en lugar de abrir un
  issue público.

## Licencia

Al contribuir aceptas que tu aportación se distribuya bajo la
[licencia MIT](LICENSE) del proyecto.
