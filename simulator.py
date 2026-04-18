#!/usr/bin/env python3
"""Defense of the Agents - Game Simulator
Simulates full games offline to test strategies before deploying.
Models: XP, levels, deaths, towers, lanes, abilities, respawn.
Run: python3 simulator.py
"""

from __future__ import annotations
import random
import math
import json
import os
from dataclasses import dataclass, field

TICK_RATE = 20
GAME_TICKS = 15 * 60 * TICK_RATE  # 15 min
TOWER_BUFF_TICKS = 105 * TICK_RATE

# ── Hero/Unit/Tower Stats ──────────────────────────────────

CLASSES = {
    "melee":  {"hp": 266, "dmg": 25, "range": 40},
    "ranged": {"hp": 168, "dmg": 15, "range": 150},
    "mage":   {"hp": 140, "dmg": 15, "range": 150},
}

TOWER_HP = 1200
TOWER_DMG = 70
BASE_HP = 1500


# ── Simulated Hero ────────────────────────────────────────

@dataclass
class SimHero:
    name: str
    faction: str  # "human" or "orc"
    cls: str
    lane: str
    strategy: str  # "aggressive", "balanced", "defensive", "turtle"
    recall_threshold: float = 0.70

    # State
    level: int = 1
    xp: int = 0
    hp: float = 0
    alive: bool = True
    respawn_timer: int = 0
    recall_cd: int = 0
    kills: int = 0
    deaths: int = 0
    abilities: list = field(default_factory=list)

    # Computed
    _base_hp: float = 0
    _base_dmg: float = 0

    def __post_init__(self):
        base = CLASSES[self.cls]
        self._base_hp = base["hp"]
        self._base_dmg = base["dmg"]
        self.hp = self.max_hp

    @property
    def max_hp(self):
        hp = self._base_hp * (1.15 ** (self.level - 1))
        if "fortitude" in self.abilities:
            hp *= 1 + [0, 0.20, 0.30, 0.40][min(self.abilities.count("fortitude"), 3)]
        return hp

    @property
    def dps(self):
        dmg = self._base_dmg * (1.15 ** (self.level - 1))
        if "fury" in self.abilities:
            dmg *= 1.25
        multi = 1.0
        if "critical_strike" in self.abilities:
            multi *= 1.25  # avg
        if "volley" in self.abilities:
            multi *= 1.5
        spell = 0
        if "fireball" in self.abilities:
            spell += 60 * (1 + 0.025 * self.level) / 4
        if "raise_skeleton" in self.abilities:
            spell += 15  # skeleton DPS averaged
        return dmg * multi + spell

    @property
    def xp_to_next(self):
        return 200 * self.level

    def gain_xp(self, amount):
        self.xp += amount
        while self.xp >= self.xp_to_next:
            self.xp -= self.xp_to_next
            self.level += 1
            # Auto-pick ability every 3 levels
            if self.level % 3 == 0:
                self._pick_ability()

    def _pick_ability(self):
        if self.cls == "mage":
            prio = ["raise_skeleton", "fireball", "fortitude", "tornado"]
        elif self.cls == "melee":
            prio = ["divine_shield", "fortitude", "thorns"]
        else:
            prio = ["critical_strike", "volley", "fortitude"]

        if self.strategy == "aggressive":
            prio = list(reversed(prio))

        self.abilities.append(prio[0] if prio else "fortitude")

    def die(self):
        self.alive = False
        self.deaths += 1
        self.respawn_timer = int(min(30, 3 + 1.5 * self.level) * TICK_RATE)

    def respawn(self):
        self.alive = True
        self.hp = self.max_hp
        self.recall_cd = 0

    def should_recall(self, enemies_in_lane: int) -> bool:
        if self.recall_cd > 0 or not self.alive:
            return False
        thr = self.recall_threshold
        thr += min(0.20, self.level * 0.015)
        thr += 0.05 * enemies_in_lane
        thr = min(thr, 0.90)
        return (self.hp / self.max_hp) < thr

    def recall(self):
        self.hp = self.max_hp
        self.recall_cd = 120 * TICK_RATE


