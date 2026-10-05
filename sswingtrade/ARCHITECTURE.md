# SSWingTrade - Arquitetura Técnica Completa

**Data:** 2026-10-05 | **Versão:** 0.1.0 - FASE 1 | **Status:** Foundation

---

## 📐 Visão Arquitetural Geral

```
┌─────────────────────────────────────────────────────────────────┐
│                      APRESENTAÇÃO (Grafana)                     │
│                    Dashboard + Monitoramento                     │
└──────────────┬────────────────────────────────┬──────────────────┘
               │                                │
      ┌────────▼────────┐            ┌──────────▼─────────┐
      │   REST API      │            │   Prometheus       │
      │   (FastAPI)     │            │   (Métricas)       │
      │  :8000          │            │   :9090            │
      └────────┬────────┘            └────────────────────┘
               │
      ┌────────▼───────────────────────────────┐
      │     CAMADA DE DOMÍNIO (Business Logic) │
      │  ┌─────────────────────────────────┐   │
      │  │  Market Data | Capital Mgmt     │   │
      │  │  Risk Engine | Execution        │   │
      │  │  Quant | ML | Agents           │   │
      │  └─────────────────────────────────┘   │
      └────────┬───────────────────────────────┘
               │
      ┌────────▼──────────┬────────────────┬──────────┐
      │                   │                │          │
   ┌──▼──┐         ┌──────▼──┐      ┌──────▼────┐   ┌─▼────┐
   │ PG  │         │ Redis   │      │  Ollama   │   │Logs  │
   │ 5432│         │ 6379    │      │  11434    │   │JSON  │
   └─────┘         └─────────┘      └───────────┘   └──────┘
```

---

## 🏗️ Stack Tecnológico

### Backend
- **Linguagem:** Python 3.11
- **Framework:** FastAPI 0.104
- **Async Runtime:** asyncio + uvicorn
- **ORM:** SQLAlchemy 2.0 (async)

### Dados
- **Database:** PostgreSQL 16 + TimescaleDB
- **Cache:** Redis 7 (sessões, rate limiting)
- **Logs:** JSON estruturado (loguru)

### ML/IA
- **LLM Local:** Ollama (Qwen 3B / Llama 3.2)
- **ML Models:** XGBoost, LightGBM, Scikit-Learn
- **TA:** TA-Lib, Pandas, NumPy

### Observabilidade
- **Metrics:** Prometheus
- **Visualization:** Grafana
- **Tracing:** OpenTelemetry (FASE 2+)
- **Logging:** JSON + File rotation

### Containerização
- **Runtime:** Docker + Docker Compose
- **Registry:** Local (build local)

---

## 🎯 Componentes de Negócio

### 1. Market Data Service
**Responsabilidade:** Obter e armazenar dados de mercado

```
yfinance → PostgreSQL (market_candles)
  ↓
Candles diárias (OHLCV)
  ↓
Indexação por (ticker, date)
```

**Endpoints:**
- `GET /api/v1/market/candles/{ticker}?start_date&end_date`
- `GET /api/v1/market/candles/{ticker}/latest`
- `POST /api/v1/market/sync/yfinance` (FASE 2)

---

### 2. Capital Management Service
**Responsabilidade:** Gestão dinâmica de capital com juros compostos

```
R$ 500/semana (base) + Lucros Anteriores + Aporte Mensal
        ↓
Position Sizing Dinâmico (R$ 100/dia escalável)
        ↓
Auditoria Completa (capital_history)
```

**Modelo de Capital:**
```
Semana 1: R$ 500,00
Semana 2: R$ 500,00 + Lucro_Semana1 (reinvestimento)
Semana 3: R$ 500,00 + Lucro_Acumulado
Mês 2:   R$ 500,00 + Lucro_Acumulado + MONTHLY_DEPOSIT
```

**Endpoints:**
- `POST /api/v1/capital/account` - Criar conta
- `GET /api/v1/capital/account/{id}` - Info da conta
- `POST /api/v1/capital/allocation/check` - Validar alocação
- `GET /api/v1/capital/account/{id}/history` - Auditoria

