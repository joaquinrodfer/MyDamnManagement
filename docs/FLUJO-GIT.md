# Flujo de git (Git Flow)

Este documento dice cómo se ramifica y qué llega a `main`. Sustituye al apaño
anterior, en el que `release/v1.0` acabó siendo la rama de trabajo mientras las
versiones subían a 2.x.

## Las tres ramas que importan

```
main      producción. Solo entra por pull request desde release/*. Lo revisa el dueño.
develop   integración. Aquí se junta el trabajo del día a día; es de donde salen las ramas.
release/  estabilización de una versión concreta, camino de main.
```

`main` **no se toca a mano**: ni merges locales, ni commits directos, ni
force-push. Lo que está en `main` es lo que el dueño ha leído y aprobado.

## Ramas de trabajo

Salen de `develop` y mueren en `develop`, en cuanto la tarjeta de revisión está
aprobada y el CI verde:

| Prefijo | Para |
|---|---|
| `feature/…` | funcionalidad nueva |
| `fix/…` | corregir un defecto |
| `chore/…` | tooling, dependencias, CI |
| `docs/…` | documentación sola |

Una rama, una intención. Si al terminar toca dos cosas, eran dos tarjetas.

```bash
git switch develop && git pull --ff-only
git switch -c feature/lo-que-sea
# … commits pequeños …
pytest -q && (cd frontend/editor-src && npm test)
# revisión de la tarjeta aprobada + CI verde
git switch develop && git pull --ff-only
git merge --no-ff feature/lo-que-sea
git push origin develop
git branch -d feature/lo-que-sea
```

- **`--no-ff` siempre** al fusionar una rama de trabajo: queda en el historial y
  se revierte de una pieza (`git revert -m 1`).
- **`--ff-only` siempre** en los `pull`: si no avanza limpio, algo se ha
  desviado y hay que mirarlo, no taparlo con un merge automático.

## De `develop` a `main`: `release/*`

Cuando el lote está cerrado y probado:

```bash
git switch develop && git pull --ff-only
git switch -c release/2.2
# solo arreglos: nada de funcionalidad nueva dentro de una release
git push -u origin release/2.2
```

Y ahí, en este orden:

1. **revisión final** (la tarjeta de revisión, sobre la punta de la release);
2. **pull request `release/2.2` → `main`**, con el resumen de lo que entra;
3. **el dueño revisa y fusiona**. Nadie más fusiona en `main`;
4. etiqueta `v2.2.0` sobre el commit de `main` ya fusionado;
5. la release se fusiona **también en `develop`** (para no perder los arreglos
   hechos durante la estabilización) y se borra.

**El paso 5 no es opcional ni «para luego».** Si se olvida, `develop` y `main`
quedan con historias divergentes: el PR de la release siguiente sale
`mergeable: false` / `mergeable_state: dirty`, y el conflicto aparece en el
`CHANGELOG.md` porque las dos ramas renombraron la sección de arriba y
describen cosas distintas. Parece un conflicto tonto; es la señal de que falta
la re-fusión. Remedio:

```bash
git switch develop && git pull --ff-only
git merge --no-ff origin/main        # resuelve el CHANGELOG separando por versión
git push origin develop
git branch -f release/2.5 develop    # local, y luego:
git push --force origin release/2.5  # seguro: la release es tuya y no está en main
```

Si durante la estabilización aparece un defecto: se arregla **en la release**
(commit pequeño, con su prueba), no en `develop`, para que `main` reciba
exactamente lo revisado.

## Arreglos urgentes sobre producción

```bash
git switch -c hotfix/2.1.1 v2.1.0
# arreglo + prueba
# PR a main (revisión del dueño), etiqueta v2.1.1
git switch develop && git merge --no-ff hotfix/2.1.1
```

## Etiquetas

- `vX.Y.Z` **sobre `main`**, anotadas, con el porqué en el mensaje (fichero con
  `-F`, nunca `-m` con backticks: ya nos mordió una vez).
- **Se despliega una etiqueta, nunca una rama.** `git switch v2.1.0`.
- Mayor: cambia el contrato del vault o de la API de forma incompatible.
  Menor: funcionalidad visible. Parche: correcciones sin funcionalidad nueva.
- La etiqueta se crea **después** de fusionar y con el CI verde en esa punta.

## Qué impide fusionar

| Puerta | Quién |
|---|---|
| La tarjeta de revisión, aprobada | el revisor |
| `pytest -q` verde (396+ tests) | el CI, job `tests` |
| bundle del editor reproducible | el CI, job `bundle` |
| la imagen de Docker construye | el CI, job `imagen` |

El CI corre en cada push y cada pull request. Si está rojo, no se fusiona: se
arregla o se revierte.

## Limpieza

Las ramas fusionadas se borran (`git branch --merged develop`). Las etiquetas
**no** se borran: son la historia.

## Despliegue

Hoy: `git switch v2.1.0` y `docker compose -f docker-compose.prod.yml up -d --build`.
Cuando haya contenedor propio: `docker pull` de una imagen etiquetada y
`docker compose up -d` con esa etiqueta. La regla no cambia: se despliega una
etiqueta, nunca una rama.

## Ajustes del repositorio en GitHub

- **Rama por defecto: `develop`** (Settings → General → Default branch). Con `main` por defecto, un `git clone` deja al que llega en la rama de producción.
- **`main` protegida**: pull request obligatorio, sin force-push ni borrado. La fusión la hace el dueño.
- `develop` **no** lleva regla de pull request: ahí se fusiona directo desde la sesión del orquestador cuando la tarjeta está aprobada, y una regla obligaría a abrir un PR por cada arreglo.
