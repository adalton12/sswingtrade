# SSWingTrade - Status do Projeto & Guia de Push

## Status Atual: FASE 1 CONCLUIDA

**Repositorio:** https://github.com/adalton12/sswingtrade
**Branch:** main
**Arquivos:** 24 arquivos da arquitetura base

---

## Como fazer Push para o GitHub

### Pre-requisitos
- Git instalado no Windows
- Token classico do GitHub com permissao "repo"
  - Gerar em: GitHub > Settings > Developer settings > Personal access tokens > Tokens (classic)

### Comandos para Push

```bash
# 1. Entrar na pasta do projeto
cd C:\Users\adalto.junior\Downloads\sswingtrade-FASE1-complete\sswingtrade

# 2. Se for a primeira vez (sem .git na pasta)
git init
git remote add origin https://adalton12:SEU_TOKEN_AQUI@github.com/adalton12/sswingtrade.git
git add -A
git commit -m "Descricao do que mudou"
git branch -M main
git push -u origin main --force

# 3. Se ja tem .git configurado (pushes seguintes)
git add -A
git commit -m "Descricao do que mudou"
git push

# 4. Se der erro de permissao (credencial antiga do Windows)
git remote set-url origin https://adalton12:SEU_TOKEN_AQUI@github.com/adalton12/sswingtrade.git
git push
```

### Cuidados Importantes
- **NUNCA** commite arquivos com tokens/senhas (ex: .env, push_github.html)
- O `.gitignore` ja protege `.env`, `__pycache__`, `logs/`, etc.
- Se o GitHub bloquear por "secret scanning", apague o `.git`, remova o arquivo com o secret, e reinicie o repo
- Apos o push, limpe o token da URL: `git remote set-url origin https://github.com/adalton12/sswingtrade.git`

---

## Fases do Projeto - O que falta

### FASE 1 - Arquitetura Base [CONCLUIDA]
- [x] Docker Compose (PostgreSQL, Redis, Ollama, API, Prometheus, Grafana)
- [x] FastAPI com lifespan, CORS, error handlers
- [x] SQLAlchemy 2.0 async ORM (13 tabelas)
- [x] Pydantic BaseSettings (47 variaveis)
- [x] Redis cache service
- [x] Structured logging (loguru)
- [x] Health check endpoints
- [x] Routes: health, capital, market_data
- [x] Dockerfile multi-stage
- [x] PostgreSQL init com TimescaleDB
- [x] Prometheus config
- [x] README, ARCHITECTURE.md, IMPLEMENTATION_CHECKLIST

### FASE 2 - Market Data EOD [PENDENTE]
- [ ] Integracao com yfinance para dados gratuitos da B3
- [ ] Servico de coleta automatica de candles diarios/horarios
- [ ] Armazenamento de historico no PostgreSQL/TimescaleDB
- [ ] Tabelas: daily_candles, intraday_candles
- [ ] API endpoints para consulta de dados historicos
- [ ] Scheduler para coleta pos-fechamento de mercado
- [ ] Cache Redis para dados frequentemente acessados
- [ ] Suporte a multiplos ativos (PETR4, VALE3, ITUB4, etc.)

### FASE 3 - Modulo Quantitativo [PENDENTE]
- [ ] Calculo de indicadores tecnicos locais:
  - SMA (Simple Moving Average)
  - EMA (Exponential Moving Average)
  - RSI (Relative Strength Index)
  - MACD (Moving Average Convergence Divergence)
  - VWAP diaria
  - ATR (Average True Range / volatilidade)
  - Bandas de Bollinger
  - Variacao de volume
- [ ] Feature engineering para ML
- [ ] Armazenamento de indicadores calculados
- [ ] API para consulta de indicadores por ativo/periodo

### FASE 4 - Motor de Backtesting [PENDENTE]
- [ ] Engine de backtesting sem look-ahead bias
- [ ] Simulacao com taxas da B3 e slippage
- [ ] Metricas: Sharpe, Sortino, Max Drawdown, Win Rate
- [ ] Relatorios de performance por estrategia
- [ ] Suporte a multiplas estrategias simultaneas
- [ ] Exportacao de resultados (CSV, JSON)

