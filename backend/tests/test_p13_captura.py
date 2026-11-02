"""P13: captura rápida — `POST /capture`, la bandeja y los tokens personales.

Lo que se fija aquí, en el orden en que importa:

1. **Una captura crea una nota de verdad**, por el camino del vault: `Bandeja/`
   (configurable), frontmatter con `captured_at`/`capture_source`/
   `captured_by`, fila en el índice y evento para el panel.
2. **Título**: el que llega o uno por defecto con la hora; si ya existe en la
   bandeja no falla, el vault resuelve la colisión con ` (2)`.
3. **Dos puertas para capturar**: la sesión del navegador y un token personal.
   El token solo abre `POST /capture` (403 en el resto), se guarda como SHA-256
   y se revoca; revocado deja de valer (401).
4. **La bandeja se puede vaciar**: `GET /inbox` la lista y el `PATCH` de
   `parent_id` de siempre la mueve a su sitio (es lo que hace «Procesar»).
5. **Nada de esto rompe el índice**: un reindexado desde el vault vuelve a
   encontrar la captura con su frontmatter.

Los tests se autentican por HTTP (ver `iniciar_sesion` en `conftest.py`): nada
de saltarse el middleware, ni siquiera para hablar del middleware.
"""

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from app.config import settings
from conftest import NOMBRE_PRUEBA


def _crear_token(client, name="Atajo de prueba"):
    respuesta = client.post("/auth/tokens", json={"name": name})
    assert respuesta.status_code == 201, respuesta.text
    return respuesta.json()


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _frontmatter(path):
    from app.vault import fm

    texto = path.read_text(encoding="utf-8")
    raw, cuerpo = fm.split_document(texto)
    assert raw is not None, texto
    return fm.parse(raw).data, cuerpo


# --------------------------------------------------------------------------
# 1. capturar: la nota, su frontmatter y el índice
# --------------------------------------------------------------------------


def test_una_captura_crea_la_nota_en_la_bandeja_con_su_frontmatter(client, vault_path):
    respuesta = client.post(
        "/capture",
        json={
            "text": "Comprar pan\nY llamar al fontanero",
            "title": "Recados",
            "source": "cli",
        },
    )
    assert respuesta.status_code == 201, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["title"] == "Recados"
    assert cuerpo["path"] == "Bandeja/Recados.md"

    fichero = vault_path / cuerpo["path"]
    assert fichero.is_file()
    datos, texto = _frontmatter(fichero)
    assert datos["id"] == cuerpo["id"]
    assert datos["captured_by"] == NOMBRE_PRUEBA
    assert datos["capture_source"] == "cli"
    # `captured_at` es un instante ISO con zona, no la fecha del fichero: un
    # `rsync` mueve la segunda y no la primera.
    assert datetime.fromisoformat(datos["captured_at"]).tzinfo is not None
    assert "Comprar pan" in texto and "fontanero" in texto

    # Y está en el índice con esa identidad (no solo el fichero).
    leida = client.get(f"/pages/{cuerpo['id']}")
    assert leida.status_code == 200, leida.text
    assert leida.json()["title"] == "Recados"
    assert leida.json()["body_markdown"] == "Comprar pan\nY llamar al fontanero"


def test_la_bandeja_esta_en_el_indice_y_sobrevive_a_un_reindexado(client, vault_path):
    """El índice es derivado: si la captura no se reconstruye, no está guardada."""
    creada = client.post("/capture", json={"text": "algo", "title": "Suelta"}).json()

    reindexado = client.post("/reindex")
    assert reindexado.status_code == 200, reindexado.text
    # La bandeja es una carpeta conocida: no sale como aviso de «fuera de».
    avisos = " ".join(reindexado.json().get("warning_sample", []))
    assert "Bandeja" not in avisos
    assert reindexado.json()["warnings"] == 0, reindexado.json()

    leida = client.get(f"/pages/{creada['id']}")
    assert leida.status_code == 200, leida.text
    assert leida.json()["title"] == "Suelta"
    datos, _ = _frontmatter(vault_path / creada["path"])
    assert datos["capture_source"] == "api"


