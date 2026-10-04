# argonus-moex

[![Read in English](docs/assets/readme-en.svg)](README.en.md) [![阅读简体中文版](docs/assets/readme-zh-CN.svg)](README.zh-CN.md) [![Leer en español](docs/assets/readme-es.svg)](README.es.md) [![Ler em português do Brasil](docs/assets/readme-pt-BR.svg)](README.pt-BR.md) [![日本語で読む](docs/assets/readme-ja.svg)](README.ja.md)

Argonus — Python-проект для анализа акций Московской биржи, генерации вотчлистов,
исследования внутридневных стратегий и исполнения заявок через T-Invest API.

## Структура

```text
argonus/
  trading/       Торговый бот и исполнение заявок
  strategies/    Сигналы и правила риска
  watchlists/    Генерация и отбор кандидатов
  market_data/   Работа с MOEX и T-Invest
  models/        Код прогнозных моделей
  shadow/        Сбор и оценка теневых экспериментов
  backtesting/   Исторические симуляции
  research/      Исследования стратегий
  training/      Обучение моделей
  runtime/       Планировщик тиков
config/          Настройки теневых экспериментов и шаблоны активации
scripts/         Скрипты запуска и локальной проверки
tests/           Тесты
data/            Локальные рыночные данные и отчёты
models/          Локальные обученные модели
runtime/         Локальные журналы и состояние бота
```

## Установка

Требуется Python 3.10 или новее. Команды выполняются из корня проекта.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[research]'
```

## Проверка

```bash
python -m unittest discover -s tests -t .
./run_candidate.sh --dry-run --once
python -m argonus.trading.trade_bot --help
python -m argonus.watchlists.generate_watchlist --help
```

Тесты используют поддельные ответы брокера и временные файлы. Проверки исторических
архивов, сохранённых моделей и локальных манифестов активации пропускаются, если
соответствующих файлов нет. Сами эти файлы в публичный репозиторий не включены.

## Доступ к данным

```bash
cp .tbank_token.example .tbank_token
chmod 600 .tbank_token
```

В `.tbank_token` находится шаблон `YOUR_TBANK_API_TOKEN_HERE` — замените его своим
токеном для работы с T-Invest. Также поддерживается переменная `TINVEST_TOKEN`.
Настоящий токен храните только локально: `.tbank_token` и `.env` исключены из Git.
Название брокерского счёта задаётся через `BOT_ACCOUNT_NAME`; значение шаблона —
`YOUR_ACCOUNT_NAME`.

Для генерации вотчлиста с загрузкой рыночных данных:

```bash
python -m argonus.watchlists.generate_watchlist -o data/watchlists/watchlist.txt
```

## Запуск бота

`./run_tick.sh` запускает тик без разрешения на выставление заявок. Для запросов
данных ему могут потребоваться токен и счёт. Выставление заявок включается явно
флагом `--live`. Планировщик `./run_candidate.sh --dry-run --once` проверяет
расписание без обращения к брокеру.

Публичная копия содержит только неактивные шаблоны производственных манифестов
`config/*activation_manifest.example.json`. Они показывают структуру настроек,
но требуют собственных артефактов, контрольных сумм и отдельной настройки.
Манифесты теневых экспериментов работают только в режиме `shadow_only`.
Скрипт `scripts/validate_opening_integration.py` предназначен для локальной
установки с историческими данными, обученными моделями и манифестами активации.

Новые исследования добавляйте в `argonus/research/`, тесты — в `tests/`.
Пути к данным берите из `argonus.paths`. Код запускается как модуль:
`python -m argonus.<пакет>.<модуль>`.
