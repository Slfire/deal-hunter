"""
Deal Hunter — snapshot quotidien CheapShark (couche RAW).

Récupère :
  - la liste des boutiques  (/stores)
  - les deals paginés       (/deals)

Et sauvegarde le JSON brut, sans aucune transformation, dans :
  data/raw/stores/YYYY-MM-DD/stores_<HHMMSS>.json.gz
  data/raw/deals/YYYY-MM-DD/<HHMMSS>/page_0000.json.gz ...
  data/raw/deals/YYYY-MM-DD/<HHMMSS>/_manifest.json

Usage :
  python src/ingestion/snapshot.py                 # 3 pages (mode test)
  python src/ingestion/snapshot.py --max-pages 0   # toutes les pages
  python src/ingestion/snapshot.py --max-pages 10 --sleep 1.5

Config (.env ou variable d'environnement) :
  DEALHUNTER_CONTACT=ton.mail@example.com
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

import requests

from common import (PROJECT_ROOT, RAW_DIR, build_session, get_contact, get_with_retry, log,
                    setup_logging, write_json)

BASE_URL = "https://www.cheapshark.com/api/1.0"
PAGE_SIZE = 60          # max autorisé par l'API


def snapshot_stores(session: requests.Session, run_ts: datetime) -> int:
    resp = get_with_retry(session, f"{BASE_URL}/stores")
    stores = resp.json()
    path = RAW_DIR / "stores" / run_ts.strftime("%Y-%m-%d") / f"stores_{run_ts.strftime('%H%M%S')}.json.gz"
    write_json(path, stores)
    log.info("Stores : %d boutiques -> %s", len(stores), path.relative_to(PROJECT_ROOT))
    return len(stores)


def snapshot_deals(session: requests.Session, run_ts: datetime, max_pages: int, sleep_s: float) -> dict:
    run_dir = RAW_DIR / "deals" / run_ts.strftime("%Y-%m-%d") / run_ts.strftime("%H%M%S")
    page = 0
    total_pages = None
    total_deals = 0

    while True:
        params = {"pageSize": PAGE_SIZE, "pageNumber": page}
        resp = get_with_retry(session, f"{BASE_URL}/deals", params=params)
        deals = resp.json()

        if total_pages is None:
            header = resp.headers.get("X-Total-Page-Count")
            total_pages = int(header) + 1 if header and header.isdigit() else None  # header = index de la dernière page
            log.info("Pages disponibles côté API : %s", total_pages if total_pages is not None else "inconnu")

        if not deals:
            log.info("Page %d vide, fin de pagination.", page)
            break

        write_json(run_dir / f"page_{page:04d}.json.gz", deals)
        total_deals += len(deals)
        log.info("Page %d : %d deals", page, len(deals))

        page += 1
        if max_pages and page >= max_pages:
            log.info("Limite --max-pages=%d atteinte.", max_pages)
            break
        if total_pages is not None and page >= total_pages:
            break

        time.sleep(sleep_s)

    manifest = {
        "source": "cheapshark",
        "endpoint": "/deals",
        "run_ts_utc": run_ts.isoformat(),
        "page_size": PAGE_SIZE,
        "pages_fetched": page,
        "pages_available": total_pages,
        "deals_fetched": total_deals,
        "complete": total_pages is not None and page >= total_pages,
    }
    write_json(run_dir / "_manifest.json", manifest)
    log.info("Deals : %d deals sur %d pages -> %s", total_deals, page, run_dir.relative_to(PROJECT_ROOT))
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Snapshot RAW CheapShark (stores + deals)")
    parser.add_argument("--max-pages", type=int, default=3, help="Nb max de pages de deals (0 = toutes). Défaut : 3")
    parser.add_argument("--sleep", type=float, default=1.0, help="Pause entre deux pages, en secondes. Défaut : 1.0")
    args = parser.parse_args()
    setup_logging()

    contact = get_contact()
    if not contact:
        log.error("DEALHUNTER_CONTACT manquant dans le .env (ton mail, exigé pour le User-Agent).")
        return 1

    run_ts = datetime.now(timezone.utc)
    session = build_session(contact)

    try:
        snapshot_stores(session, run_ts)
        manifest = snapshot_deals(session, run_ts, args.max_pages, args.sleep)
    except RuntimeError as exc:
        log.error(str(exc))
        return 1

    log.info("Terminé : %d deals récupérés (snapshot complet : %s)", manifest["deals_fetched"], manifest["complete"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
