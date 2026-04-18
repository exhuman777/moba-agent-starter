#!/usr/bin/env python3
"""Defense of the Agents - Quantitative Game Theory Engine
All math, simulations, EV calculations, and analytical models.
"""

from __future__ import annotations
import math

# ── Constants ──────────────────────────────────────────────

TICK_RATE = 20
SUDDEN_DEATH_TICKS = 15 * 60 * TICK_RATE
TOWER_BUFF_TICKS = 105 * TICK_RATE

HERO_BASE = {
    "melee":  {"hp": 266, "dmg": 25, "range": 40, "atk_cd": 1.0},
    "ranged": {"hp": 168, "dmg": 15, "range": 150, "atk_cd": 1.0},
    "mage":   {"hp": 140, "dmg": 15, "range": 150, "atk_cd": 1.0},
}

TOWER = {"hp": 1200, "dmg": 70, "range": 275, "atk_cd": 0.75}
BASE = {"hp": 1500, "dmg": 60, "range": 250, "atk_cd": 0.6}
UNIT_MELEE = {"hp": 95, "dmg": 10, "spawn_cd": 2.5}
UNIT_RANGED = {"hp": 40, "dmg": 6, "spawn_cd": 7.0}

ABILITY_DATA = {
    "cleave":         {"type": "passive", "values": [0.30, 0.40, 0.50], "desc": "splash %"},
    "thorns":         {"type": "passive", "values": [0.40, 0.60, 0.80], "desc": "reflect %"},
    "divine_shield":  {"type": "auto",    "values": [3, 4, 5], "cd": 16, "desc": "immune sec"},
    "volley":         {"type": "passive", "values": [3, 5, 7], "desc": "arrows (66% secondary)"},
    "bloodlust":      {"type": "auto",    "values": [5, 6, 7], "cd": 15, "desc": "2x atk sec"},
    "critical_strike": {"type": "passive", "values": [0.15, 0.25, 0.35], "desc": "crit chance"},
    "fireball":       {"type": "auto",    "values": [40, 65, 90], "cd": 4, "desc": "AOE dmg", "scale": 0.025},
    "tornado":        {"type": "auto",    "values": [12, 18, 25], "cd": 8, "desc": "dmg/tick", "scale": 0.025},
    "raise_skeleton": {"type": "auto",    "values": [200, 300, 400], "cd": 15, "desc": "skeleton HP"},
    "fortitude":      {"type": "passive", "values": [0.20, 0.30, 0.40], "desc": "+HP %"},
    "fury":           {"type": "passive", "values": [0.15, 0.25, 0.35], "desc": "+dmg %"},
}


# ── Hero Stats Calculator ─────────────────────────────────

def hero_stats(cls: str, level: int, abilities: list[dict] = None) -> dict:
    """Calculate exact hero stats at given level with abilities."""
    base = HERO_BASE[cls]
    hp = base["hp"] * (1.15 ** (level - 1))
    dmg = base["dmg"] * (1.15 ** (level - 1))
    atk_cd = base["atk_cd"]
    atk_multi = 1.0
    crit_chance = 0
    splash = 0
    reflect = 0
    shield_dur = 0
    skeleton_hp = 0
    spell_dps = 0

    abilities = abilities or []
    for a in abilities:
        aid = a.get("id", "")
        alv = min(a.get("level", 1), 3)
        vals = ABILITY_DATA.get(aid, {}).get("values", [])
        if not vals or alv < 1:
            continue
        v = vals[alv - 1]

        if aid == "fortitude":
            hp *= (1 + v)
        elif aid == "fury":
            dmg *= (1 + v)
        elif aid == "cleave":
            splash = v
        elif aid == "thorns":
            reflect = v
        elif aid == "divine_shield":
            shield_dur = v
        elif aid == "critical_strike":
            crit_chance = v
        elif aid == "volley":
            # Base arrow + secondary arrows at 66% dmg
            atk_multi = 1 + (v - 1) * 0.66
        elif aid == "bloodlust":
            # Averaged: 2x for v seconds out of 15s CD
            uptime = v / 15
            atk_multi *= (1 + uptime)  # average boost
        elif aid == "fireball":
            fb_dmg = v * (1 + 0.025 * level)
            spell_dps += fb_dmg / 4
        elif aid == "tornado":
            t_ticks = [4, 5, 6][alv - 1]  # approx ticks
            t_dmg = v * t_ticks * (1 + 0.025 * level)
            spell_dps += t_dmg / 8
        elif aid == "raise_skeleton":
            skeleton_hp = v

    effective_dps = (dmg / atk_cd) * atk_multi * (1 + crit_chance) + spell_dps

    return {
        "hp": hp, "dmg": dmg, "dps": effective_dps,
        "atk_multi": atk_multi, "crit_chance": crit_chance,
        "splash": splash, "reflect": reflect,
        "shield_dur": shield_dur, "skeleton_hp": skeleton_hp,
        "spell_dps": spell_dps,
    }


