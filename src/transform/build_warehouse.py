"""
Deal Hunter — construction du warehouse DuckDB.

Deux temps :
  1. Python charge le BRUT dans le schéma raw (chaque fichier une seule fois, idempotent) :
       raw.deals, raw.stores          tableaux JSON CheapShark
       raw.games, raw.steam_prices    objets {id: {...}} -> une ligne par id et par snapshot
       raw.steam, raw.steamspy        cache de référence, rechargé entièrement à chaque run
  2. dbt (dossier dbt/) construit les schémas staging et marts, puis lance les tests de qualité.

Base produite : data/warehouse/dealhunter.duckdb

Usage :
  python src/transform/build_warehouse.py
  python src/transform/build_warehouse.py --full-refresh   # supprime la base et recharge tout
  python src/transform/build_warehouse.py --raw-only       # charge le brut sans lancer dbt
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = PROJECT_ROOT / "data" / "raw"
REF_DIR = PROJECT_ROOT / "data" / "ref"
DB_PATH = PROJECT_ROOT / "data" / "warehouse" / "dealhunter.duckdb"
DBT_DIR = PROJECT_ROOT / "dbt"

log = logging.getLogger("warehouse")

# Colonnes du brut : on force tout en VARCHAR (sauf les timestamps Unix) pour ne rien
# perdre ni interpréter au chargement. Le typage se fait en staging (dbt).
DEALS_COLUMNS = {
    "internalName": "VARCHAR", "title": "VARCHAR", "metacriticLink": "VARCHAR", "dealID": "VARCHAR",
    "storeID": "VARCHAR", "gameID": "VARCHAR", "salePrice": "VARCHAR", "normalPrice": "VARCHAR",
    "isOnSale": "VARCHAR", "savings": "VARCHAR", "metacriticScore": "VARCHAR", "steamRatingText": "VARCHAR",
    "steamRatingPercent": "VARCHAR", "steamRatingCount": "VARCHAR", "steamAppID": "VARCHAR",
    "releaseDate": "BIGINT", "lastChange": "BIGINT", "dealRating": "VARCHAR", "thumb": "VARCHAR",
}
STORES_COLUMNS = {"storeID": "VARCHAR", "storeName": "VARCHAR", "isActive": "VARCHAR", "images": "JSON"}


def duck_struct(columns: dict) -> str:
    return "{" + ", ".join(f"'{k}': '{v}'" for k, v in columns.items()) + "}"


RAW_DDL = f"""
CREATE SCHEMA IF NOT EXISTS raw;
CREATE TABLE IF NOT EXISTS raw._loaded_files (
    source_file VARCHAR PRIMARY KEY,
    loaded_at   TIMESTAMP DEFAULT current_timestamp
);
CREATE TABLE IF NOT EXISTS raw.deals (
    {", ".join(f'"{k}" {v}' for k, v in DEALS_COLUMNS.items())},
    snapshot_ts TIMESTAMP, source_file VARCHAR
);
CREATE TABLE IF NOT EXISTS raw.stores (
    {", ".join(f'"{k}" {v}' for k, v in STORES_COLUMNS.items())},
    snapshot_ts TIMESTAMP, source_file VARCHAR
);
CREATE TABLE IF NOT EXISTS raw.games (
    game_id VARCHAR, payload JSON, snapshot_ts TIMESTAMP, source_file VARCHAR
);
CREATE TABLE IF NOT EXISTS raw.steam_prices (
    app_id VARCHAR, payload JSON, snapshot_ts TIMESTAMP, source_file VARCHAR
);
"""

# Le timestamp du snapshot est déduit du chemin :
#   data/raw/<source>/2026-10-26/112925/page_0000.json.gz   (deals, games, steam_prices)
#   data/raw/stores/2026-10-26/stores_112925.json.gz
RUN_TS = r"strptime(regexp_extract(filename, '(\d{4}-\d{2}-\d{2})/(\d{6})/', 1) || ' ' || regexp_extract(filename, '(\d{4}-\d{2}-\d{2})/(\d{6})/', 2), '%Y-%m-%d %H%M%S')"
STORES_TS = r"strptime(regexp_extract(filename, '(\d{4}-\d{2}-\d{2})/stores_(\d{6})', 1) || ' ' || regexp_extract(filename, '(\d{4}-\d{2}-\d{2})/stores_(\d{6})', 2), '%Y-%m-%d %H%M%S')"
SOURCE_FILE = r"regexp_extract(filename, '(data/raw/.*)$', 1)"


def new_files(con: duckdb.DuckDBPyConnection, files: list[Path]) -> list[str]:
    loaded = {r[0] for r in con.execute("SELECT source_file FROM raw._loaded_files").fetchall()}
    rel = [str(f.relative_to(PROJECT_ROOT)) for f in files]
    return [f for f in rel if f not in loaded]


def count(con: duckdb.DuckDBPyConnection, table: str) -> int:
    return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def load_incremental(con: duckdb.DuckDBPyConnection, table: str, files: list[Path], select_sql: str) -> None:
    """Charge les fichiers jamais vus, puis les marque comme chargés."""
    todo = new_files(con, files)
    if not todo:
        log.info("raw.%-13s aucun nouveau fichier", table)
        return
    before = count(con, f"raw.{table}")
    con.execute(f"INSERT INTO raw.{table} {select_sql}", [[str(PROJECT_ROOT / f) for f in todo]])
    con.executemany("INSERT INTO raw._loaded_files (source_file) VALUES (?)", [[f] for f in todo])
    log.info("raw.%-13s %d fichiers, %d lignes", table, len(todo), count(con, f"raw.{table}") - before)


def array_select(columns: dict, ts_expr: str) -> str:
    """Fichiers au format tableau JSON ([{...}, {...}]) : deals, stores."""
    cols = ", ".join(f'"{k}"' for k in columns)
    return f"""
        SELECT {cols}, {ts_expr}, {SOURCE_FILE}
        FROM read_json(?, columns = {duck_struct(columns)}, format = 'array', filename = true, compression = 'auto_detect')
    """


KEYED_SELECT = f"""
    SELECT j.key, j.value, {RUN_TS}, {SOURCE_FILE}
    FROM read_json_objects(?, filename = true, compression = 'auto_detect') r, json_each(r.json) j
