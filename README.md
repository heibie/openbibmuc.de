# OpenData Münchner Stadtbibliothek

Quellen: https://opendata.muenchen.de (Suche "stadtbibliothek") + https://blog.muenchner-stadtbibliothek.de/open-meta-data/

**Live:** https://openbibmuc.de · **Repo:** https://github.com/heibie/openbibmuc.de

## Deployment

GitHub-Repo `heibie/openbibmuc.de`, Branch `main`. Jeder Push auf `main` löst automatisch
`.github/workflows/deploy.yml` aus (rsync über SSH nach All-Inkl, Muster identisch zu
rathausmuc.de/data.parkraumwende.de). Ausgeschlossen vom Deploy: `.git`, `.github`,
`__pycache__`, `*.py`, `*.bak-*`, `scripts/retired`, `variants`, `cron/_cfg.php`.

Nötige GitHub-Secrets (Repo-Settings → Secrets and variables → Actions):
- `SSH_PRIVATE_KEY`, `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_PATH` — für den Deploy-Workflow
- `GOOGLE_BOOKS_API_KEY` — für den naechtlichen Neuzugänge-Fetch (siehe unten)

`cron/_cfg.php` (Cron-Token + GitHub Fine-grained PAT für `check_sources.php`, siehe unten)
ist gitignored und liegt nur lokal + manuell hochgeladen auf dem Server, niemals im Repo.

### Zwei nächtliche Automatisierungen

1. **Neuzugänge abholen** (`.github/workflows/neuzugaenge.yml`, taeglich 3 Uhr UTC):
   läuft als GitHub-Actions-Workflow (nicht auf All-Inkl, da dort kein Python verfügbar ist —
   deshalb ist `check_sources.php` ja überhaupt erst in PHP portiert worden). Ruft
   `scripts/fetch_neuzugaenge.py` ohne Argument auf (= "gestern"), committet das Ergebnis
   nach `data/neuzugaenge/`, was automatisch den Deploy-Workflow anstößt.
2. **Quellen-Status-Check** (`cron/check_sources.php`): läuft SERVERSEITIG auf All-Inkl per
   KAS-Cronjob (im All-Inkl-Kundenmenü selbst einzurichten, empfohlen wöchentlich), URL:
   `https://openbibmuc.de/cron/check_sources.php?token=<CRON_TOKEN>`. Committet
   `data/source-status.json` bei Änderung per GitHub-Git-Data-API — stößt ebenfalls
   automatisch den Deploy-Workflow an.

## Datensätze (`data/`)

