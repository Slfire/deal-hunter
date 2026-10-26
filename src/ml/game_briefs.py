"""
Deal Hunter — « Le jeu en bref » : résumé IA de chaque jeu, en français.

Source du résumé : UNIQUEMENT la fiche officielle Steam (description, genres, modes de jeu,
développeur) et les tags posés par les joueurs (SteamSpy). Ce n'est pas un résumé des avis :
le site l'affiche comme « résumé IA de la fiche du jeu ».
(L'endpoint public des avis Steam, /appreviews, redirige vers l'accueil depuis octobre 2026.)

LLM : GitHub Models (https://models.github.ai), gratuit dans GitHub Actions avec le
GITHUB_TOKEN du workflow (permission `models: read`). Aucune clé à gérer.
Quota gratuit limité : nombre de résumés borné par run, jeux les plus populaires d'abord,
arrêt propre dès que l'API refuse (ce qui est fait est gardé).

Entrées : data/ref/steam/<appid>.json.gz, data/ref/steamspy/<appid>.json.gz
Sortie  : data/ref/game_briefs/<appid>.json.gz
          {"model", "created", "source_hash", "brief": {"en_bref", "pour_qui", "points": [...]}}

Un résumé n'est recalculé que si sa source (fiche + tags) a changé.

Usage (en local, il faut un token GitHub avec accès à GitHub Models) :
  GITHUB_TOKEN=... python src/ml/game_briefs.py --max-new 5
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ingestion"))
from common import REF_DIR, latest_deals, read_json, write_json  # noqa: E402

ENDPOINT = "https://models.github.ai/inference/chat/completions"
DEFAULT_MODEL = "openai/gpt-4.1-mini"
MAX_POINTS = 3
MAX_LEN = 140
MIN_DESCRIPTION = 40          # en dessous, la fiche est trop pauvre pour un résumé utile

STEAM_DIR = REF_DIR / "steam"
SPY_DIR = REF_DIR / "steamspy"
OUT_DIR = REF_DIR / "game_briefs"

log = logging.getLogger("briefs")

SYSTEM = (
    "Tu présentes des jeux vidéo PC pour un site français de bons plans. "
    "Tu t'appuies UNIQUEMENT sur la fiche officielle et les tags fournis : n'invente rien, "
    "aucune note, aucun chiffre, aucune comparaison absente de la fiche, et ne prétends jamais "
    "rapporter l'avis des joueurs. Les tags sont souvent en anglais : traduis-les en français. "
    "Style : phrases courtes, concrètes, sans superlatifs publicitaires. "
    'Réponds uniquement en JSON : {"en_bref": "une phrase qui dit ce qu\'est le jeu", '
    '"pour_qui": "une phrase qui commence par Pour", "points": [jusqu\'à 3 caractéristiques marquantes]}'
)


class QuotaReached(Exception):
    pass


def game_source(app_id: str) -> dict | None:
    """Ce que le modèle a le droit de lire pour ce jeu. None si la fiche est absente ou trop pauvre."""
    path = STEAM_DIR / f"{app_id}.json.gz"
    if not path.exists():
        return None
    entry = read_json(path).get(app_id) or {}
    if not entry.get("success"):
        return None
    d = entry.get("data") or {}
    desc = (d.get("short_description") or "").strip()
    if len(desc) < MIN_DESCRIPTION:
        return None

    tags: list[str] = []
    spy = SPY_DIR / f"{app_id}.json.gz"
    if spy.exists():
        t = read_json(spy).get("tags")
        if isinstance(t, dict):
            tags = list(t)[:12]

    return {
        "titre": d.get("name"),
        "description": desc,
        "genres": [g.get("description") for g in d.get("genres") or []],
        "modes": [c.get("description") for c in d.get("categories") or []][:8],
        "developpeurs": d.get("developers") or [],
        "tags_joueurs": tags,
    }


def source_hash(src: dict) -> str:
    return hashlib.sha1(json.dumps(src, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]


def build_prompt(src: dict) -> str:
    return "Fiche du jeu :\n" + json.dumps(src, ensure_ascii=False, indent=1)


def clean(brief: dict) -> dict:
    """On ne fait pas confiance aveuglément à la sortie du modèle : formes et longueurs contrôlées."""
    points = brief.get("points") or []
    if not isinstance(points, list):
        points = []
    return {
        "en_bref": str(brief.get("en_bref") or "").strip()[:220],
        "pour_qui": str(brief.get("pour_qui") or "").strip()[:180],
        "points": [str(p).strip()[:MAX_LEN] for p in points if str(p).strip()][:MAX_POINTS],
    }


def generate(session: requests.Session, model: str, src: dict) -> dict:
    body = {
        "model": model,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": build_prompt(src)}],
    }
    for attempt in range(1, 4):
        resp = session.post(ENDPOINT, json=body, timeout=60)
        if resp.status_code == 200:
            content = resp.json()["choices"][0]["message"]["content"]
            out = clean(json.loads(content))
            if not out["en_bref"]:
                raise ValueError("réponse vide")
            return out
        if resp.status_code in (401, 403, 429):
            raise QuotaReached(f"HTTP {resp.status_code} : {resp.text[:200]}")
        if resp.status_code >= 500:
            time.sleep(5 * attempt)
            continue
        raise RuntimeError(f"HTTP {resp.status_code} : {resp.text[:300]}")
    raise RuntimeError("GitHub Models ne répond pas")


def popular_app_ids() -> list[str]:
    """Jeux Steam du dernier snapshot, du plus au moins évalué : leurs résumés servent le plus."""
    counts: dict[str, int] = {}
    for d in latest_deals():
        app = d.get("steamAppID")
        if app:
            counts[app] = max(counts.get(app, 0), int(d.get("steamRatingCount") or 0))
    return sorted(counts, key=lambda a: -counts[a])


def main() -> int:
    parser = argparse.ArgumentParser(description="« Le jeu en bref » par IA (GitHub Models)")
    parser.add_argument("--max-new", type=int, default=100, help="Nb max de résumés par run. Défaut : 100")
    parser.add_argument("--sleep", type=float, default=4.5, help="Pause entre deux appels. Défaut : 4.5")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    token = os.getenv("GITHUB_TOKEN")
    if not token:
        log.warning("GITHUB_TOKEN absent : résumés ignorés (ils sont générés dans GitHub Actions).")
        return 0
    model = os.getenv("DEALHUNTER_LLM_MODEL") or DEFAULT_MODEL

    index = read_json(OUT_DIR / "_index.json") if (OUT_DIR / "_index.json").exists() else {}   # appid -> hash source
    todo, skipped = [], 0
    for app_id in popular_app_ids():
        src = game_source(app_id)
        if src is None:
            skipped += 1
            continue
        h = source_hash(src)
        if index.get(app_id) != h:
            todo.append((app_id, src, h))
    log.info("%d résumés à jour, %d à (re)générer (max %d ce run, modèle %s), %d jeux sans fiche exploitable",
             len(index), len(todo), args.max_new, model, skipped)

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    done = failed = 0
    try:
        for app_id, src, h in todo[:args.max_new]:
            try:
                brief = generate(session, model, src)
            except (RuntimeError, ValueError, KeyError) as exc:
                failed += 1
                log.warning("Résumé impossible pour %s (%s) : on passe au suivant", app_id, exc)
                continue
            write_json(OUT_DIR / f"{app_id}.json.gz",
                       {"model": model, "created": date.today().isoformat(), "source_hash": h, "brief": brief})
            index[app_id] = h
            done += 1
            if done % 20 == 0:
                write_json(OUT_DIR / "_index.json", index)
                log.info("%d résumés", done)
            time.sleep(args.sleep)
    except QuotaReached as exc:
        log.warning("Quota GitHub Models atteint (%s) — on reprendra au prochain run.", exc)
    finally:
        if done:
            write_json(OUT_DIR / "_index.json", index)

    log.info("Jeu en bref : %d nouveaux, %d échecs, %d au total", done, failed, len(index))
    return 0


if __name__ == "__main__":
    sys.exit(main())
