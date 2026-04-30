# Sandbox setup — Binance Testnet

> One-time setup the operator runs before FASE 9 work begins. Sandbox
> uses virtual money, so the keys here are NOT financially sensitive
> in the same way as a production key — but treat them with the same
> hygiene anyway. They still grant order placement on a real Binance-
> operated endpoint.

## 1. Create a testnet account

Go to <https://testnet.binance.vision/> and sign in with GitHub. The
testnet seeds your account with virtual BTC, ETH, USDT, BNB and a few
others — enough to exercise every order type the bot will use.

## 2. Generate API keys

In the testnet dashboard:

1. Click **Generate HMAC_SHA256 Key**.
2. Label the key `mib-trader-testnet` (so it's distinguishable later).
3. Permissions: **Spot trading ON**, **Withdrawals OFF**.
4. Copy the API key + secret (the secret is shown only once).

If you have a production Binance account, it's a *different* domain
(`api.binance.com` vs `testnet.binance.vision`). The triple seatbelt
in `CCXTTrader` rejects any URL that doesn't contain `testnet` or
`sandbox`, so a production key + URL combination won't work in
FASE 9 by design.

## 3. Put the keys in `.env`

```
BINANCE_SANDBOX_API_KEY=<your testnet api key>
BINANCE_SANDBOX_SECRET=<your testnet secret>
BINANCE_SANDBOX_BASE_URL=https://testnet.binance.vision
```

`.env` is gitignored. Never commit it. `.env.example` (which IS
committed) carries placeholders so future operators see the names
without seeing the values.

## 4. Smoke-test the keys

```bash
PYTHONUTF8=1 uv run python scripts/smoke_sandbox_ping.py
```

Expected output: lists non-zero balances and the testnet hostname.
If the script raises an authentication error, the most likely cause
is a stray space at the end of the API key copied from Binance. Re-
generate or carefully retype the key.

## 5. Operational notes

- **Reset**: testnet wipes balances roughly monthly. If you log in
  and your seed funds are gone, that's expected — re-seed via the
  dashboard's "Faucet" feature.
- **Rate limits**: testnet rate limits are stricter than production
  in some endpoints. CCXT's `enableRateLimit` handles backoff
  automatically; the `RateLimiter` in `mib.sources.base` pre-paces
  reads.
- **Order book depth**: testnet liquidity is thin. Limit orders may
  not fill as quickly as on production. This is fine for the
  smoke-tests but means you can't infer real fill latency from
  testnet behaviour.
- **WebSocket fills (FASE 23)**: testnet supports the same WebSocket
  endpoints as production at slightly different URLs. Address that
  when 23 lands.

## 6. When FASE 14 LIVE arrives

Production credentials live in `.env` under different variable names
(to be defined in 14.x). The triple seatbelt's third gate stays in
place; relaxing it is a deliberate FASE 14 patch.
