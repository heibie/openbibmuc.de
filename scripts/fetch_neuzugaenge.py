#!/usr/bin/env python3
"""
PROTOTYP - noch nicht Teil des regulaeren Datenflusses.

Holt Neuzugaenge EINES Tages (Default: gestern) aus dem OAI-PMH-Katalog der
Muenchner Stadtbibliothek (https://data-bib.muenchen.de/oai-pmh, Gesamtbestand
DE-M36), reichert sie ueber die ISBN mit einem Cover-Bild an (erst ekz.de,
dann Open Library, dann Google Books als Fallback - siehe unten) und schreibt
sie nach data/neuzugaenge/<TAG>.csv - eine Datei pro Tag, damit das Frontend
tageweise blaettern kann, ohne bei jedem Laden die komplette Historie laden
zu muessen. data/neuzugaenge/index.json listet alle bisher geholten Tage.
Wiederholte Laeufe fuer denselben Tag sind idempotent (Tagesdatei wird nicht
erneut geschrieben, falls sie schon existiert).

Cover-Quellen, in dieser Reihenfolge (erster Treffer gewinnt):
1. ekz.de (Einkaufszentrale fuer Bibliotheken, cover.ekz.de/<ISBN>.jpg) - deutscher
   Bibliotheks-Medienlieferant, deutlich bessere Trefferquote fuer den stark
   deutschsprachigen MSB-Bestand als die folgenden Quellen.
2. Open Library (schwache deutsche Abdeckung, Test 2026-08-02: nur 18 von 384
   ISBN-Treffern, ~4.7%)
3. Google Books (braucht eigenen API-Key in scripts/_secrets.py, siehe
   _secrets.example.py; deren Backend antwortet z.Zt. oft mit transientem 503,
   siehe GOOGLE_BOOKS_RETRIES)

NICHT genutzt: isbn.de (robots.txt verbietet ClaudeBot/automatisierte
Datenerhebung explizit), ssl.muenchen.de/vlb (robots.txt verbietet den
Cover-Pfad explizit, Direktzugriff liefert zudem HTTP 403), Goodreads (seit
2020 keine offene API mehr, Scraping der Amazon-Tochter bewusst nicht
umgesetzt, auch nicht ueber Wrapper-APIs wie bookcover-api). DNB liefert zwar
saubere Metadaten ueber die SRU-Schnittstelle, aber keine Cover-Bilder.

Warum marc_xml statt oai_dc: bei identischer from/until/set-Abfrage liefert
oai_dc nachweislich WENIGER Treffer als marc_xml fuer denselben Tag (Test
2026-08-02: 31.07. -> oai_dc 95 Treffer, marc_xml 478 Treffer). Vermutlich
zwei nicht ganz synchrone Backend-Pipelines. marc_xml gilt daher als die
verlaesslichere Quelle, auch wenn das Parsen aufwaendiger ist.

Warum ein Tag statt eines rollierenden 7-Tage-Fensters: der Katalog hat >1 Mio
Datensaetze, ein 7-Tage-Fenster ergibt schon 900+ Treffer - jedes Mal alles neu
abzurufen ist Verschwendung. Ein Tag pro Lauf + Archivierung baut stattdessen
mit der Zeit eine vollstaendige lokale Historie auf, ohne je einen Tag doppelt
abzufragen. Gedacht als taeglicher Cronjob (aehnlich cron/check_sources.php),
hier erstmal als lokaler Test.

Aufruf: python3 scripts/fetch_neuzugaenge.py [YYYY-MM-DD]
        (ohne Datum: gestern)
"""
import csv
import json
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

try:
    from _secrets import GOOGLE_BOOKS_API_KEY
except ImportError:
    GOOGLE_BOOKS_API_KEY = None

BASE = "https://data-bib.muenchen.de/oai-pmh"
SET_SPEC = "DE-M36"
# Pause zwischen Requests - ohne die blockt die WAF der Stadt gelegentlich mit
# 503 "Access Denied" (bekanntes Verhalten, siehe README, Abschnitt OAI-PMH).
REQUEST_PAUSE_SECONDS = 2
COVER_PAUSE_SECONDS = 0.3
# Google Books' Backend antwortet gerade oft mit transientem 503 "backendFailed"
# (unabhaengig vom API-Key, getestet 2026-08-02) - ohne Retry wuerden wir viele
# echte Cover faelschlich als "nicht gefunden" werten.
GOOGLE_BOOKS_RETRIES = 3
GOOGLE_BOOKS_RETRY_PAUSE_SECONDS = 3

NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "marc": "http://www.loc.gov/MARC21/slim",
}

# Rohwert aus 245$h -> das freundlichere Label, das der MSB-OPAC selbst als
# "Medienart" anzeigt (per Stichprobe an 11 Datensaetzen am 2026-08-02 manuell
# im OPAC nachgeschaut - https://ssl.muenchen.de/aDISWeb/app/opac?sp=...).
# Werte ohne Eintrag hier werden unveraendert durchgereicht (z.B. "CD", "Noten",
# "Konsolenspiel" - die zeigt der OPAC schon 1:1 so an).
GMD_DISPLAY_LABELS = {
    "Druckschrift": "Buch",
    "DVD-Video": "DVD",
    "MP3-CD": "MP3",
    "Blu-Ray": "Blu-ray Disc",
    "Spiel": "Brettspiel",
    "Buch + CD": "Medienkombination",
    "Buch + DVD-Video + CD": "Medienkombination",
}

# Nur noch Fallback fuer den (seltenen) Fall, dass 245$h fehlt - siehe
# parse_records(). MARC-Leader Position 6 (Type of Record) -> deutsches Label,
# wie es auch der Muenchner OAI-PMH-Server fuer dc:type verwendet (empirisch
# abgeglichen, siehe README). 'o' (Kit) hat dort bewusst kein Label.
TYPE_LABELS = {
    "a": "Druckschrift",
    "t": "Druckschrift",
    "g": "visuelle Materialien",
    "k": "visuelle Materialien",
    "m": "Dateien",
    "c": "Musikalien",
    "d": "Musikalien",
    "j": "Musikalien",
    "e": "Karten",
    "f": "Karten",
}

DATA = Path(__file__).resolve().parent.parent / "data"
ARCHIVE_DIR = DATA / "neuzugaenge"
INDEX_PATH = ARCHIVE_DIR / "index.json"
FIELDNAMES = [
    "datestamp", "title", "subtitle", "creator", "year",
    "type", "language", "isbn", "subjects", "cover_url", "opac_url",
]


def day_path(day):
    return ARCHIVE_DIR / f"{day}.csv"


