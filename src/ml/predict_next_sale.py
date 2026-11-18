"""
Deal Hunter — v3 : quand aura lieu la prochaine promo Steam d'un jeu, et de combien ?

Pour chaque jeu du catalogue qui n'est PAS en promo aujourd'hui :
  - « quand » : la probabilité qu'une promo commence dans les 14, 30, 60 et 90 jours.
    Un modèle par horizon (gradient boosting), entraîné sur des années d'historique IsThereAnyDeal.
  - « de combien » : une RÈGLE, pas un modèle : la réduction de la dernière promo (fourchette = les 3 dernières).
    Testée contre un modèle de machine learning, la règle faisait mieux (la plupart des éditeurs répètent
    exactement la même réduction). Son taux de réussite est recalculé et publié à chaque run.

Évaluation honnête, dans le temps : on entraîne sur tout ce qui s'est passé jusqu'à il y a 6 mois,
on mesure sur les 6 derniers mois. Les dates d'entraînement et de test ne se chevauchent pas.
Garde-fou : rien n'est publié si le modèle à 30 jours a une AUC < 0,75.

Lit marts.ml_next_sale_samples (dbt). Écrit ml.next_sale_predictions et ml.next_sale_report.
À lancer APRÈS build_warehouse.py et AVANT l'export du site.

Usage :
  python src/ml/predict_next_sale.py
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import brier_score_loss, roc_auc_score

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / "data" / "warehouse" / "dealhunter.duckdb"

HORIZONS = [14, 30, 60, 90]
TEST_DAYS = 180                 # les 6 derniers mois servent de test
MIN_AUC_30 = 0.75
MIN_TEST_ROWS = 1000
SEED = 42

FEATURES = [
    "log_regular_price", "age_days", "month_of_year",
    "n_sales", "n_sales_365", "n_sales_730",
    "days_since_sale_start", "days_since_sale_end",
    "last_cut", "max_cut_all", "max_cut_365", "mean_cut_730", "mean_sale_length_730",
    "share_in_major_sales_730", "median_gap_days", "mean_gap_days_730",
    "days_to_next_major_sale", "days_since_major_sale",
]
CALIBRATION_BINS = [0.0, 0.2, 0.4, 0.6, 0.8, 1.01]

log = logging.getLogger("next_sale")


def make_model() -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(max_iter=300, learning_rate=0.08, max_leaf_nodes=31,
                                          min_samples_leaf=100, l2_regularization=1.0, random_state=SEED)


def load(con: duckdb.DuckDBPyConnection) -> dict:
    cols = ", ".join(f"coalesce(cast({c} as double), 'NaN'::double)" for c in FEATURES)
    rows = con.execute(f"""
        SELECT {cols}, coalesce(days_to_next_sale, -1), days_observed_after, observed_on, is_current, itad_id
        FROM marts.ml_next_sale_samples ORDER BY observed_on, itad_id""").fetchall()
    n = len(FEATURES)
    return {
        "X": np.array([r[:n] for r in rows], dtype=float).reshape(len(rows), n),
        "days_to_next": np.array([r[n] for r in rows], dtype=float),
        "observed_after": np.array([r[n + 1] for r in rows], dtype=float),
        "date": np.array([r[n + 2] for r in rows]),
        "current": np.array([bool(r[n + 3]) for r in rows]),
        "game": np.array([r[n + 4] for r in rows]),
    }


def labels(d: dict, horizon: int) -> tuple[np.ndarray, np.ndarray]:
    """Cible « une promo commence dans les H jours » et masque des lignes où elle est connue
    (la période de H jours après l'observation est entièrement passée)."""
    known = ~d["current"] & (d["observed_after"] >= horizon)
    y = ((d["days_to_next"] > 0) & (d["days_to_next"] <= horizon)).astype(int)
    return y, known


def calibration(y: np.ndarray, p: np.ndarray) -> list[dict]:
    out = []
    for lo, hi in zip(CALIBRATION_BINS, CALIBRATION_BINS[1:]):
        m = (p >= lo) & (p < hi)
        out.append({"from": lo, "to": min(hi, 1.0), "n": int(m.sum()),
                    "predicted": round(float(p[m].mean()), 4) if m.any() else None,
                    "observed": round(float(y[m].mean()), 4) if m.any() else None})
    return out


def evaluate(d: dict, horizon: int, split) -> dict | None:
    y, known = labels(d, horizon)
    train = known & np.array([t + timedelta(days=horizon) <= split for t in d["date"]])
    test = known & (d["date"] >= split)
    if train.sum() < 1000 or test.sum() < MIN_TEST_ROWS or len(set(y[test])) < 2:
        return None
    p = make_model().fit(d["X"][train], y[train]).predict_proba(d["X"][test])[:, 1]
    yt = y[test]
    # référence naïve : « au rythme de l'an dernier » (n promos sur 365 jours -> probabilité de Poisson)
    rate = np.nan_to_num(d["X"][test, FEATURES.index("n_sales_365")]) / 365
    naive = 1 - np.exp(-rate * horizon)
    return {
        "horizon_days": horizon, "train_rows": int(train.sum()), "test_rows": int(test.sum()),
        "base_rate": round(float(yt.mean()), 4),
        "auc": round(float(roc_auc_score(yt, p)), 4),
        "brier": round(float(brier_score_loss(yt, p)), 4),
        "naive_auc": round(float(roc_auc_score(yt, naive)), 4),
        "naive_brier": round(float(brier_score_loss(yt, naive)), 4),
        "calibration": calibration(yt, p),
    }


def depth_rule_check(con: duckdb.DuckDBPyConnection, split) -> dict | None:
    """La règle « même réduction que la dernière promo » tient-elle ? Mesuré sur les 6 derniers mois."""
    row = con.execute("""
        WITH test AS (
            SELECT s.itad_id, s.observed_on, s.next_sale_cut, s.last_cut,
                   (SELECT list(e.cut ORDER BY e.start_date DESC)[1:3] FROM marts.fct_steam_sale_episodes e
                    WHERE e.itad_id = s.itad_id AND e.start_date < s.observed_on) AS last_3
            FROM marts.ml_next_sale_samples s
            WHERE NOT s.is_current AND s.observed_on >= ? AND s.days_to_next_sale <= 90 AND s.last_cut IS NOT NULL
        )
        SELECT count(*), avg((next_sale_cut = last_cut)::int),
               avg((next_sale_cut BETWEEN list_min(last_3) AND list_max(last_3))::int),
               avg(abs(next_sale_cut - last_cut))
        FROM test""", [split]).fetchall()[0]
    if not row[0]:
        return None
    return {"checked": int(row[0]), "exact": round(float(row[1]), 4), "in_range": round(float(row[2]), 4),
            "mean_abs_error_points": round(float(row[3]), 2)}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if not DB_PATH.exists():
        log.error("Base introuvable (%s) : lance d'abord build_warehouse.py", DB_PATH)
        return 1
    con = duckdb.connect(str(DB_PATH))
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS ml")
        con.execute("DROP TABLE IF EXISTS ml.next_sale_predictions")
        con.execute("DROP TABLE IF EXISTS ml.next_sale_report")
        d = load(con)
        if not d["current"].any():
            log.warning("Aucun jeu à prédire (historique IsThereAnyDeal pas encore récupéré ?)")
            return 0
        today = max(d["date"])
        split = today - timedelta(days=TEST_DAYS)

        evaluations = [e for e in (evaluate(d, h, split) for h in HORIZONS) if e]
        for e in evaluations:
            log.info("H=%2d j : AUC %.3f (référence naïve %.3f), Brier %.4f (naïve %.4f), %d tests",
                     e["horizon_days"], e["auc"], e["naive_auc"], e["brier"], e["naive_brier"], e["test_rows"])
        auc30 = next((e["auc"] for e in evaluations if e["horizon_days"] == 30), 0.0)
        ok = auc30 >= MIN_AUC_30
        reason = "ok" if ok else (f"modèle à 30 jours pas assez fiable (AUC {auc30} < {MIN_AUC_30})" if evaluations
                                  else "pas encore assez d'historique pour évaluer le modèle")

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "date": today.isoformat(), "test_from": split.isoformat(), "published": ok, "reason": reason,
            "horizons": HORIZONS, "features": FEATURES, "evaluations": evaluations,
            "depth_rule": depth_rule_check(con, split),
            "games": int(len(set(d["game"]))), "training_rows": int((~d["current"]).sum()),
        }

        if ok:
            now = d["current"]
            probs = []
            for h in HORIZONS:
                y, known = labels(d, h)
                probs.append(make_model().fit(d["X"][known], y[known]).predict_proba(d["X"][now])[:, 1])
            # une promo « dans 30 jours » est forcément au moins aussi probable que « dans 14 jours »
            P = np.maximum.accumulate(np.vstack(probs).T, axis=1)
            rows = [(g, today, *[round(float(x), 4) for x in p]) for g, p in zip(d["game"][now], P)]
            con.execute("""CREATE TABLE ml.next_sale_predictions (itad_id VARCHAR, observed_on DATE,
                           p14 DOUBLE, p30 DOUBLE, p60 DOUBLE, p90 DOUBLE)""")
            con.executemany("INSERT INTO ml.next_sale_predictions VALUES (?, ?, ?, ?, ?, ?)", rows)
            log.info("Prévisions publiées pour %d jeux (promo probable sous 30 jours : %d)",
                     len(rows), sum(1 for r in rows if r[3] >= 0.5))
        else:
            log.warning("Prévisions NON publiées : %s", reason)
        con.execute("CREATE TABLE ml.next_sale_report (report JSON)")
        con.execute("INSERT INTO ml.next_sale_report VALUES (?)", [json.dumps(report, ensure_ascii=False)])
        if report["depth_rule"]:
            r = report["depth_rule"]
            log.info("Règle « même réduction que la dernière fois » : exacte %.0f %%, dans la fourchette %.0f %%",
                     100 * r["exact"], 100 * r["in_range"])
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
