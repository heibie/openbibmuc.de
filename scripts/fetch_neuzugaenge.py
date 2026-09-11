#!/usr/bin/env python3
"""
Holt Neuzugaenge der Muenchner Stadtbibliothek aus dem OAI-PMH-Katalog
(https://data-bib.muenchen.de/oai-pmh, Gesamtbestand DE-M36), reichert sie ueber
die ISBN mit einem Cover-Bild an (erst ekz.de, dann Open Library, dann Google
Books als Fallback) und schreibt sie nach data/neuzugaenge/<TAG>.csv - eine
Datei pro Tag, damit das Frontend tageweise blaettern kann.
data/neuzugaenge/index.json listet alle bisher geholten Tage.

WAS "NEUZUGANG" HEISST (geaendert 2026-09-09)
============================================
Frueher wurde per OAI-Datumsfilter `from`/`until` genau ein Tag geholt und
komplett als "Neuzugaenge dieses Tages" abgelegt. Problem: `from`/`until` filtert
auf den OAI-*datestamp* = LETZTE AENDERUNG des Datensatzes. Jede Mini-Bearbeitung
(Re-Indexierung, Bestandsaenderung, Normdaten-Update) hebt den datestamp - und
schon taucht ein Uralt-Datensatz als "Neuzugang" auf (Rueckmeldung der
Stadtbibliothek 2026-09: eine Zeitschrift von 1934 auf Seite 1). Messung am
2026-09-08: von 948 "Neuzugaengen" waren nur 143 (15%) wirklich neu katalogisiert,
75% aelter als 2026.

Loesung (Hinweis der Stadtbibliothek): MARC-Controlfield **008, Positionen 00-05**
= Erfassungsdatum des Datensatzes im Format JJMMTT. Dieses Feld ist fix und
aendert sich nach der Neuaufnahme nie (anders als der datestamp). In der Stichprobe
zu 100% befuellt und sauber parsebar.

Neuer Ablauf:
1. Weiterhin per datestamp harvesten (die API kann NICHT nach 008 filtern), aber
   ein ROLLIERENDES FENSTER der letzten HARVEST_WINDOW_DAYS Tage - nicht nur
   "gestern". Grund: der datestamp eines neu erfassten Satzes kommt teils Tage
   nach dem 008-Datum (am 2026-09-08 tauchten noch 008-Daten vom 01.-04.09. auf).
2. Pro Datensatz das 008-Datum parsen (Century-Pivot: JJ < 70 -> 20JJ).
3. Nur behalten, wenn das 008-Datum aktuell ist (Standardlauf: <= heute und
   hoechstens KEEP_MAX_AGE_DAYS alt).
4. Einsortieren nach 008-Datum, NICHT nach Harvest-Tag: ein Satz mit 008 = 04.09.
   landet in data/neuzugaenge/2026-09-04.csv.
5. Tagesdateien werden GEMERGT (Dedup ueber MARC-001), nicht "ueberspringen wenn
   vorhanden" - ein Tag bekommt bei Folgelaeufen noch Nachzuegler. Cover werden
   nur fuer wirklich neu hinzugekommene Zeilen geholt.

Aufruf
======
  python3 scripts/fetch_neuzugaenge.py
      Standard-Cron-Lauf: rollierendes Fenster (heute-HARVEST_WINDOW_DAYS .. heute),
      Datensatz behalten wenn 008-Datum in den letzten KEEP_MAX_AGE_DAYS Tagen.

  python3 scripts/fetch_neuzugaenge.py YYYY-MM-DD
      Einen einzelnen datestamp-Tag nachholen (from=until=Tag), gleiche
      Behalten-Regel wie oben. Fuer manuell verpasste Tage.

  python3 scripts/fetch_neuzugaenge.py --rebuild
      Historie neu aufbauen: alle vorhandenen Tagesdateien loeschen, breit
      harvesten (ab REBUILD_FLOOR) und sauber nach 008-Datum neu schreiben.
      Cover werden dabei aus den alten CSVs weiterverwendet, soweit vorhanden.
      Laeuft lange (zig Minuten, in CHUNK_DAYS-Haeppchen) - einmalige lokale Aktion.

  python3 scripts/fetch_neuzugaenge.py --rebuild YYYY-MM-DD
      Rebuild ab diesem datestamp-Tag FORTSETZEN (loescht nichts) - fuer den Fall,
      dass ein voller --rebuild unterwegs abgebrochen ist.

  python3 scripts/fetch_neuzugaenge.py YYYY-MM-DD YYYY-MM-DD
      Expliziter datestamp-Bereich (from until), Behalten-Regel: 008-Datum
      >= dem Von-Datum. Fuer gezielte Teil-Rebuilds.

Cover-Quellen (erster Treffer gewinnt), unveraendert:
1. ekz.de (cover.ekz.de/<ISBN>.jpg) - beste Trefferquote fuer den deutschsprachigen
   Bestand (~91-94%). 2. Open Library (schwache deutsche Abdeckung). 3. Google
   Books (braucht scripts/_secrets.py mit GOOGLE_BOOKS_API_KEY, Backend oft 503).
NICHT genutzt: isbn.de (robots.txt), ssl.muenchen.de/vlb (403), Goodreads (keine
API), DNB (keine Cover). Details siehe README.md.

Warum marc_xml statt oai_dc: bei identischer Abfrage liefert oai_dc nachweislich
weniger Treffer (Test 2026-08-02). Warum complete=true: bezieht die digitalen
Onleihe/Overdrive-Angebote mit ein.
"""
import csv
import glob
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    from _secrets import GOOGLE_BOOKS_API_KEY
except ImportError:
    GOOGLE_BOOKS_API_KEY = None