- **standorte.csv** — 26 Standorte (2 Zentralbibliotheken, 22 Stadtteilbibliotheken, 2 Sonderbibliotheken: Juristische Bibliothek, Monacensia). Ursprüngliche OpenData-Spalten: `bib_type, bib_name, bib_abkuerzung, bib_strasse, bib_plz, lat, lon, publikumsflaeche, anzahl_arbeitsplaetze, webseite, open_library, barrierefreiheit`. Stand 02.06.2025. Lizenz: DL-DE-BY 2.0.

  **⚠ Komplett angereichert (2026-08-01/02):** ALLE Steckbrief-Detaildaten stehen mittlerweile direkt in dieser Datei, nicht nur Kontakt/Services wie ursprünglich. `scripts/enrich_standorte.py` ist das einzige Anreicherungs-Skript im Projekt und schreibt:
  - `email`, `telefon`, `rueckgabezeiten` — aus dem AJAX-Endpunkt (siehe unten)
  - `oeffnungszeiten_wochentags`, `oeffnungszeiten_samstag` — Freitext aus demselben Endpunkt, wird im Frontend geparst (siehe Abschnitt "Öffnungszeiten")
  - `service_wlan`, `service_drucker`, `service_gaming`, `service_cafeteria`, `service_international`, `service_ladestation`, `service_parkplaetze` — von den 24 Standort-Unterseiten (siehe Abschnitt "Services")
  - `barrierefreiheit_detail` — Langtext aus dem "vor Ort"-Akkordeon, ergänzt die offizielle Kurz-Spalte `barrierefreiheit`, überschreibt sie NICHT (andere Provenienz)
  - 14 `vor_ort_*`-Spalten (z.B. `vor_ort_arbeitsplaetze`, `vor_ort_lastenfahrrad`, `vor_ort_kreislaufschrank`, ...) — der Rest der "vor Ort"-Liste, siehe Abschnitt "vor Ort"-Ausstattung
  - `aktuell_titel`, `aktuell_text` — Ad-hoc-Schließungsmeldungen, siehe Abschnitt "Ad-hoc-Schließungen"
  - `dauerhaft_geschlossen` (Ja/Nein) — **manuell gepflegt**, wird von `enrich_standorte.py` bei Re-Runs NICHT automatisch zurückgesetzt/neu ermittelt (kein Text-Heuristik-Versuch, siehe Abschnitt "Ad-hoc-Schließungen")
  - `bild_url` — **manuell gepflegt** (seit 2026-08-02): URL eines Fotos für den Steckbrief. Jede MSB-Standort-Unterseite hat weiter unten einen "Impressionen"-Bilderslider (Innenaufnahmen, KEINE Gebäude-Außenansichten vorhanden), Bilder dort als `<figure><a class="lightbox" href="/fileadmin/_processed_/.../csm_NAME_HASH.jpg" data-lity-desc="Alt-Text">` (Lightbox-Version, ~800×533px) verlinkt. Heiko sucht pro Standort manuell ein passendes Bild aus und trägt die volle `https://www.muenchner-stadtbibliothek.de/fileadmin/...`-URL ein. `enrich_standorte.py` legt die Spalte nur an (leer), wählt aber nichts automatisch aus und überschreibt bestehende Werte bei Re-Runs nicht.

  Die `service_*`- und `vor_ort_*`-Spalten enthalten entweder einen Beschreibungstext oder schlicht `ja` (wenn die Seite kein Detailtext hatte). **Absichtlich weggelassen:** eine `service_wheelchair`-Spalte — deckt sich beim Gegenprüfen fast immer mit der schon vorhandenen `barrierefreiheit`-Spalte. Falls `standorte.csv` je neu von opendata.muenchen.de heruntergeladen wird, fehlen alle diese Spalten wieder — `python3 scripts/enrich_standorte.py` erneut laufen lassen, um sie wiederherzustellen (außer `dauerhaft_geschlossen` und `bild_url`, die danach von Hand neu gesetzt werden müssten).
- **besucherzahlen.csv** / **bestand.csv** / **entleihungen.csv** — je 522 Zeilen, monatlich Jan 2024–Jun 2025, pro Standort. Spalten: `jahr_monat, bibliothekssigel, bibliothek, anzahl_*`. 29 Standorte inkl. 5 Bücherbusse (mobile Bibliotheken, keine feste Adresse). Lizenz: DL-DE-BY 2.0.

### Wichtiger Fallstrick: Standortnamen unterscheiden sich zwischen den Dateien
`standorte.csv` nennt z.B. "Stadtteilbibliothek Bogenhausen", die Kennzahlen-Dateien nur "Bogenhausen" (+ eigenes `bibliothekssigel`, das in `standorte.csv` gar nicht vorkommt). Die Zentralbibliotheken ("Stadtbibliothek im Motorama"/"...im HP8") behalten dagegen ihren vollen Namen in beiden Dateien. Verknüpfung nur über normalisierten Namen möglich (Präfix "Stadtteilbibliothek " entfernen, "Stadtbibliothek im " NICHT). Die 5 Bücherbusse und die 2 Zentralbibliotheken (Motorama, HP8) haben eigene Kennzahlen; Juristische Bibliothek + Monacensia tauchen in den Kennzahlen-Dateien gar nicht auf.

### Öffnungszeiten (nicht im offiziellen OpenData-Portal)

