# Herkunft und Lizenzhinweise

Dieses Projekt wird unter GNU GPL Version 2 veröffentlicht; siehe [LICENSE](LICENSE).
Die Python-Fassung ist eine Weiterentwicklung des persönlichen IC35-Sync-Clients.

## IC35Link 1.18

Die Protokollimplementierung wurde anhand von IC35Link 1.18 und dessen
Protokolldokumentation aufgebaut. Der ältere Python-Code beschreibt unter anderem
die Schreibsequenzen ausdrücklich als Nachbildung von IC35Link. Deshalb wird
die Veröffentlichung vorsorglich GPL-kompatibel behandelt; es wird keine
unabhängige Neuentwicklung sämtlicher Protokollteile behauptet.

Originalhinweis aus `src/syntrans.c`: **Copyright (C) 2000 Thomas Schulz**.
IC35Link enthält die GNU General Public License Version 2 in `COPYING`.
Maßgebliche Referenzen: `src/syntrans.c`, `doc/ic35sync.txt`, `doc/ic35mgr.txt`.

- Projekt: https://ic35link.sourceforge.net/
- Quellarchiv: https://ic35link.sourceforge.net/download/ic35link-1.18.tar.gz
- Betroffene Python-Module: `ic35_protocol.py`, `manager_protocol.py`,
  `memo_protocol.py`, `todo_protocol.py`.

Änderungen gegenüber den historischen Werkzeugen: Python/Windows-Transport,
GUI, Google- und Datei-Abgleich, strenge Rückleseprüfung und Journale.
Die historischen C-Quellen werden nicht in dieses Repository kopiert.

## Python-Abhängigkeiten

Die Pakete aus `requirements.txt` werden separat installiert; ihre Quelltexte
und Binärdateien sind nicht im Release-Archiv enthalten. Ihre jeweiligen
Lizenzhinweise gelten zusätzlich. Vor einer späteren EXE-Auslieferung müssen
die Lizenzen und Hinweise aller tatsächlich gebündelten Pakete mitgeliefert werden.

Der Radicale-Kompatibilitätsstarter verwendet Radicale als installiertes Paket
und behandelt unter Windows den Fehlercode 1314 beim Symlink-Test.

## Sounds und Namen

Die drei WAV-Dateien sind für dieses Projekt synthetisch erzeugte Signaltöne
und werden unter der Projektlizenz bereitgestellt. Es werden keine Siemens-
oder Google-Logos mitgeliefert. Siemens IC35, Google und Thunderbird werden
zur Beschreibung der Kompatibilität genannt; das Projekt ist unabhängig.
