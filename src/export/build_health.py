"""
Deal Hunter — santé du pipeline (observabilité), publiée sur le site (sante.html).

Rassemble :
  - fraîcheur : date du dernier snapshot
  - volumes : deals et jeux relevés chaque jour (détecte une source qui s'effondre)
  - couverture des enrichissements : plus bas historique, fiches Steam, tags, euros, résumés IA
  - résultats des tests dbt du dernier run (dbt/target/run_results.json)
  - historique des runs : durée, volumes, tests (data/ref/pipeline_runs.jsonl, un run par ligne)

Dans GitHub Actions (GITHUB_RUN_ID présent), le run est ajouté à l'historique, qui est
committé avec les données. En local, l'historique est lu mais pas modifié.

Sortie : site/data/health.json

Usage :
  python src/export/build_health.py
  RUN_STARTED_AT=<epoch> python src/export/build_health.py   # durée du run (fourni par le workflow)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / "data" / "warehouse" / "dealhunter.duckdb"
RUN_RESULTS = PROJECT_ROOT / "dbt" / "target" / "build_results.json"   # copie faite par build_warehouse.py
RUNS_LOG = PROJECT_ROOT / "data" / "ref" / "pipeline_runs.jsonl"
OUT = PROJECT_ROOT / "site" / "data" / "health.json"
KEEP_RUNS = 60

log = logging.getLogger("health")

COVERAGE_SQL = """
SELECT
    count(*)                                                                         AS games,
    round(100.0 * avg(has_full_history::INT), 1)                                     AS lowest_ever,
    round(100.0 * avg((description IS NOT NULL)::INT) FILTER (WHERE steam_app_id IS NOT NULL), 1) AS steam_sheet,
    round(100.0 * avg((len(tags) > 0)::INT) FILTER (WHERE steam_app_id IS NOT NULL), 1)           AS tags,
    round(100.0 * avg((steam_price_eur IS NOT NULL)::INT) FILTER (WHERE steam_app_id IS NOT NULL), 1) AS eur,
    round(100.0 * avg((reviews_tldr IS NOT NULL)::INT) FILTER (WHERE steam_app_id IS NOT NULL), 1) AS reviews
FROM marts.dim_games
WHERE game_id IN (SELECT game_id FROM marts.current_deals)
"""

VOLUMES_SQL = """
SELECT snapshot_date AS day, count(*) AS deals, count(DISTINCT game_id) AS games
FROM marts.fct_price_history
GROUP BY snapshot_date ORDER BY snapshot_date DESC LIMIT 60
"""


def plain(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v


def dbt_results() -> dict:
    if not RUN_RESULTS.exists():
        return {"available": False}
    rr = json.loads(RUN_RESULTS.read_text(encoding="utf-8"))
    tests = [r for r in rr.get("results", []) if r.get("unique_id", "").startswith("test.")]
    models = [r for r in rr.get("results", []) if r.get("unique_id", "").startswith("model.")]
    status = lambda rows, s: sum(1 for r in rows if r.get("status") == s)  # noqa: E731
    return {
        "available": True,
        "generated_at": rr.get("metadata", {}).get("generated_at"),
        "elapsed_s": round(rr.get("elapsed_time", 0), 1),
        "models_ok": status(models, "success"),
        "models_error": status(models, "error"),
        "tests_pass": status(tests, "pass"),
        "tests_warn": status(tests, "warn"),
        "tests_fail": status(tests, "fail") + status(tests, "error"),
        "problems": [
            {"test": r["unique_id"].split(".")[2], "status": r["status"], "failures": r.get("failures")}
            for r in tests if r.get("status") in ("warn", "fail", "error")
        ],
    }


def read_runs() -> list[dict]:
    if not RUNS_LOG.exists():
        return []
    return [json.loads(line) for line in RUNS_LOG.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if not DB_PATH.exists():
        log.error("Base introuvable : lance d'abord build_warehouse.py")
        return 1

    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        snapshot = con.execute("SELECT max(snapshot_ts) FROM marts.current_deals").fetchone()[0]
        cur = con.execute(COVERAGE_SQL)
        coverage = {c[0]: plain(v) for c, v in zip(cur.description, cur.fetchone())}
        cur = con.execute(VOLUMES_SQL)
        cols = [c[0] for c in cur.description]
        volumes = [{k: plain(v) for k, v in zip(cols, r)} for r in cur.fetchall()][::-1]
        raw_files = con.execute("SELECT count(*) FROM raw._loaded_files").fetchone()[0]
    finally:
        con.close()

    tests = dbt_results()
    runs = read_runs()

    started = os.getenv("RUN_STARTED_AT")
    if os.getenv("GITHUB_RUN_ID"):
        runs.append({
            "run_id": os.getenv("GITHUB_RUN_ID"),
            "date": datetime.now(timezone.utc).isoformat(timespec="minutes"),
            "duration_min": round((time.time() - int(started)) / 60, 1) if started else None,
            "games": coverage["games"],
            "deals": volumes[-1]["deals"] if volumes else None,
            "tests_pass": tests.get("tests_pass"),
            "tests_warn": tests.get("tests_warn"),
            "tests_fail": tests.get("tests_fail"),
        })
        runs = runs[-KEEP_RUNS:]
        RUNS_LOG.parent.mkdir(parents=True, exist_ok=True)
        RUNS_LOG.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in runs), encoding="utf-8")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "snapshot_ts": plain(snapshot),
        "coverage": coverage,
        "volumes": volumes,
        "raw_files_loaded": raw_files,
        "dbt": tests,
        "runs": runs[-30:],
        "repo": os.getenv("GITHUB_REPOSITORY"),
    }, ensure_ascii=False), encoding="utf-8")
    log.info("Santé : %d jours de volumes, %d runs dans l'historique, tests dbt %s",
             len(volumes), len(runs), f"{tests.get('tests_pass')} OK / {tests.get('tests_warn')} alertes" if tests["available"] else "indisponibles")
    return 0


if __name__ == "__main__":
    sys.exit(main())
