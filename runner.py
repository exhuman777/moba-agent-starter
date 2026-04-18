#!/usr/bin/env python3
"""Defense of the Agents - Headless Server Runner
No TUI, just logs. Runs forever, auto-rejoins games.
"""

from __future__ import annotations

import requests
import time
import json
import sys
import os
import signal
import random
import logging
from datetime import datetime

BASE = "https://wc2-agentic-dev-3o6un.ondigitalocean.app"
DIR = os.path.dirname(os.path.abspath(__file__))
FLEET_FILE = os.path.join(DIR, "fleet.json")
PARAMS_FILE = os.path.join(DIR, "params.json")
STATS_FILE = os.path.join(DIR, "stats.json")

TICK_RATE = 20
TOWER_BUFF_TICKS = 105 * TICK_RATE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(DIR, "bot.log"), mode="a"),
    ],
)
log = logging.getLogger("dota")


def api_get(path, params=None):
    try:
        r = requests.get(f"{BASE}{path}", params=params, timeout=10)
        return r.json() if r.status_code == 200 else {}
    except Exception as e:
        log.warning(f"GET {path} failed: {e}")
        return {}


def api_post(path, api_key, payload):
    try:
        r = requests.post(
            f"{BASE}{path}",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload, timeout=10,
        )
        return r.json() if r.status_code == 200 else {"error": f"{r.status_code}"}
    except Exception as e:
        return {"error": str(e)[:60]}


class Stats:
    def __init__(self):
        self.games = []
        if os.path.exists(STATS_FILE):
            try:
                with open(STATS_FILE) as f:
                    self.games = json.load(f)
            except Exception:
                pass

    def record(self, winner, faction, state, bots):
        won = winner == faction
        tick = state.get("tick", 0)
        our_heroes = [h for h in state.get("heroes", []) if h.get("faction") == faction]
        enemy_heroes = [h for h in state.get("heroes", []) if h.get("faction") != faction]

        bot_snaps = []
        for b in bots:
            hero = b.find_hero(state)
            snap = {
                "name": b.name, "class": b.hero_class, "role": b.role,
                "style": b.style, "lane": b.current_lane,
                "kills_est": b.kills_est, "deaths": b.deaths,
            }
            if hero:
                snap["level"] = hero.get("level", 1)
                snap["abilities"] = [{"id": a["id"], "level": a["level"]} for a in hero.get("abilities", [])]
            bot_snaps.append(snap)

        towers = {}
        for t in state.get("towers", []):
            towers[f"{t['faction']}_{t['lane']}"] = {"hp": t.get("hp", 0), "alive": t.get("alive", False)}

        enemy_f = "orc" if faction == "human" else "human"
        self.games.append({
            "time": datetime.now().isoformat(),
            "winner": winner, "faction": faction, "won": won,
            "game_secs": tick / TICK_RATE, "tick": tick,
            "our_count": len(our_heroes), "enemy_count": len(enemy_heroes),
            "our_max_level": max((h.get("level", 1) for h in our_heroes), default=1),
            "enemy_max_level": max((h.get("level", 1) for h in enemy_heroes), default=1),
            "our_base_hp": state.get("bases", {}).get(faction, {}).get("hp", 0),
            "enemy_base_hp": state.get("bases", {}).get(enemy_f, {}).get("hp", 0),
            "towers": towers,
            "bots": bot_snaps,
        })
        with open(STATS_FILE, "w") as f:
            json.dump(self.games, f, indent=2)
        w = sum(1 for g in self.games if g["won"])
        l = len(self.games) - w
        log.info(f"GAME OVER: {'WON' if won else 'LOST'} | Record: {w}W {l}L ({100*w/len(self.games):.0f}%)")