def test_el_titulo_por_defecto_lleva_la_fecha_y_la_hora(client):
    cuerpo = client.post("/capture", json={"text": "sin título"}).json()
    # La hora va con `-` y no con `:` porque el título ES el nombre del fichero
    # y `:` no vale en Windows (ADR-0001).
    assert re.match(r"^Captura \d{4}-\d{2}-\d{2} \d{2}-\d{2}$", cuerpo["title"]), cuerpo
    assert cuerpo["path"] == f"Bandeja/{cuerpo['title']}.md"


def test_dos_capturas_con_el_mismo_titulo_no_fallan_y_llevan_sufijo(client, vault_path):
    primera = client.post("/capture", json={"text": "una", "title": "Idea"})
    segunda = client.post("/capture", json={"text": "otra", "title": "Idea"})
    assert primera.status_code == 201 and segunda.status_code == 201
    assert primera.json()["title"] == "Idea"
    assert primera.json()["path"] == "Bandeja/Idea.md"
    assert segunda.json()["title"] == "Idea (2)"
    assert segunda.json()["path"] == "Bandeja/Idea (2).md"
    # Las dos existen y no son la misma página.
    assert primera.json()["id"] != segunda.json()["id"]
    assert (vault_path / "Bandeja" / "Idea.md").is_file()
    assert (vault_path / "Bandeja" / "Idea (2).md").is_file()


def test_una_captura_avisa_al_panel_por_el_bus_de_eventos(client, monkeypatch):
    from app.routers import capture as capture_router

    vistos = []
    monkeypatch.setattr(capture_router.bus, "publish", vistos.append)

    creada = client.post("/capture", json={"text": "avísame"}).json()
    assert vistos == [
        {
            "type": "page",
            "action": "added",
            "page_id": creada["id"],
            "path": creada["path"],
        }
    ]


def test_el_texto_es_obligatorio(client, vault_path):
    vacio = client.post("/capture", json={"text": "   \n "})
    assert vacio.status_code == 400
    assert "vacío" in vacio.json()["detail"]
    # Sin `text` es un cuerpo inválido (422), no una nota vacía.
    assert client.post("/capture", json={}).status_code == 422
    assert not (vault_path / "Bandeja").exists()


def test_el_texto_tiene_tope(client, monkeypatch):
    monkeypatch.setattr(settings, "capture_max_bytes", 32)
    respuesta = client.post("/capture", json={"text": "x" * 33})
    assert respuesta.status_code == 413
    assert "33" in respuesta.json()["detail"]
    # Justo en el tope sí entra.
    assert client.post("/capture", json={"text": "x" * 32}).status_code == 201


def test_la_fuente_es_una_de_las_conocidas(client):
    assert client.post("/capture", json={"text": "x", "source": "telepatía"}).status_code == 422
    for source in ("shortcut", "raycast", "cli", "api"):
        assert client.post("/capture", json={"text": "x", "source": source}).status_code == 201


def test_capturar_con_sesion_funciona(client, vault_path):
    """El navegador (o un `curl` con la cookie) no necesita token."""
    creada = client.post("/capture", json={"text": "desde el panel"})
    assert creada.status_code == 201, creada.text
    ruta = vault_path / creada.json()["path"]
    assert ruta.is_file()
    datos, _ = _frontmatter(ruta)
    assert datos["captured_by"] == NOMBRE_PRUEBA


def test_sin_sesion_ni_token_no_se_captura(client, vault_path):
    client.cookies.clear()
    assert client.post("/capture", json={"text": "x"}).status_code == 401
    assert not (vault_path / "Bandeja").exists()


# --------------------------------------------------------------------------
# 2. tokens personales de captura
# --------------------------------------------------------------------------


def test_el_token_se_crea_lista_sin_secreto_y_se_revoca(client):
    creado = _crear_token(client, name="Mac de casa")
    assert creado["name"] == "Mac de casa"
    assert creado["last_used_at"] is None
    assert len(creado["token"]) >= 40

    listado = client.get("/auth/tokens").json()
    assert len(listado) == 1
    assert listado[0]["id"] == creado["id"]
    # El secreto no vuelve a salir nunca: en la base solo está su SHA-256.
    assert "token" not in listado[0]
    assert creado["token"] not in str(listado)

    assert client.delete(f"/auth/tokens/{creado['id']}").status_code == 204
    assert client.get("/auth/tokens").json() == []
    # Revocar dos veces la misma llave es un 404, no un 204 mentiroso.
    assert client.delete(f"/auth/tokens/{creado['id']}").status_code == 404