# ── Combat Simulations ────────────────────────────────────

def duel_sim(a_cls: str, a_level: int, a_abilities: list,
             b_cls: str, b_level: int, b_abilities: list) -> dict:
    """Simulate 1v1 fight. Returns winner, remaining HP, time to kill."""
    a = hero_stats(a_cls, a_level, a_abilities)
    b = hero_stats(b_cls, b_level, b_abilities)

    a_hp, b_hp = a["hp"], b["hp"]
    a_dps, b_dps = a["dps"], b["dps"]

    # Account for thorns
    if b["reflect"] > 0:
        a_self_dmg = a_dps * b["reflect"]  # reflect per second
    else:
        a_self_dmg = 0
    if a["reflect"] > 0:
        b_self_dmg = b_dps * a["reflect"]
    else:
        b_self_dmg = 0

    # Account for divine shield (subtracts time from fight)
    a_immune_time = a["shield_dur"]
    b_immune_time = b["shield_dur"]

    # Effective DPS after reflect
    a_net_dps = a_dps - a_self_dmg
    b_net_dps = b_dps - b_self_dmg

    # Time to kill
    if a_net_dps <= 0:
        a_ttk = 999
    else:
        a_ttk = b_hp / a_net_dps + b_immune_time  # time for A to kill B

    if b_net_dps <= 0:
        b_ttk = 999
    else:
        b_ttk = a_hp / b_net_dps + a_immune_time  # time for B to kill A

    if a_ttk < b_ttk:
        winner = "A"
        remaining_hp = a_hp - b_net_dps * a_ttk
    elif b_ttk < a_ttk:
        winner = "B"
        remaining_hp = b_hp - a_net_dps * b_ttk
    else:
        winner = "draw"
        remaining_hp = 0

    return {
        "winner": winner,
        "a_ttk": a_ttk, "b_ttk": b_ttk,
        "a_hp": a_hp, "b_hp": b_hp,
        "a_dps": a_dps, "b_dps": b_dps,
        "remaining_hp": max(0, remaining_hp),
        "fight_time": min(a_ttk, b_ttk),
    }


# ── XP Economy ─────────────────────────────────────────────

def xp_for_level(level: int) -> int:
    """XP needed to reach given level from level 1."""
    return sum(200 * i for i in range(1, level))

def xp_to_next(level: int) -> int:
    """XP needed for next level."""
    return 200 * level

def kill_xp_value(victim_level: int) -> int:
    """XP granted when killing a hero of given level."""
    return 200 + max(0, (victim_level - 1)) * 10

def death_cost(level: int) -> dict:
    """Cost of dying at given level."""
    respawn = min(30, 3 + 1.5 * level)
    # XP given to enemy
    xp_given = kill_xp_value(level)
    # Lost farming time (50 XP/unit kill, ~1 kill per 3s with hero present)
    xp_lost_per_sec = 50 / 3
    total_xp_lost = xp_given + respawn * xp_lost_per_sec
    return {
        "respawn_sec": respawn,
        "xp_given": xp_given,
        "xp_lost_farming": respawn * xp_lost_per_sec,
        "total_xp_cost": total_xp_lost,
    }


def kill_ev(my_level: int, enemy_level: int, kill_prob: float) -> float:
    """Expected value of engaging. Positive = worth fighting."""
    win_xp = kill_xp_value(enemy_level)
    lose_cost = death_cost(my_level)["total_xp_cost"]
    return kill_prob * win_xp - (1 - kill_prob) * lose_cost


# ── Tower Push Calculator ──────────────────────────────────

