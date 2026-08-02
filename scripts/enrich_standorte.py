#!/usr/bin/env python3
"""
Reichert data/standorte.csv um ALLE Steckbrief-Detaildaten an, die NICHT vom
offiziellen OpenData-Portal kommen - Kontakt, Services, Oeffnungszeiten,
"vor Ort"-Ausstattung, #aktuell-Meldungen. Einziges Anreicherungs-Skript im
Projekt (2026-08-02: build_data.py entfaellt, siehe README.md).

Frontend (index.html) laedt data/standorte.csv direkt per PapaParse - es gibt
keine generierte data/bibliotheken.json mehr. Fuer die drei Kennzahlen-Dateien
(besucherzahlen/bestand/entleihungen.csv) ist kein Python-Schritt noetig, die
werden im Browser direkt dazu-gejoint.

WICHTIG: Wird die Original-standorte.csv jemals neu von opendata.muenchen.de
heruntergeladen, fehlen alle hier ergaenzten Spalten wieder - dieses Skript
einfach erneut laufen lassen, um sie wiederherzustellen. AUSNAHME:
`dauerhaft_geschlossen` und `bild_url` werden NICHT automatisch neu ermittelt
(siehe unten), ein bestehender Wert bleibt jeweils beim Re-Run erhalten.
`bild_url` wird von diesem Skript nur als leere Spalte angelegt, falls sie
fehlt - die eigentliche Bildauswahl ist bewusst manuell (Heiko sucht pro
Standort ein passendes Foto von der jeweiligen MSB-Unterseite aus, siehe
README.md).

Aufruf: python3 scripts/enrich_standorte.py
"""
import csv
import json
import re
import ssl
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.parent
DATA = ROOT / "data"
CSV_PATH = DATA / "standorte.csv"

HOURS_URL = (
    "https://www.muenchner-stadtbibliothek.de/?type=951&uids="
    "29,30,31,34,32,33,23,1,3,4,5,6,8,7,9,39,38,10,11,12,13,14,24,15,16,17,18,27,19,20,21,22"
)

PREFIXES = ["Stadtteilbibliothek ", "Stadtbibliothek im ", "Stadtbibliothek "]

# slug (aus dem FontAwesome-Icon-Klassennamen der Standort-Unterseite,
# z.B. "fa-wifi") -> CSV-Spaltenname. "wheelchair" bewusst ausgelassen,
# siehe Docstring oben.
SERVICE_COLUMNS = {
    "wifi": "service_wlan",
    "print": "service_drucker",
    "gamepad": "service_gaming",
    "coffee": "service_cafeteria",
    "globe": "service_international",
    "battery-full": "service_ladestation",
    "parking": "service_parkplaetze",
}

# "vor Ort"-Akkordeon-Titel, die inhaltlich ein bestehendes Service-/OpenData-
# Feld doppeln koennten - Text wird dort zusammengefuehrt statt in einer
# eigenen vor_ort_*-Spalte zu landen. Prinzip (Heiko, 2026-08-02, Fund am
# Beispiel Riem): der Icon-Klicktext ist immer die BASIS (kurz, manchmal
# leer), ein inhaltlich passender Akkordeon-Eintrag ERGAENZT diese Basis
# (siehe merge_text() unten - haengt an, ueberschreibt nicht).
#
# Generisch fuer alle 7 Service-Kategorien angelegt, nicht nur die zwei
# tatsaechlich an allen 24 Standorten gefundenen Faelle (Fremdsprachige
# Medien, Barrierefreiheit) - falls ein Standort mal einen Akkordeon-Titel
# wie "Cafeteria" oder "WLAN" hat, soll das automatisch zusammengefuehrt
# werden statt eine redundante eigene Spalte zu bekommen. Titel, die (noch)
# bei keinem der 24 Standorte vorkommen, sind das beste Ratefeld basierend
# auf den Icon-Labels - taucht ein davon abweichender echter Titel auf,
# faellt er in die "unbekannter Titel"-WARNUNG (siehe unten) und muss hier
# nachgezogen werden.
#
# "Barrierefreiheit" ist bewusst eine Ausnahme: NICHT die offizielle
# OpenData-Spalte `barrierefreiheit` ueberschreiben (andere Provenienz!),
# sondern als eigene Spalte barrierefreiheit_detail ergaenzen.
VOR_ORT_MERGE_TITLES = {
    "Fremdsprachige Medien": "service_international",
    "Barrierefreiheit": "barrierefreiheit_detail",
    "International": "service_international",
    "Cafeteria": "service_cafeteria",
    "W-Lan": "service_wlan",
    "WLAN": "service_wlan",
    "Drucker": "service_drucker",
    "Gaming": "service_gaming",
    "Parkplätze": "service_parkplaetze",
    "Handy-Ladestation": "service_ladestation",
}


