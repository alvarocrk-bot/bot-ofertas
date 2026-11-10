#!/usr/bin/env python3
"""Bot de ofertas / errores de precio en perfumes originales (tiendas chilenas). v2

Solo usa la libreria estandar de Python. Pensado para GitHub Actions.
Secrets: SMTP_USER, SMTP_PASS, MAIL_TO

Lectores (se autodetectan por tienda): VTEX, Shopify, WooCommerce y,
si ninguno funciona, un lector alternativo que sigue el sitemap.xml de la
tienda y lee el precio de cada producto desde su pagina (formato JSON-LD).
El lector alternativo revisa un lote de paginas por corrida y va rotando.

Las tiendas sin acceso se anotan y se reintentan cada REINTENTO_DIAS dias.
"""
import json, os, re, smtplib, statistics, sys, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, date
from email.mime.text import MIMEText
from pathlib import Path

# ---------------------------------------------------------------- CONFIG
# Agrega o quita tiendas aqui (solo el dominio base, sin la parte despues del .cl)
TIENDAS = [
    # --- las que ya se leen bien ---
    "https://productosdelujo.cl",
    "https://www.comprarenchile.cl",
    "https://www.chileperfume.cl",
    "https://www.lodoro.cl",
    "https://www.beautyperfumes.cl",
    "https://www.perfumisimo.cl",
    "https://cosmetic.cl",
    "https://www.nichostore.cl",
    "https://www.zonaperfumes.cl",
    "https://perfumeschic.cl",
    "https://www.eliteperfumes.cl",
    "https://maisonniche.cl",
    "https://santiagoperfumes.cl",
    "https://perfumesneroli.cl",
    "https://misticaperfumes.cl",
    "https://dperfumes.cl",
    "https://saideep.cl",
    "https://mperfumes.cl",
    "https://www.ofertaperfumes.cl",
    # --- las que antes fallaban: ahora prueban el lector alternativo ---
    "https://aromachil.cl",
    "https://perfumesprodigo.cl",
    "https://www.ohparfums.cl",
    "https://sairam.cl",
    "https://www.cruzyvalencia.cl",
    "https://lacasadelperfume.cl",
    "https://joyperfumes.cl",
    "https://laperfumeriaonline.cl",
    "https://vypstore.cl",
    "https://www.parfums.cl",
    "https://elreydelosperfumes.cl",
    "https://dreamsparfums.cl",
    # --- tiendas grandes (lo mas probable es que bloqueen al bot) ---
    "https://www.paris.cl",
    "https://www.ripley.cl",
    "https://www.preunic.cl",
    "https://www.sallybeauty.cl",
]
BUSQUEDAS = [
    "perfume", "eau de parfum", "eau de toilette", "colonia hombre", "perfume mujer",
    "dior", "chanel", "carolina herrera", "paco rabanne", "versace", "hugo boss",
    "calvin klein", "armani", "lancome", "ysl", "jean paul gaultier", "givenchy",
]
# Lo que SI deja pasar: marcas conocidas (diseñador y nicho) y cualquier producto
# que se vea como perfume (EDP, EDT, parfum, extrait...). Cremas, labiales y
# maquillaje no pasan.
MARCAS_OK = re.compile(
    r"dior|chanel|herrera|rabanne|versace|boss|klein|armani|lanc[oô]me|ysl|saint laurent|"
    r"gaultier|givenchy|burberry|tom ford|prada|dolce|azzaro|herm[eè]s|bvlgari|bulgari|cartier|"
    r"montblanc|mugler|issey|lacoste|nautica|davidoff|jimmy choo|valentino|narciso|"
    r"ralph lauren|marc jacobs|chlo[eé]|gucci|loewe|creed|byredo|maison margiela|"
    r"carolina|paco|jpg|kenzo|bentley|police|guess|adidas|ariana grande|britney|elizabeth arden|"
    r"marly|xerjoff|amouage|nishane|initio|kilian|le labo|diptyque|penhaligon|kurkdjian|"
    r"montale|mancera|nasomatto|jo malone|acqua di parma|frederic malle|maison francis|"
    r"orto parisi|tiziana terenzi|ex nihilo|clive christian|"
    r"\bedp\b|\bedt\b|\bedc\b|parfum|perfume\b|eau de|cologne|colonia|extrait|elixir", re.I)
