# argonus-moex

[![Читать на русском](docs/assets/readme-ru.svg)](README.md) [![阅读简体中文版](docs/assets/readme-zh-CN.svg)](README.zh-CN.md) [![Leer en español](docs/assets/readme-es.svg)](README.es.md) [![Ler em português do Brasil](docs/assets/readme-pt-BR.svg)](README.pt-BR.md) [![日本語で読む](docs/assets/readme-ja.svg)](README.ja.md) [![한국어로 읽기](docs/assets/readme-ko.svg)](README.ko.md) [![Auf Deutsch lesen](docs/assets/readme-de.svg)](README.de.md)

[![README views](https://hits.sh/github.com/Solrikk/argonus-moex.svg?style=for-the-badge&label=README+views&color=2563eb&labelColor=1f2937)](https://hits.sh/github.com/Solrikk/argonus-moex/)

<sub>Approximate views of all language versions since setup. Repeat loads may count.</sub>

Argonus is a Python project for analyzing stocks on the Moscow Exchange (MOEX),
generating watchlists, researching intraday strategies, and executing orders
through the T-Invest API.

## Project structure

```text
argonus/
  trading/       Trading bot and order execution
  strategies/    Signals and risk rules
  watchlists/    Watchlist generation and candidate selection
  market_data/   MOEX and T-Invest market data
  models/        Predictive model code
  shadow/        Shadow experiment data collection and evaluation
  backtesting/   Historical simulations
  research/      Strategy research
  training/      Model training
  runtime/       Tick scheduler
config/          Shadow experiment settings and activation templates
scripts/         Launch scripts and local validation
tests/           Tests
data/            Local market data and reports
models/          Local trained models
runtime/         Local logs and bot state
```

## Installation

Requires Python 3.10 or later. Run the commands from the project root.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[research]'
```

## Validation

```bash
python -m unittest discover -s tests -t .
./run_candidate.sh --dry-run --once
python -m argonus.trading.trade_bot --help
python -m argonus.watchlists.generate_watchlist --help
```

Tests use mock broker responses and temporary files. Checks that depend on
historical archives, saved models, or local activation manifests are skipped
when the required files are absent. These files are not included in the public
repository.

## Data access

```bash
cp .tbank_token.example .tbank_token
chmod 600 .tbank_token
```

The `.tbank_token` file contains the placeholder `YOUR_TBANK_API_TOKEN_HERE`.
Replace it with your own token to use T-Invest. The `TINVEST_TOKEN` environment
variable is also supported. Keep your actual token local: `.tbank_token` and
`.env` are excluded from Git. Set the broker account name through
`BOT_ACCOUNT_NAME`; its placeholder value is `YOUR_ACCOUNT_NAME`.

To generate a watchlist using downloaded market data:

```bash
python -m argonus.watchlists.generate_watchlist -o data/watchlists/watchlist.txt
```

## Running the bot

`./run_tick.sh` runs a tick without enabling order submission. Data requests may
require a token and an account. Order submission must be enabled explicitly
with `--live`. The scheduler command `./run_candidate.sh --dry-run --once` checks
the schedule without contacting the broker.

The public version contains only inactive production activation manifest
templates in `config/*activation_manifest.example.json`. They show the
configuration structure and require your own artifacts, checksums, and setup.
Shadow experiment manifests operate only in `shadow_only` mode.
The `scripts/validate_opening_integration.py` script is intended for a local
installation with historical data, trained models, and activation manifests.

Add new research to `argonus/research/` and tests to `tests/`.
Use `argonus.paths` for data locations. Run the code as a Python module:
`python -m argonus.<package>.<module>`.
