# Veröffentlichungsstand: 3.3.0a1

## Vorbereitet

- Eigenständiger Quellbaum ohne Nutzerdateien, Tokens oder Protokolle.
- GPL-2.0 mit Lizenztext und Hinweisen zur IC35Link-Herkunft.
- Bereinigte GUI ohne alte Testaktionen; ein Sync-Button und manuelles Vollbackup.
- Freie Kalenderauswahl; keine persönliche Kalenderbezeichnung und keine v2-Baseline-Sperre.
- Eigener Preview-Datenordner; kalenderzielbezogene State-Dateien.
- Aktuelle README, Beitrags-/Sicherheitshinweise, Offline-Tests und Release-Prüfung.
- GitHub-Actions-Konfiguration für Tests und Prüfung; kein automatisches Publishing.

## Vor einer stabilen Endnutzer-Version offen

1. Frische Installation, Kalender-Erstabgleich und GUI an echter Hardware testen.
2. Konto wechseln/trennen und Token-Speicherung vervollständigen.
3. Zentrale Google-OAuth-Konfiguration samt Verifizierung und Betreiberangaben.
4. Abhängigkeiten für ein reproduzierbares Binärrelease festschreiben und
   gegebenenfalls EXE-Build, Codesignatur sowie Drittanbieterhinweise ergänzen.
5. Wiederherstellung aus Gerätebackups implementieren und testen.
6. Sichere Migration der bisherigen privaten Installation planen.

Die Tests belegen die simulierten Abläufe, keine vollständige Hardwarefreigabe
dieser bereinigten Fassung. Keine automatische Freigabe oder Veröffentlichung
bei GitHub findet statt. Benutzer-/Repository-Name und öffentliches Ziel sind
noch nicht festgelegt. Dieses Paket ist ein Quellcode-Prerelease zur Durchsicht.
