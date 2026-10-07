# SSWingTrade — Guia de uso (FASES 1 a 9)

Tudo roda em **paper trading** (simulação). `ENABLE_REAL_TRADING=false` e não existe corretora real conectada.

## 1. Subir o sistema (Docker)
Pré-requisitos: **Docker Desktop aberto** (esperar o ícone ficar verde) e, para as notícias com IA, o **Ollama do seu PC ligado**
(ele roda fora do Docker; o sistema o acessa por `host.docker.internal`).

```bash
cd sswingtrade
cp .env.example .env          # só na 1ª vez; troque cada CHANGE_ME por um valor aleatório (letras e números)
docker compose up -d --build  # 1ª vez: 5–10 min e ~3–4 GB de disco
docker compose ps             # postgres, redis, api e grafana devem ficar "healthy"
```
- **Dashboard:** http://localhost:8000/dashboard · **Parâmetros:** http://localhost:8000/settings · **API/Swagger:** http://localhost:8000/docs
- **Grafana:** http://localhost:3000 — usuário `admin` e a senha `GRAFANA_ADMIN_PASSWORD` do seu `.env` → pasta "SSWingTrade"
- Tudo escuta só em `127.0.0.1` (esta máquina). A API ainda **não tem login**: não abra as portas para a rede.
- Ver o log: `docker compose logs -f api` · Parar (mantém os dados): `docker compose down` · **Apagar o banco:** `docker compose down -v`
- Conferir o Ollama: `curl http://localhost:8000/api/v1/news/llm/status` deve mostrar `"reachable": true` e `"model_installed": true`.
  O modelo é o `OLLAMA_MODEL` do `.env` (`qwen3:8b`, o que você já tem; veja os seus com `ollama list`). Se aparecer `reachable: false`,
  o Ollama está desligado ou o Docker não o alcança: ligue-o e, se persistir, defina a variável de usuário `OLLAMA_HOST=0.0.0.0` e reinicie o Ollama.
- Para as rotinas diárias (coleta 18:30, notícias 19:30, ciclo 20:00) rodarem, **o PC precisa estar ligado e o Docker Desktop aberto**;
  em Docker Desktop → Settings → General, ative "Start Docker Desktop when you sign in". Ligue o Ollama antes das 19:30.
- Pouco espaço em disco? Depois do primeiro build: `docker builder prune -f`.
- **Solução de problemas**
  - `no configuration file provided`: você está na pasta errada; o prompt deve terminar em `\sswingtrade>`.
  - Um container reinicia sem parar com `exec format error`: a imagem foi baixada corrompida (acontece com o disco quase cheio).
    `docker pull` não resolve ("Image is up to date" só compara o identificador). Faça `docker compose down`,
    `docker image rm <imagem>` e `docker pull <imagem>` de novo, depois `docker compose up -d`.
  - Para ver por que um container falha: `docker logs --tail 50 sswingtrade-<postgres|redis|api|grafana>`.
  - `/health` com `"status": "degraded"` e só o Ollama como `unhealthy` é normal quando o Ollama está desligado; o resto funciona.

Se você já tinha um banco criado em fases anteriores, recrie: `docker compose down -v` (apaga os dados).

### Ollama na nuvem (opcional)
Com uma chave do Ollama, os agentes de texto (pesquisa, advogado do diabo, eventos) usam um modelo grande na nuvem (`gemma4:31b`, plano gratuito)
e **caem sozinhos para o Ollama local** se a nuvem falhar, se a chave for recusada ou se a cota acabar (a nuvem fica em pausa por 5 min).
1. Crie a chave em https://ollama.com/settings/keys. **Copie o valor completo na hora**: depois a tela só mostra o identificador.
2. Cole-a no seu `.env`, na linha `OLLAMA_API_KEY=` (só ali: nunca em chat, código ou Git). Para trocar a chave, é só editar a linha.
3. Aplique: `docker compose up -d` e confira com `curl "http://localhost:8000/api/v1/news/llm/status?probe_cloud=true"`
   (gasta **uma** chamada mínima da cota; `"probe": {"ok": true}` = chave e modelo funcionando; `http_status: 401` = chave recusada).
- A chave só é enviada a `https://ollama.com`, nunca ao Ollama do seu PC, e nunca aparece em log nem em resposta da API.
- **O lote noturno de notícias (~40 chamadas por dia) fica no modelo local por padrão**, para não esgotar a cota gratuita
  (pelas suas contas, uma chamada de 120B custa ~0,13% do mês). Mudar em `/settings`: "Lote de notícias na nuvem do Ollama".
- Os prompts vão para os servidores do Ollama (eles dizem não usá-los para treino e não informam por quanto tempo guardam).
  O sistema envia só manchetes e indicadores públicos; seus saldos e posições nunca entram num prompt.
- A resposta de cada notícia guarda quem a produziu (`cloud:<modelo>` ou `local:<modelo>`).

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

---

## Integridade dos dados (correções da 2ª auditoria)

- **Dados do dia:** a coleta pede candles até *amanhã* (o `end` do yfinance é exclusivo), então o pregão que acabou de fechar chega no mesmo dia.
  Um candle de "hoje" antes das 18:10 (horário de Brasília) é parcial e é descartado.
- **Histórico ajustado:** o Yahoo reescreve todo o passado a cada dividendo/split. Se um dia re-baixado diferir do guardado (>0,2% e >1 centavo),
  o histórico completo do ativo é baixado de novo e os indicadores são recalculados. O job de sábado refaz o histórico de todos os ativos.
  Forçar na hora: `POST /api/v1/market/sync/daily {"days_back": 730, "full_resync": true}`.
- **Dados velhos:** nova regra do Risk Engine `fresh_data` (parâmetro `risk.max_data_age_bdays`, padrão 3 pregões, 0 = desligado) recusa entradas
  se o último candle estiver velho demais (coleta falhou). Aparece em `/trading/decisions`.
- **Backtest com aportes:** Sharpe, Sortino e drawdown agora usam retorno ponderado pelo tempo (aportes não contam como lucro nem escondem perdas);
  `twr_return_pct` é o retorno puro da estratégia.
- **Score de notícias:** uma manchete isolada de baixo impacto/confiança não leva mais o score a 0 ou 100 (encolhe para neutro com um peso prior de 0,25).

**Depois de atualizar um banco que já tinha dados:** rode o `full_resync` acima, depois `POST /api/v1/indicators/compute {"days": 400}` e retreine o ML
(`POST /api/v1/ml/train`). Resultados de paper trading e backtests anteriores foram gerados com dados atrasados/inconsistentes e com métricas distorcidas;
compare-os com cautela ou refaça.
