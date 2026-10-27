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

