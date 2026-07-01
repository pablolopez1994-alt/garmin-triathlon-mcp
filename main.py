import os
import json
import base64
import uuid
import threading
import queue as _queue
import smtplib
from email.mime.text import MIMEText
from datetime import date, timedelta

import anyio
import uvicorn
from garminconnect import Garmin
from mcp.server.fastmcp import FastMCP
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.requests import Request
from starlette.responses import HTMLResponse
from apscheduler.schedulers.background import BackgroundScheduler

PORT     = int(os.environ.get("PORT", 8000))
GARTH_DIR = "/tmp/garth_tokens"

mcp = FastMCP("Garmin Triatlón")

# ── Token persistence ─────────────────────────────────────────────────────────

def load_tokens_from_env() -> bool:
    tokens_b64 = os.environ.get("GARMIN_TOKENS")
    if not tokens_b64:
        return False
    try:
        os.makedirs(GARTH_DIR, exist_ok=True)
        data = json.loads(base64.b64decode(tokens_b64).decode())
        for fname, content in data.items():
            with open(os.path.join(GARTH_DIR, fname), "w") as f:
                f.write(content)
        return True
    except Exception as e:
        print(f"Error cargando tokens: {e}")
        return False

def export_tokens(client) -> str:
    import garth
    os.makedirs(GARTH_DIR, exist_ok=True)
    garth.save(GARTH_DIR)
    data = {}
    for fname in os.listdir(GARTH_DIR):
        fpath = os.path.join(GARTH_DIR, fname)
        if os.path.isfile(fpath):
            with open(fpath) as f:
                data[fname] = f.read()
    return base64.b64encode(json.dumps(data).encode()).decode()

def connect_garmin() -> Garmin:
    load_tokens_from_env()
    if os.path.exists(GARTH_DIR) and os.listdir(GARTH_DIR):
        try:
            client = Garmin()
            client.login(tokenstore=GARTH_DIR)
            return client
        except Exception:
            pass
    email    = os.environ.get("GARMIN_EMAIL", "")
    password = os.environ.get("GARMIN_PASSWORD", "")
    if not email or not password:
        raise ValueError("Sin credenciales. Visita /auth para autenticarte.")
    client = Garmin(email, password)
    client.login()
    return client

# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, m = s // 3600, (s % 3600) // 60
    return f"{h}h {m:02d}min" if h else f"{m}min"

def sport_label(type_key: str) -> str:
    return {
        "running": "Carrera", "trail_running": "Trail",
        "cycling": "Ciclismo", "road_biking": "Ciclismo carretera",
        "mountain_biking": "MTB", "swimming": "Natación",
        "open_water_swimming": "Natación aguas abiertas",
        "strength_training": "Fuerza", "transition": "Transición",
    }.get(type_key, type_key.replace("_", " ").title())

def format_activity(act: dict) -> str:
    type_key = act.get("activityType", {}).get("typeKey", "other")
    duration = act.get("duration", 0)
    distance = (act.get("distance") or 0) / 1000
    hr       = act.get("averageHR") or 0
    calories = act.get("calories") or 0
    start    = (act.get("startTimeLocal") or "")[:10]
    lines = [f"📅 {start} — {sport_label(type_key)}"]
    lines.append(f"   ⏱  {fmt_duration(duration)}")
    if distance > 0.05:
        lines.append(f"   📏 {distance:.2f} km")
        if "running" in type_key and duration > 0:
            p = duration / distance
            lines.append(f"   🏃 Ritmo: {int(p//60)}:{int(p%60):02d} /km")
        if "cycling" in type_key or "biking" in type_key:
            lines.append(f"   🚴 Velocidad: {distance/(duration/3600):.1f} km/h")
    if hr > 0:
        lines.append(f"   ❤️  FC media: {int(hr)} ppm")
    if calories > 0:
        lines.append(f"   🔥 {int(calories)} kcal")
    return "\n".join(lines)

# ── MCP tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
def get_weekly_summary() -> str:
    """Resumen de entrenamientos de los últimos 7 días desde Garmin Connect."""
    client = connect_garmin()
    end, start = date.today(), date.today() - timedelta(days=7)
    activities = client.get_activities_by_date(start.isoformat(), end.isoformat())
    if not activities:
        return "No hay actividades en los últimos 7 días."
    header = f"📊 RESUMEN SEMANAL  {start.strftime('%d/%m')} – {end.strftime('%d/%m/%Y')}\n"
    blocks = [format_activity(a) for a in activities]
    total_time = sum(a.get("duration", 0) for a in activities)
    total_km   = sum((a.get("distance") or 0) / 1000 for a in activities)
    total_kcal = sum((a.get("calories") or 0) for a in activities)
    footer = (
        f"\n──────────────────────────────\n"
        f"TOTAL  ⏱ {fmt_duration(total_time)}  |  📏 {total_km:.1f} km  |  🔥 {int(total_kcal)} kcal\n"
        f"Sesiones: {len(activities)}"
    )
    return header + "\n\n".join(blocks) + footer

