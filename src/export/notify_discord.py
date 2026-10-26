"""
Deal Hunter — bot Discord : poste chaque matin les nouveaux plus bas historiques.

Fonctionne avec un simple webhook Discord (Paramètres du salon > Intégrations > Webhooks),
à mettre dans le secret DISCORD_WEBHOOK_URL du repo. Sans ce secret, le script ne fait rien.

Sélection : même règle que le flux RSS (plus bas de toute l'histoire, réduction ≥ 50 %),
les jeux les plus populaires d'abord. Un jeu déjà annoncé dans les 14 derniers jours
n'est pas reposté (mémoire dans data/ref/discord_posted.json).

À lancer APRÈS export_site_data.py (lit site/data/deals.json).

Usage :
  DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/... python src/export/notify_discord.py
  python src/export/notify_discord.py --dry-run     # affiche le message sans l'envoyer
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEALS = PROJECT_ROOT / "site" / "data" / "deals.json"
POSTED = PROJECT_ROOT / "data" / "ref" / "discord_posted.json"
SITE_URL = os.getenv("SITE_URL", "https://slfire.github.io/deal-hunter/").rstrip("/") + "/"

MAX_GAMES = 10          # Discord accepte 10 embeds par message
REPOST_AFTER_DAYS = 14
COLOR = 0xFFD23F        # le jaune des pastilles du site

log = logging.getLogger("discord")


def pick_games(games: list[dict], posted: dict[str, str], today: date) -> list[dict]:
    cutoff = (today - timedelta(days=REPOST_AFTER_DAYS)).isoformat()
    picks = [g for g in games
             if g.get("is_lowest_ever") and g.get("full_history") and (g.get("best_savings") or 0) >= 50
             and posted.get(str(g["id"]), "") < cutoff]
    picks.sort(key=lambda g: -(g.get("steam_count") or 0))
    return picks[:MAX_GAMES]


def game_url(g: dict) -> str:
    return f"{SITE_URL}jeu/{g['id']}-{g.get('slug') or 'jeu'}.html"


def fr_price(value: float, symbol: str) -> str:
    return f"{value:.2f}".replace(".", ",") + f" {symbol}"


def build_payload(picks: list[dict], sales: list[dict], today: date) -> dict:
    embeds = []
    for g in picks:
        lines = [f"**{fr_price(g['best_price'], '$')}** au lieu de {fr_price(g['normal_price'], '$')} "
                 f"(−{round(g['best_savings'])} %)"]
        if g.get("eur") is not None:
            lines.append(f"Sur Steam France : {fr_price(g['eur'], '€')}")
        if g.get("reviews") and g["reviews"].get("tldr"):
            lines.append(f"_{g['reviews']['tldr']}_")
        elif g.get("tags"):
            lines.append(", ".join(g["tags"][:4]))
        embed = {"title": g["title"][:250], "url": game_url(g), "description": "\n".join(lines)[:4000], "color": COLOR}
        if g.get("header"):
            embed["thumbnail"] = {"url": g["header"]}
        embeds.append(embed)

    intro = f"{len(picks)} jeu{'x' if len(picks) > 1 else ''} au prix le plus bas de leur histoire aujourd'hui."
    for s in sales:
        start = date.fromisoformat(s["start"])
        if 0 < (start - today).days <= 7:
            name = s["name"][:1].lower() + s["name"][1:]
            intro += f"\nRappel : les {name} commencent dans {(start - today).days} jours."
    return {"username": "Deal Hunter", "content": intro + f"\nToutes les promos : {SITE_URL}", "embeds": embeds}


def main() -> int:
    parser = argparse.ArgumentParser(description="Poste les plus bas historiques du jour sur Discord")
    parser.add_argument("--dry-run", action="store_true", help="Affiche le message sans l'envoyer")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    webhook = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook and not args.dry_run:
        log.info("DISCORD_WEBHOOK_URL non configuré : rien à poster.")
        return 0
    if not DEALS.exists():
        log.error("site/data/deals.json introuvable : lance d'abord export_site_data.py")
        return 1

    data = json.loads(DEALS.read_text(encoding="utf-8"))
    posted: dict[str, str] = json.loads(POSTED.read_text(encoding="utf-8")) if POSTED.exists() else {}
    today = date.today()
    picks = pick_games(data["games"], posted, today)
    if not picks:
        log.info("Aucun nouveau plus bas historique à annoncer aujourd'hui.")
        return 0

    payload = build_payload(picks, data.get("steam_sales", []), today)
    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    resp = requests.post(webhook, json=payload, timeout=30)
    if resp.status_code >= 300:
        log.error("Discord a refusé le message : HTTP %d %s", resp.status_code, resp.text[:200])
        return 1

    for g in picks:
        posted[str(g["id"])] = today.isoformat()
    # on oublie ce qui a plus de 60 jours pour garder le fichier petit
    old = (today - timedelta(days=60)).isoformat()
    posted = {k: v for k, v in posted.items() if v >= old}
    POSTED.parent.mkdir(parents=True, exist_ok=True)
    POSTED.write_text(json.dumps(posted, indent=0), encoding="utf-8")
    log.info("Discord : %d jeux annoncés", len(picks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
