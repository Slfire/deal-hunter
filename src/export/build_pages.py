"""
Deal Hunter — une page HTML statique par jeu, plus sitemap.xml et robots.txt.

Pourquoi : le site principal est une seule page remplie en JavaScript, invisible pour Google
et sans aperçu quand on partage un lien. Une page par jeu, en HTML pur, règle les deux :
  - indexable par les moteurs de recherche (titre, description, données structurées)
  - aperçu riche sur Discord, WhatsApp, X... (balises Open Graph)

Entrées : site/data/deals.json, site/data/history/<id>.json, site/data/search/similar.json
Sorties : site/jeu/<id>-<slug>.html, site/sitemap.xml, site/robots.txt

À lancer APRÈS export_site_data.py et embed_games.py.

Usage :
  python src/export/build_pages.py
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from datetime import date
from html import escape
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SITE_DIR = PROJECT_ROOT / "site"
DATA_DIR = SITE_DIR / "data"
OUT_DIR = SITE_DIR / "jeu"
SITE_URL = os.getenv("SITE_URL", "https://slfire.github.io/deal-hunter/").rstrip("/") + "/"

MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
          "septembre", "octobre", "novembre", "décembre"]

log = logging.getLogger("pages")


def usd(v: float) -> str:
    return f"{v:,.2f}".replace(",", " ").replace(".", ",") + " $"


def eur(v: float) -> str:
    return f"{v:,.2f}".replace(",", " ").replace(".", ",") + " €"


def month_year(iso: str | None) -> str:
    if not iso:
        return ""
    d = date.fromisoformat(iso[:10])
    return f"{MONTHS[d.month - 1]} {d.year}"


def page_path(g: dict) -> str:
    return f"jeu/{g['id']}-{g['slug']}.html"


def verdict(g: dict) -> tuple[str, str]:
    """Même logique que la fiche du site principal. Renvoie (titre, explication)."""
    low, best = g.get("lowest_ever"), g["best_price"]
    if low is None or (not g.get("full_history") and (g.get("days_tracked") or 0) < 7):
        return "Historique en construction", "On suit ce jeu depuis peu : il faut quelques jours de plus pour juger cette promo."
    scope = "de son histoire" if g.get("full_history") else f"de nos {g['days_tracked']} jours de relevés"
    if best <= low:
        return "Bon moment pour acheter", f"C'est le prix le plus bas {scope}."
    when = month_year(g.get("lowest_ever_date"))
    if low <= 0:   # jeu déjà offert (Epic, Prime Gaming…) : pas d'écart en % possible
        return ("Déjà offert gratuitement",
                f"Ce jeu a déjà été donné gratuitement{' en ' + when if when else ''}. Ça peut se reproduire, "
                f"mais sans garantie : à {usd(best)}, c'est à toi de voir.")
    if (best - low) / low <= 0.10:
        return "Bon prix", f"Très proche du plus bas {scope} ({usd(low)}{', en ' + when if when else ''})."
    pct = round((best - low) / best * 100)
    return "Tu peux attendre", f"Ce jeu est déjà descendu à {usd(low)}{' en ' + when if when else ''}, soit {pct} % de moins qu'aujourd'hui."


def chart_svg(points: list[dict], normal: float) -> str:
    by_day: dict[str, float] = {}
    for p in points:
        by_day[p["d"]] = min(by_day.get(p["d"], float("inf")), p["p"])
    days = sorted(by_day)
    if len(days) < 2:
        return '<p class="note">La courbe des prix apparaîtra après quelques jours de relevés.</p>'
    vals = [by_day[d] for d in days]
    W, H, L, R, T, B = 700, 200, 50, 10, 10, 28
    top = max(normal, *vals) * 1.05
    x = lambda i: L + i / (len(days) - 1) * (W - L - R)          # noqa: E731
    y = lambda v: T + (1 - v / top) * (H - T - B)                 # noqa: E731
    path = f"M{x(0):.1f},{y(vals[0]):.1f}" + "".join(f" H{x(i):.1f} V{y(v):.1f}" for i, v in enumerate(vals) if i)
    i_low = len(vals) - 1 - vals[::-1].index(min(vals))
    first, last = date.fromisoformat(days[0]), date.fromisoformat(days[-1])
    return f"""<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="Meilleur prix du jour sur {len(days)} jours">
  <line x1="{L}" x2="{W-R}" y1="{y(normal):.1f}" y2="{y(normal):.1f}" stroke="#5c6070" stroke-dasharray="4 4"/>
  <text x="{W-R}" y="{y(normal)-6:.1f}" text-anchor="end" font-size="12" fill="#5c6070">Prix normal {escape(usd(normal))}</text>
  <line x1="{L}" x2="{W-R}" y1="{y(0):.1f}" y2="{y(0):.1f}" stroke="#d9d5c7"/>
  <path d="{path}" fill="none" stroke="#1a1c24" stroke-width="2.5"/>
  <circle cx="{x(i_low):.1f}" cy="{y(vals[i_low]):.1f}" r="5" fill="#1f9d5c"/>
  <text x="{L}" y="{H-8}" font-size="12" fill="#5c6070">{first.day} {MONTHS[first.month-1][:4]}.</text>
  <text x="{W-R}" y="{H-8}" text-anchor="end" font-size="12" fill="#5c6070">{last.day} {MONTHS[last.month-1][:4]}.</text>
