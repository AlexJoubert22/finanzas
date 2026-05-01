# Dead-man heartbeat (FASE 13.7)

External monitor that pages the operator if MIB stops responding.

The bot exposes `GET /heartbeat?token=<HEARTBEAT_TOKEN>`. A
remote scraper hits it every 5 minutes; on three consecutive
failures (or any 503) the operator is paged. This is the **only**
external-facing surface — everything else stays loopback. If the
process wedges, the dead-man is what catches it.

This document covers the **manual operator setup** because none of
the steps below are automatable from inside the bot.

## 1. Generate the token

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Add to `.env`:

```
HEARTBEAT_TOKEN=<paste-here>
```

Restart the bot. Without a token the endpoint refuses every request
(returns 503 "dead-man disabled") so you don't get half-baked
monitoring.

## 2. Expose the endpoint via Cloudflare Tunnel

The FastAPI server binds to `127.0.0.1:8000` (spec §13). To let an
external scraper reach `/heartbeat` we tunnel through Cloudflare —
no port forwarding, no public IP exposure.

### Install the tunnel client (Linux/RPi/VPS)

```bash
sudo curl -L https://pkg.cloudflare.com/install-cloudflared.sh | sudo sh
cloudflared tunnel login        # browser auth flow
cloudflared tunnel create mib-dead-man
```

This emits a tunnel UUID and a credentials file under
`~/.cloudflared/<UUID>.json`.

### Configure the tunnel

`~/.cloudflared/config.yml`:

```yaml
tunnel: <UUID>
credentials-file: /home/<user>/.cloudflared/<UUID>.json

ingress:
  - hostname: mib-deadman.<your-domain>
    path: ^/heartbeat$           # ONLY heartbeat reaches the bot
    service: http://localhost:8000
  - service: http_status:404     # everything else 404
```

Critical: the `path` regex restricts the tunnel to the heartbeat
endpoint only. **Never expose `/`** — it would reveal `/docs` and
the rest of the API.

### Route DNS

```bash
cloudflared tunnel route dns mib-dead-man mib-deadman.<your-domain>
```

### Run the tunnel as a service

```bash
sudo cloudflared service install
sudo systemctl enable cloudflared
sudo systemctl start cloudflared
```

Test from a different machine:

```bash
curl -s 'https://mib-deadman.<your-domain>/heartbeat?token=<TOKEN>'
# expected: {"status":"ok","ts":"2026-..."}
```

## 3. GitHub Actions cron

Create a private repo (or a private workflow in an existing private
repo) — never commit the token to a public repo.

`.github/workflows/dead-man.yml`:

```yaml
name: MIB dead-man

on:
  schedule:
    - cron: "*/5 * * * *"       # every 5 minutes
  workflow_dispatch:

jobs:
  heartbeat:
    runs-on: ubuntu-latest
    steps:
      - name: Hit /heartbeat
        env:
          HB_URL: ${{ secrets.MIB_HB_URL }}        # https://...
          HB_TOKEN: ${{ secrets.MIB_HB_TOKEN }}
        run: |
          set -e
          for i in 1 2 3; do
            if curl -sfS --max-time 10 \
                "$HB_URL?token=$HB_TOKEN" \
                -o /tmp/hb.json; then
              cat /tmp/hb.json
              exit 0
            fi
            sleep 30
          done
          # Three failures in a row — page the operator.
          echo "MIB heartbeat FAILED 3x" >&2
          exit 1

      - name: Notify on failure
        if: failure()
        env:
          TG_BOT: ${{ secrets.TG_BOT_TOKEN }}
          TG_CHAT: ${{ secrets.TG_CHAT_ID }}
        run: |
          curl -s "https://api.telegram.org/bot$TG_BOT/sendMessage" \
            -d "chat_id=$TG_CHAT" \
            -d "text=🚨 MIB heartbeat FAILED — $(date -u). Investigate."
```

Add the secrets in *Repo Settings → Secrets and variables → Actions*:

- `MIB_HB_URL` — `https://mib-deadman.<your-domain>/heartbeat`
- `MIB_HB_TOKEN` — the token from step 1
- `TG_BOT_TOKEN` — Telegram bot token (a separate one for paging is
  fine)
- `TG_CHAT_ID` — operator's Telegram chat id

## 4. Verify end-to-end

1. Hit the URL from your laptop with curl — expect 200 + `status: ok`.
2. Use a wrong token — expect 401.
3. Stop the bot process; wait for the next cron run; expect a
   Telegram alert within 5–15 minutes.
4. Start the bot back up; expect the next cron run to succeed.

## 5. Threat-model notes

- **Token rotation**: rotate `HEARTBEAT_TOKEN` every 90 days. Update
  `.env`, restart bot, update the GitHub secret. Old tokens stop
  working immediately on restart.
- **Cloudflare account compromise**: the tunnel routes only
  `^/heartbeat$`; a compromised Cloudflare account leaks the token
  but cannot reach `/docs` or any other endpoint. Rotate the token
  and the Cloudflare account credentials together.
- **Rate-limit**: Cloudflare has its own rate limit; the bot itself
  has no rate-limiter on `/heartbeat`. If you ever expose this more
  broadly, add one.
- **Don't reuse `TELEGRAM_BOT_TOKEN`** for the paging bot — keep the
  paging bot scoped narrowly so a compromised paging path can't also
  send messages on behalf of the trader bot.

## 6. What "stalled" means

The endpoint returns 503 on:

- `last_tick_at` older than `heartbeat_scheduler_max_age_sec`
  (default 60s). The scheduler isn't running portfolio_sync /
  reconcile_job ticks.
- `last_reconcile_at` older than `heartbeat_reconcile_max_age_sec`
  (default 600s). The reconciler hasn't completed a successful run
  in over 10 minutes.

`reason` field names which check failed so the page tells the
operator where to look.