---

### 3. Quantitative Module
**Responsabilidade:** Indicadores técnicos e feature engineering

**Indicadores (FASE 3):**
- SMA, EMA (médias móveis)
- RSI, MACD (momentum)
- ATR (volatilidade)
- Bollinger Bands
- VWAP diária

**Features:**
```python
def calculate_technical_score(candles: List[Candle]) -> float:
    """
    Agrega indicadores técnicos em score 0-100
    Fórmula:
    - RSI > 70 (overbought) | RSI < 30 (oversold)
    - MACD crossover signal
    - Volume spike
    - ATR for position sizing
    """
```

---

### 4. Machine Learning Pipeline
**Responsabilidade:** Predição probabilística de retornos

**Modelo de Target (FASE 5):**
```
P(retorno >= 2% nos próximos 3 dias)  → 0-1
P(retorno >= 4% nos próximos 5 dias)  → 0-1
```

**Features:**
- Indicadores técnicos calculados
- Volume e volatilidade
- Padrões históricos
- Tendência de preço

**Modelo:**
- XGBoost ou LightGBM
- K-Fold cross-validation
- Hyperparameter tuning
- Model versioning (MLflow FASE 5+)

---

### 5. LLM Agents (Ollama)
**Responsabilidade:** Análise qualitativa de notícias e eventos

**Agentes (FASE 6):**

#### News Analyst
```json
Input: Título + Conteúdo de notícia
Output: {
  "sentiment": -1.0 to 1.0,
  "impact_score": 0-10,
  "confidence": 0-1,
  "horizon": "short_term_1_5_days",
  "bullish_factors": [...],
  "bearish_factors": [...]
}
```

#### Event Analyst
- Detecta datas de balanços, dividendos
- Estima se já foi precificado pelo mercado
- Event-driven trading signals

#### Devil's Advocate
- Tenta contradizer tese de compra/venda
- Busca riscos regulatórios
- Divergências técnicas

**Processing:**
```
Batch Processing (não real-time)
  ↓
Fechamento do mercado + Nova notícia
  ↓
Hash do conteúdo (evitar reprocessamento)
  ↓
LLM → JSON → PostgreSQL
```

---

### 6. Risk Engine
**Responsabilidade:** Validação inviolável de ordens

```
┌─────────────────────────────┐
│   Ordem Simulada/Real       │
└────────────┬────────────────┘
             │
      ┌──────▼────────────────┐
      │ Risk Engine Valida    │
      │  ✓ Saldo disponível   │
      │  ✓ Daily limit (100)  │
      │  ✓ Weekly limit (500) │
      │  ✓ Circuit breaker    │
      │  ✓ Stop loss / TP     │
      └──────┬────────────────┘
             │
      ┌──────▼────────────┐
      │  Aprovado? → PB   │
      │  Rejeitado? → Log │
      └───────────────────┘
```

**Regras (Invioláveis):**
1. MAX_DAILY_ALLOCATION: R$ 100/dia (escalável)
2. MAX_WEEKLY_CAPITAL: R$ 500/semana
3. STOP_LOSS: 1.5x ATR
4. TAKE_PROFIT: Ratio 1:2
5. MAX_DAILY_LOSS: 1.5% (circuit breaker)

---

### 7. Paper Broker (Simulação)
**Responsabilidade:** Simular execuções antes de real trading

```
Order → PaperBroker → Simulação
  ↓
- Entry price (close do dia)
- Slippage (estimado 0.1%)
- Fees B3 (0.08%)
- Stop loss / Take profit triggered?
  ↓
Registra: entrada, saída, P&L, score, modelo usado
  ↓
Auditoria 100% das decisões
```

---

### 8. Strategy Engine
**Responsabilidade:** Coordenar análises em teses de trading

