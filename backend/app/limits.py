"""Topes de tamaño de lo que se escribe en el vault, con su 413.

Vive aparte porque lo usan dos routers: el de sugerencias del dueño
(`POST /pages/{id}/suggestions`) y la puerta de los agentes
(`POST /agent/suggestions`). Los topes en sí están en
`app/vault/suggestions.py`, al lado de los campos del `.json` que limitan;
aquí solo está la traducción a HTTP, para que el mensaje sea el mismo por las
dos puertas.
"""

from fastapi import HTTPException


def check_size(value: str, limit: int, what: str) -> None:
    """413 si `value` pasa del tope, contando bytes UTF-8 (no caracteres)."""
    if len((value or "").encode("utf-8")) > limit:
        # En KB cuando tiene sentido; los topes pequeños (200 B) se dicen en
        # bytes, que "0 KB" no informa de nada.
        tope = f"{limit // 1024} KB" if limit >= 1024 else f"{limit} B"
        raise HTTPException(413, f"{what} pasa del tope de {tope}")
