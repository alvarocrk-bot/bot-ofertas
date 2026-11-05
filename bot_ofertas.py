#!/usr/bin/env python3
"""Bot de ofertas / errores de tarifa para vuelos desde Chile."""
import os, sqlite3, smtplib, statistics, time
from datetime import date
from email.message import EmailMessage
import requests

TP_TOKEN = os.getenv("TRAVELPAYOUTS_TOKEN", "")
ORIGENES = os.getenv("ORIGENES", "SCL").split(",")
DESTINOS = os.getenv("DESTINOS", "LIM,MIA,MAD,GRU,EZE,CUN,PUQ,IQQ").split(",")
MONEDA = "clp"
MESES_ADELANTE = int(os.getenv("MESES_ADELANTE", "6"))

UMBRAL_PCT = float(os.getenv("UMBRAL_PCT", "0.60"))
MIN_HISTORIAL = int(os.getenv("MIN_HISTORIAL", "8"))
PRECIO_ABS = {
    # "SCL-MAD": 600000,
}

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
MAIL_TO = os.getenv("MAIL_TO", SMTP_USER)

CALLMEBOT_PHONE = os.getenv("CALLMEBOT_PHONE", "")
CALLMEBOT_KEY = os.getenv("CALLMEBOT_KEY", "")

DB = os.getenv("DB_PATH", "precios.db")


def fuente_travelpayouts(origen, destino, mes):
    r = requests.get(
        "https://api.travelpayouts.com/aviasales/v3/prices_for_dates",
        params={
            "origin": origen, "destination": destino,
            "departure_at": mes, "currency": MONEDA,
            "sorting": "price", "limit": 10, "token": TP_TOKEN,
        },
        timeout=30,
    )
    r.raise_for_status()
    for d in r.json().get("data", []):
        yield {
            "ruta": f"{origen}-{destino}",
            "fecha": d.get("departure_at", "")[:10],
            "precio": int(d["price"]),
            "aerolinea": d.get("airline", ""),
            "escalas": d.get("transfers", 0),
            "link": "https://www.aviasales.com" + d.get("link", ""),
        }


def abrir_db():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS precios(ruta TEXT, fecha TEXT, precio INTEGER, ts REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS avisados(ruta TEXT, fecha TEXT, precio INTEGER, ts REAL)")
    return con


def historial(con, ruta):
    return [r[0] for r in con.execute("SELECT precio FROM precios WHERE ruta=?", (ruta,))]


def ya_avisado(con, o):
    return con.execute(
        "SELECT 1 FROM avisados WHERE ruta=? AND fecha=? AND precio<=?",
        (o["ruta"], o["fecha"], o["precio"])).fetchone() is not None


def evaluar(con, o):
    h = historial(con, o["ruta"])
    tope = PRECIO_ABS.get(o["ruta"])
    if tope and o["precio"] <= tope:
        return f"bajo tu tope de ${tope:,}".replace(",", ".")
    if len(h) >= MIN_HISTORIAL:
        med = statistics.median(h)
        if o["precio"] <= med * UMBRAL_PCT:
            pct = round((1 - o["precio"] / med) * 100)
            return f"{pct}% bajo la mediana (${int(med):,})".replace(",", ".")
    return None


def formatear(alertas):
    lineas = ["Ofertas / posibles errores de tarifa:\n"]
    for o, motivo in alertas:
        lineas.append(
            f"{o['ruta']} {o['fecha']} | ${o['precio']:,} CLP | "
            f"{o['aerolinea']} | {o['escalas']} escalas\n  {motivo}\n  {o['link']}\n"
            .replace(",", "."))
    return "\n".join(lineas)


def enviar_correo(texto):
    if not (SMTP_USER and SMTP_PASS):
        return
    m = EmailMessage()
    m["Subject"], m["From"], m["To"] = "Alerta de pasajes baratos", SMTP_USER, MAIL_TO
    m.set_content(texto)
    with smtplib.SMTP_SSL(SMTP_HOST, 465) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(m)


def enviar_whatsapp(texto):
    if CALLMEBOT_PHONE and CALLMEBOT_KEY:
        requests.get("https://api.callmebot.com/whatsapp.php", timeout=30, params={
            "phone": CALLMEBOT_PHONE, "apikey": CALLMEBOT_KEY, "text": texto[:1500]})


def meses():
    hoy = date.today().replace(day=1)
    for i in range(MESES_ADELANTE):
        y, m = divmod(hoy.month - 1 + i, 12)
        yield f"{hoy.year + y}-{m + 1:02d}"
def main():
    con = abrir_db()
    alertas = []
    for o_ in ORIGENES:
        for d_ in DESTINOS:
            for mes in meses():
                try:
                    ofertas = list(fuente_travelpayouts(o_, d_, mes))
                except Exception as e:
                    print("error", o_, d_, mes, e)
                    continue
                for o in ofertas:
                    motivo = evaluar(con, o)
                    if motivo and not ya_avisado(con, o):
                        alertas.append((o, motivo))
                        con.execute("INSERT INTO avisados VALUES(?,?,?,?)",
                                    (o["ruta"], o["fecha"], o["precio"], time.time()))
                    con.execute("INSERT INTO precios VALUES(?,?,?,?)",
                                (o["ruta"], o["fecha"], o["precio"], time.time()))
                con.commit()
                time.sleep(0.5)
    if alertas:
        alertas.sort(key=lambda x: x[0]["precio"])
        texto = formatear(alertas[:15])
        enviar_correo(texto)
        enviar_whatsapp(texto)
        print(texto)
    else:
        print("Sin ofertas nuevas.")


if __name__ == "__main__":
    main()


