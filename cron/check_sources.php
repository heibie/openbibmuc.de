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
 *      nach main committet (rein informative Ampel auf quellen.html, ändert keine
 *      Kartendaten, daher unkritisch für Auto-Commit).
 *   2. Erkennt der Check bei einer "ckan"-Quelle eine echte Änderung gegenüber dem letzten
 *      bekannten Stand, wird zusätzlich ein GitHub Issue angelegt ("Quelle X hat sich
 *      geändert: alt -> neu"). Es wird NICHT versucht, die betroffene CSV automatisch neu
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
                $result = check_ckan($src['check']['dataset'], $prev);
                if ($result['status'] === 'changed') {
                    $changed[] = ['id' => $id, 'title' => $src['title'], 'result' => $result, 'prev' => $prev];
                }
            } elseif ($method === 'heuristic') {
                $result = check_heuristic($src, $prev);
            } else {
                $result = ['status' => 'unknown', 'detail' => "Unbekannte Prüfmethode: {$method}"];
            }

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
    $old = $c['prev']['source_modified'] ?? '(unbekannt, erster Check)';
    $new = $r['source_modified'] ?? '(unbekannt)';
    return "Automatischer Wochen-Check hat eine Änderung an der Quelle **{$c['title']}** erkannt.\n\n"
         . "- Zuletzt bekannt: `{$old}`\n"
         . "- Jetzt laut Portal: `{$new}`\n\n"
         . "Bitte in der [Quellen-Übersicht](https://openbibmuc.de/quellen.html) gegenprüfen "
         . "und die betroffene CSV bei Bedarf manuell aktualisieren. Dieses Issue danach schließen.\n\n"
         . "_Automatisch erstellt von `cron/check_sources.php`._";
}

function check_ckan(string $dataset, array $prev): array
{
    $url = 'https://opendata.muenchen.de/api/3/action/package_show?id=' . urlencode($dataset);
    $ctx = stream_context_create([
        'http' => ['timeout' => 20, 'header' => "User-Agent: openbibmuc-source-check\r\n"],
    ]);
    $raw = @file_get_contents($url, false, $ctx);
    if ($raw === false) {
        return ['status' => 'unknown', 'detail' => 'CKAN-Abruf fehlgeschlagen (keine Antwort).'];
    }
    $data = json_decode($raw, true);
    if (!($data['success'] ?? false)) {
        $msg = $data['error']['message'] ?? 'unbekannter Fehler';
        return ['status' => 'unknown', 'detail' => "CKAN-Abruf fehlgeschlagen: {$msg}"];
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
    $isChanged    = $prevModified !== null && $prevModified !== $sourceModified;

    return [
        'status'          => $isChanged ? 'changed' : 'ok',
        'source_modified' => $sourceModified,
        'detail'          => $isChanged
            ? "Quelle wurde seit letzter Prüfung aktualisiert (laut {$field})."
            : "Laut Portal zuletzt geändert am {$sourceModified} ({$field}), keine Änderung seit letzter Prüfung.",
    ];
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
