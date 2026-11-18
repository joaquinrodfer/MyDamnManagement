"""El mapa de títulos del enlazado se construye UNA vez por reindexado.

Antes, `sync_wikilinks` pedía los títulos de todo el workspace en cada llamada:
resolver los dos `[[wikilinks]]` de una nota traía las 12.000 filas de la tabla
`pages` y montaba un diccionario de 12.000 entradas. Repetido nota a nota, el
enlazado del reindexado era cuadrático (144 millones de filas leídas y ~230 s
de los ~270 s del arranque en frío, ver `docs/MEDICIONES-P16.md`). Ahora
`VaultStore.rebuild` construye el mapa una vez (`TitleIndex.load`) y lo pasa a
todas las notas.

Es un cambio de **coste**, no de resultado, y eso es lo que se fija aquí:

- el mapa se pide una vez por reindexado (no una por nota), y el guardado de
  una nota suelta —donde esa consulta es una sola— sigue funcionando;
- las filas de `links` son las mismas que producía el camino de antes, fila por
  fila: misma deduplicación de formas del mismo título, una fila por página
  destino cuando dos páginas comparten título, sin fila para la propia página y
  sin fila para un `[[Inexistente]]`.
"""

from __future__ import annotations

import sqlite3

from conftest import esperar_indice_listo, iniciar_sesion

SIN_ESPERA_DE_ARRANQUE = True


def _contar_cargas(monkeypatch) -> list[str]:
    """Cuenta cuántas veces se construye el mapa de títulos.

    Se parchea `TitleIndex.load`, que es el único sitio donde se construye, y
    no `fold_title`: el contador cuenta consultas, que es el coste que se
    quería quitar.
    """
    from app import links as links_mod

    cargas: list[str] = []
    original = links_mod.TitleIndex.load.__func__

    def cargar(cls, conn, workspace_id):
        cargas.append(str(workspace_id))
        return original(cls, conn, workspace_id)

    monkeypatch.setattr(links_mod.TitleIndex, "load", classmethod(cargar))
    return cargas


def _abrir_cliente(index_path, monkeypatch):
    """Un `TestClient` sobre el índice del test, sin esperar al arranque."""
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "index_path", str(index_path))
    return TestClient(app)


def test_el_reindexado_pide_el_mapa_de_titulos_una_vez(index_path, monkeypatch):
    """12.000 notas -> 12.000 consultas de 12.000 filas: eso es lo que se va."""
    cliente = _abrir_cliente(index_path, monkeypatch)
    cargas = _contar_cargas(monkeypatch)
    with cliente:
        esperar_indice_listo(cliente)
        iniciar_sesion(cliente)
        # El reindexado del arranque: una vez para el vault entero, aunque no
        # haya ni una nota todavía.
        assert len(cargas) == 1, cargas

        # El guardado de una nota suelta NO construye el mapa entero: desde la
        # 3.0 (hito 5) pide solo los títulos que nombra su cuerpo
        # (`TitleIndex.load_for`, una consulta por índice). Antes este test
        # contaba una carga completa por guardado (7 en total), que con miles
        # de notas era la mayor parte de lo que costaba guardar.
        from app import links as links_mod

        acotadas: list[str] = []
        original_for = links_mod.TitleIndex.load_for.__func__

        def cargar_para(cls, conn, workspace_id, titles):
            acotadas.append(str(workspace_id))
            return original_for(cls, conn, workspace_id, titles)

        monkeypatch.setattr(links_mod.TitleIndex, "load_for", classmethod(cargar_para))
        for i in range(6):
            creada = cliente.post(
                "/pages",
                json={
                    "title": f"Nota {i}",
                    "body_markdown": f"[[Nota {(i + 1) % 6}]]",
                },
            )
            assert creada.status_code == 200, creada.text
        assert len(cargas) == 1, "un guardado suelto no debe cargar el mapa entero"
        assert len(acotadas) == 6, "una consulta acotada por guardado"

        # Y el reindexado entero: otra vez una sola vez.
        cargas.clear()
        respuesta = cliente.post("/reindex?force=true")
        assert respuesta.status_code == 200, respuesta.text
        assert len(cargas) == 1, f"el reindexado pidió el mapa {len(cargas)} veces"
        # El mapa se usa de verdad: el anillo de seis notas son seis enlaces.
        assert respuesta.json()["links"] == 6, respuesta.json()


# --------------------------------------------------------------- mismos enlaces

