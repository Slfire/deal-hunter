"""
Deal Hunter — chargement RAW -> DuckDB + couche staging/marts.

Lit tous les snapshots présents dans data/raw/ et construit :
  data/warehouse/dealhunter.duckdb

Schémas :
  raw.deals / raw.stores        : JSON brut, tout en texte, + snapshot_ts et fichier source
  staging.stg_deals / stg_stores : typé et nettoyé (casts, faux zéros -> NULL, timestamps)
  marts.dim_stores               : boutiques
  marts.dim_games                : un jeu = une ligne (dernière info connue)
  marts.fct_price_history        : un prix par jeu x boutique x jour  <- le cœur du projet
  marts.current_deals            : les deals du dernier snapshot, prêts pour le site

Idempotent : chaque fichier brut n'est chargé qu'une fois (table raw._loaded_files).
Les couches staging et marts sont reconstruites entièrement à chaque run.

Usage :
  python src/transform/build_warehouse.py
  python src/transform/build_warehouse.py --full-refresh   # recharge tout le brut
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = PROJECT_ROOT / "data" / "raw"
DB_PATH = PROJECT_ROOT / "data" / "warehouse" / "dealhunter.duckdb"

log = logging.getLogger("warehouse")

# Colonnes du brut : on force tout en VARCHAR (sauf les timestamps Unix) pour ne rien
# perdre ni interpréter au chargement. Le typage se fait en staging.
DEALS_COLUMNS = {
    "internalName": "VARCHAR",
    "title": "VARCHAR",
    "metacriticLink": "VARCHAR",
    "dealID": "VARCHAR",
    "storeID": "VARCHAR",
    "gameID": "VARCHAR",
    "salePrice": "VARCHAR",
    "normalPrice": "VARCHAR",
    "isOnSale": "VARCHAR",
    "savings": "VARCHAR",
    "metacriticScore": "VARCHAR",
    "steamRatingText": "VARCHAR",
    "steamRatingPercent": "VARCHAR",
    "steamRatingCount": "VARCHAR",
    "steamAppID": "VARCHAR",
    "releaseDate": "BIGINT",
    "lastChange": "BIGINT",
    "dealRating": "VARCHAR",
    "thumb": "VARCHAR",
}

STORES_COLUMNS = {
    "storeID": "VARCHAR",
    "storeName": "VARCHAR",
    "isActive": "VARCHAR",
    "images": "JSON",
}


def duck_struct(columns: dict) -> str:
    return "{" + ", ".join(f"'{k}': '{v}'" for k, v in columns.items()) + "}"


# --------------------------------------------------------------------------
# RAW
# --------------------------------------------------------------------------
RAW_DDL = f"""
CREATE SCHEMA IF NOT EXISTS raw;
CREATE TABLE IF NOT EXISTS raw._loaded_files (
    source_file VARCHAR PRIMARY KEY,
    loaded_at   TIMESTAMP DEFAULT current_timestamp
);
CREATE TABLE IF NOT EXISTS raw.deals (
    {", ".join(f'"{k}" {v}' for k, v in DEALS_COLUMNS.items())},
    snapshot_ts TIMESTAMP,
    source_file VARCHAR
);
CREATE TABLE IF NOT EXISTS raw.stores (
    {", ".join(f'"{k}" {v}' for k, v in STORES_COLUMNS.items())},
    snapshot_ts TIMESTAMP,
    source_file VARCHAR
);
"""

# Le timestamp du snapshot est déduit du chemin :
#   deals  : data/raw/deals/2026-10-26/112925/page_0000.json
#   stores : data/raw/stores/2026-10-26/stores_112925.json
DEALS_TS = r"strptime(regexp_extract(filename, '(\d{4}-\d{2}-\d{2})/(\d{6})/', 1) || ' ' || regexp_extract(filename, '(\d{4}-\d{2}-\d{2})/(\d{6})/', 2), '%Y-%m-%d %H%M%S')"
STORES_TS = r"strptime(regexp_extract(filename, '(\d{4}-\d{2}-\d{2})/stores_(\d{6})', 1) || ' ' || regexp_extract(filename, '(\d{4}-\d{2}-\d{2})/stores_(\d{6})', 2), '%Y-%m-%d %H%M%S')"


def load_raw(con: duckdb.DuckDBPyConnection, kind: str, files: list[Path], columns: dict, ts_expr: str) -> int:
    loaded = {r[0] for r in con.execute("SELECT source_file FROM raw._loaded_files").fetchall()}
    new_files = [str(f.relative_to(PROJECT_ROOT)) for f in files if str(f.relative_to(PROJECT_ROOT)) not in loaded]
    if not new_files:
        log.info("raw.%s : aucun nouveau fichier", kind)
        return 0

    abs_files = [str(PROJECT_ROOT / f) for f in new_files]
    cols = ", ".join(f'"{k}"' for k in columns)
    con.execute(f"""
        INSERT INTO raw.{kind}
        SELECT {cols},
               {ts_expr} AS snapshot_ts,
               regexp_extract(filename, '(data/raw/.*)$', 1) AS source_file
        FROM read_json(?, columns = {duck_struct(columns)}, format = 'array', filename = true, compression = 'auto_detect')
    """, [abs_files])
    con.executemany("INSERT INTO raw._loaded_files (source_file) VALUES (?)", [[f] for f in new_files])
    n = con.execute(f"SELECT count(*) FROM raw.{kind} WHERE source_file IN (SELECT unnest(?))", [new_files]).fetchone()[0]
    log.info("raw.%s : %d fichiers, %d lignes chargées", kind, len(new_files), n)
    return n


# --------------------------------------------------------------------------
# STAGING + MARTS
# --------------------------------------------------------------------------
TRANSFORM_SQL = r"""
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS marts;

