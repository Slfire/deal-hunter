import xml.dom.minidom
from datetime import datetime, timezone

import numpy as np

import embed_games
import export_site_data


def test_game_text_puts_tags_first_and_uses_e5_prefix():
    text = embed_games.game_text("Hades", ["Action"], ["Roguelike", "Difficult"], True, True,
                                 "Défie le dieu des morts.", ["Supergiant"])
    assert text.startswith("passage: Hades. Tags : Roguelike, Difficult")
    assert "coopération" in text and "Supergiant" in text and text.endswith(".")


def test_game_text_handles_missing_enrichment():
    assert embed_games.game_text("Jeu inconnu", None, [], None, None, None, None) == "passage: Jeu inconnu."


def test_int8_quantization_keeps_ranking():
    rng = np.random.default_rng(1)
    v = rng.normal(size=(200, 384)).astype(np.float32)
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    q, scales = embed_games.quantize(v)
    approx = q.astype(np.float32) * scales[:, None]
    query = v[7]
    assert np.argmax(approx @ query) == 7
    assert np.corrcoef(approx @ query, v @ query)[0, 1] > 0.999


def _game(**kw):
    base = dict(id=1, title="Jeu", best_price=4.99, best_savings=80.0, is_lowest_ever=True, full_history=True,
                steam_count=1000, eur=5.49, tags=["Roguelike"])
    return {**base, **kw}


def test_feed_selects_real_all_time_lows_and_escapes_xml():
    games = [
        _game(id=1, title="Rock & Roll <Deluxe>"),
        _game(id=2, is_lowest_ever=False),           # pas au plus bas
        _game(id=3, best_savings=30.0),              # réduction trop faible
        _game(id=4, full_history=False),             # plus bas sur nos seuls relevés : pas assez fiable
    ]
    feed = export_site_data.build_feed(games, datetime(2026, 10, 26, 6, tzinfo=timezone.utc))
    xml.dom.minidom.parseString(feed)                # XML valide malgré & et <>
    assert feed.count("<item>") == 1
    assert "jeu/1-rock-roll-deluxe.html" in feed and "dealhunter-1-2026-10-26" in feed
