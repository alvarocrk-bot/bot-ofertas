#!/usr/bin/env python3
"""Bot de ofertas / errores de precio en perfumes originales (tiendas chilenas).

Solo usa la libreria estandar de Python. Pensado para GitHub Actions.
Secrets: SMTP_USER, SMTP_PASS, MAIL_TO

Cada tienda se autodetecta: VTEX, Shopify o WooCommerce. Lo que detecta se
guarda en plataformas_perfumes.json; las tiendas que no responden quedan
anotadas en el log y en el correo de resumen semanal.
"""
import json, os, re, smtplib, statistics, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

# ---------------------------------------------------------------- CONFIG
# Agrega o quita tiendas aqui (solo el dominio base).
TIENDAS = [
    "https://www.preunic.cl",
    "https://productosdelujo.cl",
    "https://www.sallybeauty.cl",
    "https://www.paris.cl",
    "https://www.ripley.cl",
    "https://vypstore.cl",
    "https://www.parfums.cl",
    "https://elreydelosperfumes.cl",
    "https://www.comprarenchile.cl",
    "https://www.chileperfume.cl",
    "https://www.lodoro.cl",
    "https://dreamsparfums.cl",
    "https://www.beautyperfumes.cl",
    "https://www.perfumisimo.cl",
    "https://cosmetic.cl",
    "https://www.nichostore.cl",
    "https://www.zonaperfumes.cl",
    "https://aromachil.cl",
    "https://perfumesprodigo.cl",
    "https://www.ohparfums.cl",
    "https://sairam.cl",
    "https://perfumeschic.cl",
    "https://www.eliteperfumes.cl",
    "https://yauras.cl",
]
BUSQUEDAS = [
    "perfume", "eau de parfum", "eau de toilette", "colonia hombre", "perfume mujer",
    "dior", "chanel", "carolina herrera", "paco rabanne", "versace", "hugo boss",
    "calvin klein", "armani", "lancome", "ysl", "jean paul gaultier", "givenchy",
]
MARCAS_OK = re.compile(
    r"dior|chanel|herrera|rabanne|versace|boss|klein|armani|lanc[oô]me|ysl|saint laurent|"
    r"gaultier|givenchy|burberry|tom ford|prada|dolce|azzaro|herm[eè]s|bvlgari|bulgari|cartier|"
    r"montblanc|mugler|issey|lacoste|nautica|davidoff|jimmy choo|valentino|narciso|"
    r"ralph lauren|marc jacobs|chlo[eé]|gucci|loewe|creed|byredo|maison margiela|"
    r"carolina|paco|jpg|kenzo|bentley|police|guess|adidas|ariana grande|britney|elizabeth arden|armaf|lattafa|afnan|rasasi|haramain|alhambra|marly|xerjoff|amouage|nishane|initio|kilian|le labo|diptyque|penhaligon|kurkdjian|montale|mancera|nasomatto|jo malone|acqua di parma|\bedp\b|\bedt\b|\bedc\b|parfum|perfume\b|eau de|cologne|colonia|extrait|elixir"
, re.I)
EXCLUIR = re.compile(r"\b(replica|réplica|inspirad[oa]|alternativ[oa]|similar|imitaci[oó]n|decant|"
                     r"muestra|miniatura|mini|tester|sin caja|usado|vac[ií]o|body mist|desodorante)\b", re.I)

# Una oferta "de gran impacto" cumple AL MENOS UNO:
DESC_MIN_PCT = 55          # % de descuento vs precio de lista
DESC_ERROR_PCT = 65        # % que huele a error de precio
AHORRO_MIN_CLP = 40000     # ahorro absoluto vs lista (con >=25% de descuento)
BAJA_HIST_PCT = 35         # % bajo la mediana historica del mismo producto
PRECIO_MIN_VALIDO = 10000

HIST_FILE = Path("historial_perfumes.json")
AVISADOS_FILE = Path("avisados_perfumes.json")
PLAT_FILE = Path("plataformas_perfumes.json")
UA = {"User-Agent": "Mozilla/5.0 (compatible; bot-ofertas/1.0)", "Accept": "application/json"}
# ---------------------------------------------------------------------


