# argonus-moex

[![Читать на русском](docs/assets/readme-ru.svg)](README.md) [![Read in English](docs/assets/readme-en.svg)](README.en.md) [![Leer en español](docs/assets/readme-es.svg)](README.es.md) [![Ler em português do Brasil](docs/assets/readme-pt-BR.svg)](README.pt-BR.md) [![日本語で読む](docs/assets/readme-ja.svg)](README.ja.md) [![한국어로 읽기](docs/assets/readme-ko.svg)](README.ko.md) [![Auf Deutsch lesen](docs/assets/readme-de.svg)](README.de.md)

Argonus 是一个 Python 项目，用于分析莫斯科交易所（MOEX）的股票、生成股票观察列表、
研究日内交易策略，并通过 T-Invest API 执行交易订单。

## 项目结构

```text
argonus/
  trading/       交易机器人与订单执行
  strategies/    交易信号与风险规则
  watchlists/    观察列表生成与候选标的筛选
  market_data/   MOEX 和 T-Invest 市场数据
  models/        预测模型代码
  shadow/        影子实验的数据采集与评估
  backtesting/   历史模拟
  research/      交易策略研究
  training/      模型训练
  runtime/       Tick 调度器
config/          影子实验配置与激活模板
scripts/         启动脚本与本地验证
tests/           测试
data/            本地市场数据与报告
models/          本地已训练模型
runtime/         本地日志与机器人状态
```

## 安装

需要 Python 3.10 或更高版本。请在项目根目录中执行命令。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[research]'
```

## 验证

```bash
python -m unittest discover -s tests -t .
./run_candidate.sh --dry-run --once
python -m argonus.trading.trade_bot --help
python -m argonus.watchlists.generate_watchlist --help
```

测试使用模拟的券商响应和临时文件。依赖历史归档数据、已保存模型或本地激活清单的
检查，在所需文件不存在时会跳过。这些文件未包含在公共仓库中。

## 数据访问

```bash
cp .tbank_token.example .tbank_token
chmod 600 .tbank_token
```

`.tbank_token` 文件中包含占位符 `YOUR_TBANK_API_TOKEN_HERE`。请将其替换为你自己的
令牌以使用 T-Invest。也支持 `TINVEST_TOKEN` 环境变量。实际令牌应仅保存在本地：
`.tbank_token` 和 `.env` 已被 Git 忽略。券商账户名称通过 `BOT_ACCOUNT_NAME` 设置；
其占位值为 `YOUR_ACCOUNT_NAME`。

使用下载的市场数据生成观察列表：

```bash
python -m argonus.watchlists.generate_watchlist -o data/watchlists/watchlist.txt
```

## 运行机器人

`./run_tick.sh` 执行一个处理周期，但默认不启用订单提交。数据请求可能需要令牌和
账户。必须显式指定 `--live` 才能启用订单提交。调度器命令
`./run_candidate.sh --dry-run --once` 检查调度计划，不会连接券商。

公共版本仅包含 `config/*activation_manifest.example.json` 中的未激活生产环境
激活清单模板。它们展示了配置结构，使用时仍需提供你自己的相关文件、校验和，
并完成配置。影子实验清单仅在 `shadow_only` 模式下运行。
`scripts/validate_opening_integration.py` 脚本适用于包含历史数据、已训练模型和
激活清单的本地安装环境。

将新增研究放入 `argonus/research/`，测试放入 `tests/`。
请通过 `argonus.paths` 获取数据路径。以 Python 模块方式运行代码：
`python -m argonus.<package>.<module>`。
