# SSWingTrade — Guia de uso (FASES 1 a 9)

Tudo roda em **paper trading** (simulação). `ENABLE_REAL_TRADING=false` e não existe corretora real conectada.

## 1. Subir o sistema
```bash
docker compose up --build -d
docker exec sswingtrade-ollama ollama pull qwen2.5:3b     # só para os agentes de notícias (FASE 6)
```
- API/Swagger: http://localhost:8000/docs
- **Dashboard:** http://localhost:8000/dashboard
- Grafana: http://localhost:3000 (admin/admin) → pasta "SSWingTrade"

Se você já tinha um banco criado em fases anteriores, recrie: `docker compose down -v` (apaga os dados).

## 2. Primeiro uso (ordem)
1. **Dados históricos** — `POST /api/v1/market/sync/daily` com `{"days_back": 730}`
2. **Indicadores** — `POST /api/v1/indicators/compute` com `{"days": 400}`
3. **Treinar o ML** — `POST /api/v1/ml/train` (precisa de ~600 linhas rotuladas)
4. **Notícias (opcional)** — `POST /api/v1/news/batch`
5. **Backtest** — `POST /api/v1/backtest/run` (avalie as estratégias antes de confiar)
6. **Simular decisões sem criar ordens** — `POST /api/v1/trading/preview`
7. **Rodar o ciclo** — `POST /api/v1/trading/cycle` (ou deixe o agendador fazer)

## 3. Rotina automática (horário de Brasília, seg–sex)
| Hora | Job |
|---|---|
| 07:00 | aporte semanal (segunda), aporte mensal (1º dia útil), reset do disjuntor |
| 18:30 / 18:45 | candles diários / intraday |
| 19:00 | indicadores técnicos |
| 19:30 | notícias + análise LLM em lote |
| 20:00 | **ciclo de trading**: executa ordens pendentes na abertura seguinte, gerencia stops/alvos, cria novas ordens aprovadas pelo Risk Engine |

## 4. Regras que o Risk Engine aplica (toda ordem)
score composto ≥ 60 · stop = 1,5×ATR · relação risco/retorno ≥ 1:2 · orçamento diário = 20% do patrimônio
(R$100 de R$500, escala com os juros compostos) · exposição ≤ patrimônio · risco por operação ≤ 2% ·
máx. 5 posições / 5 entradas por dia · sem duplicar ativo · disjuntor com perda diária ≥ 1,5% (travado até o dia seguinte) ·
veto do Advogado do Diabo (score ≥ 80). Sem aprovação assinada, o broker recusa a ordem.

Score composto: técnico 25% + notícias 25% + ML 30% + volume/momentum 20% (componente ausente = neutro 50, registrado em `missing`).

## 5. Auditoria
`GET /api/v1/trading/decisions` mostra **todas** as decisões (aprovadas ou não), com os scores e cada regra aprovada/reprovada.

## 6. Interpretação do capital (confirme se é o que você quer)
- Patrimônio de trabalho da semana = capital anterior + lucro acumulado + **R$500 novos** (`WEEKLY_DEPOSIT`, desligue com `WEEKLY_DEPOSIT_ENABLED=false`).
- O mês recebe ainda `MONTHLY_DEPOSIT` no 1º dia útil (`MONTHLY_DEPOSIT_ENABLED`).
- A semana em que a conta é criada conta o capital inicial como aporte (sem duplicar).

## 7. Limitações conhecidas
- Dados diários (EOD) do yfinance: execução simulada na abertura do dia seguinte; sem book real, sem leilão.
- IR (15% swing) e feriados da B3 não são modelados.
- O disjuntor considera só prejuízo **realizado**.
- O Advogado do Diabo entra no Risk Engine apenas como nota pré-calculada (`advocate_scores`), nunca como chamada ao vivo.
- Resultados de backtest/paper não garantem resultado real.

---

## FASE 10 — Tudo parametrizável, limites de perda e provisão de ganhos

**Parâmetros em tempo de execução** (sem redeploy): tela `http://localhost:8000/settings` ou `GET/PUT /api/v1/settings`.
Cobre capital (inicial, aporte semanal/mensal, % por operação, exposição, entradas/dia, posições), risco (stop ATR, R/R, risco por trade,
score mínimo, veto do advogado do diabo), **limites de perda diário/semanal/mensal** (em % do patrimônio e/ou R$ fixo; 0 = desligado; vale o menor),
pesos do score, custos, universo de ativos, notícias, horários do agendador e premissas da provisão. Valores do `.env` são só o padrão inicial.
`ENABLE_REAL_TRADING` continua somente por variável de ambiente.

**Aportes variáveis:** `POST /api/v1/capital/planned-deposits {"due_date":"2026-11-03","amount":237.90,"note":"13º"}` (ou pela tela /settings).
Qualquer valor, qualquer data; aplicado uma única vez pelo job das 07:00 (ou na hora, se a data já chegou).

**Perfis de agressividade:** conservador / moderado / agressivo / muito agressivo (`POST /api/v1/settings/profile`). Cada perfil ajusta de uma vez
% por operação, risco por trade, stop, score mínimo, trades/semana e limites de perda; depois você pode refinar item a item (o perfil vira "personalizado").

**Limites de perda:** o bloqueio é *travado* até o fim do período (dia/semana/mês) e o tamanho de cada ordem é reduzido para que o stop caiba
no que ainda resta do limite. `GET /api/v1/capital/loss-limits`; `POST /api/v1/trading/circuit-breaker/reset?period=day|week|month` (override manual do operador: a perda já realizada naquele período
deixa de contar e abre uma nova franquia de perda de um limite inteiro; o evento fica registrado no histórico de capital como `breaker_override`;
períodos ainda bloqueados continuam bloqueados, então um dia travado por perda semanal precisa do override `week` também).
O reset automático das 07:00 só limpa um bloqueio antigo: uma perda ainda acima do limite trava de novo, a rotina nunca libera folga sozinha.

**Provisão de ganhos:** `/forecast` ou `POST /api/v1/forecast` (cenários pessimista/base/otimista + simulação Monte Carlo P10–P90, prob. de prejuízo,
drawdown e de bater limites) e `GET /api/v1/forecast/compare` (todos os perfis lado a lado). A chance de acerto vem das evidências da própria IA
(AUC e probabilidades do modelo de ML, sentimento das notícias, histórico do paper trading e backtests). Sem modelo validado, a provisão assume
vantagem zero e mostra valor esperado levemente NEGATIVO (custos). É uma estimativa hipotética — **não é garantia de resultado**.

**Atualização de banco:** há tabelas novas (`app_settings`, `planned_deposits`, `loss_limit_events`). Em ambiente de teste: `docker compose down -v && docker compose up --build`.

Limitações: períodos em UTC; limites contam só P&L realizado; IR e feriados não modelados; a provisão ignora saídas por tempo e correlação entre ativos.
