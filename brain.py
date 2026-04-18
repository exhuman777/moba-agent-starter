#!/usr/bin/env python3
"""Defense of the Agents - Learning Brain
3-layer adaptive intelligence:
  Layer 1: UCB1 lane selection (multi-armed bandit)
  Layer 2: Adaptive recall threshold (self-tuning)
  Layer 3: Behavioral cloning from replay data (neural policy)
"""

from __future__ import annotations
import math
import json
import os
import numpy as np
from collections import defaultdict

DIR = os.path.dirname(os.path.abspath(__file__))
STATS_FILE = os.path.join(DIR, "stats.json")
MODEL_FILE = os.path.join(DIR, "policy_model.json")

LANES = ["top", "mid", "bot"]
TICK_RATE = 20


# ── Layer 1: UCB1 Lane Selection ──────────────────────────

class UCB1LaneSelector:
    """Multi-armed bandit for lane choice.
    Each lane = arm. Reward = XP gained per decision cycle.
    UCB1 balances exploitation (best lane) vs exploration (untried lanes).

    UCB1(lane) = avg_reward + c * sqrt(ln(N) / n_lane)
    c = sqrt(2) ~1.41 (proven optimal)

    Converges to optimal lane in ~50 decisions (2.5 min at 3s poll).
    """

    def __init__(self):
        self.reset()

    def reset(self):
        """Reset for new game."""
        self.counts = {l: 0 for l in LANES}     # times each lane chosen
        self.rewards = {l: 0.0 for l in LANES}  # cumulative XP reward
        self.total = 0
        self._prev_xp = 0
        self._last_lane = "mid"

    def update(self, lane: str, hero_xp_total: int):
        """Call after each decision cycle with current total XP."""
        if self._prev_xp > 0 and lane == self._last_lane:
            xp_delta = max(0, hero_xp_total - self._prev_xp)
            self.rewards[lane] += xp_delta
            self.counts[lane] += 1
            self.total += 1
        self._prev_xp = hero_xp_total
        self._last_lane = lane

    def select(self, available_lanes: list[str] = None, avoid: set[str] = None) -> str:
        """Pick best lane using UCB1."""
        candidates = available_lanes or LANES
        if avoid:
            candidates = [l for l in candidates if l not in avoid]
        if not candidates:
            candidates = LANES

        # Initial exploration: try each lane at least twice
        for lane in candidates:
            if self.counts[lane] < 2:
                return lane

        # UCB1 formula
        best_lane = candidates[0]
        best_score = -999

        for lane in candidates:
            n = self.counts[lane]
            if n == 0:
                return lane  # unexplored

            avg = self.rewards[lane] / n
            exploration = 1.41 * math.sqrt(math.log(self.total) / n)
            score = avg + exploration

            if score > best_score:
                best_score = score
                best_lane = lane

        return best_lane

    def stats(self) -> dict:
        """Return current arm statistics."""
        return {
            lane: {
                "count": self.counts[lane],
                "total_xp": self.rewards[lane],
                "avg_xp": self.rewards[lane] / max(self.counts[lane], 1),
            }
            for lane in LANES
        }


# ── Layer 2: Adaptive Recall Threshold ────────────────────

