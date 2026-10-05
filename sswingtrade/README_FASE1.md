# SSWingTrade - FASE 1: Foundation & Architecture

## 🏗️ Project Overview

**SSWingTrade** é uma plataforma modular, local-first para análise e trading automatizado focado em **Swing Trade** (1-5 dias).

### Core Principles
- ✅ **Zero-cost market data** (yfinance)
- ✅ **Local execution** (Docker + Ollama)
- ✅ **Open source** (Python + FastAPI + PostgreSQL)
- ✅ **High observability** (JSON logs + Prometheus + Grafana)
- ✅ **Paper trading first** (simulação antes de real)
- ✅ **Dynamic capital management** (R$ 100/dia, R$ 500/semana, juros compostos)

---

## 📋 FASE 1 Deliverables

FASE 1 entrega a **base arquitetural** e **infraestrutura**:

### Files Included

```
✅ docker-compose.yml       - Orquestração de serviços (PostgreSQL, Redis, Ollama, API, Prometheus, Grafana)
✅ requirements.txt         - Dependências Python
✅ Dockerfile              - Imagem da aplicação FastAPI
✅ config.py               - Settings management com Pydantic
✅ models.py               - SQLAlchemy ORM (13 tabelas)
✅ database.py             - Inicialização, sessions, migrations
✅ main.py                 - FastAPI app com lifespan, error handlers
✅ routes_health.py        - Health check endpoints
✅ routes_capital.py       - Capital management APIs
✅ routes_market_data.py   - Market data APIs
✅ logger.py               - Structured logging (JSON)
✅ cache.py                - Redis cache service
✅ .env.example            - Template de variáveis de ambiente
✅ init.sql                - Script SQL de inicialização
✅ prometheus.yml          - Configuração de métricas
✅ README_FASE1.md         - Este arquivo
```

---

## 🚀 Quick Start

### 1. Prerequisites

- **Docker** (20.10+)
- **Docker Compose** (2.0+)
- **Git** (para clonar o repo)

Verificar instalação:
```bash
docker --version
docker compose --version
```

### 2. Setup do Projeto

```bash
# 1. Clonar ou extrair o projeto
cd /caminho/para/sswingtrade

# 2. Copiar variáveis de ambiente
cp .env.example .env

# 3. Criar diretórios necessários
mkdir -p docker/postgres docker/grafana/provisioning logs models

# 4. Colocar arquivos na estrutura correta:
# (Ajustar paths conforme seu setup local)
mkdir -p app/routes app/services app/models
```

### 3. Estrutura de Diretórios

```
sswingtrade/
├── app/
│   ├── __init__.py
│   ├── main.py                    (main.py do arquivo fornecido)
│   ├── config.py                  (config.py)
│   ├── database.py                (database.py)
│   ├── models.py                  (models.py)
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── health.py              (routes_health.py → copiar para aqui)
│   │   ├── capital.py             (routes_capital.py → copiar para aqui)
│   │   └── market_data.py         (routes_market_data.py → copiar para aqui)
│   └── services/
│       ├── __init__.py
│       ├── logger.py              (logger.py)
│       └── cache.py               (cache.py)
│
├── docker/
│   ├── Dockerfile                 (Dockerfile)
│   ├── postgres/
│   │   └── init.sql               (init.sql)
│   ├── prometheus/
│   │   └── prometheus.yml         (prometheus.yml)
│   └── grafana/
│       └── provisioning/          (create if needed)
│
├── docker-compose.yml             (docker-compose.yml)
├── requirements.txt               (requirements.txt)
├── .env                           (copy from .env.example)
├── .env.example                   (.env.example)
├── logs/                          (create for logs)
├── models/                        (create for ML models)
└── README_FASE1.md               (este arquivo)
```

### 4. Start Services

```bash
# Build imagens e start containers
docker compose up -d

# Verificar status
docker compose ps

# Output esperado:
# NAME                    STATUS
# sswingtrade-postgres    Up (healthy)
# sswingtrade-redis       Up (healthy)
# sswingtrade-ollama      Up
# sswingtrade-api         Up (healthy)
# sswingtrade-prometheus  Up
# sswingtrade-grafana     Up
```

### 5. Verificar Componentes

```bash
# API Health Check
curl http://localhost:8000/health

# API Documentation
# Abrir no browser: http://localhost:8000/docs

# Prometheus
# http://localhost:9090

# Grafana
# http://localhost:3000 (admin / admin)

# Database
psql -h localhost -U trading -d sswingtrade
# Password: trading_dev_pwd

# Redis
redis-cli -h localhost ping
# PONG

# Ollama
curl http://localhost:11434/api/tags
```

---

## 🔧 API Endpoints (FASE 1)

### Health Check
```bash
GET /health
GET /health/detailed
```

### Capital Management
```bash
POST   /api/v1/capital/account              # Create account
GET    /api/v1/capital/account/{account_id} # Get account
POST   /api/v1/capital/allocation/check     # Check allocation
GET    /api/v1/capital/account/{account_id}/history # Get history
```

### Market Data
```bash
GET    /api/v1/market/candles/{ticker}      # Get candles
GET    /api/v1/market/candles/{ticker}/latest # Latest candle
GET    /api/v1/market/tickers/{ticker}      # Ticker info
POST   /api/v1/market/tickers/{ticker}/register # Register ticker
POST   /api/v1/market/sync/yfinance         # Sync data (FASE 2)
```

### Exemplo de Requisição

