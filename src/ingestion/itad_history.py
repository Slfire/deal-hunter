"""
Deal Hunter — historique complet des prix Steam de chaque jeu (IsThereAnyDeal).

GET /games/history/v2?id=<uuid>&country=FR&shops=61&since=2010-01-01 renvoie chaque changement de prix
du jeu sur Steam France depuis sa sortie (sans « since », ITAD ne renvoie que 3 mois).
Une requête par jeu : pour ~75 000 jeux et 800 requêtes / 5 min, le rattrapage complet prend ~8 heures
de requêtes. Il est découpé en runs bornés dans le temps (--minutes), les jeux les plus joués d'abord.

Un jeu n'est récupéré qu'UNE fois : ensuite, ses changements de prix sont suivis chaque jour par
itad_prices.py (beaucoup moins cher : 200 jeux par requête).

Sorties (morceaux jamais réécrits) :
  data/ref/itad/history/part-<horodatage>.parquet      itad_id, changed_at, price_cents, regular_cents, cut
  data/ref/itad/history_log/part-<horodatage>.parquet  itad_id, fetched_at, n_changes   (qui a déjà été fait)

Usage :
  python src/ingestion/itad_history.py --minutes 45
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

from common import log, setup_logging
from itad_common import (COUNTRY, ITAD_DIR, STEAM_SHOP_ID, Itad, ItadError, catalog_appids_by_popularity, itad_ids,
                         now_stamp, read_parts, write_part)

SINCE = "2010-01-01T00:00:00Z"
FLUSH_EVERY = 1000          # écrit un morceau tous les 1 000 jeux : un run coupé ne perd presque rien
MAX_ERRORS_IN_A_ROW = 10

HISTORY_COLUMNS = {"itad_id": "VARCHAR", "changed_at": "TIMESTAMP", "price_cents": "INTEGER",
                   "regular_cents": "INTEGER", "cut": "SMALLINT"}
LOG_COLUMNS = {"itad_id": "VARCHAR", "fetched_at": "TIMESTAMP", "n_changes": "INTEGER"}


def parse_history(itad_id: str, payload: list) -> list[tuple]:
    """Réponse ITAD -> lignes compactes. Horodatages convertis en UTC (sans fuseau)."""
    rows = []
    for e in payload or []:
        if (e.get("shop") or {}).get("id") != STEAM_SHOP_ID:
            continue
        deal = e.get("deal") or {}
        try:
            ts = datetime.fromisoformat(e["timestamp"]).astimezone(timezone.utc).replace(tzinfo=None)
            rows.append((itad_id, ts, int(deal["price"]["amountInt"]), int(deal["regular"]["amountInt"]), int(deal.get("cut") or 0)))
        except (KeyError, TypeError, ValueError):
            continue
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Rattrapage de l'historique des prix Steam (ITAD)")
    parser.add_argument("--minutes", type=float, default=45, help="Durée maximale de ce run")
    parser.add_argument("--max-games", type=int, default=100_000)
    args = parser.parse_args()
    setup_logging()
    deadline = time.monotonic() + args.minutes * 60

    ids = itad_ids()
    done = {r[0] for r in read_parts(ITAD_DIR / "history_log", "SELECT DISTINCT itad_id FROM parts")}
    order = [ids[a] for a in catalog_appids_by_popularity() if a in ids]
    todo = [i for i in dict.fromkeys(order) if i not in done][:args.max_games]
    log.info("%d jeux associés à ITAD, %d historiques déjà récupérés, %d restants", len(ids), len(done), len(todo))
    if not todo:
        return 0
    try:
        itad = Itad()
    except ItadError as exc:
        log.error("%s", exc)
        return 1

    history, fetched = [], []
    total_games = total_rows = errors = 0

    def flush() -> None:
        nonlocal history, fetched
        stamp = now_stamp()
        write_part(ITAD_DIR / "history", history, HISTORY_COLUMNS, stamp)
        write_part(ITAD_DIR / "history_log", fetched, LOG_COLUMNS, stamp)
        history, fetched = [], []

    try:
        for gid in todo:
            if time.monotonic() > deadline:
                log.info("Temps écoulé pour ce run : la suite au prochain")
                break
            try:
                payload = itad.request("GET", "/games/history/v2",
                                       params={"id": gid, "country": COUNTRY, "shops": STEAM_SHOP_ID, "since": SINCE})
                errors = 0
            except ItadError as exc:
                errors += 1
                log.warning("Historique de %s ignoré : %s", gid, exc)
                if errors >= MAX_ERRORS_IN_A_ROW:
                    log.error("%d erreurs d'affilée : arrêt de ce run", errors)
                    break
                continue
            rows = parse_history(gid, payload)
            history += rows
            fetched.append((gid, datetime.now(timezone.utc).replace(tzinfo=None), len(rows)))
            total_games += 1
            total_rows += len(rows)
            if total_games % FLUSH_EVERY == 0:
                flush()
                log.info("%d jeux, %d changements de prix", total_games, total_rows)
    finally:
        flush()
    log.info("Run terminé : %d jeux, %d changements de prix, %d restants", total_games, total_rows, len(todo) - total_games)
    return 0


if __name__ == "__main__":
    sys.exit(main())