class AdaptiveRecall:
    """Self-tuning recall threshold.

    threshold = base + death_pressure + level_gap_pressure - aggression_bonus
    Clamp to [0.35, 0.85]

    Dying a lot? -> threshold rises (recall earlier)
    Ahead in KD? -> threshold drops (stay aggressive)
    Enemy outlevels you? -> threshold rises (play safe)
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.recent_deaths = []  # timestamps of recent deaths
        self.total_kills = 0
        self.total_deaths = 0

    def record_death(self, tick: int):
        self.recent_deaths.append(tick)
        self.total_deaths += 1
        # Keep only last 3 minutes of death history
        cutoff = tick - 3 * 60 * TICK_RATE
        self.recent_deaths = [t for t in self.recent_deaths if t > cutoff]

    def record_kill(self):
        self.total_kills += 1

    def threshold(self, my_level: int, enemy_avg_level: float,
                  enemies_in_lane: int, tick: int) -> float:
        """Calculate current recall threshold.
        thebestpizza (#1 human ranked) stays at 40-50% HP without recalling.
        Lower base = more farming time = higher level."""
        # Base: 35% (thebestpizza stays at 40-50%, we recall slightly below)
        base = 0.35

        # Death pressure: +10% per death in last 3 minutes (up to +30%)
        cutoff = tick - 3 * 60 * TICK_RATE
        recent = sum(1 for t in self.recent_deaths if t > cutoff)
        death_pressure = min(0.30, recent * 0.10)

        # Level gap pressure: +5% per level behind (up to +20%)
        gap = max(0, enemy_avg_level - my_level)
        level_pressure = min(0.20, gap * 0.05)

        # Aggression bonus: -5% per KD ratio point above 1.0 (up to -15%)
        kd = self.total_kills / max(self.total_deaths, 1)
        aggression = min(0.15, max(0, (kd - 1.0)) * 0.05)

        # Enemy presence: +5% per enemy hero in lane
        enemy_pressure = enemies_in_lane * 0.05

        threshold = base + death_pressure + level_pressure - aggression + enemy_pressure

        return max(0.35, min(0.85, threshold))

    def stats(self) -> dict:
        return {
            "kills": self.total_kills,
            "deaths": self.total_deaths,
            "kd": self.total_kills / max(self.total_deaths, 1),
            "recent_deaths_3min": len(self.recent_deaths),
        }


# ── Layer 3: Behavioral Cloning Policy ────────────────────

class BehavioralPolicy:
    """Learns lane selection from replay data.

    Simple approach: weighted frequency table.
    For each discretized game state, counts which lane choices
    led to wins vs losses. Picks the lane most correlated with winning.

    State features (discretized):
    - game_phase: early(0-3min) / mid(3-9min) / late(9-15min)
    - level_bucket: low(1-5) / mid(6-10) / high(11+)
    - hp_bucket: critical(<30%) / low(30-60%) / ok(60%+)
    - lane_pressure: which lane has most enemies

    No neural net needed. A lookup table with Laplace smoothing
    outperforms MLPs on <1000 samples per state.
    """

    def __init__(self):
        # state_key -> {lane: {"wins": n, "losses": n}}
        self.table: dict[str, dict[str, dict[str, int]]] = {}
        self.trained = False

    def _state_key(self, game_phase: str, level_bucket: str,
                   hp_bucket: str, pressure_lane: str) -> str:
        return f"{game_phase}|{level_bucket}|{hp_bucket}|{pressure_lane}"

    def _discretize(self, tick: int, level: int, hp_pct: float,
                    lane_enemies: dict[str, int]) -> tuple:
        # Game phase
        secs = tick / TICK_RATE
        if secs < 180:
            phase = "early"
        elif secs < 540:
            phase = "mid"
        else:
            phase = "late"

        # Level bucket
        if level <= 5:
            lvl_b = "low"
        elif level <= 10:
            lvl_b = "mid"
        else:
            lvl_b = "high"

        # HP bucket
        if hp_pct < 0.3:
            hp_b = "critical"
        elif hp_pct < 0.6:
            hp_b = "low"
        else:
            hp_b = "ok"

        # Most pressured lane
        if lane_enemies:
            pressure = max(lane_enemies, key=lambda l: lane_enemies.get(l, 0))
        else:
            pressure = "mid"

        return phase, lvl_b, hp_b, pressure

    def train_from_replays(self):
        """Build policy table from stats.json replay data."""
        if not os.path.exists(STATS_FILE):
            return

        with open(STATS_FILE) as f:
            games = json.load(f)

        self.table = {}

        for game in games:
            bots = game.get("bots", [])
            tick = game.get("tick", 0)
            if not tick:
                dur = game.get("game_time", 0)
                if isinstance(dur, str) and ":" in dur:
                    p = dur.split(":")
                    tick = (int(p[0]) * 60 + int(p[1])) * TICK_RATE
                elif isinstance(dur, (int, float)):
                    tick = int(dur * TICK_RATE)

            for bot in bots:
                if not bot.get("name", "").startswith("Ex"):
                    continue

                level = bot.get("level", 1)
                lane = bot.get("lane", "mid")
                won = bot.get("won", False)
                hp = bot.get("hp", 0)
                maxHp = bot.get("maxHp", 1)
                hp_pct = hp / max(maxHp, 1) if maxHp else 0.5

                # Approximate lane enemies from game context
                # We don't have per-tick data, so use game-level approximation
                phase, lvl_b, hp_b, pressure = self._discretize(
                    tick, level, hp_pct, {"top": 2, "mid": 3, "bot": 2}
                )

                key = self._state_key(phase, lvl_b, hp_b, pressure)
                if key not in self.table:
                    self.table[key] = {l: {"wins": 0, "losses": 0} for l in LANES}

                if lane in self.table[key]:
                    if won:
                        self.table[key][lane]["wins"] += 1
                    else:
                        self.table[key][lane]["losses"] += 1

        self.trained = True
        self._save()

    def _save(self):
        with open(MODEL_FILE, "w") as f:
            json.dump(self.table, f, indent=2)

    def _load(self):
        if os.path.exists(MODEL_FILE):
            with open(MODEL_FILE) as f:
                self.table = json.load(f)
            self.trained = True

    def suggest_lane(self, tick: int, level: int, hp_pct: float,
                     lane_enemies: dict[str, int],
                     avoid: set[str] = None) -> str | None:
        """Suggest best lane based on learned policy. Returns None if no data."""
        if not self.trained:
            self._load()
        if not self.table:
            return None

        phase, lvl_b, hp_b, pressure = self._discretize(
            tick, level, hp_pct, lane_enemies
        )
        key = self._state_key(phase, lvl_b, hp_b, pressure)

        if key not in self.table:
            return None

        # Pick lane with highest win rate (Laplace smoothing)
        best_lane, best_wr = None, -1
        for lane in LANES:
            if avoid and lane in avoid:
                continue
            d = self.table[key].get(lane, {"wins": 0, "losses": 0})
            wr = (d["wins"] + 1) / (d["wins"] + d["losses"] + 2)  # Laplace
            if wr > best_wr:
                best_wr = wr
                best_lane = lane

        return best_lane

    def stats(self) -> dict:
        total_entries = sum(
            sum(d["wins"] + d["losses"] for d in lanes.values())
            for lanes in self.table.values()
        ) if self.table else 0
        return {
            "trained": self.trained,
            "states": len(self.table),
            "total_entries": total_entries,
        }


# ── Combined Brain ────────────────────────────────────────

class LearningBrain:
    """Combines all 3 layers into one decision engine.

    Priority:
    1. UCB1 for real-time lane optimization (adapts within game)
    2. Adaptive recall for survival (self-tunes per game)
    3. Behavioral policy for initial lane suggestion (learned from history)

    The UCB1 overrides behavioral policy after ~50 decisions (2.5 min)
    because it has live data from THIS game. Behavioral policy provides
    a warm start for the first 2.5 minutes.
    """

    def __init__(self):
        self.ucb = UCB1LaneSelector()
        self.recall = AdaptiveRecall()
        self.policy = BehavioralPolicy()
        self.policy.train_from_replays()

    def reset_game(self):
        """Call when new game starts."""
        self.ucb.reset()
        self.recall.reset()

    def pick_lane(self, tick: int, level: int, hp_pct: float,
                  lane_enemies: dict[str, int],
                  avoid: set[str] = None) -> str:
        """Pick lane using layered strategy."""

        # Layer 3: behavioral policy suggestion (warm start)
        bc_suggestion = self.policy.suggest_lane(
            tick, level, hp_pct, lane_enemies, avoid
        )

        # Layer 1: UCB1 (takes over after enough data)
        if self.ucb.total >= 10:
            # UCB1 has enough data, trust it
            return self.ucb.select(avoid=avoid)

        # Early game: use behavioral cloning if available
        if bc_suggestion:
            return bc_suggestion

        # Fallback: UCB1 exploration
        return self.ucb.select(avoid=avoid)

    def update_xp(self, lane: str, hero_xp_total: int):
        """Feed XP data to UCB1 after each decision cycle."""
        self.ucb.update(lane, hero_xp_total)

    def should_recall(self, my_level: int, enemy_avg_level: float,
                      enemies_in_lane: int, hp_pct: float, tick: int) -> bool:
        """Layer 2: adaptive recall decision."""
        threshold = self.recall.threshold(
            my_level, enemy_avg_level, enemies_in_lane, tick
        )
        return hp_pct < threshold

    def get_recall_threshold(self, my_level: int, enemy_avg_level: float,
                             enemies_in_lane: int, tick: int) -> float:
        """Get current threshold (for display)."""
        return self.recall.threshold(my_level, enemy_avg_level, enemies_in_lane, tick)

    def on_death(self, tick: int):
        self.recall.record_death(tick)

    def on_kill(self):
        self.recall.record_kill()

    def retrain(self):
        """Retrain behavioral policy from updated stats."""
        self.policy.train_from_replays()

    def stats(self) -> dict:
        return {
            "ucb1": self.ucb.stats(),
            "recall": self.recall.stats(),
            "policy": self.policy.stats(),
        }


# ── CLI: Train and inspect ─────────────────────────────────

if __name__ == "__main__":
    print("=== LEARNING BRAIN ===")
    print()

    brain = LearningBrain()

    # UCB1 demo
    print("Layer 1: UCB1 Lane Selector")
    print(f"  Status: {brain.ucb.total} decisions made")
    print()

    # Adaptive recall demo
    print("Layer 2: Adaptive Recall")
    for scenario in [
        {"my_level": 5, "enemy_avg": 5, "enemies": 0, "label": "even, safe"},
        {"my_level": 5, "enemy_avg": 8, "enemies": 2, "label": "behind, danger"},
        {"my_level": 10, "enemy_avg": 8, "enemies": 0, "label": "ahead, safe"},
    ]:
        thr = brain.recall.threshold(
            scenario["my_level"], scenario["enemy_avg"],
            scenario["enemies"], 5000
        )
        print(f"  {scenario['label']:20} -> recall at {thr:.0%} HP")
    print()

    # Behavioral cloning
    print("Layer 3: Behavioral Cloning")
    ps = brain.policy.stats()
    print(f"  Trained: {ps['trained']}")
    print(f"  States: {ps['states']}")
    print(f"  Data points: {ps['total_entries']}")

    if ps["trained"]:
        print()
        print("  Lane suggestions by game phase:")
        for phase in ["early", "mid", "late"]:
            for lvl in ["low", "mid", "high"]:
                suggestion = brain.policy.suggest_lane(
                    {"early": 1000, "mid": 5000, "late": 12000}[phase] * TICK_RATE // 60,
                    {"low": 3, "mid": 7, "high": 12}[lvl],
                    0.8,
                    {"top": 2, "mid": 3, "bot": 2}
                )
                if suggestion:
                    print(f"    {phase}/{lvl}: -> {suggestion}")