BASE = "https://data-bib.muenchen.de/oai-pmh"
SET_SPEC = "DE-M36"

# Rollierendes datestamp-Fenster pro Standardlauf.
HARVEST_WINDOW_DAYS = 14
# Ein Datensatz gilt als "Neuzugang", wenn sein 008-Erfassungsdatum hoechstens so
# viele Tage zurueckliegt. Grosszuegig gewaehlt, um die beobachtete Verzoegerung
# zwischen 008-Datum und datestamp (bis ~2 Wochen) sicher abzudecken.
KEEP_MAX_AGE_DAYS = 45
# Ab hier beginnt die Historie beim --rebuild (erster je erfasste Tag des Features).
REBUILD_FLOOR = date(2026, 7, 26)

# Pause zwischen Requests - ohne die blockt die WAF der Stadt gelegentlich mit
# 503 "Access Denied" (bekanntes Verhalten, siehe README, Abschnitt OAI-PMH).
REQUEST_PAUSE_SECONDS = 2
COVER_PAUSE_SECONDS = 0.3
# Google Books' Backend antwortet gerade oft mit transientem 503 "backendFailed"
# (unabhaengig vom API-Key) - ohne Retry wuerden wir echte Cover faelschlich als
# "nicht gefunden" werten.
GOOGLE_BOOKS_RETRIES = 3
GOOGLE_BOOKS_RETRY_PAUSE_SECONDS = 3

NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "marc": "http://www.loc.gov/MARC21/slim",
}

# Rohwert aus 245$h -> das freundlichere Label, das der MSB-OPAC selbst als
# "Medienart" anzeigt (per Stichprobe an 11 Datensaetzen am 2026-08-02 manuell im
# OPAC nachgeschaut). Werte ohne Eintrag hier werden unveraendert durchgereicht.
GMD_DISPLAY_LABELS = {
    "Druckschrift": "Buch",
    "DVD-Video": "DVD",
    "MP3-CD": "MP3",
    "Blu-Ray": "Blu-ray Disc",
    "Spiel": "Brettspiel",
    "Buch + CD": "Medienkombination",
    "Buch + DVD-Video + CD": "Medienkombination",
}

# Nur Fallback, falls 245$h fehlt. MARC-Leader Position 6 (Type of Record) ->
# deutsches Label wie beim Muenchner OAI-PMH-Server fuer dc:type.
TYPE_LABELS = {
    "a": "Druckschrift", "t": "Druckschrift",
    "g": "visuelle Materialien", "k": "visuelle Materialien",
    "m": "Dateien",
    "c": "Musikalien", "d": "Musikalien", "j": "Musikalien",
    "e": "Karten", "f": "Karten",
}

DATA = Path(__file__).resolve().parent.parent / "data"
ARCHIVE_DIR = DATA / "neuzugaenge"
INDEX_PATH = ARCHIVE_DIR / "index.json"
FIELDNAMES = [
    "id", "created", "datestamp", "title", "subtitle", "creator", "year",
    "type", "language", "isbn", "subjects", "cover_url", "opac_url",
]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def day_path(day):
    return ARCHIVE_DIR / f"{day}.csv"