def tower_push_time(num_heroes: int, avg_dps: float, avg_hp: float,
                    num_units: int = 10, tower_buffed: bool = False) -> dict:
    """Estimate time to destroy tower with given force."""
    t_hp = TOWER["hp"]
    t_dmg = TOWER["dmg"] * (2 if tower_buffed else 1)
    t_cd = TOWER["atk_cd"]
    t_dps = t_dmg / t_cd

    # Tower focuses one target at a time
    # Units absorb aggro first (95hp each, tower does 70/0.75s = 93 dps)
    unit_hp_total = num_units * 95
    unit_absorb_time = unit_hp_total / t_dps if t_dps > 0 else 0

    # Hero DPS on tower
    total_dps = avg_dps * num_heroes + num_units * 10  # units contribute too
    push_time = t_hp / total_dps if total_dps > 0 else 999

    # Can we take tower before units die?
    safe = push_time < unit_absorb_time
    # Hero deaths if units die
    hero_deaths = 0
    if not safe:
        remaining_time = push_time - unit_absorb_time
        hero_deaths = math.ceil(remaining_time * t_dps / avg_hp) if avg_hp > 0 else 99

    return {
        "push_time_sec": push_time,
        "unit_absorb_sec": unit_absorb_time,
        "safe_push": safe,
        "hero_deaths_est": hero_deaths,
    }


# ── Dragon Calculator ──────────────────────────────────────

def dragon_stats(total_players: int, respawn_num: int = 0) -> dict:
    """Dragon stats. Patch: 1500 HP base, scales with players, +50% per respawn."""
    base_hp = 1500  # updated from 900, patch note says 1500
    # Also scales with player count: 900 + 75*players in original, but patch says 1500 base
    # Using max of both formulas
    scaled_hp = max(base_hp, 900 + 75 * total_players)
    multiplier = 1 + 0.5 * respawn_num  # +50% HP and damage per respawn
    return {
        "hp": scaled_hp * multiplier,
        "damage_multi": multiplier,  # +50% damage per respawn too
        "multiplier": multiplier,
        "respawn_num": respawn_num,
    }


def dragon_kill_time(num_heroes: int, avg_dps: float, total_players: int,
                     respawn_num: int = 0) -> float:
    """Time to kill dragon."""
    d = dragon_stats(total_players, respawn_num)
    total_dps = avg_dps * num_heroes
    return d["hp"] / total_dps if total_dps > 0 else 999


# ── Power Curve Tables ─────────────────────────────────────

def power_curve(cls: str, abilities_at_levels: dict = None) -> list[dict]:
    """Generate power curve from L1-20 for a class.
    abilities_at_levels: {3: "divine_shield", 6: "thorns", ...}
    """
    abilities_at_levels = abilities_at_levels or {}
    current_abilities = []
    curve = []

    for level in range(1, 21):
        # Add abilities at milestone levels
        if level in abilities_at_levels:
            aid = abilities_at_levels[level]
            # Check if already have this ability
            existing = next((a for a in current_abilities if a["id"] == aid), None)
            if existing:
                existing["level"] = min(existing["level"] + 1, 3)
            else:
                current_abilities.append({"id": aid, "level": 1})

        stats = hero_stats(cls, level, current_abilities)
        dc = death_cost(level)
        curve.append({
            "level": level,
            "hp": stats["hp"],
            "dps": stats["dps"],
            "death_cost": dc["total_xp_cost"],
            "respawn": dc["respawn_sec"],
            "xp_bounty": kill_xp_value(level),
        })

    return curve


# ── Ability EV Calculator ──────────────────────────────────

def ability_ev(cls: str, level: int, current_abilities: list, choice: str) -> dict:
    """Calculate the value of picking a specific ability."""
    # Stats without new ability
    before = hero_stats(cls, level, current_abilities)

    # Stats with new ability
    new_abs = list(current_abilities)
    existing = next((a for a in new_abs if a["id"] == choice), None)
    if existing:
        test_abs = [a.copy() for a in new_abs]
        for a in test_abs:
            if a["id"] == choice:
                a["level"] = min(a["level"] + 1, 3)
    else:
        test_abs = new_abs + [{"id": choice, "level": 1}]

    after = hero_stats(cls, level, test_abs)

    return {
        "ability": choice,
        "hp_delta": after["hp"] - before["hp"],
        "hp_delta_pct": (after["hp"] - before["hp"]) / before["hp"] * 100 if before["hp"] > 0 else 0,
        "dps_delta": after["dps"] - before["dps"],
        "dps_delta_pct": (after["dps"] - before["dps"]) / before["dps"] * 100 if before["dps"] > 0 else 0,
        "shield_dur": after["shield_dur"],
        "skeleton_hp": after["skeleton_hp"],
        "reflect": after["reflect"],
    }


