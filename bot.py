#!/usr/bin/env python3
"""Defense of the Agents - Autonomous Bot"""

from __future__ import annotations

import requests
import time
import json
import sys
import os
import argparse

BASE = "https://wc2-agentic-dev-3o6un.ondigitalocean.app"
STATE_INTERVAL = 5  # seconds between polls (aggressive but not spammy)
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}


def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def register(name: str) -> str:
    """Register agent, return API key."""
    r = requests.post(f"{BASE}/api/agents/register", json={"agentName": name})
    if r.status_code == 201:
        data = r.json()
        key = data.get("apiKey") or data.get("api_key") or data.get("key")
        print(f"[+] Registered as '{name}'. API key: {key}")
        return key
    elif r.status_code == 409:
        print(f"[!] Name '{name}' already taken.")
        sys.exit(1)
    else:
        print(f"[!] Register failed: {r.status_code} {r.text}")
        sys.exit(1)


def headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def get_state(game: int = 1) -> dict:
    r = requests.get(f"{BASE}/api/game/state", params={"game": game})
    if r.status_code == 200:
        return r.json()
    print(f"[!] State fetch failed: {r.status_code}")
    return {}


def deploy(api_key: str, payload: dict) -> dict:
    r = requests.post(f"{BASE}/api/strategy/deployment", headers=headers(api_key), json=payload)
    if r.status_code == 200:
        return r.json()
    print(f"[!] Deploy failed: {r.status_code} {r.text}")
    return {}


