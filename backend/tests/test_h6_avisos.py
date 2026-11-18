"""H6: avisos. Menciones, asignaciones, plazos, cambios y suscripción por página."""

import time
from datetime import date, timedelta

import pytest

from app import notifications
from app.events import bus
from test_h1_espacios import _sesion, cierra  # noqa: F401 - fixtures


def _avisos(c, **q):
    return c.get("/notifications", params=q).json()


def _compartida(client, titulo="Compartida", cuerpo=""):
    """Una página en el espacio, compartida con todos (para que otros la vean)."""
    return client.post("/pages", json={"title": titulo, "body_markdown": cuerpo, "section": "shared"}).json()


def test_una_mencion_avisa_una_vez_a_la_persona_mencionada(client, cierra):
    ana, u = _sesion(client, "anita")
    cierra.append(ana)
    pagina = _compartida(client)
    client.patch(f"/pages/{pagina['id']}", json={"body_markdown": "Revisad esto @anita por favor"})
    datos = _avisos(ana)
    assert datos["unread"] == 1 and datos["items"][0]["type"] == "mention"
    assert datos["items"][0]["page_id"] == pagina["id"] and "Compartida" in datos["items"][0]["message"]
    # Guardar otra vez lo mismo no repite el aviso, ni mencionarla de nuevo el mismo día.
    client.patch(f"/pages/{pagina['id']}", json={"body_markdown": "Revisad esto @anita por favor, gracias"})
    assert _avisos(ana)["unread"] == 1
    # Quien escribe no se avisa a sí mismo, y un @ dentro de un correo no cuenta.
    client.patch(f"/pages/{pagina['id']}", json={"body_markdown": "yo soy @demo y mi correo es a@anita.com"})
    assert _avisos(client)["unread"] == 0


def test_no_se_avisa_de_lo_que_no_se_puede_ver(client, cierra):
    ana, _ = _sesion(client, "anita")
    cierra.append(ana)
    privada = client.post("/pages", json={"title": "Privada", "section": "private"}).json()
    client.patch(f"/pages/{privada['id']}", json={"body_markdown": "hola @anita"})
    assert _avisos(ana)["items"] == []


def test_marcar_como_leidos(client, cierra):
    ana, _ = _sesion(client, "anita")
    cierra.append(ana)
    for i in range(3):
        p = _compartida(client, f"P{i}")
        client.patch(f"/pages/{p['id']}", json={"body_markdown": "@anita"})
    datos = _avisos(ana)
    assert datos["unread"] == 3
    uno = datos["items"][0]["id"]
    assert ana.post("/notifications/read", json={"ids": [uno]}).json() == {"marked": 1, "unread": 2}
    assert len(_avisos(ana, unread="true")["items"]) == 2
    assert ana.post("/notifications/read", json={"all": True}).json()["unread"] == 0
    assert ana.post("/notifications/read", json={}).status_code == 400
    # Solo toca los suyos.
    assert client.post("/notifications/read", json={"ids": [uno]}).json()["marked"] == 0


def test_silenciar_y_pedirlo_todo(client, cierra):
    ana, _ = _sesion(client, "anita")
    cierra.append(ana)
    p = _compartida(client, "Con modos")
    sub = f"/notifications/subscriptions/{p['id']}"
    assert ana.get(sub).json()["mode"] == "mentions_only"  # por defecto
    # Un cambio sin mención no avisa a quien solo quiere menciones.
    client.patch(f"/pages/{p['id']}", json={"body_markdown": "uno"})
    assert _avisos(ana)["items"] == []
    # Con `all`, sí; y agrupado: varios cambios seguidos, un solo aviso.
    assert ana.put(sub, json={"mode": "all"}).json()["mode"] == "all"
    for texto in ("dos", "tres", "cuatro"):
        client.patch(f"/pages/{p['id']}", json={"body_markdown": texto})
    cambios = [a for a in _avisos(ana)["items"] if a["type"] == "page_update"]
    assert len(cambios) == 1
    # Silenciada: ni menciones.
    ana.put(sub, json={"mode": "muted"})
    client.patch(f"/pages/{p['id']}", json={"body_markdown": "ahora @anita"})
    assert [a for a in _avisos(ana)["items"] if a["type"] == "mention"] == []
    assert ana.put(sub, json={"mode": "otra"}).status_code == 422
    # Y no se suscribe a lo que no ve.
    privada = client.post("/pages", json={"title": "Oculta", "section": "private"}).json()
    assert ana.put(f"/notifications/subscriptions/{privada['id']}", json={"mode": "all"}).status_code == 404


