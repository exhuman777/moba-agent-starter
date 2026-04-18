#!/usr/bin/env python3
"""Defense of the Agents - Reinforcement Learning Engine
Reward-shaped Q-learning with experience replay.
Learns optimal (state, action) mapping from live gameplay.

Inspired by:
- DQN reward shaping (OpenAI Five: dense rewards >> sparse win/loss)
- Karpathy's "train on what matters" principle
- Thompson Sampling for exploration with uncertainty

Run standalone: python3 rl_engine.py (analyzes replay data)
"""

from __future__ import annotations
import json
import os
import math
import random
import numpy as np
from collections import defaultdict
from datetime import datetime

DIR = os.path.dirname(os.path.abspath(__file__))
STATS_FILE = os.path.join(DIR, "stats.json")
Q_TABLE_FILE = os.path.join(DIR, "q_table.json")
REWARD_LOG_FILE = os.path.join(DIR, "reward_log.json")

TICK_RATE = 20
LANES = ["top", "mid", "bot"]


# ── Reward Function ───────────────────────────────────────

class RewardCalculator:
    """Dense reward signal from game state deltas.

    OpenAI Five insight: sparse win/loss gives 1 bit per 15-min game.
    Dense rewards give feedback EVERY decision cycle (3 seconds).

    Reward components (all normalized to roughly [-1, +1] range):
      +XP gained (primary driver of leveling)
      +Level up bonus (power spike reward)
      +Survival bonus (staying alive = farming time)
      -Death penalty (scaled by level, higher = worse)
      -HP loss penalty (taking damage = approaching death)
      +Kill bonus (hero kills give team advantage)
      +Tower damage (progressing toward win)
      -Tower lost (defensive failure)

    Weights tuned for XP-maximization goal (reaching highest level).
    """

    # Reward weights (tune these to change bot behavior)
    WEIGHTS = {
        "xp_gain":        1.0,   # XP gained this cycle (normalized by level)
        "level_up":       5.0,   # Bonus for leveling up
        "alive_tick":     0.1,   # Small reward for staying alive each cycle
        "death":         -8.0,   # Penalty for dying (scaled by level)
        "fast_death":   -12.0,   # Extra penalty for dying within 30s of last death
        "hp_loss":       -0.3,   # Penalty per 10% HP lost
        "hp_recovery":    0.2,   # Reward for HP recovery (successful recall)
        "kill":           2.0,   # Reward for getting a kill
        "multi_kill":     4.0,   # Bonus for kills while already ahead in KD
        "tower_damage":   1.5,   # Enemy tower took damage
        "tower_lost":    -3.0,   # Our tower took damage
        "solo_lane":      0.5,   # Bonus for being in a solo lane (more XP)
        "crowded_lane":  -0.3,   # Penalty for sharing lane with 3+ allies
    }

    def __init__(self):
        self.prev_state = None
        self.prev_tick = 0
        self.last_death_tick = -9999
        self.cumulative_reward = 0
        self.reward_history: list[dict] = []

    def reset(self):
        self.prev_state = None
        self.prev_tick = 0
        self.last_death_tick = -9999
        self.cumulative_reward = 0

    def calculate(self, hero: dict, state: dict, faction: str,
                  lane: str, prev_hero: dict | None) -> float:
        """Calculate reward for this decision cycle."""
        if not prev_hero:
            self.prev_state = hero
            self.prev_tick = state.get("tick", 0)
            return 0

        tick = state.get("tick", 0)
        reward = 0
        breakdown = {}
        W = self.WEIGHTS

        # 1. XP gain (most important signal)
        level = hero.get("level", 1)
        prev_level = prev_hero.get("level", 1)
        xp = hero.get("xp", 0)
        prev_xp = prev_hero.get("xp", 0)
        xp_delta = xp - prev_xp
        if level > prev_level:
            xp_delta += prev_hero.get("xpToNext", 200)  # account for level rollover
        normalized_xp = xp_delta / max(100, 50 * level)  # normalize by expected XP
        r = normalized_xp * W["xp_gain"]
        reward += r
        breakdown["xp_gain"] = r

        # 2. Level up bonus
        if level > prev_level:
            r = W["level_up"] * (level - prev_level)
            reward += r
            breakdown["level_up"] = r

        # 3. Alive tick bonus
        if hero.get("alive"):
            r = W["alive_tick"]
            reward += r
            breakdown["alive_tick"] = r

        # 4. Death penalty
        was_alive = prev_hero.get("alive", True)
        now_alive = hero.get("alive", True)
        if was_alive and not now_alive:
            # Scale by level: dying at L10 is 3x worse than L1
            level_multiplier = 1 + (level - 1) * 0.2
            r = W["death"] * level_multiplier
            reward += r
            breakdown["death"] = r

            # 5. Fast death penalty (died within 30s of last death)
            if tick - self.last_death_tick < 30 * TICK_RATE:
                r = W["fast_death"]
                reward += r
                breakdown["fast_death"] = r
            self.last_death_tick = tick

        # 6. HP loss/recovery
        if hero.get("alive") and prev_hero.get("alive"):
            max_hp = hero.get("maxHp", 1)
            hp_pct = hero.get("hp", 0) / max(max_hp, 1)
            prev_hp_pct = prev_hero.get("hp", 0) / max(prev_hero.get("maxHp", 1), 1)
            hp_change = hp_pct - prev_hp_pct

            if hp_change < -0.1:  # lost >10% HP
                r = W["hp_loss"] * abs(hp_change) * 10
                reward += r
                breakdown["hp_loss"] = r
            elif hp_change > 0.3:  # recovered >30% (recall)
                r = W["hp_recovery"]
                reward += r
                breakdown["hp_recovery"] = r

        # 7. Kill detection (XP jump > 150 suggests hero kill)
        if xp_delta > 150 and hero.get("alive"):
            r = W["kill"]
            reward += r
            breakdown["kill"] = r

        # 8. Solo lane bonus
        enemy = "orc" if faction == "human" else "human"
        allies_in_lane = sum(1 for h in state.get("heroes", [])
                             if h.get("lane") == lane and h.get("faction") == faction
                             and h.get("alive") and h.get("name") != hero.get("name"))
        if allies_in_lane == 0:
            r = W["solo_lane"]
            reward += r
            breakdown["solo_lane"] = r
        elif allies_in_lane >= 3:
            r = W["crowded_lane"]
            reward += r
            breakdown["crowded_lane"] = r

        # 9. Tower state changes
        for tower in state.get("towers", []):
            if tower["lane"] != lane:
                continue
            if tower["faction"] == faction and not tower.get("alive"):
                r = W["tower_lost"]
                reward += r
                breakdown["tower_lost"] = r
                break
            elif tower["faction"] == enemy and tower.get("hp", 1200) < 1000:
                r = W["tower_damage"] * (1 - tower["hp"] / 1200)
                reward += r
                breakdown["tower_damage"] = r
                break

        self.cumulative_reward += reward
        self.reward_history.append({
            "tick": tick,
            "reward": round(reward, 3),
            "breakdown": {k: round(v, 3) for k, v in breakdown.items()},
            "level": level,
            "lane": lane,
            "alive": hero.get("alive", True),
        })

        # Keep last 500 entries
        if len(self.reward_history) > 500:
            self.reward_history = self.reward_history[-500:]

        self.prev_state = hero
        self.prev_tick = tick
        return reward


