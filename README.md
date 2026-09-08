# IC35 Sync

Windows-Desktop-Client für den Siemens IC35: Kontakte mit Thunderbird/CardDAV,
Kalender und Aufgaben mit Google, Memos mit einem lokalen Notizordner synchronisieren.

**3.3.0a1 ist eine Vorabversion zur GitHub-Vorbereitung.** Grundlage ist der
persönlich erprobte Client v3.2.5. Die bereinigte Oberfläche und der neue Kalender-
Erststart brauchen noch einen Test an echter Hardware. Dies ist keine offizielle
Siemens-, Google- oder Thunderbird-Anwendung.

## Funktionen

- Ein Button für den Gesamtabgleich, ein SyncStation-Tastendruck.
- Bidirektionaler Abgleich mit Konflikthinweisen und getrennten Zuordnungen.
- Auswahl eines beschreibbaren Google-Kalenders und einer Google-Aufgabenliste.
- Memo-Dateien als UTF-8 TXT oder Markdown; optional im lokal synchronisierten Cloud-Ordner.
- Vollbackup ausschließlich über **Nur Backup**; Start- und Abschlusssounds.

## Installieren und starten

Voraussetzungen: Windows, Python **3.12** inklusive Tcl/Tk und Python Launcher,
IC35-SyncStation sowie ein funktionierender serieller Anschluss/USB-Seriell-Treiber.

1. Source-ZIP vollständig entpacken.
2. `install.bat` starten. Abhängigkeiten werden in eine lokale `.venv` installiert.
3. `start.bat` starten und den COM-Port auswählen.
4. Google-Ziele und gegebenenfalls den Notizordner einrichten.
5. **ALLES SYNCHRONISIEREN** anklicken und bei Aufforderung die SyncStation drücken.

Für Google ist in dieser Entwicklerfassung noch eine eigene Desktop-OAuth-Datei
nötig: [Google-Einrichtung](docs/GOOGLE_SETUP.md). Ein zentral verifizierter
Anmeldeclient wird nicht vorgetäuscht und keine persönliche Anmeldung mitgeliefert.

Die Vorabversion verwendet `%APPDATA%\IC35SyncPreview`, getrennt vom alten
`IC35ThunderbirdSync`-Ordner. Es gibt **keine automatische Migration**. Nicht
beide Programme gleichzeitig starten: Radicale nutzt auf beiden Seiten Port 5232.
Beim neuen Erstabgleich werden Bestände vereinigt; vorhandene Löschhistorie der
alten Installation wird nicht automatisch übernommen. Vor einem Test mit echten
Daten ein manuelles Vollbackup erstellen und die alten State-Dateien sichern.

## Thunderbird-Kontakte

Der lokale Radicale-Dienst lauscht ausschließlich auf `127.0.0.1:5232`.
Für ein Thunderbird-CardDAV-Adressbuch die Adresse
`http://127.0.0.1:5232/ic35/addressbook/` verwenden; Benutzer `ic35`, kein Passwort.
Im ersten Gesamtlauf wird der Dienst gestartet. Es gibt keine LAN-Freigabe.
Derzeit läuft der Kontakte-Abgleich zusammen mit einem eingerichteten Kalender.
Nur Google Tasks kann über denselben Button ohne Kalender laufen.

## Verhalten und Grenzen

- Ein Erstabgleich verbindet gleiche Inhalte und übernimmt fehlende Gegenstücke.
  Spätere Löschungen werden anhand der letzten gemeinsamen Baseline übertragen.
- Memos/Aufgaben: maximal 60 Byte Titel und 255 Byte Text in Windows-1252.
  Zu große oder nicht darstellbare Inhalte werden nicht still gekürzt.
- Google Tasks: Titel, Text, Datum und Status. Keine Uhrzeit-/Serienlogik;
  Unteraufgaben, Elternaufgaben und zugewiesene Aufgaben werden ausgelassen.
- Google-Kalenderserien werden in einem begrenzten Zeitfenster als Einzeltermine
  abgebildet. Die Kalenderzeitzone im Client ist derzeit **Europe/Berlin**.
  IC35-Alarme/-Wiederholungen sind in der Gegenrichtung eingeschränkt.
- Der Sync ist keine Transaktion über alle Systeme. Fehler können einen teilweise
  abgeschlossenen Lauf hinterlassen. Journale nicht blind löschen.
- Gerätesperren/Passwörter werden derzeit nicht unterstützt.
- **Wiederherstellen von Vollbackups ist noch nicht implementiert.**

## Entwickeln und prüfen

```text
python -m unittest discover -s tests -p "test_*.py" -v
python scripts/check_release.py
python scripts/build_release.py
```

Die Offline-Tests verwenden simulierte Gerätedaten. Für GUI-/Google-Tests werden
die normalen Abhängigkeiten benötigt. `IC35_SYNC_DATA_DIR` kann für isolierte Tests
einen eigenen Datenordner vorgeben. Niemals fremde Benutzer- oder Google-Tokens verwenden.

[Beitragen](CONTRIBUTING.md) · [Sicherheit](SECURITY.md) ·
[Datenschutz](docs/PRIVACY.md) · [Veröffentlichungsstand](docs/RELEASE_READINESS.md)

## Lizenz

GNU GPL Version 2, siehe [LICENSE](LICENSE) und
[Herkunftshinweise](THIRD_PARTY_NOTICES.md). Bereitstellung ohne Gewährleistung.