</svg>"""


STYLE = """
:root{--shelf:#1d2742;--tag:#f6f4ec;--ink:#1a1c24;--soft:#5c6070;--sticker:#ffd23f;--low:#1f9d5c;--line:#d9d5c7}
*{box-sizing:border-box}body{margin:0;background:var(--shelf);color:var(--tag);font-family:"Archivo",system-ui,sans-serif;line-height:1.45;overflow-x:clip}
a{color:inherit;text-underline-offset:3px}:focus-visible{outline:3px solid #7cc4ff;outline-offset:2px}
.wrap{max-width:820px;margin:0 auto;padding:0 16px}header{padding:22px 0}
.brand{font-weight:800;font-stretch:125%;font-size:22px;text-decoration:none}
article{background:var(--tag);color:var(--ink);border-radius:8px;overflow:hidden;margin-bottom:28px;position:relative}
.cover{display:block;width:100%;aspect-ratio:460/215;object-fit:cover;background:#c9c5b6}
.sticker{position:absolute;top:12px;right:12px;width:74px;height:74px;border-radius:50%;display:grid;place-items:center;
background:var(--sticker);color:var(--ink);font-weight:800;font-stretch:75%;font-size:24px;transform:rotate(-12deg);box-shadow:0 2px 0 rgba(0,0,0,.25)}
.body{padding:20px 22px 26px}h1{margin:0 0 6px;font-size:30px;line-height:1.1;font-stretch:110%;font-weight:800}
h2{margin:28px 0 10px;font-size:18px;font-weight:750}.meta{color:var(--soft);font-size:14px}
.price{font-size:44px;font-weight:800;font-stretch:62%;line-height:1;margin:16px 0 0}.was{color:var(--soft);text-decoration:line-through;font-size:16px;margin-left:8px}
.verdict{margin:14px 0 0;padding:12px 14px;border-radius:4px;background:#ebe8dc}.chips{list-style:none;display:flex;flex-wrap:wrap;gap:6px;padding:0;margin:12px 0 0}
.chips li{font-size:13px;padding:3px 9px;border-radius:999px;background:#e4e0d2}table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
td,th{padding:9px 8px 9px 0;border-top:1px solid var(--line);text-align:left}th{font-size:13px;color:var(--soft);font-weight:600;border-top:0}.num{text-align:right}
.reviews{display:grid;grid-template-columns:1fr 1fr;gap:18px}.reviews h3{margin:0 0 6px;font-size:15px}.reviews ul{margin:0;padding-left:18px}
.chart{width:100%;height:auto}.note{color:var(--soft);font-size:14px}.similar{list-style:none;padding:0;margin:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:8px}
.similar a{display:block;padding:10px 12px;border-radius:4px;background:#ebe8dc;text-decoration:none}.similar span{display:block;color:var(--soft);font-size:13px}
.cta{display:inline-block;margin-top:18px;padding:11px 16px;border-radius:4px;background:var(--ink);color:var(--tag);font-weight:700;text-decoration:none}
footer{padding:0 0 36px;color:#8f98b3;font-size:14px}
@media(max-width:600px){.reviews{grid-template-columns:1fr}.body{padding:16px}.price{font-size:36px}h1{font-size:25px}}
"""


def render(g: dict, stores: dict, history: list[dict], similar: list[dict], snapshot: str) -> str:
    url = SITE_URL + page_path(g)
    title_v, text_v = verdict(g)
    pct = round(g["best_savings"])
    image = g.get("header") or g.get("thumb") or ""
    desc_meta = f"{g['title']} à {usd(g['best_price'])} au lieu de {usd(g['normal_price'])} (−{pct} %). {title_v}. Comparatif des prix entre boutiques et historique."
    tags = (g.get("tags") or [])[:6] + [x for x in (g.get("genres") or []) if x.lower() not in {t.lower() for t in g.get("tags") or []}][:3]
    if g.get("coop"):
        tags.append("Coop")

    offers = "".join(
        f"<tr><td>{escape(stores.get(str(o['store_id']), {}).get('name', 'Boutique'))}</td>"
        f"<td class='num'>{escape(usd(o['price']))}</td><td class='num'>−{round(o['savings'])} %</td>"
        f"<td class='num'><a href='{escape(o['url'])}' rel='nofollow sponsored noopener' target='_blank'>Voir l'offre</a></td></tr>"
        for o in g["offers"])

    rv = g.get("reviews")
    reviews_html = ""
    if rv:
        li = lambda xs: "".join(f"<li>{escape(x)}</li>" for x in xs) or "<li>Rien de marquant.</li>"  # noqa: E731
        reviews_html = f"""<h2>Ce qu'en disent les joueurs</h2>
<p>{escape(rv['tldr'])}</p>
<div class="reviews"><div><h3>Ils aiment</h3><ul>{li(rv.get('likes') or [])}</ul></div>
<div><h3>Ils regrettent</h3><ul>{li(rv.get('dislikes') or [])}</ul></div></div>
<p class="note">Résumé généré par IA à partir de {rv.get('n') or 'plusieurs'} avis Steam.</p>"""

    similar_html = ""
    if similar:
        items = "".join(f"<li><a href='../{page_path(s)}'>{escape(s['title'])}<span>{escape(usd(s['best_price']))}, −{round(s['best_savings'])} %</span></a></li>" for s in similar)
        similar_html = f"<h2>Jeux similaires en promo</h2><ul class='similar'>{items}</ul>"

    ld = {"@context": "https://schema.org", "@type": "VideoGame", "name": g["title"], "url": url,
          "gamePlatform": "PC", "image": image or None, "description": g.get("desc") or None,
          "offers": {"@type": "AggregateOffer", "priceCurrency": "USD", "lowPrice": g["best_price"],
                     "highPrice": max(o["price"] for o in g["offers"]), "offerCount": len(g["offers"])}}
    ld = {k: v for k, v in ld.items() if v is not None}

    eur_html = ""
    if g.get("eur") is not None:
        eur_html = f"<p>Sur Steam France : <strong>{escape(eur(g['eur']))}</strong>"
        if g.get("eur_normal") and g["eur_normal"] > g["eur"]:
            eur_html += f" au lieu de {escape(eur(g['eur_normal']))}"
        eur_html += ".</p>"

    infos = [x for x in [
        f"Par {', '.join(g['devs'])}" if g.get("devs") else "",
        f"Steam : {g['steam_pct']} % d'avis positifs" if g.get("steam_pct") is not None else "",
        f"Metacritic : {g['metacritic']}" if g.get("metacritic") else "",
    ] if x]

    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(g['title'])} à {escape(usd(g['best_price']))} (−{pct} %) — Deal Hunter</title>
<meta name="description" content="{escape(desc_meta)}">
<link rel="canonical" href="{escape(url)}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Deal Hunter">
<meta property="og:title" content="{escape(g['title'])} à {escape(usd(g['best_price']))} (−{pct} %)">
<meta property="og:description" content="{escape(title_v)}. {escape(text_v)}">
<meta property="og:url" content="{escape(url)}">
{f'<meta property="og:image" content="{escape(image)}">' if image else ''}
<meta name="twitter:card" content="summary_large_image">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,400..800&display=swap" rel="stylesheet">
<style>{STYLE}</style>
<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False).replace("<", "\\u003c")}</script>
</head>
<body>
<header class="wrap"><a class="brand" href="../">Deal Hunter</a></header>
<main class="wrap">
<article>
  {f'<img class="cover" src="{escape(image)}" alt="">' if image else ''}
  <span class="sticker">−{pct}%</span>
  <div class="body">
    <h1>{escape(g['title'])}</h1>
    <div class="meta">{'<br>'.join(escape(i) for i in infos)}</div>
    <p class="price">{escape(usd(g['best_price']))}<span class="was">{escape(usd(g['normal_price']))}</span></p>
    {eur_html}
    <p class="verdict"><strong>{escape(title_v)}.</strong> {escape(text_v)}</p>
    {f"<p>{escape(g['desc'])}</p>" if g.get('desc') else ''}
    {'<ul class="chips">' + ''.join(f'<li>{escape(t)}</li>' for t in tags) + '</ul>' if tags else ''}
    <h2>Prix par boutique</h2>
    <table><thead><tr><th>Boutique</th><th class="num">Prix</th><th class="num">Réduction</th><th></th></tr></thead><tbody>{offers}</tbody></table>
    <h2>Historique du meilleur prix</h2>
    {chart_svg(history, g['normal_price'])}
    {reviews_html}
    {similar_html}
    <a class="cta" href="../#jeu-{g['id']}">Suivre ce jeu sur Deal Hunter</a>
  </div>
</article>
</main>
<footer class="wrap">Prix relevés le {escape(snapshot)}, en dollars US tels qu'affichés par les boutiques. Données : CheapShark, Steam, SteamSpy.</footer>
</body>
</html>
"""


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    deals_path = DATA_DIR / "deals.json"
    if not deals_path.exists():
        log.error("site/data/deals.json introuvable : lance d'abord export_site_data.py")
        return 1

    data = json.loads(deals_path.read_text(encoding="utf-8"))
    games = data["games"]
    by_id = {g["id"]: g for g in games}
    sim_path = DATA_DIR / "search" / "similar.json"
    similar = json.loads(sim_path.read_text(encoding="utf-8")) if sim_path.exists() else {}
    snap = data["snapshot_ts"][:10]
    d = date.fromisoformat(snap)
    snapshot_fr = f"{d.day} {MONTHS[d.month - 1]} {d.year}"

    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)          # les jeux qui ne sont plus en promo disparaissent
    OUT_DIR.mkdir(parents=True)

    built, failed = [], 0
    for g in games:
        hist_path = DATA_DIR / "history" / f"{g['id']}.json"
        history = json.loads(hist_path.read_text(encoding="utf-8")) if hist_path.exists() else []
        sims = [by_id[i] for i in similar.get(str(g["id"]), [])
                if i in by_id and by_id[i].get("best_price") is not None and by_id[i].get("best_savings") is not None]
        try:
            html = render(g, data["stores"], history, sims, snapshot_fr)
        except Exception:   # un jeu aux données inattendues ne doit pas bloquer la publication du site
            log.exception("Page impossible pour le jeu %s (%s) : ignorée", g.get("id"), g.get("title"))
            failed += 1
            continue
        (SITE_DIR / page_path(g)).write_text(html, encoding="utf-8")
        built.append(g)
    if failed > len(games) * 0.05:   # au-delà de 5 % d'échecs, ce n'est plus un cas isolé : on arrête
        log.error("%d pages en échec sur %d : publication interrompue", failed, len(games))
        return 1
    games = built

    urls = [f"  <url><loc>{SITE_URL}</loc><lastmod>{snap}</lastmod><changefreq>daily</changefreq></url>",
            f"  <url><loc>{SITE_URL}insights.html</loc><lastmod>{snap}</lastmod></url>"]
    urls += [f"  <url><loc>{escape(SITE_URL + page_path(g))}</loc><lastmod>{snap}</lastmod></url>" for g in games]
    (SITE_DIR / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls) + "\n</urlset>\n", encoding="utf-8")
    (SITE_DIR / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}sitemap.xml\n", encoding="utf-8")

    log.info("Pages : %d fiches jeu, sitemap de %d URL -> site/jeu/", len(games), len(urls))
    return 0


if __name__ == "__main__":
    sys.exit(main())