# Lo que NO deja pasar (se revisa primero y manda sobre lo anterior):
EXCLUIR = re.compile(
    r"\b(replica|réplica|inspirad[oa]|alternativ[oa]|similar|imitaci[oó]n|decant|"
    r"muestra|miniatura|mini|tester|sin caja|usado|vac[ií]o|body mist|desodorante|"
    r"lattafa|armaf|afnan|rasasi|haramain|alhambra|fragrance world|tubbees|grandeur|"
    r"ajmal|swiss arabian|paris corner|khadlaj|zimaya|french avenue|orientica|"
    r"ard al zaafaran)\b", re.I)

# Una oferta "de gran impacto" cumple AL MENOS UNO:
DESC_MIN_PCT = 55          # % de descuento vs precio de lista
DESC_ERROR_PCT = 65        # % que huele a error de precio
AHORRO_MIN_CLP = 40000     # ahorro absoluto vs lista (con >=25% de descuento)
BAJA_HIST_PCT = 35         # % bajo la mediana historica del mismo producto
PRECIO_MIN_VALIDO = 10000  # ignora productos de menos de este precio
MAX_OFERTAS_CORREO = 40    # maximo de ofertas por correo
HIST_LARGO = 60            # cuantos precios guarda por producto

# Lector alternativo (sitemap)
PAGINAS_POR_CORRIDA = 40  # paginas de producto que lee por tienda en cada corrida
HILOS = 4                  # peticiones en paralelo por tienda
REINTENTO_DIAS = 7         # cada cuantos dias reintenta una tienda sin acceso
DEADLINE_SEG = 6 * 60     # corta la corrida a los 14 min (el limite del workflow es 20)

HIST_FILE = Path("historial_perfumes.json")
AVISADOS_FILE = Path("avisados_perfumes.json")
PLAT_FILE = Path("plataformas_perfumes.json")
UA = {"User-Agent": "Mozilla/5.0 (compatible; bot-ofertas/1.0)", "Accept": "application/json"}
UA_HTML = {"User-Agent": "Mozilla/5.0 (compatible; bot-ofertas/1.0)",
           "Accept": "text/html,application/xml;q=0.9,*/*;q=0.8"}
# ---------------------------------------------------------------------

_OFFSETS = {}
_T0 = [time.time()]


def get_json(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def get_text(url, timeout=15, max_bytes=8_000_000):
    req = urllib.request.Request(url, headers=UA_HTML)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(max_bytes).decode("utf-8", "replace")


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def precio_num(x):
    """Convierte '69.990', '69990.00', 69990 -> 69990.0 (pesos chilenos)."""
    if x is None:
        return 0.0
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().replace("$", "").replace(" ", "")
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", s):
        return float(re.sub(r"[.,]", "", s))
    if re.fullmatch(r"\d+[.,]\d{1,2}", s):
        return float(s.replace(",", "."))
    if re.fullmatch(r"\d+", s):
        return float(s)
    return 0.0


def se_acabo_el_tiempo():
    return time.time() - _T0[0] > DEADLINE_SEG


# ----------------------------------------------------------- adaptadores
def vtex(base, q):
    out = []
    for desde in range(0, 100, 50):
        data = get_json(f"{base}/api/catalog_system/pub/products/search?ft={urllib.parse.quote(q)}"
                        f"&_from={desde}&_to={desde + 49}", timeout=15)
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
            data = get_json(f"{base}/products.json?limit=250&page={page}", timeout=20)
            prods = data["products"]
            if not prods:
                break
            for p in prods:
                # usa la variante mas cara disponible (el frasco grande), no la primera,
                # que a veces es una muestra o un formato chico
                vs = [v for v in p.get("variants", []) if v.get("available") is not False]
                if not vs:
                    continue
                v = max(vs, key=lambda v: num(v.get("price")))
                items.append(dict(id=f"{base}|{p['id']}", tienda=base, nombre=p["title"],
                                  marca=p.get("vendor", ""), precio=num(v["price"]),
                                  lista=num(v.get("compare_at_price")),
                                  url=f"{base}/products/{p['handle']}"))
            time.sleep(0.5)
        _cache_catalogo[base] = items
    return _cache_catalogo[base] if q == BUSQUEDAS[0] else []


def woo(base, q):
    out = []
    data = get_json(f"{base}/wp-json/wc/store/v1/products?search={urllib.parse.quote(q)}&per_page=100",
                    timeout=15)
    if not isinstance(data, list):
        raise ValueError("no Woo")
    for p in data:
        pr = p["prices"]
        if p.get("is_in_stock") is False or p.get("stock_status") in ("outofstock", "out_of_stock"):
            continue
        div = 10 ** int(pr.get("currency_minor_unit", 0))
        out.append(dict(id=f"{base}|{p['id']}", tienda=base, nombre=p["name"],
                        marca=" ".join(c["name"] for c in p.get("brands", []) or []) if p.get("brands") else "",
                        precio=num(pr["price"]) / div, lista=num(pr.get("regular_price")) / div,
                        url=p["permalink"]))
    return out


# ------------------------------------------------- lector alternativo (sitemap)
LD_RE = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)
META_LISTA_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:product:original_price:amount|og:price:standard_amount|'
    r'product:price:standard_amount)["\'][^>]+content=["\']([^"\']+)', re.I)
