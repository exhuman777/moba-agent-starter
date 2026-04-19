```
 ███╗   ███╗ ██████╗ ██████╗  █████╗
 ████╗ ████║██╔═══██╗██╔══██╗██╔══██╗
 ██╔████╔██║██║   ██║██████╔╝███████║
 ██║╚██╔╝██║██║   ██║██╔══██╗██╔══██║
 ██║ ╚═╝ ██║╚██████╔╝██████╔╝██║  ██║
 ╚═╝     ╚═╝ ╚═════╝ ╚═════╝ ╚═╝  ╚═╝
     A G E N T   S T A R T E R
```

Build your first AI bot fleet for **[defenseoftheagents.com](https://www.defenseoftheagents.com)**.

Open-source starter kit — view-only TUI dashboard + headless WebSocket controller + a strategy that actually scores kills. No ML, no game-theory PhD, just clean if/else rules tuned against the live game.

> **Status:** v28 (2026-04-19). Validated +10pp win rate over the previous strategy in a 10-game A/B test. Full strategy spec in [`STRATEGY.md`](STRATEGY.md).

## Quick start

```bash
git clone https://github.com/exhuman777/moba-agent-starter.git
cd moba-agent-starter
pip install -r requirements.txt

# 1. Edit register.py — change NEW_BOTS to your own names
# 2. Optional: drop your wallet PK in wallet.key (chmod 600), or `export WALLET_PK=0x...`
python3 register.py             # registers names, connects wallet, writes fleet.json

# 3. Launch — dashboard auto-spawns the WebSocket controller in the background
python3 dashboard.py
```

Press `q` in the dashboard to quit (cleans up the controller too).

## Architecture

```
┌────────────────────────┐
│  dashboard.py          │  Rich TUI (4 views) — VIEW ONLY, never POSTs
│  - polls game state    │
│  - tails ws_runner.log │  spawns + auto-respawns ↓
└──────────┬─────────────┘
           │ subprocess
           ▼
┌────────────────────────┐
│  ws_runner.py          │  Headless controller. The only writer of stats.json.
│  - 1 WebSocket per     │
│    unique gameId       │  ─── HTTP + WSS ──→  defenseoftheagents.com API
│  - per-bot deploy      │                       /api/strategy/deployment
│  - hot-reloads         │                       /api/game/state
│    fleet.json          │                       /api/wallet/connect
└────────────────────────┘                       wss://…/ws?game=N
```

Two processes by design — split avoids the "TUI crash kills bots" failure mode.

## What's in here

```
dashboard.py        view-only Rich TUI; spawns + supervises ws_runner
ws_runner.py        headless controller, 20Hz WebSocket, multi-game aware
experiment.py       lane rotation + per-bot snapshot helpers (pure)
wallet.py           eth_account signing for /api/wallet/connect
connect_wallet.py   CLI: re-connect wallets ad hoc
register.py         CLI: register agents, optional wallet bind, write fleet.json
quant.py            game theory / EV math (used by dashboard quant view)
fleet.json          empty starter (populated by register.py)
fleet.example.json  reference template showing the shape
tests/              54 unit tests (rotation, wallet, deploy gates, schema,
                    multi-game dispatch, hot-reload, RunnerProcess lifecycle,
                    kill estimator, adaptive recall, tower-dive guard)
STRATEGY.md         full strategy spec — ability prio, recall, tower-dive,
                    kill estimator, multi-game WS pattern, debug table
```

## The strategy in 5 bullets

(Full version: [`STRATEGY.md`](STRATEGY.md).)

1. **`fireball` first, `tornado` second.** Mage AoE leaks kills to allies. Single-target burst gets you the kill credit.
2. **`sigma` lane style** — field-aware switching (carry rotates to outnumbered lane, snipe weak towers, retreat from dead-tower lanes). Beats passive farming by ~3 levels per game.
3. **Adaptive recall.** Don't recall just because an enemy is in lane. Trigger only on actual burst damage (>30% HP lost in 2s) or critical (<15% HP). Old "panic recall" cost 4 levels of XP per game.
4. **Tower-dive guard.** Don't chase under enemy tower with HP < 70%. Skip the command tick, let the bot drift back.
5. **Kill estimator** — XP-jump detection in [80, 500). Catches low-level hero kills that the old 180-XP threshold missed.

## Multi-game gotcha

The server can split your fleet across `gameId` slots — three bots deployed at the same tick may land in different games. `ws_runner.py` opens one WebSocket per unique `gameId` (one thread each) and dispatches state to bots that match. See `STRATEGY.md` § "Multi-game WebSocket handling" for the pattern.

## Wallet integration

```python
# wallet.py uses eth_account to sign offline.
# PK never leaves your machine — only signature + address + timestamp hit the API.

# Two ways to provide the key:
#   1. environment:  export WALLET_PK=0x...
#   2. file:         echo "0x..." > wallet.key && chmod 600 wallet.key
```

The wallet endpoint returns:
- `tokenHolder: true` — DOTA holder buff (+10% HP/dmg) applies to every bot bound to this wallet, automatically.
- `pixagreenMage: true` (and other NFTs) — owned skins. Reported per-bot, but the server visually equips the skin on **only one bot** at a time. Owning more NFTs lets more bots wear skins.

## Testing

```bash
python3 -m unittest tests.test_experiment -v
```

54 tests. CI-ready.

## Operational notes

- `q` in the dashboard sends SIGTERM to the controller, then SIGKILL after timeout. Always prefer `q` over `Ctrl-C` or `pkill`.
- If `ws_runner` dies for any reason, the dashboard auto-respawns it on the next poll tick. The runner-log panel shows `ws_runner respawned (#N)`.
- Deploy retry throttle: 5s between failed attempts per bot — won't spam the server.
- `/api/wallet/connect` is idempotent. Re-runs at startup and during fleet hot-reload (only when a bot's `skin` field changed).

## Stack

```
Python 3.9+
requests, websocket-client, eth-account, rich
```

No ML deps. Pure if/else rules. Reaction speed (20Hz WebSocket) > strategy depth in this game.

## License

MIT. Have at it.

## Credits

Reverse-engineered + tuned against the live game over 250+ recorded matches. The `lmeow` reference style is a tribute to a competitor bot whose if/else discipline beat several ML-based attempts.
