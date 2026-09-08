# Google einrichten

## Entwicklerfassung: eigenes Cloud-Projekt

1. Ein Google-Cloud-Projekt anlegen oder ein eigenes vorhandenes Projekt verwenden.
2. Google Calendar API und Google Tasks API aktivieren, soweit benötigt.
3. OAuth-Zielgruppe Extern konfigurieren und im Testmodus die eigenen Testkonten eintragen.
4. Einen OAuth-Client vom Typ **Desktop-App** anlegen und seine JSON-Konfiguration herunterladen.
5. Im Programm **Google verbinden** wählen und diese Datei auswählen. Der Browser
   öffnet die Google-Anmeldung. Einen beschreibbaren Kalender auswählen und speichern.
6. Unter **Aufgaben-Ziel** Google Tasks verbinden, Liste wählen und Teilnahme aktivieren.

Benötigte Berechtigungen:

- `https://www.googleapis.com/auth/calendar.events`
- `https://www.googleapis.com/auth/calendar.calendarlist.readonly`
- `https://www.googleapis.com/auth/tasks`

Kalender und Tasks verwenden getrennte Tokens. Derzeit sind deshalb zwei
Freigabeabläufe möglich. Die App speichert die Tokens im lokalen Datenordner.
OAuth-Dateien und Tokens gehören nicht ins Repository. Im Google-Testmodus
können Berechtigungen zeitlich begrenzt sein und erneute Anmeldung verlangen.

## Späterer öffentlicher Anmeldeclient

Für einen einfachen Download mit zentralem Google-Client fehlen noch:

- Name, Supportkontakt, öffentliche Projekt-Homepage und Datenschutzerklärung;
- Google-Branding und erforderliche Scope-Verifizierung;
- eine ausdrücklich für die Veröffentlichung bestimmte Desktop-Client-Konfiguration;
- zusammenhängendes Konto-/Berechtigungsmanagement einschließlich sicherem
  Kontowechsel und sicherer Tokenablage über Windows Credential Manager/DPAPI.

Ein Wechsel des Google-Kontos wird in dieser Vorabversion nicht als eigener
GUI-Ablauf angeboten. Für Tests verschiedener Konten getrennte Datenordner
über `IC35_SYNC_DATA_DIR` verwenden. Ein Kalenderziel erhält einen eigenen State;
Tasks-Zuordnungen sind nach Listen-ID getrennt. Keine Token-Datei zwischen
Konten oder Rechnern kopieren, um die Anmeldung zu umgehen.

Das Veröffentlichen des Quellcodes bei GitHub erteilt noch keine Google-Verifizierung.

Offizielle Referenzen:

- https://developers.google.com/identity/protocols/oauth2/native-app
- https://developers.google.com/identity/protocols/oauth2/production-readiness/sensitive-scope-verification
- https://developers.google.com/workspace/tasks/quickstart/python