class Bot:
    def __init__(self, api_key: str, game: int, hero_class: str = "mage"):
        self.api_key = api_key
        self.game = game
        self.hero_class = hero_class
        self.joined = False
        self.my_name = None
        self.my_faction = None
        self.last_ping_tick = 0
        self.tick = 0

    def find_me(self, state: dict) -> dict | None:
        """Find our hero in state."""
        # If we already know our name, look directly
        if self.my_name:
            for h in state.get("heroes", []):
                if h.get("name") == self.my_name:
                    self.my_faction = h.get("faction", self.my_faction)
                    return h
            return None

        # agents dict is {faction: [name_string, ...]}
        # We need to identify ourselves, check config for agent_name
        cfg = load_config()
        agent_name = cfg.get("agent_name")
        if agent_name:
            for faction, names in state.get("agents", {}).items():
                if agent_name in names:
                    self.my_name = agent_name
                    self.my_faction = faction
                    for h in state.get("heroes", []):
                        if h.get("name") == agent_name:
                            return h
        return None

    def pick_ability(self, hero: dict) -> str | None:
        """Choose best ability from available choices."""
        choices = hero.get("abilityChoices", [])
        if not choices:
            return None

        current = {a["id"]: a["level"] for a in hero.get("abilities", [])}
        cls = hero.get("class", self.hero_class)

        # Priority by class
        if cls == "mage":
            prio = ["fireball", "tornado", "raise_skeleton", "fury", "fortitude"]
        elif cls == "melee":
            prio = ["cleave", "divine_shield", "thorns", "fortitude", "fury"]
        else:  # ranged
            prio = ["critical_strike", "bloodlust", "volley", "stim_pack", "fury", "fortitude"]

        for ability in prio:
            if ability in choices:
                return ability

        return choices[0] if choices else None

    def pick_lane(self, state: dict, hero: dict) -> str:
        """Pick lane based on where we're needed most."""
        lanes = state.get("lanes", {})
        my_faction = self.my_faction or hero.get("faction", "")

        # Count allied heroes per lane
        hero_lanes = {"top": 0, "mid": 0, "bot": 0}
        for h in state.get("heroes", []):
            if h.get("faction") == my_faction and h.get("alive") and h.get("name") != self.my_name:
                lane = h.get("lane", "mid")
                if lane in hero_lanes:
                    hero_lanes[lane] += 1

        # Check tower status, prioritize lanes where our tower still stands
        towers = state.get("towers", [])
        our_towers_alive = {"top": False, "mid": False, "bot": False}
        enemy_towers_alive = {"top": False, "mid": False, "bot": False}
        for t in towers:
            if not t.get("alive", False):
                continue
            lane = t.get("lane", "")
            if lane not in our_towers_alive:
                continue
            if t.get("faction") == my_faction:
                our_towers_alive[lane] = True
            else:
                enemy_towers_alive[lane] = True

        # Score each lane (lower = go there)
        best_lane = "mid"
        best_score = 999

        for lane in ["top", "mid", "bot"]:
            score = hero_lanes[lane] * 3  # fewer allies = better

            # Defend lanes where our tower lives
            if our_towers_alive[lane]:
                score -= 1

            # Push lanes where enemy tower already dead
            if not enemy_towers_alive[lane]:
                score -= 2

            # Check frontline pressure from lane data
            lane_data = lanes.get(lane, {})
            enemy_key = "orc" if my_faction == "human" else "human"
            our_units = lane_data.get(my_faction, 0)
            enemy_units = lane_data.get(enemy_key, 0)
            if enemy_units > our_units + 3:
                score -= 2  # lane under pressure
            # Frontline: positive = pushed toward orc, negative = toward human
            frontline = lane_data.get("frontline", 0)
            if my_faction == "human" and frontline < -30:
                score -= 1  # enemy pushing into our side
            elif my_faction == "orc" and frontline > 30:
                score -= 1

            if score < best_score:
                best_score = score
                best_lane = lane

        return best_lane

    def should_recall(self, hero: dict) -> bool:
        """Recall if HP critically low and recall available."""
        hp = hero.get("hp", 0)
        max_hp = hero.get("maxHp", 1)
        cooldown = hero.get("recallCooldownMs", 0)

        if cooldown > 0:
            return False
        if not hero.get("alive", True):
            return False

        ratio = hp / max_hp if max_hp > 0 else 1
        return ratio < 0.25

    def should_ping(self, state: dict, hero: dict) -> str | None:
        """Ping team if lane under heavy pressure. 8s cooldown ~ 160 ticks."""
        if self.tick - self.last_ping_tick < 160:
            return None

        lanes = state.get("lanes", {})
        my_faction = self.my_faction or hero.get("faction", "")
        enemy_key = "orc" if my_faction == "human" else "human"

        worst_lane = None
        worst_diff = 0
        for lane in ["top", "mid", "bot"]:
            ld = lanes.get(lane, {})
            enemy_units = ld.get(enemy_key, 0)
            our_units = ld.get(my_faction, 0)
            diff = enemy_units - our_units
            if diff > worst_diff:
                worst_diff = diff
                worst_lane = lane

        # Also check base HP
        bases = state.get("bases", {})
        our_base = bases.get(my_faction, {})
        base_hp = our_base.get("hp", 1500)
        if base_hp < 500:
            self.last_ping_tick = self.tick
            return "defend"

        if worst_diff > 5 and worst_lane:
            self.last_ping_tick = self.tick
            return worst_lane

        return None

    def run(self):
        print(f"[*] Starting bot | game={self.game} class={self.hero_class}")
        print(f"[*] Polling every {STATE_INTERVAL}s")

        while True:
            try:
                state = get_state(self.game)
                if not state:
                    time.sleep(STATE_INTERVAL)
                    continue

                self.tick = state.get("tick", 0)
                winner = state.get("winner")
                if winner:
                    print(f"[!] Game over. Winner: {winner}")
                    print("[*] Waiting for new game...")
                    time.sleep(30)
                    self.joined = False
                    continue

                payload = {}

                # First deploy: pick class
                if not self.joined:
                    payload["heroClass"] = self.hero_class
                    payload["heroLane"] = "mid"
                    result = deploy(self.api_key, payload)
                    if result:
                        print(f"[+] Joined game {result.get('gameId', self.game)}")
                        self.joined = True
                        self.game = result.get("gameId", self.game)
                        # Try to get our name from config
                        cfg = load_config()
                        self.my_name = cfg.get("agent_name")
                        if result.get("warning"):
                            print(f"[!] Warning: {result['warning']}")
                    time.sleep(STATE_INTERVAL)
                    continue

                hero = self.find_me(state)
                if not hero:
                    # Try to find by scanning all heroes
                    # We may not know our name yet, just keep deploying
                    payload["heroLane"] = "mid"
                    deploy(self.api_key, payload)
                    time.sleep(STATE_INTERVAL)
                    continue

                # Dead? Just wait
                if not hero.get("alive", True):
                    status = f"[.] Dead. Level {hero.get('level', 1)}"
                    print(status)
                    time.sleep(STATE_INTERVAL)
                    continue

                # Build deployment payload
                # Lane choice
                lane = self.pick_lane(state, hero)
                payload["heroLane"] = lane

                # Ability choice
                ability = self.pick_ability(hero)
                if ability:
                    payload["abilityChoice"] = ability
                    print(f"[+] Choosing ability: {ability}")

                # Recall check
                if self.should_recall(hero):
                    payload["action"] = "recall"
                    print(f"[!] Recalling (HP: {hero['hp']}/{hero['maxHp']})")

                # Ping check
                ping = self.should_ping(state, hero)
                if ping:
                    payload["ping"] = ping
                    print(f"[>] Pinging: {ping}")

                # Status message
                hp_pct = int(100 * hero.get("hp", 0) / max(hero.get("maxHp", 1), 1))
                payload["message"] = f"L{hero.get('level', 1)} {lane} {hp_pct}%hp"

                result = deploy(self.api_key, payload)

                # Log
                level = hero.get("level", 1)
                xp = hero.get("xp", 0)
                xp_next = hero.get("xpToNext", 0)
                abilities = [f"{a['id']}:{a['level']}" for a in hero.get("abilities", [])]
                print(
                    f"[tick {self.tick}] L{level} | {lane} | "
                    f"HP:{hero.get('hp', '?')}/{hero.get('maxHp', '?')} | "
                    f"XP:{xp}/{xp_next} | {abilities}"
                )

            except requests.exceptions.RequestException as e:
                print(f"[!] Network error: {e}")
                time.sleep(10)
            except KeyboardInterrupt:
                print("\n[*] Shutting down.")
                sys.exit(0)
            except Exception as e:
                print(f"[!] Error: {e}")
                time.sleep(5)

            time.sleep(STATE_INTERVAL)


def main():
    parser = argparse.ArgumentParser(description="Defense of the Agents Bot")
    parser.add_argument("--register", type=str, help="Register new agent with this name")
    parser.add_argument("--key", type=str, help="API key (overrides saved)")
    parser.add_argument("--game", type=int, default=3, help="Game number (3-5 for agents)")
    parser.add_argument("--class", dest="hero_class", default="mage",
                        choices=["melee", "ranged", "mage"], help="Hero class")
    args = parser.parse_args()

    cfg = load_config()

    if args.register:
        key = register(args.register)
        cfg["api_key"] = key
        cfg["agent_name"] = args.register
        save_config(cfg)
        print(f"[+] Saved key to {CONFIG_FILE}")
    elif args.key:
        cfg["api_key"] = args.key
        save_config(cfg)

    api_key = cfg.get("api_key")
    if not api_key:
        print("[!] No API key. Run with --register <name> or --key <key>")
        sys.exit(1)

    bot = Bot(api_key=api_key, game=args.game, hero_class=args.hero_class)
    bot.run()


if __name__ == "__main__":
    main()
