<?php
/**
 * Quellen-Status-Check
 *
 * Cronjob-URL: https://openbibmuc.de/cron/check_sources.php?token=DEIN_CRON_TOKEN
 * Intervall: wöchentlich (KAS-Cronjob)
 *
 * Analog zu data.parkraumwende.de/cron/check_sources.php und rathausmuc.de/cron/check_sources.php:
 *
 *   1. data/source-status.json wird bei JEDEM Lauf neu berechnet und automatisch direkt
 *      nach main committet (rein informative Ampel auf daten.html, ändert keine
 *      Kartendaten, daher unkritisch für Auto-Commit).
 *   2. Ist eine "ckan"-Quelle auf dem Portal neuer als der übernommene Stand (Registry-Feld
 *      check.imported_source_modified), bleibt der Status "changed", bis die CSV aktualisiert
 *      und das Feld hochgesetzt wurde. Bei jeder neuen Portalversion wird einmalig ein
 *      GitHub Issue angelegt ("Quelle X hat sich geändert: alt -> neu"). Es wird NICHT versucht, die betroffene CSV automatisch neu
 *      zu erzeugen - die Übernahme von OpenData-Rohdaten in unsere Struktur ist pro Quelle
 *      unterschiedlich, falsch übernommene Werte sollen nicht ungeprüft in die Karte wandern.
 *      Die CSV-Aktualisierung bleibt ein manueller Schritt.
 *
 * "heuristic"-Quellen (die MSB-Website-Anreicherungen: Öffnungszeiten, Services, vor-Ort-
 * Ausstattung, #aktuell-Meldungen) haben keine Live-API und keinen festen Turnus, sie werden
 * daher ohne Netzwerk-Call immer als "manuell gepflegt" markiert (siehe check_heuristic()).
 */

require __DIR__ . '/_cfg.php';
require __DIR__ . '/github_api.php';

// Token-Check
$token = $_GET['token'] ?? '';
if (!$token || $token !== $cfg['cron']['token']) {
    http_response_code(403);
    echo json_encode(['error' => 'forbidden']);
    exit;
}

$githubToken = $cfg['github']['token'];
$githubRepo  = $cfg['github']['repo'];
$now         = date('c');
$debug       = isset($_GET['debug']);

try {
    $registryRaw = file_get_contents(__DIR__ . '/../data/source-registry.json');
    if ($registryRaw === false) {
        throw new RuntimeException('data/source-registry.json nicht lesbar');
    }
    $registry = json_decode($registryRaw, true);
    if (!$registry) {
        throw new RuntimeException('data/source-registry.json ist kein gültiges JSON');
    }

    $prevStatusRaw = @file_get_contents(__DIR__ . '/../data/source-status.json');
    $prevStatus    = $prevStatusRaw ? (json_decode($prevStatusRaw, true)['sources'] ?? []) : [];

    $results  = [];
    $changed  = [];

    foreach ($registry['categories'] as $cat) {
        foreach ($cat['sources'] as $src) {
            $id     = $src['id'];
            $method = $src['check']['method'];
            $prev   = $prevStatus[$id] ?? [];

            if ($method === 'ckan') {
                $result = check_ckan($src['check'], $prev);
                // Issue nur einmal pro neuer Portalversion, nicht bei jedem Lauf, solange
                // die Daten noch nicht übernommen sind (Status bleibt dann "changed").
                if ($result['status'] === 'changed' && ($result['new_since_last_check'] ?? false)) {
                    $changed[] = ['id' => $id, 'title' => $src['title'], 'result' => $result, 'prev' => $prev];
                }
            } elseif ($method === 'heuristic') {
                $result = check_heuristic($src, $prev);
            } else {
                $result = ['status' => 'unknown', 'detail' => "Unbekannte Prüfmethode: {$method}"];
            }

            unset($result['new_since_last_check']);
            $result['method']        = $method;
            $result['checked_at']    = $now;
            $result['local_updated'] = $result['local_updated'] ?? ($prev['local_updated'] ?? null);
            $results[$id]            = $result;
        }
    }

    $newStatusJson = json_encode(
        ['generated_at' => $now, 'sources' => $results],
        JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES
    ) . "\n";

    if ($debug) {
        header('Content-Type: application/json');
        echo $newStatusJson;
        exit;
    }

    $statusUnchanged = $prevStatusRaw !== false && normalize_json($prevStatusRaw) === normalize_json($newStatusJson);

    if (!$statusUnchanged) {
        // Direkt auf Festplatte schreiben - sofort sichtbar ohne Deploy-Umweg
        file_put_contents(__DIR__ . '/../data/source-status.json', $newStatusJson);

        github_commit_multiple(
            ['data/source-status.json' => $newStatusJson],
            "data: Quellen-Status aktualisiert ({$now})",
            $githubToken,
            $githubRepo
        );
    }

    $issuesCreated = [];
    foreach ($changed as $c) {
        $issue = github_create_issue(
            "Quelle geändert: {$c['title']}",
            issue_body($c),
            $githubToken,
            $githubRepo
        );
        $issuesCreated[] = $issue['number'] ?? null;
    }

    http_response_code(200);
    echo json_encode([
        'ok'             => true,
        'timestamp'      => $now,
        'checked'        => count($results),
        'status_changed' => !$statusUnchanged,
        'changed'        => count($changed),
        'issues_created' => $issuesCreated,
    ]);

} catch (Exception $e) {
    http_response_code(500);
    echo json_encode(['error' => $e->getMessage()]);
}

