"""
Deal Hunter — prix Steam en euros (snapshot quotidien).

CheapShark donne les prix en dollars US. Pour un site français, on récupère aussi
le prix Steam officiel en euros, chaque jour (contrairement aux fiches de référence,
un prix change souvent : on ne le met pas en cache).

Source : https://store.steampowered.com/api/appdetails?appids=1,2,3&cc=fr&filters=price_overview
  - avec filters=price_overview, Steam accepte plusieurs jeux par appel -> lots de 50

Sortie brute (réponse telle quelle) :
  data/raw/steam_prices/YYYY-MM-DD/<HHMMSS>/batch_0000.json.gz ...

À lancer APRÈS snapshot.py (on s'aligne sur l'horodatage du snapshot de deals).

Usage :
  python src/ingestion/steam_prices.py
"""

from __future__ import annotations

import argparse
import sys
import time

from common import (PROJECT_ROOT, RAW_DIR, build_session, get_contact, get_with_retry, latest_deals_run, log,
                    setup_logging, steam_app_ids_from_latest_deals, write_json)

URL = "https://store.steampowered.com/api/appdetails"
BATCH = 50


def main() -> int:
    parser = argparse.ArgumentParser(description="RAW prix Steam en euros")
    parser.add_argument("--sleep", type=float, default=1.6, help="Pause entre deux appels. Défaut : 1.6")
    args = parser.parse_args()
    setup_logging()

    ids = steam_app_ids_from_latest_deals()
    run = latest_deals_run()
    if not ids or run is None:
        log.error("Aucun snapshot de deals : lance d'abord snapshot.py")
        return 1

    run_dir = RAW_DIR / "steam_prices" / run.parent.name / run.name
    batches = [ids[i:i + BATCH] for i in range(0, len(ids), BATCH)]
    log.info("%d jeux Steam, %d appels", len(ids), len(batches))

    session = build_session(get_contact() or "anonymous")
    done = 0
    try:
        for k, batch in enumerate(batches):
            params = {"appids": ",".join(batch), "cc": "fr", "filters": "price_overview"}
            resp = get_with_retry(session, URL, params=params, allow_empty=False)
            write_json(run_dir / f"batch_{k:04d}.json.gz", resp.json())
            done += 1
            if k < len(batches) - 1:
                time.sleep(args.sleep)
    except RuntimeError as exc:
        log.warning("Steam ne répond plus (%s) — %d/%d lots récupérés.", exc, done, len(batches))

    if done:
        log.info("Prix Steam : %d lots -> %s", done, run_dir.relative_to(PROJECT_ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
