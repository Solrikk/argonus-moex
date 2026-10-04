# argonus-moex

[![Читать на русском](docs/assets/readme-ru.svg)](README.md) [![Read in English](docs/assets/readme-en.svg)](README.en.md) [![阅读简体中文版](docs/assets/readme-zh-CN.svg)](README.zh-CN.md) [![Leer en español](docs/assets/readme-es.svg)](README.es.md) [![Ler em português do Brasil](docs/assets/readme-pt-BR.svg)](README.pt-BR.md) [![한국어로 읽기](docs/assets/readme-ko.svg)](README.ko.md) [![Auf Deutsch lesen](docs/assets/readme-de.svg)](README.de.md)

[![README の表示回数](https://hits.sh/github.com/Solrikk/argonus-moex.svg?style=for-the-badge&label=README+views&color=2563eb&labelColor=1f2937)](https://hits.sh/github.com/Solrikk/argonus-moex/)

Argonus は、モスクワ証券取引所（MOEX）の株式分析、ウォッチリストの生成、
デイトレード戦略の研究、T-Invest API を通じた注文執行を行う Python プロジェクトです。

## プロジェクト構成

```text
argonus/
  trading/       取引ボットと注文執行
  strategies/    シグナルとリスク管理ルール
  watchlists/    ウォッチリストの生成と候補の選定
  market_data/   MOEX と T-Invest の市場データ
  models/        予測モデルのコード
  shadow/        シャドー実験のデータ収集と評価
  backtesting/   過去データによるシミュレーション
  research/      取引戦略の研究
  training/      モデルの学習
  runtime/       Tick 実行のスケジューラー
config/          シャドー実験の設定と有効化用テンプレート
scripts/         起動スクリプトとローカル検証
tests/           テスト
data/            ローカルの市場データとレポート
models/          ローカルの学習済みモデル
runtime/         ローカルのログとボットの状態
```

## インストール

Python 3.10 以降が必要です。コマンドはプロジェクトのルートディレクトリで実行してください。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[research]'
```

## 検証

```bash
python -m unittest discover -s tests -t .
./run_candidate.sh --dry-run --once
python -m argonus.trading.trade_bot --help
python -m argonus.watchlists.generate_watchlist --help
```

テストでは、証券会社の模擬レスポンスと一時ファイルを使用します。
履歴アーカイブ、保存済みモデル、ローカルの有効化マニフェストに依存する検証は、
必要なファイルが存在しない場合にスキップされます。これらのファイルは
公開リポジトリに含まれていません。

## データへのアクセス

```bash
cp .tbank_token.example .tbank_token
chmod 600 .tbank_token
```

`.tbank_token` ファイルにはプレースホルダー `YOUR_TBANK_API_TOKEN_HERE` が入っています。
T-Invest を使用するには、自分のトークンに置き換えてください。環境変数
`TINVEST_TOKEN` も使用できます。実際のトークンはローカルにのみ保存してください：
`.tbank_token` と `.env` は Git の管理対象から除外されています。証券会社の口座名は
`BOT_ACCOUNT_NAME` で設定します。プレースホルダー値は `YOUR_ACCOUNT_NAME` です。

取得した市場データを使ってウォッチリストを生成するには、次のコマンドを実行します。

```bash
python -m argonus.watchlists.generate_watchlist -o data/watchlists/watchlist.txt
```

## ボットの実行

`./run_tick.sh` は、注文送信を有効にせず、実行サイクルを 1 回実行します。
データのリクエストにはトークンと口座が必要になる場合があります。注文送信を
有効にするには、`--live` を明示的に指定する必要があります。スケジューラーの
コマンド `./run_candidate.sh --dry-run --once` は、証券会社に接続せずに
スケジュールを確認します。

公開版の本番環境用の有効化マニフェストは、
`config/*activation_manifest.example.json` にある、まだ有効化されていない
テンプレートのみです。これらは設定の構造を示しており、利用するには自分の
成果物、チェックサム、設定が必要です。シャドー実験のマニフェストは
`shadow_only` モードでのみ動作します。
`scripts/validate_opening_integration.py` は、履歴データ、学習済みモデル、
有効化マニフェストを備えたローカル環境向けのスクリプトです。

新しい研究は `argonus/research/` に、テストは `tests/` に追加してください。
データパスは `argonus.paths` を使って取得してください。コードは Python モジュールとして
実行します：`python -m argonus.<package>.<module>`。