function normalize_json(string $json): ?string
{
    $data = json_decode($json, true);
    if ($data === null) {
        return null;
    }
    unset($data['generated_at']);
    $sources = $data['sources'] ?? [];
    foreach ($sources as &$s) {
        unset($s['checked_at']);
    }
    unset($s);
    $data['sources'] = $sources;
    return json_encode($data);
}

function issue_body(array $c): string
{
    $r   = $c['result'];
    $old = $c['result']['local_updated'] ?? $c['prev']['source_modified'] ?? '(unbekannt, erster Check)';
    $new = $r['source_modified'] ?? '(unbekannt)';
    return "Automatischer Wochen-Check hat eine neue Version der Quelle **{$c['title']}** erkannt.\n\n"
         . "- Übernommen bzw. zuletzt bekannt: `{$old}`\n"
         . "- Jetzt laut Portal: `{$new}`\n\n"
         . "Bitte in der [Quellen-Übersicht](https://openbibmuc.de/daten.html) gegenprüfen "
         . "und die betroffene CSV bei Bedarf manuell aktualisieren. Danach in `data/source-registry.json` "
         . "`imported_source_modified` auf den neuen Stand setzen (sonst bleibt der Status rot) und dieses Issue schließen.\n\n"
         . "_Automatisch erstellt von `cron/check_sources.php`._";
}

