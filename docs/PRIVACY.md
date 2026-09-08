# Datenverarbeitung in der Vorabversion

Die App liest Kontakte, Termine, Memos und Aufgaben des verbundenen IC35.
Je nach eingerichteten Zielen werden diese mit lokalem CardDAV, Notizdateien
oder Google Calendar/Tasks abgeglichen. Löschungen können in beide Richtungen wirken.

Die App besitzt keinen eigenen Betreiber-Server, keine Telemetrie und keinen
automatischen Log-Upload. Google-Anmeldung und Google-API-Zugriffe erfolgen
direkt vom PC. Die Installation lädt Abhängigkeiten von Python-Paketquellen.
Ein separat verwendeter Cloud-Ordner wird durch dessen Desktop-Client übertragen.

Im lokalen Datenordner stehen Tokens, Zuordnungen, Exporte, Protokolle und
optionale Backups. Diese können vollständige persönliche Inhalte enthalten.
Tokens und Backups sind in dieser Vorabversion **nicht zusätzlich verschlüsselt**;
der Schutz hängt auch von den Windows-Dateirechten und dem Benutzerkonto ab.
Der lokale CardDAV-Dienst ist ohne Passwort auf Loopback erreichbar.

Bei GitHub-Issues niemals ungeprüfte Logs, Tokens, Gerätebackups oder Exporte
anhängen. Ein anonymisierter Fehlertext reicht für den ersten Bericht.
Zugriffsrechte können in den Google-Kontoeinstellungen widerrufen werden.
Lokale Daten werden nicht automatisch gelöscht, wenn die App deinstalliert wird.

Dieses technische Dokument beschreibt die aktuelle Software. Für einen
öffentlichen Google-OAuth-Client muss der Herausgeber zusätzlich die reale
Projektidentität und einen erreichbaren Datenschutzkontakt veröffentlichen.