```
Ticker X
  ├─ Quant Score (25%)
  │   └─ Technical indicators
  ├─ News Score (25%)
  │   └─ LLM sentiment + impact
  ├─ ML Probability (30%)
  │   └─ Forecast model output
  └─ Volume/Momentum (20%)
      └─ Volume spike detection
  
  ↓ (Pesos configuráveis)
  
  COMPOSITE SCORE (0-100)
  
  ↓
  
  Score > MIN_SCORE_TO_TRADE (60)?
    ├─ SIM → Passa para Risk Engine
    └─ NÃO → Rejeitado
```

---

## 📊 Banco de Dados - Schema

### Tabelas Principais

#### `market_candles` (Time-Series)
```sql
CREATE TABLE market_candles (
  id INTEGER PRIMARY KEY,
  ticker VARCHAR(10),
  date DATETIME,
  open_price NUMERIC(10,2),
  high_price NUMERIC(10,2),
  low_price NUMERIC(10,2),
  close_price NUMERIC(10,2),
  volume INTEGER,
  created_at DATETIME
);
-- Índices: (ticker, date), (date)
-- Particionado por date em TimescaleDB
```

#### `accounts`
```sql
-- Capital tracking com juros compostos
- id, user_id
- current_balance (total)
- available_balance (para trade)
- invested_capital (em posições)
- weekly_base_allocation, daily_limit_per_operation
- total_gains, total_losses
- daily_loss_triggered (circuit breaker)
```

#### `positions`
```sql
-- Posições abertas e fechadas
- id, account_id, ticker
- operation_type (buy/sell)
- quantity, entry_price, entry_date
- exit_price, exit_date
- stop_loss_price, take_profit_price, atr_value
- gross_pnl, fees, net_pnl, pnl_percent
- technical_score, news_score, ml_probability, composite_score
- status (open, closed, partial)
```

#### `capital_history`
```sql
-- Auditoria de movimentação de capital
- id, account_id
- event_type (deposit, trade_pnl, reinvestment, monthly_deposit)
- amount, balance_before, balance_after
- description, metadata (JSON)
- date
```

#### `orders`
```sql
-- Ordens individuais (pode ter múltiplas por posição)
- id, account_id, position_id
- ticker, operation_type, quantity
- target_price, executed_price, executed_quantity
- status (pending, filled, partial, cancelled)
- fees, execution_date
```

#### `news_events` (FASE 6+)
```sql
-- Notícias analisadas por LLM
- id, ticker, title, content, source, event_date
- sentiment_score, impact_score, confidence, horizon
- event_type, analysis_metadata (JSON)
- content_hash (evitar duplicatas)
```

#### `backtest_runs` (FASE 4+)
```sql
-- Resultados de backtesting
- id, name, strategy_id, start_date, end_date
- total_trades, winning_trades, losing_trades, win_rate
- initial_capital, final_capital, total_return, max_drawdown, sharpe_ratio
- parameters (JSON), results (JSON)
```

---

## 🔄 Fluxo de Execução

### Exemplo: Identificar Oportunidade de Swing Trade

```
1. DIARIAMENTE (Fim do dia)
   └─ Fetch candles para watchlist (yfinance)
   └─ Calcular indicadores técnicos
   └─ Gerar Technical Score

2. NOTÍCIA/EVENTO
   └─ Ingerir notícia de news API (FASE 6)
   └─ Enviar para News Analyst (Ollama)
   └─ Gerar News Score + Impact

3. LLM ANALYSIS (Batch)
   └─ Consolidar notícias diárias
   └─ Executar Devil's Advocate
   └─ Armazenar JSON + hash em DB

4. MACHINE LEARNING
   └─ Usar features técnicas + históricas
   └─ Predict P(High >= 2% em 3 dias)
   └─ Gerar ML Probability

5. SCORING
   └─ Composite = (Tech 25% + News 25% + ML 30% + Volume 20%)
   └─ Score >= 60? → Continuar
   └─ Score < 60?  → Descartar

6. RISK ENGINE
   └─ Capital disponível?
   └─ Daily limit ok?
   └─ Stop loss 1.5x ATR?
   └─ R:R ratio 1:2?
   └─ Circuit breaker?

7. APROVAÇÃO
   └─ SIM → Gerar ordem para Paper Broker
   └─ NÃO → Log razão da rejeição

8. PAPER TRADING
   └─ Simular entrada no close do dia seguinte
   └─ Monitorar stop loss / take profit
   └─ Registrar auditoria completa

9. CONSOLIDAÇÃO
   └─ Atualizar P&L
   └─ Calcular capital dinâmico
   └─ Rebalancear allocation da semana
```

