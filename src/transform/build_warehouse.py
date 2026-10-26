"""
Deal Hunter — chargement RAW -> DuckDB + couche staging/marts.

Lit tous les fichiers présents dans data/raw/ et data/ref/ et construit :
  data/warehouse/dealhunter.duckdb

Schémas :
  raw.deals / raw.stores / raw.games : JSON brut + snapshot_ts et fichier source (chargés une seule fois)
  raw.steam                          : fiches Steam de référence (rechargées à chaque run)
  staging.*                          : typé et nettoyé (casts, faux zéros -> NULL, timestamps)
  marts.dim_stores                   : boutiques
  marts.dim_games                    : un jeu = une ligne, enrichie (plus bas historique, genres, coop...)
  marts.fct_price_history            : un prix par jeu x boutique x jour  <- le cœur du projet
  marts.current_deals                : les deals du dernier snapshot, prêts pour le site

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
REF_DIR = PROJECT_ROOT / "data" / "ref"
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
CREATE TABLE IF NOT EXISTS raw.games (
    game_id     VARCHAR,
    payload     JSON,
    snapshot_ts TIMESTAMP,
    source_file VARCHAR
);
"""

# Le timestamp du snapshot est déduit du chemin :
#   deals  : data/raw/deals/2026-10-26/112925/page_0000.json.gz
#   games  : data/raw/games/2026-10-26/112925/batch_0000.json.gz
#   stores : data/raw/stores/2026-10-26/stores_112925.json.gz
RUN_TS = r"strptime(regexp_extract(filename, '(\d{4}-\d{2}-\d{2})/(\d{6})/', 1) || ' ' || regexp_extract(filename, '(\d{4}-\d{2}-\d{2})/(\d{6})/', 2), '%Y-%m-%d %H%M%S')"
STORES_TS = r"strptime(regexp_extract(filename, '(\d{4}-\d{2}-\d{2})/stores_(\d{6})', 1) || ' ' || regexp_extract(filename, '(\d{4}-\d{2}-\d{2})/stores_(\d{6})', 2), '%Y-%m-%d %H%M%S')"
SOURCE_FILE = r"regexp_extract(filename, '(data/raw/.*)$', 1)"


def new_files(con: duckdb.DuckDBPyConnection, files: list[Path]) -> list[str]:
    loaded = {r[0] for r in con.execute("SELECT source_file FROM raw._loaded_files").fetchall()}
    return [str(f.relative_to(PROJECT_ROOT)) for f in files if str(f.relative_to(PROJECT_ROOT)) not in loaded]


def mark_loaded(con: duckdb.DuckDBPyConnection, files: list[str]) -> None:
    con.executemany("INSERT INTO raw._loaded_files (source_file) VALUES (?)", [[f] for f in files])


def load_raw_array(con: duckdb.DuckDBPyConnection, kind: str, files: list[Path], columns: dict, ts_expr: str) -> None:
    """Fichiers au format tableau JSON ([{...}, {...}]) : deals, stores."""
    todo = new_files(con, files)
    if not todo:
        log.info("raw.%-7s aucun nouveau fichier", kind)
        return
    cols = ", ".join(f'"{k}"' for k in columns)
    before = con.execute(f"SELECT count(*) FROM raw.{kind}").fetchone()[0]
    con.execute(f"""
        INSERT INTO raw.{kind}
        SELECT {cols}, {ts_expr} AS snapshot_ts, {SOURCE_FILE} AS source_file
        FROM read_json(?, columns = {duck_struct(columns)}, format = 'array', filename = true, compression = 'auto_detect')
    """, [[str(PROJECT_ROOT / f) for f in todo]])
    mark_loaded(con, todo)
    after = con.execute(f"SELECT count(*) FROM raw.{kind}").fetchone()[0]
    log.info("raw.%-7s %d fichiers, %d lignes chargées", kind, len(todo), after - before)


def load_raw_games(con: duckdb.DuckDBPyConnection, files: list[Path]) -> None:
    """Fichiers /games?ids= : un objet {gameID: {...}} par lot -> une ligne par jeu."""
    todo = new_files(con, files)
    if not todo:
        log.info("raw.%-7s aucun nouveau fichier", "games")
        return
    before = con.execute("SELECT count(*) FROM raw.games").fetchone()[0]
    con.execute(f"""
        INSERT INTO raw.games
        SELECT j.key, j.value, {RUN_TS}, {SOURCE_FILE}
        FROM read_json_objects(?, filename = true, compression = 'auto_detect') r, json_each(r.json) j
    """, [[str(PROJECT_ROOT / f) for f in todo]])
    mark_loaded(con, todo)
    after = con.execute("SELECT count(*) FROM raw.games").fetchone()[0]
    log.info("raw.%-7s %d fichiers, %d lignes chargées", "games", len(todo), after - before)


