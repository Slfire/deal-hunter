"""
Deal Hunter — export du warehouse DuckDB vers les fichiers statiques du site.

Produit :
  site/data/deals.json             les jeux du dernier snapshot, offres regroupées par jeu
  site/data/history/<game_id>.json historique des prix d'un jeu (toutes boutiques)
  site/data/insights.json          statistiques pour la page Insights
  site/feed.xml                    flux RSS des meilleurs « plus bas historiques » du jour

Usage :
  python src/export/export_site_data.py
  SITE_URL=https://mon-domaine.fr/ python src/export/export_site_data.py
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / "data" / "warehouse" / "dealhunter.duckdb"
SITE_DIR = PROJECT_ROOT / "site"
OUT_DIR = SITE_DIR / "data"
SITE_URL = os.getenv("SITE_URL", "https://slfire.github.io/deal-hunter/").rstrip("/") + "/"

FEED_SIZE = 30

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
               'lowest': is_lowest_ever,
               'url': deal_url
           } ORDER BY sale_price, store_id) AS offers,
           min(sale_price)::DOUBLE   AS best_price,
           max(normal_price)::DOUBLE AS normal_price,
           max(savings_pct)          AS best_savings,
           bool_or(is_lowest_ever)   AS is_lowest_ever
    FROM marts.current_deals
    GROUP BY game_id
)
SELECT
    g.game_id            AS id,
    g.title,
    g.steam_app_id       AS steam_id,
    g.release_date       AS release,
    g.metacritic_score   AS metacritic,
    g.steam_rating_pct   AS steam_pct,
    g.steam_rating_count AS steam_count,
    g.thumb_url          AS thumb,
    g.header_image       AS header,
    g.description        AS "desc",
    g.genres,
    g.tags,
    g.developers         AS devs,
    g.has_coop           AS coop,
    g.has_multi          AS multi,
    g.steam_price_eur    AS eur,
    g.steam_normal_eur   AS eur_normal,
    o.best_price, o.normal_price, o.best_savings, o.is_lowest_ever,
    g.lowest_ever_price::DOUBLE AS lowest_ever,
    g.lowest_ever_date   AS lowest_ever_date,
    g.has_full_history   AS full_history,
    g.days_tracked,
    o.offers
FROM offers o
JOIN marts.dim_games g USING (game_id)
ORDER BY o.best_savings DESC, g.steam_rating_count DESC NULLS LAST
"""

HISTORY_SQL = """
SELECT game_id, snapshot_date AS d, store_id AS s, sale_price::DOUBLE AS p, normal_price::DOUBLE AS n
FROM marts.fct_price_history
WHERE game_id IN (SELECT DISTINCT game_id FROM marts.current_deals)
ORDER BY game_id, snapshot_date, store_id
"""

INSIGHTS_SQL = {
    "stores": "SELECT * FROM marts.insights_store_stats WHERE games_on_sale >= 5 ORDER BY games_on_sale DESC",
    # répartition des réductions (meilleure offre par jeu), par tranches de 10 %
    "discounts": """
        WITH best AS (SELECT game_id, max(savings_pct) AS s FROM marts.current_deals GROUP BY game_id)
        SELECT least(floor(s / 10) * 10, 90)::INT AS bucket, count(*) AS games
        FROM best GROUP BY bucket ORDER BY bucket
    """,
    # tags les plus fréquents parmi les jeux en promo
    "tags": """
        SELECT tag, count(*) AS games
        FROM (SELECT unnest(tags) AS tag FROM marts.dim_games
              WHERE game_id IN (SELECT game_id FROM marts.current_deals))
        GROUP BY tag ORDER BY games DESC, tag LIMIT 15
    """,
    "summary": """
        WITH best AS (
            SELECT game_id, max(savings_pct) AS s, bool_or(is_lowest_ever) AS low, count(*) AS n
            FROM marts.current_deals GROUP BY game_id
        )
        SELECT count(*) AS games,
               round(avg(s), 1) AS avg_best_savings,
               count(*) FILTER (WHERE low) AS lowest_ever_games,
               count(*) FILTER (WHERE n > 1) AS multi_store_games,
               (SELECT count(DISTINCT snapshot_date) FROM marts.fct_price_history) AS days_tracked
        FROM best
    """,
}


def build_feed(games: list[dict], snapshot_ts) -> str:
    """Flux RSS : les plus bas historiques du jour, réduction ≥ 50 %, les jeux les plus populaires d'abord."""
    picks = [g for g in games if g["is_lowest_ever"] and g["full_history"] and (g["best_savings"] or 0) >= 50]
    picks.sort(key=lambda g: -(g["steam_count"] or 0))
    picks = picks[:FEED_SIZE]

    snap = snapshot_ts if isinstance(snapshot_ts, datetime) else datetime.now(timezone.utc)
    if snap.tzinfo is None:
        snap = snap.replace(tzinfo=timezone.utc)
    day = snap.date().isoformat()

    items = []
    for g in picks:
        price = f"{g['best_price']:.2f} $".replace(".", ",")
        eur = f" (Steam : {g['eur']:.2f} €)".replace(".", ",") if g.get("eur") is not None else ""
        desc = f"{price}{eur}, −{round(g['best_savings'])} %. Plus bas prix de son histoire."
        if g.get("tags"):
            desc += " " + ", ".join(g["tags"][:4]) + "."
        items.append(f"""    <item>
      <title>{escape(g['title'])} à {escape(price)} (−{round(g['best_savings'])} %)</title>
      <link>{escape(SITE_URL)}#jeu-{g['id']}</link>
      <guid isPermaLink="false">dealhunter-{g['id']}-{day}</guid>
      <pubDate>{format_datetime(snap)}</pubDate>
      <description>{escape(desc)}</description>
    </item>""")

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Deal Hunter — plus bas historiques du jour</title>
    <link>{escape(SITE_URL)}</link>
    <description>Les jeux PC qui atteignent aujourd'hui le prix le plus bas de leur histoire.</description>
    <language>fr-fr</language>
    <lastBuildDate>{format_datetime(snap)}</lastBuildDate>
{chr(10).join(items)}
  </channel>
</rss>
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
        insights = {k: rows(con, sql) for k, sql in INSIGHTS_SQL.items()}
    finally:
        con.close()

    # on repart d'un dossier propre pour ne pas garder l'historique de jeux disparus
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)

    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    dump(OUT_DIR / "deals.json", {
        "generated_at": generated,
        "snapshot_ts": to_jsonable(snapshot_ts),
        "stores": stores,
        "genres": sorted({x for g in games for x in (g["genres"] or [])}, key=str.casefold),
        "games": games,
    })

    by_game: dict[int, list] = {}
    for h in history:
        by_game.setdefault(h.pop("game_id"), []).append(h)
    for game_id, points in by_game.items():
        dump(OUT_DIR / "history" / f"{game_id}.json", points)

    insights["summary"] = insights["summary"][0] if insights["summary"] else {}
    insights["snapshot_ts"] = to_jsonable(snapshot_ts)
    dump(OUT_DIR / "insights.json", insights)

    feed = build_feed(games, snapshot_ts)
    (SITE_DIR / "feed.xml").write_text(feed, encoding="utf-8")

    log.info("Export : %d jeux, %d offres, %d historiques, %d articles RSS -> site/",
             len(games), sum(len(g["offers"]) for g in games), len(by_game), feed.count("<item>"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
