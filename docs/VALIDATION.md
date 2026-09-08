# Lokale Prüfung der Vorabversion

Stand: 8. September 2026.

- 32 Offline-Tests bestanden, einschließlich der bisherigen Aufgaben-/Memo-Tests.
- Neu geprüft: Kalenderwechsel und Umbenennung ohne Verlust der jeweiligen
  Zuordnungen, paginierte Auswahl beschreibbarer Kalender, Erstplanung ohne
  bestehende Baseline und Verweise auf entfernte GUI-Elemente.
- Python-Syntaxprüfung für alle ausgelieferten Python-Dateien bestanden.
- Release-Dateiliste und Prüfung auf bekannte Zugangsdatenmuster bestanden.
- Archiv wird aus einer ausdrücklichen Dateiliste erstellt und byteweise mit
  den Quelldateien verglichen. Laufzeitdaten werden nicht eingesammelt.

Diese Prüfungen sind keine vollständige Sicherheitsprüfung. GUI, Google-OAuth
und serielle Kommunikation der bereinigten Vorabversion sind noch nicht live
getestet. Die früheren Hardwaretests bezogen sich auf den persönlichen Client.
