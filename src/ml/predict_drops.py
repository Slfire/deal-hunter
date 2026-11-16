"""
Deal Hunter — prédiction des baisses de prix.

Question posée au modèle, pour chaque jeu en promo aujourd'hui :
  « Le meilleur prix va-t-il baisser d'au moins 10 % dans les 14 prochains jours ? »
(l'horizon et le seuil sont les variables dbt drop_horizon_days et drop_threshold)

Étapes :
  1. Lit marts.ml_price_drop_features (construit par dbt, variables connues le jour J uniquement).
  2. Évalue le modèle honnêtement : validation croisée PAR JEU (un jeu n'est jamais à la fois dans
     l'entraînement et dans le test, sinon le modèle « reconnaît » le jeu et les scores sont faux).
  3. Garde-fou : si le modèle n'est pas assez bon (AUC < 0,65) ou qu'il manque de données,
     rien n'est publié. Le site garde alors son verdict habituel.
  4. Réentraîne sur toutes les données, prédit pour les jeux du dernier relevé et écrit
     ml.price_drop_predictions dans le warehouse (lu ensuite par export_site_data.py).
  5. Archive les prédictions du jour (data/ref/predictions/) pour mesurer plus tard si elles
     se sont réalisées, et l'historique des scores (data/ref/model_runs.json) pour la page Santé.

À lancer APRÈS build_warehouse.py et AVANT export_site_data.py.

Usage :
  python src/ml/predict_drops.py
"""

from __future__ import annotations

import gzip
import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.model_selection import GroupKFold

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / "data" / "warehouse" / "dealhunter.duckdb"
PRED_DIR = PROJECT_ROOT / "data" / "ref" / "predictions"
RUNS_FILE = PROJECT_ROOT / "data" / "ref" / "model_runs.json"
DBT_PROJECT = PROJECT_ROOT / "dbt" / "dbt_project.yml"

# Variables du jour J. Volontairement AUCUNE variable d'identité du jeu (note Steam, âge, Metacritic) :
# avec si peu d'historique, le modèle apprendrait « tel jeu a baissé » au lieu de « tel profil de prix baisse ».
FEATURES = [
    "discount",                # réduction du jour
    "ratio_to_lowest_ever",    # prix du jour / plus bas historique : la variable qui pèse le plus
    "at_lowest_ever",
    "log_retail_price",
    "days_seen_before",
    "days_since_prev_seen",
    "change_vs_prev_seen",
    "ratio_to_min_seen",
    "ratio_to_max_seen",
    "days_seen_last_7d",
    "days_on_sale_last_7d",
    "n_stores",
    "n_stores_on_sale",
    "steam_on_sale",
]

MIN_TRAIN_ROWS = 2000
MIN_POSITIVES = 100
MIN_AUC = 0.65
N_FOLDS = 5
SEED = 42

# Tranches affichées sur le site : on montre la fréquence RÉELLEMENT observée dans chaque tranche
# (validation croisée), pas la probabilité brute, plus honnête et plus parlante.
BANDS = [
    ("peu_probable", 0.00, 0.10),
    ("possible", 0.10, 0.25),
    ("assez_probable", 0.25, 0.40),
    ("probable", 0.40, 1.01),
]

log = logging.getLogger("predict")


def dbt_vars() -> dict:
    """Horizon et seuil, lus dans dbt_project.yml pour n'avoir qu'une seule source de vérité."""
    out = {"drop_horizon_days": 14, "drop_threshold": 0.10}
    if DBT_PROJECT.exists():
        for line in DBT_PROJECT.read_text(encoding="utf-8").splitlines():
            key, _, value = line.strip().partition(":")
            if key in out and value.strip():
                out[key] = type(out[key])(value.split("#")[0].strip())
    return out


def make_model() -> HistGradientBoostingClassifier:
    # Arbres de décision en série (gradient boosting). Gère seul les valeurs manquantes (NaN).
    # Réglages prudents : peu de feuilles, au moins 40 exemples par feuille, régularisation L2.
    return HistGradientBoostingClassifier(
        max_iter=200, learning_rate=0.05, max_leaf_nodes=15,
        min_samples_leaf=40, l2_regularization=1.0, random_state=SEED,
    )


