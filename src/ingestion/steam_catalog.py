"""
Deal Hunter — catalogue complet des jeux Steam payants (source : SteamSpy).

L'ancienne liste publique de Steam (ISteamApps/GetAppList) a été fermée (404). SteamSpy publie tout
le catalogue par pages de 1 000 applications (request=all&page=N), avec le nombre de propriétaires
estimé, les avis et le prix. Limite SteamSpy : 1 page par minute sur cette requête, soit ~1 h 30 pour
les ~85 pages : d'où un rafraîchissement mensuel dans son propre workflow.

On ne garde que les jeux PAYANTS (prix de lancement > 0) et quelques colonnes utiles.

Sortie : data/ref/catalog/steam_catalog_YYYY-MM-DD.parquet (les anciens fichiers sont supprimés :
git garde de toute façon l'historique).

Usage :
  python src/ingestion/steam_catalog.py               # tout le catalogue
  python src/ingestion/steam_catalog.py --max-pages 2 # pour tester
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date

import requests

from common import build_session, get_contact, log, setup_logging
from itad_common import CATALOG_DIR, write_part

URL = "https://steamspy.com/api.php"
PAGE_PAUSE_S = 61          # SteamSpy : 1 appel par minute sur request=all

COLUMNS = {
    "appid": "INTEGER", "name": "VARCHAR", "developer": "VARCHAR", "publisher": "VARCHAR",
    "owners_min": "BIGINT", "owners_max": "BIGINT", "positive": "INTEGER", "negative": "INTEGER", "reviews": "INTEGER",
    "price_usd_cents": "INTEGER", "initial_price_usd_cents": "INTEGER", "ccu": "INTEGER",
}


def parse_owners(text: str) -> tuple[int, int]:
    """« 1,000,000 .. 2,000,000 » -> (1000000, 2000000)"""
    try:
        lo, hi = (int(x.strip().replace(",", "").replace(" ", "")) for x in text.split(".."))
        return lo, hi
    except (AttributeError, ValueError):
        return 0, 0


def to_int(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def parse_page(payload: dict) -> list[tuple]:
    rows = []
    for app in payload.values():
        initial, price = to_int(app.get("initialprice")), to_int(app.get("price"))
        if max(initial, price) <= 0:
            continue                                    # gratuit : pas de promo possible
        lo, hi = parse_owners(app.get("owners", ""))
        pos, neg = to_int(app.get("positive")), to_int(app.get("negative"))
        rows.append((to_int(app.get("appid")), (app.get("name") or "").strip(), app.get("developer") or None,
                     app.get("publisher") or None, lo, hi, pos, neg, pos + neg, price, initial, to_int(app.get("ccu"))))
    return rows


def fetch_page(session: requests.Session, page: int) -> dict | None:
    """Une page du catalogue. None = page vide ou inexistante (fin du catalogue)."""
    for attempt in range(1, 4):
        try:
            resp = session.get(URL, params={"request": "all", "page": page}, timeout=120)
        except requests.RequestException as exc:
            log.warning("SteamSpy page %d : %s (tentative %d/3)", page, type(exc).__name__, attempt)
            time.sleep(PAGE_PAUSE_S)
            continue
        if resp.status_code == 200 and not resp.text.strip():
            return None
        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                return None
            return data or None
        log.warning("SteamSpy page %d : HTTP %d (tentative %d/3)", page, resp.status_code, attempt)
        time.sleep(PAGE_PAUSE_S)
    raise RuntimeError(f"SteamSpy page {page} : échec après 3 tentatives")


def main() -> int:
    parser = argparse.ArgumentParser(description="Catalogue Steam payant via SteamSpy")
    parser.add_argument("--max-pages", type=int, default=200)
    args = parser.parse_args()
    setup_logging()

    session = build_session(get_contact() or "anonymous")
    rows: list[tuple] = []
    for page in range(args.max_pages):
        if page:
            time.sleep(PAGE_PAUSE_S)
        payload = fetch_page(session, page)
        if payload is None:
            log.info("Page %d vide : fin du catalogue", page)
            break
        new = parse_page(payload)
        rows += new
        log.info("Page %d : %d applications, %d payantes (total %d)", page, len(payload), len(new), len(rows))

    rows = list({r[0]: r for r in rows}.values())       # un appid apparaît parfois sur deux pages
    if len(rows) < 1000 and args.max_pages >= 50:
        log.error("Seulement %d jeux payants : SteamSpy a probablement coupé en route, catalogue précédent conservé", len(rows))
        return 1

    old = sorted(CATALOG_DIR.glob("steam_catalog_*.parquet"))
    out = CATALOG_DIR / f"steam_catalog_{date.today().isoformat()}.parquet"
    path = write_part(CATALOG_DIR, rows, COLUMNS, stamp="tmp")
    path.replace(out)
    for f in old:
        if f != out:
            f.unlink()
    log.info("Catalogue : %d jeux payants -> %s", len(rows), out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