def test_ser_asignado_a_una_tarea_avisa(client, cierra):
    ana, u = _sesion(client, "anita")
    cierra.append(ana)
    base = client.post("/databases/from-template", json={"template": "tareas", "title": "Tareas", "section": "shared"}).json()
    yo = client.get("/auth/me").json()["id"]
    fila = client.post(f"/databases/{base['id']}/rows", json={"title": "Maquetar", "properties": {"responsable": u["id"]}}).json()
    datos = _avisos(ana)
    assert [a["type"] for a in datos["items"]] == ["task_assigned"]
    assert "Maquetar" in datos["items"][0]["message"]
    # Reasignarse a uno mismo no avisa; cambiar a otra persona, sí.
    client.patch(f"/databases/{base['id']}/rows/{fila['id']}", json={"properties": {"responsable": yo}})
    assert _avisos(client)["items"] == []
    client.patch(f"/databases/{base['id']}/rows/{fila['id']}", json={"properties": {"responsable": u["id"]}})
    assert _avisos(ana)["unread"] == 1  # el mismo día: el aviso ya existía (no se duplica)


def test_los_plazos_avisan_una_vez_al_dia(client, cierra):
    ana, u = _sesion(client, "anita")
    cierra.append(ana)
    base = client.post("/databases/from-template", json={"template": "tareas", "title": "Plazos", "section": "shared"}).json()
    hoy = date.today()
    for titulo, dias in (("Vencida", -2), ("Para mañana", 1), ("Lejana", 20)):
        client.post(f"/databases/{base['id']}/rows", json={"title": titulo, "properties": {
            "responsable": u["id"], "estado": "En curso", "fin": (hoy + timedelta(days=dias)).isoformat()}})
    # Se quitan los avisos de asignación para ver solo los de plazo.
    ana.post("/notifications/read", json={"all": True})
    from app.config import settings
    from app.index import open_index

    conn = open_index(settings.index_path)
    try:
        assert notifications.scan_deadlines(conn) == 2
        assert notifications.scan_deadlines(conn) == 0  # el mismo día no se repite
    finally:
        conn.close()
    tipos = sorted((a["type"], a["message"]) for a in _avisos(ana, unread="true")["items"])
    assert [t for t, _ in tipos] == ["deadline_overdue", "deadline_soon"]
    assert any("Vencida" in m for _, m in tipos) and any("mañana" in m for _, m in tipos)


def test_el_aviso_llega_por_eventos_solo_a_su_destinatario(client, cierra):
    from app.routers.events import _le_toca

    ana, u = _sesion(client, "anita")
    cierra.append(ana)
    yo = client.get("/auth/me").json()

    class Quien:
        def __init__(self, id, admin):
            self.id, self.is_admin = id, admin

    aviso = {"type": "notification", "action": "created", "user_id": u["id"]}
    assert _le_toca(Quien(u["id"], False), aviso) is True
    assert _le_toca(Quien(yo["id"], True), aviso) is False  # ni el administrador
    assert _le_toca(None, aviso) is False


def test_los_avisos_viejos_leidos_se_limpian(client):
    from app.config import settings
    from app.index import open_index

    conn = open_index(settings.index_path)
    try:
        conn.execute("INSERT INTO notifications (id,user_id,type,message,is_read,created_at) VALUES ('x','u','mention','viejo',1,'2020-01-01T00:00:00+00:00')")
        conn.execute("INSERT INTO notifications (id,user_id,type,message,is_read,created_at) VALUES ('y','u','mention','sin leer',0,'2020-01-01T00:00:00+00:00')")
        assert notifications.purge_old(conn, 90) == 1
        assert conn.execute("SELECT id FROM notifications").fetchall()[0][0] == "y"
    finally:
        conn.close()
