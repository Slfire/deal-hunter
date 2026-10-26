"""
Deal Hunter — export du warehouse DuckDB vers les JSON statiques du site.

Produit :
  site/data/deals.json             : les jeux du dernier snapshot, offres regroupées par jeu
  site/data/history/<game_id>.json : historique des prix d'un jeu (toutes boutiques)

Usage :
  python src/export/export_site_data.py
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / "data" / "warehouse" / "dealhunter.duckdb"
OUT_DIR = PROJECT_ROOT / "site" / "data"

log = logging.getLogger("export")


def to_jsonable(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v


def rows(con, sql: str) -> list[dict]:
    cur = con.execute(sql)
    cols = [c[0] for c in cur.description]
    return [{k: to_jsonable(v) for k, v in zip(cols, r)} for r in cur.fetchall()]


def dump(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))


GAMES_SQL = """
WITH offers AS (
    SELECT game_id,
           list({
               'store_id': store_id,
               'price': sale_price::DOUBLE,
               'normal': normal_price::DOUBLE,
               'savings': savings_pct,
               'rating': deal_rating,
               'lowest': is_lowest_seen,
               'url': deal_url
           } ORDER BY sale_price, store_id) AS offers,
           min(sale_price)::DOUBLE  AS best_price,
           max(normal_price)::DOUBLE AS normal_price,
           max(savings_pct)         AS best_savings,
           bool_or(is_lowest_seen)  AS is_lowest_seen
    FROM marts.current_deals
    GROUP BY game_id
),
lowest AS (
    SELECT game_id, min(sale_price)::DOUBLE AS lowest_ever, count(DISTINCT snapshot_date) AS days_tracked
    FROM marts.fct_price_history GROUP BY game_id
)
SELECT
    g.game_id          AS id,
    g.title,
    g.steam_app_id     AS steam_id,
    g.release_date     AS release,
    g.metacritic_score AS metacritic,
    g.steam_rating_pct AS steam_pct,
    g.steam_rating_count AS steam_count,
    g.steam_rating_text  AS steam_text,
    g.thumb_url        AS thumb,
    o.best_price, o.normal_price, o.best_savings, o.is_lowest_seen,
    l.lowest_ever, l.days_tracked,
    o.offers
FROM offers o
JOIN marts.dim_games g USING (game_id)
JOIN lowest l USING (game_id)
ORDER BY o.best_savings DESC, g.steam_rating_count DESC NULLS LAST
"""

HISTORY_SQL = """
SELECT game_id, snapshot_date AS d, store_id AS s, sale_price::DOUBLE AS p, normal_price::DOUBLE AS n
FROM marts.fct_price_history
WHERE game_id IN (SELECT DISTINCT game_id FROM marts.current_deals)
ORDER BY game_id, snapshot_date, store_id
"""


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if not DB_PATH.exists():
        log.error("Base introuvable (%s) : lance d'abord build_warehouse.py", DB_PATH)
        return 1

    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        snapshot_ts = con.execute("SELECT max(snapshot_ts) FROM marts.current_deals").fetchone()[0]
        stores = {
            r["store_id"]: {"name": r["store_name"], "icon": r["icon_url"]}
            for r in rows(con, "SELECT store_id, store_name, icon_url FROM marts.dim_stores WHERE is_active")
        }
        games = rows(con, GAMES_SQL)
        history = rows(con, HISTORY_SQL)
    finally:
        con.close()

    # on repart d'un dossier propre pour ne pas garder l'historique de jeux disparus
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)

    dump(OUT_DIR / "deals.json", {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "snapshot_ts": to_jsonable(snapshot_ts),
        "stores": stores,
        "games": games,
    })

    by_game: dict[int, list] = {}
    for h in history:
        by_game.setdefault(h.pop("game_id"), []).append(h)
    for game_id, points in by_game.items():
        dump(OUT_DIR / "history" / f"{game_id}.json", points)

    log.info("Export : %d jeux, %d offres, %d historiques -> %s",
             len(games), sum(len(g["offers"]) for g in games), len(by_game), OUT_DIR.relative_to(PROJECT_ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
