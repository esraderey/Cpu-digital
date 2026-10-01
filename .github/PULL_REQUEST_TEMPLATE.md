## Qué cambia y por qué

## Cómo se verificó

- [ ] `python -m unittest discover -s tests` pasa.
- [ ] `python -m ruff check cpu_digital tests benchmarks tramoya_vm.py` pasa.
- [ ] Si corrige un error, hay un test que fallaba antes del arreglo.
- [ ] Si toca el acelerador de bucles, `tests/test_vm32_loops.py` pasa y el fuzzing
      diferencial cubre lo nuevo.
- [ ] Si cambia el comportamiento, se actualizó la documentación (README, VM32.md o RFC).
- [ ] No añade dependencias.