META_PRECIO_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:product:price:amount|og:price:amount)["\'][^>]+content=["\']([^"\']+)', re.I)
META_TITULO_RE = re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', re.I)
SITEMAPS_CANDIDATOS = ["/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml", "/product-sitemap.xml",
                       "/sitemap_products_1.xml"]


def _locs(xml):
    return re.findall(r"<loc>\s*(?:<!\[CDATA\[)?\s*([^<\s\]]+)", xml)


def urls_sitemap(base):
    """Devuelve las URLs de producto que parecen perfumes, sin repetir."""
    host = urllib.parse.urlparse(base).netloc.replace("www.", "")
    hojas, pendientes, visitados = [], [], 0
    for cand in SITEMAPS_CANDIDATOS:
        try:
            xml = get_text(base + cand)
        except Exception:
            continue
        if "<loc>" in xml:
            pendientes = [(base + cand, xml)]
            break
    cola = list(pendientes)
    while cola and visitados < 8:
        _, xml = cola.pop(0)
        visitados += 1
        locs = _locs(xml)
        if "<sitemapindex" in xml:
            prod = [u for u in locs if re.search(r"product|producto|catalog", u, re.I)]
            for u in (prod or locs)[:5]:
                try:
                    cola.append((u, get_text(u)))
                except Exception:
                    continue
        else:
            hojas.extend(locs)
    out, vistos = [], set()
    for u in hojas:
        if u in vistos or re.search(r"\.(jpg|jpeg|png|webp|gif|pdf)(\?|$)", u, re.I):
            continue
        pu = urllib.parse.urlparse(u)
        if host not in pu.netloc.replace("www.", ""):
            continue
        slug = urllib.parse.unquote(pu.path).replace("-", " ").replace("/", " ").replace("_", " ")
        if EXCLUIR.search(slug) or not MARCAS_OK.search(slug):
            continue
        vistos.add(u)
        out.append(u)
    return out


def _productos_ld(html):
    out = []
    for blk in LD_RE.findall(html):
        try:
            data = json.loads(blk.strip())
        except Exception:
            continue
        pila = [data]
        while pila:
            d = pila.pop()
            if isinstance(d, list):
                pila.extend(d)
                continue
            if not isinstance(d, dict):
                continue
            if isinstance(d.get("@graph"), list):
                pila.extend(d["@graph"])
            t = d.get("@type")
            tipos = t if isinstance(t, list) else [t]
            if "Product" in tipos:
                out.append(d)
            if "ProductGroup" in tipos and isinstance(d.get("hasVariant"), list):
                pila.extend(d["hasVariant"])
    return out


def _oferta_ld(d):
    """(precio, lista) del producto JSON-LD, tomando la oferta mas cara con stock."""
    ofs = d.get("offers")
    if isinstance(ofs, dict):
        ofs = [ofs]
    if not isinstance(ofs, list):
        return 0.0, 0.0
    mejor = (0.0, 0.0)
    for o in ofs:
        if not isinstance(o, dict) or "OutOfStock" in str(o.get("availability", "")):
            continue
        precio = precio_num(o.get("price")) or precio_num(o.get("highPrice")) or precio_num(o.get("lowPrice"))
        lista = 0.0
        ps = o.get("priceSpecification")
        for s in (ps if isinstance(ps, list) else [ps] if isinstance(ps, dict) else []):
            if re.search(r"List|Strikethrough|Regular|MSRP", f"{s.get('priceType', '')} {s.get('name', '')}", re.I):
                lista = max(lista, precio_num(s.get("price")))
        if precio > mejor[0]:
            mejor = (precio, lista)
    return mejor