def merge_text(base, supplement):
    """Icon-Text ist Basis, Akkordeon-Text ergaenzt (haengt an) statt zu
    ueberschreiben - Prinzip siehe VOR_ORT_MERGE_TITLES oben."""
    base = (base or "").strip()
    if base == "ja":
        base = ""
    supplement = (supplement or "").strip()
    if base and supplement:
        return supplement if supplement in base else f"{base} {supplement}"
    return base or supplement

# Alle uebrigen "vor Ort"-Themen (Stand 2026-08-02, alle 24 Standorte
# durchsucht) -> CSV-Spaltenname. Fester Vokabular wie bei SERVICE_COLUMNS -
# taucht ein NEUER, hier unbekannter Titel auf, wird eine WARNUNG ausgegeben
# (er landet dann nirgends, muss hier von Hand ergaenzt werden).
VOR_ORT_COLUMNS = {
    "Arbeitsplätze": "vor_ort_arbeitsplaetze",
    "Bargeldlose Bibliothek": "vor_ort_bargeldlose_bibliothek",
    "Klimaanlage": "vor_ort_klimaanlage",
    "Konferenzraum": "vor_ort_konferenzraum",
    "Kreativ": "vor_ort_kreativ",
    "Kreislaufschrank": "vor_ort_kreislaufschrank",
    "Lastenfahrrad": "vor_ort_lastenfahrrad",
    "Lese- und Naschgarten": "vor_ort_lese_naschgarten",
    "Lesegarten": "vor_ort_lesegarten",
    "Musik": "vor_ort_musik",
    "Räume": "vor_ort_raeume",
    "Stadtteilinfo": "vor_ort_stadtteilinfo",
    "Studio": "vor_ort_studio",
    "Wickeltisch": "vor_ort_wickeltisch",
    "Zum Mitnehmen": "vor_ort_zum_mitnehmen",
}

CLOSURE_KEYWORDS = [
    "geschlossen", "schließ", "schliess", "öffnungszeiten", "personalmangel",
    "wasserschaden", "kalenderwoche", "eingeschränkt", "sanierung", "umbau",
    "ausfall", "vorübergehend", "ausnahmsweise", "geändert", "verkürzt",
]


def normalize(name):
    for p in PREFIXES:
        if name.startswith(p):
            return name[len(p):]
    return name


def fetch_places():
    ctx = ssl.create_default_context()
    ctx.load_verify_locations("/etc/ssl/cert.pem")
    req = urllib.request.Request(HOURS_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, context=ctx, timeout=20) as resp:
        text = resp.read().decode("utf-8")
    text = (
        text.replace('"coordinates":"', '"coordinates":[')
        .replace('","properties"', ']},"properties"')
        .replace("}}}", "}}")
    )
    return json.loads(text)


def fetch_page(url):
    ctx = ssl.create_default_context()
    ctx.load_verify_locations("/etc/ssl/cert.pem")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, context=ctx, timeout=20) as resp:
        return resp.read().decode("utf-8")


def extract_services(html):
    # Bugfix 2026-08-02 (Fund: Stadtteilbibliothek Moosach, Parkplaetze-Text
    # fehlte): Blockende NICHT ueber "</div></div>" erkennen - das "frisst"
    # versehentlich das schliessende </div> des LETZTEN Service-Icons mit
    # (das ist ja Teil der zwei aufeinanderfolgenden schliessenden divs),
    # wodurch dessen Text bei JEDEM Standort verloren ging, nicht nur bei
    # Moosach/Parkplaetze - welches Icon zuletzt in der Liste steht variiert
    # pro Standort. Stattdessen bis zum naechsten Content-Frame matchen
    # (verifiziert an mehreren Standort-Seiten stabil).
    m = re.search(r'theme_service_tabs.*?<div class="service-tabs tabs">(.*?)<div id="c\d+" class="frame', html, re.S)
    if not m:
        return []
    block = m.group(1)
    results = []
    for bm in re.finditer(
        r'<i class="fas (fa-[a-z0-9-]+)"[^>]*data-toggle="(toggle-[^"]+)"[^>]*></i>'
        r'<span class="sr-only">([^<]+)</span>',
        block,
    ):
        icon, toggle_id, label = bm.groups()
        slug = icon[3:]
        text = ""
        tm = re.search(r'id="' + re.escape(toggle_id) + r'"[^>]*>(.*?)</div>', block, re.S)
        if tm:
            text = re.sub(r"<[^>]+>", " ", tm.group(1))
            text = re.sub(r"\s+", " ", text).strip()
        results.append({"slug": slug, "label": label.strip(), "text": text})
    return results


