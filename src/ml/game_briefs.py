"""
Deal Hunter — « Le jeu en bref » : résumé IA de chaque jeu, en français.

Source du résumé : UNIQUEMENT la fiche officielle Steam (description, genres, modes de jeu,
développeur) et les tags posés par les joueurs (SteamSpy). Ce n'est pas un résumé des avis :
le site l'affiche comme « résumé IA de la fiche du jeu ».
(L'endpoint public des avis Steam, /appreviews, redirige vers l'accueil depuis octobre 2026.)

LLM : n'importe quelle API compatible OpenAI (/chat/completions). Par défaut Groq, gratuit
sans carte bancaire (https://console.groq.com) avec Llama 3.3 70B. Configuration :
  LLM_API_KEY   la clé (secret du repo)            — sans clé, l'étape ne fait rien
  LLM_BASE_URL  défaut https://api.groq.com/openai/v1   (Mistral : https://api.mistral.ai/v1)
  LLM_MODEL     défaut llama-3.3-70b-versatile          (Mistral : mistral-small-latest)
(GitHub Models, utilisé au départ, a été retiré par GitHub le 30 juillet 2026.)
Quota gratuit limité : nombre de résumés borné par run, jeux les plus populaires d'abord,
arrêt propre si l'API refuse ou échoue plusieurs fois d'affilée (ce qui est fait est gardé).

Entrées : data/ref/steam/<appid>.json.gz, data/ref/steamspy/<appid>.json.gz
Sortie  : data/ref/game_briefs/<appid>.json.gz
          {"model", "created", "source_hash", "brief": {"en_bref", "pour_qui", "points": [...]}}

Un résumé n'est recalculé que si sa source (fiche + tags) a changé.

Usage :
  LLM_API_KEY=gsk_... python src/ml/game_briefs.py --max-new 5
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

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "llama-3.3-70b-versatile"
# Les fournisseurs renouvellent leur catalogue : on demande la liste des modèles disponibles
# et on prend le premier de cette liste de préférence (bons en français, gratuits chez Groq).
PREFERRED_MODELS = [
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-120b",
    "meta-llama/llama-4-maverick-17b-128e-instruct",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "qwen/qwen3-32b",
    "openai/gpt-oss-20b",
    "llama-3.1-8b-instant",
]
NOT_CHAT = ("whisper", "tts", "guard", "embed", "playai", "orpheus", "distil")
MAX_FAILURES_IN_A_ROW = 5
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


def parse_content(content: str) -> dict:
    """Le JSON du modèle, même s'il l'a entouré de ```json ... ``` malgré la consigne."""
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"pas de JSON dans la réponse du modèle : {content[:160]!r}")
    return json.loads(text[start:end + 1])


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


def available_models(session: requests.Session, base_url: str) -> list[str]:
    """Modèles proposés par le fournisseur (GET /models). Liste vide si l'API ne le permet pas."""
    try:
        resp = session.get(base_url.rstrip("/") + "/models", timeout=30)
        if resp.status_code != 200:
            return []
        return [m["id"] for m in resp.json().get("data", []) if m.get("id")]
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return []


def pick_model(wanted: str | None, available: list[str]) -> str | None:
    """Le modèle demandé s'il existe, sinon le premier disponible de la liste de préférence,
    sinon n'importe quel modèle de chat. None si rien ne convient."""
    if not available:                      # on ne peut pas vérifier : on fait confiance
        return wanted or DEFAULT_MODEL
    if wanted and wanted in available:
        return wanted
    for m in PREFERRED_MODELS:
        if m in available:
            return m
    chat = [m for m in available if not any(x in m.lower() for x in NOT_CHAT)]
    return chat[0] if chat else None