def leer_pagina(base, url):
    try:
        html = get_text(url)
    except Exception:
        return None
    for d in _productos_ld(html):
        precio, lista = _oferta_ld(d)
        if precio <= 0:
            continue
        if not lista:
            m = META_LISTA_RE.search(html)
            lista = precio_num(m.group(1)) if m else 0.0
        marca = d.get("brand")
        marca = marca.get("name", "") if isinstance(marca, dict) else (marca or "")
        return dict(id=f"{base}|{url}", tienda=base, nombre=str(d.get("name", "")), marca=str(marca),
                    precio=precio, lista=lista, url=url)
    # sin JSON-LD: intenta con las etiquetas meta de la pagina
    mp, mt = META_PRECIO_RE.search(html), META_TITULO_RE.search(html)
    if mp and mt:
        ml = META_LISTA_RE.search(html)
        return dict(id=f"{base}|{url}", tienda=base, nombre=mt.group(1), marca="",
                    precio=precio_num(mp.group(1)), lista=precio_num(ml.group(1)) if ml else 0.0, url=url)
    return None


def sitemap(base, q):
    if q != BUSQUEDAS[0]:
        return []
    urls = urls_sitemap(base)
    if not urls:
        return []
    ini = _OFFSETS.get(base, 0) % len(urls)
    lote = (urls[ini:] + urls[:ini])[:PAGINAS_POR_CORRIDA]
    _OFFSETS[base] = (ini + len(lote)) % len(urls)
    out = []
    with ThreadPoolExecutor(max_workers=HILOS) as ex:
        for p in ex.map(lambda u: None if se_acabo_el_tiempo() else leer_pagina(base, u), lote):
            if p:
                out.append(p)
    return out


ADAPTADORES = {"vtex": vtex, "shopify": shopify, "woo": woo, "sitemap": sitemap}


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


def clp(s):
    return s.replace(",", ".")


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
    for o in ofertas[:MAX_OFERTAS_CORREO]:
        p = o["p"]
        lineas.append(clp(f"{p['nombre']} ({p['marca']}) - {p['tienda'].split('//')[-1]}\n"
                          f"  Precio: ${p['precio']:,.0f}  |  Lista: ${p['lista']:,.0f}\n")
                      + "  " + " · ".join(o["motivos"]) + f"\n  {p['url']}\n")
    cuerpo = ("Ofertas de perfumes detectadas:\n\n" + "\n".join(lineas) +
              "\nOjo: confirma que el vendedor sea la tienda y que el precio/stock siga vigente. "
              "Los errores de precio suelen anularse. Desconfia de precios muy bajos en tiendas chicas, "
              "y revisa que el precio corresponda al frasco/tamaño que quieres.")
    enviar(f"🔥 {len(ofertas)} oferta(s) de perfumes - mejor: {ofertas[0]['p']['nombre'][:50]}", cuerpo)


def main():
    _T0[0] = time.time()
    hoy = date.today()
    hist = cargar(HIST_FILE, {})
    avisados = cargar(AVISADOS_FILE, {})
    plat = cargar(PLAT_FILE, {})
    if not isinstance(plat, dict):
        plat = {}
    fallos = plat.setdefault("_fallos", {})
    _OFFSETS.clear()
    _OFFSETS.update(plat.get("_offsets", {}))
    productos, estado = {}, {}

    for base in TIENDAS:
        if se_acabo_el_tiempo():
            estado[base] = "omitida: se acabo el tiempo de esta corrida"
            continue
        if not plat.get(base):
            f = fallos.get(base)
            if f:
                dias = (hoy - date.fromisoformat(f)).days
                if dias < REINTENTO_DIAS:
                    estado[base] = f"sin acceso (reintenta en {REINTENTO_DIAS - dias} dias)"
                    continue
            plat[base] = detectar(base)
            if not plat[base]:
                fallos[base] = hoy.isoformat()
                estado[base] = "sin acceso: no responde, la bloquea o no se puede leer"
                continue
            fallos.pop(base, None)
        fn, n0 = ADAPTADORES[plat[base]], len(productos)
        consultas = BUSQUEDAS[:1] if plat[base] in ("shopify", "sitemap") else BUSQUEDAS
        for q in consultas:
            if se_acabo_el_tiempo():
                break
            try:
                for p in fn(base, q):
                    productos[p["id"]] = p
            except Exception as e:
                print(f"[{plat[base]}] {base} '{q}': {e}", file=sys.stderr)
                break
            time.sleep(0.4)
        nuevos = len(productos) - n0
        estado[base] = (f"{plat[base]}: {nuevos} productos" if plat[base] != "sitemap"
                        else f"sitemap: {nuevos} paginas leidas en este lote (rota entre corridas)")
        if nuevos == 0:
            plat[base] = None  # reintenta deteccion la proxima vez
    for b, s in estado.items():
        print(f"{b} -> {s}")
    print(f"{len(productos)} productos revisados en {time.time() - _T0[0]:.0f} s")

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
        del h[:-HIST_LARGO]

    plat["_offsets"] = dict(_OFFSETS)
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
