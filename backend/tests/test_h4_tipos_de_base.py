"""H4: bases tipadas, Proyectos y Tareas.

Una base tipada sigue siendo libre pero declara qué columna hace de qué
(responsable, estado, inicio, fin, horas…). Las columnas de un rol se pueden
renombrar y ocultar, no borrar ni cambiar de tipo.
"""

import pytest


def _crear(client, plantilla, **extra):
    r = client.post("/databases/from-template", json={"template": plantilla, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_el_catalogo_ofrece_proyectos_y_tareas_primero(client):
    catalogo = client.get("/databases/templates").json()
    assert [(p["key"], p["kind"]) for p in catalogo[:2]] == [("proyectos", "projects"), ("tareas", "tasks")]
    assert all(p["kind"] is None for p in catalogo[2:])


def test_crear_una_base_de_proyectos(client):
    base = _crear(client, "proyectos", title="Mis proyectos")
    assert base["kind"]["type"] == "projects"
    assert base["kind"]["roles"] == {"assignee": "responsable", "status": "estado", "start": "inicio", "end": "fin"}
    # El estado nace con grupos y «Hecho» cierra.
    assert [(g["name"], g["closed"]) for g in base["closed"]["groups"]] == [
        ("Pendiente", False), ("En curso", False), ("Hecho", True)]
    assert base["closed"]["values"] == ["Hecho"]
    assert client.get(f"/databases/{base['id']}").json()["kind"] == base["kind"]


def test_crear_tareas_con_y_sin_proyecto(client):
    sin = _crear(client, "tareas", title="Sueltas")
    assert sin["kind"]["type"] == "tasks" and "project" not in sin["kind"]["roles"]
    assert sin["kind"]["options"] == {"hours_split": "full"}
    assert {"horas", "fin", "inicio"} <= {p["key"] for p in sin["schema_def"]}
    proyectos = _crear(client, "proyectos", title="Proyectos")
    con = _crear(client, "tareas", title="De proyecto", project_database=proyectos["id"])
    clave = con["kind"]["roles"]["project"]
    columna = next(p for p in con["schema_def"] if p["key"] == clave)
    assert columna["type"] == "relation" and columna["target_database"] == proyectos["id"]
    # Una base que no es de Proyectos no vale como destino.
    assert client.post("/databases/from-template", json={"template": "tareas", "project_database": sin["id"]}).status_code == 400


def test_las_columnas_de_un_rol_no_se_borran_ni_cambian_de_tipo(client):
    base = _crear(client, "tareas", title="Protegida")
    esquema = base["schema_def"]
    sin_horas = [p for p in esquema if p["key"] != "horas"]
    r = client.patch(f"/databases/{base['id']}", json={"schema_def": sin_horas})
    assert r.status_code == 400 and "Horas" in r.json()["detail"]
    como_texto = [dict(p, type="text") if p["key"] == "horas" else p for p in esquema]
    r = client.patch(f"/databases/{base['id']}", json={"schema_def": como_texto})
    assert r.status_code == 400 and "tipo" in r.json()["detail"]
    # Renombrar sí, y añadir columnas propias también.
    renombrada = [dict(p, name="Esfuerzo (h)") if p["key"] == "horas" else p for p in esquema]
    renombrada.append({"key": "", "name": "Notas", "type": "text"})
    r = client.patch(f"/databases/{base['id']}", json={"schema_def": renombrada})
    assert r.status_code == 200, r.text
    assert any(p["name"] == "Esfuerzo (h)" for p in r.json()["schema_def"])
    # Una columna que no es de ningún rol se borra cuando quieras.
    sin_notas = [p for p in r.json()["schema_def"] if p["name"] != "Notas"]
    assert client.patch(f"/databases/{base['id']}", json={"schema_def": sin_notas}).status_code == 200


def test_convertir_una_base_que_ya_existe_reutiliza_sus_columnas(client):
    vieja = client.post("/databases", json={"title": "Mi tablero", "schema_def": [
        {"name": "Quién", "type": "person"},
        {"name": "Estado", "type": "select", "options": ["Nueva", "En marcha", "Completada"]},
        {"name": "Entrega", "type": "date"},
        {"name": "Notas", "type": "text"},
    ]}).json()
    r = client.put(f"/databases/{vieja['id']}/kind", json={"type": "tasks"})
    assert r.status_code == 200, r.text
    base = r.json()
    por_clave = {p["key"]: p["name"] for p in base["schema_def"]}
    roles = base["kind"]["roles"]
    assert por_clave[roles["assignee"]] == "Quién"
    assert por_clave[roles["status"]] == "Estado"
    assert por_clave[roles["end"]] == "Entrega"
    # Las que faltaban se crean: inicio y horas.
    assert por_clave[roles["start"]] == "Inicio" and por_clave[roles["hours"]] == "Horas"
    assert "Notas" in por_clave.values()  # lo que no es de un rol se queda
    # El estado, que no declaraba nada, recibe grupos con «Completada» como hecha.
    assert base["closed"]["values"] == ["Completada"]
    assert [g["name"] for g in base["closed"]["groups"]] == ["Pendiente", "Hecho"]
    # Y vuelve a ser normal cuando se quiere.
    normal = client.delete(f"/databases/{vieja['id']}/kind").json()
    assert normal["kind"] is None and len(normal["schema_def"]) == len(base["schema_def"])


def test_convertir_crea_el_estado_si_no_hay_y_acepta_opciones(client):
    vacia = client.post("/databases", json={"title": "Vacía", "schema_def": []}).json()
    r = client.put(f"/databases/{vacia['id']}/kind", json={"type": "tasks", "options": {"hours_split": "equal"}})
    assert r.status_code == 200, r.text
    roles = r.json()["kind"]["roles"]
    assert set(roles) == {"assignee", "status", "start", "end", "hours"}
    assert r.json()["kind"]["options"] == {"hours_split": "equal"}
    assert r.json()["closed"]["values"] == ["Hecha"]
    assert client.put(f"/databases/{vacia['id']}/kind", json={"type": "tasks", "options": {"hours_split": "mitad"}}).status_code == 400
    assert client.put(f"/databases/{vacia['id']}/kind", json={"type": "tasks", "options": {"otra": 1}}).status_code == 400


def test_proyecto_con_estimacion_y_roles_a_mano(client):
    base = client.post("/databases", json={"title": "Prj", "schema_def": [
        {"name": "Lead", "type": "person"}, {"name": "Fase", "type": "select", "options": ["A", "Hecho"]},
        {"name": "Arranque", "type": "date"}, {"name": "Cierre", "type": "date"},
    ]}).json()
    claves = {p["name"]: p["key"] for p in base["schema_def"]}
    r = client.put(f"/databases/{base['id']}/kind", json={
        "type": "projects", "estimate": True,
        "roles": {"assignee": claves["Lead"], "status": claves["Fase"], "start": claves["Arranque"], "end": claves["Cierre"]},
    })
    assert r.status_code == 200, r.text
    assert "estimate" in r.json()["kind"]["roles"]
    # Un rol apuntando a una columna del tipo equivocado es un 400.
    mal = client.put(f"/databases/{base['id']}/kind", json={"type": "projects", "roles": {"assignee": claves["Fase"]}})
    assert mal.status_code == 400


def test_el_tipo_sobrevive_a_reindexar(client):
    base = _crear(client, "tareas", title="Persistente")
    assert client.post("/reindex?force=true").status_code == 200
    assert client.get(f"/databases/{base['id']}").json()["kind"] == base["kind"]


def test_renombrar_una_columna_de_rol_mueve_el_rol_a_su_clave_nueva(client):
    base = _crear(client, "tareas", title="Con renombrado")
    nuevo = [dict(p, name="Esfuerzo (h)") if p["key"] == "horas" else p for p in base["schema_def"]]
    r = client.patch(f"/databases/{base['id']}", json={"schema_def": nuevo})
    assert r.status_code == 200, r.text
    claves = {p["name"]: p["key"] for p in r.json()["schema_def"]}
    assert r.json()["kind"]["roles"]["hours"] == claves["Esfuerzo (h)"]
    # Y sigue protegida bajo su nombre nuevo.
    sin = [p for p in r.json()["schema_def"] if p["name"] != "Esfuerzo (h)"]
    assert client.patch(f"/databases/{base['id']}", json={"schema_def": sin}).status_code == 400


def test_guardar_el_esquema_no_pierde_los_grupos_de_estado(client):
    base = _crear(client, "tareas", title="Con grupos")
    r = client.patch(f"/databases/{base['id']}", json={"schema_def": base["schema_def"] + [{"key": "", "name": "Notas", "type": "text"}]})
    assert r.status_code == 200, r.text
    assert [g["name"] for g in r.json()["closed"]["groups"]] == ["Pendiente", "En curso", "Hecho"]
    assert r.json()["closed"]["values"] == ["Hecha"]