class Bot:
    def __init__(self, cfg, params, game):
        self.name = cfg["name"]
        self.key = cfg["key"]
        self.hero_class = cfg["class"]
        self.default_lane = cfg.get("lane", "mid")
        self.role = cfg.get("role", "dps")
        self.ability_prio = cfg.get("ability_prio", ["fury", "fortitude"])
        self.style = cfg.get("style", "balanced")
        self.game = game
        self.params = params
        self.joined = False
        self.faction = None
        self.current_lane = self.default_lane
        self.last_ping_tick = 0
        self.kills_est = 0
        self.deaths = 0
        self._prev_alive = True
        self._prev_xp_total = 0

    def find_hero(self, state):
        for h in state.get("heroes", []):
            if h.get("name") == self.name:
                self.faction = h.get("faction")
                return h
        return None

    def pick_ability(self, hero):
        choices = hero.get("abilityChoices", [])
        if not choices:
            return None
        if self.style == "random" or self.ability_prio == ["random"]:
            pick = random.choice(choices)
            log.info(f"{self.name} random pick: {pick}")
            return pick
        for a in self.ability_prio:
            if a in choices:
                return a
        return choices[0]

    def _count_lane(self, state, lane, faction):
        return sum(1 for h in state.get("heroes", [])
                   if h.get("lane") == lane and h.get("faction") == faction and h.get("alive"))

    def pick_lane(self, state, hero, all_bots):
        strat = self.params.get("strategy", "smart")
        tick = state.get("tick", 0)
        faction = self.faction or ""
        enemy = "orc" if faction == "human" else "human"

        if strat in ("top", "mid", "bot"):
            return strat
        if strat == "converge":
            return self.params.get("converge_lane", "mid")

        # ANTI-SELF-FIGHTING: avoid lanes where our bots are on the enemy team
        avoid = set()
        my_count = sum(1 for b in all_bots if b.faction == faction)
        total_bots = sum(1 for b in all_bots if b.faction is not None)
        solo = my_count < total_bots / 2
        for b in all_bots:
            if b.name != self.name and b.faction == enemy:
                avoid.add(b.current_lane)
        if solo and avoid:
            safe = [l for l in ["top", "mid", "bot"] if l not in avoid]
            if safe:
                best = min(safe, key=lambda l: self._count_lane(state, l, enemy))
                log.info(f"{self.name} solo avoidance -> {best} (avoiding {avoid})")
                return best

        # LEVEL-GAP CIRCUIT BREAKER
        our_lvls = [h.get("level", 1) for h in state.get("heroes", [])
                    if h.get("faction") == faction and h.get("alive")]
        enemy_lvls = [h.get("level", 1) for h in state.get("heroes", [])
                      if h.get("faction") == enemy and h.get("alive")]
        our_avg = sum(our_lvls) / max(len(our_lvls), 1)
        enemy_avg = sum(enemy_lvls) / max(len(enemy_lvls), 1)
        level_gap = enemy_avg - our_avg

        # Count our bots on this faction
        my_faction_bots = sum(1 for b in all_bots if b.faction == faction)
        total_bots = sum(1 for b in all_bots if b.faction is not None)
        minority = my_faction_bots < total_bots / 2

        turtle_mode = level_gap >= 3 or minority

        if tick < TOWER_BUFF_TICKS:
            return self.default_lane

        # TURTLE: group behind strongest tower
        if turtle_mode and strat not in ("push", "converge"):
            def lane_allies(l):
                return sum(1 for h in state.get("heroes", [])
                           if h.get("lane") == l and h.get("faction") == faction and h.get("alive"))
            def lane_enemies(l):
                return sum(1 for h in state.get("heroes", [])
                           if h.get("lane") == l and h.get("faction") == enemy and h.get("alive"))
            best_lane, best_score = self.default_lane, -999
            for lane in ["top", "mid", "bot"]:
                score = lane_allies(lane) * 3 - lane_enemies(lane) * 2
                for t in state.get("towers", []):
                    if t["faction"] == faction and t["lane"] == lane and t.get("alive"):
                        score += 5
                if score > best_score:
                    best_score = score
                    best_lane = lane
            return best_lane

        # Check finishable towers (only if not turtling)
        for t in state.get("towers", []):
            if t["faction"] == enemy and t.get("alive") and t.get("hp", 1200) < 200:
                if strat == "push":
                    return t["lane"]

        # Check endangered towers
        for t in state.get("towers", []):
            if t["faction"] == faction and t.get("alive") and t.get("hp", 1200) < 400:
                if strat == "defend":
                    return t["lane"]

        # Smart: go where needed
        lanes = state.get("lanes", {})
        bot_lanes = {"top": 0, "mid": 0, "bot": 0}
        for b in all_bots:
            if b.name != self.name and b.current_lane in bot_lanes:
                bot_lanes[b.current_lane] += 1

        best, best_score = self.default_lane, 999
        for lane in ["top", "mid", "bot"]:
            score = bot_lanes[lane] * 3
            ld = lanes.get(lane, {})
            enemy_u = ld.get(enemy, 0)
            our_u = ld.get(faction, 0)
            if enemy_u > our_u + 3:
                score -= 2
            for t in state.get("towers", []):
                if t["lane"] == lane and t["faction"] == enemy and t.get("alive") and t["hp"] < 300:
                    score -= 3
                if t["lane"] == lane and t["faction"] == faction and not t.get("alive"):
                    score -= 1
            if score < best_score:
                best_score = score
                best = lane
        return best

    def should_recall(self, hero):
        if hero.get("recallCooldownMs", 0) > 0 or not hero.get("alive"):
            return False
        mx = hero.get("maxHp", 1)
        threshold = self.params.get("recall_threshold", 0.25)
        return mx > 0 and hero.get("hp", 0) / mx < threshold

    def tick(self, state, all_bots):
        if state.get("winner"):
            self.joined = False
            return

        if not self.joined:
            result = api_post("/api/strategy/deployment", self.key,
                              {"heroClass": self.hero_class, "heroLane": self.default_lane})
            if "error" not in result:
                self.joined = True
                self.game = result.get("gameId", self.game)
                log.info(f"{self.name} joined G{self.game} as {self.hero_class} ({self.role})")
            return

        hero = self.find_hero(state)
        if not hero:
            return

        # Track deaths
        alive_now = hero.get("alive", False)
        if self._prev_alive and not alive_now:
            self.deaths += 1
        self._prev_alive = alive_now

        # Estimate kills via XP jumps
        level = hero.get("level", 1)
        xp_total = sum(200 * i for i in range(1, level)) + hero.get("xp", 0)
        if self._prev_xp_total > 0:
            xp_delta = xp_total - self._prev_xp_total
            if xp_delta >= 180:
                self.kills_est += xp_delta // 180
        self._prev_xp_total = xp_total

        if not alive_now:
            return

        payload = {}
        lane = self.pick_lane(state, hero, all_bots)
        # Only send heroLane when switching (resending same lane triggers shove micro)
        hero_lane = hero.get("lane", self.current_lane)
        if lane != hero_lane:
            payload["heroLane"] = lane
        self.current_lane = lane

        ability = self.pick_ability(hero)
        if ability:
            payload["abilityChoice"] = ability
            log.info(f"{self.name} picked {ability}")

        if self.should_recall(hero):
            payload["action"] = "recall"
            log.info(f"{self.name} RECALL {int(hero['hp'])}/{int(hero['maxHp'])}")

        hp_pct = int(100 * hero.get("hp", 0) / max(hero.get("maxHp", 1), 1))
        payload["message"] = f"{self.role} L{hero.get('level',1)} {hp_pct}%"

        api_post("/api/strategy/deployment", self.key, payload)