# Reiner Navigations-/Verweis-Text ohne inhaltlichen Wert, der bei fast
# jedem Standort als eigener <p> im Barrierefreiheit-Absatz auftaucht (Fund
# 2026-08-02, 23 von 26 Standorten betroffen): ein Link "Zur Kultur
# barrierefrei Webseite" zu einer externen Bewertungsseite
# (kultur-barrierefrei-muenchen.de), typischerweise VOR dem eigentlichen
# Beschreibungstext. Wird grundsaetzlich rausgefiltert, nicht nur bei
# Barrierefreiheit - falls derselbe Linktext je in einem anderen "vor
# Ort"-Absatz auftaucht, soll er genauso verschwinden.
BOILERPLATE_TEXT = ["Zur Kultur barrierefrei Webseite"]


def extract_vor_ort(html):
    """Alle Titel+Text-Bloecke im "vor Ort"-Akkordeon der Standort-Unterseite."""
    # Bugfix 2026-08-02 (Fund: Stadtteilbibliothek Am Westkreuz): jede Seite hat
    # ZWEI "vor Ort"-Ueberschriften - eine im Navigationsmenue (immer "Vor Ort",
    # gefolgt von einem alten alt-Elemente-Feld) und die echte Inhaltssektion
    # (Gross-/Kleinschreibung variiert pro Standort! z.B. HP8: "vor Ort",
    # Westkreuz: "Vor Ort" - deshalb re.I). Reines "naechstes h2"-Matching
    # (auch mit re.I) trifft bei manchen Standorten faelschlich das Menue statt
    # der Inhaltssektion. Zuverlaessiges Unterscheidungsmerkmal: nur die echte
    # Inhaltssektion ist direkt von <section class="accordeon-container">
    # gefolgt.
    m = re.search(
        r'<h2 class="">\s*vor Ort\s*</h2>\s*</div>\s*<section class="accordeon-container">(.*?)<h2 class="">',
        html, re.S | re.I,
    )
    if not m:
        return []
    block = m.group(1)
    items = []
    for hm in re.finditer(
        r'<h3 class="">\s*(.*?)\s*</h3>(.*?)(?=<div id="c\d+"|</section>)', block, re.S
    ):
        title = re.sub(r"\s+", " ", hm.group(1)).strip()
        paras = re.findall(r"<p[^>]*>(.*?)</p>", hm.group(2), re.S)
        text = " ".join(re.sub(r"<[^>]+>", " ", p) for p in paras)
        text = re.sub(r"&nbsp;", " ", text)
        for junk in BOILERPLATE_TEXT:
            text = text.replace(junk, " ")
        text = re.sub(r"\s+", " ", text).strip()
        if title or text:
            items.append({"titel": title, "text": text})
    return items


def extract_aktuell_notice(html):
    """
    "#aktuell"-Abschnitt einer Standort-Unterseite - allgemeiner News-Block,
    keine reine Schliessungs-Meldung (auch Ehrenamts-Aufrufe o.ae. moeglich).
    Nur Treffer mit Schliessungs-/Oeffnungszeiten-Schluesselwoertern
    (CLOSURE_KEYWORDS) werden uebernommen, um keine falschen Warnhinweise zu
    speichern. Struktur: <h2>#aktuell</h2> -> <h3>Titel</h3> + <p>-Absaetze,
    endet immer vor <h3>Feiertage ...</h3>.
    """
    m = re.search(r'<h2 class="">\s*#aktuell\s*</h2>(.*?)<h3 class="">\s*Feiertage', html, re.S)
    if not m:
        return None
    block = m.group(1)
    title_m = re.search(r'<h3 class="">\s*(.*?)\s*</h3>', block, re.S)
    if not title_m:
        return None
    title = re.sub(r"<[^>]+>", " ", title_m.group(1))
    title = re.sub(r"\s+", " ", title).strip()
    paras = re.findall(r"<p>(.*?)</p>", block, re.S)
    text = " ".join(re.sub(r"<[^>]+>", " ", p) for p in paras)
    text = re.sub(r"\s+", " ", text).strip()
    if not title and not text:
        return None

    haystack = (title + " " + text).lower()
    if not any(kw in haystack for kw in CLOSURE_KEYWORDS):
        return None

    return {"titel": title, "text": text}