# ── State Discretizer ─────────────────────────────────────

def discretize_state(tick: int, level: int, hp_pct: float,
                     allies_in_lane: int, enemies_in_lane: int,
                     tower_alive: bool, kd_ratio: float) -> str:
    """Convert continuous state to discrete key for Q-table.

    State space: 3 * 3 * 3 * 3 * 3 * 2 * 3 = 1,458 states
    Small enough for tabular Q-learning. Large enough to capture
    meaningful game situations.
    """
    # Game phase (3 buckets)
    secs = tick / TICK_RATE
    if secs < 180:
        phase = "early"
    elif secs < 540:
        phase = "mid"
    else:
        phase = "late"

    # Level bucket (3)
    if level <= 4:
        lvl = "low"
    elif level <= 8:
        lvl = "mid"
    else:
        lvl = "high"

    # HP bucket (3)
    if hp_pct < 0.35:
        hp = "crit"
    elif hp_pct < 0.65:
        hp = "low"
    else:
        hp = "ok"

    # Lane company (3)
    if allies_in_lane == 0:
        company = "solo"
    elif allies_in_lane <= 2:
        company = "small"
    else:
        company = "crowd"

    # Threat level (3)
    if enemies_in_lane == 0:
        threat = "safe"
    elif enemies_in_lane <= 2:
        threat = "contested"
    else:
        threat = "danger"

    # Tower (2)
    tower = "up" if tower_alive else "down"

    # KD momentum (3)
    if kd_ratio > 1.5:
        momentum = "winning"
    elif kd_ratio > 0.8:
        momentum = "even"
    else:
        momentum = "losing"

    return f"{phase}|{lvl}|{hp}|{company}|{threat}|{tower}|{momentum}"


