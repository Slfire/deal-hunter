import gzip
import json

import pytest

import common


def test_write_and_read_json_gzip(tmp_path):
    path = tmp_path / "a" / "b.json.gz"
    common.write_json(path, [{"title": "Hadès", "price": "9.99"}])
    with gzip.open(path, "rt", encoding="utf-8") as f:     # vrai gzip, lisible par DuckDB
        assert json.load(f)[0]["title"] == "Hadès"
    assert common.read_json(path) == [{"title": "Hadès", "price": "9.99"}]


def test_latest_deals_uses_most_recent_run(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "RAW_DIR", tmp_path)
    common.write_json(tmp_path / "deals/2026-10-25/060000/page_0000.json.gz", [{"gameID": "1"}])
    common.write_json(tmp_path / "deals/2026-10-26/060000/page_0000.json.gz", [{"gameID": "2", "steamAppID": "20"}])
    common.write_json(tmp_path / "deals/2026-10-26/060000/page_0001.json.gz", [{"gameID": "3", "steamAppID": None}])

    assert [d["gameID"] for d in common.latest_deals()] == ["2", "3"]
    assert common.steam_app_ids_from_latest_deals() == ["20"]


def test_fill_ref_cache_is_incremental_and_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "REF_DIR", tmp_path)
    calls = []

    def fetch(item):
        calls.append(item)
        return {"id": item}

    assert common.fill_ref_cache("src", ["1", "2", "3"], fetch, max_new=2, sleep_s=0, refresh_days=60) == 2
    assert calls == ["1", "2"]
    # deuxième run : seul le jeu manquant est récupéré
    assert common.fill_ref_cache("src", ["1", "2", "3"], fetch, max_new=2, sleep_s=0, refresh_days=60) == 1
    assert calls == ["1", "2", "3"]
    assert set(common.read_json(tmp_path / "src/_index.json")) == {"1", "2", "3"}


def test_fill_ref_cache_keeps_progress_when_source_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "REF_DIR", tmp_path)

    def fetch(item):
        if item == "2":
            raise RuntimeError("source indisponible")
        return {"id": item}

    done = common.fill_ref_cache("src", ["1", "2", "3"], fetch, max_new=10, sleep_s=0, refresh_days=60)
    assert done == 1
    assert common.read_json(tmp_path / "src/_index.json").keys() == {"1"}
    assert (tmp_path / "src/1.json.gz").exists()


def test_get_with_retry_does_not_retry_client_errors(monkeypatch):
    class Resp:
        status_code, text, url, headers = 404, "not found", "https://x", {}

    class Session:
        n = 0

        def get(self, *a, **k):
            self.n += 1
            return Resp()

    s = Session()
    with pytest.raises(RuntimeError):
        common.get_with_retry(s, "https://x")
    assert s.n == 1


def test_fill_ref_cache_skips_bad_items_and_stops_when_source_is_broken(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "REF_DIR", tmp_path)

    def fetch(item):
        if item in {"2", "3"}:
            raise ValueError("page HTML au lieu de JSON")
        return {"id": item}

    # un ou deux éléments bizarres : on les saute, on continue
    assert common.fill_ref_cache("src", ["1", "2", "3", "4"], fetch, max_new=10, sleep_s=0, refresh_days=60) == 2
    assert set(common.read_json(tmp_path / "src/_index.json")) == {"1", "4"}

    # tout échoue : arrêt après MAX_FAILURES_IN_A_ROW tentatives, pas 50
    calls = []

    def broken(item):
        calls.append(item)
        raise ValueError("source cassée")

    ids = [str(i) for i in range(100, 150)]
    assert common.fill_ref_cache("src2", ids, broken, max_new=50, sleep_s=0, refresh_days=60) == 0
    assert len(calls) == common.MAX_FAILURES_IN_A_ROW

