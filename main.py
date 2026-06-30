import os
import smtplib
from email.mime.text import MIMEText
from datetime import date, timedelta
from garminconnect import Garmin
from mcp.server.fastmcp import FastMCP
from apscheduler.schedulers.background import BackgroundScheduler

mcp = FastMCP("Garmin Triatlón")

# ── helpers ──────────────────────────────────────────────────────────────────

def connect_garmin() -> Garmin:
    client = Garmin(os.environ["GARMIN_EMAIL"], os.environ["GARMIN_PASSWORD"])
    client.login()
    return client

def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, m = s // 3600, (s % 3600) // 60
    return f"{h}h {m:02d}min" if h else f"{m}min"

def sport_label(type_key: str) -> str:
    labels = {
        "running": "Carrera",
        "trail_running": "Trail",
        "cycling": "Ciclismo",
        "road_biking": "Ciclismo carretera",
        "mountain_biking": "MTB",
        "swimming": "Natación",
        "open_water_swimming": "Natación aguas abiertas",
        "strength_training": "Fuerza",
        "transition": "Transición",
    }
    return labels.get(type_key, type_key.replace("_", " ").title())

def format_activity(act: dict) -> str:
    type_key  = act.get("activityType", {}).get("typeKey", "other")
    duration  = act.get("duration", 0)
    distance  = (act.get("distance", 0) or 0) / 1000  # m → km
    hr        = act.get("averageHR") or 0
    calories  = act.get("calories") or 0
    start     = (act.get("startTimeLocal") or "")[:10]

    lines = [f"📅 {start} — {sport_label(type_key)}"]
    lines.append(f"   ⏱  {fmt_duration(duration)}")
    if distance > 0.05:
        lines.append(f"   📏 {distance:.2f} km")
        if "running" in type_key and duration > 0:
            pace = duration / distance
            lines.append(f"   🏃 Ritmo: {int(pace//60)}:{int(pace%60):02d} /km")
        if "cycling" in type_key or "biking" in type_key:
            speed = distance / (duration / 3600)
            lines.append(f"   🚴 Velocidad media: {speed:.1f} km/h")
    if hr > 0:
        lines.append(f"   ❤️  FC media: {int(hr)} ppm")
    if calories > 0:
        lines.append(f"   🔥 {int(calories)} kcal")
    return "\n".join(lines)

# ── MCP tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
def get_weekly_summary() -> str:
    """Devuelve el resumen de entrenamientos de los últimos 7 días desde Garmin Connect."""
    client = connect_garmin()
    end   = date.today()
    start = end - timedelta(days=7)
    activities = client.get_activities_by_date(start.isoformat(), end.isoformat())

    if not activities:
        return "No hay actividades en los últimos 7 días."

    header = f"📊 RESUMEN SEMANAL  {start.strftime('%d/%m')} – {end.strftime('%d/%m/%Y')}\n"
    blocks = [format_activity(a) for a in activities]

    # totals
    total_time = sum(a.get("duration", 0) for a in activities)
    total_km   = sum((a.get("distance", 0) or 0) / 1000 for a in activities)
    total_kcal = sum((a.get("calories", 0) or 0) for a in activities)

    footer = (
        f"\n──────────────────────────────\n"
        f"TOTAL  ⏱ {fmt_duration(total_time)}  |  📏 {total_km:.1f} km  |  🔥 {int(total_kcal)} kcal\n"
        f"Sesiones: {len(activities)}"
    )
    return header + "\n\n".join(blocks) + footer


@mcp.tool()
def get_recent_activities(n: int = 5) -> str:
    """Devuelve las últimas N actividades registradas en Garmin Connect (por defecto 5)."""
    client     = connect_garmin()
    activities = client.get_activities(0, max(1, min(n, 20)))

    if not activities:
        return "No se encontraron actividades recientes."

    header = f"🏋️  ÚLTIMAS {len(activities)} ACTIVIDADES\n"
    blocks  = [format_activity(a) for a in activities]
    return header + "\n\n".join(blocks)


@mcp.tool()
def get_training_load() -> str:
    """Devuelve la carga de entrenamiento aguda/crónica y el estado HRV si el dispositivo lo soporta."""
    client = connect_garmin()
    today  = date.today().isoformat()
    lines  = ["📈 ESTADO DE FORMA\n"]

    try:
        status = client.get_training_status(today)
        if status:
            balance = status.get("trainingLoadBalance") or {}
            acute   = balance.get("shortTermTrainingLoad")
            chronic = balance.get("longTermTrainingLoad")
            ratio   = balance.get("trainingLoadBalanceValue")
            if acute:
                lines.append(f"Carga aguda  (7 días):  {int(acute)}")
            if chronic:
                lines.append(f"Carga crónica (28 días): {int(chronic)}")
            if ratio:
                lines.append(f"Ratio ATL/CTL: {ratio:.2f}")
    except Exception:
        pass

    try:
        hrv = client.get_hrv_data(today)
        if hrv and hrv.get("hrvSummary"):
            weekly_avg = hrv["hrvSummary"].get("weeklyAvg")
            last_night = hrv["hrvSummary"].get("lastNight")
            if last_night:
                lines.append(f"\nHRV anoche:      {last_night} ms")
            if weekly_avg:
                lines.append(f"HRV media semanal: {weekly_avg} ms")
    except Exception:
        pass

    if len(lines) == 1:
        return "Datos de carga no disponibles (puede depender del modelo de tu Garmin)."
    return "\n".join(lines)


# ── Email semanal automático ───────────────────────────────────────────────────

def send_weekly_email():
    recipient = os.environ.get("REPORT_EMAIL")
    smtp_user  = os.environ.get("SMTP_USER")
    smtp_pass  = os.environ.get("SMTP_PASS")

    if not all([recipient, smtp_user, smtp_pass]):
        print("[Scheduler] Variables de email no configuradas, se omite el envío.")
        return

    try:
        body = get_weekly_summary()
        msg  = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = f"🏊🚴🏃 Análisis semanal triatlón – {date.today().strftime('%d/%m/%Y')}"
        msg["From"]    = smtp_user
        msg["To"]      = recipient

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)

        print(f"[Scheduler] Email enviado a {recipient}")
    except Exception as e:
        print(f"[Scheduler] Error enviando email: {e}")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Programar email cada lunes a las 8:00
    scheduler = BackgroundScheduler()
    scheduler.add_job(send_weekly_email, "cron", day_of_week="mon", hour=8, minute=0)
    scheduler.start()

    port = int(os.environ.get("PORT", 8000))
    mcp.run(transport="sse", host="0.0.0.0", port=port)