Gibt es dort nicht als Datensatz. Stattdessen kommen sie von einem öffentlichen, unauthentifizierten AJAX-Endpunkt der MSB-Website selbst (`https://www.muenchner-stadtbibliothek.de/?type=951&uids=...`), der die interaktive Karte auf `muenchner-stadtbibliothek.de` speist (gefunden über `map.js`/`map_detail.js` der Seite). Liefert pro Standort u.a. `hoursWeekdays`/`hoursSaturday` als **Freitext** (z.B. "Di – Fr: 10.00 – 19.00 Uhr"), Format variiert leicht pro Standort, Feldnamen sind teils irreführend (bei einigen steht in `hoursSaturday` tatsächlich der Sonntag).

**Seit 2026-08-02 statisch:** `scripts/enrich_standorte.py` schreibt den Rohtext unverändert in `oeffnungszeiten_wochentags`/`oeffnungszeiten_samstag`. Geparst wird er erst im Browser (`index.html::parseHoursField()`, Port der ursprünglichen Python-Funktion) in `{tage:[0=Mo..6=So], von, bis}` — der live berechnete Geöffnet/Geschlossen-Status hängt ja von der aktuellen Uhrzeit ab, das muss also im Frontend passieren, nicht beim Datenabruf. Deckt alle 24 Kartenstandorte sauber ab; andere MSB-Standorttypen (Jura, Monacensia, Klinikbibliotheken) haben Sonderformate (Komma-Tageslisten, `<br>`, Zusatztexte für Ausstellungen) und werden übersprungen (`console.warn`) statt die Seite zu brechen.

### Ad-hoc-Schließungen / Abweichungen

Weder OpenData noch der `type=951`-Endpunkt kennen ausserplanmäßige Schließungen (Wasserschaden, personalmangel-bedingte Sa-Reduzierung o.ä.) — dessen `info`-Feld bleibt dafür leer, selbst wenn ein Standort laut eigener Unterseite aktuell geschlossen ist (Fund: Stadtteilbibliothek Berg am Laim, "wegen Wasserschaden aktuell geschlossen", `info` trotzdem `""`). Diese Hinweise stehen nur auf der jeweiligen Standort-Unterseite (`webseite`-Spalte) im Abschnitt "#aktuell".

`scripts/enrich_standorte.py::extract_aktuell_notice()` ruft dafür alle 24 Standort-Unterseiten einzeln ab (HTML-Muster: `<h2>#aktuell</h2>` → Titel+Text → endet immer vor `<h3>Feiertage …</h3>`, getestet/stabil) und schreibt Titel+Text in `aktuell_titel`/`aktuell_text`. **Wichtig:** "#aktuell" ist ein allgemeiner News-Block der Bibliotheken, keine reine Schließungs-Meldung — enthielt beim Testen auch Ehrenamts-Aufrufe und Umbau-Ankündigungen. Nur Treffer mit Schließungs-/Öffnungszeiten-Schlüsselwörtern (`CLOSURE_KEYWORDS`, z.B. "geschlossen", "Personalmangel", "Wasserschaden", "Kalenderwoche") werden übernommen, alles andere wird verworfen. Der berechnete Geöffnet/Geschlossen-Status wird **nicht** automatisch anhand des Freitexts überschrieben (z.B. "nur jedes 2. Wochenende" lässt sich nicht zuverlässig aus Prosa in eine Regel giessen) — stattdessen zeigt das Frontend den Original-Text als Warnhinweis.

**Seit 2026-08-02 statisch, manuell aktualisiert** (bewusste Entscheidung von Heiko): diese Meldungen sind naturgemäß potenziell kurzlebig — anders als Services oder Öffnungszeiten kann sich hier jederzeit was ändern. Die Seite zeigt aber trotzdem nur den Stand vom letzten `enrich_standorte.py`-Lauf, nicht den echten Live-Zustand. **Konsequenz: bei Verdacht auf eine neue/behobene Schließung das Skript manuell erneut laufen lassen** — es gibt (noch) keinen automatischen Refresh-Mechanismus (kein Cron). Eine VOLLSTÄNDIGE, unbefristete Schließung (wie Berg am Laim) wird zusätzlich in der eigenen Spalte `dauerhaft_geschlossen` (Ja/Nein) festgehalten — **rein manuell gepflegt**, kein Text-Heuristik-Versuch ("bis auf Weiteres geschlossen" erkennen o.ä.), robuster, muss aber von Hand wieder auf "Nein" gesetzt werden, sobald der Standort regulär öffnet.

