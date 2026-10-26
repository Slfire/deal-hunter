"""
Deal Hunter — avis des joueurs Steam (matière première des résumés IA).

Source : https://store.steampowered.com/appreviews/<appid>?json=1&filter=all&language=all
  - les avis jugés « utiles » d'abord (filter=all), toutes langues confondues
  - on priorise les jeux les plus populaires : ce sont ceux dont le résumé sert le plus

Données de RÉFÉRENCE : cache dans data/ref/steam_reviews/ (voir common.fill_ref_cache).
Exception assumée au principe « brut tel quel » : on ne garde que les champs utiles
et le début de chaque avis (700 caractères), sinon le cache pèserait des centaines de Mo.

Usage :
  python src/ingestion/steam_reviews.py
  python src/ingestion/steam_reviews.py --max-new 150
"""

from __future__ import annotations

import argparse
import sys

from common import build_session, fill_ref_cache, get_contact, get_with_retry, latest_deals, log, setup_logging

URL = "https://store.steampowered.com/appreviews/{app_id}"
KEEP_REVIEWS = 30
MAX_CHARS = 700


def popular_app_ids() -> list[str]:
    """Jeux Steam du dernier snapshot, du plus au moins évalué."""
    counts: dict[str, int] = {}
    for d in latest_deals():
        app = d.get("steamAppID")
        if app:
            counts[app] = max(counts.get(app, 0), int(d.get("steamRatingCount") or 0))
    return sorted(counts, key=lambda a: -counts[a])


def trim(payload: dict) -> dict:
    reviews = []
    for r in (payload.get("reviews") or [])[:KEEP_REVIEWS]:
        text = (r.get("review") or "").strip()
        if len(text) < 20:          # « bon jeu », « 10/10 » : rien à résumer
            continue
        reviews.append({
            "text": text[:MAX_CHARS],
            "positive": bool(r.get("voted_up")),
            "votes_up": r.get("votes_up", 0),
            "language": r.get("language"),
            "hours": round((r.get("author") or {}).get("playtime_forever", 0) / 60, 1),
        })
    return {"success": payload.get("success"), "query_summary": payload.get("query_summary"), "reviews": reviews}


def main() -> int:
    parser = argparse.ArgumentParser(description="Cache des avis Steam (référence)")
    parser.add_argument("--max-new", type=int, default=150, help="Nb max de jeux à récupérer. Défaut : 150")
    parser.add_argument("--sleep", type=float, default=1.6, help="Pause entre deux appels. Défaut : 1.6")
    parser.add_argument("--refresh-days", type=int, default=90, help="Re-télécharge les avis plus vieux. Défaut : 90")
    args = parser.parse_args()
    setup_logging()

    ids = popular_app_ids()
    if not ids:
        log.error("Aucun snapshot de deals : lance d'abord snapshot.py")
        return 1

    session = build_session(get_contact() or "anonymous")

    def fetch(app_id: str):
        params = {"json": 1, "filter": "all", "language": "all", "review_type": "all",
                  "purchase_type": "all", "num_per_page": 40}
        resp = get_with_retry(session, URL.format(app_id=app_id), params=params, allow_empty=False)
        ctype = resp.headers.get("Content-Type", "")
        try:
            payload = resp.json()
        except ValueError:
            # on garde la trace de ce que Steam a vraiment renvoyé, pour comprendre
            raise ValueError(f"réponse non JSON : HTTP {resp.status_code}, {ctype or 'sans type'}, "
                             f"URL {resp.url}, début : {resp.text[:160]!r}") from None
        if not isinstance(payload, dict) or payload.get("success") != 1:
            raise ValueError(f"réponse JSON inattendue : {str(payload)[:160]}")
        return trim(payload)

    fill_ref_cache("steam_reviews", ids, fetch, args.max_new, args.sleep, args.refresh_days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