# ── Simulated Lane ─────────────────────────────────────────

@dataclass
class SimLane:
    name: str
    human_units: int = 10
    orc_units: int = 10
    human_tower_hp: float = TOWER_HP
    orc_tower_hp: float = TOWER_HP
    frontline: float = 0  # -100 to +100

    def spawn_tick(self, tick):
        # Melee every 2.5s, ranged every 7s
        if tick % (int(2.5 * TICK_RATE)) == 0:
            self.human_units += 1
            self.orc_units += 1
        if tick % (int(7 * TICK_RATE)) == 0:
            self.human_units += 1
            self.orc_units += 1


# ── Game Simulator ─────────────────────────────────────────

class GameSim:
    def __init__(self, human_heroes: list[SimHero], orc_heroes: list[SimHero]):
        self.heroes = {"human": human_heroes, "orc": orc_heroes}
        self.lanes = {
            "top": SimLane("top"),
            "mid": SimLane("mid"),
            "bot": SimLane("bot"),
        }
        self.human_base = BASE_HP
        self.orc_base = BASE_HP
        self.tick = 0
        self.winner = None
        self.log: list[str] = []

    def _log(self, msg):
        t = self.tick / TICK_RATE
        self.log.append(f"[{int(t)//60}:{int(t)%60:02d}] {msg}")

    def run(self, verbose=False) -> dict:
        """Run full game simulation. Returns result dict."""
        while self.tick < GAME_TICKS and not self.winner:
            self._tick()
            self.tick += 1

        # Sudden death: base with lower HP loses
        if not self.winner:
            if self.human_base <= self.orc_base:
                self.winner = "orc"
            else:
                self.winner = "human"
            self._log(f"Sudden death: {self.winner} wins")

        if verbose:
            for line in self.log[-20:]:
                print(line)

        return self._result()

    def _tick(self):
        # Spawn units
        for lane in self.lanes.values():
            lane.spawn_tick(self.tick)

        # Process every 1 second (20 ticks)
        if self.tick % TICK_RATE != 0:
            return

        tower_buffed = self.tick < TOWER_BUFF_TICKS

        for lane_name, lane in self.lanes.items():
            h_heroes = [h for h in self.heroes["human"] if h.lane == lane_name and h.alive]
            o_heroes = [h for h in self.heroes["orc"] if h.lane == lane_name and h.alive]

            # Unit combat (simplified: units trade based on numbers)
            if lane.human_units > 0 and lane.orc_units > 0:
                # Each unit does ~10 dmg/s, kills at 95 HP
                h_kills = min(lane.orc_units, max(1, lane.human_units // 4))
                o_kills = min(lane.human_units, max(1, lane.orc_units // 4))
                lane.orc_units = max(0, lane.orc_units - h_kills)
                lane.human_units = max(0, lane.human_units - o_kills)

            # XP from unit kills (heroes in lane get XP)
            for h in h_heroes:
                if lane.orc_units > 0:
                    h.gain_xp(12)  # ~50 XP per kill, distributed
            for h in o_heroes:
                if lane.human_units > 0:
                    h.gain_xp(12)

            # Hero vs hero combat
            for attacker_list, defender_list in [(h_heroes, o_heroes), (o_heroes, h_heroes)]:
                if not attacker_list or not defender_list:
                    continue
                total_dps = sum(h.dps for h in attacker_list)
                # Distribute damage across defenders
                for defender in defender_list:
                    dmg = total_dps / len(defender_list)
                    # Strategy modifier
                    if defender.strategy == "turtle":
                        dmg *= 0.6  # tower protection reduces incoming
                    elif defender.strategy == "defensive":
                        dmg *= 0.8
                    defender.hp -= dmg

                    if defender.hp <= 0:
                        defender.die()
                        kill_xp = 200 + max(0, (defender.level - 1)) * 10
                        for a in attacker_list:
                            a.gain_xp(kill_xp // len(attacker_list))
                            a.kills += 1
                        self._log(f"{defender.name}(L{defender.level}) killed in {lane_name}")

            # Frontline movement
            h_power = lane.human_units * 10 + sum(h.dps for h in h_heroes)
            o_power = lane.orc_units * 10 + sum(h.dps for h in o_heroes)
            if h_power + o_power > 0:
                lane.frontline += (h_power - o_power) / (h_power + o_power) * 2
                lane.frontline = max(-100, min(100, lane.frontline))

            # Tower damage
            tower_dmg_multi = 2 if tower_buffed else 1
            tower_dr = 0.5 if tower_buffed else 1.0

            # Human tower attacks orc if frontline < -30
            if lane.frontline < -30 and lane.orc_tower_hp > 0:
                for h in h_heroes:
                    lane.orc_tower_hp -= h.dps * 0.3 * tower_dr  # heroes do reduced to towers
                lane.orc_tower_hp -= lane.human_units * 2 * tower_dr
                # Tower hits back
                if o_heroes:
                    o_heroes[0].hp -= TOWER_DMG * tower_dmg_multi * 0.5
                elif lane.human_units > 0:
                    lane.human_units = max(0, lane.human_units - 1)

            if lane.frontline > 30 and lane.human_tower_hp > 0:
                for h in o_heroes:
                    lane.human_tower_hp -= h.dps * 0.3 * tower_dr
                lane.human_tower_hp -= lane.orc_units * 2 * tower_dr
                if h_heroes:
                    h_heroes[0].hp -= TOWER_DMG * tower_dmg_multi * 0.5
                elif lane.orc_units > 0:
                    lane.orc_units = max(0, lane.orc_units - 1)

            lane.orc_tower_hp = max(0, lane.orc_tower_hp)
            lane.human_tower_hp = max(0, lane.human_tower_hp)

            if lane.orc_tower_hp <= 0 and lane.frontline < -50:
                self._log(f"Orc tower {lane_name} destroyed")
                lane.orc_tower_hp = 0

            if lane.human_tower_hp <= 0 and lane.frontline > 50:
                self._log(f"Human tower {lane_name} destroyed")
                lane.human_tower_hp = 0

        # Base damage (if all towers in a path are down)
        for faction, base_attr, tower_check, attacker_faction in [
            ("human", "human_base", lambda l: l.human_tower_hp <= 0, "orc"),
            ("orc", "orc_base", lambda l: l.orc_tower_hp <= 0, "human"),
        ]:
            exposed_lanes = [l for l in self.lanes.values() if tower_check(l)]
            if len(exposed_lanes) >= 1:
                for lane in exposed_lanes:
                    attackers = [h for h in self.heroes[attacker_faction] if h.lane == lane.name and h.alive]
                    dmg = sum(h.dps * 0.2 for h in attackers)
                    if faction == "human":
                        self.human_base -= dmg
                    else:
                        self.orc_base -= dmg

        if self.human_base <= 0:
            self.winner = "orc"
            self._log("Orc wins! Human base destroyed.")
        elif self.orc_base <= 0:
            self.winner = "human"
            self._log("Human wins! Orc base destroyed.")

        # Respawn / recall processing
        for faction_heroes in self.heroes.values():
            for h in faction_heroes:
                if not h.alive:
                    h.respawn_timer -= TICK_RATE
                    if h.respawn_timer <= 0:
                        h.respawn()
                else:
                    if h.recall_cd > 0:
                        h.recall_cd -= TICK_RATE
                    enemies_in_lane = len([e for e in self.heroes[
                        "orc" if h.faction == "human" else "human"
                    ] if e.lane == h.lane and e.alive])
                    if h.should_recall(enemies_in_lane):
                        h.recall()

    def _result(self):
        all_heroes = self.heroes["human"] + self.heroes["orc"]
        return {
            "winner": self.winner,
            "ticks": self.tick,
            "duration_sec": self.tick / TICK_RATE,
            "human_base": max(0, self.human_base),
            "orc_base": max(0, self.orc_base),
            "heroes": [{
                "name": h.name, "faction": h.faction, "class": h.cls,
                "strategy": h.strategy, "level": h.level,
                "kills": h.kills, "deaths": h.deaths,
                "kd": h.kills / max(h.deaths, 1),
                "recall_threshold": h.recall_threshold,
            } for h in all_heroes],
            "towers": {
                lane_name: {
                    "human": max(0, lane.human_tower_hp),
                    "orc": max(0, lane.orc_tower_hp),
                } for lane_name, lane in self.lanes.items()
            },
        }


# ── Strategy Presets ───────────────────────────────────────

def make_our_bots(faction="human", strategy="defensive", recall_thr=0.70):
    """Create our 3-bot squad."""
    return [
        SimHero("ExHuman777", faction, "mage", "top", strategy, recall_thr),
        SimHero("ExH_Mage3", faction, "mage", "mid", strategy, recall_thr),
        SimHero("ExH_Tank2", faction, "melee", "bot", strategy, recall_thr),
    ]

def make_enemies(faction="orc", count=7, strategy="balanced"):
    """Create enemy players (mix of classes)."""
    classes = ["mage", "ranged", "melee", "ranged", "mage", "ranged", "melee",
               "ranged", "mage", "ranged"]
    lanes = ["top", "mid", "bot", "mid", "top", "bot", "mid", "top", "bot", "mid"]
    bots = []
    for i in range(count):
        bots.append(SimHero(
            f"Enemy_{i}", faction, classes[i % len(classes)],
            lanes[i % len(lanes)], strategy, 0.50,
        ))
    return bots


def run_simulation(our_strategy="defensive", our_recall=0.70,
                   enemy_strategy="balanced", enemy_recall=0.50,
                   enemy_count=7, games=100, our_faction="human"):
    """Run N games and return aggregate stats."""
    results = {"wins": 0, "losses": 0, "our_avg_level": 0, "our_avg_kd": 0,
               "our_avg_deaths": 0, "enemy_avg_level": 0}

    enemy_faction = "orc" if our_faction == "human" else "human"

    for _ in range(games):
        ours = make_our_bots(our_faction, our_strategy, our_recall)
        enemies = make_enemies(enemy_faction, enemy_count, enemy_strategy)

        sim = GameSim(
            ours if our_faction == "human" else enemies,
            enemies if our_faction == "human" else ours,
        )
        r = sim.run()

        if r["winner"] == our_faction:
            results["wins"] += 1
        else:
            results["losses"] += 1

        our_heroes = [h for h in r["heroes"] if h["name"].startswith("Ex")]
        enemy_heroes = [h for h in r["heroes"] if h["name"].startswith("Enemy")]

        results["our_avg_level"] += sum(h["level"] for h in our_heroes) / len(our_heroes)
        results["our_avg_kd"] += sum(h["kd"] for h in our_heroes) / len(our_heroes)
        results["our_avg_deaths"] += sum(h["deaths"] for h in our_heroes) / len(our_heroes)
        results["enemy_avg_level"] += sum(h["level"] for h in enemy_heroes) / len(enemy_heroes)

    n = games
    results["our_avg_level"] /= n
    results["our_avg_kd"] /= n
    results["our_avg_deaths"] /= n
    results["enemy_avg_level"] /= n
    results["winrate"] = 100 * results["wins"] / n

    return results


# ── Main: Test All Strategies ──────────────────────────────

def main():
    print("=" * 70)
    print("DEFENSE OF THE AGENTS - STRATEGY SIMULATOR")
    print("=" * 70)
    print()
    print("Simulating 100 games per strategy combo...")
    print("Our squad: 2 mage + 1 melee (3 bots) vs 7 enemies")
    print()

    # Test different strategies
    strategies = [
        ("aggressive", 0.40),
        ("balanced", 0.55),
        ("defensive", 0.70),
        ("turtle", 0.85),
    ]

    print(f"{'Our Strategy':15} {'Recall':>7} {'WR':>5} {'Avg Lv':>7} {'KD':>5} {'Deaths':>7} {'E.Lv':>5}")
    print("-" * 60)

    for strat, recall in strategies:
        r = run_simulation(strat, recall, "balanced", 0.50, 7, 100)
        print(f"{strat:15} {recall:>6.0%} {r['winrate']:>4.0f}% "
              f"{r['our_avg_level']:>6.1f} {r['our_avg_kd']:>5.1f} "
              f"{r['our_avg_deaths']:>6.1f} {r['enemy_avg_level']:>5.1f}")

    print()
    print("--- Recall Threshold Sweep (defensive strategy) ---")
    print(f"{'Recall%':>8} {'WR':>5} {'Avg Lv':>7} {'KD':>5} {'Deaths':>7}")
    print("-" * 40)

    for recall_pct in [40, 50, 60, 70, 75, 80, 85, 90]:
        r = run_simulation("defensive", recall_pct / 100, "balanced", 0.50, 7, 100)
        print(f"{recall_pct:>7}% {r['winrate']:>4.0f}% "
              f"{r['our_avg_level']:>6.1f} {r['our_avg_kd']:>5.1f} "
              f"{r['our_avg_deaths']:>6.1f}")

    print()
    print("--- Enemy Count Impact (defensive, 70% recall) ---")
    print(f"{'Enemies':>8} {'WR':>5} {'Avg Lv':>7} {'KD':>5} {'Deaths':>7}")
    print("-" * 40)

    for enemy_count in [3, 5, 7, 9]:
        r = run_simulation("defensive", 0.70, "balanced", 0.50, enemy_count, 100)
        print(f"{enemy_count:>8} {r['winrate']:>4.0f}% "
              f"{r['our_avg_level']:>6.1f} {r['our_avg_kd']:>5.1f} "
              f"{r['our_avg_deaths']:>6.1f}")

    print()
    print("--- Our Comp Test (defensive, 70% recall vs 7 balanced enemies) ---")
    print()

    # Test different comps
    comps = [
        ("3 mage", [("mage","top"), ("mage","mid"), ("mage","bot")]),
        ("2M+1T (current)", [("mage","top"), ("mage","mid"), ("melee","bot")]),
        ("1M+2T", [("mage","mid"), ("melee","top"), ("melee","bot")]),
        ("2M+1R", [("mage","top"), ("mage","mid"), ("ranged","bot")]),
        ("1M+1T+1R", [("mage","mid"), ("melee","top"), ("ranged","bot")]),
    ]

    print(f"{'Comp':15} {'WR':>5} {'Avg Lv':>7} {'KD':>5} {'Deaths':>7}")
    print("-" * 45)

    for comp_name, comp_def in comps:
        wins, total_lvl, total_kd, total_d = 0, 0, 0, 0
        n = 100
        for _ in range(n):
            ours = []
            for i, (cls, lane) in enumerate(comp_def):
                ours.append(SimHero(f"Bot{i}", "human", cls, lane, "defensive", 0.70))
            enemies = make_enemies("orc", 7, "balanced")
            sim = GameSim(ours, enemies)
            r = sim.run()
            if r["winner"] == "human":
                wins += 1
            bots = [h for h in r["heroes"] if h["name"].startswith("Bot")]
            total_lvl += sum(h["level"] for h in bots) / len(bots)
            total_kd += sum(h["kd"] for h in bots) / len(bots)
            total_d += sum(h["deaths"] for h in bots) / len(bots)

        print(f"{comp_name:15} {100*wins/n:>4.0f}% {total_lvl/n:>6.1f} "
              f"{total_kd/n:>5.1f} {total_d/n:>6.1f}")

    print()
    print("--- Single Game Verbose (defensive vs balanced) ---")
    print()
    ours = make_our_bots("human", "defensive", 0.70)
    enemies = make_enemies("orc", 7, "balanced")
    sim = GameSim(ours, enemies)
    r = sim.run(verbose=True)
    print()
    print(f"Winner: {r['winner']}  Duration: {r['duration_sec']:.0f}s")
    print(f"{'Name':15} {'Faction':7} {'Class':6} {'Lv':>3} {'K':>4} {'D':>4} {'KD':>5}")
    for h in r["heroes"]:
        print(f"{h['name']:15} {h['faction']:7} {h['class']:6} {h['level']:3} "
              f"{h['kills']:4} {h['deaths']:4} {h['kd']:5.1f}")


if __name__ == "__main__":
    main()
