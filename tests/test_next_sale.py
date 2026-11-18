"""v3 : promos Steam reconstituées depuis l'historique IsThereAnyDeal, grandes soldes détectées, modèle « prochaine promo »."""
import gzip
import json
from datetime import date, datetime, timedelta

import duckdb
import numpy as np
import pytest

import build_warehouse
import export_catalog
import itad_common
import predict_next_sale
import steam_catalog

START = date(2023, 1, 2)
N_GAMES = 150
SALE_EVERY = 91          # une grande soldes par trimestre, suivie par tous les jeux
SALE_LENGTH = 14
FIRST_SALE = START + timedelta(days=30)
N_SALES = len(range(0, (date(2026, 11, 1) - FIRST_SALE).days, SALE_EVERY))   # promos avant le 1er novembre 2026


def _w(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(payload, f)


@pytest.fixture
def warehouse(tmp_path, monkeypatch):
    """150 jeux à 20 € qui passent tous à -50 % le même jour, chaque trimestre, pendant 14 jours.
    Le jeu « g0 » fait en plus -75 % à sa dernière promo."""
    rows = []
    for g in range(N_GAMES):
        gid = f"g{g}"
        rows.append((gid, datetime.combine(START, datetime.min.time()), 2000, 2000, 0))
        d = FIRST_SALE
        while d < date(2026, 11, 1):
            last = d + timedelta(days=SALE_EVERY) >= date(2026, 11, 1)
            cut = 75 if (gid == "g0" and last) else 50
            rows.append((gid, datetime.combine(d, datetime.min.time()), 2000 * (100 - cut) // 100, 2000, cut))
            rows.append((gid, datetime.combine(d + timedelta(days=SALE_LENGTH), datetime.min.time()), 2000, 2000, 0))
            d += timedelta(days=SALE_EVERY)
    data = tmp_path / "data"
    itad_common.write_part(data / "ref/itad/history", rows, {"itad_id": "VARCHAR", "changed_at": "TIMESTAMP",
                           "price_cents": "INTEGER", "regular_cents": "INTEGER", "cut": "SMALLINT"}, stamp="1")
    # catalogue Steam et correspondance ITAD : le jeu g{n} a l'appid 10 * (n + 1)
    itad_common.write_part(data / "ref/catalog", [(10 * (g + 1), f"Jeu {g}", "Studio", "Éditeur", 1000 * (N_GAMES - g), 2000,
                           90, 10, 100, 2000, 2000, 5) for g in range(N_GAMES)], steam_catalog.COLUMNS, stamp="x")
    (data / "ref/catalog/part-x.parquet").rename(data / "ref/catalog/steam_catalog_2026-11-01.parquet")
    itad_common.write_part(data / "ref/itad/ids", [(10 * (g + 1), f"g{g}", datetime(2026, 11, 1)) for g in range(N_GAMES)],
                           {"appid": "INTEGER", "itad_id": "VARCHAR", "looked_up_at": "TIMESTAMP"}, stamp="1")
    # le reste du warehouse a besoin d'au moins une page de deals et de boutiques
    _w(data / f"raw/deals/{START.isoformat()}/060000/page_0000.json.gz", [])
    _w(data / f"raw/stores/{START.isoformat()}/stores_060000.json.gz",
       [{"storeID": "1", "storeName": "Steam", "isActive": 1, "images": {"logo": "/l.png", "icon": "/i.png"}}])
    monkeypatch.setattr(build_warehouse, "RAW_DIR", data / "raw")
    monkeypatch.setattr(build_warehouse, "REF_DIR", data / "ref")
    monkeypatch.setattr(build_warehouse, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(build_warehouse, "DB_PATH", data / "warehouse/test.duckdb")
    monkeypatch.setattr("sys.argv", ["build_warehouse.py"])
    assert build_warehouse.main() == 0
    return duckdb.connect(str(build_warehouse.DB_PATH))   # même configuration que la connexion encore ouverte par dbt


def test_sale_episodes(warehouse):
    n, length, cut = warehouse.execute("""
        SELECT count(*), avg(end_date - start_date), max(cut) FROM marts.fct_steam_sale_episodes WHERE itad_id = 'g1'""").fetchall()[0]
    assert n == N_SALES and length == SALE_LENGTH and cut == 50


def test_major_sales_are_detected_from_the_data(warehouse):
    days = [r[0] for r in warehouse.execute(
        "SELECT start_date FROM marts.dim_steam_major_sales WHERE origin = 'détectée' ORDER BY 1").fetchall()]
    assert len(days) == N_SALES and days[0] == FIRST_SALE


def test_samples_know_only_the_past(warehouse):
    # au jour d'observation, la prochaine promo est dans moins d'un trimestre et la dernière est passée
    rows = warehouse.execute("""
        SELECT days_since_sale_start, days_to_next_sale, last_cut FROM marts.ml_next_sale_samples
        WHERE NOT is_current AND itad_id = 'g1' AND days_to_next_sale IS NOT NULL""").fetchall()
    assert rows
    assert all(0 < since and 0 < nxt <= SALE_EVERY and cut == 50 for since, nxt, cut in rows)


def test_expected_discount_is_the_last_one(warehouse):
    # g0 a fait -75 % à sa dernière promo : c'est la réduction attendue, la fourchette va de -50 à -75 %
    row = warehouse.execute("""
        SELECT expected_cut, expected_cut_low, expected_cut_high FROM (
            SELECT arg_max(cut, start_date) expected_cut, list_min(list(cut ORDER BY start_date DESC)[1:3]) expected_cut_low,
                   list_max(list(cut ORDER BY start_date DESC)[1:3]) expected_cut_high
            FROM marts.fct_steam_sale_episodes WHERE itad_id = 'g0')""").fetchall()[0]
    assert row == (75, 50, 75)


def test_model_on_a_regular_pattern(warehouse, monkeypatch):
    """Un rythme parfaitement régulier : le modèle doit le trouver et publier ses prévisions."""
    warehouse.close()
    monkeypatch.setattr(predict_next_sale, "DB_PATH", build_warehouse.DB_PATH)
    monkeypatch.setattr(predict_next_sale, "MIN_TEST_ROWS", 50)
    assert predict_next_sale.main() == 0
    con = duckdb.connect(str(build_warehouse.DB_PATH))
    report = json.loads(con.execute("SELECT report FROM ml.next_sale_report").fetchall()[0][0])
    preds = con.execute("SELECT p14, p30, p60, p90 FROM ml.next_sale_predictions").fetchall()
    con.close()
    assert report["published"], report["reason"]
    assert all(a <= b <= c <= d for a, b, c, d in preds)          # probabilités croissantes avec l'horizon
    assert report["depth_rule"]["in_range"] > 0.9


def test_labels_only_when_the_future_is_known():
    d = {"days_to_next": np.array([10, 40, -1, 5]), "observed_after": np.array([100, 100, 100, 20]),
         "current": np.array([False, False, False, False])}
    y, known = predict_next_sale.labels(d, 30)
    assert list(y) == [1, 0, 0, 1]
    assert list(known) == [True, True, True, False]       # 20 jours observés : la cible à 30 jours est inconnue


def test_catalog_export(warehouse, monkeypatch, tmp_path):
    """Le site reçoit un index léger + des fichiers de détail, et retrouve chaque jeu dans le bon fichier."""
    warehouse.close()
    for mod in (predict_next_sale, export_catalog):
        monkeypatch.setattr(mod, "DB_PATH", build_warehouse.DB_PATH)
    monkeypatch.setattr(predict_next_sale, "MIN_TEST_ROWS", 50)
    monkeypatch.setattr(export_catalog, "OUT_DIR", tmp_path / "site/data/catalog")
    assert predict_next_sale.main() == 0 and export_catalog.main() == 0
    out = tmp_path / "site/data/catalog"
    index = json.loads((out / "index.json").read_text(encoding="utf-8"))
    assert len(index["games"]) == N_GAMES
    assert index["games"][0][1] == "Jeu 0"                         # le plus joué d'abord
    appid = index["games"][0][0]
    detail = json.loads((out / f"details/{export_catalog.shard_of(appid)}.json").read_text(encoding="utf-8"))[str(appid)]
    assert detail["expected"] == {"cut": 75, "low": 50, "high": 75}
    assert detail["forecast"] and set(detail["forecast"]) == {"14", "30", "60", "90"}
    assert len({export_catalog.shard_of(10 * k) for k in range(1, 2000)}) == 256   # appid en 0 : bien répartis

