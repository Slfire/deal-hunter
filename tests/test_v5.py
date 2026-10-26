"""Tests des fonctions ajoutées en v5 : jeux similaires, résumés IA, Discord, pages jeux, santé, soldes Steam."""
import json
from datetime import date
from html.parser import HTMLParser

import numpy as np
import pytest

import build_health
import build_pages
import embed_games
import export_site_data
import notify_discord
import summarize_reviews


# ---------- jeux similaires ----------
def test_similar_games_skips_editions_of_the_same_game():
    v = np.array([[1, 0, 0], [0.99, 0.1, 0], [0.9, 0.3, 0], [0, 1, 0]], dtype=np.float32)
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    sims = embed_games.similar_games([1, 2, 3, 4], ["Hades", "Hades - Deluxe Edition", "Dead Cells", "Stardew Valley"], v, k=2)
    assert sims[1] == [3, 4]                      # la Deluxe de Hades n'est pas une recommandation
    assert 2 not in sims[1] and 1 not in sims[1]


# ---------- résumés IA ----------
def test_clean_bounds_model_output():
    out = summarize_reviews.clean({"aime": ["a" * 500, "", "b", "c", "d"], "regrette": "pas une liste", "en_bref": "ok"})
    assert len(out["aime"]) == 3 and len(out["aime"][0]) == summarize_reviews.MAX_LEN
    assert out["regrette"] == [] and out["en_bref"] == "ok"


def test_summarize_calls_github_models_and_parses_json():
    class Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": json.dumps(
                {"aime": ["Combats nerveux"], "regrette": ["Fin abrupte"], "en_bref": "Pour les fans de roguelike."})}}]}

    class Session:
        def post(self, url, json, timeout):
            assert url == summarize_reviews.ENDPOINT
            assert json["response_format"] == {"type": "json_object"}
            assert "Jeu : Hades" in json["messages"][1]["content"]
            return Resp()

    reviews = [{"text": "Super jeu", "positive": True, "hours": 12.5}] * 5
    out = summarize_reviews.summarize(Session(), "openai/gpt-4.1-mini", "Hades", reviews)
    assert out["aime"] == ["Combats nerveux"]


def test_summarize_stops_on_quota():
    class Resp:
        status_code, text = 429, "rate limit"

    class Session:
        def post(self, *a, **k):
            return Resp()

    with pytest.raises(summarize_reviews.QuotaReached):
        summarize_reviews.summarize(Session(), "m", "Jeu", [])


# ---------- Discord ----------
def _game(i, **kw):
    g = dict(id=i, title=f"Jeu {i}", slug=f"jeu-{i}", best_price=4.99, normal_price=19.99, best_savings=75.0,
             is_lowest_ever=True, full_history=True, steam_count=i * 100, eur=5.49, tags=["Roguelike"], header=None)
    return {**g, **kw}


def test_discord_picks_new_lows_once_and_most_popular_first():
    today = date(2026, 10, 26)
    games = [_game(1), _game(2), _game(3, best_savings=20.0), _game(4, full_history=False), _game(5)]
    posted = {"5": "2026-10-20"}                                  # déjà annoncé il y a 6 jours
    picks = notify_discord.pick_games(games, posted, today)
    assert [g["id"] for g in picks] == [2, 1]


def test_discord_payload_mentions_upcoming_steam_sale():
    payload = notify_discord.build_payload([_game(1)], [{"name": "Soldes d'hiver Steam", "start": "2026-12-17",
                                                         "end": "2027-01-04"}], date(2026, 12, 12))
    assert "les soldes d'hiver Steam commencent dans 5 jours" in payload["content"]
    assert payload["embeds"][0]["url"].endswith("jeu/1-jeu-1.html")
    assert len(payload["embeds"]) <= 10


# ---------- pages jeux ----------
class _Checker(HTMLParser):
    def __init__(self):
        super().__init__()
        self.metas, self.title = {}, ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta" and ("property" in a or "name" in a):
            self.metas[a.get("property") or a.get("name")] = a.get("content")
        self._in_title = tag == "title"

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def test_game_page_has_seo_and_share_metadata_and_escapes():
    g = _game(7, title='Rock & Roll <script>alert(1)</script>', lowest_ever=2.99, lowest_ever_date="2021-11-20",
              days_tracked=3, header="https://example.com/h.jpg", desc="Un jeu.", genres=["Action"], coop=True,
              offers=[{"store_id": 1, "price": 4.99, "savings": 75.0, "url": "https://www.cheapshark.com/redirect?dealID=x"}],
              reviews={"tldr": "Pour les fans.", "likes": ["Rythme"], "dislikes": [], "n": 25})
    html = build_pages.render(g, {"1": {"name": "Steam"}}, [], [], "26 octobre 2026")
    assert "<script>alert(1)</script>" not in html
    c = _Checker()
    c.feed(html)
    assert c.metas["og:image"] == "https://example.com/h.jpg"
    assert "Tu peux attendre" in c.metas["og:description"] and "novembre 2021" in html
    assert "4,99 $" in c.title
    assert "Ils aiment" in html and "Rien de marquant" in html


def test_verdict_matches_site_logic():
    assert build_pages.verdict(_game(1, lowest_ever=4.99, days_tracked=1))[0] == "Bon moment pour acheter"
    assert build_pages.verdict(_game(1, lowest_ever=4.70, days_tracked=1))[0] == "Bon prix"
    assert build_pages.verdict(_game(1, lowest_ever=1.0, full_history=False, days_tracked=2))[0] == "Historique en construction"


# ---------- santé ----------
def test_health_reads_dbt_results(tmp_path, monkeypatch):
    rr = {"metadata": {"generated_at": "2026-10-26T06:00:00Z"}, "elapsed_time": 4.2, "results": [
        {"unique_id": "model.dealhunter.dim_games", "status": "success"},
        {"unique_id": "test.dealhunter.unique_dim_games_game_id.abc", "status": "pass"},
        {"unique_id": "test.dealhunter.assert_sale_price_not_above_normal", "status": "warn", "failures": 3},
    ]}
    path = tmp_path / "build_results.json"
    path.write_text(json.dumps(rr))
    monkeypatch.setattr(build_health, "RUN_RESULTS", path)
    out = build_health.dbt_results()
    assert (out["models_ok"], out["tests_pass"], out["tests_warn"], out["tests_fail"]) == (1, 1, 1, 0)
    assert out["problems"] == [{"test": "assert_sale_price_not_above_normal", "status": "warn", "failures": 3}]


# ---------- export ----------
def test_slugify():
    assert export_site_data.slugify("Baldur's Gate 3 : Édition Deluxe") == "baldurs-gate-3-edition-deluxe"
    assert export_site_data.slugify("!!!") == "jeu"


def test_upcoming_sales_keeps_current_and_future(tmp_path, monkeypatch):
    cal = tmp_path / "cal.json"
    cal.write_text(json.dumps({"sales": [
        {"name": "Passées", "start": "2026-10-01", "end": "2026-10-08"},
        {"name": "Hiver", "start": "2026-12-17", "end": "2027-01-04"}]}))
    monkeypatch.setattr(export_site_data, "SALES_CALENDAR", cal)
    assert [s["name"] for s in export_site_data.upcoming_sales(date(2026, 10, 26))] == ["Hiver"]
    assert [s["name"] for s in export_site_data.upcoming_sales(date(2026, 12, 20))] == ["Hiver"]
