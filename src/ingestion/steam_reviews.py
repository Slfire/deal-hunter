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

# Plusieurs formes de requête documentées pour /appreviews : Steam redirige certaines
# combinaisons vers sa page d'accueil. On essaie dans l'ordre, la première qui marche est gardée.
VARIANTS = [
    {"json": 1, "filter": "all", "language": "all", "review_type": "all", "purchase_type": "all",
     "num_per_page": 40, "cursor": "*"},
    {"json": 1, "filter": "recent", "language": "all", "purchase_type": "all", "num_per_page": 20, "cursor": "*"},
    {"json": 1, "language": "all"},
    {"json": 1},
]
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


def make_fetch(session):
    """Fonction de récupération des avis d'un jeu, qui essaie les variantes de requête dans l'ordre."""
    def try_variant(app_id: str, params: dict):
        url = URL.format(app_id=app_id)
        resp = get_with_retry(session, url, params=params, allow_empty=False)
        try:
            payload = resp.json()
        except ValueError:
            hops = " -> ".join(f"{h.status_code} {h.headers.get('Location', '')}" for h in resp.history) or "aucune"
            raise ValueError(f"réponse non JSON : HTTP {resp.status_code}, {resp.headers.get('Content-Type', 'sans type')}, "
                             f"redirections : {hops}, URL finale {resp.url}, début : {resp.text[:120]!r}") from None
        if not isinstance(payload, dict) or payload.get("success") != 1:
            raise ValueError(f"réponse JSON inattendue : {str(payload)[:160]}")
        return payload

    working = {"variant": None}

    def fetch(app_id: str):
        # la première variante qui marche est gardée pour la suite du run
        order = ([working["variant"]] if working["variant"] is not None else []) + \
                [i for i in range(len(VARIANTS)) if i != working["variant"]]
        errors = []
        for i in order:
            try:
                payload = try_variant(app_id, VARIANTS[i])
            except ValueError as exc:
                errors.append(f"variante {i} : {exc}")
                if working["variant"] is not None:   # une variante marchait déjà : ce jeu-là est juste bizarre
                    break
                continue
            if working["variant"] != i:
                log.info("Avis Steam : variante de requête %d retenue (%s)", i, VARIANTS[i])
                working["variant"] = i
            return trim(payload)
        raise ValueError(" | ".join(errors))

    return fetch


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
    session.headers["Accept"] = "*/*"     # comme un navigateur : Steam renvoie le JSON grâce à json=1

    fetch = make_fetch(session)

    fill_ref_cache("steam_reviews", ids, fetch, args.max_new, args.sleep, args.refresh_days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