### Services

Jede Standort-Unterseite hat einen Icon-Tag-Bereich (FontAwesome-Icons: WLAN, Drucker, Gaming, Cafeteria, barrierefrei, international, Handy-Ladestation, Parkplätze — fester Vokabular über alle 24 Standorte, im Gegensatz zur variablen "vor Ort"-Liste, siehe unten). Steht in den `service_*`-Spalten von `standorte.csv`, im Frontend baut `index.html::servicesFromRow()` daraus die Anzeige-Liste (Port von `scripts/enrich_standorte.py::extract_services()`, das die Rohdaten liefert). Im Steckbrief werden die Klicktexte vollständig sichtbar angezeigt (nicht nur als Hover-Tooltip auf dem Icon-Badge).

**Bugfix 2026-08-02** (Fund am Beispiel Stadtteilbibliothek Moosach, Parkplätze-Text fehlte): `extract_services()` erkannte das Ende des Service-Blocks bisher über zwei aufeinanderfolgende `</div></div>` - das "fraß" dabei versehentlich das schließende `</div>` des *letzten* Icons in der Liste mit, wodurch dessen Klicktext verloren ging (fiel auf den "ja"-Platzhalter zurück). Betraf **13 von 24 Standorten** (je nachdem, welches Icon zufällig zuletzt in der HTML-Reihenfolge stand: Gaming, Drucker, International oder Parkplätze). Fix: Blockende wird jetzt über den nächsten Content-Frame (`<div id="cNNNN" class="frame`) erkannt statt über Div-Zählen. Die "vor Ort"-Extraktion (`extract_vor_ort()`) nutzt einen anderen Mechanismus (Lookahead statt verbrauchtem Trennzeichen) und war von diesem Bug nicht betroffen (verifiziert).

### "vor Ort"-Ausstattung

Zusätzlich gibt's pro Standort-Unterseite eine variable Liste an Detail-Informationen (Bargeldlose Bibliothek, Arbeitsplätze, Lastenfahrrad-Kooperation, aber auch Einzelstücke wie "Naschgarten" oder "Kreislaufschrank") — anders als bei Services ursprünglich **kein festes Schema**, jeder Standort hatte eine andere Teilmenge.

**Bugfix 2026-08-02** (Fund: Stadtteilbibliothek Am Westkreuz, komplette Liste fehlte): Jede Standort-Seite hat **zwei** `<h2>vor Ort</h2>`-Überschriften — eine im Navigationsmenü, eine für die echte Inhaltssektion. Groß-/Kleinschreibung variiert dabei zusätzlich pro Standort uneinheitlich ("vor Ort" vs. "Vor Ort"), sodass weder reines Textmatching noch "einfach case-insensitiv machen" zuverlässig war (Letzteres traf bei manchen Standorten sogar faelschlich das Menü). `extract_vor_ort()` verankert sich jetzt zusätzlich am strukturellen Merkmal `<section class="accordeon-container">`, das nur auf die echte Inhaltssektion folgt.

