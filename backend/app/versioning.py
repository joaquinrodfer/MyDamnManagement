"""`If-Match` y el 409 de las escrituras con versión (3.0, hito 2; ADR-0009 D3).

Cada documento (nota, base o fila) lleva `version`, que sube en cada escritura.
Quien guarda manda la versión desde la que hizo su cambio en `If-Match`; si la
página ya va por otra, el servidor no escribe y contesta **409** con la versión
actual y el documento de ahora, para que el cliente decida. Sin `If-Match` todo
sigue como antes (el último que guarda gana): la cabecera es opcional para no
romper a ningún cliente que no la conozca.

Se acepta la forma de HTTP (`"7"`, `W/"7"`) y la desnuda (`7`). `*` es «me da
igual la versión» (RFC 9110): como no mandarla.
"""

from typing import Any, Optional

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse


def parse_if_match(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    raw = value.strip()
    if raw in ("", "*"):
        return None
    if raw.startswith("W/"):
        raw = raw[2:]
    raw = raw.strip().strip('"')
    try:
        version = int(raw)
    except ValueError:
        raise HTTPException(400, f"If-Match no es una versión: {value!r}") from None
    if version < 1:
        raise HTTPException(400, f"If-Match no es una versión: {value!r}")
    return version


def etag(version: int) -> str:
    return f'"{int(version)}"'


def conflict(current: Any, version: int, expected: int) -> JSONResponse:
    """El 409: la versión actual, la que mandó el cliente y el documento de ahora."""
    return JSONResponse(
        status_code=409,
        content={
            "detail": (
                f"el documento ha cambiado: va por la versión {version} y el cambio "
                f"se hizo sobre la {expected}"
            ),
            "version": version,
            "expected_version": expected,
            "current": jsonable_encoder(current),
        },
        headers={"ETag": etag(version)},
    )