def load_raw_steam(con: duckdb.DuckDBPyConnection) -> None:
    """Fiches Steam de référence : petit volume, on recharge tout à chaque run."""
    files = sorted(REF_DIR.glob("steam/*.json.gz"))
    con.execute("CREATE OR REPLACE TABLE raw.steam (app_id VARCHAR, payload JSON)")
    if files:
        con.execute("""
            INSERT INTO raw.steam
            SELECT j.key, j.value
            FROM read_json_objects(?, compression = 'auto_detect') r, json_each(r.json) j
        """, [[str(f) for f in files]])
    log.info("raw.%-7s %d fiches de référence", "steam", len(files))


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

-- plus bas prix historique selon CheapShark (dernière version connue par jeu)
CREATE OR REPLACE TABLE staging.stg_games_info AS
SELECT DISTINCT ON (game_id)
    CAST(game_id AS INTEGER)                                                   AS game_id,
    CAST(payload ->> '$.cheapestPriceEver.price' AS DECIMAL(10,2))             AS cheapest_ever_price,
    CASE WHEN CAST(payload ->> '$.cheapestPriceEver.date' AS BIGINT) > 0
         THEN CAST(to_timestamp(CAST(payload ->> '$.cheapestPriceEver.date' AS BIGINT)) AS DATE) END
                                                                               AS cheapest_ever_date,
    snapshot_ts
FROM raw.games
WHERE payload ->> '$.cheapestPriceEver.price' IS NOT NULL
ORDER BY game_id, snapshot_ts DESC;

-- fiches Steam : on ne garde que les jeux trouvés (success = true)
-- ids de catégories Steam (indépendants de la langue) :
--   1 multijoueur, 9 coop, 36 PvP en ligne, 38 coop en ligne, 39 coop écran partagé, 48 coop LAN, 49 PvP
CREATE OR REPLACE TABLE staging.stg_steam AS
WITH s AS (
    SELECT CAST(app_id AS INTEGER) AS steam_app_id,
           payload -> '$.data' AS d,
           CAST(json_extract_string(payload, '$.data.categories[*].id') AS INTEGER[]) AS cat_ids
    FROM raw.steam
    WHERE CAST(payload ->> '$.success' AS BOOLEAN)
)
SELECT
    steam_app_id,
    d ->> '$.type'                                      AS steam_type,
    d ->> '$.short_description'                         AS description,
    d ->> '$.header_image'                              AS header_image,
    json_extract_string(d, '$.genres[*].description')   AS genres,
    json_extract_string(d, '$.developers[*]')           AS developers,
    list_has_any(cat_ids, [9, 38, 39, 48])              AS has_coop,
    list_has_any(cat_ids, [1, 36, 49, 9, 38, 39, 48])   AS has_multi,
    CAST(d ->> '$.is_free' AS BOOLEAN)                  AS is_free
FROM s;

-- ---------- marts ----------
CREATE OR REPLACE TABLE marts.dim_stores AS
SELECT store_id, store_name, is_active,
       'https://www.cheapshark.com' || logo_path AS logo_url,
       'https://www.cheapshark.com' || icon_path AS icon_url
FROM staging.stg_stores;

-- historique des prix : 1 ligne par jeu x boutique x jour (dernier snapshot du jour)
CREATE OR REPLACE TABLE marts.fct_price_history AS
SELECT DISTINCT ON (game_id, store_id, snapshot_date)
    game_id, store_id, snapshot_date,
    sale_price, normal_price, is_on_sale, savings_pct, deal_rating, deal_id
FROM staging.stg_deals
ORDER BY game_id, store_id, snapshot_date, snapshot_ts DESC;