# ── Q-Learning Agent ──────────────────────────────────────

class QLearningAgent:
    """Tabular Q-learning with experience replay.

    Q(s,a) <- Q(s,a) + alpha * (reward + gamma * max_a' Q(s',a') - Q(s,a))

    Actions: stay_in_lane, switch_top, switch_mid, switch_bot, recall
    """

    ACTIONS = ["stay", "top", "mid", "bot", "recall"]

    def __init__(self, alpha=0.1, gamma=0.95, epsilon=0.15):
        self.alpha = alpha      # learning rate
        self.gamma = gamma      # discount factor
        self.epsilon = epsilon  # exploration rate (decays)
        self.q_table: dict[str, dict[str, float]] = {}
        self.experience_buffer: list[tuple] = []
        self.total_updates = 0
        self._load()

    def _load(self):
        if os.path.exists(Q_TABLE_FILE):
            with open(Q_TABLE_FILE) as f:
                data = json.load(f)
                self.q_table = data.get("q_table", {})
                self.total_updates = data.get("total_updates", 0)

    def save(self):
        with open(Q_TABLE_FILE, "w") as f:
            json.dump({
                "q_table": self.q_table,
                "total_updates": self.total_updates,
                "saved_at": datetime.now().isoformat(),
                "states_learned": len(self.q_table),
            }, f, indent=2)

    def get_q(self, state: str, action: str) -> float:
        return self.q_table.get(state, {}).get(action, 0.0)

    def get_best_action(self, state: str, available_actions: list[str] = None) -> str:
        """Epsilon-greedy action selection."""
        actions = available_actions or self.ACTIONS

        # Exploration (decreasing over time)
        effective_epsilon = self.epsilon * max(0.1, 1 - self.total_updates / 10000)
        if random.random() < effective_epsilon:
            return random.choice(actions)

        # Exploitation
        best_action = actions[0]
        best_q = self.get_q(state, actions[0])
        for action in actions[1:]:
            q = self.get_q(state, action)
            if q > best_q:
                best_q = q
                best_action = action
        return best_action

    def update(self, state: str, action: str, reward: float, next_state: str):
        """Q-learning update."""
        if state not in self.q_table:
            self.q_table[state] = {a: 0.0 for a in self.ACTIONS}
        if next_state not in self.q_table:
            self.q_table[next_state] = {a: 0.0 for a in self.ACTIONS}

        current_q = self.q_table[state].get(action, 0.0)
        max_next_q = max(self.q_table[next_state].values())

        new_q = current_q + self.alpha * (reward + self.gamma * max_next_q - current_q)
        self.q_table[state][action] = round(new_q, 4)

        self.total_updates += 1

        # Experience replay: store and replay
        self.experience_buffer.append((state, action, reward, next_state))
        if len(self.experience_buffer) > 5000:
            self.experience_buffer = self.experience_buffer[-5000:]

        # Replay 5 random past experiences each update
        if len(self.experience_buffer) >= 10:
            for _ in range(5):
                s, a, r, ns = random.choice(self.experience_buffer)
                if s in self.q_table and ns in self.q_table:
                    cq = self.q_table[s].get(a, 0.0)
                    mnq = max(self.q_table[ns].values())
                    self.q_table[s][a] = round(cq + self.alpha * (r + self.gamma * mnq - cq), 4)

    def suggest_action(self, tick: int, level: int, hp_pct: float,
                       allies: int, enemies: int, tower_alive: bool,
                       kd: float, current_lane: str,
                       avoid_lanes: set[str] = None) -> str:
        """Get action recommendation from Q-table."""
        state = discretize_state(tick, level, hp_pct, allies, enemies, tower_alive, kd)

        available = list(self.ACTIONS)
        if avoid_lanes:
            available = [a for a in available if a not in avoid_lanes]

        action = self.get_best_action(state, available)

        if action == "stay":
            return current_lane
        elif action == "recall":
            return "recall"
        else:
            return action  # top/mid/bot

    def stats(self) -> dict:
        """Return learning statistics."""
        if not self.q_table:
            return {"states": 0, "updates": 0, "exploration": self.epsilon}

        # Find states with clear preferences
        decisive_states = 0
        for state, actions in self.q_table.items():
            values = list(actions.values())
            if max(values) - min(values) > 1.0:
                decisive_states += 1

        return {
            "states_learned": len(self.q_table),
            "total_updates": self.total_updates,
            "decisive_states": decisive_states,
            "exploration_rate": f"{self.epsilon * max(0.1, 1 - self.total_updates/10000):.1%}",
            "buffer_size": len(self.experience_buffer),
        }

    def top_actions(self, n=10) -> list[dict]:
        """Show states with strongest learned preferences."""
        results = []
        for state, actions in self.q_table.items():
            best_action = max(actions, key=actions.get)
            best_q = actions[best_action]
            worst_q = min(actions.values())
            if best_q - worst_q > 0.5:
                results.append({
                    "state": state,
                    "best_action": best_action,
                    "q_value": best_q,
                    "confidence": best_q - worst_q,
                })
        results.sort(key=lambda x: -x["confidence"])
        return results[:n]