def update_index(day):
    days = json.loads(INDEX_PATH.read_text(encoding="utf-8"))["days"] if INDEX_PATH.exists() else []
    if day not in days:
        days.append(day)
    days.sort()
    INDEX_PATH.write_text(
        json.dumps({"days": days}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def fetch(url, retries=3):
    # curl statt urllib: auf diesem Rechner fehlt Python der Zertifikatsspeicher
    # fuer diese Domain (SSL-Fehler), curl nutzt den System-Truststore.
    for attempt in range(1, retries + 1):
        result = subprocess.run(
            ["curl", "-s", "-A", USER_AGENT, url],
            capture_output=True, timeout=30,
        )
        body = result.stdout
        if b"<OAI-PMH" in body:
            return body
        print(f"  Antwort sieht nicht nach OAI-PMH-XML aus (Versuch {attempt}/{retries}), warte laenger ...")
        time.sleep(5)
    raise RuntimeError(f"Abruf endgueltig fehlgeschlagen: {url}")


def clean_isbn(raw):
    return re.sub(r"[^0-9Xx]", "", raw or "")


def best_isbn(isbns):
    """Bevorzugt die 13-stellige ISBN (aktueller Standard), sonst erste verfuegbare."""
    thirteen = [i for i in isbns if len(i) == 13]
    return thirteen[0] if thirteen else (isbns[0] if isbns else "")


def opac_url(record_id):
    """Verlinkt auf den Original-Katalogeintrag im MSB-OPAC. Nur reine URL-Konstruktion
    aus MARC-Feld 001 (Format 'AK<Nummer>'), KEIN automatisierter Abruf dieser Seite -
    robots.txt von ssl.muenchen.de verbietet zwar /aDISWeb/ fuers Crawlen, das gilt aber
    nicht fuers Verlinken (Nutzer klicken das im eigenen Browser an). Muster empirisch
    bestaetigt 2026-08-02 (ein manueller Testabruf): 'AK4664719' -> sp=SAK04664719.
    """
    m = re.match(r"^([A-Z]+)(\d+)$", record_id or "")
    if not m:
        return ""
    modul, nummer = m.groups()
    return f"https://ssl.muenchen.de/aDISWeb/app/opac?sp=S{modul}{nummer.zfill(8)}"


def parse_records(xml_bytes):
    root = ET.fromstring(xml_bytes)
    records = []
    for rec in root.findall(".//oai:record", NS):
        header = rec.find("oai:header", NS)
        datestamp = header.findtext("oai:datestamp", default="", namespaces=NS)
        marc = rec.find(".//marc:record", NS)
        if marc is None:
            continue

        leader = marc.findtext("marc:leader", default="", namespaces=NS)
        type_code = leader[6] if len(leader) > 6 else ""
        record_id = marc.findtext("marc:controlfield[@tag='001']", default="", namespaces=NS)

        def datafield_subs(tag, code):
            return [
                sf.text for df in marc.findall(f"marc:datafield[@tag='{tag}']", NS)
                for sf in df.findall(f"marc:subfield[@code='{code}']", NS)
                if sf.text
            ]

        title_vals = datafield_subs("245", "a")
        subtitle_vals = datafield_subs("245", "b")
        gmd_vals = datafield_subs("245", "h")
        creator_vals = datafield_subs("100", "a") or datafield_subs("700", "a")
        isbns = [clean_isbn(v) for v in datafield_subs("020", "a")]
        isbns = [i for i in isbns if i]
        year_vals = datafield_subs("264", "c") or datafield_subs("260", "c")
        lang_vals = datafield_subs("041", "a")
        subject_vals = datafield_subs("650", "a")

        # 245$h (allgemeine Materialbenennung, z.B. "[Buch + CD]", "[DVD-Video]",
        # "[Konsolenspiel]") ist viel feinkoerniger als der MARC-Leader-Code und
        # steht in praktisch jedem Datensatz (Stichprobe 2026-08-02: 150/150).
        # ABER: der OPAC selbst zeigt nochmal ein anderes, freundlicheres Label an
        # (z.B. "Druckschrift" -> "Buch", "Buch + CD" -> "Medienkombination") -
        # per Stichprobe (11 Datensaetze, 2026-08-02) manuell nachgebaut in
        # GMD_DISPLAY_LABELS. Leader-Code nur als Fallback, falls $h mal fehlen sollte.
        gmd_raw = gmd_vals[0].strip("[]") if gmd_vals else ""
        gmd = GMD_DISPLAY_LABELS.get(gmd_raw, gmd_raw)

        records.append({
            "datestamp": datestamp,
            "title": title_vals[0] if title_vals else "",
            "subtitle": subtitle_vals[0] if subtitle_vals else "",
            "creator": creator_vals[0] if creator_vals else "",
            "year": re.sub(r"[^0-9]", "", year_vals[0])[:4] if year_vals else "",
            "type": gmd or TYPE_LABELS.get(type_code, ""),
            "language": lang_vals[0] if lang_vals else "",
            "isbn": best_isbn(isbns),
            "subjects": "; ".join(subject_vals),
            "opac_url": opac_url(record_id),
        })

    resumption = root.find(".//oai:resumptionToken", NS)
    token = resumption.text if resumption is not None and resumption.text else None
    complete_size = resumption.get("completeListSize") if resumption is not None else None
    return records, token, complete_size


def ekz_cover(isbn):
    """Primaere Cover-Quelle: ekz.de (Einkaufszentrale fuer Bibliotheken, deutscher
    Bibliotheks-Medienlieferant). Deutlich bessere Trefferquote fuer deutschsprachige
    Titel als Open Library/Google Books (Stichprobe 2026-08-02). cover.ekz.de hat
    kein robots.txt (404 bei Abruf), die Haupt-Domain ekz.de erlaubt Crawling
    ausdruecklich ('Disallow:' leer) - anders als isbn.de oder ssl.muenchen.de/vlb,
    die wir deshalb bewusst NICHT nutzen. Fehlendes Cover liefert sauber HTTP 404."""
    if not isbn:
        return ""
    url = f"https://cover.ekz.de/{isbn}.jpg"
    result = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-A", USER_AGENT, url],
        capture_output=True, text=True, timeout=15,
    )
    if result.stdout.strip() == "200":
        return url
    return ""


def openlibrary_cover(isbn):
    if not isbn:
        return ""
    url = f"https://covers.openlibrary.org/b/isbn/{isbn}-M.jpg?default=false"
    result = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-A", USER_AGENT, url],
        capture_output=True, text=True, timeout=15,
    )
    if result.stdout.strip() == "200":
        return f"https://covers.openlibrary.org/b/isbn/{isbn}-M.jpg"
    return ""


