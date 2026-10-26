"""Test d'intégration : brut au format des API -> chargement -> dbt (modèles + tests de qualité)."""
import gzip
import json

import duckdb
import pytest

import build_warehouse


def _w(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(payload, f)


def _deal(game, store, sale, normal, app="100"):
    return {"internalName": "X", "title": f"Jeu {game}", "metacriticLink": None, "dealID": f"d{game}-{store}",
            "storeID": str(store), "gameID": str(game), "salePrice": str(sale), "normalPrice": str(normal),
            "isOnSale": "1", "savings": str(round(100 * (1 - sale / normal), 4)), "metacriticScore": "0",
            "steamRatingText": None, "steamRatingPercent": "0", "steamRatingCount": "0", "steamAppID": app,
            "releaseDate": 0, "lastChange": 1792975242, "dealRating": "9.0", "thumb": "https://example.com/t.jpg"}


@pytest.fixture
def project(tmp_path, monkeypatch):
    raw, ref = tmp_path / "data/raw", tmp_path / "data/ref"
    stores = [{"storeID": "1", "storeName": "Steam", "isActive": 1, "images": {"logo": "/l.png", "icon": "/i.png"}},
              {"storeID": "7", "storeName": "GOG", "isActive": 1, "images": {"logo": "/l.png", "icon": "/i.png"}}]
    for day, price in [("2026-10-25", 9.99), ("2026-10-26", 4.99)]:
        _w(raw / f"stores/{day}/stores_060000.json.gz", stores)
        _w(raw / f"deals/{day}/060000/page_0000.json.gz",
           [_deal(1, 1, price, 19.99), _deal(1, 7, 7.99, 19.99), _deal(2, 7, 2.99, 9.99, app=None)])
    _w(raw / "games/2026-10-26/060000/batch_0000.json.gz",
       {"1": {"info": {"title": "Jeu 1"}, "cheapestPriceEver": {"price": "3.99", "date": 1700000000}}})
    _w(raw / "steam_prices/2026-10-26/060000/batch_0000.json.gz",
       {"100": {"success": True, "data": {"price_overview": {"currency": "EUR", "initial": 1999, "final": 499,
                                                              "discount_percent": 75}}}})
    _w(ref / "steam/100.json.gz", {"100": {"success": True, "data": {
        "type": "game", "short_description": "Un roguelike.", "genres": [{"id": "1", "description": "Action"}],
        "categories": [{"id": 9, "description": "Coop"}], "developers": ["Studio"], "is_free": False}}})
    _w(ref / "steamspy/100.json.gz", {"appid": 100, "tags": {"Roguelike": 900, "Co-op": 300}})

    monkeypatch.setattr(build_warehouse, "RAW_DIR", raw)
    monkeypatch.setattr(build_warehouse, "REF_DIR", ref)
    monkeypatch.setattr(build_warehouse, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(build_warehouse, "DB_PATH", tmp_path / "data/warehouse/test.duckdb")
    return tmp_path


def test_full_build(project, monkeypatch):
    monkeypatch.setattr("sys.argv", ["build_warehouse.py"])
    assert build_warehouse.main() == 0

    con = duckdb.connect(str(build_warehouse.DB_PATH))
    g = con.execute("""SELECT lowest_ever_price, has_full_history, has_coop, tags, steam_price_eur, days_tracked
                       FROM marts.dim_games WHERE game_id = 1""").fetchone()
    assert float(g[0]) == 3.99          # le plus bas CheapShark (3,99) bat notre relevé (4,99)
    assert g[1] and g[2]
    assert g[3] == ["Roguelike", "Co-op"]
    assert g[4] == 4.99 and g[5] == 2
    # historique : 1 ligne par jeu x boutique x jour
    assert con.execute("SELECT count(*) FROM marts.fct_price_history").fetchone()[0] == 6
    # deals du jour seulement, et le jeu 2 (pas sur Steam) reste présent
    assert con.execute("SELECT count(DISTINCT game_id) FROM marts.current_deals").fetchone()[0] == 2
    con.close()


def test_rebuild_is_idempotent(project, monkeypatch):
    monkeypatch.setattr("sys.argv", ["build_warehouse.py"])
    assert build_warehouse.main() == 0
    assert build_warehouse.main() == 0
    con = duckdb.connect(str(build_warehouse.DB_PATH))
    assert con.execute("SELECT count(*) FROM raw.deals").fetchone()[0] == 6
    con.close()