# ── Integrated RL Brain ───────────────────────────────────

class RLBrain:
    """Combines reward calculator + Q-learning agent.
    Plugs into BotBrain for real-time decision making.
    """

    def __init__(self):
        self.reward_calc = RewardCalculator()
        self.q_agent = QLearningAgent()
        self._prev_state_key = None
        self._prev_action = None
        self._game_rewards: list[float] = []

    def reset_game(self):
        self.reward_calc.reset()
        self._prev_state_key = None
        self._prev_action = None
        self._game_rewards = []

    def decide_and_learn(self, hero: dict, state: dict, faction: str,
                         current_lane: str, allies: int, enemies: int,
                         tower_alive: bool, kd: float, prev_hero: dict,
                         avoid_lanes: set[str] = None) -> tuple[str, float]:
        """Make a decision AND learn from the outcome of the previous decision.
        Returns (action, reward_this_cycle).
        """
        tick = state.get("tick", 0)
        level = hero.get("level", 1)
        hp_pct = hero.get("hp", 0) / max(hero.get("maxHp", 1), 1)

        # Calculate reward for previous action
        reward = self.reward_calc.calculate(hero, state, faction, current_lane, prev_hero)
        self._game_rewards.append(reward)

        # Current state
        state_key = discretize_state(tick, level, hp_pct, allies, enemies, tower_alive, kd)

        # Learn from previous (state, action, reward, new_state)
        if self._prev_state_key and self._prev_action:
            self.q_agent.update(self._prev_state_key, self._prev_action, reward, state_key)

        # Get new action
        action = self.q_agent.suggest_action(
            tick, level, hp_pct, allies, enemies, tower_alive, kd,
            current_lane, avoid_lanes
        )

        self._prev_state_key = state_key
        self._prev_action = action if action != "recall" else "stay"

        return action, reward

    def end_game(self, won: bool):
        """Apply final win/loss reward and save."""
        terminal_reward = 10.0 if won else -5.0
        if self._prev_state_key and self._prev_action:
            self.q_agent.update(self._prev_state_key, self._prev_action,
                                terminal_reward, self._prev_state_key)
        self.q_agent.save()

    def get_reward_summary(self) -> dict:
        if not self._game_rewards:
            return {"total": 0, "avg": 0, "positive": 0, "negative": 0}
        return {
            "total": round(sum(self._game_rewards), 1),
            "avg": round(sum(self._game_rewards) / len(self._game_rewards), 3),
            "positive": sum(1 for r in self._game_rewards if r > 0),
            "negative": sum(1 for r in self._game_rewards if r < 0),
            "cycles": len(self._game_rewards),
        }

    def stats(self) -> dict:
        return {
            "q_learning": self.q_agent.stats(),
            "reward_summary": self.get_reward_summary(),
        }


