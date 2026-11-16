"""
Deal Hunter — correspondance « jeu Steam -> identifiant IsThereAnyDeal ».

ITAD identifie les jeux par un UUID. On le retrouve à partir de l'identifiant Steam (appid) avec
POST /lookup/id/shop/61/v1, jusqu'à 1 000 jeux par requête (on en envoie 500).

Ne cherche que les jeux du catalogue jamais cherchés, plus ceux introuvables depuis plus de 30 jours
(un jeu peut être ajouté chez ITAD plus tard). Écrit un morceau : data/ref/itad/ids/part-<horodatage>.parquet
(appid, itad_id ou NULL, looked_up_at).

Usage :
  python src/ingestion/itad_ids.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from common import log, setup_logging
from itad_common import ITAD_DIR, STEAM_SHOP_ID, Itad, ItadError, catalog_appids_by_popularity, read_parts, write_part

BATCH = 500
RETRY_NOT_FOUND_DAYS = 30
COLUMNS = {"appid": "INTEGER", "itad_id": "VARCHAR", "looked_up_at": "TIMESTAMP"}


def todo_appids(catalog: list[int], known: dict[int, tuple[str | None, datetime]], now: datetime) -> list[int]:
    retry_before = now - timedelta(days=RETRY_NOT_FOUND_DAYS)
    return [a for a in catalog if a not in known or (known[a][0] is None and known[a][1] < retry_before)]


def parse_lookup(payload: dict) -> dict[int, str | None]:
    """{"app/220": "uuid" | null} -> {220: "uuid" | None}"""
    out = {}
    for key, value in (payload or {}).items():
        if key.startswith("app/") and key[4:].isdigit():
            out[int(key[4:])] = value or None
    return out


def main() -> int:
    setup_logging()
    catalog = catalog_appids_by_popularity()
    if not catalog:
        log.error("Pas de catalogue : lance d'abord steam_catalog.py")
        return 1
    known = {a: (i, t) for a, i, t in read_parts(ITAD_DIR / "ids", """
        SELECT appid, arg_max(itad_id, looked_up_at), max(looked_up_at) FROM parts GROUP BY appid""")}
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    todo = todo_appids(catalog, known, now)
    log.info("Catalogue : %d jeux, %d déjà associés, %d à chercher", len(catalog),
             sum(1 for v in known.values() if v[0]), len(todo))
    if not todo:
        return 0

    try:
        itad = Itad()
    except ItadError as exc:
        log.error("%s", exc)
        return 1
    rows: list[tuple] = []
    try:
        for k in range(0, len(todo), BATCH):
            batch = todo[k:k + BATCH]
            found = parse_lookup(itad.request("POST", f"/lookup/id/shop/{STEAM_SHOP_ID}/v1",
                                              json_body=[f"app/{a}" for a in batch]))
            rows += [(a, found.get(a), now) for a in batch]
            if (k // BATCH) % 20 == 0:
                log.info("Recherche : %d/%d", k + len(batch), len(todo))
    except ItadError as exc:
        log.warning("ITAD a refusé la recherche (%s) — on garde les %d déjà faites", exc, len(rows))
    path = write_part(ITAD_DIR / "ids", rows, COLUMNS)
    log.info("%d jeux cherchés, %d trouvés chez ITAD -> %s", len(rows), sum(1 for r in rows if r[1]), path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