**Seit 2026-08-02 trotzdem statisch:** Alle am 2026-08-02 über alle 24 Standorte gefundenen Titel (14 Stück, siehe `VOR_ORT_COLUMNS` in `scripts/enrich_standorte.py` UND `index.html` — beide Listen müssen synchron bleiben) haben jeweils eine eigene `vor_ort_*`-Spalte in `standorte.csv` bekommen. Zwei Titel wurden dabei nicht als eigene Spalte angelegt, sondern mit bereits bestehenden Feldern zusammengeführt (Fund: inhaltliche Überschneidung, siehe `VOR_ORT_MERGE_TITLES`):
- **"Fremdsprachige Medien"** ersetzt/verbessert `service_international` — das reine "ja"/leer-Icon-Flag war unvollständig (z.B. hatte die Zentralbibliothek Motorama kein "globe"-Icon, obwohl die Akkordeon-Liste dort 8 Sprachen nennt).
- **"Barrierefreiheit"** überschreibt NICHT die offizielle OpenData-Spalte `barrierefreiheit` (andere Provenienz!), sondern landet separat in `barrierefreiheit_detail`.

**Ergänzung 2026-08-02** (Fund: Stadtbibliothek im HP8): Das "wheelchair"-Service-Icon selbst ist weiterhin bewusst nicht in `SERVICE_COLUMNS` (keine eigene Badge, siehe oben), sein Klicktext kann aber über die offizielle `barrierefreiheit`-Spalte hinausgehende Details enthalten (HP8 nennt zusätzlich Behindertenparkplätze). Wird deshalb jetzt als **Basis** für `barrierefreiheit_detail` übernommen, auf die der Akkordeon-Text bei Bedarf noch draufsetzt (`merge_text()`) - konsequent nach dem "Icon = Basis, Akkordeon ergänzt"-Prinzip von oben, nicht nur für die Services mit eigener Badge.

**Trade-off:** feste Spalten statt freier Liste heißt, ein komplett NEUER Akkordeon-Titel (den noch kein Standort bisher hatte) würde beim nächsten `enrich_standorte.py`-Lauf nicht automatisch eine neue Spalte bekommen, sondern nur als `WARNUNG: unbekannte 'vor Ort'-Titel gefunden` in der Konsolenausgabe auftauchen — `VOR_ORT_COLUMNS` müsste dann von Hand in **beiden** Dateien ergänzt werden. Bewusst in Kauf genommen fürs komplett CSV-basierte, JSON-freie Datenmodell.

**Zusammenführungsregel (Fund am Beispiel Riem, 2026-08-02):** Icon-Klicktexte und Akkordeon-Einträge behandeln teils dasselbe Thema mit unterschiedlicher Ausführlichkeit (z.B. Icon "international" oft leer, Akkordeon "Fremdsprachige Medien" nennt die Sprachen aus). Prinzip: **der Icon-Text ist immer die Basis, ein passender Akkordeon-Eintrag ergänzt sie** (`enrich_standorte.py::merge_text()`, hängt an statt zu überschreiben). `VOR_ORT_MERGE_TITLES` ist dafür generisch für alle 7 Service-Kategorien angelegt (nicht nur die zwei tatsächlich gefundenen Fälle) — falls ein Standort mal einen Akkordeon-Titel wie "Cafeteria" oder "WLAN" bekommt, wird der automatisch mit `service_cafeteria`/`service_wlan` zusammengeführt statt eine redundante `vor_ort_*`-Spalte anzulegen.

## Katalogdaten & Neuzugänge (OAI-PMH-API, `neuzugaenge.html`)

OAI-PMH-Schnittstelle: `https://data-bib.muenchen.de/oai-pmh` — komplette bibliographische Metadaten (MARC21 oder Dublin Core), CC0-Lizenz, `verb=ListRecords`/`GetRecord`/Sets nach ISIL (6 Teilbestände: Gesamt `DE-M36`, Musik `DE-M36b`, Jura `DE-M36c`, Philatelie `DE-M36d`, Monacensia `DE-M36e`). Pagination per Resumption-Token (Token läuft nach 1h Inaktivität ab). Datumsfilter via `from`/`until`. Offizielle Doku: https://blog.muenchner-stadtbibliothek.de/open-meta-data/, Swagger: https://data-bib.muenchen.de/swagger-ui/index.html.

