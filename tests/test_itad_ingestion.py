"""Collecte v3 : catalogue SteamSpy et IsThereAnyDeal, testés sur de vraies réponses enregistrées par la sonde."""
import json
from datetime import datetime, timedelta
from pathlib import Path

import itad_common
import itad_history
import itad_ids
import itad_prices
import steam_catalog

FIX = Path(__file__).parent / "fixtures" / "itad"
load = lambda name: json.loads((FIX / name).read_text(encoding="utf-8"))   # noqa: E731


def test_catalog_keeps_only_paid_games():
    rows = steam_catalog.parse_page(load("steamspy_page.json"))
    assert len(rows) == 15                                   # 5 jeux gratuits écartés
    palworld = next(r for r in rows if r[1] == "Palworld")
    assert palworld[4:6] == (50_000_000, 100_000_000)        # propriétaires min / max
    assert palworld[9] == 2999                               # prix en cents de dollar


def test_owners_parsing():
    assert steam_catalog.parse_owners("1,000 .. 2,000") == (1000, 2000)
    assert steam_catalog.parse_owners("n'importe quoi") == (0, 0)


def test_lookup_parsing():
    assert itad_ids.parse_lookup(load("lookup.json"))[1145360] == "018d937f-33f0-7200-80fc-87f769196c84"
    assert itad_ids.parse_lookup({"app/1": None, "sub/2": "x"}) == {1: None}


def test_lookup_retries_only_old_misses():
    now = datetime(2026, 11, 16)
    known = {1: ("uuid", now), 2: (None, now - timedelta(days=5)), 3: (None, now - timedelta(days=40))}
    assert itad_ids.todo_appids([1, 2, 3, 4], known, now) == [3, 4]


def test_history_parsing_on_hades():
    rows = itad_history.parse_history("hades", load("history_hades.json"))
    assert len(rows) == 108
    first = min(rows, key=lambda r: r[1])
    assert first[1].year == 2019                             # depuis la sortie du jeu
    assert all(r[1].tzinfo is None for r in rows)            # tout en UTC, sans fuseau
    assert max(r[4] for r in rows) == 75                     # meilleure promo : -75 %
    assert all(0 < r[2] <= r[3] for r in rows)               # prix > 0 et jamais au-dessus du prix normal


def test_prices_parsing():
    parsed = itad_prices.parse_prices(load("prices_5_games.json"))
    assert len(parsed) == 5
    steam_price, steam_regular, steam_cut, best_price, *_ = next(iter(parsed.values()))
    assert steam_price <= steam_regular and best_price <= steam_price


def test_only_changes_are_written():
    now = datetime(2026, 11, 16)
    previous = {"a": (100, 100, 0), "b": (100, 100, 0)}
    current = {"a": (100, 100, 0), "b": (50, 100, 50), "c": (10, 10, 0)}
    assert [r[0] for r in itad_prices.changed_rows(current, previous, now)] == ["b", "c"]


def test_parts_roundtrip(tmp_path):
    cols = {"itad_id": "VARCHAR", "n": "INTEGER"}
    itad_common.write_part(tmp_path, [("a", 1), ("b", 2)], cols, stamp="1")
    itad_common.write_part(tmp_path, [("a", 3)], cols, stamp="2")
    assert itad_common.write_part(tmp_path, [], cols) is None          # rien à écrire : pas de fichier vide
    rows = itad_common.read_parts(tmp_path, "SELECT itad_id, max(n) FROM parts GROUP BY 1 ORDER BY 1")
    assert rows == [("a", 3), ("b", 2)]


def test_rate_limiter_pauses(monkeypatch):
    clock = {"t": 0.0}
    slept = []
    monkeypatch.setattr(itad_common.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(itad_common.time, "sleep", lambda s: slept.append(s) or clock.__setitem__("t", clock["t"] + s))
    limiter = itad_common.RateLimiter(max_calls=3, window_s=10)
    for _ in range(4):
        limiter.wait()
    assert len(slept) == 1 and 10 <= slept[0] <= 11       # le 4e appel attend la fin de la fenêtre
