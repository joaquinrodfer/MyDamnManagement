"""La pantalla de entrada (P11a): login y alta del primer usuario.

Se sirve desde FastAPI (`GET /login`) y no desde el panel: la SPA vive detrás
de la sesión, así que la página que pide la sesión no puede ser parte de la
SPA. Es HTML con su CSS y su JS en línea, sin dependencias ni peticiones
externas: nada que cargar antes de poder entrar.

El tema es el mismo del panel (copia mínima de las variables del `:root` de
`frontend/index.html`, no un import: si el panel cambia de paleta, esta página
puede quedarse atrás sin romper nada, que es peor lo contrario —que el login
dependa del fichero que solo se sirve con sesión).

El formulario que se pinta depende de si hay usuarios:

- sin usuarios: alta del primero (que será admin);
- con usuarios: login normal.

La misma página hace las dos cosas porque el JS solo cambia el `endpoint` del
formulario (`/auth/setup` o `/auth/login`); no hay dos plantillas que mantener.
"""

from __future__ import annotations

import html
from urllib.parse import quote

# Variables del `:root` de `frontend/index.html` (tema oscuro). Se copian y no
# se enlazan a propósito: ver el docstring del módulo.
_CSS = """
  :root {
    color-scheme: dark;
    --bg: #0f1117;
    --panel: #161922;
    --surface-elevated: #1e222e;
    --panel-border: #2a2f3d;
    --border-input: #6c7690;
    --ink: #eef0f6;
    --ink-muted: #949db3;
    --accent: #5e6ad2;
    --accent-btn: #4e5ac0;
    --accent-contrast: #ffffff;
    --down: #f26464;
    --down-bg: rgba(242, 100, 100, 0.15);
    --font-body: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui,
      "Helvetica Neue", Arial, sans-serif;
  }
  @font-face { font-family: "Inter"; font-style: normal; font-display: swap; font-weight: 100 900;
    src: url(/fonts/inter-latin-wght-normal.woff2) format("woff2-variations");
    unicode-range: U+0000-00FF,U+0131,U+0152-0153,U+2000-206F,U+20AC,U+2122; }
  @font-face { font-family: "Inter"; font-style: normal; font-display: swap; font-weight: 100 900;
    src: url(/fonts/inter-latin-ext-wght-normal.woff2) format("woff2-variations");
    unicode-range: U+0100-02BA,U+1E00-1E9F,U+A720-A7FF; }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0;
    display: grid;
    place-items: center;
    padding: 24px;
    background: var(--bg);
    color: var(--ink);
    font-family: var(--font-body);
    font-size: 15px;
    line-height: 1.5;
  }
  main {
    width: 100%;
    max-width: 380px;
    background: var(--panel);
    border: 1px solid var(--panel-border);
    border-radius: 16px;
    padding: 32px;
    box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.04), 0 24px 60px -20px rgba(0, 0, 0, 0.7);
  }
  h1 { margin: 0 0 4px; font-size: 19px; font-weight: 600; }
  p.sub { margin: 0 0 22px; color: var(--ink-muted); font-size: 13px; }
  p.sub:empty { display: none; }
  h1 + p.sub:empty + p.error + form { margin-top: 18px; }
  /* La marca (ver `BRAND.md`): símbolo B2 a 28 px y el nombre escrito. */
  .brand { display: flex; align-items: center; gap: 7px; margin: 0 0 20px; }
  .brand-name { font-family: Inter, var(--font-body); font-weight: 600;
    letter-spacing: -0.03em; font-size: 15px; color: var(--ink); }
  .brand-name span { color: #9AA2FF; }
  label { display: block; margin-bottom: 6px; font-size: 13px; color: var(--ink-muted); }
  input {
    width: 100%;
    margin-bottom: 16px;
    padding: 9px 11px;
    background: #11131a;
    border: 1px solid #343a4a;
    border-radius: 8px;
    color: var(--ink);
    font: inherit;
    transition: border-color .12s ease, box-shadow .12s ease;
  }
  input:hover { border-color: #4b5367; }
  input:focus-visible {
    outline: none;
    border-color: var(--accent);
    box-shadow: 0 0 0 3px rgba(94, 106, 210, 0.35);
  }
  button:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  button {
    width: 100%;
    padding: 10px 12px;
    background: linear-gradient(180deg, #6872d6, var(--accent-btn));
    color: var(--accent-contrast);
    border: 1px solid #3d4699;
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.16), 0 1px 2px rgba(0, 0, 0, 0.25);
    border-radius: 8px;
    font: inherit;
    font-weight: 600;
    cursor: pointer;
  }
  button[disabled] { opacity: 0.6; cursor: default; }
  .error {
    display: none;
    margin: 0 0 16px;
    padding: 9px 11px;
    background: var(--down-bg);
    border: 1px solid var(--down);
    border-radius: 7px;
    color: var(--down);
    font-size: 13px;
  }
  .error.on { display: block; }
"""