def get_json(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# ----------------------------------------------------------- adaptadores
def vtex(base, q):
    out = []
    for desde in range(0, 100, 50):
        data = get_json(f"{base}/api/catalog_system/pub/products/search?ft={urllib.parse.quote(q)}"
                        f"&_from={desde}&_to={desde + 49}")
        if not isinstance(data, list):
            raise ValueError("no VTEX")
        if not data:
            break
        for p in data:
            try:
                o = p["items"][0]["sellers"][0]["commertialOffer"]
                if not o.get("AvailableQuantity"):
                    continue
                out.append(dict(id=f"{base}|{p['productId']}", tienda=base, nombre=p["productName"],
                                marca=p.get("brand", ""), precio=num(o["Price"]),
                                lista=num(o.get("ListPrice")), url=p["link"]))
            except (KeyError, IndexError, TypeError):
                continue
        time.sleep(0.5)
    return out


_cache_catalogo = {}


def shopify(base, q):
    # Catalogo completo una sola vez por corrida; luego se filtra localmente.
    if base not in _cache_catalogo:
        items = []
        for page in range(1, 9):
            data = get_json(f"{base}/products.json?limit=250&page={page}")
            prods = data["products"]
            if not prods:
                break
            for p in prods:
                for v in p["variants"][:1]:
                    if v.get("available") is False:
                        continue
                    items.append(dict(id=f"{base}|{p['id']}", tienda=base, nombre=p["title"],
                                      marca=p.get("vendor", ""), precio=num(v["price"]),
                                      lista=num(v.get("compare_at_price")),
                                      url=f"{base}/products/{p['handle']}"))
            time.sleep(0.5)
        _cache_catalogo[base] = items
    return _cache_catalogo[base] if q == BUSQUEDAS[0] else []


def woo(base, q):
    out = []
    data = get_json(f"{base}/wp-json/wc/store/v1/products?search={urllib.parse.quote(q)}&per_page=100")
    if not isinstance(data, list):
        raise ValueError("no Woo")
    for p in data:
        pr = p["prices"]
        div = 10 ** int(pr.get("currency_minor_unit", 0))
        out.append(dict(id=f"{base}|{p['id']}", tienda=base, nombre=p["name"],
                        marca=" ".join(c["name"] for c in p.get("brands", []) or []) if p.get("brands") else "",
                        precio=num(pr["price"]) / div, lista=num(pr.get("regular_price")) / div,
                        url=p["permalink"]))
    return out


ADAPTADORES = {"vtex": vtex, "shopify": shopify, "woo": woo}


def detectar(base):
    for nombre, fn in ADAPTADORES.items():
        try:
            res = fn(base, BUSQUEDAS[0])
            if res:
                return nombre
        except Exception:
            continue
        finally:
            _cache_catalogo.pop(base, None)
    return None


def cargar(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def evaluar(p, hist):
    precio, lista = p["precio"], p["lista"]
    motivos = []
    if lista > precio:
        pct = (lista - precio) / lista * 100
        ahorro = lista - precio
        if pct >= DESC_ERROR_PCT:
            motivos.append(f"Posible ERROR DE PRECIO: {pct:.0f}% bajo precio de lista")
        elif pct >= DESC_MIN_PCT:
            motivos.append(f"{pct:.0f}% de descuento vs lista")
        if ahorro >= AHORRO_MIN_CLP and pct >= 25:
            motivos.append(clp(f"Ahorro de ${ahorro:,.0f}"))
    precios = hist.get(p["id"], [])
    if len(precios) >= 3:
        med = statistics.median(precios)
        if med > 0 and precio <= med * (1 - BAJA_HIST_PCT / 100):
            motivos.append(clp(f"{(1 - precio / med) * 100:.0f}% bajo su precio habitual (${med:,.0f})"))
    return motivos


def clp(s):
    return s.replace(",", ".")


def enviar(asunto, cuerpo):
    user, pw, to = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS"), os.environ.get("MAIL_TO")
    if not (user and pw and to):
        print("Faltan SMTP_USER / SMTP_PASS / MAIL_TO", file=sys.stderr)
        sys.exit(1)
    msg = MIMEText(cuerpo, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = asunto, user, to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.send_message(msg)


def correo_ofertas(ofertas):
    ofertas.sort(key=lambda o: -o["score"])
    lineas = []
    for o in ofertas[:25]:
        p = o["p"]
        lineas.append(clp(f"{p['nombre']} ({p['marca']}) - {p['tienda'].split('//')[-1]}\n"
                          f"  Precio: ${p['precio']:,.0f}  |  Lista: ${p['lista']:,.0f}\n")
                      + "  " + " · ".join(o["motivos"]) + f"\n  {p['url']}\n")
    cuerpo = ("Ofertas de perfumes detectadas:\n\n" + "\n".join(lineas) +
              "\nOjo: confirma que el vendedor sea la tienda y que el precio/stock siga vigente. "
              "Los errores de precio suelen anularse. Desconfia de precios muy bajos en tiendas chicas.")
    enviar(f"🔥 {len(ofertas)} oferta(s) de perfumes - mejor: {ofertas[0]['p']['nombre'][:50]}", cuerpo)


def main():
    hist = cargar(HIST_FILE, {})
    avisados = cargar(AVISADOS_FILE, {})
    plat = cargar(PLAT_FILE, {})
    productos, estado = {}, {}

    for base in TIENDAS:
        # redetecta si no hay plataforma guardada o si falto de datos la ultima vez
        if not plat.get(base):
            plat[base] = detectar(base)
        if not plat[base]:
            estado[base] = "sin API compatible (no VTEX/Shopify/Woo) o bloqueada"
            continue
        fn, n0 = ADAPTADORES[plat[base]], len(productos)
        for q in BUSQUEDAS:
            try:
                for p in fn(base, q):
                    productos[p["id"]] = p
            except Exception as e:
                print(f"[{plat[base]}] {base} '{q}': {e}", file=sys.stderr)
                break
            time.sleep(0.4)
        estado[base] = f"{plat[base]}: {len(productos) - n0} productos"
        if len(productos) == n0:
            plat[base] = None  # reintenta deteccion la proxima vez
    for b, s in estado.items():
        print(f"{b} -> {s}")
    print(f"{len(productos)} productos revisados")

    ofertas = []
    for p in productos.values():
        if p["precio"] < PRECIO_MIN_VALIDO:
            continue
        texto = f"{p['nombre']} {p['marca']}"
        if EXCLUIR.search(texto) or not MARCAS_OK.search(texto):
            continue
        motivos = evaluar(p, hist)
        if motivos:
            previo = avisados.get(p["id"])
            if previo is None or p["precio"] < previo * 0.9:
                desc = (p["lista"] - p["precio"]) / p["lista"] * 100 if p["lista"] > p["precio"] else 0
                ofertas.append(dict(p=p, motivos=motivos, score=len(motivos) * 10 + desc))
                avisados[p["id"]] = p["precio"]

    for p in productos.values():
        h = hist.setdefault(p["id"], [])
        h.append(p["precio"])
        del h[:-30]

    HIST_FILE.write_text(json.dumps(hist))
    AVISADOS_FILE.write_text(json.dumps(avisados))
    PLAT_FILE.write_text(json.dumps(plat, indent=1))

    if ofertas:
        correo_ofertas(ofertas)
        print(f"Correo enviado con {len(ofertas)} ofertas")
    else:
        print("Sin ofertas de impacto esta vez")

    # Resumen de estado de tiendas: lunes, primera corrida del dia (UTC)
    ahora = datetime.now(timezone.utc)
    if ahora.weekday() == 0 and ahora.hour < 3:
        enviar("Estado bot perfumes (semanal)",
               "Tiendas monitoreadas:\n\n" + "\n".join(f"{b}: {s}" for b, s in estado.items()))


if __name__ == "__main__":
    main()