def band_of(p: float) -> str:
    for name, lo, hi in BANDS:
        if lo <= p < hi:
            return name
    return BANDS[-1][0]


def load(con: duckdb.DuckDBPyConnection, where: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, list]:
    """Renvoie X (features, NaN si inconnu), y, game_id, snapshot_date pour les lignes demandées."""
    cols = ", ".join(f"coalesce(cast({c} as double), 'NaN'::double)" for c in FEATURES)
    rows = con.execute(f"""
        SELECT {cols}, coalesce(will_drop, -1), game_id, snapshot_date
        FROM marts.ml_price_drop_features WHERE {where}
        ORDER BY snapshot_date, game_id
    """).fetchall()
    if not rows:
        return np.empty((0, len(FEATURES))), np.empty(0, dtype=int), np.empty(0, dtype=int), []
    arr = np.array([r[:len(FEATURES) + 2] for r in rows], dtype=float)
    dates = [r[-1] for r in rows]
    return arr[:, :len(FEATURES)], arr[:, len(FEATURES)].astype(int), arr[:, len(FEATURES) + 1].astype(int), dates


def band_stats(y: np.ndarray, p: np.ndarray) -> list[dict]:
    out = []
    for name, lo, hi in BANDS:
        m = (p >= lo) & (p < hi)
        out.append({"band": name, "n": int(m.sum()),
                    "predicted": round(float(p[m].mean()), 4) if m.any() else None,
                    "observed": round(float(y[m].mean()), 4) if m.any() else None})
    return out


def evaluate(X: np.ndarray, y: np.ndarray, groups: np.ndarray) -> dict:
    """Validation croisée groupée par jeu : chaque prédiction est faite par un modèle qui n'a jamais vu ce jeu."""
    oof = np.zeros(len(y))
    for train_idx, test_idx in GroupKFold(n_splits=N_FOLDS).split(X, y, groups):
        assert not set(groups[train_idx]) & set(groups[test_idx])     # aucun jeu des deux côtés
        oof[test_idx] = make_model().fit(X[train_idx], y[train_idx]).predict_proba(X[test_idx])[:, 1]
    base = float(y.mean())
    return {
        "rows": int(len(y)), "games": int(len(set(groups))), "positives": int(y.sum()), "base_rate": round(base, 4),
        "auc": round(float(roc_auc_score(y, oof)), 4),
        "average_precision": round(float(average_precision_score(y, oof)), 4),
        "brier": round(float(brier_score_loss(y, oof)), 4),
        "brier_baseline": round(float(brier_score_loss(y, np.full(len(y), base))), 4),
        "bands": band_stats(y, oof),
    }


def publishable(m: dict) -> tuple[bool, str]:
    if m["rows"] < MIN_TRAIN_ROWS:
        return False, f"pas assez d'exemples ({m['rows']} < {MIN_TRAIN_ROWS})"
    if m["positives"] < MIN_POSITIVES:
        return False, f"pas assez de baisses observées ({m['positives']} < {MIN_POSITIVES})"
    if m["auc"] < MIN_AUC:
        return False, f"modèle pas assez fiable (AUC {m['auc']} < {MIN_AUC})"
    return True, "ok"


def track_record(con: duckdb.DuckDBPyConnection, horizon: int) -> dict | None:
    """Bilan réel : les prédictions archivées dont les 14 jours sont passés se sont-elles réalisées ?"""
    if not PRED_DIR.exists():
        return None
    max_date = con.execute("SELECT max(snapshot_date) FROM marts.ml_price_drop_features").fetchone()[0]
    ys, ps = [], []
    for f in sorted(PRED_DIR.glob("*.json.gz")):
        day = date.fromisoformat(f.name[:10])
        if (max_date - day).days < horizon:
            continue                                   # pas encore vérifiable
        preds = json.loads(gzip.open(f, "rt", encoding="utf-8").read())["p"]
        truth = dict(con.execute("""SELECT game_id, will_drop FROM marts.ml_price_drop_features
                                    WHERE snapshot_date = ? AND will_drop IS NOT NULL""", [day]).fetchall())
        for gid, p in preds.items():
            if int(gid) in truth:
                ys.append(truth[int(gid)])
                ps.append(p)
    if len(ys) < 200 or len(set(ys)) < 2:
        return {"checked": len(ys)}
    y, p = np.array(ys), np.array(ps)
    return {"checked": len(ys), "auc": round(float(roc_auc_score(y, p)), 4),
            "base_rate": round(float(y.mean()), 4), "bands": band_stats(y, p)}


