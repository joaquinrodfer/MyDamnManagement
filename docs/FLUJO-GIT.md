# Flujo de git

Este documento dice cómo se ramifica, cómo se fusiona y qué se puede desplegar.
Sustituye al apaño anterior, en el que `release/v1.0` acabó siendo la rama de
trabajo mientras las versiones subían a 2.x: una rama con nombre de versión que
contenía todo lo demás. Eso se terminó el 2026-11-10: `main` es el tronco.

## Las tres reglas

1. **`main` es el tronco y siempre se puede desplegar.** Lo que está en `main`
   ha pasado la revisión y el CI. No hay ramas largas paralelas.
2. **El trabajo vive en ramas cortas que salen de `main` y mueren al fusionarse.**
   Un nombre por intención, no por versión: `feat/…`, `fix/…`, `chore/…`,
   `docs/…`. Nada de `release/vX.Y`.
3. **Se despliega una etiqueta, nunca la punta de una rama.** `v2.1.0` es un
   punto del tiempo que no se mueve; `main` sí. Un despliegue que apunta a
   `main` cambia solo cuando alguien fusiona, y eso no es un despliegue, es una
   sorpresa.

## El día a día

```bash
git switch main && git pull --ff-only
git switch -c feat/lo-que-sea          # rama corta
# … trabajo, commits pequeños …
pytest -q && (cd frontend/editor-src && npm test)
# revisión aprobada + CI verde → fusión
git switch main && git pull --ff-only
git merge --no-ff feat/lo-que-sea
git push origin main
git branch -d feat/lo-que-sea
```

- **`--no-ff` siempre** en las fusiones de ramas de trabajo: la rama queda
  visible en el historial y se puede revertir de una pieza (`git revert -m 1`).
- **`--ff-only` siempre** en los `pull`: si no avanza limpio, algo se ha
  desviado y hay que mirarlo, no taparlo con un merge automático.
- **Una rama, una intención.** Si al terminar toca dos cosas, eran dos tarjetas.

## Qué impide fusionar

| Puerta | Quién |
|---|---|
| La tarjeta de revisión, aprobada | el revisor |
| `pytest -q` verde (396+ tests) | el CI, job `tests` |
| bundle del editor reproducible | el CI, job `bundle` |
| la imagen de Docker construye | el CI, job `imagen` |

El CI corre en **cada push y cada pull request** (`.github/workflows/tests.yml`).
Si el CI está rojo, no se fusiona: se arregla o se revierte, no se espera a que
se ponga verde solo.

## Etiquetas

- **`vX.Y.Z` sobre `main`**, anotada, con el porqué en el mensaje (`-F` con un
  fichero, nunca `-m` con backticks: ya nos mordió una vez).
- **mayor** (`X`): cambia el contrato del vault o de la API de forma incompatible.
- **menor** (`Y`): funcionalidad visible.
- **parche** (`Z`): correcciones sin funcionalidad nueva.
- La etiqueta se crea **después** de fusionar y con el CI verde en esa punta.
  Si el CI está corriendo, se espera: una etiqueta sobre un commit que luego se
  pone rojo no vale de nada a la hora de volver atrás.

## Ramas de mantenimiento

`release/x.y` solo tiene sentido si hay que arreglar una versión **ya
desplegada** sin subir de versión. Hoy no hay ninguna en uso: si aparece, sale
de la etiqueta, no de `main`, y se fusiona en los dos sitios (mantenimiento y
`main`). Mientras no exista esa necesidad, no se crea.

## Limpieza

Las ramas fusionadas se borran. Al 2026-11-10 hay 54 ramas ya fusionadas en
`main` (`git branch --merged main`) que solo son ruido al buscar algo.

```bash
git branch --merged main | grep -vE '^\*|main$' | xargs -r git branch -d
git push origin --delete <rama>        # una a una, o con --dry-run primero
```

Las etiquetas **no se borran**: son la historia.

## Despliegue (lo que viene)

Hoy: `git switch <etiqueta>` y `docker compose -f docker-compose.prod.yml up -d --build`.
Cuando haya contenedor propio en el Proxmox del dueño, el despliegue pasa a ser
`docker pull` de una imagen **etiquetada** y `docker compose up -d` con esa
etiqueta en el `.env`. La regla no cambia: se despliega una etiqueta.