# ── Historical Analysis ───────────────────────────────────

def analyze_history(games: list[dict]) -> dict:
    """Analyze game history for patterns."""
    if not games:
        return {"total": 0}

    total = len(games)
    wins = sum(1 for g in games if g.get("won"))
    losses = total - wins

    # Per-style performance
    style_stats = {}
    for g in games:
        for b in g.get("bots", []):
            style = b.get("style", "unknown")
            if style not in style_stats:
                style_stats[style] = {"kills": 0, "deaths": 0, "total_level": 0, "games": 0}
            style_stats[style]["kills"] += b.get("kills_est", 0)
            style_stats[style]["deaths"] += b.get("deaths", 0)
            style_stats[style]["total_level"] += b.get("level", 1)
            style_stats[style]["games"] += 1

    for s, d in style_stats.items():
        d["avg_level"] = d["total_level"] / max(d["games"], 1)
        d["kd_ratio"] = d["kills"] / max(d["deaths"], 1)

    # Per-class performance
    class_stats = {}
    for g in games:
        for b in g.get("bots", []):
            cls = b.get("class", "unknown")
            if cls not in class_stats:
                class_stats[cls] = {"kills": 0, "deaths": 0, "total_level": 0, "games": 0}
            class_stats[cls]["kills"] += b.get("kills_est", 0)
            class_stats[cls]["deaths"] += b.get("deaths", 0)
            class_stats[cls]["total_level"] += b.get("level", 1)
            class_stats[cls]["games"] += 1

    for s, d in class_stats.items():
        d["avg_level"] = d["total_level"] / max(d["games"], 1)
        d["kd_ratio"] = d["kills"] / max(d["deaths"], 1)

    # Game duration stats
    durations = [g.get("game_secs", 0) for g in games if g.get("game_secs")]
    avg_duration = sum(durations) / len(durations) if durations else 0

    # Win/loss by faction
    faction_wins = {}
    for g in games:
        f = g.get("our_faction", "?")
        if f not in faction_wins:
            faction_wins[f] = {"wins": 0, "losses": 0}
        if g.get("won"):
            faction_wins[f]["wins"] += 1
        else:
            faction_wins[f]["losses"] += 1

    # Rolling winrate (last 5, 10, 20)
    def rolling_wr(n):
        recent = games[-n:] if len(games) >= n else games
        w = sum(1 for g in recent if g.get("won"))
        return w / len(recent) * 100 if recent else 0

    return {
        "total": total, "wins": wins, "losses": losses,
        "winrate": wins / total * 100,
        "style_stats": style_stats,
        "class_stats": class_stats,
        "avg_duration_sec": avg_duration,
        "faction_wins": faction_wins,
        "rolling_5": rolling_wr(5),
        "rolling_10": rolling_wr(10),
        "rolling_20": rolling_wr(20),
    }


# ── Live Matchup Analysis ─────────────────────────────────

