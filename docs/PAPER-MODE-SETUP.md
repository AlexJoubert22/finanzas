# Activación PAPER mode — Guía operador

Guía mínima para arrancar la ventana de validación de 30 días en PAPER
con capital virtual de 6000 USDT en Binance Testnet. NO toca dinero
real. NO requiere `/go_live`. PAPER es la antesala antes de poder
escalar a SEMI_AUTO.

## Requisitos previos

1. **Binance Testnet keys** en `.env`:
   ```
   BINANCE_SANDBOX_API_KEY=...
   BINANCE_SANDBOX_SECRET=...
   ```
   Conseguir keys en [testnet.binance.vision](https://testnet.binance.vision).
   Permisos: trade=ON, withdraw=OFF (en testnet no aplica, pero
   mantén el hábito).

2. **TELEGRAM_BOT_TOKEN** configurado y bot añadido a tu chat
   privado:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
   TELEGRAM_ALLOWED_USERS=<tu_user_id>
   OPERATOR_TELEGRAM_ID=<tu_user_id>
   ```
   `OPERATOR_TELEGRAM_ID` es donde el scheduler envía signals
   automáticas y el daily report. Sin este valor el scheduler ejecuta
   pero no manda mensajes (modo headless).

3. **Capital virtual** disponible en testnet. Verifica saldo en
   testnet.binance.vision/account. La baseline `6000 USDT` se aplica
   automáticamente si el balance reportado es menor — los reseteos
   periódicos de testnet no rompen el cálculo de PnL/%.

4. **`config/scanner_universe.yaml`** parseado al boot. El bot fallará
   con error explícito si el YAML está ausente o mal formado.

## Pasos de activación

1. Verifica que el bot arranca:
   ```
   docker compose up -d        # o uv run uvicorn mib.api.app:create_app
   docker compose logs -f mib  # busca "scheduler: registered scanner jobs"
   ```

2. Verifica conexión testnet desde Telegram:
   ```
   /preflight
   ```
   Espera ✅ en `api_keys`, `trading_state`, `risk_gates`,
   `scheduler`, `reconcile_clean`, `dead_man`. Otros checks
   (`days_clean_streak`, `paper_validation`) NO se requieren para
   entrar a PAPER — son sólo para LIVE.

3. Verifica modo actual:
   ```
   /mode_status
   ```
   Debería estar en `SHADOW` o `OFF`.

4. Activa PAPER. Como sandbox no requiere validación temporal previa,
   se entra con `/mode_force`:
   ```
   /mode_force PAPER "Activación PAPER inicial para 30d validación con 6000 USDT virtual"
   ```
   El reason de `/mode_force` exige ≥20 chars y se rate-limita a
   1/semana por actor — usa la frase con cuidado.

5. Verifica activación:
   ```
   /paper_status
   ```
   Espera:
   - `🎮 /paper_status` (no el banner ⚠️)
   - `capital baseline: 6000 USDT`
   - `días en PAPER: 0` (recién activado)
   - `próximo modo: 🔒 SEMI_AUTO bloqueado: faltan 30d y 50 trades`

## Qué esperar primeros días

- **Día 1-2**: 0-3 signals/día probable. El motor está aprendiendo el
  régimen actual del universo. Verifica que las cards aparecen en tu
  chat con botones `✅ Aprobar` / `❌ Descartar` / `📊 Chart`.
- **Día 3-7**: 5-15 signals/día. Aprueba/descarta manualmente para
  coger ritmo. Cada decisión queda en `signal_status_events`.
- **Día 7+**: si confías el flujo, puedes mantener SEMI_AUTO con
  approval manual o transitar a aprobación automática (no
  recomendado en validation window).

## Daily report

Cada mañana ~08:00 Madrid (06:00 UTC) recibes:
- Header `🎮 PAPER MODE — Capital virtual baseline: 6000 USDT`
- PnL D-1 absoluto y porcentaje sobre baseline
- Trades W/L/BE + win-rate
- PnL 7d (rolling)
- Drawdown vs baseline si equity actual está por debajo
- Posiciones abiertas, días limpios, modo, incidentes 24h

## Comparativos semanales

A partir del día 7, el postmortem nocturno (02:00 UTC) incluye
comparativos current_week vs previous_week: aggregate PnL, win rate,
avg R-multiple. El LLM busca tendencias semana-a-semana, no sólo del
día.

## Si algo va mal

- `/panic`: cancela todas las órdenes pendientes, cierra todas las
  posiciones a mercado y bloquea trading 7 días.
- `/freeze` o `/wind_down "razón ≥20 chars"`: bloquea nuevas entradas
  pero deja las posiciones abiertas con sus stops nativos.
- `/mode_force OFF "razón ≥20 chars"`: vuelve a OFF (gasta tu cupo
  semanal de override; piénsalo).

## Cuándo escalar a SEMI_AUTO

`/paper_status` muestra el gate. Requisitos:
- `días_in_paper ≥ 30`
- `closed_trades ≥ 50`

Cuando `✅ SEMI_AUTO desbloqueado` aparezca, el operador decide. No es
automático.

NUNCA `/go_live` desde PAPER directamente — el path crítico exige
SEMI_AUTO entre medio y los 60 días limpios + preflight 100% verde
antes de tocar capital real.