def test_el_token_guarda_solo_el_sha256(client, index_path):
    import hashlib
    import sqlite3

    creado = _crear_token(client)
    conn = sqlite3.connect(index_path)
    try:
        fila = conn.execute("SELECT token_hash FROM capture_tokens").fetchone()
    finally:
        conn.close()
    assert fila is not None
    assert fila[0] == hashlib.sha256(creado["token"].encode("utf-8")).hexdigest()
    assert fila[0] != creado["token"]


def test_capturar_con_el_token_y_ver_que_se_uso(client, vault_path):
    creado = _crear_token(client)
    respuesta = client.post(
        "/capture",
        json={"text": "desde el atajo", "title": "Atajo", "source": "shortcut"},
        headers=_bearer(creado["token"]),
    )
    assert respuesta.status_code == 201, respuesta.text
    datos, _ = _frontmatter(vault_path / respuesta.json()["path"])
    assert datos["capture_source"] == "shortcut"
    assert datos["captured_by"] == NOMBRE_PRUEBA

    # `last_used_at` se mueve al usarlo (la lista se pide con la sesión).
    listado = client.get("/auth/tokens").json()
    assert listado[0]["last_used_at"] is not None
    assert listado[0]["id"] == creado["id"]


def test_el_token_tambien_vale_en_la_cabecera_x_user_token(client):
    creado = _crear_token(client)
    client.cookies.clear()
    respuesta = client.post(
        "/capture",
        json={"text": "por la otra cabecera"},
        headers={"X-User-Token": creado["token"]},
    )
    assert respuesta.status_code == 201, respuesta.text


def test_un_token_revocado_ya_no_captura(client):
    creado = _crear_token(client)
    cabeceras = _bearer(creado["token"])
    assert client.post("/capture", json={"text": "x"}, headers=cabeceras).status_code == 201
    # Revocar se hace con la sesión, que ahora mismo sigue puesta.
    assert client.delete(f"/auth/tokens/{creado['id']}").status_code == 204
    client.cookies.clear()
    assert client.post("/capture", json={"text": "y"}, headers=cabeceras).status_code == 401


def test_un_token_desconocido_no_captura(client):
    client.cookies.clear()
    respuesta = client.post(
        "/capture", json={"text": "x"}, headers={"Authorization": "Bearer inventado"}
    )
    assert respuesta.status_code == 401


def test_el_token_solo_abre_post_capture(client):
    creado = _crear_token(client)
    client.cookies.clear()  # sin sesión: lo único que queda es el token
    cabeceras = _bearer(creado["token"])

    for ruta in ("/pages", "/pages/tree", "/inbox", "/status", "/auth/me", "/auth/tokens"):
        respuesta = client.get(ruta, headers=cabeceras)
        assert respuesta.status_code == 403, (ruta, respuesta.status_code, respuesta.text)
        assert "solo abre POST /capture" in respuesta.json()["detail"]

    # Ni siquiera las rutas de escritura: es una llave de captura, no del dueño.
    assert client.post("/pages", json={"title": "x"}, headers=cabeceras).status_code == 403
    assert (
        client.delete(
            "/pages/00000000-0000-0000-0000-000000000000", headers=cabeceras
        ).status_code
        == 403
    )
    # Ni crear más tokens con ella.
    assert client.post("/auth/tokens", json={}, headers=cabeceras).status_code == 403
    # `GET /capture` tampoco: el permiso es a `POST /capture`, no al prefijo.
    assert client.get("/capture", headers=cabeceras).status_code == 403


def test_el_token_de_agente_y_el_de_captura_no_se_pisan(client, monkeypatch):
    """Dos secretos acotados distintos: el de agente sigue sin abrir /capture."""
    monkeypatch.setattr(settings, "agent_tokens", "tok-agente:agente-prueba")
    client.cookies.clear()
    respuesta = client.post(
        "/capture", json={"text": "x"}, headers={"X-Agent-Token": "tok-agente"}
    )
    assert respuesta.status_code == 403
    assert "/agent" in respuesta.json()["detail"]