def main():
    places = fetch_places()

    by_norm = {}
    by_full = {}
    for feature in places:
        p = feature["properties"]
        info = {
            "email": p.get("mail", ""),
            "telefon": p.get("phone", ""),
            "rueckgabezeiten": p.get("returnHours", ""),
            "oeffnungszeiten_wochentags": p.get("hoursWeekdays", ""),
            "oeffnungszeiten_samstag": p.get("hoursSaturday", ""),
        }
        by_norm[normalize(p["name"])] = info
        by_full[p["name"]] = info

    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    new_cols = (
        ["email", "telefon", "rueckgabezeiten", "oeffnungszeiten_wochentags", "oeffnungszeiten_samstag"]
        + list(SERVICE_COLUMNS.values())
        + ["barrierefreiheit_detail"]
        + list(VOR_ORT_COLUMNS.values())
        + ["aktuell_titel", "aktuell_text", "dauerhaft_geschlossen", "bild_url"]
    )
    for col in new_cols:
        if col not in fieldnames:
            fieldnames = fieldnames + [col]

    unmatched = []
    unknown_titles = set()
    print(f"Hole Kontakt/Oeffnungszeiten/Services/Ausstattung/#aktuell von {len(rows)} Standort-Unterseiten ...")
    for row in rows:
        norm = normalize(row["bib_name"])
        info = by_norm.get(norm)
        if not info:
            # Fallback: Teilstring-Suche (z.B. "Juristische Bibliothek" vs.
            # "Juristische Bibliothek im Rathaus" - unterschiedliche Namen,
            # betrifft nur die 2 Sonderbibliotheken außerhalb der 24 Kartenstandorte)
            for full_name, candidate in by_full.items():
                if norm in full_name or full_name.split(" im ")[0] in row["bib_name"]:
                    info = candidate
                    break
        if info:
            row.update(info)
        else:
            row.update({c: "" for c in [
                "email", "telefon", "rueckgabezeiten",
                "oeffnungszeiten_wochentags", "oeffnungszeiten_samstag",
            ]})
            unmatched.append(row["bib_name"])

        row.update({c: "" for c in SERVICE_COLUMNS.values()})
        row["barrierefreiheit_detail"] = ""
        row.update({c: "" for c in VOR_ORT_COLUMNS.values()})
        row["aktuell_titel"] = ""
        row["aktuell_text"] = ""
        # dauerhaft_geschlossen und bild_url bewusst NICHT zurueckgesetzt - manuell
        # gepflegte Werte (siehe README), bleiben ueber Re-Runs erhalten falls schon gesetzt.
        row.setdefault("dauerhaft_geschlossen", "")
        row.setdefault("bild_url", "")

        if row.get("webseite"):
            try:
                html = fetch_page(row["webseite"])

                for s in extract_services(html):
                    col = SERVICE_COLUMNS.get(s["slug"])
                    if col:
                        row[col] = s["text"] or "ja"
                    elif s["slug"] == "wheelchair" and s["text"]:
                        # "wheelchair"-Icon ist bewusst NICHT in SERVICE_COLUMNS (keine
                        # eigene Badge, doppelt sich mit dem Facts-Grid-Feld
                        # `barrierefreiheit`) - der Kurztext selbst kann aber ueber die
                        # offizielle Spalte hinausgehende Details haben (Fund 2026-08-02,
                        # HP8: Icon-Text nennt zusaetzlich Behindertenparkplaetze). Wird
                        # deshalb als Basis fuer barrierefreiheit_detail uebernommen -
                        # der Akkordeon-Text (siehe VOR_ORT_MERGE_TITLES) ergaenzt das
                        # dann wie gehabt per merge_text().
                        row["barrierefreiheit_detail"] = merge_text(row.get("barrierefreiheit_detail", ""), s["text"])

                for item in extract_vor_ort(html):
                    title = item["titel"]
                    if title in VOR_ORT_MERGE_TITLES:
                        col = VOR_ORT_MERGE_TITLES[title]
                        row[col] = merge_text(row.get(col, ""), item["text"])
                    elif title in VOR_ORT_COLUMNS:
                        row[VOR_ORT_COLUMNS[title]] = item["text"] or "ja"
                    else:
                        unknown_titles.add((row["bib_name"], title))

                notice = extract_aktuell_notice(html)
                if notice:
                    row["aktuell_titel"] = notice["titel"]
                    row["aktuell_text"] = notice["text"]
            except Exception as e:
                print(f"WARNUNG: Detailseite fuer {row['bib_name']} nicht abrufbar: {e}")
        time.sleep(0.3)

    with open(CSV_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"{len(rows)} Zeilen in {CSV_PATH.relative_to(ROOT)} angereichert ({len(new_cols)} Spalten)")
    if unmatched:
        print(f"WARNUNG: keine Kontaktdaten/Oeffnungszeiten gefunden fuer: {unmatched}")
    if unknown_titles:
        print(f"WARNUNG: unbekannte 'vor Ort'-Titel gefunden (nicht gespeichert, bitte VOR_ORT_COLUMNS ergaenzen): {sorted(unknown_titles)}")


if __name__ == "__main__":
    main()