@mcp.tool()
def get_recent_activities(n: int = 5) -> str:
    """Últimas N actividades de Garmin Connect (máx. 20)."""
    client = connect_garmin()
    activities = client.get_activities(0, max(1, min(n, 20)))
    if not activities:
        return "No se encontraron actividades."
    return f"🏋️  ÚLTIMAS {len(activities)} ACTIVIDADES\n\n" + "\n\n".join(format_activity(a) for a in activities)

@mcp.tool()
def get_training_load() -> str:
    """Carga de entrenamiento aguda/crónica y HRV si el dispositivo lo soporta."""
    client = connect_garmin()
    today = date.today().isoformat()
    lines = ["📈 ESTADO DE FORMA\n"]
    try:
        status = client.get_training_status(today)
        if status:
            b = status.get("trainingLoadBalance") or {}
            if b.get("shortTermTrainingLoad"):
                lines.append(f"Carga aguda  (7d):   {int(b['shortTermTrainingLoad'])}")
            if b.get("longTermTrainingLoad"):
                lines.append(f"Carga crónica (28d): {int(b['longTermTrainingLoad'])}")
    except Exception:
        pass
    try:
        hrv = client.get_hrv_data(today)
        if hrv and hrv.get("hrvSummary"):
            s = hrv["hrvSummary"]
            if s.get("lastNight"):
                lines.append(f"\nHRV anoche:        {s['lastNight']} ms")
            if s.get("weeklyAvg"):
                lines.append(f"HRV media semanal: {s['weeklyAvg']} ms")
    except Exception:
        pass
    return "\n".join(lines) if len(lines) > 1 else "Datos no disponibles para tu dispositivo."

# ── Auth web flow ─────────────────────────────────────────────────────────────

_sessions: dict = {}

class AuthSession:
    def __init__(self):
        self.mfa_queue    = _queue.Queue()
        self.result_queue = _queue.Queue()
        self.needs_mfa    = threading.Event()
        self.client       = None

def _run_login(email: str, password: str, sess: AuthSession):
    def get_mfa():
        print("[AUTH] Garmin pide MFA, esperando código del usuario...")
        sess.needs_mfa.set()
        code = sess.mfa_queue.get(timeout=300)
        print(f"[AUTH] Código MFA recibido, verificando con Garmin...")
        return code
    try:
        print(f"[AUTH] Iniciando login para {email}")
        client = Garmin(email, password)
        client.prompt_mfa = get_mfa
        client.login()
        print("[AUTH] Login exitoso")
        sess.client = client
        sess.result_queue.put(("ok", None))
    except Exception as e:
        import traceback
        print(f"[AUTH] Error completo:\n{traceback.format_exc()}")
        sess.result_queue.put(("err", str(e)))

CSS = """<style>
body{font-family:sans-serif;max-width:500px;margin:60px auto;padding:24px;background:#f4f4f4}
h2{color:#1a1a2e}
input{width:100%;padding:12px;margin:8px 0;box-sizing:border-box;border:1px solid #ccc;border-radius:6px;font-size:16px}
button{background:#0062cc;color:white;padding:14px;width:100%;border:none;border-radius:6px;font-size:16px;cursor:pointer;margin-top:8px}
.token{background:#fff;padding:14px;font-family:monospace;font-size:11px;word-break:break-all;
       border:1px solid #ddd;border-radius:6px;margin:12px 0;user-select:all}
.ok{background:#28a745}.card{background:#fff;padding:24px;border-radius:10px;box-shadow:0 2px 8px rgba(0,0,0,.1)}
</style>"""

async def auth_page(request: Request):
    return HTMLResponse(f"""<!DOCTYPE html><html><head><meta charset="utf-8">{CSS}</head><body>
<div class="card">
<h2>🏊🚴🏃 Conectar Garmin Connect</h2>
<p>Introduce tus credenciales de Garmin Connect. Solo necesitas hacer esto una vez.</p>
<form method="post" action="/auth/start">
  <input name="email" type="email" placeholder="Email de Garmin" required>
  <input name="password" type="password" placeholder="Contraseña" required>
  <button type="submit">Iniciar sesión</button>
</form>
</div></body></html>""")

