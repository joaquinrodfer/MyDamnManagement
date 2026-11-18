"""Carga del hito 5 de la 3.0 (ver docs/MEDICIONES-3.0-H5.md): N clientes con WebSocket + SSE.

Uso: python carga.py BASE PID [--clients 10] [--seconds 60]
Saca RSS/CPU del proceso PID, RTT de update->ack (p50/p95), latencia del
guardado en la base (de /status) y p95 de un PATCH por HTTP en paralelo.
"""
import argparse, asyncio, json, os, random, statistics, time, urllib.request, http.cookiejar
import websockets

def http_session(base):
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    def req(method, path, body=None):
        r = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                   headers={"Content-Type": "application/json"})
        try:
            with op.open(r, timeout=30) as f:
                raw = f.read()
                return f.status, (json.loads(raw) if raw[:1] in b"{[" else raw)
        except urllib.error.HTTPError as e:
            return e.code, e.read()
    return req, jar

def cookie(jar): return "; ".join(f"{c.name}={c.value}" for c in jar)

def proc_sample(pid):
    with open(f"/proc/{pid}/status") as f:
        rss = next(int(l.split()[1]) for l in f if l.startswith("VmRSS"))
    with open(f"/proc/{pid}/stat") as f:
        parts = f.read().rsplit(")", 1)[1].split()
    ticks = int(parts[11]) + int(parts[12])
    return rss, ticks

async def cliente(base, ck, pid_note, seconds, rtts, errores, typing_hz):
    url = base.replace("http", "ws") + f"/ws/pages/{pid_note}"
    async with websockets.connect(url, additional_headers={"Cookie": ck}, max_size=None) as ws:
        await ws.send(json.dumps({"type": "join", "lastSeenVersion": None}))
        j = json.loads(await ws.recv())
        doc_len = len(j["doc"].encode("utf-16-le")) // 2
        version = j["version"]
        fin = time.monotonic() + seconds
        cid = f"c{random.randrange(10**9)}"
        while time.monotonic() < fin:
            txt = random.choice(["a", "b ", "c", "de", " ", "\n"])
            t0 = time.perf_counter()
            await ws.send(json.dumps({"type": "update", "version": version,
                                      "updates": [{"clientID": cid, "changes": [doc_len, [0] + txt.split("\n")]}]}))
            while True:
                m = json.loads(await ws.recv())
                if m["type"] == "ack":
                    rtts.append((time.perf_counter() - t0) * 1000)
                    version = m["version"] + len(m["updates"]); doc_len += len(txt.encode("utf-16-le")) // 2
                    break
                if m["type"] == "reject":
                    errores.append(m.get("reason")); version = m.get("version", version)
                    if "doc" in m: doc_len = len(m["doc"].encode("utf-16-le")) // 2
                    break
            await asyncio.sleep(max(0, 1 / typing_hz - (time.perf_counter() - t0)))

async def sse(base, ck, seconds):
    # Un navegador mantiene /events abierto: se lee y se tira.
    loop = asyncio.get_running_loop()
    def leer():
        r = urllib.request.Request(base + "/events", headers={"Cookie": ck})
        fin = time.monotonic() + seconds
        try:
            with urllib.request.urlopen(r, timeout=seconds + 5) as f:
                while time.monotonic() < fin:
                    if not f.readline():
                        break
        except Exception:
            pass
    await loop.run_in_executor(None, leer)

async def patcher(req, pid_note, seconds, lat):
    loop = asyncio.get_running_loop(); fin = time.monotonic() + seconds; i = 0
    while time.monotonic() < fin:
        t0 = time.perf_counter()
        await loop.run_in_executor(None, req, "PATCH", f"/pages/{pid_note}", {"body_markdown": f"patch {i}"})
        lat.append((time.perf_counter() - t0) * 1000); i += 1
        await asyncio.sleep(1.0)

async def lector(req, seconds, lat):
    loop = asyncio.get_running_loop(); fin = time.monotonic() + seconds
    while time.monotonic() < fin:
        t0 = time.perf_counter()
        await loop.run_in_executor(None, req, "GET", "/pages/tree")
        lat.append((time.perf_counter() - t0) * 1000)
        await asyncio.sleep(2.0)

def pct(xs, p):
    xs = sorted(xs); return round(xs[min(len(xs) - 1, int(len(xs) * p))], 1) if xs else None

async def main():
    from concurrent.futures import ThreadPoolExecutor
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=64))
    ap = argparse.ArgumentParser(); ap.add_argument("base"); ap.add_argument("pid", type=int)
    ap.add_argument("--clients", type=int, default=10); ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--hz", type=float, default=5.0)
    a = ap.parse_args()
    admin, ajar = http_session(a.base)
    admin("POST", "/auth/login", {"username": "demo", "password": "demo-larga-123"})
    paginas = [p for p in admin("GET", "/pages")[1] if p["type"] == "note"]
    random.seed(5); elegidas = random.sample(paginas, a.clients + 1)
    sesiones = []
    for i in range(a.clients):
        admin("POST", "/auth/users", {"username": f"carga{i}", "display_name": f"Carga {i}", "password": f"contrasena-{i}"})
        req, jar = http_session(a.base); req("POST", "/auth/login", {"username": f"carga{i}", "password": f"contrasena-{i}"})
        sesiones.append((req, jar))
    rss0, t0 = proc_sample(a.pid); w0 = time.monotonic()
    muestras = []
    async def sampler():
        prev_t, prev_w = t0, w0
        while True:
            await asyncio.sleep(0.5)
            rss, t = proc_sample(a.pid); w = time.monotonic()
            muestras.append((rss, (t - prev_t) / os.sysconf("SC_CLK_TCK") / (w - prev_w) * 100)); prev_t, prev_w = t, w
    s = asyncio.create_task(sampler())
    rtts, errores, patch_lat, tree_lat = [], [], [], []
    tareas = [cliente(a.base, cookie(jar), elegidas[i]["id"], a.seconds, rtts, errores, a.hz) for i, (_, jar) in enumerate(sesiones)]
    tareas += [sse(a.base, cookie(jar), a.seconds) for _, jar in sesiones]
    tareas += [patcher(admin, elegidas[-1]["id"], a.seconds, patch_lat)]
    tareas += [lector(req, a.seconds, tree_lat) for req, _ in sesiones[:3]]
    await asyncio.gather(*tareas)
    await asyncio.sleep(1.5); s.cancel()
    rss1, _ = proc_sample(a.pid)
    st = admin("GET", "/status")[1].get("collab")
    cpus = [c for _, c in muestras]
    print(json.dumps({
        "clientes": a.clients, "segundos": a.seconds, "tecleo_hz": a.hz,
        "updates": len(rtts), "rechazos": len(errores),
        "rss_mb_antes": round(rss0 / 1024, 1), "rss_mb_max": round(max(r for r, _ in muestras) / 1024, 1),
        "rss_mb_despues": round(rss1 / 1024, 1),
        "cpu_pct_media": round(statistics.mean(cpus), 1), "cpu_pct_p95": pct(cpus, 0.95),
        "ack_ms_p50": pct(rtts, 0.5), "ack_ms_p95": pct(rtts, 0.95),
        "guardado_sala_ms": {"p50": st and st["persist_ms_p50"], "p95": st and st["persist_ms_p95"], "n": st and st["persists"]},
        "patch_ms_p50": pct(patch_lat, 0.5), "patch_ms_p95": pct(patch_lat, 0.95),
        "arbol_ms_p95": pct(tree_lat, 0.95),
    }, ensure_ascii=False, indent=1))

asyncio.run(main())
