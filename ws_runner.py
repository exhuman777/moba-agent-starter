#!/usr/bin/env python3
"""HORDE AGENTS - WebSocket Runner
20x/sec game state via WebSocket instead of 1s HTTP polling.
Matches lmeow's reaction speed.

Run: python3 ws_runner.py
"""

from __future__ import annotations
import json, os, time, threading, logging
from collections import deque
# (threading kept at top for multi-game WS fan-out in WSRunner.run)
from datetime import datetime
import requests
import websocket

DIR = os.path.dirname(os.path.abspath(__file__))
FLEET_FILE = os.path.join(DIR, "fleet.json")
STATS_FILE = os.path.join(DIR, "stats.json")
BASE = "https://wc2-agentic-dev-3o6un.ondigitalocean.app"
WS_URL = "wss://wc2-agentic-dev-3o6un.ondigitalocean.app/ws"
TICK_RATE = 20
SUDDEN_DEATH_TICKS = 15 * 60 * TICK_RATE
LANES = ["top", "mid", "bot"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("ws")

def api_post(path, key, payload):
    try:
        r = requests.post(f"{BASE}{path}",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload, timeout=5)
        return r.json() if r.status_code == 200 else {"error": str(r.status_code)}
    except Exception as e:
        return {"error": str(e)[:60]}

def is_our_bot(name):
    # Any bot naming convention we've used: ExH*, ExHuman*, EX_* (v3).
    # If you add a new naming scheme, extend this prefix list.
    return (
        name.startswith("ExH")
        or name.startswith("ExHuman")
        or name.startswith("EX_")
    )


class WSBot:
    """Bot that reacts to WebSocket state at 20x/sec.
    Only sends commands when something changes (no shove spam).

    Each bot has its own `game_id` — the server may place different bots in
    different game slots, so ws_runner opens one WebSocket per unique gameId.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.name = cfg["name"]
        self.key = cfg["key"]
        self.hero_class = cfg["class"]
        self.default_lane = cfg.get("lane", "mid")
        self.style = cfg.get("style", "lmeow")
        self.skin = cfg.get("skin")
        self.rotate_lane = bool(cfg.get("rotate_lane"))
        self.wallet_required = bool(cfg.get("wallet"))
        self.wallet_ok = cfg.get("wallet_ok", False)
        self.wallet_skin_ok = cfg.get("wallet_skin_ok", True)
        self.wallet_holder = cfg.get("wallet_holder")
        self.ability_prio = cfg.get("ability_prio", ["tornado", "fortitude", "fireball", "fury"])
        self.joined = False
        self.game_id: int | None = None        # server-assigned on deploy
        self.faction = None
        self.current_lane = self.default_lane
        self.kills = 0                         # XP-jump-detected hero kills/assists this game
        self.deaths = 0
        self._prev_alive = True
        self._prev_hp = 0
        self._prev_level = 0
        self._prev_xp_total = 0                # cumulative XP last tick (for kill detect)
        self._last_command_tick = 0
        self._last_switch_tick = 0
        self._last_recall_tick = 0
        self._last_deploy_try = 0.0            # unix ts for retry throttle
        # Rolling HP window for burst-detection recall: ~2.5s @ 20Hz
        self._hp_hist: deque[tuple[int, float]] = deque(maxlen=50)
        # Death-streak filter: WS state can flicker alive=False for 1 tick on
        # respawn ack. Require 3 consecutive dead ticks before counting a death.
        self._dead_streak = 0
        self._death_recorded = False
        # Fresh-game gate: don't parachute into a mid-game match on startup.
        # Once we've seen a game-end (winner set), the gate is open forever
        # because every subsequent deploy IS at the start of a new game.
        self._seen_game_over = False

    def process(self, state):
        """Called every WebSocket tick (20x/sec). Only sends API calls when needed."""
        if state.get("winner"):
            if self.joined:
                log.info(f"{self.name}: game over ({state['winner']})")
            self.joined = False
            self.kills = 0; self.deaths = 0
            self._dead_streak = 0
            self._death_recorded = False
            self._seen_game_over = True   # opens the fresh-game deploy gate
            return

        # Find our hero
        hero = None
        for h in state.get("heroes", []):
            if h.get("name") == self.name:
                hero = h
                self.faction = h.get("faction")
                break

        if not hero:
            if not self.joined:
                # FRESH-GAME GATE: don't deploy into a mid-game match. If we
                # boot at tick 8000 of an in-progress game, our L1 mages get
                # paired vs L10+ enemies and feed. Wait for either:
                #   (a) the current tick is low (game just started), OR
                #   (b) we've seen a game-end since startup (next game is fresh)
                tick_now = state.get("tick", 0)
                if tick_now > 600 and not self._seen_game_over:
                    return
                # Throttle retries so a misbehaving deploy doesn't spam the
                # server 20x/sec. 5s between attempts is ample.
                now = time.time()
                if now - self._last_deploy_try < 5:
                    return
                self._last_deploy_try = now
                if self.wallet_required and not self.wallet_ok:
                    log.warning(f"{self.name}: deploy gated — wallet not ok")
                    return
                if self.skin and not self.wallet_skin_ok:
                    log.warning(f"{self.name}: deploy gated — skin NFT '{self.skin}' missing")
                    return
                deploy = {"heroClass": self.hero_class, "heroLane": self.default_lane}
                if self.skin:
                    deploy["skin"] = self.skin
                r = api_post("/api/strategy/deployment", self.key, deploy)
                if "error" in r:
                    log.warning(f"{self.name}: deploy failed: {r['error']}")
                    return
                self.joined = True
                self.game_id = r.get("gameId")
                log.info(f"{self.name}: joined gameId={self.game_id} class={self.hero_class} lane={self.default_lane}"
                         + (f" skin={self.skin}" if self.skin else ""))
            return

        tick = state.get("tick", 0)
        alive = hero.get("alive", False)
        level = hero.get("level", 1)
        hp = hero.get("hp", 0)
        max_hp = hero.get("maxHp", 1)
        hp_pct = hp / max(max_hp, 1)
        lane = hero.get("lane", self.current_lane)
        faction = self.faction or ""
        enemy = "orc" if faction == "human" else "human"

        # Track deaths — DEATH-STREAK FILTER: WS state flickers alive=False
        # for 1 tick on respawn ack, causing a stale 1-tick alive bot to be
        # double/triple-counted. Require 3 consecutive dead ticks before
        # counting a death; reset the latch when actually alive.
        if alive:
            self._dead_streak = 0
            self._death_recorded = False
        else:
            self._dead_streak += 1
            if self._dead_streak >= 3 and not self._death_recorded:
                self.deaths += 1
                self._death_recorded = True
        # Death rotation: on respawn, pick best lane (ONCE per death, 30s cooldown)
        if not self._prev_alive and alive and tick - self._last_switch_tick > 30 * TICK_RATE:
            lane_enemies = {}
            for h in state.get("heroes", []):
                if h.get("faction") == enemy and h.get("alive"):
                    l = h.get("lane", "mid")
                    lane_enemies[l] = lane_enemies.get(l, 0) + 1
            best = self.default_lane
            best_score = -1
            for l in LANES:
                e = lane_enemies.get(l, 0)
                score = e if e <= 2 else -e
                if score > best_score:
                    best_score = score; best = l
            if best != self.current_lane:
                self.current_lane = best
                self._last_switch_tick = tick
                api_post("/api/strategy/deployment", self.key, {"heroLane": best})
                log.info(f"{self.name}: death rotation -> {best}")
        self._prev_alive = alive

        if not alive:
            return

        game_secs = tick / TICK_RATE

        # === KILL ESTIMATION via XP jumps ===
        # Hero kills give 100-300+ XP depending on enemy level. Creep waves
        # give ~10-30 XP per creep. A jump in [80, 500) is almost certainly
        # a hero kill or assist. Old code never tracked this — Beta showed
        # 1 server-confirmed kill while our snap recorded 0.
        xp_total = sum(200 * i for i in range(1, level)) + hero.get("xp", 0)
        if self._prev_xp_total > 0:
            xp_delta = xp_total - self._prev_xp_total
            if 80 <= xp_delta < 500:
                self.kills += 1
                log.info(f"{self.name}: KILL/ASSIST detected (xp+{xp_delta}, total={self.kills})")
        self._prev_xp_total = xp_total

        # === ADAPTIVE RECALL ===
        # Old: any enemy in lane bumped threshold to ~33% → bots panicked at
        # mere presence and lost XP. New: trigger only on actual burst damage
        # OR critical absolute HP. Tower-dive guard added separately.
        self._hp_hist.append((tick, hp))
        recall_cd = hero.get("recallCooldownMs", 0)

        should_recall = False
        recall_reason = ""
        if recall_cd == 0 and game_secs < 780:
            if hp_pct < 0.15:
                should_recall = True
                recall_reason = f"CRIT {hp_pct:.0%}"
            else:
                # Burst detection: max HP seen in last ~2s vs now
                burst_cutoff = tick - 2 * TICK_RATE
                recent = [h for t, h in self._hp_hist if t >= burst_cutoff]
                if recent:
                    hp_then = max(recent)
                    hp_lost_pct = (hp_then - hp) / max(max_hp, 1)
                    if hp_lost_pct > 0.30 and hp_pct < 0.55:
                        should_recall = True
                        recall_reason = f"BURST -{hp_lost_pct:.0%}/2s"

        if should_recall and tick - self._last_recall_tick > 5 * TICK_RATE:
            api_post("/api/strategy/deployment", self.key,
                     {"action": "recall", "message": f"R-{recall_reason}"})
            self._last_recall_tick = tick
            log.info(f"{self.name}: RECALL {recall_reason}")
            return

        # === ABILITY PICK — BYPASS THROTTLE + GUARDS ===
        # Pick FIRST and IMMEDIATELY when offered. Previously this was
        # downstream of the tower-dive guard + 20-tick throttle, which
        # starved the picker during active fights — bots were leveling up
        # but never spending their ability points (e.g. EX_1 at L8 with 1
        # ability instead of 3-4). Picks are not lane commands; sending
        # them on demand is fine for server load.
        choices = hero.get("abilityChoices", [])
        if choices:
            pick = None
            pick_reason = ""
            have_ids = {a.get("id") for a in hero.get("abilities", []) if isinstance(a, dict)}
            # Phase 0: KILL-EXECUTE override
            if "fireball" in choices:
                enemies_low = [h for h in state.get("heroes", [])
                               if h.get("faction") == enemy and h.get("lane") == lane
                               and h.get("alive")
                               and h.get("hp", 0) / max(h.get("maxHp", 1), 1) < 0.30]
                if enemies_low:
                    pick = "fireball"; pick_reason = "exec"
            # Phase 1: BREADTH-FIRST (AzFlin) — pick a NEW ability over an upgrade
            if not pick:
                for a in self.ability_prio:
                    if a in choices and a not in have_ids:
                        pick = a; pick_reason = "new"; break
            # Phase 2: fall back to upgrade order
            if not pick:
                for a in self.ability_prio:
                    if a in choices:
                        pick = a; pick_reason = "upgrade"; break
            if not pick:
                pick = choices[0]; pick_reason = "fallback"
            api_post("/api/strategy/deployment", self.key, {"abilityChoice": pick})
            log.info(f"{self.name}: ability {pick} [{pick_reason}]")

        # === TOWER-DIVE GUARD ===
        # Block lane/recall commands when chasing under enemy tower at low HP.
        # Critical: DO NOT bump _last_command_tick here — that starved the
        # ability picker for 20 ticks every time the guard fired, which was
        # essentially constant during mid-lane combat.
        enemy_tower_alive = any(
            t.get("faction") == enemy and t.get("lane") == lane and t.get("alive")
            and t.get("hp", 0) > 200
            for t in state.get("towers", [])
        )
        enemies_near = sum(1 for h in state.get("heroes", [])
                           if h.get("faction") == enemy and h.get("lane") == lane and h.get("alive"))
        if enemy_tower_alive and enemies_near > 0 and hp_pct < 0.70:
            return  # let the bot drift back naturally; ability already picked above

        # === REACT TO STATE (only send commands when needed) ===
        # Throttle: max 1 command per second (20 ticks) to avoid API spam
        if tick - self._last_command_tick < 20:
            return

        payload = {}
        send = False

        # 3. PING: weak enemy tower
        for t in state.get("towers", []):
            if t["faction"] == enemy and t.get("alive") and t.get("hp", 1200) < 400:
                payload["ping"] = t["lane"]
                send = True
                break

        # 4. SIGMA STYLE: field-aware lane switching (WebSocket advantage)
        # Uses 20x/sec position data to understand battlefield in real-time
        if self.style == "sigma" and game_secs > 120:
            # Only switch every 90s (no shove spam)
            if tick - self._last_switch_tick > 90 * TICK_RATE:
                # Count enemies and allies per lane
                lane_power = {}
                for l in LANES:
                    allies = sum(1 for h in state.get("heroes", [])
                                 if h.get("faction") == faction and h.get("lane") == l and h.get("alive"))
                    enemies_l = sum(1 for h in state.get("heroes", [])
                                    if h.get("faction") == enemy and h.get("lane") == l and h.get("alive"))
                    lane_power[l] = {"a": allies, "e": enemies_l}

                # Rule 1: if I'm highest level, go help weakest lane
                my_team = [h for h in state.get("heroes", [])
                           if h.get("faction") == faction and h.get("alive")]
                am_carry = my_team and level >= max(h.get("level", 1) for h in my_team)

                if am_carry:
                    # Find lane where team is most outnumbered
                    worst = None
                    worst_diff = 0
                    for l in LANES:
                        diff = lane_power[l]["e"] - lane_power[l]["a"]
                        if diff > worst_diff:
                            worst_diff = diff; worst = l
                    if worst and worst != lane and worst_diff >= 2:
                        payload["heroLane"] = worst
                        self.current_lane = worst
                        self._last_switch_tick = tick
                        send = True
                        log.info(f"{self.name}: SIGMA carry rotation -> {worst} (outnumbered by {worst_diff})")

                # Rule 2: enemy tower about to die, go finish
                for t in state.get("towers", []):
                    if t["faction"] == enemy and t.get("alive") and t.get("hp", 1200) < 150:
                        target = t["lane"]
                        if target != lane:
                            payload["heroLane"] = target
                            self.current_lane = target
                            self._last_switch_tick = tick
                            send = True
                            log.info(f"{self.name}: SIGMA tower snipe -> {target} ({t['hp']:.0f} HP)")
                            break

                # Rule 3: my tower dead, go to lane with tower
                my_tower = any(t["faction"] == faction and t["lane"] == lane and t.get("alive")
                               for t in state.get("towers", []))
                if not my_tower:
                    for l in LANES:
                        if l != lane and any(t["faction"] == faction and t["lane"] == l and t.get("alive")
                                             for t in state.get("towers", [])):
                            payload["heroLane"] = l
                            self.current_lane = l
                            self._last_switch_tick = tick
                            send = True
                            log.info(f"{self.name}: SIGMA tower retreat -> {l}")
                            break

        # 5. ENDGAME: group in last 60s
        if tick > SUDDEN_DEATH_TICKS - 60 * TICK_RATE:
            best = max(LANES, key=lambda l: sum(1 for h in state.get("heroes", [])
                       if h.get("faction") == faction and h.get("lane") == l and h.get("alive")))
            if best != lane:
                payload["heroLane"] = best
                self.current_lane = best
                send = True

        if send:
            payload["message"] = f"L{level} {hp_pct:.0%}"
            api_post("/api/strategy/deployment", self.key, payload)
            self._last_command_tick = tick

        self._prev_hp = hp
        self._prev_level = level


class WSRunner:
    """WebSocket game state receiver + bot controller.

    Multi-game aware: the server may split our fleet across game slots
    (e.g. Beta+Alpha -> game 3, Sigma -> game 4 when game 3 was already
    "in progress" for the fresh bot). For each unique bot.game_id we
    maintain a dedicated WebSocket connection in its own thread.
    """

    def __init__(self):
        with open(FLEET_FILE) as f:
            fleet = json.load(f)
        self.default_game = fleet.get("game", 3)
        from wallet import auto_connect
        from experiment import apply_lane_rotation
        auto_connect(fleet["bots"])
        self.game_idx = self._load_game_count()
        apply_lane_rotation(fleet["bots"], self.game_idx)
        self.bots = [WSBot(b) for b in fleet["bots"]]
        # Every bot initially targets the fleet.json default game. If the
        # server redirects a bot to a different gameId on deploy, that bot's
        # game_id is updated and a new WS is spawned lazily in run().
        for b in self.bots:
            b.game_id = self.default_game
        self.tick_count = 0
        self.state: dict = {}  # last state from the DEFAULT game (for summary logs)
        # Per-game winner de-dup so GAME OVER isn't recorded twice when the
        # same final-state frame arrives multiple times.
        self._last_winner_by_game: dict[int, str | None] = {}
        self._ws_by_game: dict[int, object] = {}
        self._threads_by_game: dict[int, threading.Thread] = {}
        self._stop = False
        # REST-cache for abilityChoices. The WS unit array format changed in
        # patch 1.14.1 — index 18 is now the skin string, and abilityChoices
        # was dropped from WS entirely. Only REST /api/game/state returns it.
        # We poll REST every 3s per game and inject the choices into the
        # WS-parsed state so the picker can fire.
        self._choices_cache: dict[tuple[int, str], list] = {}  # (gid, name) -> choices
        self._last_choices_poll: dict[int, float] = {}         # gid -> unix ts

        log.info(f"WSRunner: {len(self.bots)} bots, default game={self.default_game}, idx={self.game_idx}")
        for b in self.bots:
            rot = " [ROTATE]" if b.rotate_lane else ""
            log.info(f"  {b.name} lane:{b.default_lane} style:{b.style}{rot} prio:{'>'.join(a[:4] for a in b.ability_prio[:4])}")

    def _load_game_count(self) -> int:
        if not os.path.exists(STATS_FILE):
            return 0
        try:
            with open(STATS_FILE) as f:
                return len(json.load(f))
        except Exception:
            return 0

    def _advance_lane_rotation(self):
        """Bump game_idx + propagate new rotated lane to flagged bots. Called
        once per game-end. Updates default_lane + current_lane so the NEXT
        deploy uses the new lane. No mid-game lane commands sent here."""
        from experiment import rotate_lane
        self.game_idx += 1
        for bot in self.bots:
            if bot.rotate_lane:
                new_lane = rotate_lane(self.game_idx)
                bot.default_lane = new_lane
                bot.current_lane = new_lane
                log.info(f"rotation: {bot.name} -> {new_lane} (idx={self.game_idx})")

    def _reload_fleet_config(self) -> None:
        """Hot-reload runtime config from fleet.json without restarting.

        Matches bots by `name`. Picks up changes to `skin`, `style`,
        `ability_prio`, `lane`, `rotate_lane`, `wallet`. If any wallet bot's
        `skin` changed, re-runs wallet.auto_connect so wallet_skin_ok is
        re-validated against the NFT. Bots added/removed from fleet.json are
        ignored — that still requires a full restart.
        """
        try:
            with open(FLEET_FILE) as f:
                fleet = json.load(f)
        except Exception as e:
            log.warning(f"fleet reload: read failed: {e}")
            return

        new_cfgs = {b["name"]: b for b in fleet.get("bots", [])}

        # Detect whether a wallet re-check is needed (skin changed on any wallet bot)
        needs_wallet_recheck = False
        for bot in self.bots:
            cfg = new_cfgs.get(bot.name)
            if not cfg:
                continue
            if bool(cfg.get("wallet")) and cfg.get("skin") != bot.skin:
                needs_wallet_recheck = True
                break

        if needs_wallet_recheck:
            from wallet import auto_connect
            auto_connect(fleet["bots"])  # mutates each dict with wallet_* fields

        changed: list[str] = []
        for bot in self.bots:
            cfg = new_cfgs.get(bot.name)
            if not cfg:
                continue
            before = (bot.skin, bot.style, tuple(bot.ability_prio),
                      bot.default_lane, bot.rotate_lane, bot.wallet_required)
            bot.skin = cfg.get("skin")
            bot.style = cfg.get("style", bot.style)
            bot.ability_prio = cfg.get("ability_prio", bot.ability_prio)
            bot.default_lane = cfg.get("lane", bot.default_lane)
            bot.rotate_lane = bool(cfg.get("rotate_lane"))
            bot.wallet_required = bool(cfg.get("wallet"))
            # Only overwrite wallet_* if we just re-ran auto_connect (those
            # fields were just mutated on the cfg dict). Otherwise keep
            # whatever the bot already had from startup.
            if needs_wallet_recheck:
                bot.wallet_ok = cfg.get("wallet_ok", False)
                bot.wallet_skin_ok = cfg.get("wallet_skin_ok", True)
                bot.wallet_holder = cfg.get("wallet_holder")
            after = (bot.skin, bot.style, tuple(bot.ability_prio),
                     bot.default_lane, bot.rotate_lane, bot.wallet_required)
            if before != after:
                changed.append(bot.name)

        if changed:
            log.info(f"fleet reload: applied config changes to {changed}"
                     + (" (wallet re-checked)" if needs_wallet_recheck else ""))

    def _refresh_choices_cache(self, gid: int) -> None:
        """Poll REST for ability choices (WS payload dropped them in 1.14.1).
        Cached for 3 seconds per game. Cheap — single HTTP GET per interval.
        """
        now = time.time()
        if now - self._last_choices_poll.get(gid, 0) < 3.0:
            return
        self._last_choices_poll[gid] = now
        try:
            r = requests.get(f"{BASE}/api/game/state", params={"game": gid}, timeout=3)
            if r.status_code != 200:
                return
            for h in r.json().get("heroes", []):
                nm = h.get("name")
                if nm and is_our_bot(nm):
                    self._choices_cache[(gid, nm)] = h.get("abilityChoices", []) or []
        except Exception:
            pass

    def _on_message_for_game(self, gid: int, message):
        """Dispatch a message from game `gid` to bots assigned to that game."""
        try:
            data = json.loads(message)
        except Exception:
            return
        if data.get("gameId") != gid:
            return

        self.tick_count += 1
        self._refresh_choices_cache(gid)

        state = self._parse_ws_state(data)
        if not state:
            return

        # Inject REST-sourced ability choices into each of our heroes.
        for h in state.get("heroes", []):
            nm = h.get("name")
            if nm and is_our_bot(nm):
                h["abilityChoices"] = self._choices_cache.get((gid, nm), [])

        # A bot is "in this game" if it's already tagged for this gid, OR it's
        # post-game-reset (game_id=None) and this is the default game — that's
        # where fresh deploys go. Without the second clause, bots are orphaned
        # after _record_game resets their game_id and never re-deploy.
        bots_in_game = [b for b in self.bots
                        if b.game_id == gid
                        or (b.game_id is None and gid == self.default_game)]

        if gid == self.default_game:
            self.state = state  # used by periodic summary log

        # Game-over / winner detection — per game
        winner = state.get("winner")
        last = self._last_winner_by_game.get(gid)
        if winner and winner != last:
            self._record_game(winner, state, bots_in_game, gid)
            self._last_winner_by_game[gid] = winner
        elif not winner:
            self._last_winner_by_game[gid] = None

        for bot in bots_in_game:
            try:
                bot.process(state)
            except Exception as e:
                log.error(f"{bot.name}: {e}")
            # If this bot just re-deployed and was redirected to another game,
            # ensure that game's WebSocket is running.
            if bot.game_id and bot.game_id != gid and bot.game_id not in self._ws_by_game:
                self._start_ws_for_game(bot.game_id)

        # Log every 100 ticks (5 s) per game
        if self.tick_count % 100 == 0:
            tick = state.get("tick", 0)
            heroes = state.get("heroes", [])
            our = [h for h in heroes if is_our_bot(h.get("name", ""))]
            our_str = " ".join(f"{h['name'][-5:]}:L{h.get('level',1)}" for h in our)
            log.info(f"[g{gid} {tick/20:.0f}s] {len(heroes)} heroes | {our_str}")

    def _parse_ws_state(self, data):
        """Convert WebSocket unit array format to REST API format."""
        units = data.get("units", [])
        tick = data.get("tick", 0)

        # Parse heroes from units (heroes have name string at index 11)
        heroes = []
        for u in units:
            if len(u) > 11 and isinstance(u[11], str) and u[11]:
                # Unit format: [id, type, faction, x, y, hp, maxHp, alive, ?, ?, ?, name, ?, level, xp, xpToNext, ?, abilities, abilityChoices, recallCd]
                hero = {
                    "name": u[11],
                    "faction": "human" if u[2] == 0 else "orc",
                    "hp": u[5], "maxHp": u[6],
                    "alive": u[7] == 1,
                    "level": u[13] if len(u) > 13 else 1,
                    "xp": u[14] if len(u) > 14 else 0,
                    "xpToNext": u[15] if len(u) > 15 else 200,
                }
                # Parse class from type
                unit_type = u[1]
                if unit_type in (3, 4):
                    hero["class"] = "mage"
                elif unit_type in (5, 6):
                    hero["class"] = "ranged"
                else:
                    hero["class"] = "melee"

                # Parse abilities
                if len(u) > 17 and isinstance(u[17], list):
                    hero["abilities"] = [{"id": a[0], "level": a[1]} for a in u[17] if isinstance(a, list)]
                else:
                    hero["abilities"] = []

                # Parse ability choices
                if len(u) > 18 and isinstance(u[18], list):
                    hero["abilityChoices"] = u[18]
                else:
                    hero["abilityChoices"] = []

                # Recall cooldown
                if len(u) > 19:
                    hero["recallCooldownMs"] = u[19] if isinstance(u[19], (int, float)) else 0
                else:
                    hero["recallCooldownMs"] = 0

                # Determine lane from x position (approximate)
                x = u[3]
                if x < 1200:
                    hero["lane"] = "top"
                elif x < 2400:
                    hero["lane"] = "mid"
                else:
                    hero["lane"] = "bot"

                heroes.append(hero)

        if not heroes:
            return None

        # Build simplified state (matching REST API format)
        return {
            "tick": tick,
            "heroes": heroes,
            "winner": data.get("winner"),
            "towers": data.get("towers", []),
            "bases": data.get("bases", {}),
            "lanes": data.get("lanes", {}),
        }

    def _record_game(self, winner, state, bots_in_game, gid):
        """Save game result to stats.json, then advance lane rotation.

        Records only the bots that were actually playing in this game (`bots_in_game`).
        Schema matches what dashboard's StatsTracker reads: majority_won +
        per-faction counts + per-bot snap.
        """
        try:
            games = []
            if os.path.exists(STATS_FILE):
                with open(STATS_FILE) as f:
                    games = json.load(f)

            from experiment import bot_snap
            snaps = []
            for bot in bots_in_game:
                bot._game_winner = winner
                hero = None
                for h in state.get("heroes", []):
                    if h.get("name") == bot.name:
                        hero = h; break
                snaps.append(bot_snap(bot, hero))

            h_bots = sum(1 for b in bots_in_game if b.faction == "human")
            o_bots = sum(1 for b in bots_in_game if b.faction == "orc")
            # Scoring fix (v3.1): the old rule was `majority = "human" if
            # h_bots > o_bots else "orc"`, which defaulted 2/2 ties to orc
            # and silently flipped half our 2/2 games to losses. 47% of v3
            # games fall in 2/2 — this was a large systematic bias.
            # New rule: `majority_won` is True iff at least half our bots
            # ended on the winning side (`bots_won >= total_bots / 2`).
            # Tied-split games now count as wins when half our fleet won.
            if h_bots > o_bots:
                majority = "human"
            elif o_bots > h_bots:
                majority = "orc"
            else:
                majority = "tie"
            total_bots = len(bots_in_game)
            bots_won = sum(1 for b in bots_in_game if b.faction == winner)
            majority_won = (2 * bots_won >= total_bots) if total_bots else False

            heroes = state.get("heroes", [])
            h_levels = [h.get("level", 1) for h in heroes if h.get("faction") == "human"]
            o_levels = [h.get("level", 1) for h in heroes if h.get("faction") == "orc"]
            bases = state.get("bases", {})

            games.append({
                "time": datetime.now().isoformat(),
                "winner": winner,
                "majority_faction": majority,
                "majority_won": majority_won,
                "human_bots": h_bots,
                "orc_bots": o_bots,
                "tick": state.get("tick", 0),
                "game_time": state.get("tick", 0) / TICK_RATE,
                "human_max_level": max(h_levels, default=1),
                "orc_max_level": max(o_levels, default=1),
                "human_base_hp": bases.get("human", {}).get("hp", 0),
                "orc_base_hp": bases.get("orc", {}).get("hp", 0),
                "game_idx": self.game_idx,
                "gameId": gid,
                "bots": snaps,
            })

            with open(STATS_FILE, "w") as f:
                json.dump(games, f, indent=2)

            w = sum(1 for b in snaps if b.get("won"))
            verdict = "WIN" if majority_won else "LOSS"
            log.info(f"GAME OVER [g{gid}] (idx={self.game_idx}): {winner} wins. "
                     f"Majority {majority} {verdict}. Our bots in this game: {w}/{len(snaps)} won.")
        except Exception as e:
            log.error(f"Stats save error: {e}")
        finally:
            # Reset joined/game_id so bot tries to redeploy into the next game
            for b in bots_in_game:
                b.joined = False
                b.game_id = None
                b._last_deploy_try = 0.0
            self._advance_lane_rotation()
            # Hot-reload fleet.json so live edits (skin swaps, lane tweaks,
            # style changes) apply on the NEXT deploy without restart.
            self._reload_fleet_config()

    def on_error(self, ws, error):
        log.error(f"WS error: {error}")

    def _start_ws_for_game(self, gid: int) -> None:
        """Open a dedicated WebSocket for game `gid` in its own daemon thread.
        Idempotent — already-running game_ids are no-ops.
        """
        if gid in self._ws_by_game:
            return
        url = f"{WS_URL}?game={gid}"
        log.info(f"Connecting to {url}")

        def on_open(ws):
            log.info(f"WS connected to game {gid}")

        def on_close(ws, code, msg):
            log.info(f"WS closed game {gid}: {code} {msg}")
            # Let the supervisor loop reopen on next tick if we're not stopping.
            self._ws_by_game.pop(gid, None)

        def on_msg(ws, msg):
            self._on_message_for_game(gid, msg)

        ws = websocket.WebSocketApp(
            url, on_message=on_msg, on_error=self.on_error,
            on_close=on_close, on_open=on_open,
        )
        t = threading.Thread(
            # 20/10 is tighter than the old 30/10. Overnight run saw 5+
            # ping/pong timeouts; faster pings detect dead connections
            # sooner so auto-respawn loses less state during blips.
            target=lambda: ws.run_forever(ping_interval=20, ping_timeout=10),
            daemon=True, name=f"ws-game-{gid}",
        )
        self._ws_by_game[gid] = ws
        self._threads_by_game[gid] = t
        t.start()

    def run(self):
        """Supervisor loop. Opens WS for the default game, then monitors
        self.bots for newly-assigned game_ids and spawns additional WS
        connections on demand.
        """
        self._start_ws_for_game(self.default_game)
        try:
            while not self._stop:
                time.sleep(5)
                for b in self.bots:
                    if b.game_id and b.game_id not in self._ws_by_game:
                        self._start_ws_for_game(b.game_id)
        except KeyboardInterrupt:
            log.info("Shutdown requested.")
            self._stop = True
            for gid, ws in list(self._ws_by_game.items()):
                try:
                    ws.close()
                except Exception:
                    pass


if __name__ == "__main__":
    print("HORDE AGENTS - WebSocket Runner (20x/sec)")
    print("Ctrl+C to stop")
    print()
    runner = WSRunner()
    runner.run()