def google_books_cover(isbn):
    """Fallback fuer ISBNs ohne Open-Library-Cover. Braucht scripts/_secrets.py
    mit GOOGLE_BOOKS_API_KEY (siehe _secrets.example.py). Retried bei 503
    ('Service temporarily unavailable') - siehe GOOGLE_BOOKS_RETRIES oben."""
    if not isbn or not GOOGLE_BOOKS_API_KEY:
        return ""
    url = f"https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}&key={GOOGLE_BOOKS_API_KEY}"
    for attempt in range(1, GOOGLE_BOOKS_RETRIES + 1):
        result = subprocess.run(
            ["curl", "-s", "-A", USER_AGENT, url],
            capture_output=True, text=True, timeout=15,
        )
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            data = {}
        if "error" in data:
            time.sleep(GOOGLE_BOOKS_RETRY_PAUSE_SECONDS)
            continue
        items = data.get("items") or []
        if not items:
            return ""
        image_links = items[0].get("volumeInfo", {}).get("imageLinks") or {}
        cover = image_links.get("thumbnail") or image_links.get("smallThumbnail") or ""
        return cover.replace("http://", "https://")
    return ""


def main():
    target_day = sys.argv[1] if len(sys.argv) > 1 else (date.today() - timedelta(days=1)).isoformat()

    if day_path(target_day).exists():
        print(f"{target_day} ist schon archiviert ({day_path(target_day)}), ueberspringe.")
        return

    # complete=true bezieht auch die digitalen Onleihe/Overdrive-Angebote mit ein
    # (E-Books, eAudio) - ohne den Parameter fehlen die komplett (Test 2026-08-02:
    # 478 vs. 536 Treffer fuer denselben Tag).
    url = (
        f"{BASE}?verb=ListRecords&metadataPrefix=marc_xml"
        f"&set={SET_SPEC}&from={target_day}&until={target_day}&complete=true"
    )

    all_records = []
    page = 1
    while True:
        print(f"Seite {page} ...")
        body = fetch(url)
        if b"noRecordsMatch" in body:
            print(f"Keine Neuzugaenge fuer {target_day} (evtl. noch nicht katalogisiert).")
            return
        records, token, complete_size = parse_records(body)
        all_records.extend(records)
        print(f"  {len(records)} Datensaetze, bisher {len(all_records)}"
              + (f" von {complete_size}" if complete_size else ""))
        if not token:
            break
        url = f"{BASE}?verb=ListRecords&resumptionToken={token}"
        page += 1
        time.sleep(REQUEST_PAUSE_SECONDS)

    n_isbn = sum(1 for r in all_records if r["isbn"])
    print(f"\nSuche Cover (ekz.de -> Open Library -> Google Books) fuer {n_isbn} Datensaetze mit ISBN ...")
    if not GOOGLE_BOOKS_API_KEY:
        print("  (kein scripts/_secrets.py mit GOOGLE_BOOKS_API_KEY gefunden - letzter Fallback entfaellt)")
    from_ekz, from_ol, from_gb = 0, 0, 0
    for r in all_records:
        r["cover_url"] = ekz_cover(r["isbn"])
        if r["cover_url"]:
            from_ekz += 1
        else:
            r["cover_url"] = openlibrary_cover(r["isbn"])
            if r["cover_url"]:
                from_ol += 1
            else:
                r["cover_url"] = google_books_cover(r["isbn"])
                if r["cover_url"]:
                    from_gb += 1
        time.sleep(COVER_PAUSE_SECONDS)
    with_cover = from_ekz + from_ol + from_gb

    ARCHIVE_DIR.mkdir(exist_ok=True)
    out_path = day_path(target_day)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for r in all_records:
            writer.writerow({k: r.get(k, "") for k in FIELDNAMES})
    update_index(target_day)

    print(f"\n{len(all_records)} Neuzugaenge fuer {target_day} archiviert "
          f"({with_cover} mit Cover: {from_ekz} von ekz.de, {from_ol} von Open Library, "
          f"{from_gb} von Google Books) in {out_path}")


if __name__ == "__main__":
    main()
