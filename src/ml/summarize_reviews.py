"""
Deal Hunter — résumés IA des avis Steam : « ce que les joueurs aiment / regrettent ».

LLM : GitHub Models (https://models.github.ai), gratuit dans GitHub Actions avec le
GITHUB_TOKEN du workflow (permission `models: read`). Aucune clé à gérer.
Le quota gratuit est limité : on borne le nombre de résumés par run (--max-new) et on
s'arrête proprement dès que l'API refuse, ce qui est fait est gardé.

Entrée : data/ref/steam_reviews/<appid>.json.gz   (steam_reviews.py)
Sortie : data/ref/review_summaries/<appid>.json.gz
         {"model", "created", "n_reviews", "summary": {"aime": [...], "regrette": [...], "en_bref": "..."}}

Le résumé n'est recalculé que si les avis ont été re-téléchargés depuis.

Usage (en local, il faut un token GitHub avec accès à GitHub Models) :
  GITHUB_TOKEN=... python src/ml/summarize_reviews.py --max-new 5
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ingestion"))
from common import REF_DIR, read_json, write_json  # noqa: E402

ENDPOINT = "https://models.github.ai/inference/chat/completions"
DEFAULT_MODEL = "openai/gpt-4.1-mini"
MIN_REVIEWS = 5
MAX_ITEMS = 3
MAX_LEN = 140

REVIEWS_DIR = REF_DIR / "steam_reviews"
OUT_DIR = REF_DIR / "review_summaries"

log = logging.getLogger("summaries")

SYSTEM = (
    "Tu résumes des avis de joueurs Steam pour un site français de bons plans. "
    "Tu t'appuies UNIQUEMENT sur les avis fournis : n'invente rien, ne cite aucun chiffre absent des avis. "
    "Les avis peuvent être dans n'importe quelle langue ; tu réponds toujours en français, "
    "avec des formulations courtes et concrètes (pas de « les joueurs trouvent que »). "
    "Si les avis sont trop vagues pour un point, mets une liste vide. "
    'Réponds uniquement en JSON : {"aime": [jusqu\'à 3 points], "regrette": [jusqu\'à 3 points], '
    '"en_bref": "une phrase qui dit pour qui est ce jeu"}'
)


class QuotaReached(Exception):
    pass


def build_prompt(title: str, reviews: list[dict]) -> str:
    lines = [f"Jeu : {title}", f"{len(reviews)} avis (les plus utiles d'abord) :"]
    for r in reviews:
        tag = "POSITIF" if r["positive"] else "NÉGATIF"
        lines.append(f"- [{tag}, {r['hours']} h de jeu] {r['text']}")
    return "\n".join(lines)


def clean(summary: dict) -> dict:
    """On ne fait pas confiance aveuglément à la sortie du modèle : formes et longueurs contrôlées."""
    def items(key):
        vals = summary.get(key) or []
        if not isinstance(vals, list):
            return []
        return [str(v).strip()[:MAX_LEN] for v in vals if str(v).strip()][:MAX_ITEMS]

    tldr = str(summary.get("en_bref") or "").strip()[:220]
    return {"aime": items("aime"), "regrette": items("regrette"), "en_bref": tldr}


def summarize(session: requests.Session, model: str, title: str, reviews: list[dict]) -> dict:
    body = {
        "model": model,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": build_prompt(title, reviews)}],
    }
    for attempt in range(1, 4):
        resp = session.post(ENDPOINT, json=body, timeout=60)
        if resp.status_code == 200:
            content = resp.json()["choices"][0]["message"]["content"]
            return clean(json.loads(content))
        if resp.status_code in (401, 403, 429):
            raise QuotaReached(f"HTTP {resp.status_code} : {resp.text[:200]}")
        if resp.status_code >= 500:
            time.sleep(5 * attempt)
            continue
        raise RuntimeError(f"HTTP {resp.status_code} : {resp.text[:300]}")
    raise RuntimeError("GitHub Models ne répond pas")


def titles_by_app() -> dict[str, str]:
    from common import latest_deals
    return {d["steamAppID"]: d["title"] for d in latest_deals() if d.get("steamAppID")}


def main() -> int:
    parser = argparse.ArgumentParser(description="Résumés IA des avis Steam (GitHub Models)")
    parser.add_argument("--max-new", type=int, default=100, help="Nb max de résumés par run. Défaut : 100")
    parser.add_argument("--sleep", type=float, default=4.5, help="Pause entre deux appels. Défaut : 4.5")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    token = os.getenv("GITHUB_TOKEN")
    if not token:
        log.warning("GITHUB_TOKEN absent : résumés ignorés (ils sont générés dans GitHub Actions).")
        return 0
    model = os.getenv("DEALHUNTER_LLM_MODEL") or DEFAULT_MODEL

    reviews_index = read_json(REVIEWS_DIR / "_index.json") if (REVIEWS_DIR / "_index.json").exists() else {}
    done_index = read_json(OUT_DIR / "_index.json") if (OUT_DIR / "_index.json").exists() else {}
    titles = titles_by_app()

    # à faire : avis présents, jeu encore en promo, et résumé absent ou plus vieux que les avis
    todo = [a for a in reviews_index if a in titles and done_index.get(a, "") < reviews_index[a]]
    log.info("%d jeux avec avis, %d déjà résumés, %d à résumer (max %d ce run, modèle %s)",
             len(reviews_index), len(done_index), len(todo), args.max_new, model)

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    done = skipped = 0
    try:
        for app_id in todo:
            if done >= args.max_new:
                break
            data = read_json(REVIEWS_DIR / f"{app_id}.json.gz")
            reviews = data.get("reviews") or []
            if len(reviews) < MIN_REVIEWS:
                done_index[app_id] = date.today().isoformat()     # pas assez d'avis : on ne réessaie pas tous les jours
                skipped += 1
                continue
            try:
                summary = summarize(session, model, titles[app_id], reviews[:25])
            except (RuntimeError, json.JSONDecodeError, KeyError) as exc:
                log.warning("Résumé impossible pour %s (%s) : on passe au suivant", app_id, exc)
                continue
            write_json(OUT_DIR / f"{app_id}.json.gz", {
                "model": model, "created": date.today().isoformat(),
                "n_reviews": len(reviews[:25]), "summary": summary,
            })
            done_index[app_id] = date.today().isoformat()
            done += 1
            if done % 20 == 0:
                write_json(OUT_DIR / "_index.json", done_index)
                log.info("%d résumés", done)
            time.sleep(args.sleep)
    except QuotaReached as exc:
        log.warning("Quota GitHub Models atteint (%s) — on reprendra au prochain run.", exc)
    finally:
        write_json(OUT_DIR / "_index.json", done_index)

    log.info("Résumés : %d nouveaux, %d jeux avec trop peu d'avis, %d au total",
             done, skipped, len(list(OUT_DIR.glob("*.json.gz"))) if OUT_DIR.exists() else 0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
