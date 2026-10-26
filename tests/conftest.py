"""Rend les scripts de src/ importables dans les tests (ils sont faits pour être lancés en ligne de commande)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for sub in ["ingestion", "transform", "export", "ml", "lake"]:
    sys.path.insert(0, str(ROOT / "src" / sub))