function check_ckan(array $check, array $prev): array
{
    $dataset = $check['dataset'];
    $url = 'https://opendata.muenchen.de/api/3/action/package_show?id=' . urlencode($dataset);
    $ctx = stream_context_create([
        'http' => ['timeout' => 20, 'header' => "User-Agent: openbibmuc-source-check\r\n"],
    ]);
    // Mehrere Versuche: das Portal hat um Mitternacht (Cron-Zeitpunkt) gelegentlich
    // einzelne Aussetzer (2026-11-08 + 11-15 je eine von vier Abfragen ohne Antwort).
    $attempts = 3;
    $data     = null;
    for ($i = 1; $i <= $attempts; $i++) {
        $raw  = @file_get_contents($url, false, $ctx);
        $data = $raw === false ? null : json_decode($raw, true);
        if ($data['success'] ?? false) {
            break;
        }
        if ($i < $attempts) {
            sleep(5 * $i);
        }
    }
    if (!($data['success'] ?? false)) {
        $msg = $data === null ? 'keine Antwort' : ($data['error']['message'] ?? 'unbekannter Fehler');
        return ckan_failure($prev, "CKAN-Abruf nach {$attempts} Versuchen fehlgeschlagen ({$msg}).");
    }
    $result = $data['result'];
    $extras = [];
    foreach ($result['extras'] ?? [] as $e) {
        $extras[$e['key']] = $e['value'];
    }

    // "GeoPortal Last Modified Date" (nur bei Geoportal-Datensätzen) ist das echte
    // Änderungsdatum. Bei diesen (nicht-geo) Bibliotheks-Datensätzen gibt es das nicht,
    // daher der resources-last_modified-Fallback als Regelfall.
    if (isset($extras['GeoPortal Last Modified Date'])) {
        $sourceModified = $extras['GeoPortal Last Modified Date'];
        $field          = 'GeoPortal Last Modified Date';
    } else {
        $stamps = [];
        foreach ($result['resources'] ?? [] as $res) {
            $stamps[] = $res['last_modified'] ?? $res['created'] ?? null;
        }
        $stamps         = array_values(array_filter($stamps));
        $sourceModified = $stamps ? max($stamps) : ($result['metadata_modified'] ?? null);
        $field          = 'resource last_modified/created';
    }

    $prevModified = $prev['source_modified'] ?? null;
    $newSinceLast = $prevModified !== null && $prevModified !== $sourceModified;

    // Verglichen wird mit dem Portal-Stand, der in unserer CSV steckt (Registry-Feld
    // imported_source_modified), nicht nur mit dem letzten Lauf - sonst kippt "changed"
    // nach einer Woche auf "ok", obwohl die neuen Daten nie übernommen wurden.
    $imported = $check['imported_source_modified'] ?? null;
    if ($imported === null) {
        $isChanged = $newSinceLast;
        $detail    = $isChanged
            ? "Quelle wurde seit letzter Prüfung aktualisiert (laut {$field})."
            : "Laut Portal zuletzt geändert am {$sourceModified} ({$field}), keine Änderung seit letzter Prüfung.";
    } else {
        $isChanged = $sourceModified !== null && $sourceModified > $imported;
        $detail    = $isChanged
            ? "Neuere Version auf dem Portal (Stand {$sourceModified}), übernommen ist Stand {$imported}."
            : "Übernommener Stand {$imported} entspricht dem Portal ({$field}).";
    }

    $result = [
        'status'               => $isChanged ? 'changed' : 'ok',
        'source_modified'      => $sourceModified,
        'detail'               => $detail,
        'new_since_last_check' => $newSinceLast || ($prevModified === null && $isChanged),
    ];
    if ($imported !== null) {
        $result['local_updated']         = $imported;
        $result['local_updated_precise'] = true;
    }
    return $result;
}

// Bei einem Fehlschlag den zuletzt bekannten Stand behalten - sonst fehlt beim nächsten
// Lauf der Vergleichswert und eine echte Änderung würde nur als "ok" durchrutschen.
function ckan_failure(array $prev, string $detail): array
{
    $result = ['status' => 'unknown', 'detail' => $detail];
    if (isset($prev['local_updated_precise'])) {
        $result['local_updated_precise'] = $prev['local_updated_precise'];
    }
    if (isset($prev['source_modified'])) {
        $result['source_modified'] = $prev['source_modified'];
        $result['detail']         .= " Zuletzt bekannter Stand: {$prev['source_modified']}.";
    }
    return $result;
}

function check_heuristic(array $src, array $prev): array
{
    $localUpdated = $prev['local_updated'] ?? null;
    $turnus       = $src['turnus'] ?? '';

    $intervalDays = null;
    if (str_contains($turnus, '~5 Jahre')) {
        $intervalDays = 365 * 5;
    } elseif (str_contains($turnus, 'jährlich')) {
        $intervalDays = 365;
    }

    if (!$localUpdated || $intervalDays === null) {
        return [
            'status'        => 'manual',
            'local_updated' => $localUpdated,
            'detail'        => 'Kein automatischer Änderungscheck möglich (keine Live-API, kein fester Turnus). Turnus: ' . ($turnus ?: 'unbekannt'),
        ];
    }

    $ageDays = (int) floor((strtotime('today') - strtotime($localUpdated)) / 86400);
    $due     = $ageDays > $intervalDays;

    return [
        'status'        => $due ? 'check_due' : 'ok',
        'local_updated' => $localUpdated,
        'detail'        => $due
            ? "Seit {$ageDays} Tagen nicht aktualisiert, Turnus ist '{$turnus}' – Prüfung beim Original empfohlen."
            : "Zuletzt aktualisiert vor {$ageDays} Tagen, Turnus '{$turnus}' noch nicht erreicht.",
    ];
}
