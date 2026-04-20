# Defense-of-the-Agents bot strategy — portable spec

Hand this to another Claude on another machine to reproduce the setup. No paths, no keys, no host-specific bits — pure strategy + architecture.

## Game context

[defenseoftheagents.com](https://www.defenseoftheagents.com) — API-driven MOBA. Two factions (human, orc), 3 lanes (top/mid/bot), heroes + creep waves + towers + bases. Bots register via `/api/agents/register`, deploy via `/api/strategy/deployment`. Game state available via REST `/api/game/state?game=N` and WebSocket `wss://…/ws?game=N` at 20Hz. Wallet integration: `/api/wallet/connect` accepts ETH-signature, returns `tokenHolder` (DOTA holder buff) and per-NFT ownership flags.

Server quirks worth knowing:
- **gameId is assigned per deploy, not per fleet.** Three bots deployed at the same moment may land in different gameIds. Each game runs its own state stream.
- **DOTA holder buff** (`tokenHolder: true`) is account-level — applies to every bot connected to the wallet.
- **Skin NFTs** (e.g. `pixagreenMage`) are 1:1. Wallet endpoint reports `pixagreenMage: true` for every bot bound to that wallet, but the server visually equips the skin on only ONE bot at a time. Don't flag this as a bug.
- **Class is locked at first deploy.** Once a bot deploys with `heroClass: mage`, its class is permanent for that bot.

## Two-process architecture

**Controller** (headless, WebSocket-driven, the only writer of stats):
- Loads fleet config (3+ bots).
- Connects each bot's wallet at startup (`/api/wallet/connect`).
- Pre-deploys each bot. Captures returned `gameId` per bot.
- Opens one WebSocket per unique `gameId` (in its own thread).
- Per WS tick (~20Hz), runs `bot.process(state)` for bots assigned to that gameId.
- Writes `stats.json` on game-over.

**Dashboard** (TUI, view-only):
- Spawns the controller as a subprocess (`subprocess.Popen` with `start_new_session=True`).
- Polls `/api/game/state` for display.
- Tails the controller's log for a live panel.
- Auto-respawns the controller if it dies (poll-tick check on `proc.poll()`).
- On user quit, sends SIGTERM to the controller's process group, then SIGKILL after timeout.
- **Never POSTs strategy.** Read-only by design.

Why split: a single-process TUI that also controls bots ends up with two failure modes (display crash kills bots, bot logic blocks display). Splitting gives crash isolation + a clean "view-only" boundary.

## Bot strategy ("sigma + fireball-first" — the v2 that works)

### Ability priority (kill-focused)

```
fireball > tornado > fortitude > fury > raise_skeleton
```

- **fireball**: single-target burst. Kill execution tool. Pick first.
- **tornado**: AoE damage-over-time. Pressure tool. Pick second.
- **fortitude**: HP buff. Defensive layer.
- **fury**: attack speed. Late-game scaling.
- **raise_skeleton**: situational tank.

The trap: prioritizing `tornado` first looks great on damage charts but feeds kills to allies (mage softens, melee scoops). For kill credit you want `fireball` first.

### Lane strategy: "sigma" (field-aware)

Every ~90 seconds (no shove-spam), evaluate three rules:

1. **Carry rotation.** If our hero is the highest level on team AND another lane is outnumbered (enemy_count − ally_count ≥ 2), rotate there.
2. **Tower snipe.** If an enemy tower is < 150 HP and not in our current lane, rotate to finish it.
3. **Tower retreat.** If our tower in current lane is dead, rotate to the lane that still has a friendly tower (XP-safe farming).

Avoid rule 1 plus rule 2 firing at the same time — rule 1 takes precedence.

For the `rotate_lane` flag (one bot only): on game-end, set their default lane to `LANES[game_idx % 3]` (top→mid→bot round-robin across games). Gives lane-coverage data without splitting bot identities.

### Adaptive recall (the critical fix)

**Old (broken):** `recall_threshold = 0.25 + enemies_in_lane * 0.08`. Means at 33% HP with one enemy in lane, recall fires. Bots panic-recall on enemy *presence* and lose XP/farm. Result: bots stay 4 levels behind opponents permanently.

**New:** track HP history (deque, last ~50 ticks = 2.5s). Recall fires only when:
- `hp_pct < 15%` (critical HP — recall regardless), OR
- Lost > 30% of max HP in the last 2 seconds AND now < 55% (burst damage detected — taking heavy fire).

Don't recall just because someone is nearby. Stay and farm.

### Kill estimator (XP-jump detection)

Server doesn't expose K/D in the WS unit array. Detect via XP delta:

```python
xp_total = sum(200 * i for i in range(1, level)) + xp_in_current_level
xp_delta = xp_total - prev_xp_total
if 80 <= xp_delta < 500:
    self.kills += 1   # hero kill or assist (low-level kills give ~100-150 XP)
```

Threshold band tuned for low-level kills (the previous threshold of 180 missed every L1-2 kill). The < 500 upper bound prevents level-up double-counts from inflating.

### Tower-dive guard

Mage bots love chasing low-HP enemies under enemy towers and dying. Hard rule:

```python
if enemy_tower_alive_in_my_lane and enemies_near and hp_pct < 0.70:
    return  # skip command tick — bot drifts back naturally
```

The "drift back naturally" works because absent commands, the bot follows the last `heroLane` to its own side.

### Death rotation

On respawn, count enemies per lane. Pick the lane with 0–2 enemies (skip lanes with 3+ enemies — fight you'd lose). 30-second cooldown on death-rotation lane changes (don't oscillate).

### Endgame (last 60s before sudden death)

All bots converge on the lane with most living allies. Group-up beats split-push when bases are ticking down.

### Throttling

- **Command throttle:** max 1 `/api/strategy/deployment` POST per second per bot. Burst commands cause API rate-limit and confused server state.
- **Recall and ability picks bypass the throttle** (instant reactions matter).
- **Deploy retry throttle:** if a deploy POST fails, wait 5 seconds before retry. Prevents spam loops on server hiccups.

## Multi-game WebSocket handling

Required because the server may split your fleet across game slots:

```
class WSBot:
    self.game_id: int | None  # set from deploy response

class Controller:
    self._ws_by_game: dict[int, WebSocketApp] = {}
    self._threads_by_game: dict[int, Thread] = {}

    def _start_ws_for_game(self, gid):
        if gid in self._ws_by_game:
            return
        ws = WebSocketApp(f"{WS_URL}?game={gid}",
                          on_message=lambda ws, msg: self._on_msg(gid, msg),
                          ...)
        Thread(target=ws.run_forever, daemon=True).start()
        self._ws_by_game[gid] = ws

    def _on_msg(self, gid, msg):
        bots_in_game = [b for b in self.bots
                        if b.game_id == gid
                        or (b.game_id is None and gid == self.default_game)]
        for bot in bots_in_game:
            bot.process(parse_state(msg))
            if bot.game_id and bot.game_id not in self._ws_by_game:
                self._start_ws_for_game(bot.game_id)

    def run(self):
        self._start_ws_for_game(self.default_game)
        while not stopped:
            time.sleep(5)
            for b in self.bots:
                if b.game_id and b.game_id not in self._ws_by_game:
                    self._start_ws_for_game(b.game_id)
```

The orphan filter `b.game_id is None and gid == default_game` is critical. After a game ends, you reset `bot.game_id = None` so the next deploy fires. Without that filter, the bot's `process()` is never called in any WS stream → it sits forever in "wait" state.

## Hot-reload (no restart for config tweaks)

In `_record_game` (right after stats write), call `_reload_fleet_config`:

```python
def _reload_fleet_config(self):
    fleet = json.load(open(FLEET_FILE))
    new_cfgs = {b["name"]: b for b in fleet["bots"]}
    needs_wallet_recheck = any(
        cfg.get("skin") != bot.skin and cfg.get("wallet")
        for bot in self.bots
        for cfg in [new_cfgs.get(bot.name)] if cfg
    )
    if needs_wallet_recheck:
        wallet.auto_connect(fleet["bots"])  # re-validates skin NFT
    for bot in self.bots:
        cfg = new_cfgs.get(bot.name)
        if not cfg: continue
        bot.skin = cfg.get("skin")
        bot.style = cfg.get("style", bot.style)
        bot.ability_prio = cfg.get("ability_prio", bot.ability_prio)
        bot.default_lane = cfg.get("lane", bot.default_lane)
        ...
```

Match by name. Adding/removing bots requires full restart (bot list rebuild); tweaking existing bots' skin/style/lane/abilities applies on the next game without restart.

## Stats schema (one writer, multiple readers)

```jsonc
{
  "time": "ISO8601",
  "winner": "human" | "orc",
  "majority_faction": "human" | "orc",   // faction holding more of OUR bots
  "majority_won": bool,                   // did our majority faction win?
  "human_bots": int, "orc_bots": int,
  "tick": int, "game_time": float,        // seconds
  "human_max_level": int, "orc_max_level": int,
  "human_base_hp": int, "orc_base_hp": int,
  "game_idx": int,                        // monotonic across all games
  "gameId": int,                          // server slot
  "bots": [
    {"name": str, "class": str, "style": str, "lane": str,
     "skin": str | null, "wallet_holder": bool, "faction": str,
     "kills_est": int, "deaths": int, "won": bool,
     "level": int, "hp": int, "maxHp": int, "xp": int,
     "abilities": [{"id": str, "level": int}]}
  ]
}
```

**Strict rule: only the controller writes stats.json. Ever.** A second writer (e.g. a TUI that also calls `record()` on game-end) silently corrupts winrate math because each writer fills different optional fields. Make the dashboard read-only.

## Test coverage worth building

Pin these or you will regress them:

1. **Lane rotation is round-robin** (`rotate_lane(N) == LANES[N % 3]`).
2. **Wallet-required + wallet_ok=False blocks deploy.**
3. **Skin field included in deploy payload only when wallet owns the NFT.**
4. **Shove-micro prevention:** `heroLane` not re-sent when bot is already in that lane.
5. **Stats schema:** every record has `majority_won` + `bots[]` of correct length + `gameId`.
6. **Multi-game dispatch:** orphan bots (game_id=None) routed only to the default game's WS.
7. **Adaptive recall:** does NOT fire on mere enemy presence; DOES fire on burst (>30%/2s) and critical (<15%).
8. **Kill estimator:** counts XP jumps in [80, 500), ignores creep XP and level-up double-counts.
9. **Tower-dive guard:** blocks command tick when chasing under enemy tower at <70% HP.
10. **Subprocess lifecycle:** SIGTERM → SIGKILL escalation, idempotent stop, auto-respawn unless user-initiated stop.

## Common failure modes (debug log)

| Symptom | Likely cause |
|---|---|
| Bot stays in "wait" forever | Server assigned it to a non-default gameId. Check deploy response, ensure multi-game WS code is in place. |
| All games count as losses | Stats schema mismatch — a second writer is appending entries without `majority_won`. Find and remove. |
| Win rate steady ~30% | Bots aren't scoring kills. Check ability_prio (fireball first?) + recall threshold (too aggressive?). |
| Deploy spams 20 times/sec | Missing retry throttle. Add `_last_deploy_try` timestamp + 5s gate. |
| `class: melee` shows in roster despite mage config | Don't trust the WS unit-type heuristic. Use `/api/game/state`'s explicit `class` field. |

## Minimal fleet config

```json
{
  "game": 3,
  "bots": [
    {"name": "Bot_A", "class": "mage", "lane": "top", "style": "sigma",
     "ability_prio": ["fireball","tornado","fortitude","fury","raise_skeleton"],
     "wallet": true, "skin": "pixagreen_mage", "key": "<api_key>"},
    {"name": "Bot_B", "class": "mage", "lane": "mid", "style": "sigma",
     "ability_prio": ["fireball","tornado","fortitude","fury","raise_skeleton"],
     "wallet": true, "skin": "pixagreen_mage", "key": "<api_key>"},
    {"name": "Bot_C", "class": "mage", "lane": "bot", "style": "sigma",
     "ability_prio": ["fireball","tornado","fortitude","fury","raise_skeleton"],
     "wallet": true, "skin": "pixagreen_mage", "rotate_lane": true,
     "key": "<api_key>"}
  ]
}
```

Keep `wallet.key` (PK file) at 0600 perms, gitignored.

## Stack

```
Python 3.9+
requests           # HTTP
websocket-client   # WS at 20Hz
eth-account        # wallet signature
rich               # TUI rendering (dashboard only)
```

No ML deps. Pure if/else strategy. The competitor `lmeow` bot we reverse-engineered is also pure if/else — that's not a coincidence; reaction speed and clean rules beat shallow ML in this game.

## Game meta (worth knowing before tuning)

These come from patch notes + community Discord, not the API. They directly affect strategy decisions.

### AI Ranked ladder — Game 3 (Patch 1.13)

**Game 3 is the dedicated AI bot ladder.** Bots have their own MMR + leaderboard. Top-ranked bots earn $DOTA at end of each season. Currently preseason (season 0), transitioning to season 1.

Implication: every game your fleet plays in slot 3 counts toward MMR. Strategy improvements directly translate to $DOTA rewards. Pick game 3 as default.

### MMR rules (Patch 1.14.1)

- Games with <6 players now count for HALF MMR (was: zero MMR).
- Most fleet matches have 10-20 heroes total → full MMR.

### New ping types (Patch 1.14.1)

- **Thumbs up** — social signal, probably cosmetic.
- **Recall** — broadcasts your current recall CD to allies. Useful for coordinating disengages.

Worth wiring into bots: emit a Recall ping when self-recalling so allies don't dive without their carry.

### WebSocket action submission (community tip — Arcanum)

The WebSocket isn't just for receiving state. Per Arcanum (Discord 2026-04-19): you can SUBMIT actions via the WS too. Most bots HTTP-POST every command via `/api/strategy/deployment` — these are subject to rate limits and add 50-200ms of latency per action. WS submission likely bypasses both.

Implementation path: open the game in a browser, dive into the network tab, find the WS message format for actions. Mirror it from the controller. Untested in this codebase as of v28.

### Build hint — "one of each gud ability" (AzFlin, Twitter 2026-04-19)

Conventional build wisdom: spam one ability to L3 for max damage. AzFlin's contrarian build at L12: **L1 of every ability** (fortitude + fireball + tornado + raise_skeleton). Pairs with Ring of Regen NFT + $DOTA holder buff.

Counter-intuitive but defensible — at L1 each ability gives a new tool; L2/L3 only give +25-35% effectiveness on a tool you already have. Breadth > depth for sustained-fight characters.

Implementation:

```python
have_ids = {a["id"] for a in hero.get("abilities", [])}
choices = hero.get("abilityChoices", [])
pick = None
# Phase 1: prefer a NEW ability over upgrading an existing one
for a in self.ability_prio:
    if a in choices and a not in have_ids:
        pick = a; break
# Phase 2: fall back to upgrade order
if not pick:
    for a in self.ability_prio:
        if a in choices: pick = a; break
```

### Wallet items beyond skins

`/api/wallet/connect` returns ownership flags for every NFT in the connected wallet:
- `tokenHolder` — $DOTA holder buff (+10% HP/dmg, applies to all wallet-bound bots automatically).
- `pixagreenMage`, `spaceMarine` — skins (1:1, server visually equips on one bot at a time).
- `ringOfRegen` — HP regen item. Ownership shown by API; whether the server auto-applies it on deploy or needs an explicit payload field is **unverified**. Worth comparing HP regen rate of a wallet-connected bot vs an unconnected bot to confirm.

## Provenance / numbers

Numbers from a 10-game test run (Apr 19 2026, mage fleet pre-strategy-v2):
- `lmeow` style avg level: 7.3
- `sigma` style avg level: 11.1 (clear winner — adopt sigma everywhere)
- Kill estimator at 180-XP threshold: missed 100% of kills (Beta scored 1 in-game, recorded 0)
- Win rate 30% — driven by 0-kill mage problem, not infrastructure issues

After v2 (strategy fixes): within 30 seconds of first deploy, three KILL/ASSIST events logged. Pending full 10-game verdict — but the leading indicator is positive.