# Dos ficheros con el MISMO título en carpetas distintas, la misma página
# enlazándose a sí misma, un título con tilde que no es el otro y un
# `[[Inexistente]]`.
A = "11111111-1111-5111-8111-111111111111"   # Notas/Reunión de equipo.md
B = "22222222-2222-5222-8222-222222222222"   # Notas/Otra/Reunión de equipo.md
P = "33333333-3333-5333-8333-333333333333"   # Notas/Peña.md
S = "44444444-4444-5444-8444-444444444444"   # Notas/Pena.md

PAGINAS = [
    ("Notas/Reunión de equipo.md", "Reunión de equipo", A,
     "[[REUNIÓN DE EQUIPO]]\n[[Peña]]\n[[Pena]]\n"),
    ("Notas/Otra/Reunión de equipo.md", "Reunión de equipo", B,
     "[[reunión de equipo]]\n[[Inexistente]]\n"),
    # Se enlaza a sí misma (sin fila) y repite la misma forma del mismo
    # destino: una fila, no dos.
    ("Notas/Peña.md", "Peña", P,
     "[[Peña]]\n[[Reunión de equipo]]\n[[Reunión de equipo]]\n"),
    ("Notas/Pena.md", "Pena", S, "sin enlaces\n"),
]


def _escribir_vault(vault_path) -> None:
    for ruta, _, page_id, cuerpo in PAGINAS:
        destino = vault_path / ruta
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(
            "---\n"
            f"id: {page_id}\n"
            "created: 2026-01-01T00:00:00+00:00\n"
            "---\n" + cuerpo,
            encoding="utf-8",
        )


def _enlaces(conn: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    """Filas de `links`: (id desde, id hasta, tipo, target_title), ordenadas."""
    return [
        (fila["from_id"], fila["to_id"], fila["kind"], fila["target_title"])
        for fila in conn.execute(
            "SELECT from_id, to_id, kind, target_title FROM links"
            " ORDER BY from_id, to_id, kind, target_title"
        )
    ]


def test_el_mapa_no_cambia_ninguna_fila_de_links(index_path, vault_path):
    """Mismo vault, dos caminos: las filas de `links` son idénticas.

    El camino de antes es `sync_wikilinks` sin mapa (construye el suyo, como en
    el guardado de una nota); el de ahora le pasa un `TitleIndex` construido
    una vez. Se comparan las dos salidas, y además contra lo que tiene que
    salir, que es lo que fija que no se ha colado comportamiento nuevo.
    """
    from app import index as indice
    from app.links import TitleIndex, sync_wikilinks

    _escribir_vault(vault_path)
    indice.ensure_index(str(index_path))
    conn = indice.open_index(str(index_path))
    try:
        workspace_id = indice.DEFAULT_WORKSPACE_ID
        conn.execute(
            "INSERT INTO workspaces (id, name, created_at) VALUES (?, ?, ?)",
            (workspace_id, "default", "2026-01-01T00:00:00+00:00"),
        )
        for ruta, titulo, page_id, cuerpo in PAGINAS:
            conn.execute(
                "INSERT INTO pages (id, workspace_id, type, title, body, path,"
                " created_at, updated_at) VALUES (?, ?, 'note', ?, ?, ?, ?, ?)",
                (
                    page_id,
                    workspace_id,
                    titulo,
                    cuerpo,
                    ruta,
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )

        # Camino de antes: cada nota pide el mapa por su cuenta.
        for _, _, page_id, cuerpo in PAGINAS:
            sync_wikilinks(conn, page_id, workspace_id, cuerpo)
        antes = _enlaces(conn)

        # Camino de ahora: el mapa, una vez, pasado a todas.
        conn.execute("DELETE FROM links")
        mapa = TitleIndex.load(conn, workspace_id)
        for _, _, page_id, cuerpo in PAGINAS:
            sync_wikilinks(conn, page_id, workspace_id, cuerpo, title_index=mapa)
        ahora = _enlaces(conn)
    finally:
        conn.close()

    esperado = sorted(
        [
            # A enlaza a la OTRA «Reunión de equipo» (la suya se descarta), a
            # Peña y a Pena. Se guarda la forma que escribió el cuerpo.
            (A, B, "wikilink", "REUNIÓN DE EQUIPO"),
            (A, P, "wikilink", "Peña"),
            (A, S, "wikilink", "Pena"),
            # B enlaza a A; `[[Inexistente]]` no resuelve, así que no hay fila.
            (B, A, "wikilink", "reunión de equipo"),
            # Peña: las DOS páginas que se llaman «Reunión de equipo» (una fila
            # por página destino, no por forma), y a sí misma no.
            (P, A, "wikilink", "Reunión de equipo"),
            (P, B, "wikilink", "Reunión de equipo"),
            # Pena no enlaza a nada.
        ]
    )
    assert ahora == antes, "el mapa compartido cambia las filas de `links`"
    assert ahora == esperado, ahora
