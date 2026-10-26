"""
Deal Hunter — enrichissement Steam (genres, catégories coop/multi, description FR, développeurs).

Source : https://store.steampowered.com/api/appdetails?appids=<id>&l=french
  - 1 jeu par appel (l'API n'accepte pas plusieurs ids hors filtre prix)
  - limitée à ~200 appels / 5 min -> pause de 1,6 s par défaut

Données de RÉFÉRENCE : cache dans data/ref/steam/ (voir common.fill_ref_cache).

Note : Steam est bloqué sur certains réseaux (école, entreprise). Le script s'arrête
proprement si Steam ne répond pas ; il tourne de toute façon dans GitHub Actions.

Usage :
  python src/ingestion/steam_details.py
  python src/ingestion/steam_details.py --max-new 400 --sleep 1.6
"""

from __future__ import annotations

import argparse
import sys

from common import build_session, fill_ref_cache, get_contact, get_with_retry, log, setup_logging, \
    steam_app_ids_from_latest_deals

URL = "https://store.steampowered.com/api/appdetails"


def main() -> int:
    parser = argparse.ArgumentParser(description="Cache des fiches Steam (référence)")
    parser.add_argument("--max-new", type=int, default=400, help="Nb max de fiches à télécharger. Défaut : 400")
    parser.add_argument("--sleep", type=float, default=1.6, help="Pause entre deux appels. Défaut : 1.6")
    parser.add_argument("--refresh-days", type=int, default=60, help="Re-télécharge les fiches plus vieilles. Défaut : 60")
    args = parser.parse_args()
    setup_logging()

    ids = steam_app_ids_from_latest_deals()
    if not ids:
        log.error("Aucun snapshot de deals : lance d'abord snapshot.py")
        return 1

    session = build_session(get_contact() or "anonymous")
    session.headers["Accept-Language"] = "fr-FR,fr;q=0.9"

    def fetch(app_id: str):
        params = {"appids": app_id, "l": "french", "cc": "fr"}
        return get_with_retry(session, URL, params=params, allow_empty=False).json()

    fill_ref_cache("steam", ids, fetch, args.max_new, args.sleep, args.refresh_days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