def test_los_tokens_son_de_cada_usuario(client):
    """Un token no se puede revocar desde otra cuenta, aunque se sepa su id."""
    from app import auth
    from app.index import open_index

    creado = _crear_token(client, name="mío")
    conn = open_index(settings.index_path)
    try:
        auth.create_user(
            conn, username="marta", display_name="Marta", password="otra-contrasena"
        )
    finally:
        conn.close()

    # Con la sesión del dueño se ve el suyo; con la de Marta, ninguno.
    assert len(client.get("/auth/tokens").json()) == 1
    client.cookies.clear()
    entrada = client.post(
        "/auth/login", json={"username": "marta", "password": "otra-contrasena"}
    )
    assert entrada.status_code == 200, entrada.text
    assert client.get("/auth/tokens").json() == []
    assert client.delete(f"/auth/tokens/{creado['id']}").status_code == 404

    # Y el token sigue valiendo para capturar: su dueño no lo ha revocado.
    client.cookies.clear()
    assert (
        client.post(
            "/capture", json={"text": "sigue vivo"}, headers=_bearer(creado["token"])
        ).status_code
        == 201
    )


# --------------------------------------------------------------------------
# 3. la bandeja en la interfaz: listar y procesar
# --------------------------------------------------------------------------


def test_la_bandeja_lista_lo_que_hay_y_se_vacia_al_procesar(client, vault_path):
    vacia = client.get("/inbox").json()
    assert vacia["count"] == 0 and vacia["pages"] == []
    assert vacia["dir"] == settings.capture_dir == "Bandeja"

    primera = client.post("/capture", json={"text": "una", "title": "Primera"}).json()
    segunda = client.post("/capture", json={"text": "dos", "title": "Segunda"}).json()

    bandeja = client.get("/inbox").json()
    assert bandeja["count"] == 2
    # Lo más reciente primero: es lo que se procesa antes.
    assert [p["title"] for p in bandeja["pages"]] == ["Segunda", "Primera"]
    assert {p["id"] for p in bandeja["pages"]} == {primera["id"], segunda["id"]}

    # «Procesar» = el PATCH de `parent_id` de siempre: la nota pasa a colgar de
    # la página elegida (que hace de carpeta), como cualquier otra nota.
    carpeta = client.post("/pages", json={"title": "Proyectos"}).json()
    movida = client.patch(f"/pages/{primera['id']}", json={"parent_id": carpeta["id"]})
    assert movida.status_code == 200, movida.text
    assert movida.json()["parent_id"] == carpeta["id"]
    # El fichero se ha movido de la bandeja a la carpeta, no copiado.
    assert not (vault_path / primera["path"]).exists()
    assert (vault_path / "Notas" / "Proyectos" / "Primera.md").is_file()
    assert [p["id"] for p in client.get("/inbox").json()["pages"]] == [segunda["id"]]
    # Y sigue en el índice después de reconstruirlo desde el vault.
    assert client.post("/reindex").status_code == 200
    assert client.get(f"/pages/{primera['id']}").status_code == 200


def test_la_bandeja_se_puede_cambiar_de_carpeta(client, vault_path, monkeypatch):
    monkeypatch.setattr(settings, "capture_dir", "Entrada")
    creada = client.post(
        "/capture", json={"text": "en otra carpeta", "title": "Suelta"}
    ).json()
    assert creada["path"] == "Entrada/Suelta.md"
    assert (vault_path / "Entrada" / "Suelta.md").is_file()
    assert client.get("/inbox").json()["dir"] == "Entrada"
    assert [p["id"] for p in client.get("/inbox").json()["pages"]] == [creada["id"]]


@pytest.mark.parametrize("ruta", ["/inbox", "/capture"])
def test_la_bandeja_y_la_captura_piden_identidad(client, ruta):
    client.cookies.clear()
    assert client.get(ruta).status_code == 401


# --------------------------------------------------------------------------
# 4. la PWA: manifiesto e iconos, sin sesión
# --------------------------------------------------------------------------


def _tamano_png(raw: bytes) -> tuple[int, int]:
    """Ancho y alto de un PNG, leídos de su cabecera IHDR."""
    import struct

    assert raw[:8] == b"\x89PNG\r\n\x1a\n", "no es un PNG"
    ancho, alto = struct.unpack(">II", raw[16:24])
    return ancho, alto