async def auth_start(request: Request):
    import asyncio
    form     = await request.form()
    email    = str(form.get("email", ""))
    password = str(form.get("password", ""))
    sid      = str(uuid.uuid4())
    sess     = AuthSession()
    _sessions[sid] = sess

    threading.Thread(target=_run_login, args=(email, password, sess), daemon=True).start()

    for _ in range(600):  # hasta 60 segundos
        await asyncio.sleep(0.1)
        if sess.needs_mfa.is_set():
            return HTMLResponse(f"""<!DOCTYPE html><html><head><meta charset="utf-8">{CSS}</head><body>
<div class="card">
<h2>📱 Código de verificación</h2>
<p>Garmin te ha enviado un código al móvil. Introdúcelo aquí:</p>
<form method="post" action="/auth/mfa">
  <input type="hidden" name="sid" value="{sid}">
  <input name="code" placeholder="123456" autofocus
         style="font-size:32px;text-align:center;letter-spacing:10px">
  <button type="submit">Verificar</button>
</form>
</div></body></html>""")
        try:
            result, err = sess.result_queue.get_nowait()
            return _result_page(sess, result, err)
        except _queue.Empty:
            pass

    return HTMLResponse("<h2>Tiempo agotado</h2><a href='/auth'>Volver</a>")

async def auth_mfa(request: Request):
    form = await request.form()
    sid  = str(form.get("sid", ""))
    code = str(form.get("code", "")).strip()
    sess = _sessions.get(sid)
    if not sess:
        return HTMLResponse("<h2>Sesión no encontrada</h2><a href='/auth'>Volver</a>")
    print(f"[AUTH] Enviando código MFA a la cola: {code}")
    sess.mfa_queue.put(code)
    try:
        result, err = sess.result_queue.get(timeout=120)
    except _queue.Empty:
        print("[AUTH] Timeout esperando resultado de Garmin")
        return HTMLResponse("<h2>Tiempo agotado</h2><a href='/auth'>Volver</a>")
    print(f"[AUTH] Resultado: {result} / {err}")
    return _result_page(sess, result, err)

def _result_page(sess: AuthSession, result: str, err) -> HTMLResponse:
    if result == "ok":
        tokens = export_tokens(sess.client)
        return HTMLResponse(f"""<!DOCTYPE html><html><head><meta charset="utf-8">{CSS}</head><body>
<div class="card">
<h2>✅ ¡Conectado correctamente!</h2>
<p>Copia el token y guárdalo en Railway como variable <strong>GARMIN_TOKENS</strong>:</p>
<div class="token" id="t">{tokens}</div>
<button class="ok" onclick="navigator.clipboard.writeText(document.getElementById('t').innerText);this.innerText='✅ Copiado!'">
  Copiar token
</button>
<hr>
<h3>Pasos finales en Railway:</h3>
<ol>
  <li>Variables → <strong>+ New Variable</strong></li>
  <li>Nombre: <code>GARMIN_TOKENS</code> · Valor: el token copiado</li>
  <li>Railway se reinicia solo — ¡listo para siempre!</li>
</ol>
</div></body></html>""")
    return HTMLResponse(f"""<!DOCTYPE html><html><head><meta charset="utf-8">{CSS}</head><body>
<div class="card"><h2>❌ Error</h2><p>{err}</p>
<a href="/auth"><button>Volver a intentar</button></a>
</div></body></html>""")

# ── Email semanal ─────────────────────────────────────────────────────────────

def send_weekly_email():
    to   = os.environ.get("REPORT_EMAIL")
    user = os.environ.get("SMTP_USER")
    pwd  = os.environ.get("SMTP_PASS")
    if not all([to, user, pwd]):
        return
    try:
        body = get_weekly_summary()
        msg  = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = f"🏊🚴🏃 Análisis semanal – {date.today().strftime('%d/%m/%Y')}"
        msg["From"]    = user
        msg["To"]      = to
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(user, pwd)
            s.send_message(msg)
    except Exception as e:
        print(f"Error email: {e}")

# ── Entry point ────────────────────────────────────────────────────────────────

async def run():
    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request):
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await mcp._mcp_server.run(
                streams[0], streams[1],
                mcp._mcp_server.create_initialization_options(),
            )

    app = Starlette(routes=[
        Route("/sse",        endpoint=handle_sse),
        Mount("/messages/",  app=sse.handle_post_message),
        Route("/auth",       endpoint=auth_page),
        Route("/auth/start", endpoint=auth_start, methods=["POST"]),
        Route("/auth/mfa",   endpoint=auth_mfa,   methods=["POST"]),
    ])

    scheduler = BackgroundScheduler()
    scheduler.add_job(send_weekly_email, "cron", day_of_week="mon", hour=8, minute=0)
    scheduler.start()

    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info")
    await uvicorn.Server(config).serve()

if __name__ == "__main__":
    anyio.run(run)