**Kein Such-API, nur Harvesting:** `GetRecord` holt genau einen Datensatz, aber nur über eine bereits bekannte ID (aDIS-Identifier wie `AK02384786` oder OAI-UUID) — keine Suche nach Titel/ISBN/Autor möglich. Man muss den Titel erst im normalen Online-Katalog finden, um an die ID zu kommen.

**`oai_dc` vs. `marc_xml`:** Bei identischer `from`/`until`/`set`-Abfrage liefert `oai_dc` nachweislich WENIGER Treffer als `marc_xml` für denselben Tag (Test 2026-08-02, 31.07.: `oai_dc` 95 Treffer, `marc_xml` 478 Treffer) — vermutlich zwei nicht ganz synchrone Backend-Pipelines. `marc_xml` ist daher die verlässlichere Quelle, wird überall verwendet.

**`complete=true`:** Ohne diesen Zusatzparameter fehlen die digitalen Onleihe/Overdrive-Angebote (E-Books, eAudio) komplett (Test: 478 vs. 536 Treffer für denselben Tag). Immer mitgeben.

**Katalog-Sets sind riesig:** Gesamtbestand 1.159.366 Datensätze, selbst der kleinste Sonderbestand (Jura) hat 25.344 — "live durchblättern" für eine Sonderbestand-Ansicht ist damit keine Option (>500 Requests). Die Katalog-Metadaten (`setSpec`) unterscheiden zudem nur die 4-5 Spezialbibliotheken vom Gesamtbestand, keine Zuordnung auf Ebene der 22 Stadtteilbibliotheken (kein MARC-Holdings-Feld 852/910-919) — "welches Buch liegt in welcher Filiale" lässt sich mit den offenen Daten nicht beantworten.

### Neuzugänge-Feature (`neuzugaenge.html` + `scripts/fetch_neuzugaenge.py`)

Zeigt neu katalogisierte Medien, ein Tag pro Archiv-Datei (`data/neuzugaenge/<TAG>.csv`, Liste der Tage in `data/neuzugaenge/index.json`). **BETA-Status** (Badge im Header + Hinweis im Intro-Text) — bewusst so gekennzeichnet, kann kaputtgehen oder Datenfehler enthalten.

**Warum ein Tag statt eines rollierenden Fensters:** ein 7-Tage-Fenster über den Gesamtbestand ergibt schon 900+ Treffer, jedes Mal alles neu abzurufen wäre Verschwendung. Ein Tag pro Lauf + Archivierung baut stattdessen mit der Zeit eine vollständige lokale Historie auf, ohne je einen Tag doppelt abzufragen (`fetch_neuzugaenge.py` prüft `data/neuzugaenge/<TAG>.csv` auf Existenz, idempotent).

**Medienart kommt aus MARC-Feld `245$h`** (allgemeine Materialbenennung, z.B. "[Buch + CD]", "[DVD-Video]"), nicht aus dem MARC-Leader-Code — deutlich feinkörniger. Der MSB-OPAC selbst übersetzt diese Rohwerte nochmal in freundlichere Labels (z.B. "Druckschrift" → "Buch", "Buch + CD" → "Medienkombination") — per Stichprobe an 11 Datensätzen manuell im OPAC nachgeschaut und in `GMD_DISPLAY_LABELS` nachgebaut. MARC-Leader-Code bleibt nur Fallback für den seltenen Fall, dass `245$h` mal fehlt.

**OPAC-Link je Eintrag:** aus MARC-Feld 001 (Format `AK<Nummer>`) lässt sich der Link zum Original-Katalogeintrag ableiten: Nummer auf 8 Stellen mit führenden Nullen auffüllen, `S` voranstellen → `https://ssl.muenchen.de/aDISWeb/app/opac?sp=SAK<Nummer>` (empirisch bestätigt). Reine URL-Konstruktion aus schon vorhandenen Daten, kein zusätzlicher Request nötig — wichtig, weil `ssl.muenchen.de/robots.txt` `/aDISWeb/` fürs Crawlen verbietet (gilt aber nicht fürs bloße Verlinken, das der Nutzer selbst anklickt).

