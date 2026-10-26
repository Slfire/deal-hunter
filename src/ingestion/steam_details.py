"""
Deal Hunter — enrichissement Steam (genres, catégories coop/multi, description FR, développeurs).

Source : https://store.steampowered.com/api/appdetails?appids=<id>&l=french
  - 1 jeu par appel (l'API n'accepte pas plusieurs ids hors filtre prix)
  - limitée à ~200 appels / 5 min -> pause de 1,6 s par défaut

Données de RÉFÉRENCE (elles bougent peu) : on ne télécharge que les jeux jamais vus,
plus ceux dont la fiche a plus de --refresh-days jours. Cache dans :
  data/ref/steam/<appid>.json.gz    (réponse brute, telle quelle)
  data/ref/steam/_index.json        ({appid: date de récupération})

--max-new borne le nombre d'appels par run : le premier remplissage se fait sur
quelques jours, puis il n'y a que les nouveaux jeux à récupérer.

Note : Steam est bloqué sur certains réseaux (école, entreprise). Le script s'arrête
proprement si Steam ne répond pas ; il tourne de toute façon dans GitHub Actions.

Usage :
  python src/ingestion/steam_details.py
  python src/ingestion/steam_details.py --max-new 400 --sleep 1.6
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta

from common import (REF_DIR, build_session, get_contact, get_with_retry, latest_deals, log, read_json,
                    setup_logging, write_json)

URL = "https://store.steampowered.com/api/appdetails"
CACHE_DIR = REF_DIR / "steam"
INDEX = CACHE_DIR / "_index.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Cache des fiches Steam (référence)")
    parser.add_argument("--max-new", type=int, default=400, help="Nb max de fiches à télécharger. Défaut : 400")
    parser.add_argument("--sleep", type=float, default=1.6, help="Pause entre deux appels. Défaut : 1.6")
    parser.add_argument("--refresh-days", type=int, default=60, help="Re-télécharge les fiches plus vieilles. Défaut : 60")
    args = parser.parse_args()
    setup_logging()

    contact = get_contact() or "anonymous"
    deals = latest_deals()
    if not deals:
        log.error("Aucun snapshot de deals : lance d'abord snapshot.py")
        return 1

    app_ids = sorted({d["steamAppID"] for d in deals if d.get("steamAppID")}, key=int)
    index: dict[str, str] = read_json(INDEX) if INDEX.exists() else {}
    stale_before = (date.today() - timedelta(days=args.refresh_days)).isoformat()

    todo = [a for a in app_ids if a not in index] + [a for a in app_ids if a in index and index[a] < stale_before]
    log.info("%d jeux Steam dans les deals, %d déjà en cache, %d à récupérer (max %d ce run)",
             len(app_ids), len(app_ids) - len([a for a in app_ids if a not in index]), len(todo), args.max_new)
    todo = todo[:args.max_new]

    session = build_session(contact)
    session.headers["Accept-Language"] = "fr-FR,fr;q=0.9"
    done = 0
    try:
        for i, app_id in enumerate(todo):
            resp = get_with_retry(session, URL, params={"appids": app_id, "l": "french", "cc": "fr"}, allow_empty=False)
            write_json(CACHE_DIR / f"{app_id}.json.gz", resp.json())
            index[app_id] = date.today().isoformat()
            done += 1
            if done % 50 == 0:
                write_json(INDEX, index)          # sauvegarde régulière : un crash ne fait pas tout perdre
                log.info("%d/%d fiches récupérées", done, len(todo))
            if i < len(todo) - 1:
                time.sleep(args.sleep)
    except RuntimeError as exc:
        log.warning("Steam ne répond plus (%s) — on garde les %d fiches déjà récupérées.", exc, done)
    finally:
        write_json(INDEX, index)

    log.info("Steam : %d fiches récupérées ce run, %d en cache au total", done, len(index))
    return 0


if __name__ == "__main__":
    sys.exit(main())
