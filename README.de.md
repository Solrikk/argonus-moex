# argonus-moex

[![Читать на русском](docs/assets/readme-ru.svg)](README.md) [![Read in English](docs/assets/readme-en.svg)](README.en.md) [![阅读简体中文版](docs/assets/readme-zh-CN.svg)](README.zh-CN.md) [![Leer en español](docs/assets/readme-es.svg)](README.es.md) [![Ler em português do Brasil](docs/assets/readme-pt-BR.svg)](README.pt-BR.md) [![日本語で読む](docs/assets/readme-ja.svg)](README.ja.md) [![한국어로 읽기](docs/assets/readme-ko.svg)](README.ko.md)

[![README-Aufrufe](https://hits.sh/github.com/Solrikk/argonus-moex.svg?style=for-the-badge&label=README+views&color=2563eb&labelColor=1f2937)](https://hits.sh/github.com/Solrikk/argonus-moex/)

<sub>Ungefähre Aufrufe aller Sprachversionen seit Einrichtung des Zählers. Erneutes Laden kann mitgezählt werden.</sub>

Argonus ist ein Python-Projekt zur Analyse von Aktien an der Moskauer Börse
(MOEX), zur Erstellung von Watchlists, zur Erforschung von Intraday-Strategien
und zur Ausführung von Orders über die T-Invest-API.

## Projektstruktur

```text
argonus/
  trading/       Trading-Bot und Orderausführung
  strategies/    Signale und Risikoregeln
  watchlists/    Erstellung von Watchlists und Auswahl von Kandidaten
  market_data/   Marktdaten von MOEX und T-Invest
  models/        Code für Prognosemodelle
  shadow/        Datenerfassung und Auswertung von Shadow-Experimenten
  backtesting/   Historische Simulationen
  research/      Strategieforschung
  training/      Modelltraining
  runtime/       Tick-Scheduler
config/          Konfiguration von Shadow-Experimenten und Aktivierungsvorlagen
scripts/         Startskripte und lokale Überprüfung
tests/           Tests
data/            Lokale Marktdaten und Berichte
models/          Lokal gespeicherte trainierte Modelle
runtime/         Lokale Protokolle und Bot-Zustand
```

## Installation

Python 3.10 oder neuer wird benötigt. Führe die Befehle im Stammverzeichnis des
Projekts aus.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[research]'
```

## Überprüfung

```bash
python -m unittest discover -s tests -t .
./run_candidate.sh --dry-run --once
python -m argonus.trading.trade_bot --help
python -m argonus.watchlists.generate_watchlist --help
```

Die Tests verwenden simulierte Antworten des Brokers und temporäre Dateien.
Prüfungen, die historische Archive, gespeicherte Modelle oder lokale
Aktivierungsmanifeste voraussetzen, werden übersprungen, wenn die erforderlichen
Dateien fehlen. Diese Dateien sind nicht im öffentlichen Repository enthalten.

## Datenzugriff

```bash
cp .tbank_token.example .tbank_token
chmod 600 .tbank_token
```

Die Datei `.tbank_token` enthält den Platzhalter `YOUR_TBANK_API_TOKEN_HERE`.
Ersetze ihn durch deinen eigenen Token, um T-Invest zu nutzen. Die
Umgebungsvariable `TINVEST_TOKEN` wird ebenfalls unterstützt. Speichere deinen
tatsächlichen Token ausschließlich lokal: `.tbank_token` und `.env` sind von
Git ausgeschlossen. Der Name des Brokerkontos wird über `BOT_ACCOUNT_NAME`
festgelegt; der Platzhalterwert lautet `YOUR_ACCOUNT_NAME`.

Um eine Watchlist mit heruntergeladenen Marktdaten zu erstellen:

```bash
python -m argonus.watchlists.generate_watchlist -o data/watchlists/watchlist.txt
```

## Bot ausführen

`./run_tick.sh` führt einen Tick aus, ohne die Übermittlung von Orders zu
aktivieren. Datenabfragen können einen Token und ein Konto voraussetzen.
Die Orderübermittlung muss mit `--live` ausdrücklich aktiviert werden.
Der Scheduler-Befehl `./run_candidate.sh --dry-run --once` überprüft die
Zeitplanung, ohne den Broker zu kontaktieren.

Die öffentliche Version enthält in `config/*activation_manifest.example.json`
nur nicht aktivierte Vorlagen für Aktivierungsmanifeste für den Produktionsbetrieb.
Sie zeigen die Konfigurationsstruktur und erfordern eigene Artefakte, Prüfsummen
und Einstellungen. Manifeste für Shadow-Experimente arbeiten ausschließlich im
Modus `shadow_only`.
Das Skript `scripts/validate_opening_integration.py` ist für eine lokale
Installation mit historischen Daten, trainierten Modellen und
Aktivierungsmanifesten vorgesehen.

Füge neue Forschungsarbeiten unter `argonus/research/` und Tests unter `tests/`
hinzu. Verwende `argonus.paths` für die Datenpfade. Führe den Code als
Python-Modul aus: `python -m argonus.<package>.<module>`.