def save_run(entry: dict) -> None:
    runs = json.loads(RUNS_FILE.read_text(encoding="utf-8")) if RUNS_FILE.exists() else []
    runs = [r for r in runs if r.get("date") != entry["date"]] + [entry]
    RUNS_FILE.parent.mkdir(parents=True, exist_ok=True)
    RUNS_FILE.write_text(json.dumps(runs[-120:], ensure_ascii=False, indent=1), encoding="utf-8")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if not DB_PATH.exists():
        log.error("Base introuvable (%s) : lance d'abord build_warehouse.py", DB_PATH)
        return 1
    v = dbt_vars()
    con = duckdb.connect(str(DB_PATH))
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS ml")
        con.execute("DROP TABLE IF EXISTS ml.price_drop_predictions")
        con.execute("DROP TABLE IF EXISTS ml.model_report")

        # les jeux gratuits ce jour-là ne peuvent pas baisser : ni appris, ni prédits
        X, y, groups, _ = load(con, "will_drop IS NOT NULL AND best_price > 0")
        X_now, _, games_now, dates_now = load(con, "is_latest AND best_price > 0")
        if len(X_now) == 0:
            log.error("Aucun jeu dans le dernier relevé")
            return 1
        today = dates_now[0]

        # pas d'évaluation possible sur trop peu de données : on s'arrête avant (et on le dit)
        metrics = {"rows": int(len(y)), "games": int(len(set(groups))), "positives": int(y.sum())}
        enough = metrics["rows"] >= MIN_TRAIN_ROWS and metrics["games"] >= N_FOLDS and 0 < metrics["positives"] < metrics["rows"]
        if enough:
            metrics = evaluate(X, y, groups)
            ok, reason = publishable(metrics)
        else:
            ok, reason = False, (f"pas assez d'historique pour entraîner le modèle ({metrics['rows']} exemples, "
                                 f"il en faut {MIN_TRAIN_ROWS})")
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "date": today.isoformat(), "horizon_days": v["drop_horizon_days"], "threshold": v["drop_threshold"],
            "published": ok, "reason": reason, "features": FEATURES, "validation": metrics,
            "track_record": track_record(con, v["drop_horizon_days"]),
        }
        log.info("Validation (par jeu) : %s", {k: metrics.get(k) for k in ("rows", "games", "base_rate", "auc", "average_precision", "brier", "brier_baseline")})

        if ok:
            p_now = make_model().fit(X, y).predict_proba(X_now)[:, 1]
            preds = [(int(g), today, round(float(p), 4), band_of(float(p))) for g, p in zip(games_now, p_now)]
            con.execute("CREATE TABLE ml.price_drop_predictions (game_id INTEGER, snapshot_date DATE, p_drop DOUBLE, band VARCHAR)")
            con.executemany("INSERT INTO ml.price_drop_predictions VALUES (?, ?, ?, ?)", preds)
            PRED_DIR.mkdir(parents=True, exist_ok=True)
            with gzip.open(PRED_DIR / f"{today.isoformat()}.json.gz", "wt", encoding="utf-8") as f:
                json.dump({"date": today.isoformat(), "horizon_days": v["drop_horizon_days"],
                           "p": {str(g): p for g, _, p, _ in preds}}, f, separators=(",", ":"))
            counts = {b: sum(1 for *_, x in preds if x == b) for b, *_ in BANDS}
            log.info("Prédictions publiées pour %d jeux : %s", len(preds), counts)
        else:
            log.warning("Prédictions NON publiées : %s", reason)

        con.execute("CREATE TABLE ml.model_report (report JSON)")
        con.execute("INSERT INTO ml.model_report VALUES (?)", [json.dumps(report, ensure_ascii=False)])
        save_run({"date": report["date"], "published": ok, "reason": reason,
                  **{k: metrics.get(k) for k in ("rows", "games", "base_rate", "auc", "average_precision", "brier")}})
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