-- ---------- staging ----------
CREATE OR REPLACE TABLE staging.stg_stores AS
SELECT DISTINCT ON (store_id)
    CAST(storeID AS INTEGER)                    AS store_id,
    trim(storeName)                             AS store_name,
    isActive = '1'                              AS is_active,
    json_extract_string(images, '$.logo')       AS logo_path,
    json_extract_string(images, '$.icon')       AS icon_path,
    snapshot_ts
FROM raw.stores
ORDER BY store_id, snapshot_ts DESC;

CREATE OR REPLACE TABLE staging.stg_deals AS
SELECT
    snapshot_ts,
    CAST(snapshot_ts AS DATE)                                       AS snapshot_date,
    dealID                                                          AS deal_id,
    CAST(gameID AS INTEGER)                                         AS game_id,
    CAST(storeID AS INTEGER)                                        AS store_id,
    trim(title)                                                     AS title,
    internalName                                                    AS internal_name,
    CAST(salePrice   AS DECIMAL(10,2))                              AS sale_price,
    CAST(normalPrice AS DECIMAL(10,2))                              AS normal_price,
    isOnSale = '1'                                                  AS is_on_sale,
    round(CAST(savings AS DOUBLE), 2)                               AS savings_pct,
    -- faux zéros : 0 = "pas de note", pas une vraie note
    NULLIF(CAST(metacriticScore AS INTEGER), 0)                     AS metacritic_score,
    CASE WHEN CAST(steamRatingCount AS INTEGER) > 0
         THEN CAST(steamRatingPercent AS INTEGER) END               AS steam_rating_pct,
    NULLIF(CAST(steamRatingCount AS INTEGER), 0)                    AS steam_rating_count,
    steamRatingText                                                 AS steam_rating_text,
    CAST(steamAppID AS INTEGER)                                     AS steam_app_id,
    CASE WHEN releaseDate > 0 THEN CAST(to_timestamp(releaseDate) AS DATE) END AS release_date,
    CASE WHEN lastChange  > 0 THEN to_timestamp(lastChange)::TIMESTAMP END    AS last_change_ts,
    CAST(dealRating AS DOUBLE)                                      AS deal_rating,
    metacriticLink                                                  AS metacritic_link,
    thumb                                                           AS thumb_url,
    source_file
FROM raw.deals
-- un même deal peut apparaître sur 2 pages si l'API bouge pendant la pagination
QUALIFY row_number() OVER (PARTITION BY snapshot_ts, dealID ORDER BY source_file) = 1;

-- ---------- marts ----------
CREATE OR REPLACE TABLE marts.dim_stores AS
SELECT store_id, store_name, is_active,
       'https://www.cheapshark.com' || logo_path AS logo_url,
       'https://www.cheapshark.com' || icon_path AS icon_url
FROM staging.stg_stores;