```bash
# Criar conta
curl -X POST "http://localhost:8000/api/v1/capital/account" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "usuario_001",
    "initial_capital": 500.00
  }'

# Resposta:
# {
#   "id": 1,
#   "user_id": "usuario_001",
#   "current_balance": 500.0,
#   "available_balance": 500.0,
#   "invested_capital": 0.0,
#   ...
# }
```

---

## 🗄️ Database Schema (ORM Models)

### Tabelas Criadas Automaticamente

| Tabela | Descrição |
|--------|-----------|
| `market_candles` | OHLCV data para análise técnica |
| `ticker_info` | Metadados de tickers |
| `accounts` | Contas de trading com capital tracking |
| `capital_history` | Auditoria de movimentações de capital |
| `positions` | Posições abertas/fechadas |
| `orders` | Ordens de compra/venda |
| `news_events` | Notícias e eventos (FASE 6+) |
| `backtest_runs` | Resultados de backtesting |

**Migrations:** Usar Alembic (FASE 2)

---

## 📊 Monitoring

### Prometheus Metrics (FASE 2+)

Métricas coletadas em `/metrics`:

```
sswingtrade_api_requests_total
sswingtrade_api_request_duration_seconds
sswingtrade_orders_total
sswingtrade_positions_open
sswingtrade_account_balance
sswingtrade_daily_loss_percent
```

### Grafana Dashboards (FASE 3+)

- Account Overview
- P&L Tracking
- Risk Monitor
- Capital Allocation

---

## 🛑 Troubleshooting

### PostgreSQL Connection Error
```bash
# Check if postgres is running
docker compose logs postgres

# Verify credentials in .env
# Default: trading / trading_dev_pwd

# Test connection
psql -h localhost -U trading -d sswingtrade -c "SELECT 1"
```

### Redis Connection Error
```bash
# Check Redis
docker compose logs redis

# Test connection
redis-cli -h localhost ping
```

### Ollama Not Responding
```bash
# Check Ollama
docker compose logs ollama

# Pull model manually
docker exec sswingtrade-ollama ollama pull qwen:3b

# Verify model loaded
curl http://localhost:11434/api/tags
```

### API Not Starting
```bash
# Check logs
docker compose logs api

# Rebuild image
docker compose build --no-cache api

# Restart
docker compose restart api
```

### Ports Already in Use
```bash
# Mudar portas em docker-compose.yml:
# Ex: "8001:8000" (host:container)

# Ou kill existing processes:
lsof -i :8000  # Find process
kill -9 <PID>
```

---

## 🔒 Security Notes (FASE 1)

⚠️ **Development Only!**

- `SECRET_KEY` é placeholder - **mudar em produção**
- Database password em `.env` é hardcoded - usar secrets management
- CORS aberto para localhost - restringir em produção
- Sem autenticação JWT implementada (FASE 7+)

**Before FASE 8 (Real Trading):**
1. ✅ Implementar OAuth2/JWT
2. ✅ Usar secrets manager (Vault/AWS Secrets)
3. ✅ Restrictar CORS
4. ✅ Habilitar HTTPS
5. ✅ Rate limiting
6. ✅ Audit logging completo

---

## 📚 FASE 1 → FASE 2 Roadmap

### FASE 2: Market Data & Database
- [ ] Integração yfinance (fetch EOD data)
- [ ] Scheduler para atualização diária
- [ ] TimescaleDB for time-series
- [ ] Alembic migrations
- [ ] Backfill histórico (5 anos)

### FASE 3: Quantitative Module
- [ ] Indicadores técnicos (TA-Lib)
- [ ] Feature engineering
- [ ] SMA, EMA, RSI, MACD, ATR, Bollinger Bands

### FASE 4: Backtesting Engine
- [ ] Backtest framework
- [ ] Realistic slippage & fees
- [ ] Walk-forward analysis
- [ ] Metrics (Sharpe, Sortino, Drawdown)

### FASE 5: Machine Learning
- [ ] Model training pipeline
- [ ] XGBoost/LightGBM
- [ ] Probability scoring
- [ ] Model versioning & serialization

### FASE 6: LLM Agents (Ollama)
- [ ] News sentiment analysis
- [ ] Event detection
- [ ] Devil's advocate validation
- [ ] Batch processing

### FASE 7: Capital Management
- [ ] Dynamic position sizing
- [ ] Compound interest calculator
- [ ] Monthly deposit automation
- [ ] Circuit breaker logic

### FASE 8: Risk Engine & Paper Trading
- [ ] Risk rules validation
- [ ] Order simulation
- [ ] Complete audit trail
- [ ] P&L tracking

### FASE 9: Dashboard
- [ ] Web UI (React/Vue)
- [ ] Real-time P&L
- [ ] Position monitor
- [ ] Capital analytics

---

## 🤝 Contributing

Contribuições são bem-vindas! Por favor:

1. Fork o projeto
2. Crie uma branch para sua feature (`git checkout -b feature/AmazingFeature`)
3. Commit suas mudanças (`git commit -m 'Add some AmazingFeature'`)
4. Push para a branch (`git push origin feature/AmazingFeature`)
5. Abra um Pull Request

---

## 📞 Support

Para dúvidas ou problemas:

1. Verificar este README primeiro
2. Checar logs: `docker compose logs <service>`
3. Abrir issue no GitHub (quando disponível)

---

## 📄 License

MIT License - veja LICENSE file para detalhes.

---

## 🎯 Next Steps

1. **Execute** `docker compose up -d`
2. **Acesse** `http://localhost:8000/docs` para explorar APIs
3. **Leia** a arquitetura completa no ADR-001 acima
4. **Comece** a FASE 2: integração com yfinance

**Boa sorte! 🚀**

---

**SSWingTrade v0.1.0** | Última atualização: 2026-10-05