# Sanitiza `?next=` para no convertir el login en un redirector abierto: solo
# rutas del propio origen y nunca `//host` (que el navegador lee como otro
# dominio) ni una URL absoluta.
def safe_next(raw: str | None) -> str:
    value = (raw or "").strip()
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    if "\n" in value or "\r" in value:
        return "/"
    return value


def render(*, needs_setup: bool, next_url: str = "/") -> str:
    destino = html.escape(safe_next(next_url), quote=True)
    endpoint = "/auth/setup" if needs_setup else "/auth/login"
    if needs_setup:
        titulo = "Crea el primer usuario"
        subtitulo = "Es el administrador de este cerebro. Podrás crear más después."
        extra = (
            '        <label for="display_name">Nombre visible</label>\n'
            '        <input id="display_name" name="display_name" autocomplete="name"'
            ' required maxlength="80" />\n'
        )
    else:
        titulo = "Entrar"
        subtitulo = ""
        extra = ""

    return f"""<!doctype html>
<html lang="es" data-theme="dark">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{html.escape(titulo)} · Notemil</title>
<link rel="icon" href="/favicon.ico" sizes="48x48" />
<link rel="icon" href="/icons/notemil-mark.svg" type="image/svg+xml" />
<link rel="apple-touch-icon" href="/icons/notemil-180.png" />
<style>{_CSS}</style>
</head>
<body>
<main>
  <div class="brand"><svg viewBox="0 0 64 64" width="28" height="28" fill="none" aria-hidden="true"><rect x=".5" y=".5" width="63" height="63" rx="15" fill="#1C2142" stroke="#333A6B"/><path d="M20.5 46.5V18.5L43.5 46.5V31" stroke="#9AA2FF" stroke-width="4.6" stroke-linecap="round" stroke-linejoin="round"/><circle cx="43.5" cy="19" r="4.6" fill="#FF6B6B"/></svg><span class="brand-name">Note<span>mil</span></span></div>
  <h1>{html.escape(titulo)}</h1>
  <p class="sub">{html.escape(subtitulo)}</p>
  <!-- El aviso va antes del formulario y con `role="alert"`: quien usa lector
       de pantalla se entera del fallo sin tener que buscarlo. -->
  <p class="error" id="error" role="alert"></p>
  <form id="form" data-endpoint="{endpoint}" data-next="{destino}" novalidate>
    <label for="username">Usuario</label>
    <input id="username" name="username" autocomplete="username"
           autocapitalize="none" spellcheck="false" required
           pattern="[a-z][a-z0-9_]{{2,31}}" title="minúsculas, dígitos y _ (3-32)" />
{extra}    <label for="password">Contraseña</label>
    <input id="password" name="password" type="password"
           autocomplete="{'new-password' if needs_setup else 'current-password'}"
           required minlength="8" />
    <button type="submit" id="submit">{'Crear y entrar' if needs_setup else 'Entrar'}</button>
  </form>
</main>
<script>
(() => {{
  const form = document.getElementById("form");
  const error = document.getElementById("error");
  const submit = document.getElementById("submit");
  const next = form.dataset.next || "/";

  function show(message) {{
    error.textContent = message;
    error.classList.add("on");
  }}

  form.addEventListener("submit", async (event) => {{
    event.preventDefault();
    error.classList.remove("on");
    submit.disabled = true;
    const body = {{}};
    for (const [key, value] of new FormData(form).entries()) body[key] = String(value);
    try {{
      const res = await fetch(form.dataset.endpoint, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json", "Accept": "application/json" }},
        body: JSON.stringify(body),
      }});
      const data = await res.json().catch(() => null);
      if (!res.ok) {{
        show((data && data.detail) || `Error ${{res.status}}`);
        return;
      }}
      // `replace`: el login no tiene que quedar en el historial, o «atrás»
      // devuelve a la pantalla de entrada con la sesión ya abierta.
      window.location.replace(next);
    }} catch (e) {{
      show("No se pudo conectar con el servidor");
    }} finally {{
      submit.disabled = false;
    }}
  }});
}})();
</script>
</body>
</html>
"""


def redirect_to_login(path: str) -> str:
    """URL del login llevándose a dónde se quería ir (`?next=`)."""
    destino = safe_next(path)
    if destino == "/":
        return "/login"
    return "/login?next=" + quote(destino, safe="/")
