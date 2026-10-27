"""
Deal Hunter — taux de change dollar -> euro du jour (Banque centrale européenne).

CheapShark ne donne les prix qu'en dollars US. Pour afficher tout le site en euros, on
convertit avec le taux de référence publié chaque jour ouvré par la BCE :
  https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml   (1 € = X $)

Sortie : data/ref/fx_rate.json   {"date", "usd_per_eur", "source"}
Si la BCE ne répond pas, on garde le dernier taux connu (le site affiche sa date).

Usage :
  python src/ingestion/fx_rate.py
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET

from common import REF_DIR, build_session, get_contact, get_with_retry, log, setup_logging, write_json

URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
OUT = REF_DIR / "fx_rate.json"


def parse_ecb(xml_text: str) -> tuple[str, float]:
    """(date, nombre de dollars pour 1 euro) depuis le XML de la BCE."""
    root = ET.fromstring(xml_text)
    day = rate = None
    for el in root.iter():
        if el.tag.endswith("Cube") and "time" in el.attrib:
            day = el.attrib["time"]
        if el.tag.endswith("Cube") and el.attrib.get("currency") == "USD":
            rate = float(el.attrib["rate"])
    if not day or not rate or not (0.5 < rate < 2.5):
        raise ValueError(f"taux USD introuvable ou aberrant dans la réponse de la BCE ({day}, {rate})")
    return day, rate


def main() -> int:
    setup_logging()
    session = build_session(get_contact() or "anonymous")
    session.headers["Accept"] = "application/xml"
    try:
        resp = get_with_retry(session, URL)
        day, rate = parse_ecb(resp.text)
    except (RuntimeError, ValueError, ET.ParseError) as exc:
        log.warning("Taux BCE indisponible (%s) : on garde le dernier taux connu.", exc)
        return 0
    write_json(OUT, {"date": day, "usd_per_eur": rate, "source": "BCE (taux de référence)"})
    log.info("Taux BCE du %s : 1 € = %.4f $", day, rate)
    return 0


if __name__ == "__main__":
    sys.exit(main())
