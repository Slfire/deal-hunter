"""
Deal Hunter — infos jeux CheapShark (/games), dont le PLUS BAS PRIX HISTORIQUE.

Pour chaque jeu présent dans le dernier snapshot de deals, récupère via /games?ids=
(25 jeux max par appel) :
  - info.title, info.steamAppID, info.thumb
  - cheapestPriceEver.price + .date   <- tout l'historique CheapShark, dispo dès le jour 1

Sauvegarde brute (réponse telle quelle, un objet {gameID: {...}} par lot) dans :
  data/raw/games/YYYY-MM-DD/<HHMMSS>/batch_0000.json.gz ...

À lancer APRÈS snapshot.py.

Usage :
  python src/ingestion/games_info.py
  python src/ingestion/games_info.py --sleep 1.5
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

from common import (PROJECT_ROOT, RAW_DIR, build_session, get_contact, get_with_retry, latest_deals,
                    latest_deals_run, log, setup_logging, write_json)

BASE_URL = "https://www.cheapshark.com/api/1.0"
BATCH = 25   # limite de l'API pour ?ids=


def main() -> int:
    parser = argparse.ArgumentParser(description="RAW CheapShark /games (plus bas prix historique)")
    parser.add_argument("--sleep", type=float, default=1.0, help="Pause entre deux appels. Défaut : 1.0")
    args = parser.parse_args()
    setup_logging()

    contact = get_contact()
    if not contact:
        log.error("DEALHUNTER_CONTACT manquant.")
        return 1

    deals = latest_deals()
    if not deals:
        log.error("Aucun snapshot de deals : lance d'abord snapshot.py")
        return 1

    game_ids = sorted({d["gameID"] for d in deals if d.get("gameID")}, key=int)
    batches = [game_ids[i:i + BATCH] for i in range(0, len(game_ids), BATCH)]

    # on aligne l'horodatage sur le snapshot de deals : même jour, même heure
    run = latest_deals_run()
    run_dir = RAW_DIR / "games" / run.parent.name / run.name
    log.info("%d jeux à enrichir en %d appels", len(game_ids), len(batches))

    session = build_session(contact)
    fetched = 0
    try:
        for i, batch in enumerate(batches):
            resp = get_with_retry(session, f"{BASE_URL}/games", params={"ids": ",".join(batch)})
            payload = resp.json()
            write_json(run_dir / f"batch_{i:04d}.json.gz", payload)
            fetched += len(payload) if isinstance(payload, dict) else 0
            if i < len(batches) - 1:
                time.sleep(args.sleep)
    except RuntimeError as exc:
        log.error(str(exc))
        return 1

    write_json(run_dir / "_manifest.json", {
        "source": "cheapshark",
        "endpoint": "/games?ids=",
        "run_ts_utc": datetime.now(timezone.utc).isoformat(),
        "games_requested": len(game_ids),
        "games_fetched": fetched,
    })
    log.info("Games : %d/%d jeux -> %s", fetched, len(game_ids), run_dir.relative_to(PROJECT_ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
