# Plan independiente: ampliar la RAM disponible de VM32

**Estado:** propuesta; todavía no implementada.

## Objetivo

Permitir que una instancia de Tramoya VM32 use **100 MiB lógicos adicionales** sobre el valor predeterminado actual de 4 MiB, para un total configurable de **104 MiB**. La VM debe seguir asignando memoria de forma paginada y mostrar claramente la diferencia entre espacio direccionable y memoria materializada en el proceso anfitrión.

Se usa MiB (1 MiB = 1 048 576 bytes) porque el proyecto configura memoria en palabras de 32 bits. Si “100 MB” quería decir 100 MiB en total y no 100 MiB adicionales, el objetivo alternativo sería 25 165 824 palabras. Confirmar esa interpretación antes de implementar.

## Medidas actuales y objetivo propuesto

| Concepto | Palabras de 32 bits | Tamaño lógico |
|---|---:|---:|
| Predeterminado actual | 1 048 576 | 4 MiB |
| Incremento propuesto | 26 214 400 | 100 MiB |
| Total propuesto | 27 262 976 | 104 MiB |
| Máximo actual | 16 777 216 | 64 MiB |

La memoria de VM32 usa páginas de 4 096 palabras (16 KiB). Las páginas se crean al escribir valores no nulos; ampliar el máximo lógico por sí solo no reserva 100 MiB en el equipo.

## Trabajo por etapas

1. **Fijar el contrato de memoria.** Mantener el valor predeterminado de 4 MiB y permitir un máximo configurable de 104 MiB. Definir si habrá también un límite de bytes físicos materializados por instancia. Si se añade, debe comprobarse antes de materializar cada página para que el límite no dependa de fallos de asignación del sistema operativo.
2. **Ampliar y centralizar los límites.** Elevar `MAX_MEMORY_WORDS` en `cpu_digital/vm32.py` y evitar que queden topes contradictorios en el formulario de `cpu_digital/ui/index.html` y la validación de `cpu_digital/ui_server.py`. El CLI `tramoya_vm.py` ya pasa `--memory-words` a `VMConfig`; documentar el máximo nuevo.
3. **Hacer legible la configuración en la consola.** Ofrecer tamaño en MiB o mostrar el equivalente al lado de las palabras. En recursos, mostrar memoria lógica y bytes de páginas materializadas; etiquetar los bytes como estimación del payload de arrays, no como uso RSS total del proceso.
4. **Revisar snapshots antes de declarar compatibilidad completa.** El snapshot usa JSON comprimido, el límite de contenido descomprimido es 256 MiB y la restauración web admite archivos de hasta 32 MiB. Una memoria muy poblada puede generar mucho JSON antes de comprimirse. Asegurar que una VM de 104 MiB pueda guardar y restaurar snapshots sin permitir descompresión sin límite; preferir una representación compacta de páginas si el JSON excede los límites seguros.
5. **Actualizar documentación y ejemplos.** Explicar que el tamaño configurado es direccionable y la memoria física se materializa bajo demanda; añadir los valores de conversión y el uso para UI y CLI en `README.md` y `VM32.md`.

## Criterios de aceptación

- La configuración acepta 27 262 976 palabras y rechaza un valor superior al máximo publicado.
- `memory_size` y la UI informan el tamaño lógico correcto; leer páginas no escritas devuelve cero sin materializarlas.
- Escribir en direcciones que cubren el rango asigna solo las páginas tocadas y respeta el eventual límite físico por instancia.
- La UI y el CLI pueden crear una instancia con el tamaño objetivo; las entradas fuera del rango siguen fallando de forma controlada.
- Guardar y restaurar snapshots mantiene configuración y memoria, incluidos datos cercanos al final del rango, sin superar límites de memoria durante la descompresión.
- La documentación deja claro que reservar o materializar 100 MiB usa RAM del proceso anfitrión y no aumenta la RAM física instalada.

## Riesgos y límites

- Si se escribe en todo el rango de 104 MiB, los arrays de datos por sí solos ocuparían unos 104 MiB; Python, snapshots y la interfaz requieren memoria adicional.
- El contador actual `allocated_bytes` mide el contenido de arrays, no toda la sobrecarga del proceso.
- Elevar solo el máximo lógico no garantiza que el equipo tenga RAM disponible. Para reservar 100 MiB residentes de antemano se necesitaría una opción explícita de materialización y un control de disponibilidad; no debe hacerse automáticamente al iniciar la VM.
- Cambiar el máximo no debería cambiar el tamaño predeterminado ni invalidar snapshots existentes con configuraciones menores.

## Archivos principales a revisar al implementar

- `cpu_digital/vm32.py`
- `cpu_digital/memory.py`
- `cpu_digital/ui_server.py`
- `cpu_digital/ui/index.html`
- `cpu_digital/ui/app.js`
- `tramoya_vm.py`
- `README.md` y `VM32.md`
