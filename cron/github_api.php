<?php
// Gemeinsame GitHub-Git-Data-API-Helfer für die Cronjobs dieses Repos.

function github_request(string $method, string $url, array $headers, ?string $body = null): array
{
    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_CUSTOMREQUEST  => $method,
        CURLOPT_HTTPHEADER     => $headers,
        CURLOPT_TIMEOUT        => 30,
    ]);
    if ($body !== null) {
        curl_setopt($ch, CURLOPT_POSTFIELDS, $body);
    }
    $response = curl_exec($ch);
    $code     = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);
    return ['code' => $code, 'body' => $response];
}

function github_headers(string $token): array
{
    return [
        'Authorization: token ' . $token,
        'Accept: application/vnd.github.v3+json',
        'Content-Type: application/json',
        'User-Agent: openbibmuc-cron',
    ];
}

// Holt den aktuellen Inhalt einer Datei aus dem Repo (roh, ohne Anhängen). null bei 404.
function github_get_file(string $path, string $token, string $repo): ?string
{
    $url = "https://api.github.com/repos/{$repo}/contents/{$path}";
    $res = github_request('GET', $url, github_headers($token));

    if ($res['code'] === 404) {
        return null;
    }
    if ($res['code'] === 200) {
        $data = json_decode($res['body'], true);
        return base64_decode($data['content']);
    }
    throw new RuntimeException("GitHub GET {$path} fehlgeschlagen (HTTP {$res['code']})");
}

// Schreibt mehrere Dateien in EINEM Commit (Git-Data-API: Blob → Tree → Commit → Ref-Update),
// damit nur EIN Push-Event und damit EIN Deploy-Workflow-Lauf ausgelöst wird.
// $files: ['pfad/datei.json' => 'kompletter neuer inhalt', ...]
//
// Die Ref-Update-PATCH ist ein impliziter Compare-and-Swap: der neue Commit hat den beim
// GET gelesenen Ref-Stand als Parent, daher schlägt das Update fehl (kein Fast-Forward mehr),
// wenn zwischenzeitlich ein anderer Commit auf den Branch gepusht wurde.
function github_commit_multiple(array $files, string $message, string $token, string $repo, string $branch = 'main'): void
{
    $headers = github_headers($token);

    // 1. Aktuellen Branch-HEAD holen
    $refRes = github_request('GET', "https://api.github.com/repos/{$repo}/git/refs/heads/{$branch}", $headers);
    if ($refRes['code'] !== 200) {
        throw new RuntimeException("GitHub GET ref fehlgeschlagen (HTTP {$refRes['code']}): {$refRes['body']}");
    }
    $latestCommitSha = json_decode($refRes['body'], true)['object']['sha'];

    // 2. Basis-Tree des aktuellen Commits holen
    $commitRes = github_request('GET', "https://api.github.com/repos/{$repo}/git/commits/{$latestCommitSha}", $headers);
    if ($commitRes['code'] !== 200) {
        throw new RuntimeException("GitHub GET commit fehlgeschlagen (HTTP {$commitRes['code']}): {$commitRes['body']}");
    }
    $baseTreeSha = json_decode($commitRes['body'], true)['tree']['sha'];

    // 3. Für jede Datei einen Blob anlegen
    $treeEntries = [];
    foreach ($files as $path => $content) {
        $blobRes = github_request('POST', "https://api.github.com/repos/{$repo}/git/blobs", $headers, json_encode([
            'content'  => base64_encode($content),
            'encoding' => 'base64',
        ]));
        if ($blobRes['code'] !== 201) {
            throw new RuntimeException("GitHub POST blob {$path} fehlgeschlagen (HTTP {$blobRes['code']}): {$blobRes['body']}");
        }
        $treeEntries[] = [
            'path' => $path,
            'mode' => '100644',
            'type' => 'blob',
            'sha'  => json_decode($blobRes['body'], true)['sha'],
        ];
    }

    // 4. Neuen Tree auf Basis des alten anlegen (nur die geänderten Pfade überschreiben)
    $treeRes = github_request('POST', "https://api.github.com/repos/{$repo}/git/trees", $headers, json_encode([
        'base_tree' => $baseTreeSha,
        'tree'      => $treeEntries,
    ]));
    if ($treeRes['code'] !== 201) {
        throw new RuntimeException("GitHub POST tree fehlgeschlagen (HTTP {$treeRes['code']}): {$treeRes['body']}");
    }
    $newTreeSha = json_decode($treeRes['body'], true)['sha'];

    // 5. Neuen Commit anlegen
    $newCommitRes = github_request('POST', "https://api.github.com/repos/{$repo}/git/commits", $headers, json_encode([
        'message' => $message,
        'tree'    => $newTreeSha,
        'parents' => [$latestCommitSha],
    ]));
    if ($newCommitRes['code'] !== 201) {
        throw new RuntimeException("GitHub POST commit fehlgeschlagen (HTTP {$newCommitRes['code']}): {$newCommitRes['body']}");
    }
    $newCommitSha = json_decode($newCommitRes['body'], true)['sha'];

    // 6. Branch-Ref auf den neuen Commit zeigen lassen → löst genau EINEN Push aus
    $updateRes = github_request('PATCH', "https://api.github.com/repos/{$repo}/git/refs/heads/{$branch}", $headers, json_encode([
        'sha' => $newCommitSha,
    ]));
    if ($updateRes['code'] !== 200) {
        throw new RuntimeException("GitHub PATCH ref fehlgeschlagen (HTTP {$updateRes['code']}): {$updateRes['body']}");
    }
}

// Legt ein Issue an (z.B. als Hinweis "Quelle X hat sich geändert, bitte prüfen").
// Braucht auf dem Fine-grained-PAT die Berechtigung "Issues: Read and write".
function github_create_issue(string $title, string $body, string $token, string $repo): array
{
    $res = github_request('POST', "https://api.github.com/repos/{$repo}/issues", github_headers($token), json_encode([
        'title' => $title,
        'body'  => $body,
    ]));
    if ($res['code'] !== 201) {
        throw new RuntimeException("GitHub POST issue fehlgeschlagen (HTTP {$res['code']}): {$res['body']}");
    }
    return json_decode($res['body'], true);
}