---

## 🔐 Security & Governance

### FASE 1 (Atual)
- ✅ PostgreSQL credentials em .env (dev)
- ✅ Sem autenticação JWT
- ✅ CORS aberto para localhost
- ✅ Logging completo (auditoria)

### FASE 7+ (Antes de Real Trading)
- [ ] OAuth2 / JWT
- [ ] Secrets manager (Vault/AWS)
- [ ] CORS restrito
- [ ] HTTPS/TLS
- [ ] Rate limiting
- [ ] API key signing
- [ ] Audit log completo

---

## 📈 Observabilidade

### Logs
```json
{
  "timestamp": "2026-10-05T10:30:45Z",
  "level": "INFO",
  "logger": "app.services.capital",
  "event_type": "POSITION_OPENED",
  "account_id": 1,
  "ticker": "PETR4",
  "quantity": 100,
  "entry_price": 25.50,
  "technical_score": 75,
  "ml_probability": 0.68,
  "composite_score": 72
}
```

### Métricas (Prometheus)
```
sswingtrade_positions_open{status="open"} 5
sswingtrade_account_balance{account_id="1"} 625.50
sswingtrade_daily_loss_percent{account_id="1"} 0.8
sswingtrade_orders_total{status="filled"} 42
sswingtrade_api_request_duration_seconds_bucket{le="0.5"} 98
```

### Alerts (FASE 8+)
- Circuit breaker ativado
- Capital insuficiente
- LLM latência > 30s
- Database connection error
- Redis disconnect

---

## 🚀 Deployment & Operações

### Development (FASE 1-4)
```bash
docker compose up -d
```

### Staging (FASE 5-7)
```bash
docker compose -f docker-compose.prod.yml up -d
```

### Production (FASE 8+)
```bash
# Kubernetes ou Cloud Provider
# Environment variables + secrets manager
```

---

## 📊 KPIs & Métricas de Sucesso

### Técnicas
- ✅ API latency < 200ms (p99)
- ✅ Database query < 100ms (p99)
- ✅ Ollama inference < 5s (batch)
- ✅ Uptime > 99.5%

### Negócio (FASE 8+)
- Win rate > 55%
- Sharpe ratio > 1.0
- Max drawdown < 10%
- Monthly return > 2%

### Operacionais
- Zero failed orders (paper trading)
- 100% capital audit trail
- < 5 min from signal to execution
- All decisions logged

---

## 🎓 Learning & Future Evolution

### Mejoras Planejadas
1. **FASE 9:** Dashboard web + real-time
2. **FASE 10:** Integração com corretora real (ClearBroker/XP)
3. **FASE 11:** Multi-account management
4. **FASE 12:** Advanced portfolio optimization
5. **FASE 13:** Ensemble models (várias estratégias)

---

## 📝 Decisões Arquiteturais Registradas

Veja **ADR-001** acima para análise completa de trade-offs.

**Resumo:**
- ✅ Monolith modular (não microserviços)
- ✅ PostgreSQL (não MongoDB)
- ✅ Batch LLM processing (não real-time)
- ✅ Paper trading obrigatório (não real imediato)
- ✅ Local-first (não cloud-only)

---

## 🔗 Referências

- **FastAPI Docs:** https://fastapi.tiangolo.com/
- **SQLAlchemy Async:** https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html
- **PostgreSQL TimescaleDB:** https://docs.timescale.com/
- **Ollama Models:** https://ollama.ai/library
- **Prometheus:** https://prometheus.io/docs/

---

**SSWingTrade Architecture** | v0.1.0 | 2026-10-05