def rewrite_index():
    """index.json aus dem Verzeichnisinhalt neu aufbauen (selbstheilend)."""
    days = sorted(p.stem for p in ARCHIVE_DIR.glob("*.csv"))
    INDEX_PATH.write_text(
        json.dumps({"days": days}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return days


# Wartezeiten (Sekunden) bei nicht-OAI-Antwort. Die WAF der Stadt blockt bei zu
# vielen Requests laenger als nur ein paar Sekunden ("Access Denied", siehe
# README) - deshalb wachsende Backoffs bis ~5 min. Ein Resumption-Token lebt
# ~1 h, ein einzelner Aussetzer ist damit ueberbbrueckbar. Summe ~11 min.
FETCH_BACKOFFS = [10, 30, 60, 120, 180, 300]


def fetch(url):
    # curl statt urllib: auf manchen Rechnern fehlt Python der Zertifikatsspeicher
    # fuer diese Domain (SSL-Fehler), curl nutzt den System-Truststore.
    for attempt, wait in enumerate([0] + FETCH_BACKOFFS, start=1):
        if wait:
            print(f"  keine OAI-Antwort - warte {wait}s (Versuch {attempt}/{len(FETCH_BACKOFFS) + 1}) ...")
            time.sleep(wait)
        result = subprocess.run(
            ["curl", "-s", "-A", USER_AGENT, url],
            capture_output=True, timeout=45,
        )
        if b"<OAI-PMH" in result.stdout:
            return result.stdout
    raise RuntimeError(f"Abruf endgueltig fehlgeschlagen: {url}")


def clean_isbn(raw):
    return re.sub(r"[^0-9Xx]", "", raw or "")


def best_isbn(isbns):
    """Bevorzugt die 13-stellige ISBN (aktueller Standard), sonst erste verfuegbare."""
    thirteen = [i for i in isbns if len(i) == 13]
    return thirteen[0] if thirteen else (isbns[0] if isbns else "")


def opac_url(record_id):
    """Link zum Original-Katalogeintrag im MSB-OPAC, reine URL-Konstruktion aus
    MARC-Feld 001 (Format 'AK<Nummer>'), KEIN Abruf. Muster bestaetigt 2026-08-02:
    'AK4664719' -> sp=SAK04664719."""
    m = re.match(r"^([A-Z]+)(\d+)$", record_id or "")
    if not m:
        return ""
    modul, nummer = m.groups()
    return f"https://ssl.muenchen.de/aDISWeb/app/opac?sp=S{modul}{nummer.zfill(8)}"


def marc_008_date(field008):
    """MARC 008 Pos. 00-05 (JJMMTT, Erfassungsdatum) -> date oder None.
    Century-Pivot: JJ < 70 -> 20JJ, sonst 19JJ (fuer Neuzugaenge ohnehin nur 20xx
    relevant, aber sauber abgesichert). Ungueltige/Fuellzeichen -> None."""
    head = (field008 or "")[:6]
    if len(head) != 6 or not head.isdigit():
        return None
    yy, mm, dd = int(head[:2]), int(head[2:4]), int(head[4:6])
    year = 2000 + yy if yy < 70 else 1900 + yy
    try:
        return date(year, mm, dd)
    except ValueError:
        return None


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
        field008 = marc.findtext("marc:controlfield[@tag='008']", default="", namespaces=NS)
        created = marc_008_date(field008)

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

        gmd_raw = gmd_vals[0].strip("[]") if gmd_vals else ""
        gmd = GMD_DISPLAY_LABELS.get(gmd_raw, gmd_raw)

        records.append({
            "id": record_id,
            "created": created.isoformat() if created else "",
            "_created_date": created,
            "datestamp": datestamp[:10],
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


def harvest(date_from, date_until):
    """Alle Datensaetze im datestamp-Fenster [date_from, date_until] holen
    (paginiert). Liefert die geparsten Records (noch ungefiltert)."""
    url = (
        f"{BASE}?verb=ListRecords&metadataPrefix=marc_xml"
        f"&set={SET_SPEC}&from={date_from}&until={date_until}&complete=true"
    )
    all_records = []
    page = 1
    # Sicherheitsnetz: der OAI-Server der Stadt liefert nach dem Ende der Liste
    # teils WEITER einen Resumption-Token mit (statt eines leeren Elements laut
    # Spec) und dann seitenweise 0 Datensaetze. Ohne Abbruch bei leerer Seite
    # laeuft das endlos (real erlebt: 16.000+ Leerseiten). Deshalb: Stopp,
    # sobald eine Seite nichts mehr liefert, plus harte Seitenobergrenze.
    MAX_PAGES = 4000
    while True:
        body = fetch(url)
        if b"noRecordsMatch" in body:
            print(f"  Keine Datensaetze im Fenster {date_from}..{date_until}.")
            break
        records, token, complete_size = parse_records(body)
        all_records.extend(records)
        print(f"  Seite {page}: {len(records)} Datensaetze, bisher {len(all_records)}"
              + (f" von {complete_size}" if complete_size else ""))
        if not records:
            break
        if not token:
            break
        if page >= MAX_PAGES:
            raise RuntimeError(f"Seitenobergrenze {MAX_PAGES} erreicht - Abbruch "
                               f"(bisher {len(all_records)} Datensaetze).")
        url = f"{BASE}?verb=ListRecords&resumptionToken={token}"
        page += 1
        time.sleep(REQUEST_PAUSE_SECONDS)
    return all_records


# ---------------------------------------------------------------- Cover-Quellen

def ekz_cover(isbn):
    if not isbn:
        return ""
    url = f"https://cover.ekz.de/{isbn}.jpg"
    result = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-A", USER_AGENT, url],
        capture_output=True, text=True, timeout=15,
    )
    return url if result.stdout.strip() == "200" else ""


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
    if not isbn or not GOOGLE_BOOKS_API_KEY:
        return ""
    url = f"https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}&key={GOOGLE_BOOKS_API_KEY}"
    for _ in range(GOOGLE_BOOKS_RETRIES):
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


def build_cover_cache():
    """ISBN -> cover_url aus allen bereits archivierten Tagesdateien. Spart beim
    Merge/Rebuild die erneute Cover-Suche fuer schon bekannte Titel."""
    cache = {}
    for path in ARCHIVE_DIR.glob("*.csv"):
        with open(path, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                isbn, cover = row.get("isbn", ""), row.get("cover_url", "")
                if isbn and cover and isbn not in cache:
                    cache[isbn] = cover
    return cache


def resolve_cover(isbn, cache):
    if not isbn:
        return ""
    if isbn in cache:
        return cache[isbn]
    cover = ekz_cover(isbn) or openlibrary_cover(isbn) or google_books_cover(isbn)
    time.sleep(COVER_PAUSE_SECONDS)
    if cover:
        cache[isbn] = cover
    return cover


def row_keys(row):
    """Dedup-Schluessel einer Zeile: MARC-001 UND OPAC-URL (1:1 zur 001, aber auch
    in Alt-CSVs ohne id-Spalte vorhanden). So werden bestehende Zeilen auch nach
    dem Schemawechsel zuverlaessig wiedererkannt."""
    return {row.get("id", ""), row.get("opac_url", "")} - {""}


def read_day(day):
    """Bestehende Tagesdatei -> (Liste aller Zeilen, Menge bekannter Dedup-Schluessel).
    Alle vorhandenen Zeilen bleiben erhalten - auch solche ohne id-Spalte."""
    path = day_path(day)
    if not path.exists():
        return [], set()
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    seen = set()
    for r in rows:
        seen |= row_keys(r)
    return rows, seen


def write_day(day, rows):
    path = day_path(day)
    rows = sorted(rows, key=lambda r: (
        r.get("created", ""), r.get("id") or r.get("opac_url") or r.get("title", "")
    ))
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in FIELDNAMES})


# ------------------------------------------------------------------------- main

# Das datestamp-Fenster wird in Haeppchen dieser Groesse geharvestet und pro
# Haeppchen sofort verarbeitet + gespeichert. Bricht ein Lauf ab (WAF-Block,
# Token-Ablauf), gehen nur die Datensaetze des laufenden Haeppchens verloren -
# ein erneuter Lauf holt sie idempotent nach.
CHUNK_DAYS = 7


def iter_chunks(date_from, date_until, chunk_days):
    start = date_from
    while start <= date_until:
        end = min(start + timedelta(days=chunk_days - 1), date_until)
        yield start, end
        start = end + timedelta(days=1)


def process_window(w_from, w_until, keep, cover_cache):
    """Ein datestamp-Fenster holen, nach 008-Datum filtern, in die Tagesdateien
    mergen + schreiben. Gibt Liste (tag, vorher, hinzu, nachher) der geaenderten Tage."""
    print(f"Harvest datestamp-Fenster {w_from} .. {w_until} ...")
    raw = harvest(w_from, w_until)

    by_day = {}
    dropped = 0
    for r in raw:
        d = r["_created_date"]
        if not keep(d):
            dropped += 1
            continue
        by_day.setdefault(d.isoformat(), []).append(r)
    kept = sum(len(v) for v in by_day.values())
    print(f"  {len(raw)} geholt | behalten {kept} | verworfen (008 zu alt/fehlt/kuenftig) {dropped}")

    touched = []
    for day in sorted(by_day):
        rows, seen = read_day(day)
        before = len(rows)
        added_here = 0
        for r in by_day[day]:
            if row_keys(r) & seen:
                continue
            r["cover_url"] = resolve_cover(r["isbn"], cover_cache)
            rows.append(r)
            seen |= row_keys(r)
            added_here += 1
        if added_here or not day_path(day).exists():
            write_day(day, rows)
            touched.append((day, before, added_here, len(rows)))
    return touched


def main():
    args = [a for a in sys.argv[1:] if a]
    rebuild = "--rebuild" in args
    args = [a for a in args if a != "--rebuild"]
    today = date.today()

    if rebuild:
        date_from = date.fromisoformat(args[0]) if args else REBUILD_FLOOR
        date_until = today
        keep = lambda d: d is not None and REBUILD_FLOOR <= d <= today
        mode = f"REBUILD ab {date_from} (008-Datum {REBUILD_FLOOR} .. {today})"
    elif len(args) == 2:
        date_from = date.fromisoformat(args[0])
        date_until = date.fromisoformat(args[1])
        keep = lambda d: d is not None and date_from <= d <= today
        mode = f"Bereich {date_from} .. {date_until} (008-Datum ab {date_from})"
    elif len(args) == 1:
        date_from = date_until = date.fromisoformat(args[0])
        floor = today - timedelta(days=KEEP_MAX_AGE_DAYS)
        keep = lambda d: d is not None and floor <= d <= today
        mode = f"Einzeltag {date_from} (008-Datum der letzten {KEEP_MAX_AGE_DAYS} Tage)"
    else:
        date_until = today
        date_from = today - timedelta(days=HARVEST_WINDOW_DAYS)
        floor = today - timedelta(days=KEEP_MAX_AGE_DAYS)
        keep = lambda d: d is not None and floor <= d <= today
        mode = f"rollierend {date_from} .. {date_until} (008-Datum der letzten {KEEP_MAX_AGE_DAYS} Tage)"

    print(f"Modus: {mode}")
    ARCHIVE_DIR.mkdir(exist_ok=True)

    # Cover-Cache VOR dem evtl. Loeschen der alten Dateien aufbauen.
    cover_cache = build_cover_cache()
    print(f"Cover-Cache: {len(cover_cache)} ISBN->Cover aus bestehenden Tagesdateien")

    if rebuild and not args:  # nur beim vollen Rebuild von vorn wird geleert
        old = list(ARCHIVE_DIR.glob("*.csv"))
        for p in old:
            p.unlink()
        print(f"REBUILD: {len(old)} bestehende Tagesdateien geloescht")

    chunks = list(iter_chunks(date_from, date_until, CHUNK_DAYS))
    touched_by_day = {}
    for i, (c_from, c_until) in enumerate(chunks, start=1):
        print(f"\n--- Haeppchen {i}/{len(chunks)} ({c_from} .. {c_until}) ---")
        for day, before, added, after in process_window(c_from, c_until, keep, cover_cache):
            if day in touched_by_day:
                b0, a0, _ = touched_by_day[day]
                touched_by_day[day] = (b0, a0 + added, after)
            else:
                touched_by_day[day] = (before, added, after)
        rewrite_index()  # nach jedem Haeppchen: Fortschritt festhalten

    days = rewrite_index()
    total_added = sum(a for _, a, _ in touched_by_day.values())

    print("\n================ ZUSAMMENFASSUNG ================")
    if touched_by_day:
        for day in sorted(touched_by_day):
            before, added, after = touched_by_day[day]
            print(f"  {day}: +{added}  ({before} -> {after})")
    else:
        print("  Keine Aenderungen.")
    if days:
        print(f"\n  {total_added} Datensaetze neu hinzugefuegt, "
              f"{len(days)} Tage im Index ({days[0]} .. {days[-1]})")
    else:
        print("\n  (Index leer)")


if __name__ == "__main__":
    main()
