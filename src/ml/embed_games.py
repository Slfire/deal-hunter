"""
Deal Hunter — embeddings des jeux pour la recherche en langage naturel.

Modèle : multilingual-e5-small (384 dimensions, comprend le français et l'anglais),
dans sa version ONNX quantifiée publiée par Xenova, c'est-à-dire EXACTEMENT le fichier
que Transformers.js charge dans le navigateur. Les vecteurs des jeux (calculés ici) et
celui de la requête (calculé dans le navigateur) sont donc directement comparables.

Particularité e5 : on préfixe les documents par "passage: " et les requêtes par "query: ".

Entrée  : data/warehouse/dealhunter.duckdb (jeux en promo du jour)
Sortie  : site/data/search/
            vectors.i8   int8, un vecteur de 384 valeurs par jeu, à la suite
            scales.f32   float32, un facteur d'échelle par jeu (vecteur ≈ int8 * scale)
            index.json   {"model", "dim", "ids": [game_id, ...]}  (même ordre que les vecteurs)

Le stockage en int8 divise la taille par 4 (~0,5 Mo pour 1 400 jeux) pour une perte
de précision négligeable sur le classement.

À lancer APRÈS export_site_data.py (qui recrée site/data).

Usage :
  python src/ml/embed_games.py
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import duckdb
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / "data" / "warehouse" / "dealhunter.duckdb"
OUT_DIR = PROJECT_ROOT / "site" / "data" / "search"

MODEL_REPO = "Xenova/multilingual-e5-small"
MODEL_FILE = "onnx/model_quantized.onnx"
MAX_TOKENS = 256          # la description courte Steam tient largement dedans
BATCH = 32

log = logging.getLogger("embed")

GAMES_SQL = """
SELECT g.game_id, g.title, g.genres, g.has_coop, g.has_multi, g.description, g.developers
FROM marts.dim_games g
WHERE g.game_id IN (SELECT DISTINCT game_id FROM marts.current_deals)
ORDER BY g.game_id
"""


def game_text(title, genres, coop, multi, description, developers) -> str:
    """Le texte qui représente un jeu. Ce qu'on met ici, c'est ce que la recherche « comprend »."""
    parts = [title]
    if genres:
        parts.append("Genres : " + ", ".join(genres))
    modes = []
    if coop:
        modes.append("jouable en coopération à plusieurs")
    elif multi:
        modes.append("multijoueur")
    if modes:
        parts.append("Mode : " + ", ".join(modes))
    if developers:
        parts.append("Développé par " + ", ".join(developers))
    if description:
        parts.append(description)
    return "passage: " + ". ".join(p.strip().rstrip(".") for p in parts if p) + "."


class E5Onnx:
    """multilingual-e5-small en ONNX : tokenisation + passe avant + mean pooling + normalisation."""

    def __init__(self) -> None:
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer

        model_path = hf_hub_download(MODEL_REPO, MODEL_FILE)
        tok_path = hf_hub_download(MODEL_REPO, "tokenizer.json")

        self.tokenizer = Tokenizer.from_file(tok_path)
        self.tokenizer.enable_truncation(max_length=MAX_TOKENS)
        self.tokenizer.enable_padding()
        self.session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        self.input_names = {i.name for i in self.session.get_inputs()}
        log.info("Modèle chargé : %s (%s)", MODEL_REPO, MODEL_FILE)

    def encode(self, texts: list[str]) -> np.ndarray:
        out = []
        for i in range(0, len(texts), BATCH):
            enc = self.tokenizer.encode_batch(texts[i:i + BATCH])
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            feeds = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self.input_names:
                feeds["token_type_ids"] = np.zeros_like(ids)
            hidden = self.session.run(None, feeds)[0]                 # (batch, tokens, 384)
            m = mask[..., None].astype(np.float32)
            pooled = (hidden * m).sum(axis=1) / np.clip(m.sum(axis=1), 1e-9, None)   # mean pooling
            out.append(pooled / np.linalg.norm(pooled, axis=1, keepdims=True))      # normalisation
        return np.vstack(out).astype(np.float32)


class FakeEmbedder:
    """Pour tester la plomberie sans télécharger le modèle (--fake)."""

    def encode(self, texts: list[str]) -> np.ndarray:
        rng = np.random.default_rng(0)
        v = rng.normal(size=(len(texts), 384)).astype(np.float32)
        return v / np.linalg.norm(v, axis=1, keepdims=True)


def quantize(vectors: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """int8 avec une échelle par vecteur : v ≈ q * scale."""
    scales = np.abs(vectors).max(axis=1) / 127.0
    q = np.round(vectors / scales[:, None]).clip(-127, 127).astype(np.int8)
    return q, scales.astype(np.float32)


def main() -> int:
    parser = argparse.ArgumentParser(description="Embeddings des jeux pour la recherche IA")
    parser.add_argument("--fake", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    if not DB_PATH.exists():
        log.error("Base introuvable : lance d'abord build_warehouse.py")
        return 1

    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        rows = con.execute(GAMES_SQL).fetchall()
    finally:
        con.close()
    if not rows:
        log.error("Aucun jeu à indexer")
        return 1

    ids = [r[0] for r in rows]
    texts = [game_text(*r[1:]) for r in rows]
    with_desc = sum(1 for r in rows if r[5])
    log.info("%d jeux à indexer (%d avec description Steam)", len(ids), with_desc)

    model = FakeEmbedder() if args.fake else E5Onnx()
    vectors = model.encode(texts)
    q, scales = quantize(vectors)

    # contrôle : le vecteur reconstruit doit rester quasi identique à l'original
    approx = q.astype(np.float32) * scales[:, None]
    cos = (approx * vectors).sum(axis=1) / np.linalg.norm(approx, axis=1)
    log.info("Fidélité de la quantification int8 : cosinus min %.4f", cos.min())

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "vectors.i8").write_bytes(q.tobytes())
    (OUT_DIR / "scales.f32").write_bytes(scales.astype("<f4").tobytes())
    (OUT_DIR / "index.json").write_text(json.dumps({
        "model": MODEL_REPO,
        "dim": int(vectors.shape[1]),
        "ids": ids,
        "fake": bool(args.fake),
    }), encoding="utf-8")

    size_kb = (q.nbytes + scales.nbytes) / 1024
    log.info("Index de recherche : %d vecteurs, %.0f Ko -> %s", len(ids), size_kb, OUT_DIR.relative_to(PROJECT_ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
