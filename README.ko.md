# argonus-moex

[![Читать на русском](docs/assets/readme-ru.svg)](README.md) [![Read in English](docs/assets/readme-en.svg)](README.en.md) [![阅读简体中文版](docs/assets/readme-zh-CN.svg)](README.zh-CN.md) [![Leer en español](docs/assets/readme-es.svg)](README.es.md) [![Ler em português do Brasil](docs/assets/readme-pt-BR.svg)](README.pt-BR.md) [![日本語で読む](docs/assets/readme-ja.svg)](README.ja.md) [![Auf Deutsch lesen](docs/assets/readme-de.svg)](README.de.md)

Argonus는 모스크바 거래소(MOEX)의 주식 분석, 관심 종목 목록 생성,
장중 거래 전략 연구 및 T-Invest API를 통한 주문 실행을 위한 Python 프로젝트입니다.

## 프로젝트 구조

```text
argonus/
  trading/       트레이딩 봇과 주문 실행
  strategies/    신호와 위험 관리 규칙
  watchlists/    관심 종목 목록 생성과 후보 종목 선정
  market_data/   MOEX 및 T-Invest 시장 데이터
  models/        예측 모델 코드
  shadow/        섀도 실험 데이터 수집 및 평가
  backtesting/   과거 데이터 시뮬레이션
  research/      전략 연구
  training/      모델 학습
  runtime/       Tick 실행 스케줄러
config/          섀도 실험 설정과 활성화 템플릿
scripts/         실행 스크립트와 로컬 검증
tests/           테스트
data/            로컬 시장 데이터와 보고서
models/          로컬에 저장된 학습 모델
runtime/         로컬 로그와 봇 상태
```

## 설치

Python 3.10 이상이 필요합니다. 프로젝트 루트 디렉터리에서 명령을 실행하세요.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[research]'
```

## 검증

```bash
python -m unittest discover -s tests -t .
./run_candidate.sh --dry-run --once
python -m argonus.trading.trade_bot --help
python -m argonus.watchlists.generate_watchlist --help
```

테스트는 모의 증권사 응답과 임시 파일을 사용합니다. 과거 데이터 아카이브,
저장된 모델 또는 로컬 활성화 매니페스트에 의존하는 검증은 필요한 파일이 없으면
건너뜁니다. 이러한 파일은 공개 저장소에 포함되어 있지 않습니다.

## 데이터 접근

```bash
cp .tbank_token.example .tbank_token
chmod 600 .tbank_token
```

`.tbank_token` 파일에는 자리표시자 `YOUR_TBANK_API_TOKEN_HERE`가 들어 있습니다.
T-Invest를 사용하려면 본인의 토큰으로 바꾸세요. 환경 변수 `TINVEST_TOKEN`도
지원합니다. 실제 토큰은 로컬에만 저장하세요. `.tbank_token`과 `.env`는 Git 관리
대상에서 제외됩니다. 증권사 계좌 이름은 `BOT_ACCOUNT_NAME`으로 설정하며,
자리표시자 값은 `YOUR_ACCOUNT_NAME`입니다.

다운로드한 시장 데이터로 관심 종목 목록을 생성하려면 다음 명령을 실행하세요.

```bash
python -m argonus.watchlists.generate_watchlist -o data/watchlists/watchlist.txt
```

## 봇 실행

`./run_tick.sh`는 주문 전송을 활성화하지 않고 실행 주기를 한 번 수행합니다.
데이터 요청에 토큰과 계좌가 필요할 수 있습니다. 주문 전송은 `--live` 옵션으로
명시적으로 활성화해야 합니다. 스케줄러 명령 `./run_candidate.sh --dry-run --once`는
증권사에 연결하지 않고 실행 일정을 확인합니다.

공개 버전에는 `config/*activation_manifest.example.json`에 비활성 상태인
실거래 활성화 매니페스트 템플릿만 포함되어 있습니다. 이 템플릿은 설정 구조를
보여 주며, 본인의 아티팩트, 체크섬 및 별도 설정이 필요합니다.
섀도 실험 매니페스트는 `shadow_only` 모드에서만 동작합니다.
`scripts/validate_opening_integration.py` 스크립트는 과거 데이터, 학습된 모델 및
활성화 매니페스트를 갖춘 로컬 설치 환경용입니다.

새로운 연구는 `argonus/research/`에, 테스트는 `tests/`에 추가하세요.
데이터 경로는 `argonus.paths`를 사용해 가져오세요. 코드는 Python 모듈로 실행합니다.
`python -m argonus.<package>.<module>`.
