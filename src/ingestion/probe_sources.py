"""
Deal Hunter — sonde des sources de la v3 (catalogue Steam complet + historique IsThereAnyDeal).

Lancé à la main depuis GitHub Actions (workflow « Sonde des sources »), là où la clé ITAD est disponible
en secret. Il appelle chaque endpoint une seule fois, sur quelques jeux connus, et enregistre :
  - le code HTTP, la taille et un extrait de chaque réponse,
  - les en-têtes de limitation de débit,
dans data/raw/probe/<date>/. Ces échantillons servent à écrire les vrais scripts d'ingestion sur des
données réelles, sans jamais copier la clé ailleurs que dans les secrets GitHub.

La clé n'est jamais écrite : elle passe dans un en-tête HTTP et les URL enregistrées en sont expurgées.

Usage (en local, avec ITAD_API_KEY dans .env) :
  python src/ingestion/probe_sources.py
"""

from __future__ import annotations

import gzip
import json
import os
import time
from datetime import datetime, timezone

import requests

from common import RAW_DIR, build_session, get_contact, log, setup_logging

ITAD = "https://api.isthereanydeal.com"
STEAM_SHOP_ID = 61                         # identifiant de Steam chez IsThereAnyDeal
SAMPLE_APPIDS = [1145360, 413150, 292030, 1086940, 367520]   # Hades, Stardew Valley, The Witcher 3, Baldur's Gate 3, Hollow Knight

RATE_HEADERS = ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset", "retry-after")


def shrink(payload: object, limit: int = 300) -> object:
    """Garde un échantillon des grosses listes (le catalogue Steam fait plusieurs Mo) et note leur taille réelle."""
    if isinstance(payload, list) and len(payload) > limit:
        return {"_list_len": len(payload), "_head": payload[:limit]}
    if isinstance(payload, dict):
        if len(payload) > limit:
            keys = list(payload)[:limit]
            return {"_dict_len": len(payload), "_head": {k: shrink(payload[k], limit) for k in keys}}
        return {k: shrink(v, limit) for k, v in payload.items()}
    return payload


def record(name: str, resp: requests.Response | None, out_dir, error: str | None = None) -> object:
    """Enregistre la réponse (URL sans paramètres, donc sans clé) et renvoie le JSON décodé s'il y en a un."""
    entry: dict = {"name": name, "error": error}
    payload = None
    if resp is not None:
        entry.update({
            "url": resp.request.url.split("?")[0], "method": resp.request.method,
            "status": resp.status_code, "bytes": len(resp.content),
            "rate_headers": {h: resp.headers.get(h) for h in RATE_HEADERS if resp.headers.get(h)},
            "content_type": resp.headers.get("content-type"),
        })
        try:
            payload = resp.json()
        except ValueError:
            entry["text_head"] = resp.text[:500]
    with gzip.open(out_dir / f"{name}.json.gz", "wt", encoding="utf-8") as f:
        json.dump({"meta": entry, "payload": shrink(payload)}, f, ensure_ascii=False)
    log.info("%-28s HTTP %s  %s octets  %s", name, entry.get("status"), entry.get("bytes"), entry.get("rate_headers") or "")
    return payload


def call(session: requests.Session, name: str, method: str, url: str, out_dir, **kw) -> object:
    try:
        resp = session.request(method, url, timeout=60, **kw)
    except requests.RequestException as exc:
        return record(name, None, out_dir, error=type(exc).__name__)
    return record(name, resp, out_dir)


def main() -> int:
    setup_logging()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d/%H%M%S")
    out_dir = RAW_DIR / "probe" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    session = build_session(get_contact() or "contact-non-configure")

    # ---------------- catalogue Steam ----------------
    call(session, "steam_applist_v2", "GET", "https://api.steampowered.com/ISteamApps/GetAppList/v2/", out_dir)
    call(session, "steamspy_all_page0", "GET", "https://steamspy.com/api.php", out_dir, params={"request": "all", "page": 0})
    ids = ",".join(map(str, SAMPLE_APPIDS))
    call(session, "steam_price_overview_batch", "GET", "https://store.steampowered.com/api/appdetails", out_dir,
         params={"appids": ids, "filters": "price_overview", "cc": "fr"})

    # ---------------- IsThereAnyDeal ----------------
    key = os.getenv("ITAD_API_KEY")
    if not key:
        log.warning("ITAD_API_KEY absent : sonde ITAD sautée")
        return 0
    itad = requests.Session()
    itad.headers.update({**session.headers, "ITAD-API-Key": key})      # la clé ne passe que dans l'en-tête

    call(itad, "itad_shops_fr", "GET", f"{ITAD}/service/shops/v1", out_dir, params={"country": "FR"})
    lookup = call(itad, "itad_lookup_steam", "POST", f"{ITAD}/lookup/id/shop/{STEAM_SHOP_ID}/v1", out_dir,
                  json=[f"app/{a}" for a in SAMPLE_APPIDS]) or {}
    game_ids = [v for v in lookup.values() if v] if isinstance(lookup, dict) else []
    if not game_ids:
        log.warning("Aucun identifiant ITAD trouvé : le reste de la sonde est sauté")
        return 0
    gid = game_ids[0]
    # historique : taille réelle sur Steam seul et toutes boutiques, pour chacun des 5 jeux (dimensionner le stockage)
    for i, g in enumerate(game_ids):
        call(itad, f"itad_history_steam_{i}", "GET", f"{ITAD}/games/history/v2", out_dir,
             params={"id": g, "country": "FR", "shops": STEAM_SHOP_ID})
    call(itad, "itad_history_all_shops", "GET", f"{ITAD}/games/history/v2", out_dir, params={"id": gid, "country": "FR"})
    call(itad, "itad_prices_v3", "POST", f"{ITAD}/games/prices/v3", out_dir,
         params={"country": "FR", "shops": STEAM_SHOP_ID}, json=game_ids)
    call(itad, "itad_overview_v2", "POST", f"{ITAD}/games/overview/v2", out_dir, params={"country": "FR"}, json=game_ids)
    call(itad, "itad_info_v2", "GET", f"{ITAD}/games/info/v2", out_dir, params={"id": gid})

    # combien d'identifiants une seule requête de lookup accepte-t-elle ? (1 000 appids factices + vrais)
    big = [f"app/{a}" for a in SAMPLE_APPIDS] + [f"app/{a}" for a in range(10, 10 + 995 * 10, 10)]
    time.sleep(1)
    found = call(itad, "itad_lookup_1000", "POST", f"{ITAD}/lookup/id/shop/{STEAM_SHOP_ID}/v1", out_dir, json=big) or {}
    many = [v for v in found.values() if v] if isinstance(found, dict) else []
    log.info("lookup 1000 : %d identifiants trouvés", len(many))
    # combien de jeux par requête de prix ? (on teste 200 puis tout ce qu'on a)
    for n in (200, len(many)):
        if n and len(many) >= n:
            time.sleep(1)
            call(itad, f"itad_prices_v3_{n}", "POST", f"{ITAD}/games/prices/v3", out_dir,
                 params={"country": "FR", "shops": STEAM_SHOP_ID}, json=many[:n])
    log.info("Échantillons écrits dans %s", out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
