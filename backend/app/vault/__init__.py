"""El vault en disco: ficheros `.md` como verdad de las notas (P1.2a).

Los módulos `fm`, `names`, `vaultfs`, `reader` y `writer` vienen del spike P0
(`t_6fea05dc`, revisado de forma independiente) y resuelven, sin PyYAML:

- `fm`: frontmatter YAML del subconjunto que usa el vault, con round-trip de
  las claves que la app no gestiona.
- `names`: título -> nombre de fichero legible (NFC, caracteres prohibidos,
  colisiones ` (2)`, 180 bytes, nombres reservados de Windows).
- `vaultfs`: escritura atómica `tmp` + `rename`, papelera y validación de
  rutas (`PathEscape` si algo intenta salir del vault).
- `reader`: recorre el vault y lo convierte en páginas (tolerante a CRLF, BOM,
  Latin-1 y frontmatter sin cerrar).
- `writer`: crear/guardar/renombrar/borrar páginas sobre el vault.

Lo único nuevo de P1.2a es `store.py`, el puente entre esos ficheros y el índice
SQLite para las **notas**: escribir un `.md` y actualizar el índice en la misma
operación, reconstruir el índice desde el vault, y mover a la papelera.

Las bases de datos y sus filas siguen viviendo solo en el índice: son P2. Por
eso el reindexado no toca sus filas de `pages`/`databases`/`views`/`rows`, y
`store.py` rechaza anidar una nota dentro de una base con un error claro en vez
de inventarse una ruta que después no se podría volver a deducir del disco.
"""
