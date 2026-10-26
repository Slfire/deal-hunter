"""
Deal Hunter — tags utilisateurs Steam via SteamSpy (« Roguelike », « Cozy », « Souls-like »...).

Les genres officiels Steam sont trop larges (« Action », « Indépendant ») : ce sont les tags
posés par les joueurs qui décrivent vraiment un jeu. SteamSpy les expose gratuitement,
avec le nombre de votes par tag.

Source : https://steamspy.com/api.php?request=appdetails&appid=<id>
  - limitée à 1 appel par seconde -> pause de 1,1 s par défaut

Données de RÉFÉRENCE : cache dans data/ref/steamspy/ (voir common.fill_ref_cache).

Usage :
  python src/ingestion/steamspy_tags.py
  python src/ingestion/steamspy_tags.py --max-new 500
"""

from __future__ import annotations

import argparse
import sys

from common import build_session, fill_ref_cache, get_contact, get_with_retry, log, setup_logging, \
    steam_app_ids_from_latest_deals

URL = "https://steamspy.com/api.php"


def main() -> int:
    parser = argparse.ArgumentParser(description="Cache des tags SteamSpy (référence)")
    parser.add_argument("--max-new", type=int, default=500, help="Nb max de fiches à télécharger. Défaut : 500")
    parser.add_argument("--sleep", type=float, default=1.1, help="Pause entre deux appels. Défaut : 1.1")
    parser.add_argument("--refresh-days", type=int, default=60, help="Re-télécharge les fiches plus vieilles. Défaut : 60")
    args = parser.parse_args()
    setup_logging()

    ids = steam_app_ids_from_latest_deals()
    if not ids:
        log.error("Aucun snapshot de deals : lance d'abord snapshot.py")
        return 1

    session = build_session(get_contact() or "anonymous")

    def fetch(app_id: str):
        params = {"request": "appdetails", "appid": app_id}
        return get_with_retry(session, URL, params=params, allow_empty=False).json()

    fill_ref_cache("steamspy", ids, fetch, args.max_new, args.sleep, args.refresh_days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
