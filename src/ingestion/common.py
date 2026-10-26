"""Outils partagés par les scripts d'ingestion : HTTP, écriture brute, lecture du dernier snapshot."""

from __future__ import annotations

import gzip
import json
import logging
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = PROJECT_ROOT / "data" / "raw"
REF_DIR = PROJECT_ROOT / "data" / "ref"

APP_NAME = "DealHunter"
APP_VERSION = "0.2"
MAX_RETRIES = 5
TIMEOUT = 30

log = logging.getLogger("ingestion")


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")


def get_contact() -> str | None:
    load_dotenv(PROJECT_ROOT / ".env")
    return os.getenv("DEALHUNTER_CONTACT")


def build_session(contact: str) -> requests.Session:
    """Session HTTP réutilisée, avec un User-Agent descriptif (exigé par CheapShark)."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": f"{APP_NAME}/{APP_VERSION} ({contact})",
        "Accept": "application/json",
    })
    return session


class EmptyResponse(Exception):
    """L'API a répondu 200 mais sans contenu exploitable (Steam fait ça quand il limite)."""


def get_with_retry(session: requests.Session, url: str, params: dict | None = None,
                   allow_empty: bool = True) -> requests.Response:
    """GET avec retry + backoff exponentiel sur 429, 5xx, erreurs réseau (et réponses vides si allow_empty=False)."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, params=params, timeout=TIMEOUT)
        except requests.RequestException as exc:
            wait = 2 ** attempt
            log.warning("Erreur réseau (%s) — tentative %d/%d, attente %ds", exc, attempt, MAX_RETRIES, wait)
            time.sleep(wait)
            continue

        if resp.status_code == 200:
            if not allow_empty and resp.text.strip() in ("", "null"):
                wait = 2 ** attempt * 10
                log.warning("Réponse vide sur %s (limitation probable) — tentative %d/%d, attente %ds",
                            url, attempt, MAX_RETRIES, wait)
                time.sleep(wait)
                continue
            return resp

        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = resp.headers.get("Retry-After")
            wait = int(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt * 5
            log.warning("HTTP %d sur %s — tentative %d/%d, attente %ds",
                        resp.status_code, url, attempt, MAX_RETRIES, wait)
            time.sleep(wait)
            continue

        raise RuntimeError(f"HTTP {resp.status_code} sur {resp.url} : {resp.text[:300]}")

    raise RuntimeError(f"Échec après {MAX_RETRIES} tentatives : {url}")


def write_json(path: Path, payload) -> None:
    """Écrit le JSON tel quel. Si le nom finit par .gz, compresse (lisible direct par DuckDB)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "wt", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def read_json(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def latest_deals_run() -> Path | None:
    """Dossier du snapshot de deals le plus récent : data/raw/deals/<date>/<heure>/"""
    runs = sorted(p for p in RAW_DIR.glob("deals/*/*") if p.is_dir())
    return runs[-1] if runs else None


def latest_deals() -> list[dict]:
    run = latest_deals_run()
    if run is None:
        return []
    deals: list[dict] = []
    for page in sorted(run.glob("page_*.json*")):
        deals.extend(read_json(page))
    return deals
