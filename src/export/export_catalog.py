"""
Deal Hunter — v3 : export du catalogue complet pour la zone « Prévisions » du site.

Le catalogue fait des dizaines de milliers de jeux : impossible de tout mettre dans un seul gros fichier
que le navigateur chargerait d'un coup. Deux niveaux :
  site/data/catalog/index.json          un tableau compact par jeu, juste de quoi chercher et lister
                                        (nom, popularité, prix, promo en cours, chance de promo sous 30 jours)
  site/data/catalog/details/<n>.json    le détail de chaque jeu (prévisions, réduction attendue, historique
                                        des promos sur 2 ans), réparti en 256 fichiers (voir shard_of) :
                                        la fiche d'un jeu ne charge qu'un fichier d'environ 1/256e du total.
  site/data/catalog/meta.json           date, fiabilité du modèle, grandes soldes à venir, boutiques.

À lancer APRÈS predict_next_sale.py.

Usage :
  python src/export/export_catalog.py
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / "data" / "warehouse" / "dealhunter.duckdb"
OUT_DIR = PROJECT_ROOT / "site" / "data" / "catalog"
SHOPS = PROJECT_ROOT / "data" / "ref" / "itad" / "shops.json"
SHARDS = 256               # = 2 puissance 8 : les 8 bits gardés par shard_of
HISTORY_DAYS = 730          # historique des promos affiché sur la fiche : 2 ans

log = logging.getLogger("catalog")

# index : une liste par jeu, dans cet ordre (le site lit les colonnes par position, c'est 3 fois plus léger)
INDEX_COLUMNS = ["appid", "name", "owners", "price", "cut", "p30", "expected_cut"]


def shard_of(appid: int) -> int:
    """Numéro du fichier de détail d'un jeu. Les appid Steam finissent presque tous par 0 : un simple
    appid % 256 remplirait mal les fichiers. On multiplie par une grande constante et on garde les 8 bits
    du HAUT (hachage de Knuth), bien mélangés. Même calcul côté site : Math.imul(appid, 2654435761) >>> 24."""
    return ((appid * 2654435761) & 0xFFFFFFFF) >> 24


def dump(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))


def tables(con) -> set[str]:
    return {f"{s}.{t}" for s, t in con.execute("SELECT table_schema, table_name FROM information_schema.tables").fetchall()}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if not DB_PATH.exists():
        log.error("Base introuvable : lance d'abord build_warehouse.py")
        return 1
    con = duckdb.connect(str(DB_PATH))      # lecture seule en pratique ; sans option pour cohabiter avec dbt dans les tests
    try:
        known = tables(con)
        has_preds = "ml.next_sale_predictions" in known
        preds_join = "LEFT JOIN ml.next_sale_predictions p USING (itad_id)" if has_preds else ""
        p_cols = "p.p14, p.p30, p.p60, p.p90" if has_preds else "NULL, NULL, NULL, NULL"
        games = con.execute(f"""
            SELECT c.appid, c.name, c.developer, c.publisher, c.owners_min, c.positive_pct, c.reviews,
                   c.steam_price_cents, c.steam_regular_cents, c.steam_cut, c.best_price_cents, c.best_cut, c.best_shop_id,
                   c.low_all_cents, c.n_sales, c.n_sales_last_year, c.last_sale_start, c.expected_cut, c.expected_cut_low,
                   c.expected_cut_high, c.max_cut, c.first_price_date, {p_cols}, c.itad_id
            FROM marts.dim_catalog c {preds_join}
            WHERE c.itad_id IS NOT NULL AND c.steam_regular_cents IS NOT NULL
            ORDER BY c.owners_min DESC, c.reviews DESC, c.appid
        """).fetchall()
        sales = {}
        for gid, start, end, cut in con.execute(f"""
                SELECT itad_id, start_date, end_date, cut FROM marts.fct_steam_sale_episodes
                WHERE start_date >= current_date - {HISTORY_DAYS} ORDER BY itad_id, start_date""").fetchall():
            sales.setdefault(gid, []).append([start.isoformat(), end.isoformat() if end else None, cut])
        upcoming = [{"start": d.isoformat(), "name": n} for d, n in con.execute("""
            SELECT m.start_date, coalesce(c.name, 'Grandes soldes Steam') FROM marts.dim_steam_major_sales m
            LEFT JOIN raw.steam_sales_calendar c ON c.start_date = m.start_date
            WHERE m.start_date >= current_date ORDER BY 1 LIMIT 4""").fetchall()]
        report = None
        if "ml.next_sale_report" in known:
            report = json.loads(con.execute("SELECT report FROM ml.next_sale_report").fetchall()[0][0])
    finally:
        con.close()

    if not games:
        log.warning("Catalogue vide (historique IsThereAnyDeal pas encore récupéré) : rien à exporter")
        return 0
    published = bool(report and report.get("published"))

    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    index, shards = [], {}
    pct = lambda x: round(100 * x) if x is not None else None     # noqa: E731
    for (appid, name, dev, pub, owners, pos_pct, reviews, price, regular, cut, best_price, best_cut, best_shop,
         low_all, n_sales, n_year, last_start, exp_cut, exp_low, exp_high, max_cut, first_date,
         p14, p30, p60, p90, gid) in games:
        on_sale = (cut or 0) > 0
        forecast = published and not on_sale and p30 is not None
        index.append([appid, name, owners, price, cut or 0, pct(p30) if forecast else None, exp_cut])
        shards.setdefault(shard_of(appid), {})[str(appid)] = {
            "name": name, "dev": dev, "pub": pub, "owners": owners, "positive": pos_pct, "reviews": reviews,
            "price": price, "regular": regular, "cut": cut or 0,
            "best": {"price": best_price, "cut": best_cut, "shop": best_shop} if best_price is not None else None,
            "low": low_all, "n_sales": n_sales, "n_sales_year": n_year,
            "last_sale": last_start.isoformat() if last_start else None,
            "expected": {"cut": exp_cut, "low": exp_low, "high": exp_high} if exp_cut else None,
            "max_cut": max_cut, "since": first_date.isoformat() if first_date else None,
            "forecast": {"14": pct(p14), "30": pct(p30), "60": pct(p60), "90": pct(p90)} if forecast else None,
            "sales": sales.get(gid, []),
        }

    dump(OUT_DIR / "index.json", {"columns": INDEX_COLUMNS, "games": index})
    for n, payload in shards.items():
        dump(OUT_DIR / "details" / f"{n}.json", payload)
    shops = json.loads(SHOPS.read_text(encoding="utf-8")) if SHOPS.exists() else {}
    dump(OUT_DIR / "meta.json", {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "today": date.today().isoformat(),
        "shards": SHARDS, "games": len(index), "published": published, "model": report,
        "upcoming_major_sales": upcoming, "shops": shops,
    })
    log.info("Catalogue exporté : %d jeux, %d fichiers de détail, prévisions %s",
             len(index), len(shards), "publiées" if published else "non publiées")
    return 0


if __name__ == "__main__":
    sys.exit(main())