-- un jeu = une ligne : dernière info connue + plus bas historique + fiche Steam
CREATE OR REPLACE TABLE marts.dim_games AS
WITH latest AS (
    SELECT DISTINCT ON (game_id)
        game_id, title, internal_name, steam_app_id, release_date,
        metacritic_score, steam_rating_pct, steam_rating_count, steam_rating_text,
        metacritic_link, thumb_url, snapshot_ts AS last_seen_ts
    FROM staging.stg_deals
    ORDER BY game_id, snapshot_ts DESC
),
our_low AS (   -- notre propre relevé, au cas où CheapShark n'aurait pas l'info
    SELECT DISTINCT ON (game_id) game_id, sale_price AS our_lowest_price, snapshot_date AS our_lowest_date
    FROM marts.fct_price_history
    ORDER BY game_id, sale_price, snapshot_date DESC
),
tracked AS (
    SELECT game_id, count(DISTINCT snapshot_date) AS days_tracked FROM marts.fct_price_history GROUP BY game_id
)
SELECT
    l.*,
    t.days_tracked,
    -- plus bas prix historique : le plus bas entre CheapShark (tout l'historique) et nos relevés
    CASE WHEN i.cheapest_ever_price IS NULL OR o.our_lowest_price < i.cheapest_ever_price
         THEN o.our_lowest_price ELSE i.cheapest_ever_price END   AS lowest_ever_price,
    CASE WHEN i.cheapest_ever_price IS NULL OR o.our_lowest_price < i.cheapest_ever_price
         THEN o.our_lowest_date ELSE i.cheapest_ever_date END     AS lowest_ever_date,
    i.cheapest_ever_price IS NOT NULL                             AS has_full_history,
    s.description, s.header_image, s.genres, s.developers,
    s.has_coop, s.has_multi, s.is_free
FROM latest l
LEFT JOIN tracked t USING (game_id)
LEFT JOIN our_low o USING (game_id)
LEFT JOIN staging.stg_games_info i USING (game_id)
LEFT JOIN staging.stg_steam s USING (steam_app_id);

-- deals du dernier snapshot, enrichis, prêts pour le site
CREATE OR REPLACE TABLE marts.current_deals AS
WITH last_snap AS (SELECT max(snapshot_ts) AS ts FROM staging.stg_deals)
SELECT
    d.game_id, d.title, s.store_name, d.store_id,
    d.sale_price, d.normal_price, d.savings_pct, d.deal_rating,
    d.sale_price <= g.lowest_ever_price AS is_lowest_ever,
    'https://www.cheapshark.com/redirect?dealID=' || d.deal_id AS deal_url,
    d.thumb_url, d.snapshot_ts
FROM staging.stg_deals d
JOIN last_snap l ON d.snapshot_ts = l.ts
LEFT JOIN marts.dim_stores s USING (store_id)
LEFT JOIN marts.dim_games  g USING (game_id);
"""

CHECKS = {
    "deals sans game_id":         "SELECT count(*) FROM staging.stg_deals WHERE game_id IS NULL",
    "prix négatifs":              "SELECT count(*) FROM staging.stg_deals WHERE sale_price < 0 OR normal_price < 0",
    "prix promo > prix normal":   "SELECT count(*) FROM staging.stg_deals WHERE sale_price > normal_price",
    "boutiques inconnues":        "SELECT count(*) FROM staging.stg_deals WHERE store_id NOT IN (SELECT store_id FROM staging.stg_stores)",
    "doublons dans l'historique": "SELECT count(*) - count(DISTINCT (game_id, store_id, snapshot_date)) FROM marts.fct_price_history",
    "doublons dans dim_games":    "SELECT count(*) - count(DISTINCT game_id) FROM marts.dim_games",
}

# indicateurs de couverture : pas des erreurs, juste de quoi suivre l'enrichissement
COVERAGE = {
    "jeux avec plus bas historique CheapShark": "SELECT round(100.0 * avg(has_full_history::INT), 1) FROM marts.dim_games",
    "jeux Steam avec fiche enrichie": "SELECT round(100.0 * avg((description IS NOT NULL)::INT), 1) FROM marts.dim_games WHERE steam_app_id IS NOT NULL",
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
    game_files = sorted(RAW_DIR.glob("games/*/*/batch_*.json*"))
    if not deal_files or not store_files:
        log.error("Pas de données brutes dans %s : lance d'abord snapshot.py", RAW_DIR)
        return 1

    con = duckdb.connect(str(DB_PATH))
    try:
        con.execute(RAW_DDL)
        load_raw_array(con, "stores", store_files, STORES_COLUMNS, STORES_TS)
        load_raw_array(con, "deals", deal_files, DEALS_COLUMNS, RUN_TS)
        load_raw_games(con, game_files)
        load_raw_steam(con)

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
        for label, sql in COVERAGE.items():
            log.info("Couverture — %s : %s %%", label, con.execute(sql).fetchone()[0])
    finally:
        con.close()

    log.info("Base prête : %s", DB_PATH.relative_to(PROJECT_ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
