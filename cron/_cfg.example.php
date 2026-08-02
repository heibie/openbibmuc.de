<?php
// Kopiere diese Datei nach _cfg.php und trage die echten Werte ein.
// _cfg.php ist gitignored und muss manuell auf den Server hochgeladen werden.

$cfg = [
    'cron' => [
        // Sicherheits-Token für die Cron-URL (frei wählbar, geheim halten)
        'token' => 'DEIN_CRON_TOKEN_HIER',
    ],
    'github' => [
        // Fine-grained Personal Access Token mit Contents: Read+Write UND Issues: Read+Write
        // auf dieses Repo (Issues-Berechtigung wird von cron/check_sources.php gebraucht)
        'token' => 'github_pat_DEIN_TOKEN_HIER',
        // GitHub-Repo im Format owner/repo
        'repo'  => 'heibie/openbibmuc.de',
    ],
];
