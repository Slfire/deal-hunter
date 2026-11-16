"""
Deal Hunter — prix du jour de TOUT le catalogue, en ne gardant que ce qui a changé (journal des changements).

POST /games/prices/v3?country=FR accepte 200 jeux par requête : ~375 requêtes pour 75 000 jeux.
Pour chaque jeu on retient :
  - le prix Steam France (prix, prix normal, réduction),
  - la meilleure offre toutes boutiques confondues (prix, boutique, réduction),
  - le plus bas historique toutes boutiques.

Stocker 75 000 lignes par jour ferait ~500 Mo par an dans git. On compare donc au dernier état connu
et on n'écrit QUE les jeux dont quelque chose a changé (capture des changements, « CDC ») : quelques
milliers de lignes par jour. Le premier run écrit l'état complet. L'état d'un jeu à une date donnée =
sa dernière ligne avant cette date.

Sortie : data/raw/itad_prices/YYYY-MM-DD/changes.parquet

Usage :
  python src/ingestion/itad_prices.py
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone

from common import log, setup_logging
from itad_common import COUNTRY, PRICES_DIR, STEAM_SHOP_ID, Itad, ItadError, itad_ids, write_part

import duckdb

BATCH = 200          # limite ITAD de /games/prices/v3

COLUMNS = {
    "itad_id": "VARCHAR", "captured_at": "TIMESTAMP",
    "steam_price_cents": "INTEGER", "steam_regular_cents": "INTEGER", "steam_cut": "SMALLINT",
    "best_price_cents": "INTEGER", "best_regular_cents": "INTEGER", "best_cut": "SMALLINT", "best_shop_id": "INTEGER",
    "low_all_cents": "INTEGER",
}
COMPARED = list(COLUMNS)[2:]      # tout sauf l'identifiant et l'horodatage


def parse_prices(payload: list) -> dict[str, tuple]:
    """Réponse ITAD -> {itad_id: (valeurs de COMPARED)}"""
    out = {}
    for game in payload or []:
        gid = game.get("id")
        if not gid:
            continue
        deals = game.get("deals") or []
        steam = next((d for d in deals if (d.get("shop") or {}).get("id") == STEAM_SHOP_ID), None)
        best = min(deals, key=lambda d: d["price"]["amountInt"]) if deals else None
        low = ((game.get("historyLow") or {}).get("all") or {}).get("amountInt")
        out[gid] = (
            steam["price"]["amountInt"] if steam else None, steam["regular"]["amountInt"] if steam else None,
            steam.get("cut") if steam else None,
            best["price"]["amountInt"] if best else None, best["regular"]["amountInt"] if best else None,
            best.get("cut") if best else None, (best.get("shop") or {}).get("id") if best else None,
            low,
        )
    return out


def changed_rows(current: dict[str, tuple], previous: dict[str, tuple], now: datetime) -> list[tuple]:
    """Ne garde que les jeux nouveaux ou dont au moins une valeur a changé depuis le dernier relevé."""
    return [(gid, now, *vals) for gid, vals in current.items() if previous.get(gid) != vals]


def last_state() -> dict[str, tuple]:
    files = sorted(PRICES_DIR.glob("*/changes.parquet"))
    if not files:
        return {}
    con = duckdb.connect()
    try:
        cols = ", ".join(f"arg_max({c}, captured_at)" for c in COMPARED)
        rows = con.execute(f"SELECT itad_id, {cols} FROM read_parquet({[f.as_posix() for f in files]!r}) GROUP BY itad_id").fetchall()
    finally:
        con.close()
    return {r[0]: tuple(r[1:]) for r in rows}


def main() -> int:
    setup_logging()
    games = sorted(set(itad_ids().values()))
    if not games:
        log.error("Aucun jeu associé à ITAD : lance d'abord steam_catalog.py puis itad_ids.py")
        return 1
    out = PRICES_DIR / date.today().isoformat() / "changes.parquet"
    if out.exists():
        log.info("Prix du jour déjà relevés (%s)", out)
        return 0
    try:
        itad = Itad()
    except ItadError as exc:
        log.error("%s", exc)
        return 1

    current: dict[str, tuple] = {}
    for k in range(0, len(games), BATCH):
        try:
            current.update(parse_prices(itad.request("POST", "/games/prices/v3", params={"country": COUNTRY},
                                                     json_body=games[k:k + BATCH])))
        except ItadError as exc:
            log.warning("Lot %d ignoré : %s", k // BATCH, exc)
        if (k // BATCH) % 50 == 0:
            log.info("Prix : %d/%d jeux", min(k + BATCH, len(games)), len(games))
    if len(current) < 0.5 * len(games):
        log.error("Seulement %d prix sur %d jeux : relevé incomplet, rien n'est écrit", len(current), len(games))
        return 1

    previous = last_state()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    changed = changed_rows(current, previous, now)
    out.parent.mkdir(parents=True, exist_ok=True)
    path = write_part(out.parent, changed, COLUMNS, stamp="tmp")
    if path:
        path.replace(out)
    on_sale = sum(1 for v in current.values() if (v[2] or 0) > 0)
    log.info("%d jeux relevés, %d en promo sur Steam, %d changements écrits -> %s", len(current), on_sale, len(changed), out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
