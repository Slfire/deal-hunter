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
import game_briefs


# ---------- jeux similaires ----------
def test_similar_games_skips_editions_of_the_same_game():
    v = np.array([[1, 0, 0], [0.99, 0.1, 0], [0.9, 0.3, 0], [0, 1, 0]], dtype=np.float32)
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    sims = embed_games.similar_games([1, 2, 3, 4], ["Hades", "Hades - Deluxe Edition", "Dead Cells", "Stardew Valley"], v, k=2)
    assert sims[1] == [3, 4]                      # la Deluxe de Hades n'est pas une recommandation
    assert 2 not in sims[1] and 1 not in sims[1]


# ---------- « Le jeu en bref » (IA) ----------
def test_clean_bounds_model_output():
    out = game_briefs.clean({"en_bref": "x" * 500, "pour_qui": "Pour les fans.", "points": ["a" * 500, "", "b", "c", "d"]})
    assert len(out["en_bref"]) == 220 and out["pour_qui"] == "Pour les fans."
    assert len(out["points"]) == 3 and len(out["points"][0]) == game_briefs.MAX_LEN
    assert game_briefs.clean({"points": "pas une liste"})["points"] == []


def test_game_source_uses_only_official_sheet_and_tags(tmp_path, monkeypatch):
    import gzip
    monkeypatch.setattr(game_briefs, "STEAM_DIR", tmp_path / "steam")
    monkeypatch.setattr(game_briefs, "SPY_DIR", tmp_path / "steamspy")
    (tmp_path / "steam").mkdir(); (tmp_path / "steamspy").mkdir()
    sheet = {"10": {"success": True, "data": {"name": "Hades", "short_description": "Défie le dieu des morts dans ce rogue-like.",
                                             "genres": [{"description": "Action"}], "categories": [{"description": "Solo"}],
                                             "developers": ["Supergiant"]}}}
    json.dump(sheet, gzip.open(tmp_path / "steam/10.json.gz", "wt"))
    json.dump({"appid": 10, "tags": {"Roguelike": 900, "Difficult": 400}}, gzip.open(tmp_path / "steamspy/10.json.gz", "wt"))
    src = game_briefs.game_source("10")
    assert src["tags_joueurs"] == ["Roguelike", "Difficult"] and src["developpeurs"] == ["Supergiant"]
    h = game_briefs.source_hash(src)
    assert h == game_briefs.source_hash(dict(src))                       # stable
    src2 = {**src, "tags_joueurs": ["Roguelike"]}
    assert game_briefs.source_hash(src2) != h                            # change si la source change
    # fiche trop pauvre ou jeu introuvable : pas de résumé
    json.dump({"11": {"success": True, "data": {"short_description": "Court."}}}, gzip.open(tmp_path / "steam/11.json.gz", "wt"))
    json.dump({"12": {"success": False}}, gzip.open(tmp_path / "steam/12.json.gz", "wt"))
    assert game_briefs.game_source("11") is None and game_briefs.game_source("12") is None and game_briefs.game_source("99") is None


def test_generate_calls_openai_compatible_api_and_parses_json():
    class Resp:
        status_code, headers, text = 200, {}, ""

        def json(self):
            return {"choices": [{"message": {"content": json.dumps(
                {"en_bref": "Un roguelike d'action.", "pour_qui": "Pour les amateurs de défi.", "points": ["Combats rapides"]})}}]}

    class Session:
        def post(self, url, json, timeout):
            assert url == "https://api.groq.com/openai/v1/chat/completions"
            assert json["model"] == "llama-3.3-70b-versatile"
            assert json["response_format"] == {"type": "json_object"}
            assert "ne prétends jamais" in json["messages"][0]["content"]
            assert "Hades" in json["messages"][1]["content"]
            return Resp()

    out = game_briefs.generate(Session(), game_briefs.DEFAULT_BASE_URL + "/", game_briefs.DEFAULT_MODEL,
                               {"titre": "Hades", "description": "..."})
    assert out["points"] == ["Combats rapides"]


def test_parse_content_tolerates_code_fences():
    fenced = '```json\n{"en_bref": "Un jeu.", "points": []}\n```'
    assert game_briefs.parse_content(fenced)["en_bref"] == "Un jeu."
    with pytest.raises(ValueError):
        game_briefs.parse_content("désolé, je ne peux pas")


def test_non_json_api_response_is_reported_clearly():
    # ce qui s'est passé avec GitHub Models retiré : HTTP 200 mais pas de JSON
    class Resp:
        status_code, headers, text = 200, {"Content-Type": "text/html"}, "<!DOCTYPE html>"

        def json(self):
            raise ValueError("Expecting value")

    class Session:
        def post(self, *a, **k):
            return Resp()

    with pytest.raises(ValueError, match="text/html"):
        game_briefs.generate(Session(), "https://x", "m", {})


@pytest.mark.parametrize("status", [401, 403, 429])
def test_generate_stops_on_auth_or_quota(status):
    class Resp:
        status_code, text, headers = status, "refusé", {}

    class Session:
        def post(self, *a, **k):
            return Resp()

    with pytest.raises(game_briefs.QuotaReached):
        game_briefs.generate(Session(), "https://x", "m", {})


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
              brief={"summary": "Un jeu de rythme.", "for_whom": "Pour les fans.", "points": ["Rythme"]})
    html = build_pages.render(g, {"1": {"name": "Steam"}}, [], [], "26 octobre 2026")
    assert "<script>alert(1)</script>" not in html
    c = _Checker()
    c.feed(html)
    assert c.metas["og:image"] == "https://example.com/h.jpg"
    assert "Tu peux attendre" in c.metas["og:description"] and "novembre 2021" in html
    assert "4,99 $" in c.title
    assert "Le jeu en bref" in html and "Ce n'est pas un avis" in html


def test_verdict_matches_site_logic():
    assert build_pages.verdict(_game(1, lowest_ever=4.99, days_tracked=1))[0] == "Bon moment pour acheter"
    assert build_pages.verdict(_game(1, lowest_ever=4.70, days_tracked=1))[0] == "Bon prix"
    assert build_pages.verdict(_game(1, lowest_ever=1.0, full_history=False, days_tracked=2))[0] == "Historique en construction"
    # cas réel du 26/10 : jeu déjà offert gratuitement -> plus bas = 0 $, plus de division par zéro
    assert build_pages.verdict(_game(1, lowest_ever=0.0, lowest_ever_date="2024-12-19", days_tracked=1))[0] == "Déjà offert gratuitement"


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