def test_el_manifiesto_y_sus_iconos_se_sirven_sin_sesion(client):
    """Al instalar la PWA el navegador pide esto **sin** la cookie de la página."""
    client.cookies.clear()

    manifiesto = client.get("/manifest.webmanifest")
    assert manifiesto.status_code == 200, manifiesto.text
    # `StaticFiles` no conoce `.webmanifest`: con otro tipo MIME no se instala.
    assert manifiesto.headers["content-type"].startswith("application/manifest+json")
    datos = manifiesto.json()
    assert datos["display"] == "standalone"
    assert datos["start_url"] == "/" and datos["scope"] == "/"

    tamanos = set()
    for icono in datos["icons"]:
        respuesta = client.get(icono["src"])
        assert respuesta.status_code == 200, (icono["src"], respuesta.status_code)
        assert respuesta.headers["content-type"] == "image/png"
        assert "%dx%d" % _tamano_png(respuesta.content) == icono["sizes"]
        tamanos.add(icono["sizes"])
    assert tamanos == {"192x192", "512x512"}
    # Sin service worker: el manifiesto no promete nada sin conexión.
    assert "service_worker" not in manifiesto.text


def test_los_iconos_publicos_no_abren_el_panel(client):
    """El permiso es al fichero del icono, no al prefijo `/icons/`.

    `/icons/%2e%2e/app.js` llega al middleware ya decodificado: con un
    `startswith("/icons/")` el panel entero se servía sin sesión (200 con el
    `app.js`), que es justo lo que la puerta de P11a no puede permitir.
    """
    client.cookies.clear()
    for ruta in (
        "/icons/../app.js",
        "/icons/%2e%2e/app.js",
        "/icons/../index.html",
    ):
        assert client.get(ruta).status_code == 401, ruta
    # Un icono que no existe sí es público: 404, nunca el panel.
    assert client.get("/icons/no-existe.png").status_code == 404


# --------------------------------------------------------------------------
# 10. el script de Linux, ejecutado como lo lanza el escritorio
# --------------------------------------------------------------------------
#
# Los dos defectos que se arreglaron aquí solo se ven ejecutando el script tal y
# como lo lanza un atajo: la entrada en /dev/null (no hay terminal) y una
# `MDM_URL` copiada del navegador, con barra final. Se ejecuta sin red: `curl`,
# `zenity` y `notify-send` son dobles en un PATH temporal que lo apuntan todo.

SCRIPT_LINUX = (
    Path(__file__).resolve().parents[2] / "tools" / "capture" / "linux" / "captura.sh"
)
MDM_DESKTOP = SCRIPT_LINUX.with_name("mdm-captura.desktop")


def _doble(carpeta, nombre, cuerpo):
    ruta = carpeta / nombre
    ruta.write_text("#!/bin/sh\n" + cuerpo, encoding="utf-8")
    ruta.chmod(0o755)
    return ruta


def _correr_captura(tmp_path, *, entorno, entrada=None, directo=False):
    """Lanza `captura.sh` con dobles. Devuelve (resultado, llamadas, enviado).

    Con `directo=True` se ejecuta el fichero por su ruta (`execve`, como el
    atajo de GNOME y el `Exec=` del `.desktop`), no a través de `sh`.
    """
    orden = [str(SCRIPT_LINUX)] if directo else ["sh", str(SCRIPT_LINUX)]
    bin_falso = tmp_path / "bin"
    bin_falso.mkdir()
    registro = tmp_path / "llamadas.log"
    cuerpo = tmp_path / "cuerpo.json"

    _doble(
        bin_falso,
        "notify-send",
        'printf "notify-send %s\\n" "$*" >> "@REGISTRO@"\n'.replace(
            "@REGISTRO@", str(registro)
        ),
    )
    _doble(
        bin_falso,
        "zenity",
        "\n".join(
            [
                'printf "zenity %s\\n" "$*" >> "@REGISTRO@"',
                'for arg in "$@"; do',
                '  if [ "$arg" = "--entry" ]; then printf "una idea del cuadro"; fi',
                "done",
                "",
            ]
        ).replace("@REGISTRO@", str(registro)),
    )
    _doble(
        bin_falso,
        "curl",
        "\n".join(
            [
                'printf "curl %s\\n" "$*" >> "@REGISTRO@"',
                'cat > "@CUERPO@"',
                "echo '{\"id\":\"x\",\"title\":\"Captura de prueba\",\"path\":\"Bandeja/Captura de prueba.md\"}'",
                "echo 201",
                "",
            ]
        )
        .replace("@REGISTRO@", str(registro))
        .replace("@CUERPO@", str(cuerpo)),
    )

    medio = {"PATH": str(bin_falso) + ":" + os.environ.get("PATH", "/usr/bin:/bin")}
    medio.update(entorno)
    if entrada is None:
        resultado = subprocess.run(
            orden,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(tmp_path),
            env=medio,
        )
    else:
        resultado = subprocess.run(
            orden,
            input=entrada,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(tmp_path),
            env=medio,
        )

    llamadas = registro.read_text(encoding="utf-8") if registro.exists() else ""
    enviado = json.loads(cuerpo.read_text(encoding="utf-8")) if cuerpo.exists() else None
    return resultado, llamadas, enviado


