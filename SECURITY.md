# Política de seguridad

## Versiones con soporte

El proyecto no publica versiones estables todavía: las correcciones de seguridad
se aplican sobre la rama `main`.

## Cómo informar de una vulnerabilidad

No abras un issue público. Usa el aviso privado de GitHub: pestaña **Security** del
repositorio → **Report a vulnerability**. Incluye:

- la versión (commit) afectada y la plataforma;
- un programa `.tasm`, bytecode `.tvm`, snapshot o petición HTTP mínimos que
  reproduzcan el problema;
- qué garantía se rompe y qué impacto tiene.

El mantenedor confirmará la recepción, investigará el caso y coordinará contigo
la publicación del arreglo.

## Qué se considera una vulnerabilidad

Tramoya VM32 ejecuta código no confiable dentro de los límites descritos en
[VM32.md](VM32.md) («Modelo de seguridad» y «Límites honestos»). Es una
vulnerabilidad todo lo que permita a un programa invitado, un bytecode o un
snapshot manipulado:

- saltarse una capacidad (por ejemplo, usar una syscall no autorizada);
- ejecutar sin pagar gas, superar los límites de memoria, pila, salida o fibras,
  o modificar código protegido;
- romper la atomicidad por instrucción o el determinismo;
- hacer que el acelerador de bucles deje un estado distinto del intérprete;
- provocar un fallo del proceso anfitrión en lugar de un fallo controlado de la VM.

También cuentan los fallos de la consola web local (`cpu_digital/ui_server.py`)
que permitan controlarla desde otro origen o desde la red sin haberlo habilitado.

## Fuera de alcance

- VM32 no es una sandbox a nivel de proceso. Para código adversarial de alto
  riesgo, ejecuta el proceso Python dentro de un contenedor o una sandbox del
  sistema operativo.
- Las syscalls personalizadas son código Python confiable del host: la autoridad
  que expongan (disco, red, etc.) es responsabilidad de quien las registra.
- El CRC del bytecode detecta corrupción accidental, no manipulación: ni el
  bytecode ni los snapshots van firmados. Los dos se tratan como entrada no
  confiable.