# ── CLI: Analyze and Train ────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("REINFORCEMENT LEARNING ENGINE")
    print("=" * 60)
    print()

    rl = RLBrain()

    # Show reward weights
    print("REWARD WEIGHTS (tune to change behavior):")
    for k, v in RewardCalculator.WEIGHTS.items():
        direction = "+" if v > 0 else ""
        color_hint = "reward" if v > 0 else "penalty"
        print(f"  {k:18} {direction}{v:5.1f}  ({color_hint})")
    print()

    # Show Q-learning stats
    qs = rl.q_agent.stats()
    print("Q-LEARNING STATUS:")
    for k, v in qs.items():
        print(f"  {k}: {v}")
    print()

    # Show top learned actions
    top = rl.q_agent.top_actions(10)
    if top:
        print("TOP LEARNED STATE->ACTION MAPPINGS:")
        for entry in top:
            print(f"  {entry['state']}")
            print(f"    -> {entry['best_action']} (Q={entry['q_value']:.2f}, confidence={entry['confidence']:.2f})")
        print()

    # Simulate training from replay data
    print("TRAINING FROM REPLAYS...")
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE) as f:
            games = json.load(f)

        trained = 0
        for game in games:
            tick = game.get("tick", 0)
            if not tick:
                dur = game.get("game_time", 0)
                if isinstance(dur, str) and ":" in dur:
                    p = dur.split(":")
                    tick = (int(p[0]) * 60 + int(p[1])) * TICK_RATE
                elif isinstance(dur, (int, float)):
                    tick = int(dur * TICK_RATE)

            for bot in game.get("bots", []):
                if not bot.get("name", "").startswith("Ex"):
                    continue

                level = bot.get("level", 1)
                deaths = bot.get("deaths", 0)
                kills = bot.get("kills_est", 0)
                won = bot.get("won", False)
                lane = bot.get("lane", "mid")
                kd = kills / max(deaths, 1)

                # Create synthetic state transitions
                for phase_tick in range(0, tick, 60 * TICK_RATE):
                    frac = phase_tick / max(tick, 1)
                    est_level = max(1, int(level * frac))
                    est_hp = 0.7 if deaths < 5 else 0.4

                    state = discretize_state(
                        phase_tick, est_level, est_hp,
                        2, 2, frac < 0.7, kd
                    )

                    # Reward approximation
                    if won:
                        reward = 0.5 + kd * 0.3 - deaths * 0.1
                    else:
                        reward = -0.3 + kd * 0.2 - deaths * 0.15

                    next_state = discretize_state(
                        min(phase_tick + 60 * TICK_RATE, tick),
                        min(est_level + 1, level), est_hp,
                        2, 2, frac < 0.6, kd
                    )

                    rl.q_agent.update(state, lane, reward, next_state)
                    trained += 1

                # Terminal reward
                term_state = discretize_state(tick, level, 0.5, 2, 2, False, kd)
                rl.q_agent.update(term_state, lane, 10.0 if won else -5.0, term_state)

        rl.q_agent.save()
        print(f"  Trained on {trained} state transitions from {len(games)} games")
        print(f"  States learned: {len(rl.q_agent.q_table)}")
        print()

        # Show updated top actions
        top = rl.q_agent.top_actions(10)
        if top:
            print("LEARNED STRATEGIES:")
            for entry in top:
                parts = entry["state"].split("|")
                print(f"  Phase:{parts[0]} Lvl:{parts[1]} HP:{parts[2]} "
                      f"Allies:{parts[3]} Threat:{parts[4]} Tower:{parts[5]} "
                      f"Momentum:{parts[6]}")
                print(f"    BEST: {entry['best_action']} "
                      f"(Q={entry['q_value']:.2f} conf={entry['confidence']:.2f})")