def test_el_script_de_linux_pregunta_aunque_la_entrada_llegue_en_dev_null(tmp_path):
    """El atajo de GNOME y `mdm-captura.desktop` (Terminal=false) dan stdin en
    `/dev/null`: con `[ -t 0 ]` el script leía `cat` vacío y contestaba «No hay
    nada que apuntar» **sin abrir nunca el cuadro**, así que el atajo no servía
    para nada. La condición es «¿hay texto?», no «¿es un terminal?»."""
    resultado, llamadas, enviado = _correr_captura(
        tmp_path,
        entorno={"MDM_TOKEN": "token-de-prueba", "MDM_URL": "http://127.0.0.1:9"},
    )

    assert resultado.returncode == 0, resultado.stderr
    assert "zenity --entry" in llamadas, llamadas
    assert enviado == {"text": "una idea del cuadro", "source": "cli"}
    assert "http://127.0.0.1:9/capture" in llamadas


def test_el_script_de_linux_sigue_leyendo_la_tuberia_sin_abrir_el_cuadro(tmp_path):
    """`printf 'idea' | captura.sh` no tiene que abrir ninguna ventana."""
    resultado, llamadas, enviado = _correr_captura(
        tmp_path,
        entorno={"MDM_TOKEN": "token-de-prueba", "MDM_URL": "http://127.0.0.1:9"},
        entrada="desde una tubería\n",
    )

    assert resultado.returncode == 0, resultado.stderr
    assert "zenity --entry" not in llamadas, llamadas
    assert enviado == {"text": "desde una tubería", "source": "cli"}


def test_el_script_de_linux_normaliza_las_barras_finales_de_mdm_url(tmp_path):
    """Copiar `http://localhost:8000/` no puede acabar en `//capture`.

    El middleware contesta 403 «ese token solo sirve para /capture» y el aviso
    culpaba al token: el usuario revocaba uno bueno y seguía igual.
    """
    resultado, llamadas, _ = _correr_captura(
        tmp_path,
        entorno={"MDM_TOKEN": "token-de-prueba", "MDM_URL": "http://127.0.0.1:8000///"},
        entrada="con barra final",
    )

    assert resultado.returncode == 0, resultado.stderr
    assert "http://127.0.0.1:8000/capture" in llamadas, llamadas
    assert "//capture" not in llamadas, llamadas


def test_los_lanzadores_de_linux_viajan_ejecutables():
    """El atajo de GNOME (`command` con la ruta del script) y el `Exec=` del
    `.desktop` lanzan el fichero directamente, sin shell: con el bit apagado el
    proceso no llega a arrancar —`Permission denied`, exit 126— y el usuario no
    recibe ningún aviso, que es justo lo que el script evita al resto de fallos.

    El modo viaja en el índice de git, así que esto cubre también el `git
    clone` y el `cp` que hacen los documentos de instalación.
    """
    for ruta in (SCRIPT_LINUX, MDM_DESKTOP):
        modo = ruta.stat().st_mode & 0o777
        assert modo & 0o111, (
            f"{ruta.name} viaja en {modo:03o}: el atajo moriría con «Permission denied»"
        )


def test_el_script_de_linux_arranca_sin_pasar_por_sh(tmp_path):
    """Como el atajo: `execve` sobre el fichero, no `sh captura.sh`."""
    assert os.access(SCRIPT_LINUX, os.X_OK), "sin el bit de ejecución no hay execve"
    resultado, llamadas, enviado = _correr_captura(
        tmp_path,
        entorno={"MDM_TOKEN": "token-de-prueba", "MDM_URL": "http://127.0.0.1:9"},
        directo=True,
    )

    assert resultado.returncode == 0, resultado.stderr
    assert "zenity --entry" in llamadas, llamadas
    assert enviado == {"text": "una idea del cuadro", "source": "cli"}

