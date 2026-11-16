"""Prédiction des baisses de prix : cible, absence de fuite du futur, garde-fou de publication."""
import gzip
import json
from datetime import date, timedelta

import duckdb
import numpy as np
import pytest

import build_warehouse
import predict_drops

START = date(2026, 10, 1)
DAYS = 21


def _w(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(payload, f)


def _game(price, retail=20.0, lowest=4.0):
    return {"info": {"title": "X"}, "cheapestPriceEver": {"price": str(lowest), "date": 1700000000},
            "deals": [{"storeID": "1", "dealID": "a", "price": str(price), "retailPrice": str(retail),
                       "savings": str(round(100 * (1 - price / retail), 2))},
                      {"storeID": "7", "dealID": "b", "price": str(retail), "retailPrice": str(retail), "savings": "0"}]}


@pytest.fixture
def warehouse(tmp_path, monkeypatch):
    """21 jours de relevés /games pour 3 jeux :
       jeu 1 : 10 $ puis 8 $ à partir du jour 10 (baisse de 20 %)
       jeu 2 : 5 $ tout le temps (jamais de baisse)
       jeu 3 : gratuit (ne peut pas baisser)"""
    raw = tmp_path / "data/raw"
    for i in range(DAYS):
        day = (START + timedelta(days=i)).isoformat()
        _w(raw / f"games/{day}/060000/batch_0000.json.gz",
           {"1": _game(10.0 if i < 10 else 8.0), "2": _game(5.0), "3": _game(0.0)})
    # une page de deals et de boutiques suffit pour que tous les modèles dbt se construisent
    _w(raw / f"deals/{START.isoformat()}/060000/page_0000.json.gz", [])
    _w(raw / f"stores/{START.isoformat()}/stores_060000.json.gz",
       [{"storeID": "1", "storeName": "Steam", "isActive": 1, "images": {"logo": "/l.png", "icon": "/i.png"}}])
    monkeypatch.setattr(build_warehouse, "RAW_DIR", raw)
    monkeypatch.setattr(build_warehouse, "REF_DIR", tmp_path / "data/ref")
    monkeypatch.setattr(build_warehouse, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(build_warehouse, "DB_PATH", tmp_path / "data/warehouse/test.duckdb")
    monkeypatch.setattr("sys.argv", ["build_warehouse.py"])
    assert build_warehouse.main() == 0
    return tmp_path


def _features(db):
    con = duckdb.connect(str(db))   # même configuration que la connexion encore ouverte par dbt
    rows = con.execute("""SELECT game_id, snapshot_date, will_drop, label_complete, ratio_to_min_seen, is_latest
                          FROM marts.ml_price_drop_features ORDER BY game_id, snapshot_date""").fetchall()
    con.close()
    return {(g, d): tuple(r) for g, d, *r in rows}


def test_label(warehouse):
    f = _features(build_warehouse.DB_PATH)
    d = lambda i: START + timedelta(days=i)            # noqa: E731
    # jeu 1 au jour 0 : la baisse du jour 10 tombe dans les 14 jours suivants
    assert f[(1, d(0))][:2] == (1, True)
    # jeu 1 au jour 6 : dernier jour dont la fenêtre de 14 jours est complète (6 + 14 = 20)
    assert f[(1, d(6))][:2] == (1, True)
    # jour 7 : fenêtre incomplète, pas de cible (c'est ce qu'on prédit)
    assert f[(1, d(7))][:2] == (None, False)
    # un prix qui ne bouge pas et un jeu gratuit ne baissent jamais
    assert all(f[(2, d(i))][0] == 0 for i in range(7))
    assert all(f[(3, d(i))][0] == 0 for i in range(7))
    assert sum(r[3] for r in f.values()) == 3          # le dernier jour, un jeu = une ligne « à prédire »


def test_features_never_see_the_future(warehouse):
    f = _features(build_warehouse.DB_PATH)
    # au jour 0, le jeu 1 vaut 10 $ : le minimum « déjà vu » doit être 10 $, pas les 8 $ des jours suivants
    assert f[(1, START)][2] == pytest.approx(1.0)


def test_small_data_is_not_published(warehouse, monkeypatch):
    """3 jeux, c'est bien trop peu : le garde-fou bloque la publication, le rapport le dit."""
    monkeypatch.setattr(predict_drops, "DB_PATH", build_warehouse.DB_PATH)
    monkeypatch.setattr(predict_drops, "PRED_DIR", warehouse / "data/ref/predictions")
    monkeypatch.setattr(predict_drops, "RUNS_FILE", warehouse / "data/ref/model_runs.json")
    assert predict_drops.main() == 0
    con = duckdb.connect(str(build_warehouse.DB_PATH))
    report = json.loads(con.execute("SELECT report FROM ml.model_report").fetchone()[0])
    tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'ml'").fetchall()}
    con.close()
    assert report["published"] is False and "pas assez" in report["reason"]
    assert "price_drop_predictions" not in tables
    assert not (warehouse / "data/ref/predictions").exists()


def test_bands():
    assert predict_drops.band_of(0.0) == "peu_probable"
    assert predict_drops.band_of(0.1) == "possible"
    assert predict_drops.band_of(0.39) == "assez_probable"
    assert predict_drops.band_of(1.0) == "probable"


def test_guard():
    ok = {"rows": 5000, "positives": 600, "auc": 0.8}
    assert predict_drops.publishable(ok)[0]
    assert not predict_drops.publishable({**ok, "rows": 100})[0]
    assert not predict_drops.publishable({**ok, "positives": 10})[0]
    assert not predict_drops.publishable({**ok, "auc": 0.6})[0]


def test_evaluation_is_honest():
    """Validation par jeu : un signal réel est détecté, du bruit pur reste proche du hasard (AUC 0,5)."""
    rng = np.random.default_rng(0)
    n, games = 4000, np.repeat(np.arange(400), 10)
    X = rng.normal(size=(n, len(predict_drops.FEATURES)))
    y_signal = (X[:, 1] + rng.normal(scale=0.5, size=n) > 1).astype(int)
    assert predict_drops.evaluate(X, y_signal, games)["auc"] > 0.85
    y_noise = rng.integers(0, 2, size=n)
    assert abs(predict_drops.evaluate(X, y_noise, games)["auc"] - 0.5) < 0.05