def main():
    with open(FLEET_FILE) as f:
        fleet = json.load(f)

    params = {}
    if os.path.exists(PARAMS_FILE):
        with open(PARAMS_FILE) as f:
            params = json.load(f)

    game = fleet.get("game", 3)
    poll = params.get("poll_interval", 5)
    stats = Stats()

    bots = [Bot(b, params, game) for b in fleet["bots"]]
    log.info(f"Fleet: {len(bots)} bots, game {game}, poll {poll}s")
    log.info(f"Comp: {sum(1 for b in bots if b.hero_class=='melee')}M / "
             f"{sum(1 for b in bots if b.hero_class=='ranged')}R / "
             f"{sum(1 for b in bots if b.hero_class=='mage')}Ma")
    if stats.games:
        w = sum(1 for g in stats.games if g["won"])
        l = len(stats.games) - w
        log.info(f"Stats: {w}W {l}L")
    else:
        log.info("Stats: fresh")

    last_winner = None
    running = True

    def shutdown(sig, frame):
        nonlocal running
        log.info("Shutting down...")
        running = False
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    while running:
        try:
            state = api_get("/api/game/state", {"game": game})
            if not state:
                time.sleep(poll)
                continue

            winner = state.get("winner")
            if winner and winner != last_winner:
                faction = bots[0].faction
                if faction:
                    stats.record(winner, faction, state, bots)
                last_winner = winner
            elif not winner:
                last_winner = None

            for bot in bots:
                bot.tick(state, bots)

            time.sleep(poll)
        except Exception as e:
            log.error(f"Loop error: {e}")
            time.sleep(10)

    log.info("Stopped.")


if __name__ == "__main__":
    main()