**Cover-Bilder über die ISBN, in dieser Reihenfolge (erster Treffer gewinnt):**
1. **ekz.de** (`https://cover.ekz.de/<ISBN>.jpg`) — Einkaufszentrale für Bibliotheken, deutscher Medienlieferant. Mit Abstand beste Trefferquote für den stark deutschsprachigen MSB-Bestand (~91-94% der ISBN-Treffer). Kein `robots.txt` auf `cover.ekz.de`, Haupt-Domain erlaubt Crawling ausdrücklich.
2. **Open Library** — schwache deutsche Abdeckung (Test: nur 18 von 384 ISBN-Treffern, ~4.7%).
3. **Google Books** — braucht eigenen API-Key in `scripts/_secrets.py` (gitignored, siehe `_secrets.example.py`). Backend antwortet öfter mit transientem 503, deshalb mit Retry-Logik (`GOOGLE_BOOKS_RETRIES`).

**Bewusst NICHT genutzt:**
- **isbn.de** — `robots.txt` verbietet ClaudeBot/automatisierte Datenerhebung explizit, inkl. Crawl-Policy-Hinweis mit Rechtsfolgen-Androhung.
- **ssl.muenchen.de/vlb/cover/** (MSB-eigener OPAC-Cover-Link) — `robots.txt` verbietet den Pfad explizit, Direktzugriff liefert zudem HTTP 403.
- **Goodreads** — seit 2020 keine offene API mehr, Scraping der Amazon-Tochter (auch über Wrapper-APIs wie `bookcover-api`, die intern genau das tun) bewusst nicht umgesetzt.
- **DNB** (Deutsche Nationalbibliothek) — liefert über die offizielle SRU-Schnittstelle (`services.dnb.de/sru/dnb`) saubere Metadaten, aber keine Cover-Bilder (weder im `oai_dc`- noch im `MARC21`-Schema, einziger Link-Datensatz ist ein Klappentext-Link).

**Nächtlicher Cron:** `.github/workflows/neuzugaenge.yml`, siehe Abschnitt "Deployment" oben.

## Prototyp (`index.html`)

Lokale Karten-Anwendung: Leaflet-Karte mit den 24 Standorten (22 Stadtteilbibliotheken + 2 Zentralbibliotheken). Rechte Leiste zeigt standardmäßig alle Standorte als Liste mit live berechnetem Geöffnet/Geschlossen-Status (grüner/roter Punkt, aktualisiert sich jede Minute) plus ⚠-Badge bei aktiver Ad-hoc-Meldung, dazu Filter-Chips zum Eingrenzen nach Services (WLAN, Drucker, Gaming, ...). Klick auf einen Eintrag oder Kartenmarker öffnet den Steckbrief mit Adresse/Kontakt/Fakten, aktuellem Status, Rückgabezeiten, Services-Badges, "vor Ort"-Ausstattungsliste, ggf. Warnhinweis-Box zur Ad-hoc-Meldung und Trend-Charts (Chart.js) für Besuche/Bestand/Entleihungen über alle 18 Monate.

**Architektur (seit 2026-08-02, kein Python-Build-Schritt mehr):** `index.html` lädt `data/standorte.csv` + die drei Kennzahlen-CSVs direkt per PapaParse im Browser und joint sie clientseitig (`loadData()`), analog zum Muster bei data.parkraumwende.de/rathausmuc.de. Es gibt keine generierte `bibliotheken.json` mehr — `scripts/build_data.py` ist komplett entfallen (liegt zur Referenz in `scripts/retired/`), `scripts/enrich_standorte.py` ist jetzt das einzige Skript im Projekt und schreibt ausnahmslos alle Detaildaten direkt in `standorte.csv` (siehe Abschnitt "Datensätze" oben). Join-Fallstrick (Namens-Mismatch zwischen Dateien, siehe oben) und Öffnungszeiten-Parsing laufen jetzt in JavaScript statt Python (`index.html::normalizeName()` bzw. `parseHoursField()`, Ports der ursprünglichen Python-Funktionen).

Lokal starten: `python3 -m http.server <port>` im Projektordner, dann `http://localhost:<port>/` öffnen (funktioniert nicht über `file://`, da die Seite die CSV-Dateien per `fetch()` lädt).

**Daten aktualisieren:** `python3 scripts/enrich_standorte.py` neu laufen lassen (holt Kontakt, Öffnungszeiten, Services, Ausstattung und #aktuell-Meldungen neu von der MSB-Website) — kein weiterer Schritt nötig, `index.html` liest beim nächsten Laden automatisch den neuen Stand aus `standorte.csv`.

**Marker-Farbe** (seit 2026-08-02): richtet sich nach dem live berechneten Geöffnet/Geschlossen-Status (grün/rot/beige für unbekannt) statt nach dem Bibliothekstyp. Größe bleibt nach Typ gestaffelt (HP8/Motorama = Zentralbibliotheken, größerer Radius). Beim Öffnen eines Steckbriefs bekommt der zugehörige Marker zusätzlich einen dicken gelben Ring (`setActiveMarker()`/`clearActiveMarker()` in `index.html`), wird beim Zurück-zur-Liste oder Anzeigen eines anderen Standorts zurückgesetzt.

**Mobil-Optimierung** (`@media (max-width: 720px)` in `index.html`): Karte ist auf schmalen Viewports die Hauptansicht (volle Breite/Höhe), die rechte Leiste (Liste/Steckbrief) wird zum Bottom-Sheet — standardmäßig eingeklappt (nur eine 44px-Griffleiste mit Chevron-Icon sichtbar, Karte bleibt im Fokus), klappt bei Markerklick/Listenklick automatisch auf (`renderDetail()` setzt `sheet-open`), klappt bei Klick auf die Karte selbst wieder zu (`map.on('click', ...)` — Leaflet stoppt die Event-Propagation für Marker-Klicks selbst, daher kein Konflikt mit dem Auto-Aufklappen). Chevron-Icon statt reinem Wisch-Strich, weil nur Tap (kein Drag-Gesture) implementiert ist. Header verliert auf Mobil den Untertitel, damit Titel + Nav-Links in eine Zeile passen.

## Impressum & Daten-Seiten

Drei Unterseiten neben `index.html`, alle mit identischer Header-Navigation (Neuzugänge / Daten & FAQ / Impressum, rechts oben, auf allen vier Seiten gleich und die eigene Seite eingeschlossen):

- **`impressum.html`** — schlankes Standard-Impressum (§5 TMG, Kontakt, §18 MStV, Haftung Inhalte/Links, Datenschutz, Urheberrecht). Bewusst getrennt von der Datenquellen-Tabelle gehalten (war kurzzeitig in einer Datei zusammengefasst, auf Wunsch wieder aufgeteilt).
- **`daten.html`** (hieß zunächst `quellen.html`, dann kurz `impressum.html` mit-drin, jetzt eigenständig) — "Daten und FAQ": Datenquellen-Tabelle mit Live-Check-Status (aus `data/source-registry.json` + `data/source-status.json`, siehe `cron/check_sources.php`), Lizenzhinweise, Technologie-Attribution, Link zu `data/datapackage.json`. Der Name impliziert eine noch fehlende FAQ-Sektion (bisher nicht gebaut, nur der Titel ist schon so gewählt).

**Bugfix 2026-08-02:** `check_sources.php`s `check_ckan()`-Methode liefert nur `source_modified` (Datum laut OpenData-Portal), nie `local_updated` (das gibt's nur bei den 4 manuell gepflegten MSB-Quellen). Die Tabelle in `daten.html` hat ursprünglich nur nach `local_updated` geschaut, daher blieb die Spalte "Zuletzt aktualisiert" bei allen 4 CKAN-Quellen leer — jetzt fällt sie korrekt auf `source_modified` zurück.
