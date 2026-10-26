"""
Deal Hunter — export des tables du warehouse en Parquet partitionné (format « lakehouse »).

Pourquoi : le fichier DuckDB est reconstruit chaque matin à partir du brut et n'est pas
conservé. Le lake Parquet, lui, est un format ouvert lisible par n'importe quel outil
(DuckDB, Spark, Polars, pandas, Power BI…), qu'on peut interroger à distance sans rien installer.

Arborescence (data/lake/ en local, et sur Cloudflare R2 si configuré) :
  price_history/year=2026/month=10/data_0.parquet   historique partitionné par mois
  dim_games/data_0.parquet
  dim_stores/data_0.parquet
  current_deals/snapshot_date=2026-10-26/data_0.parquet

Cloudflare R2 (stockage compatible S3, gratuit jusqu'à 10 Go) : activé si ces variables
d'environnement existent, sinon seul l'export local est fait :
  R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET

Exemple de lecture à distance, depuis n'importe quel DuckDB :
  SELECT * FROM read_parquet('r2://<bucket>/lake/price_history/**/*.parquet', hive_partitioning = true);

Usage :
  python src/lake/export_lake.py
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / "data" / "warehouse" / "dealhunter.duckdb"
LOCAL_LAKE = PROJECT_ROOT / "data" / "lake"

log = logging.getLogger("lake")

EXPORTS = [
    # (dossier, requête, colonnes de partition)
    ("price_history",
     "SELECT *, year(snapshot_date) AS year, month(snapshot_date) AS month FROM marts.fct_price_history",
     ["year", "month"]),
    ("dim_games", "SELECT * FROM marts.dim_games", []),
    ("dim_stores", "SELECT * FROM marts.dim_stores", []),
    ("current_deals",
     "SELECT *, CAST(snapshot_ts AS DATE) AS snapshot_date FROM marts.current_deals",
     ["snapshot_date"]),
]


def copy_all(con: duckdb.DuckDBPyConnection, root: str) -> None:
    for name, sql, parts in EXPORTS:
        target = f"{root}/{name}"
        opts = ["FORMAT parquet", "COMPRESSION zstd", "OVERWRITE_OR_IGNORE true", "FILENAME_PATTERN 'data_{i}'"]
        if parts:
            opts.append(f"PARTITION_BY ({', '.join(parts)})")
        else:
            # pas de partition : on écrit dans un dossier pour garder la même forme partout
            opts.append("PER_THREAD_OUTPUT true")
        con.execute(f"COPY ({sql}) TO '{target}' ({', '.join(opts)})")
        n = con.execute(f"SELECT count(*) FROM ({sql})").fetchone()[0]
        log.info("%-14s %7d lignes -> %s", name, n, target)


def r2_config() -> dict | None:
    keys = ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"]
    values = {k: os.getenv(k) for k in keys}
    return values if all(values.values()) else None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if not DB_PATH.exists():
        log.error("Base introuvable : lance d'abord build_warehouse.py")
        return 1

    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        LOCAL_LAKE.mkdir(parents=True, exist_ok=True)
        copy_all(con, str(LOCAL_LAKE))

        cfg = r2_config()
        if not cfg:
            log.info("Cloudflare R2 non configuré : export local uniquement (data/lake/).")
            return 0

        con.execute("INSTALL httpfs; LOAD httpfs;")
        con.execute("""CREATE SECRET r2 (TYPE r2, KEY_ID ?, SECRET ?, ACCOUNT_ID ?)""",
                    [cfg["R2_ACCESS_KEY_ID"], cfg["R2_SECRET_ACCESS_KEY"], cfg["R2_ACCOUNT_ID"]])
        copy_all(con, f"r2://{cfg['R2_BUCKET']}/lake")
        log.info("Lake publié sur Cloudflare R2 : r2://%s/lake", cfg["R2_BUCKET"])
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