"""


def load_reference(con: duckdb.DuckDBPyConnection) -> None:
    """Caches de référence : petit volume, on recharge tout à chaque run."""
    steam = sorted(REF_DIR.glob("steam/*.json.gz"))
    con.execute("CREATE OR REPLACE TABLE raw.steam (app_id VARCHAR, payload JSON)")
    if steam:   # fichier = {"<appid>": {"success": ..., "data": {...}}}
        con.execute("""INSERT INTO raw.steam SELECT j.key, j.value
                       FROM read_json_objects(?, compression = 'auto_detect') r, json_each(r.json) j""",
                    [[str(f) for f in steam]])
    log.info("raw.%-13s %d fiches de référence", "steam", len(steam))

    spy = sorted(REF_DIR.glob("steamspy/*.json.gz"))
    con.execute("CREATE OR REPLACE TABLE raw.steamspy (app_id VARCHAR, payload JSON)")
    if spy:     # fichier = {"appid": ..., "tags": {...}} ; l'id est dans le nom du fichier
        con.execute(r"""INSERT INTO raw.steamspy
                        SELECT regexp_extract(filename, '(\d+)\.json', 1), json
                        FROM read_json_objects(?, filename = true, compression = 'auto_detect')""",
                    [[str(f) for f in spy]])
    log.info("raw.%-13s %d fiches de référence", "steamspy", len(spy))


def run_dbt() -> bool:
    from dbt.cli.main import dbtRunner

    os.environ["DEALHUNTER_DB"] = str(DB_PATH)
    log.info("dbt build (staging + marts + tests)…")
    res = dbtRunner().invoke(["build", "--project-dir", str(DBT_DIR), "--profiles-dir", str(DBT_DIR),
                              "--log-format", "text", "--quiet"])
    if not res.success:
        log.error("dbt build a échoué : %s", res.exception or "voir les erreurs ci-dessus")
        return False
    warnings = [r for r in res.result if getattr(r, "status", None) == "warn"]
    for r in warnings:
        log.warning("Test de qualité en alerte : %s (%s lignes)", r.node.name, r.failures)
    log.info("dbt : %d nœuds OK, %d alertes", len(res.result) - len(warnings), len(warnings))
    return True


COVERAGE = {
    "plus bas historique CheapShark": "SELECT round(100.0 * avg(has_full_history::INT), 1) FROM marts.dim_games",
    "fiche Steam (jeux Steam)": "SELECT round(100.0 * avg((description IS NOT NULL)::INT), 1) FROM marts.dim_games WHERE steam_app_id IS NOT NULL",
    "tags SteamSpy (jeux Steam)": "SELECT round(100.0 * avg((len(tags) > 0)::INT), 1) FROM marts.dim_games WHERE steam_app_id IS NOT NULL",
    "prix en euros (jeux Steam)": "SELECT round(100.0 * avg((steam_price_eur IS NOT NULL)::INT), 1) FROM marts.dim_games WHERE steam_app_id IS NOT NULL",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="RAW -> DuckDB, puis dbt (staging + marts + tests)")
    parser.add_argument("--full-refresh", action="store_true", help="Supprime la base et recharge tout le brut")
    parser.add_argument("--raw-only", action="store_true", help="Charge le brut sans lancer dbt")
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
        load_incremental(con, "stores", store_files, array_select(STORES_COLUMNS, STORES_TS))
        load_incremental(con, "deals", deal_files, array_select(DEALS_COLUMNS, RUN_TS))
        load_incremental(con, "games", sorted(RAW_DIR.glob("games/*/*/batch_*.json*")), KEYED_SELECT)
        load_incremental(con, "steam_prices", sorted(RAW_DIR.glob("steam_prices/*/*/batch_*.json*")), KEYED_SELECT)
        load_reference(con)
    finally:
        con.close()   # dbt a besoin d'ouvrir la base à son tour

    if args.raw_only:
        return 0
    if not run_dbt():
        return 1

    # même configuration que la connexion que dbt garde ouverte dans ce processus
    con = duckdb.connect(str(DB_PATH))
    try:
        for table in ["marts.dim_games", "marts.fct_price_history", "marts.current_deals"]:
            log.info("%-24s %6d lignes", table, count(con, table))
        for label, sql in COVERAGE.items():
            log.info("Couverture — %s : %s %%", label, con.execute(sql).fetchone()[0])
    finally:
        con.close()

    log.info("Base prête : %s", DB_PATH.relative_to(PROJECT_ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
