# argonus-moex

[![Читать на русском](docs/assets/readme-ru.svg)](README.md) [![Read in English](docs/assets/readme-en.svg)](README.en.md) [![阅读简体中文版](docs/assets/readme-zh-CN.svg)](README.zh-CN.md) [![Ler em português do Brasil](docs/assets/readme-pt-BR.svg)](README.pt-BR.md) [![日本語で読む](docs/assets/readme-ja.svg)](README.ja.md)

Argonus es un proyecto en Python para analizar acciones de la Bolsa de Moscú
(MOEX), generar listas de seguimiento, investigar estrategias intradía y
ejecutar órdenes a través de la API de T-Invest.

## Estructura del proyecto

```text
argonus/
  trading/       Bot de trading y ejecución de órdenes
  strategies/    Señales y reglas de riesgo
  watchlists/    Generación de listas y selección de candidatos
  market_data/   Datos de mercado de MOEX y T-Invest
  models/        Código de modelos predictivos
  shadow/        Recopilación de datos y evaluación de experimentos en modo sombra
  backtesting/   Simulaciones históricas
  research/      Investigación de estrategias
  training/      Entrenamiento de modelos
  runtime/       Programador de ciclos de ejecución
config/          Configuración de experimentos en modo sombra y plantillas de activación
scripts/         Scripts de inicio y validación local
tests/           Pruebas
data/            Datos de mercado e informes locales
models/          Modelos entrenados locales
runtime/         Registros y estado local del bot
```

## Instalación

Requiere Python 3.10 o posterior. Ejecuta los comandos desde la raíz del proyecto.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[research]'
```

## Validación

```bash
python -m unittest discover -s tests -t .
./run_candidate.sh --dry-run --once
python -m argonus.trading.trade_bot --help
python -m argonus.watchlists.generate_watchlist --help
```

Las pruebas utilizan respuestas simuladas del bróker y archivos temporales.
Las comprobaciones que dependen de archivos históricos, modelos guardados o
manifiestos de activación locales se omiten cuando faltan los archivos
necesarios. Estos archivos no se incluyen en el repositorio público.

## Acceso a los datos

```bash
cp .tbank_token.example .tbank_token
chmod 600 .tbank_token
```

El archivo `.tbank_token` contiene el marcador `YOUR_TBANK_API_TOKEN_HERE`.
Sustitúyelo por tu propio token para utilizar T-Invest. También se admite la
variable de entorno `TINVEST_TOKEN`. Guarda tu token real solo de forma local:
`.tbank_token` y `.env` están excluidos de Git. Configura el nombre de la cuenta
del bróker mediante `BOT_ACCOUNT_NAME`; su valor de ejemplo es `YOUR_ACCOUNT_NAME`.

Para generar una lista de seguimiento con datos de mercado descargados:

```bash
python -m argonus.watchlists.generate_watchlist -o data/watchlists/watchlist.txt
```

## Ejecución del bot

`./run_tick.sh` ejecuta un ciclo sin habilitar el envío de órdenes. Las solicitudes
de datos pueden requerir un token y una cuenta. El envío de órdenes debe
habilitarse explícitamente con `--live`. El comando del programador
`./run_candidate.sh --dry-run --once` comprueba la programación sin contactar
con el bróker.

La versión pública solo contiene plantillas inactivas de manifiestos de
activación para producción en `config/*activation_manifest.example.json`.
Estas muestran la estructura de la configuración y requieren tus propios
artefactos, sumas de comprobación y ajustes. Los manifiestos de experimentos
en modo sombra funcionan únicamente en modo `shadow_only`.
El script `scripts/validate_opening_integration.py` está destinado a una
instalación local con datos históricos, modelos entrenados y manifiestos de
activación.

Añade nuevas investigaciones a `argonus/research/` y pruebas a `tests/`.
Utiliza `argonus.paths` para obtener las rutas de los datos. Ejecuta el código
como un módulo de Python: `python -m argonus.<package>.<module>`.
