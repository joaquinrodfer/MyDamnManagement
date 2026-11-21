"""Importar festivos: .ics, URL suscrita y festivos oficiales (C1b).

Sin red: las descargas se sustituyen por textos fijos (`fetcher`).
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from app import holiday_import as hi

ICS = """BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
DTSTART;VALUE=DATE:20261208\r
DTEND;VALUE=DATE:20261209\r
SUMMARY:Inmaculada Concepci\r
 ón\r
END:VEVENT\r
BEGIN:VEVENT\r
DTSTART;VALUE=DATE:20261224\r
DTEND;VALUE=DATE:20261226\r
SUMMARY:Navidad\\, cerrado\r
END:VEVENT\r
BEGIN:VEVENT\r
DTSTART;VALUE=DATE:20250101\r
RRULE:FREQ=YEARLY\r
SUMMARY:Año nuevo\r
END:VEVENT\r
BEGIN:VEVENT\r
DTSTART;VALUE=DATE:20261012\r
STATUS:CANCELLED\r
SUMMARY:Cancelado\r
END:VEVENT\r
BEGIN:VEVENT\r
DTSTART;TZID=Europe/Madrid:20260319T090000\r
DTEND;TZID=Europe/Madrid:20260319T180000\r
SUMMARY:San José\r
END:VEVENT\r
END:VCALENDAR\r
"""


def test_lee_un_ics_con_rangos_repeticiones_y_lineas_partidas():
    dias = hi.parse_ics(ICS, range(2026, 2028))
    assert (date(2026, 12, 8), "Inmaculada Concepción") in dias
    # DTEND de un día entero es el día siguiente al último: 24 y 25.
    assert [d for d, n in dias if n == "Navidad, cerrado"] == [date(2026, 12, 24), date(2026, 12, 25)]
    # La repetición anual sale en cada año pedido, no en 2025.
    assert [d for d, n in dias if n == "Año nuevo"] == [date(2026, 1, 1), date(2027, 1, 1)]
    assert not any(n == "Cancelado" for _, n in dias)
    assert (date(2026, 3, 19), "San José") in dias


def test_lo_que_no_es_un_calendario_se_rechaza():
    with pytest.raises(hi.ImportErrorMsg):
        hi.parse_ics("<html>hola</html>")


@pytest.mark.parametrize(
    "url",
    ["http://localhost/cal.ics", "http://127.0.0.1/x", "https://10.0.0.4/x", "http://169.254.169.254/latest", "http://[::1]/x"],
)
def test_no_se_sale_a_la_red_interna(url):
    with pytest.raises(hi.ImportErrorMsg):
        hi.fetch(url)


def test_solo_http_https_y_webcal():
    assert hi.normalize_url("webcal://ejemplo.org/f.ics") == "https://ejemplo.org/f.ics"
    for mala in ("ftp://ejemplo.org/f.ics", "file:///etc/passwd", "https://u:p@ejemplo.org/f.ics", "ejemplo.org"):
        with pytest.raises(hi.ImportErrorMsg):
            hi.normalize_url(mala)


NAGER = [
    {"date": "2026-01-01", "localName": "Año Nuevo", "global": True, "counties": None},
    {"date": "2026-05-02", "localName": "Fiesta de la Comunidad de Madrid", "global": False, "counties": ["ES-MD"]},
    {"date": "2026-04-23", "localName": "San Jorge", "global": False, "counties": ["ES-AR"]},
]


def _falso(url: str) -> str:
    if url.endswith("/ES"):
        return json.dumps([{**h, "date": url.split("/")[-2] + h["date"][4:]} for h in NAGER])
    if url.endswith("AvailableCountries"):
        return json.dumps([{"countryCode": "ES", "name": "Spain"}])
    if url.endswith(".ics"):
        return ICS
    raise hi.ImportErrorMsg("El servidor del calendario respondió con un error (404).")


def test_festivos_oficiales_por_region():
    hi._cache.clear()
    dias = hi.official_days("ES", "ES-MD", range(2026, 2027), _falso)
    assert [n for _, n in dias] == ["Año Nuevo", "Fiesta de la Comunidad de Madrid"]
    assert hi.regions("ES", 2026, _falso) == ["ES-AR", "ES-MD"]
    solo_nacionales = hi.official_days("ES", None, range(2026, 2027), _falso)
    assert [n for _, n in solo_nacionales] == ["Año Nuevo"]


def test_fuentes_por_api(client, monkeypatch):
    hi._cache.clear()
    monkeypatch.setattr(hi, "fetch", _falso)
    r = client.post("/holidays/import", json={"name": "Calendario de RR. HH.", "content": ICS})
    assert r.status_code == 201, r.text
    archivo = r.json()
    assert archivo["kind"] == "file" and archivo["count"] >= 5

    r = client.post("/holidays/sources", json={"kind": "country", "country": "es", "region": "ES-MD"})
    assert r.status_code == 201, r.text
    pais = r.json()
    assert pais["name"] == "Festivos oficiales ES-MD" and pais["count"] > 0

    r = client.post("/holidays/sources", json={"kind": "url", "url": "webcal://calendario.ejemplo/f.ics"})
    assert r.status_code == 201, r.text
    assert r.json()["url"] == "https://calendario.ejemplo/f.ics"

    nombres = {f["name"] for f in client.get("/holidays/sources").json()}
    assert nombres == {"Calendario de RR. HH.", "Festivos oficiales ES-MD", "calendario.ejemplo"}
    anio = date.today().year
    festivos = client.get(f"/holidays?year={anio}").json()["holidays"]
    assert any(h["source_name"] == "Festivos oficiales ES-MD" for h in festivos)

    # Un archivo no se «actualiza»; una suscripción sí.
    assert client.post(f"/holidays/sources/{archivo['id']}/sync").status_code == 400
    assert client.post(f"/holidays/sources/{pais['id']}/sync").status_code == 200
    # Quitar la fuente se lleva sus festivos y deja los demás.
    assert client.delete(f"/holidays/sources/{pais['id']}").status_code == 204
    festivos = client.get(f"/holidays?year={anio}").json()["holidays"]
    assert not any(h["source_name"] == "Festivos oficiales ES-MD" for h in festivos)
    assert any(h["source_name"] == "Calendario de RR. HH." for h in festivos)

    # Un calendario que no lo es: 400 y no se guarda nada.
    assert client.post("/holidays/import", json={"content": "hola"}).status_code == 400
    assert len(client.get("/holidays/sources").json()) == 2
    assert client.get("/holidays/countries").json() == [{"code": "ES", "name": "Spain"}]
    assert client.get("/holidays/regions?country=es").json() == ["ES-AR", "ES-MD"]


def test_la_relectura_diaria_no_borra_si_falla(client, monkeypatch):
    hi._cache.clear()
    monkeypatch.setattr(hi, "fetch", _falso)
    fuente = client.post("/holidays/sources", json={"kind": "url", "url": "https://calendario.ejemplo/f.ics"}).json()
    from app.dependencies import get_repo  # noqa: F401
    from app.main import app  # noqa: F401
    from app.config import settings
    from app.index import open_index

    conn = open_index(settings.index_path)
    try:
        conn.execute("UPDATE holiday_sources SET synced_at = '2000-01-01T00:00:00+00:00'")

        def roto(url):
            raise hi.ImportErrorMsg("No se pudo conectar con el servidor del calendario.")

        assert hi.sync_due(conn, roto) == 0
        fila = conn.execute("SELECT error FROM holiday_sources WHERE id = ?", (fuente["id"],)).fetchone()
        assert "No se pudo conectar" in fila[0]
        assert conn.execute("SELECT count(*) FROM holidays WHERE source_id = ?", (fuente["id"],)).fetchone()[0] == fuente["count"]
        assert hi.sync_due(conn, _falso) == 1
        assert conn.execute("SELECT error FROM holiday_sources WHERE id = ?", (fuente["id"],)).fetchone()[0] is None
    finally:
        conn.close()


def test_solo_quien_gestiona_importa(client):
    r = client.post("/auth/users", json={"username": "ivo", "display_name": "Ivo", "password": "contrasena-ivo"})
    assert r.status_code == 201
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        c.post("/auth/login", json={"username": "ivo", "password": "contrasena-ivo"})
        assert c.post("/holidays/import", json={"content": ICS}).status_code == 403
        assert c.post("/holidays/sources", json={"kind": "country", "country": "ES"}).status_code == 403
