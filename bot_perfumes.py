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
    "https://www.sallybeauty.cl",
    "https://www.paris.cl",
    "https://www.ripley.cl",
    "https://vypstore.cl",
    "https://www.parfums.cl",
    "https://elreydelosperfumes.cl",
    "https://www.comprarenchile.cl",
    "https://www.chileperfume.cl",
    "https://www.lodoro.cl",
    "https://www.dreamsparfums.cl",
    "https://www.beautyperfumes.cl",
    "https://www.perfumisimo.cl",
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
    r"carolina|paco|jpg|kenzo|bentley|police|guess|adidas|ariana grande|britney|elizabeth arden", re.I)
EXCLUIR = re.compile(r"\b(replica|réplica|inspirad[oa]|alternativ[oa]|similar|imitaci[oó]n|decant|"
                     r"muestra|miniatura|mini|tester|sin caja|usado|vac[ií]o|body mist|desodorante)\b", re.I)

# Una oferta "de gran impacto" cumple AL MENOS UNO:
DESC_MIN_PCT = 45          # % de descuento vs precio de lista
DESC_ERROR_PCT = 65        # % que huele a error de precio
AHORRO_MIN_CLP = 40000     # ahorro absoluto vs lista (con >=25% de descuento)
BAJA_HIST_PCT = 35         # % bajo la mediana historica del mismo producto
PRECIO_MIN_VALIDO = 3000

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
                    items.append(d