def generate(session: requests.Session, base_url: str, model: str, src: dict) -> dict:
    body = {
        "model": model,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": build_prompt(src)}],
    }
    url = base_url.rstrip("/") + "/chat/completions"
    for attempt in range(1, 4):
        resp = session.post(url, json=body, timeout=60)
        if resp.status_code == 200:
            try:
                content = resp.json()["choices"][0]["message"]["content"]
            except (ValueError, KeyError, IndexError, TypeError):
                raise ValueError(f"réponse inattendue de l'API : HTTP 200, "
                                 f"{resp.headers.get('Content-Type', 'sans type')}, début : {resp.text[:160]!r}") from None
            out = clean(parse_content(content))
            if not out["en_bref"]:
                raise ValueError("résumé vide")
            return out
        if resp.status_code in (401, 403):
            raise QuotaReached(f"accès refusé, vérifie la clé LLM_API_KEY (HTTP {resp.status_code} : {resp.text[:160]})")
        if resp.status_code == 429:
            wait = int(resp.headers.get("retry-after", "0") or 0)
            if 0 < wait <= 60 and attempt < 3:      # limite par minute : on patiente un peu
                time.sleep(wait)
                continue
            raise QuotaReached(f"quota atteint (HTTP 429 : {resp.text[:160]})")
        if resp.status_code >= 500:
            time.sleep(5 * attempt)
            continue
        if resp.status_code == 400 and "response_format" in body and "json" in resp.text.lower():
            body.pop("response_format")       # modèle sans « mode JSON » : la consigne + parse_content suffisent
            continue
        raise RuntimeError(f"HTTP {resp.status_code} : {resp.text[:300]}")
    raise RuntimeError("l'API ne répond pas")


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

    token = os.getenv("LLM_API_KEY")
    if not token:
        log.warning("LLM_API_KEY absent : « Le jeu en bref » ignoré (voir le README pour créer une clé Groq gratuite).")
        return 0
    base_url = os.getenv("LLM_BASE_URL") or DEFAULT_BASE_URL
    wanted = os.getenv("LLM_MODEL") or None

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    models = available_models(session, base_url)
    model = pick_model(wanted, models)
    if models:
        log.info("%d modèles disponibles chez le fournisseur", len(models))
        if wanted and model != wanted:
            log.warning("Modèle demandé « %s » indisponible : on utilise « %s »", wanted, model)
    if model is None:
        log.error("Aucun modèle de chat disponible chez %s : %s", base_url, ", ".join(models[:20]))
        return 0

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
    log.info("%d résumés à jour, %d à (re)générer (max %d ce run, modèle %s via %s), %d jeux sans fiche exploitable",
             len(index), len(todo), args.max_new, model, base_url, skipped)

    done = failed = in_a_row = 0
    try:
        for app_id, src, h in todo[:args.max_new]:
            try:
                brief = generate(session, base_url, model, src)
            except (RuntimeError, ValueError, KeyError) as exc:
                failed += 1
                in_a_row += 1
                log.warning("Résumé impossible pour %s (%s) : on passe au suivant", app_id, exc)
                if in_a_row >= MAX_FAILURES_IN_A_ROW:
                    log.error("%d échecs d'affilée : l'API ou sa réponse a changé, arrêt pour ce run", in_a_row)
                    break
                time.sleep(args.sleep)
                continue
            in_a_row = 0
            write_json(OUT_DIR / f"{app_id}.json.gz",
                       {"model": model, "created": date.today().isoformat(), "source_hash": h, "brief": brief})
            index[app_id] = h
            done += 1
            if done % 20 == 0:
                write_json(OUT_DIR / "_index.json", index)
                log.info("%d résumés", done)
            time.sleep(args.sleep)
    except QuotaReached as exc:
        log.warning("Arrêt : %s — on reprendra au prochain run.", exc)
    finally:
        if done:
            write_json(OUT_DIR / "_index.json", index)

    log.info("Jeu en bref : %d nouveaux, %d échecs, %d au total", done, failed, len(index))
    return 0


if __name__ == "__main__":
    sys.exit(main())