def lane_matchup(state: dict, faction: str, lane: str) -> dict:
    """Analyze power balance in a specific lane."""
    enemy = "orc" if faction == "human" else "human"
    our_heroes = []
    enemy_heroes = []

    for h in state.get("heroes", []):
        if h.get("lane") != lane or not h.get("alive"):
            continue
        stats = hero_stats(h.get("class", "melee"), h.get("level", 1), h.get("abilities", []))
        entry = {"name": h["name"], "level": h["level"], "class": h["class"], **stats}
        if h["faction"] == faction:
            our_heroes.append(entry)
        else:
            enemy_heroes.append(entry)

    our_total_dps = sum(h["dps"] for h in our_heroes)
    our_total_hp = sum(h["hp"] for h in our_heroes)
    enemy_total_dps = sum(h["dps"] for h in enemy_heroes)
    enemy_total_hp = sum(h["hp"] for h in enemy_heroes)

    # Time-to-wipe calculations
    our_ttw = enemy_total_hp / our_total_dps if our_total_dps > 0 else 999
    enemy_ttw = our_total_hp / enemy_total_dps if enemy_total_dps > 0 else 999

    # Win probability (simplified)
    if our_ttw + enemy_ttw > 0:
        win_prob = enemy_ttw / (our_ttw + enemy_ttw)
    else:
        win_prob = 0.5

    # Unit advantage
    ld = state.get("lanes", {}).get(lane, {})
    our_units = ld.get(faction, 0)
    enemy_units = ld.get(enemy, 0)

    return {
        "lane": lane,
        "our_heroes": our_heroes, "enemy_heroes": enemy_heroes,
        "our_count": len(our_heroes), "enemy_count": len(enemy_heroes),
        "our_dps": our_total_dps, "enemy_dps": enemy_total_dps,
        "our_hp": our_total_hp, "enemy_hp": enemy_total_hp,
        "our_ttw": our_ttw, "enemy_ttw": enemy_ttw,
        "win_prob": win_prob,
        "our_units": our_units, "enemy_units": enemy_units,
        "unit_advantage": our_units - enemy_units,
        "recommendation": "push" if win_prob > 0.65 else ("hold" if win_prob > 0.4 else "retreat"),
    }


def game_state_analysis(state: dict, faction: str) -> dict:
    """Full quantitative analysis of current game state."""
    enemy = "orc" if faction == "human" else "human"
    tick = state.get("tick", 0)
    game_secs = tick / TICK_RATE
    game_pct = tick / SUDDEN_DEATH_TICKS

    # Lane matchups
    matchups = {lane: lane_matchup(state, faction, lane) for lane in ["top", "mid", "bot"]}

    # Tower state
    our_towers = 0
    enemy_towers = 0
    our_tower_hp = 0
    enemy_tower_hp = 0
    for t in state.get("towers", []):
        if t.get("alive"):
            if t["faction"] == faction:
                our_towers += 1
                our_tower_hp += t["hp"]
            else:
                enemy_towers += 1
                enemy_tower_hp += t["hp"]

    # Dragon state
    dragon_for_us = enemy_towers == 0
    dragon_for_them = our_towers == 0

    # Base state
    our_base = state.get("bases", {}).get(faction, {}).get("hp", 1500)
    enemy_base = state.get("bases", {}).get(enemy, {}).get("hp", 1500)

    # Overall power
    our_heroes = [h for h in state.get("heroes", []) if h["faction"] == faction and h.get("alive")]
    enemy_heroes_alive = [h for h in state.get("heroes", []) if h["faction"] == enemy and h.get("alive")]

    our_total_dps = sum(hero_stats(h["class"], h["level"], h.get("abilities", []))["dps"] for h in our_heroes)
    enemy_total_dps = sum(hero_stats(h["class"], h["level"], h.get("abilities", []))["dps"] for h in enemy_heroes_alive)

    # Advantage score (-100 to +100)
    factors = []
    factors.append((our_towers - enemy_towers) * 10)  # tower advantage
    factors.append((our_base - enemy_base) / 30)  # base HP advantage
    factors.append((len(our_heroes) - len(enemy_heroes_alive)) * 8)  # alive heroes
    factors.append((our_total_dps - enemy_total_dps) / max(our_total_dps + enemy_total_dps, 1) * 30)
    advantage = max(-100, min(100, sum(factors)))

    return {
        "tick": tick, "game_secs": game_secs, "game_pct": game_pct,
        "matchups": matchups,
        "our_towers": our_towers, "enemy_towers": enemy_towers,
        "our_tower_hp": our_tower_hp, "enemy_tower_hp": enemy_tower_hp,
        "dragon_for_us": dragon_for_us, "dragon_for_them": dragon_for_them,
        "our_base": our_base, "enemy_base": enemy_base,
        "our_alive": len(our_heroes), "enemy_alive": len(enemy_heroes_alive),
        "our_total_dps": our_total_dps, "enemy_total_dps": enemy_total_dps,
        "advantage_score": advantage,
        "sudden_death_in": max(0, (SUDDEN_DEATH_TICKS - tick) / TICK_RATE),
        "phase": "tower_buff" if tick < TOWER_BUFF_TICKS else
                 ("early" if game_pct < 0.3 else
                  ("mid" if game_pct < 0.6 else
                   ("late" if game_pct < 0.85 else "endgame"))),
    }