-- un jeu = une ligne, avec la dernière info connue
CREATE OR REPLACE TABLE marts.dim_games AS
SELECT DISTINCT ON (game_id)
    game_id, title, internal_name, steam_app_id, release_date,
    metacritic_score, steam_rating_pct, steam_rating_count, steam_rating_text,
    metacritic_link, thumb_url,
    snapshot_ts AS last_seen_ts
FROM staging.stg_deals
ORDER BY game_id, snapshot_ts DESC;

-- historique des prix : 1 ligne par jeu x boutique x jour (dernier snapshot du jour)
CREATE OR REPLACE TABLE marts.fct_price_history AS
SELECT DISTINCT ON (game_id, store_id, snapshot_date)
    game_id, store_id, snapshot_date,
    sale_price, normal_price, is_on_sale, savings_pct, deal_rating, deal_id
FROM staging.stg_deals
ORDER BY game_id, store_id, snapshot_date, snapshot_ts DESC;

-- deals du dernier snapshot, enrichis, prêts pour le site
CREATE OR REPLACE TABLE marts.current_deals AS
WITH last_snap AS (SELECT max(snapshot_ts) AS ts FROM staging.stg_deals),
best_ever AS (
    SELECT game_id, min(sale_price) AS lowest_price_seen
    FROM marts.fct_price_history GROUP BY game_id
)
SELECT
    d.game_id, d.title, s.store_name, d.store_id,
    d.sale_price, d.normal_price, d.savings_pct, d.deal_rating,
    d.sale_price <= b.lowest_price_seen AS is_lowest_seen,
    g.metacritic_score, g.steam_rating_pct, g.steam_rating_count, g.release_date,
    'https://www.cheapshark.com/redirect?dealID=' || d.deal_id AS deal_url,
    d.thumb_url, d.snapshot_ts
FROM staging.stg_deals d
JOIN last_snap l ON d.snapshot_ts = l.ts
LEFT JOIN marts.dim_stores s USING (store_id)
LEFT JOIN marts.dim_games  g USING (game_id)
LEFT JOIN best_ever        b USING (game_id);
"""

CHECKS = {
    "deals sans game_id":         "SELECT count(*) FROM staging.stg_deals WHERE game_id IS NULL",
    "prix négatifs":              "SELECT count(*) FROM staging.stg_deals WHERE sale_price < 0 OR normal_price < 0",
    "prix promo > prix normal":   "SELECT count(*) FROM staging.stg_deals WHERE sale_price > normal_price",
    "boutiques inconnues":        "SELECT count(*) FROM staging.stg_deals WHERE store_id NOT IN (SELECT store_id FROM staging.stg_stores)",
    "doublons dans l'historique": "SELECT count(*) - count(DISTINCT (game_id, store_id, snapshot_date)) FROM marts.fct_price_history",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="RAW -> DuckDB (staging + marts)")
    parser.add_argument("--full-refresh", action="store_true", help="Supprime la base et recharge tout le brut")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    if args.full_refresh and DB_PATH.exists():
        DB_PATH.unlink()
        log.info("Full refresh : base supprimée")
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    deal_files = sorted(RAW_DIR.glob("deals/*/*/page_*.json*"))
    store_files = sorted(RAW_DIR.glob("stores/*/stores_*.json*"))
    if not deal_files or not store_files:
        log.error("Pas de données brutes dans %s : lance d'abord snapshot.py", RAW_DIR)
        return 1

    con = duckdb.connect(str(DB_PATH))
    try:
        con.execute(RAW_DDL)
        load_raw(con, "stores", store_files, STORES_COLUMNS, STORES_TS)
        load_raw(con, "deals", deal_files, DEALS_COLUMNS, DEALS_TS)

        con.execute(TRANSFORM_SQL)
        for table in ["staging.stg_deals", "marts.dim_stores", "marts.dim_games",
                      "marts.fct_price_history", "marts.current_deals"]:
            log.info("%-26s %6d lignes", table, con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])

        warnings = 0
        for label, sql in CHECKS.items():
            n = con.execute(sql).fetchone()[0]
            if n:
                warnings += 1
                log.warning("Contrôle qualité — %s : %d", label, n)
        if not warnings:
            log.info("Contrôles qualité : OK")
    finally:
        con.close()

    log.info("Base prête : %s", DB_PATH.relative_to(PROJECT_ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