### FASE 5 - Machine Learning [PENDENTE]
- [ ] Modelos supervisionados: XGBoost / LightGBM
- [ ] Predicao probabilistica:
  - P(Alta >= 2% nos proximos 3 dias)
  - P(Alta >= 4% nos proximos 5 dias)
- [ ] Pipeline de treinamento e validacao
- [ ] Feature selection automatica
- [ ] Cross-validation temporal (walk-forward)
- [ ] Persistencia e versionamento de modelos
- [ ] API para predicoes em tempo real

### FASE 6 - IA Local (Ollama) + Agentes [PENDENTE]
- [ ] Integracao com Ollama (Qwen 3B ou Llama 3.2 3B)
- [ ] Agente NEWS ANALYST: sentimento de noticias (-1.0 a +1.0)
- [ ] Agente EVENT ANALYST: deteccao de balancos, proventos, fatos relevantes
- [ ] Agente RESEARCH ANALYST: relatorio consolidado do ativo
- [ ] Agente ADVOGADO DO DIABO: contradiz a tese de compra/venda
- [ ] Batch processing no fechamento do mercado
- [ ] Saida estruturada em JSON com hash anti-duplicacao
- [ ] Armazenamento de analises no banco

### FASE 7 - Gestao de Capital [PENDENTE]
- [ ] Capital base semanal: R$ 500,00
- [ ] Alocacao diaria: R$ 100,00 por operacao
- [ ] Reinvestimento com juros compostos (lucro semana anterior + R$ 500)
- [ ] Aporte mensal configuravel (MONTHLY_DEPOSIT)
- [ ] Position sizing dinamico (escala com patrimonio)
- [ ] Historico de evolucao patrimonial
- [ ] Relatorios de rentabilidade

### FASE 8 - Risk Engine + Paper Trading [PENDENTE]
- [ ] Risk Engine inviolavel (nenhuma ordem sem aprovacao)
- [ ] MAX_DAILY_ALLOCATION: R$ 100/operacao (escalavel)
- [ ] MAX_WEEKLY_CAPITAL: R$ 500 + lucros
- [ ] STOP_LOSS: baseado em ATR (1.5x a 2x)
- [ ] TAKE_PROFIT: relacao risco/retorno minima 1:2
- [ ] MAX_DAILY_LOSS: 1.5% do capital (circuit breaker)
- [ ] PaperBroker: simulador de execucoes
- [ ] BrokerInterface: camada abstrata para broker real futuro
- [ ] Registro 100% das decisoes (entrada, saida, score, modelo)

### FASE 9 - Dashboard Web [PENDENTE]
- [ ] Dashboard simples para acompanhamento
- [ ] Visualizacao de saldo e P&L
- [ ] Posicoes abertas e historico
- [ ] Graficos de evolucao patrimonial
- [ ] Alertas e notificacoes
- [ ] Integracao com Grafana existente

---

## Estrutura de Arquivos Atual (FASE 1)

```
sswingtrade/
├── app/
│   ├── __init__.py
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── health.py
│   │   ├── capital.py
│   │   └── market_data.py
│   └── services/
│       ├── __init__.py
│       ├── cache.py
│       └── logger.py
├── docker/
│   ├── Dockerfile
│   ├── postgres/
│   │   └── init.sql
│   └── prometheus/
│       └── prometheus.yml
├── .env.example
├── .gitignore
├── ARCHITECTURE.md
├── config.py
├── database.py
├── docker-compose.yml
├── IMPLEMENTATION_CHECKLIST.md
├── main.py
├── models.py
├── PROJECT_STRUCTURE.txt
├── README.md
├── README_FASE1.md
└── requirements.txt
```

---

## Stack Tecnologica

| Componente | Tecnologia |
|---|---|
| Linguagem | Python 3.11+ |
| Framework | FastAPI (async/await) |
| Banco de Dados | PostgreSQL 16 + TimescaleDB |
| Cache | Redis 7 |
| IA Local | Ollama (Qwen 3B) |
| ML | Scikit-Learn, XGBoost, LightGBM |
| Data | Pandas, NumPy, Polars |
| Market Data | yfinance (gratuito) |
| Containers | Docker + Docker Compose |
| Monitoramento | Prometheus + Grafana |
| ORM | SQLAlchemy 2.0 async |

---

*Ultima atualizacao: 2026-10-05*
