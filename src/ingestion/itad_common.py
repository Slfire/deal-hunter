"""
Outils partagés pour IsThereAnyDeal (ITAD) et les fichiers Parquet « par morceaux ».

Pourquoi des morceaux (parts) ?
  Le catalogue complet, c'est ~75 000 jeux. Réécrire chaque jour un gros fichier ferait gonfler le repo git
  (git garde toutes les versions). On n'écrit donc que du NOUVEAU, dans un petit fichier horodaté
  (data/ref/itad/history/part-20261116T150000.parquet…), jamais réécrit ensuite. Le warehouse relit tous
  les morceaux d'un coup avec DuckDB (read_parquet('…/*.parquet')).

Limite ITAD : 1 000 requêtes par tranche de 5 minutes. On se limite à 800 pour rester correct avec leur service.
La clé passe uniquement dans l'en-tête HTTP ITAD-API-Key, jamais dans une URL ni dans un fichier.
"""

from __future__ import annotations

import os
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import requests

from common import REF_DIR, RAW_DIR, build_session, get_contact, log

ITAD = "https://api.isthereanydeal.com"
STEAM_SHOP_ID = 61
COUNTRY = "FR"

ITAD_DIR = REF_DIR / "itad"
CATALOG_DIR = REF_DIR / "catalog"
PRICES_DIR = RAW_DIR / "itad_prices"


def now_stamp() -> str:
    # à la milliseconde : deux morceaux écrits dans la même seconde ne s'écrasent pas
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")[:-3]


class RateLimiter:
    """Fenêtre glissante : au plus max_calls appels sur window_s secondes."""

    def __init__(self, max_calls: int = 800, window_s: float = 300.0):
        self.max_calls, self.window_s = max_calls, window_s
        self.calls: deque[float] = deque()

    def wait(self) -> None:
        now = time.monotonic()
        while self.calls and now - self.calls[0] > self.window_s:
            self.calls.popleft()
        if len(self.calls) >= self.max_calls:
            pause = self.window_s - (now - self.calls[0]) + 0.5
            log.info("Limite ITAD atteinte : pause de %.0f s", pause)
            time.sleep(pause)
        self.calls.append(time.monotonic())


class ItadError(RuntimeError):
    pass


class Itad:
    """Client minimal ITAD : réessaie sur 429 / 5xx / coupure réseau, respecte la limite de débit."""

    def __init__(self, key: str | None = None, limiter: RateLimiter | None = None):
        key = key or os.getenv("ITAD_API_KEY")
        if not key:
            raise ItadError("ITAD_API_KEY absent (secret GitHub ou fichier .env)")
        self.session: requests.Session = build_session(get_contact() or "anonymous")
        self.session.headers["ITAD-API-Key"] = key
        self.limiter = limiter or RateLimiter()

    def request(self, method: str, path: str, params: dict | None = None, json_body=None, retries: int = 5):
        for attempt in range(1, retries + 1):
            self.limiter.wait()
            try:
                resp = self.session.request(method, ITAD + path, params=params, json=json_body, timeout=60)
            except requests.RequestException as exc:
                log.warning("ITAD %s : erreur réseau (%s), tentative %d/%d", path, type(exc).__name__, attempt, retries)
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = resp.headers.get("Retry-After")
                wait = int(wait) if wait and wait.isdigit() else 2 ** attempt * 5
                log.warning("ITAD %s : HTTP %d, pause %d s (tentative %d/%d)", path, resp.status_code, wait, attempt, retries)
                time.sleep(wait)
                continue
            # 400 / 403 / 404 : inutile de réessayer (clé invalide, paramètre refusé…)
            raise ItadError(f"ITAD {path} : HTTP {resp.status_code} {resp.text[:200]}")
        raise ItadError(f"ITAD {path} : échec après {retries} tentatives")


# ---------------------------------------------------------------- Parquet par morceaux

def write_part(folder: Path, rows: list[tuple], columns: dict[str, str], stamp: str | None = None) -> Path | None:
    """Écrit des lignes dans folder/part-<horodatage>.parquet (compression zstd). columns = {nom: type DuckDB}."""
    if not rows:
        return None
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"part-{stamp or now_stamp()}.parquet"
    con = duckdb.connect()
    try:
        cols = ", ".join(f"{name} {typ}" for name, typ in columns.items())
        con.execute(f"CREATE TABLE t ({cols})")
        con.executemany(f"INSERT INTO t VALUES ({', '.join('?' * len(columns))})", rows)
        con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT parquet, COMPRESSION zstd)")
    finally:
        con.close()
    return path


def read_parts(folder: Path, sql: str = "SELECT * FROM parts") -> list[tuple]:
    """Relit tous les morceaux d'un dossier. Dans sql, la table s'appelle « parts »."""
    files = sorted(folder.glob("part-*.parquet"))
    if not files:
        return []
    con = duckdb.connect()
    try:
        con.execute(f"CREATE VIEW parts AS SELECT * FROM read_parquet({[f.as_posix() for f in files]!r}, union_by_name = true)")
        return con.execute(sql).fetchall()
    finally:
        con.close()


def latest_catalog() -> Path | None:
    files = sorted(CATALOG_DIR.glob("steam_catalog_*.parquet"))
    return files[-1] if files else None


def catalog_appids_by_popularity() -> list[int]:
    """Les jeux du catalogue, les plus joués d'abord (pour que le site soit utile avant la fin du rattrapage)."""
    path = latest_catalog()
    if path is None:
        return []
    con = duckdb.connect()
    try:
        return [r[0] for r in con.execute(
            f"SELECT appid FROM read_parquet('{path.as_posix()}') ORDER BY owners_min DESC, reviews DESC, appid").fetchall()]
    finally:
        con.close()


def itad_ids() -> dict[int, str]:
    """appid Steam -> identifiant ITAD (dernier résultat connu, seulement les jeux trouvés)."""
    rows = read_parts(ITAD_DIR / "ids", """
        SELECT appid, arg_max(itad_id, looked_up_at) FROM parts GROUP BY appid""")
    return {a: i for a, i in rows if i}
