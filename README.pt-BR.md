# argonus-moex

[![Читать на русском](docs/assets/readme-ru.svg)](README.md) [![Read in English](docs/assets/readme-en.svg)](README.en.md) [![阅读简体中文版](docs/assets/readme-zh-CN.svg)](README.zh-CN.md) [![Leer en español](docs/assets/readme-es.svg)](README.es.md) [![日本語で読む](docs/assets/readme-ja.svg)](README.ja.md) [![한국어로 읽기](docs/assets/readme-ko.svg)](README.ko.md) [![Auf Deutsch lesen](docs/assets/readme-de.svg)](README.de.md)

[![Visualizações do README](https://hits.sh/github.com/Solrikk/argonus-moex.svg?style=for-the-badge&label=README+views&color=2563eb&labelColor=1f2937)](https://hits.sh/github.com/Solrikk/argonus-moex/)

Argonus é um projeto em Python para analisar ações da Bolsa de Moscou (MOEX),
gerar listas de acompanhamento, pesquisar estratégias intradiárias e executar
ordens pela API T-Invest.

## Estrutura do projeto

```text
argonus/
  trading/       Robô de negociação e execução de ordens
  strategies/    Sinais e regras de risco
  watchlists/    Geração de listas e seleção de candidatos
  market_data/   Dados de mercado da MOEX e do T-Invest
  models/        Código dos modelos preditivos
  shadow/        Coleta de dados e avaliação de experimentos em modo sombra
  backtesting/   Simulações históricas
  research/      Pesquisa de estratégias
  training/      Treinamento de modelos
  runtime/       Agendador de ciclos de execução
config/          Configurações dos experimentos em modo sombra e modelos de ativação
scripts/         Scripts de inicialização e validação local
tests/           Testes
data/            Dados de mercado e relatórios locais
models/          Modelos treinados locais
runtime/         Logs e estado local do robô
```

## Instalação

Requer Python 3.10 ou superior. Execute os comandos na raiz do projeto.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[research]'
```

## Validação

```bash
python -m unittest discover -s tests -t .
./run_candidate.sh --dry-run --once
python -m argonus.trading.trade_bot --help
python -m argonus.watchlists.generate_watchlist --help
```

Os testes usam respostas simuladas da corretora e arquivos temporários.
As verificações que dependem de dados históricos arquivados, modelos salvos ou
manifestos de ativação locais são ignoradas quando os arquivos necessários
não estão disponíveis. Esses arquivos não estão incluídos no repositório público.

## Acesso aos dados

```bash
cp .tbank_token.example .tbank_token
chmod 600 .tbank_token
```

O arquivo `.tbank_token` contém o valor de exemplo `YOUR_TBANK_API_TOKEN_HERE`.
Substitua-o pelo seu próprio token para usar o T-Invest. A variável de ambiente
`TINVEST_TOKEN` também é aceita. Mantenha seu token real apenas localmente:
`.tbank_token` e `.env` são excluídos do Git. Defina o nome da conta na corretora
pela variável `BOT_ACCOUNT_NAME`; seu valor de exemplo é `YOUR_ACCOUNT_NAME`.

Para gerar uma lista de acompanhamento com dados de mercado baixados:

```bash
python -m argonus.watchlists.generate_watchlist -o data/watchlists/watchlist.txt
```

## Execução do robô

`./run_tick.sh` executa um ciclo sem habilitar o envio de ordens. As solicitações
de dados podem exigir um token e uma conta. O envio de ordens deve ser habilitado
explicitamente com `--live`. O comando do agendador
`./run_candidate.sh --dry-run --once` verifica a programação sem acessar a corretora.

A versão pública contém apenas modelos inativos de manifestos de ativação
para produção em `config/*activation_manifest.example.json`. Eles mostram a
estrutura da configuração e exigem seus próprios artefatos, somas de verificação
e ajustes. Os manifestos de experimentos em modo sombra funcionam apenas no
modo `shadow_only`.
O script `scripts/validate_opening_integration.py` é destinado a uma instalação
local com dados históricos, modelos treinados e manifestos de ativação.

Adicione novas pesquisas a `argonus/research/` e testes a `tests/`.
Use `argonus.paths` para obter os caminhos dos dados. Execute o código como
um módulo Python: `python -m argonus.<package>.<module>`.
