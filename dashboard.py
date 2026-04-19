#!/usr/bin/env python3
"""Defense of the Agents - Fleet Dashboard v4 (view-only).

Read-only viewer. On launch it spawns ws_runner as a background subprocess
(ws_runner is the sole writer of stats.json + the only thing sending commands
to /api/strategy/deployment). Dashboard just polls /api/game/state and tails
ws_runner.log. Pressing q kills the runner and exits together.
"""

from __future__ import annotations

import requests
import time
import json
import sys
import os
import random
import select
import signal
import subprocess
import termios
import tty
from collections import deque
from datetime import datetime
from dataclasses import dataclass, field

from rich.console import Console, Group
from rich.table import Table
from rich.layout import Layout
from rich.panel import Panel
from rich.live import Live
from rich.text import Text

import quant

BASE = "https://wc2-agentic-dev-3o6un.ondigitalocean.app"
DIR = os.path.dirname(os.path.abspath(__file__))
FLEET_FILE = os.path.join(DIR, "fleet.json")
STATS_FILE = os.path.join(DIR, "stats.json")
RUNNER_LOG = os.path.join(DIR, "ws_runner.log")
POLL_INTERVAL = 1.0  # seconds between /api/game/state polls (view refresh only)

TICK_RATE = 20
SUDDEN_DEATH_TICKS = 15 * 60 * TICK_RATE
TOWER_BUFF_TICKS = 105 * TICK_RATE

HERO_BASE = {
    "melee":  {"hp": 266, "dmg": 25, "range": 40},
    "ranged": {"hp": 168, "dmg": 15, "range": 150},
    "mage":   {"hp": 140, "dmg": 15, "range": 150},
}

ABILITY_SHORT = {
    "cleave": "clv", "thorns": "thr", "divine_shield": "dsh",
    "volley": "vol", "bloodlust": "blt", "critical_strike": "crt",
    "stim_pack": "stm", "fireball": "fbl", "tornado": "trn",
    "raise_skeleton": "skl", "fortitude": "frt", "fury": "fur",
}


# ── Stats Tracker ──────────────────────────────────────────

class StatsTracker:
    def __init__(self):
        self.games: list[dict] = []
        self.load()

    def load(self):
        if os.path.exists(STATS_FILE):
            try:
                with open(STATS_FILE) as f:
                    self.games = json.load(f)
            except Exception:
                self.games = []

    def save(self):
        with open(STATS_FILE, "w") as f:
            json.dump(self.games, f, indent=2)

    def record(self, winner: str, state: dict, bots: list):
        """Record detailed game result with per-faction bot split."""
        # Count bots per faction
        faction_bots = {"human": [], "orc": []}
        for b in bots:
            if b.faction in faction_bots:
                faction_bots[b.faction].append(b)

        human_count = len(faction_bots["human"])
        orc_count = len(faction_bots["orc"])
        # "Majority faction" = the one with more of our bots
        majority = "human" if human_count > orc_count else "orc"
        majority_won = winner == majority

        # Per-bot snapshot with faction
        bot_snapshots = []
        for b in bots:
            hero = b.find_hero(state)
            snap = {
                "name": b.name, "class": b.hero_class, "role": b.role,
                "style": b.style, "lane": b.current_lane, "faction": b.faction,
                "decisions": b.decisions, "errors": b.errors,
                "kills_est": b.kills_est, "deaths": b.deaths,
                "won": b.faction == winner,
            }
            if hero:
                snap["level"] = hero.get("level", 1)
                snap["abilities"] = [{"id": a["id"], "level": a["level"]} for a in hero.get("abilities", [])]
            bot_snapshots.append(snap)

        towers = {}
        for t in state.get("towers", []):
            towers[f"{t['faction']}_{t['lane']}"] = {"hp": t.get("hp", 0), "alive": t.get("alive", False)}

        # Levels per faction
        h_heroes = [h for h in state.get("heroes", []) if h.get("faction") == "human"]
        o_heroes = [h for h in state.get("heroes", []) if h.get("faction") == "orc"]

        entry = {
            "time": datetime.now().isoformat(),
            "winner": winner,
            "majority_faction": majority,
            "majority_won": majority_won,
            "human_bots": human_count, "orc_bots": orc_count,
            "game_time": state.get("tick", 0) / TICK_RATE,
            "tick": state.get("tick", 0),
            "human_max_level": max((h.get("level", 1) for h in h_heroes), default=1),
            "orc_max_level": max((h.get("level", 1) for h in o_heroes), default=1),
            "human_base_hp": state.get("bases", {}).get("human", {}).get("hp", 0),
            "orc_base_hp": state.get("bases", {}).get("orc", {}).get("hp", 0),
            "towers": towers,
            "bots": bot_snapshots,
        }
        self.games.append(entry)
        self.save()

    @property
    def wins(self) -> int:
        # Use majority_won if available, fall back to old "won" field
        return sum(1 for g in self.games if g.get("majority_won", g.get("won")))

    @property
    def losses(self) -> int:
        return sum(1 for g in self.games if not g.get("majority_won", g.get("won")))

    @property
    def winrate(self) -> str:
        total = len(self.games)
        if total == 0:
            return "0%"
        return f"{100 * self.wins / total:.0f}%"

    @property
    def streak(self) -> str:
        if not self.games:
            return "-"
        count = 0
        last = self.games[-1].get("majority_won", self.games[-1].get("won"))
        for g in reversed(self.games):
            if g.get("majority_won", g.get("won")) == last:
                count += 1
            else:
                break
        prefix = "W" if last else "L"
        return f"{prefix}{count}"

    def summary_line(self) -> str:
        if not self.games:
            return "No games recorded"
        return f"W:{self.wins} L:{self.losses} ({self.winrate}) Streak:{self.streak}"


# ── Runner subprocess ──────────────────────────────────────

class RunnerProcess:
    """Spawn + manage ws_runner.py as a background subprocess.

    - start(): launches `python3 ws_runner.py`, redirects stdout+stderr to RUNNER_LOG.
    - alive(): True if subprocess still running.
    - stop(timeout=2.0): SIGTERM, wait, SIGKILL on timeout.
    - recent_lines(n): read last n lines of RUNNER_LOG (for live tail panel).
    - ensure_alive(): if child died (crash or external kill), relaunch it.
      Dashboard calls this every poll so a dead runner auto-respawns.
    """

    def __init__(self, cmd: list[str], log_path: str):
        self.cmd = cmd
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None
        self._log_fp = None
        self._stopped = False   # set by stop(); prevents ensure_alive respawn
        self._respawn_count = 0

    def start(self) -> None:
        # Truncate previous log only on the FIRST start; respawns append so
        # the runner-log panel shows history across restarts.
        mode = "a" if self._respawn_count else "w"
        self._log_fp = open(self.log_path, mode, buffering=1)
        if self._respawn_count:
            self._log_fp.write(f"\n[RunnerProcess] respawn #{self._respawn_count}\n")
        self.proc = subprocess.Popen(
            self.cmd,
            cwd=DIR,
            stdout=self._log_fp,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # don't share terminal signals
        )

    def ensure_alive(self) -> bool:
        """Respawn if the child died unexpectedly. Returns True if it was
        respawned. No-op when stop() was called (user-initiated shutdown).
        """
        if self._stopped:
            return False
        if self.proc is None or self.proc.poll() is not None:
            self._respawn_count += 1
            self.start()
            return True
        return False

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self, timeout: float = 2.0) -> None:
        self._stopped = True  # block further ensure_alive respawns
        if not self.proc:
            return
        if self.alive():
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                self.proc.terminate()
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    self.proc.kill()
                try:
                    self.proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass
        if self._log_fp:
            try:
                self._log_fp.close()
            except Exception:
                pass
            self._log_fp = None

    def recent_lines(self, n: int = 12) -> list[str]:
        if not os.path.exists(self.log_path):
            return []
        try:
            with open(self.log_path) as f:
                buf = deque(f, maxlen=n)
            return [ln.rstrip("\n") for ln in buf]
        except Exception:
            return []


# ── API ────────────────────────────────────────────────────

def api_get(path: str, params: dict = None) -> dict:
    try:
        r = requests.get(f"{BASE}{path}", params=params, timeout=10)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


def api_post(path: str, api_key: str, payload: dict) -> dict:
    try:
        r = requests.post(
            f"{BASE}{path}",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload, timeout=10,
        )
        return r.json() if r.status_code == 200 else {"error": f"{r.status_code}"}
    except Exception as e:
        return {"error": str(e)[:60]}


# ── Analytics ──────────────────────────────────────────────

class Analytics:
    def __init__(self):
        self.xp_history: dict[str, list[tuple[int, int]]] = {}

    def update(self, state: dict):
        tick = state.get("tick", 0)
        for h in state.get("heroes", []):
            name = h["name"]
            level = h.get("level", 1)
            xp_total = sum(200 * i for i in range(1, level)) + h.get("xp", 0)
            hist = self.xp_history.setdefault(name, [])
            hist.append((tick, xp_total))
            if len(hist) > 60:
                self.xp_history[name] = hist[-60:]

    def xp_per_min(self, name: str) -> float:
        hist = self.xp_history.get(name, [])
        if len(hist) < 2:
            return 0
        dt = (hist[-1][0] - hist[0][0]) / TICK_RATE / 60
        return (hist[-1][1] - hist[0][1]) / dt if dt > 0 else 0

    def est_dps(self, hero: dict) -> float:
        cls = hero.get("class", "melee")
        level = hero.get("level", 1)
        dmg = HERO_BASE.get(cls, {}).get("dmg", 15) * (1.15 ** (level - 1))
        spell_dps = 0
        crit = 0
        atk_m = 1.0
        for a in hero.get("abilities", []):
            aid, alv = a["id"], min(a["level"], 3)
            if aid == "fury":
                dmg *= 1 + [0, .15, .25, .35][alv]
            elif aid == "critical_strike":
                crit = [0, .15, .25, .35][alv]
            elif aid == "bloodlust":
                atk_m = 1.3
            elif aid == "cleave":
                dmg *= 1 + [0, .15, .20, .25][alv]
            elif aid == "fireball":
                spell_dps += [0, 40, 65, 90][alv] * (1 + .025 * level) / 4
            elif aid == "tornado":
                td = [0, 12, 18, 25][alv]
                dur = [0, 2, 2.5, 3][alv]
                spell_dps += td * (dur / .5) * (1 + .025 * level) / 8
        return dmg * atk_m * (1 + crit) + spell_dps

    def respawn_time(self, hero: dict) -> float:
        return min(30, 3 + 1.5 * hero.get("level", 1))

    def game_time(self, state: dict) -> str:
        s = state.get("tick", 0) / TICK_RATE
        return f"{int(s)//60}:{int(s)%60:02d}"

    def sudden_death_in(self, state: dict) -> str:
        rem = SUDDEN_DEATH_TICKS - state.get("tick", 0)
        if rem <= 0:
            return "ACTIVE"
        s = rem / TICK_RATE
        return f"{int(s)//60}:{int(s)%60:02d}"

    def team_power(self, state: dict, faction: str) -> dict:
        heroes = [h for h in state.get("heroes", []) if h.get("faction") == faction]
        alive = [h for h in heroes if h.get("alive")]
        return {
            "count": len(heroes), "alive": len(alive),
            "dead": len(heroes) - len(alive),
            "total_dps": sum(self.est_dps(h) for h in alive),
            "total_hp": sum(h.get("hp", 0) for h in alive),
            "avg_level": sum(h.get("level", 1) for h in heroes) / max(len(heroes), 1),
        }


# ── Event Feed ─────────────────────────────────────────────

class EventFeed:
    """Global scrolling event ticker for live feel."""

    def __init__(self, max_events: int = 30):
        self.events: list[str] = []
        self.max = max_events
        self._prev_heroes: dict[str, dict] = {}  # name -> snapshot

    def push(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.events.append(f"[dim]{ts}[/dim] {msg}")
        if len(self.events) > self.max:
            self.events = self.events[-self.max:]

    def diff_state(self, state: dict, my_names: set[str]):
        """Detect changes between ticks and emit events."""
        for h in state.get("heroes", []):
            name = h["name"]
            prev = self._prev_heroes.get(name)
            is_mine = name in my_names
            tag = f"[bold green]{name}[/bold green]" if is_mine else f"[dim]{name}[/dim]"

            if not prev:
                # New hero appeared
                self._prev_heroes[name] = self._snap(h)
                continue

            # Death
            if prev.get("alive") and not h.get("alive"):
                self.push(f"[red]KILL[/red] {tag} died in {h.get('lane', '?')}")

            # Respawn
            if not prev.get("alive") and h.get("alive"):
                self.push(f"[green]SPAWN[/green] {tag} respawned")

            # Level up
            if h.get("level", 1) > prev.get("level", 1):
                self.push(f"[yellow]LEVEL[/yellow] {tag} -> L{h['level']}")

            # Big HP drop (took heavy damage)
            if h.get("alive") and prev.get("alive"):
                hp_drop = prev.get("hp", 0) - h.get("hp", 0)
                max_hp = h.get("maxHp", 1)
                if max_hp > 0 and hp_drop > max_hp * 0.3:
                    pct = int(100 * h["hp"] / max_hp)
                    self.push(f"[red]DMG[/red] {tag} took {int(hp_drop)} dmg ({pct}% HP)")

            # Lane change
            if h.get("lane") != prev.get("lane") and h.get("alive"):
                self.push(f"[cyan]MOVE[/cyan] {tag} {prev.get('lane', '?')} -> {h.get('lane', '?')}")

            # New ability
            prev_abs = {a["id"] for a in prev.get("abilities", [])}
            curr_abs = {a["id"] for a in h.get("abilities", [])}
            new_abs = curr_abs - prev_abs
            for ab in new_abs:
                self.push(f"[magenta]SKILL[/magenta] {tag} learned {ab}")

            self._prev_heroes[name] = self._snap(h)

        # Tower events
        # (tracked separately if needed)

    def _snap(self, hero: dict) -> dict:
        return {
            "alive": hero.get("alive"), "hp": hero.get("hp", 0),
            "level": hero.get("level", 1), "lane": hero.get("lane"),
            "abilities": hero.get("abilities", []),
        }

    def render_lines(self, n: int = 8) -> str:
        if not self.events:
            return "[dim]Waiting for action...[/dim]"
        return "\n".join(self.events[-n:])


# ── Bot Brain ──────────────────────────────────────────────

class BotBrain:
    """Read-only observer for rendering. NEVER hits /api/strategy/deployment —
    that is ws_runner's job. Only tracks state from /api/game/state to keep
    the dashboard roster populated (K/D via XP delta, deaths, current lane).
    """

    def __init__(self, name: str, api_key: str, hero_class: str,
                 default_lane: str, role: str, ability_prio: list,
                 style: str, game: int):
        self.name = name
        self.api_key = api_key  # retained only for identification, never used for POSTs
        self.hero_class = hero_class
        self.default_lane = default_lane
        self.role = role
        self.ability_prio = ability_prio
        self.style = style
        self.game = game
        self.joined = False
        self.faction = None
        self.log: list[str] = []
        self.decisions = 0
        self.errors = 0
        self.kills_est = 0
        self.deaths = 0
        self.recalls = 0
        self.lane_switches = 0
        self.current_lane = default_lane
        self.last_action = ""
        self._prev_alive = True
        self._prev_xp_total = 0
        self._prev_lane = default_lane

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log.append(f"[{ts}] {msg}")
        if len(self.log) > 40:
            self.log = self.log[-40:]

    def find_hero(self, state: dict) -> dict | None:
        for h in state.get("heroes", []):
            if h.get("name") == self.name:
                self.faction = h.get("faction")
                return h
        return None

    def observe(self, state: dict) -> dict | None:
        """Update display stats from the authoritative /api/game/state snapshot.

        Zero API writes. Called every poll cycle. Maintains:
          joined flag, current_lane, kills_est (via XP delta), deaths,
          _prev_alive/_prev_xp for delta tracking.
        """
        if state.get("winner"):
            if self.joined:
                self._log(f"Game over: {state['winner']} (K:{self.kills_est} D:{self.deaths})")
            self.joined = False
            self.kills_est = 0
            self.deaths = 0
            self.recalls = 0
            self.lane_switches = 0
            self._prev_xp_total = 0
            self._prev_alive = True
            return None

        hero = self.find_hero(state)
        if not hero:
            # Bot hasn't spawned (ws_runner hasn't deployed yet, or dead pre-game)
            self.joined = False
            return None

        self.joined = True

        # Lane tracking from authoritative hero.lane (not a heuristic)
        new_lane = hero.get("lane", self.current_lane)
        if new_lane != self._prev_lane:
            self.lane_switches += 1
            self._prev_lane = new_lane
        self.current_lane = new_lane

        # Death tracking
        alive_now = hero.get("alive", False)
        if self._prev_alive and not alive_now:
            self.deaths += 1
            self._log(f"Died (death #{self.deaths})")
        self._prev_alive = alive_now

        # Kill estimate via XP jumps (hero kill ~180-220 XP)
        level = hero.get("level", 1)
        xp_total = sum(200 * i for i in range(1, level)) + hero.get("xp", 0)
        if self._prev_xp_total > 0:
            xp_delta = xp_total - self._prev_xp_total
            if xp_delta >= 180:
                self.kills_est += xp_delta // 180
        self._prev_xp_total = xp_total

        self.last_action = "dead" if not alive_now else new_lane
        return hero


# ── Dashboard ──────────────────────────────────────────────

class Dashboard:
    VIEWS = ["fleet", "game", "insights", "quant"]

    def __init__(self, bots: list[BotBrain], game: int, runner: RunnerProcess | None = None):
        self.bots = bots
        self.game = game
        self.runner = runner
        self.console = Console()
        self.state: dict = {}
        self.analytics = Analytics()
        self.events = EventFeed()
        self.stats = StatsTracker()
        self._prev_hp: dict[str, int] = {}  # bot name -> last HP for delta display
        self.cycle = 0
        self.running = True
        self.view = "fleet"
        self.show_help = False
        self.status_msg = ""

    def handle_key(self, key: str):
        """View-only: TAB cycles views, h toggles help, q quits.
        No strategy or param mutation — ws_runner runs on fixed fleet.json.
        """
        self.status_msg = ""

        if key == "h":
            self.show_help = not self.show_help
        elif key == "q":
            self.running = False
        elif key == "\t":  # tab cycles views
            idx = self.VIEWS.index(self.view)
            self.view = self.VIEWS[(idx + 1) % len(self.VIEWS)]
            self.status_msg = f"View: {self.view}"

    def render(self) -> Layout:
        if self.show_help:
            return Layout(self._render_help())

        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main"),
            Layout(name="footer", size=3),
        )

        layout["header"].update(self._render_header())
        layout["footer"].update(self._render_controls())

        if self.view == "fleet":
            layout["main"].update(self._render_fleet_view())
        elif self.view == "game":
            layout["main"].update(self._render_game_view())
        elif self.view == "insights":
            layout["main"].update(self._render_insights_view())
        elif self.view == "quant":
            layout["main"].update(self._render_quant_view())

        return layout

    def _render_header(self) -> Panel:
        tick = self.state.get("tick", 0)
        winner = self.state.get("winner")
        gt = self.analytics.game_time(self.state) if self.state else "0:00"
        sd = self.analytics.sudden_death_in(self.state) if self.state else "?"

        status = f"[bold red]{winner} WINS[/bold red]" if winner else "[green]LIVE[/green]"

        # Bot faction split
        h_bots = sum(1 for b in self.bots if b.faction == "human")
        o_bots = sum(1 for b in self.bots if b.faction == "orc")
        split = f"[cyan]{h_bots}H[/cyan]/[red]{o_bots}O[/red]"

        # Runner status
        if self.runner and self.runner.alive():
            runner_tag = "[green]RUNNER:LIVE[/green]"
        elif self.runner:
            runner_tag = "[bold red]RUNNER:DEAD[/bold red]"
        else:
            runner_tag = "[dim]RUNNER:OFF[/dim]"

        extra = f"  [yellow]{self.status_msg}[/yellow]" if self.status_msg else ""
        stats = self.stats.summary_line()

        pulse_chars = [">", ">>", ">>>", ">>"]
        pulse = pulse_chars[self.cycle % len(pulse_chars)]
        now = datetime.now().strftime("%H:%M:%S")

        return Panel(Text.from_markup(
            f" [green]{pulse}[/green] G{self.game} {split} {gt} T{tick} {status} SD:{sd} "
            f"{runner_tag} [bold]{stats}[/bold] {now}{extra}"
        ), style="bold white on dark_blue")

    def _render_controls(self) -> Panel:
        return Panel(Text.from_markup(
            " [bold]VIEW-ONLY[/bold]  TAB=cycle view  h=help  q=quit (also kills ws_runner)"
        ), style="dim")

    # ── Fleet View ─────────────────────────────────────────

    def _render_fleet_view(self) -> Layout:
        layout = Layout()
        # Heights sum to ~41 on typical 40-50 line terminals.
        # If terminal is smaller, roster shrinks last (min=5) since it's the
        # only flex region. Previous bug: roster could get 0 lines and vanish.
        layout.split_column(
            Layout(name="top_bar", size=3),
            Layout(name="battlefield", size=13),
            Layout(name="roster", minimum_size=7),
            Layout(name="bottom", size=11),
        )
        layout["bottom"].split_row(
            Layout(name="events", ratio=1),
            Layout(name="runner", ratio=1),
            Layout(name="history", ratio=1),
        )
        layout["top_bar"].update(self._render_fleet_summary())
        layout["battlefield"].update(self._render_battlefield())
        layout["roster"].update(self._render_roster())
        layout["bottom"]["events"].update(self._render_logs())
        layout["bottom"]["runner"].update(self._render_runner_log())
        layout["bottom"]["history"].update(self._render_mini_history())
        return layout

    def _render_fleet_summary(self) -> Panel:
        """Compact fleet-wide stats bar."""
        if not self.state:
            return Panel("[dim]...[/dim]")
        faction = self.bots[0].faction if self.bots else None
        if not faction:
            return Panel("[dim]Waiting for faction...[/dim]")

        alive = sum(1 for b in self.bots if b.find_hero(self.state) and b.find_hero(self.state).get("alive"))
        dead = len(self.bots) - alive
        total_kills = sum(b.kills_est for b in self.bots)
        total_deaths = sum(b.deaths for b in self.bots)
        total_recalls = sum(b.recalls for b in self.bots)
        total_decisions = sum(b.decisions for b in self.bots)
        kd = total_kills / max(total_deaths, 1)

        # Level comparison
        enemy = "orc" if faction == "human" else "human"
        our_lvls = [h.get("level", 1) for h in self.state.get("heroes", []) if h.get("faction") == faction]
        enemy_lvls = [h.get("level", 1) for h in self.state.get("heroes", []) if h.get("faction") == enemy]
        our_avg = sum(our_lvls) / max(len(our_lvls), 1)
        enemy_avg = sum(enemy_lvls) / max(len(enemy_lvls), 1)
        gap = enemy_avg - our_avg
        gap_color = "green" if gap < 1 else ("yellow" if gap < 3 else "red")
        gap_str = f"[{gap_color}]{gap:+.1f}[/{gap_color}]"

        # Turtle mode indicator
        turtle = "[red]TURTLE[/red]" if gap >= 3 else ""

        # Total DPS
        total_dps = 0
        for b in self.bots:
            h = b.find_hero(self.state)
            if h and h.get("alive"):
                total_dps += self.analytics.est_dps(h)

        return Panel(Text.from_markup(
            f" [green]{alive}[/green]up [red]{dead}[/red]dead  |  "
            f"K/D: {total_kills}/{total_deaths} ({kd:.1f})  |  "
            f"Recalls: {total_recalls}  |  "
            f"Fleet DPS: {total_dps:.0f}  |  "
            f"Lvl: {our_avg:.1f} vs {enemy_avg:.1f} (gap {gap_str})  |  "
            f"Decisions: {total_decisions}  {turtle}"
        ), border_style="green")

    def _render_mini_history(self) -> Panel:
        """Compact historical stats panel."""
        lines = []
        games = self.stats.games
        if not games:
            return Panel("[dim]No history yet[/dim]", title="History", border_style="dim")

        # Majority wins (faction with more of our bots won)
        w = self.stats.wins
        l = self.stats.losses
        wr = 100 * w / len(games)
        wr_color = "green" if wr > 50 else ("yellow" if wr > 30 else "red")

        # Last 5 games with faction split
        last5 = games[-5:]
        streak_parts = []
        for g in last5:
            won = g.get("majority_won", g.get("won"))
            hb = g.get("human_bots", "?")
            ob = g.get("orc_bots", "?")
            if won:
                streak_parts.append(f"[green]W[/green]")
            else:
                streak_parts.append(f"[red]L[/red]")
        streak_bar = " ".join(streak_parts)

        lines.append(f"[bold]Majority W/L:[/bold]")
        lines.append(f"  {w}W/{l}L [{wr_color}]{wr:.0f}%[/{wr_color}]")
        lines.append(f"  [dim](W = faction with more bots won)[/dim]")
        lines.append(f"[bold]Last 5:[/bold] {streak_bar}")

        # Rolling
        for n in [5, 10]:
            recent = games[-n:] if len(games) >= n else games
            rw = sum(1 for g in recent if g.get("majority_won", g.get("won")))
            rwr = 100 * rw / len(recent)
            lines.append(f"  L{n}: {rw}W/{len(recent)-rw}L ({rwr:.0f}%)")

        # Best/worst style from history
        style_kd = {}
        for g in games:
            for b in g.get("bots", []):
                s = b.get("style", "?")
                if s not in style_kd:
                    style_kd[s] = {"k": 0, "d": 0}
                style_kd[s]["k"] += b.get("kills_est", 0)
                style_kd[s]["d"] += b.get("deaths", 0)

        lines.append("")
        lines.append("[bold]Style K/D:[/bold]")
        for s, d in sorted(style_kd.items(), key=lambda x: -x[1]["k"]/max(x[1]["d"],1)):
            kd = d["k"] / max(d["d"], 1)
            c = "green" if kd > 6 else ("yellow" if kd > 4 else "red")
            lines.append(f"  {s[:7]:7} [{c}]{kd:.1f}[/{c}] ({d['k']}k/{d['d']}d)")

        # Class K/D
        class_kd = {}
        for g in games:
            for b in g.get("bots", []):
                c = b.get("class", "?")
                if c not in class_kd:
                    class_kd[c] = {"k": 0, "d": 0, "lvl": 0, "n": 0}
                class_kd[c]["k"] += b.get("kills_est", 0)
                class_kd[c]["d"] += b.get("deaths", 0)
                class_kd[c]["lvl"] += b.get("level", 1)
                class_kd[c]["n"] += 1

        lines.append("[bold]Class K/D:[/bold]")
        for cls, d in sorted(class_kd.items(), key=lambda x: -x[1]["k"]/max(x[1]["d"],1)):
            kd = d["k"] / max(d["d"], 1)
            avg = d["lvl"] / max(d["n"], 1)
            c = "green" if kd > 6 else ("yellow" if kd > 4 else "red")
            lines.append(f"  {cls[:6]:6} [{c}]{kd:.1f}[/{c}] avgL{avg:.0f}")

        return Panel("\n".join(lines), title="[bold]History[/bold]", border_style="magenta")

    def _render_battlefield(self) -> Panel:
        """Compact lane/tower/hero overview always visible in fleet view."""
        if not self.state:
            return Panel("[dim]Waiting for game data...[/dim]", title="Battlefield")

        faction = self.bots[0].faction if self.bots else None
        enemy = "orc" if faction == "human" else "human" if faction else None
        my_names = {b.name for b in self.bots}

        # Bases
        bases = self.state.get("bases", {})
        h_b = bases.get("human", {})
        o_b = bases.get("orc", {})
        def mini_bar(hp, mx, w=12):
            p = hp / max(mx, 1)
            f = int(p * w)
            c = "green" if p > .5 else ("yellow" if p > .25 else "red")
            return f"[{c}]{'█' * f}{'░' * (w - f)}[/{c}] {int(hp)}"
        base_line = f"[cyan]H.Base[/cyan] {mini_bar(h_b.get('hp',0), h_b.get('maxHp',1500))}    [red]O.Base[/red] {mini_bar(o_b.get('hp',0), o_b.get('maxHp',1500))}"

        # Lane table — no row dividers (saves 3 lines, keeps all 3 lanes visible on compact terminals)
        t = Table(expand=True, show_lines=False, padding=(0, 1))
        t.add_column("Lane", width=4, style="bold")
        t.add_column("Our Twr", width=8)
        t.add_column("Us", width=5, justify="right")
        t.add_column("Frontline", width=22, justify="center")
        t.add_column("Them", width=5)
        t.add_column("Enemy Twr", width=8)
        t.add_column("Our Heroes", width=20)
        t.add_column("Enemy Heroes", width=20)

        tower_map = {}
        for tw in self.state.get("towers", []):
            tower_map[(tw["faction"], tw["lane"])] = tw

        for lane in ["top", "mid", "bot"]:
            ld = self.state.get("lanes", {}).get(lane, {})
            fl = ld.get("frontline", 0)

            our_u = ld.get(faction, 0) if faction else ld.get("human", 0)
            enemy_u = ld.get(enemy, 0) if enemy else ld.get("orc", 0)

            # Frontline bar
            w = 8
            pos = max(-w, min(w, int(fl / 12)))
            if faction == "orc":
                pos = -pos  # flip so "our side" is always left
            if pos >= 0:
                bar = f"[green]{'█' * pos}[/green]|[red]{'░' * (w - pos)}[/red] ({fl:+d})"
            else:
                bar = f"[green]{'░' * (w + pos)}[/green]|[red]{'█' * (-pos)}[/red] ({fl:+d})"

            # Towers
            our_tw = tower_map.get((faction, lane), {}) if faction else tower_map.get(("human", lane), {})
            enemy_tw = tower_map.get((enemy, lane), {}) if enemy else tower_map.get(("orc", lane), {})
            def twr_str(tw):
                if not tw.get("alive"):
                    return "[dim]DEAD[/dim]"
                hp = tw.get("hp", 0)
                mx = tw.get("maxHp", 1200)
                pct = hp / max(mx, 1)
                c = "green" if pct > .5 else ("yellow" if pct > .25 else "red")
                return f"[{c}]{int(hp)}[/{c}]"

            # Heroes per lane
            our_heroes = []
            enemy_heroes = []
            for h in self.state.get("heroes", []):
                if h.get("lane") != lane or not h.get("alive"):
                    continue
                name = h["name"]
                short = name[:7]
                lvl = h.get("level", 1)
                if h.get("faction") == faction:
                    style = "[bold green]" if name in my_names else "[cyan]"
                    our_heroes.append(f"{style}{short}L{lvl}[/]")
                else:
                    our_heroes_in_lane = sum(1 for hh in self.state.get("heroes", [])
                                             if hh.get("lane") == lane and hh.get("alive") and hh.get("faction") == faction)
                    enemy_heroes.append(f"[red]{short}L{lvl}[/red]")

            our_h_str = " ".join(our_heroes) if our_heroes else "[dim]empty[/dim]"
            enemy_h_str = " ".join(enemy_heroes) if enemy_heroes else "[dim]empty[/dim]"

            t.add_row(lane.upper(), twr_str(our_tw), str(our_u), bar, str(enemy_u),
                      twr_str(enemy_tw), our_h_str, enemy_h_str)

        return Panel(Group(Text.from_markup(base_line), t),
                     title="[bold]Battlefield[/bold]", border_style="blue")

    def _render_roster(self) -> Panel:
        STYLE_COLOR = {"offensive": "red", "defensive": "cyan", "balanced": "yellow", "random": "magenta"}
        t = Table(expand=True, show_lines=False, title="Fleet Roster")
        t.add_column("#", width=2)
        t.add_column("Name", width=12)
        t.add_column("Style", width=5)
        t.add_column("Cls", width=4)
        t.add_column("Lv", width=3, justify="right")
        t.add_column("HP", width=11, justify="right")
        t.add_column("Lane", width=4)
        t.add_column("DPS", width=5, justify="right")
        t.add_column("K/D", width=5, justify="right")
        t.add_column("Abilities", width=18)
        t.add_column("Act", width=5)

        for i, bot in enumerate(self.bots):
            hero = bot.find_hero(self.state) if self.state else None
            sel = " "
            row_style = ""
            sc = STYLE_COLOR.get(bot.style, "white")
            style_s = f"[{sc}]{bot.style[:4]}[/{sc}]"

            if hero and hero.get("alive"):
                hp = hero.get("hp", 0)
                mx = hero.get("maxHp", 1)
                pct = int(100 * hp / max(mx, 1))
                c = "green" if pct > 50 else ("yellow" if pct > 25 else "red")
                # HP delta indicator
                prev_hp = self._prev_hp.get(bot.name, hp)
                delta = int(hp - prev_hp)
                if delta < -10:
                    arrow = f" [red]v{-delta}[/red]"
                elif delta > 10:
                    arrow = f" [green]^{delta}[/green]"
                else:
                    arrow = ""
                self._prev_hp[bot.name] = hp
                hp_str = f"[{c}]{int(hp)}/{int(mx)}[/{c}]{arrow}"
                dps = f"{self.analytics.est_dps(hero):.0f}"
                lvl = str(hero.get("level", 1))
                abs_s = " ".join(f"{ABILITY_SHORT.get(a['id'], a['id'][:3])}:{a['level']}" for a in hero.get("abilities", []))
                if hero.get("abilityChoices"):
                    abs_s += " [yellow]UP![/yellow]"
                act = bot.last_action or bot.current_lane
            elif hero:
                self._prev_hp[bot.name] = 0
                hp_str = f"[red]DEAD[/red]"
                dps, lvl = "-", str(hero.get("level", 1))
                abs_s = " ".join(f"{ABILITY_SHORT.get(a['id'], a['id'][:3])}:{a['level']}" for a in hero.get("abilities", []))
                act = "[red]dead[/red]"
            elif bot.joined:
                hp_str, dps, lvl, abs_s, act = "?", "-", "?", "", "..."
            else:
                hp_str, dps, lvl, abs_s, act = "[dim]-[/dim]", "-", "-", "", "wait"

            kd = f"{bot.kills_est}/{bot.deaths}"

            t.add_row(
                f"{sel}{i}", bot.name[:11], style_s, bot.hero_class[:4],
                lvl, hp_str, bot.current_lane, dps, kd, abs_s, act,
                style=row_style,
            )

        return Panel(t, border_style="green")

    def _render_runner_log(self) -> Panel:
        """Live tail of ws_runner.log — what the runner is actually doing."""
        if not self.runner:
            return Panel("[dim]No runner attached.[/dim]",
                         title="[bold]ws_runner[/bold]", border_style="cyan")
        lines = self.runner.recent_lines(14)
        if not lines:
            content = "[dim]runner starting... (no log yet)[/dim]"
        else:
            # Escape Rich markup so log contents can never break the panel
            safe = [Text(ln).markup for ln in lines[-14:]]
            content = "\n".join(safe)
        status = "[green]LIVE[/green]" if self.runner.alive() else "[red]DEAD[/red]"
        title = f"[bold]ws_runner {status}[/bold]"
        return Panel(content, title=title, border_style="cyan")

    def _render_logs(self) -> Panel:
        feed_lines = self.events.render_lines(10)
        text = f"[bold]Live Feed[/bold]\n{feed_lines}"
        return Panel(text, title="[bold]Events[/bold]", border_style="yellow")

    # ── Game View ──────────────────────────────────────────

    def _render_game_view(self) -> Panel:
        if not self.state:
            return Panel("[dim]Waiting...[/dim]", title="Game")

        faction = self.bots[0].faction if self.bots else None
        my_names = {b.name for b in self.bots}

        # Bases
        bases = self.state.get("bases", {})
        def hp_bar(hp, mx, w=20):
            p = hp / max(mx, 1)
            f = int(p * w)
            c = "green" if p > .5 else ("yellow" if p > .25 else "red")
            return f"[{c}]{'█' * f}{'░' * (w - f)}[/{c}] {int(hp)}/{mx}"

        h_b = bases.get("human", {})
        o_b = bases.get("orc", {})
        base_text = f"[cyan]Human[/cyan] {hp_bar(h_b.get('hp',0), h_b.get('maxHp',1500))}   [red]Orc[/red] {hp_bar(o_b.get('hp',0), o_b.get('maxHp',1500))}"

        # Lanes
        lt = Table(expand=True, show_lines=True)
        lt.add_column("Lane", width=4, style="bold")
        lt.add_column("H.units", width=7, style="cyan", justify="right")
        lt.add_column("Frontline", width=24, justify="center")
        lt.add_column("O.units", width=7, style="red")
        lt.add_column("H.Twr", width=8, style="cyan")
        lt.add_column("O.Twr", width=8, style="red")
        lt.add_column("Heroes", width=30)

        tower_map = {}
        for t in self.state.get("towers", []):
            tower_map[(t["faction"], t["lane"])] = t

        for lane in ["top", "mid", "bot"]:
            ld = self.state.get("lanes", {}).get(lane, {})
            fl = ld.get("frontline", 0)
            w = 9
            pos = max(-w, min(w, int(fl / 12)))
            if pos >= 0:
                bar = f"[cyan]{'█'*pos}[/cyan]|[red]{'░'*(w-pos)}[/red] ({fl:+d})"
            else:
                bar = f"[cyan]{'░'*(w+pos)}[/cyan]|[red]{'█'*(-pos)}[/red] ({fl:+d})"

            ht = tower_map.get(("human", lane), {})
            ot = tower_map.get(("orc", lane), {})
            ht_s = f"{int(ht.get('hp',0))}" if ht.get("alive") else "[dim]DEAD[/dim]"
            ot_s = f"{int(ot.get('hp',0))}" if ot.get("alive") else "[dim]DEAD[/dim]"

            heroes_in_lane = []
            for h in self.state.get("heroes", []):
                if h.get("lane") == lane and h.get("alive"):
                    marker = "[bold green]" if h["name"] in my_names else (
                        "[cyan]" if h.get("faction") == faction else "[red]")
                    heroes_in_lane.append(f"{marker}{h['name'][:8]}L{h['level']}[/]")
            heroes_str = " ".join(heroes_in_lane)

            lt.add_row(lane.upper(), str(ld.get("human", 0)), bar,
                       str(ld.get("orc", 0)), ht_s, ot_s, heroes_str)

        # All heroes
        ht2 = Table(expand=True, show_lines=True, title="All Heroes")
        ht2.add_column("Name", width=14)
        ht2.add_column("F")
        ht2.add_column("Cls", width=3)
        ht2.add_column("Lv", width=3)
        ht2.add_column("Lane", width=4)
        ht2.add_column("HP", width=12)
        ht2.add_column("DPS", width=5)
        ht2.add_column("Abilities", width=22)

        for h in sorted(self.state.get("heroes", []),
                        key=lambda x: (0 if x.get("faction") == faction else 1, x.get("lane",""))):
            mine = h["name"] in my_names
            ally = h.get("faction") == faction
            alive = h.get("alive", False)
            style = "bold green" if mine else ("cyan" if ally else "red")
            if not alive:
                style = f"dim {style}"
            fi = "H" if h["faction"] == "human" else "O"
            hp_s = f"{int(h.get('hp',0))}/{int(h.get('maxHp',0))}" if alive else "DEAD"
            dps_s = f"{self.analytics.est_dps(h):.0f}" if alive else "-"
            ab = " ".join(f"{ABILITY_SHORT.get(a['id'],a['id'][:3])}:{a['level']}" for a in h.get("abilities",[]))
            ht2.add_row(Text(h["name"], style=style), fi, h.get("class","?")[:3],
                        str(h.get("level",1)), h.get("lane","?"), hp_s, dps_s, ab)

        # Power comparison
        if faction:
            o = self.analytics.team_power(self.state, faction)
            ef = "orc" if faction == "human" else "human"
            e = self.analytics.team_power(self.state, ef)
            power = (f"[bold]Us:[/bold] {o['alive']}up L{o['avg_level']:.1f} DPS:{o['total_dps']:.0f} HP:{int(o['total_hp'])}  "
                     f"[bold]Them:[/bold] {e['alive']}up L{e['avg_level']:.1f} DPS:{e['total_dps']:.0f} HP:{int(e['total_hp'])}")
        else:
            power = ""

        return Panel(Group(
            Text.from_markup(base_text), "",
            lt, ht2,
            Text.from_markup(power),
        ), title="[bold]Game State[/bold]", border_style="blue")

    # ── Insights View ──────────────────────────────────────

    def _render_insights_view(self) -> Layout:
        layout = Layout()
        layout.split_row(
            Layout(name="left", ratio=1),
            Layout(name="right", ratio=1),
        )

        faction = self.bots[0].faction if self.bots and self.bots[0].faction else None

        # Left: strategic analysis
        if faction and self.state:
            enemy = "orc" if faction == "human" else "human"
            tick = self.state.get("tick", 0)
            lines = []

            # Timing
            lines.append(f"[bold]Game Clock[/bold]")
            lines.append(f"  Time: {self.analytics.game_time(self.state)}")
            lines.append(f"  Sudden Death: {self.analytics.sudden_death_in(self.state)}")
            if tick < TOWER_BUFF_TICKS:
                lines.append(f"  [cyan]Tower buff: {(TOWER_BUFF_TICKS - tick) / TICK_RATE:.0f}s left[/cyan]")
            lines.append("")

            # Dragon
            enemy_dead = sum(1 for t in self.state.get("towers",[]) if t["faction"]==enemy and not t.get("alive"))
            our_dead = sum(1 for t in self.state.get("towers",[]) if t["faction"]==faction and not t.get("alive"))
            lines.append(f"[bold]Dragon Status[/bold]")
            lines.append(f"  Our towers down: {our_dead}/3" + (" [red]THEIR DRAGON![/red]" if our_dead >= 3 else ""))
            lines.append(f"  Enemy towers down: {enemy_dead}/3" + (" [green]OUR DRAGON![/green]" if enemy_dead >= 3 else ""))
            lines.append("")

            # Underdog
            our_c = len([h for h in self.state.get("heroes",[]) if h["faction"]==faction])
            their_c = len([h for h in self.state.get("heroes",[]) if h["faction"]==enemy])
            if our_c != their_c and min(our_c, their_c) > 0:
                if our_c < their_c:
                    bonus = (their_c - our_c) / our_c * 25
                    lines.append(f"[green]Underdog XP: +{bonus:.0f}% ({our_c}v{their_c})[/green]")
                else:
                    bonus = (our_c - their_c) / their_c * 25
                    lines.append(f"[yellow]Enemy underdog: +{bonus:.0f}% ({our_c}v{their_c})[/yellow]")
                lines.append("")

            # Lane pressure
            lines.append(f"[bold]Lane Analysis[/bold]")
            for lane in ["top", "mid", "bot"]:
                ld = self.state.get("lanes",{}).get(lane,{})
                our_u = ld.get(faction, 0)
                enemy_u = ld.get(enemy, 0)
                fl = ld.get("frontline", 0)
                our_t = [t for t in self.state.get("towers",[]) if t["faction"]==faction and t["lane"]==lane]
                enemy_t = [t for t in self.state.get("towers",[]) if t["faction"]==enemy and t["lane"]==lane]
                ot_hp = f"{int(our_t[0]['hp'])}" if our_t and our_t[0].get("alive") else "DEAD"
                et_hp = f"{int(enemy_t[0]['hp'])}" if enemy_t and enemy_t[0].get("alive") else "DEAD"
                our_h = sum(1 for b in self.bots if b.current_lane == lane)
                lines.append(f"  {lane.upper()}: {our_u}v{enemy_u} units  fl:{fl:+d}  twr:{ot_hp}vs{et_hp}  bots:{our_h}")

            lines.append("")

            # Threats
            lines.append(f"[bold]Enemy Threats[/bold]")
            enemies = [h for h in self.state.get("heroes",[]) if h["faction"]==enemy and h.get("alive")]
            for h in sorted(enemies, key=lambda x: -x.get("level",1)):
                dps = self.analytics.est_dps(h)
                ab = "+".join(a["id"][:4] for a in h.get("abilities",[]))
                lines.append(f"  {h['name']} L{h['level']} {h['class'][:3]} {h['lane']} DPS:{dps:.0f} [{ab}]")

            left_panel = Panel("\n".join(lines), title="[bold]Analysis[/bold]", border_style="yellow")
        else:
            left_panel = Panel("[dim]Waiting...[/dim]", title="Analysis")

        # Right: recommendations + fleet distribution + logs
        right_lines = []
        if faction and self.state:
            right_lines.append("[bold]Recommendations[/bold]")
            tick = self.state.get("tick", 0)

            if tick < TOWER_BUFF_TICKS:
                right_lines.append("  [cyan]Farm phase: tower buff active, don't push[/cyan]")
            elif tick < SUDDEN_DEATH_TICKS * 0.5:
                right_lines.append("  [green]Mid game: push weakest tower[/green]")
            elif tick < SUDDEN_DEATH_TICKS * 0.85:
                right_lines.append("  [yellow]Late game: consider converge push[/yellow]")
            else:
                right_lines.append("  [red]Endgame: CONVERGE NOW or lose to SD[/red]")

            # Find weakest enemy tower
            weakest = None
            for t in self.state.get("towers",[]):
                if t["faction"] == enemy and t.get("alive"):
                    if not weakest or t["hp"] < weakest["hp"]:
                        weakest = t
            if weakest:
                right_lines.append(f"  Weakest enemy tower: {weakest['lane']} at {int(weakest['hp'])} HP")

            right_lines.append("")

            # Fleet distribution
            right_lines.append("[bold]Fleet Distribution[/bold]")
            for lane in ["top", "mid", "bot"]:
                bots_here = [b for b in self.bots if b.current_lane == lane]
                names = ", ".join(f"{b.name[:8]}({b.role[0]})" for b in bots_here)
                right_lines.append(f"  {lane.upper()}: [{len(bots_here)}] {names}")

            right_lines.append("")
            right_lines.append("[bold]Fleet Health[/bold]")
            alive = 0
            dead = 0
            total_dps = 0
            for b in self.bots:
                h = b.find_hero(self.state)
                if h and h.get("alive"):
                    alive += 1
                    total_dps += self.analytics.est_dps(h)
                elif h:
                    dead += 1
            right_lines.append(f"  Alive: {alive}  Dead: {dead}  Total DPS: {total_dps:.0f}")
            right_lines.append(f"  Total decisions: {sum(b.decisions for b in self.bots)}")
            right_lines.append(f"  Total errors: {sum(b.errors for b in self.bots)}")

        right_lines.append("")
        right_lines.append("[bold]Recent Fleet Log[/bold]")
        all_logs = []
        for b in self.bots:
            for entry in b.log[-3:]:
                all_logs.append(f"[dim]{b.name[:8]}[/dim] {entry}")
        for entry in all_logs[-8:]:
            right_lines.append(f"  {entry}")

        right_panel = Panel("\n".join(right_lines), title="[bold]Commander[/bold]", border_style="green")

        layout["left"].update(left_panel)
        layout["right"].update(right_panel)
        return layout

    # ── Quant View ──────────────────────────────────────────

    def _render_quant_view(self) -> Layout:
        layout = Layout()
        layout.split_row(
            Layout(name="left", ratio=1),
            Layout(name="mid", ratio=1),
            Layout(name="right", ratio=1),
        )

        faction = self.bots[0].faction if self.bots and self.bots[0].faction else None

        # LEFT: Lane matchups + advantage score
        left_lines = []
        if faction and self.state:
            analysis = quant.game_state_analysis(self.state, faction)

            # Advantage meter
            adv = analysis["advantage_score"]
            bar_w = 20
            mid = bar_w // 2
            if adv >= 0:
                pos = min(mid, int(adv / 100 * mid))
                meter = "░" * mid + "[green]" + "█" * pos + "[/green]" + "░" * (mid - pos)
            else:
                pos = min(mid, int(-adv / 100 * mid))
                meter = "░" * (mid - pos) + "[red]" + "█" * pos + "[/red]" + "░" * mid
            adv_color = "green" if adv > 10 else ("red" if adv < -10 else "yellow")
            left_lines.append(f"[bold]Advantage:[/bold] [{adv_color}]{adv:+.0f}[/{adv_color}]  {meter}")
            left_lines.append(f"Phase: [bold]{analysis['phase']}[/bold]  SD: {analysis['sudden_death_in']:.0f}s")
            left_lines.append(f"DPS: {analysis['our_total_dps']:.0f} vs {analysis['enemy_total_dps']:.0f}  "
                              f"Alive: {analysis['our_alive']} vs {analysis['enemy_alive']}")
            left_lines.append(f"Towers: {analysis['our_towers']}/3 vs {analysis['enemy_towers']}/3  "
                              f"HP: {int(analysis['our_tower_hp'])} vs {int(analysis['enemy_tower_hp'])}")
            if analysis["dragon_for_us"]:
                left_lines.append("[bold green]DRAGON SPAWNING FOR US[/bold green]")
            elif analysis["dragon_for_them"]:
                left_lines.append("[bold red]DRAGON SPAWNING FOR THEM[/bold red]")
            left_lines.append("")

            # Lane matchups
            left_lines.append("[bold]Lane Matchups[/bold]")
            for lane in ["top", "mid", "bot"]:
                m = analysis["matchups"][lane]
                wp = m["win_prob"]
                wp_color = "green" if wp > 0.6 else ("red" if wp < 0.4 else "yellow")
                rec = m["recommendation"].upper()
                rec_color = {"PUSH": "green", "HOLD": "yellow", "RETREAT": "red"}.get(rec, "white")

                left_lines.append(f"  [bold]{lane.upper()}[/bold] [{wp_color}]{wp:.0%}[/{wp_color}] [{rec_color}]{rec}[/{rec_color}]")
                left_lines.append(f"    Us: {m['our_count']}h {m['our_units']}u DPS:{m['our_dps']:.0f} HP:{int(m['our_hp'])}")
                left_lines.append(f"    Them: {m['enemy_count']}h {m['enemy_units']}u DPS:{m['enemy_dps']:.0f} HP:{int(m['enemy_hp'])}")
                if m["our_count"] > 0 and m["enemy_count"] > 0:
                    left_lines.append(f"    TTW: us {m['our_ttw']:.1f}s vs them {m['enemy_ttw']:.1f}s")

            # Kill EV for each enemy
            left_lines.append("")
            left_lines.append("[bold]Kill EV (should I fight?)[/bold]")
            enemy_f = "orc" if faction == "human" else "human"
            our_avg_lvl = sum(h.get("level", 1) for h in self.state.get("heroes", [])
                              if h["faction"] == faction) / max(1, analysis["our_alive"])
            for h in self.state.get("heroes", []):
                if h["faction"] == enemy_f and h.get("alive"):
                    # Rough win prob based on level diff
                    lvl_diff = our_avg_lvl - h["level"]
                    rough_prob = max(0.1, min(0.9, 0.5 + lvl_diff * 0.05))
                    ev = quant.kill_ev(int(our_avg_lvl), h["level"], rough_prob)
                    ev_color = "green" if ev > 0 else "red"
                    left_lines.append(f"  {h['name'][:10]} L{h['level']}: [{ev_color}]EV={ev:+.0f}[/{ev_color}] "
                                      f"(bounty:{quant.kill_xp_value(h['level'])}xp)")

        left_panel = Panel("\n".join(left_lines) if left_lines else "[dim]Waiting...[/dim]",
                           title="[bold]Live Analysis[/bold]", border_style="cyan")

        # MID: Power curves + death cost table
        mid_lines = []
        mid_lines.append("[bold]Death Cost by Level[/bold]")
        dc_table = Table(expand=True, show_lines=False, padding=(0, 1))
        dc_table.add_column("Lv", width=3)
        dc_table.add_column("Bounty", width=6)
        dc_table.add_column("Respawn", width=7)
        dc_table.add_column("XP Lost", width=7)
        dc_table.add_column("Total Cost", width=9)
        for lvl in [1, 3, 5, 7, 9, 12, 15, 18]:
            dc = quant.death_cost(lvl)
            cost_color = "green" if dc["total_xp_cost"] < 300 else ("yellow" if dc["total_xp_cost"] < 500 else "red")
            dc_table.add_row(
                str(lvl), str(dc["xp_given"]), f"{dc['respawn_sec']:.0f}s",
                f"{dc['xp_lost_farming']:.0f}", f"[{cost_color}]{dc['total_xp_cost']:.0f}[/{cost_color}]"
            )

        # DPS comparison table
        dps_table = Table(expand=True, show_lines=False, padding=(0, 1), title="DPS by Level")
        dps_table.add_column("Lv", width=3)
        dps_table.add_column("Melee", width=6)
        dps_table.add_column("Ranged", width=6)
        dps_table.add_column("Mage", width=6)
        dps_table.add_column("Mage+FB", width=7)
        for lvl in [1, 3, 6, 9, 12, 15]:
            m = quant.hero_stats("melee", lvl)
            r = quant.hero_stats("ranged", lvl)
            ma = quant.hero_stats("mage", lvl)
            ma_fb = quant.hero_stats("mage", lvl, [{"id": "fireball", "level": min(3, lvl // 3)}])
            dps_table.add_row(str(lvl), f"{m['dps']:.0f}", f"{r['dps']:.0f}",
                              f"{ma['dps']:.0f}", f"{ma_fb['dps']:.0f}")

        # HP comparison
        hp_table = Table(expand=True, show_lines=False, padding=(0, 1), title="HP by Level")
        hp_table.add_column("Lv", width=3)
        hp_table.add_column("Melee", width=6)
        hp_table.add_column("+Fort", width=6)
        hp_table.add_column("Ranged", width=6)
        hp_table.add_column("Mage", width=6)
        for lvl in [1, 3, 6, 9, 12, 15]:
            m = quant.hero_stats("melee", lvl)
            mf = quant.hero_stats("melee", lvl, [{"id": "fortitude", "level": min(3, lvl // 3)}])
            r = quant.hero_stats("ranged", lvl)
            ma = quant.hero_stats("mage", lvl)
            hp_table.add_row(str(lvl), f"{m['hp']:.0f}", f"{mf['hp']:.0f}",
                             f"{r['hp']:.0f}", f"{ma['hp']:.0f}")

        # Tower push estimates
        push_lines = []
        if faction and self.state:
            push_lines.append("[bold]Tower Push Estimates[/bold]")
            our_heroes = [h for h in self.state.get("heroes", [])
                          if h["faction"] == faction and h.get("alive")]
            if our_heroes:
                avg_dps = sum(quant.hero_stats(h["class"], h["level"], h.get("abilities", []))["dps"]
                              for h in our_heroes) / len(our_heroes)
                avg_hp = sum(quant.hero_stats(h["class"], h["level"], h.get("abilities", []))["hp"]
                             for h in our_heroes) / len(our_heroes)
                for n in [2, 3, 5, 8]:
                    p = quant.tower_push_time(n, avg_dps, avg_hp)
                    safe = "[green]safe[/green]" if p["safe_push"] else f"[red]{p['hero_deaths_est']}deaths[/red]"
                    push_lines.append(f"  {n} heroes: {p['push_time_sec']:.0f}s {safe}")

        mid_panel = Panel(
            Group(dc_table, "", dps_table, "", hp_table, "", "\n".join(push_lines)),
            title="[bold]Power Tables[/bold]", border_style="yellow"
        )

        # RIGHT: Historical analysis
        right_lines = []
        hist = quant.analyze_history(self.stats.games)
        if hist["total"] > 0:
            right_lines.append("[bold]Historical Record[/bold]")
            wr_color = "green" if hist["winrate"] > 55 else ("red" if hist["winrate"] < 45 else "yellow")
            right_lines.append(f"  Total: {hist['total']} games")
            right_lines.append(f"  W/L: {hist['wins']}/{hist['losses']} [{wr_color}]({hist['winrate']:.0f}%)[/{wr_color}]")
            right_lines.append(f"  Last 5: {hist['rolling_5']:.0f}%  Last 10: {hist['rolling_10']:.0f}%")
            right_lines.append(f"  Avg game: {hist['avg_duration_sec']:.0f}s")
            right_lines.append("")

            # Faction performance
            right_lines.append("[bold]By Faction[/bold]")
            for f, d in hist.get("faction_wins", {}).items():
                total = d["wins"] + d["losses"]
                wr = d["wins"] / total * 100 if total > 0 else 0
                right_lines.append(f"  {f}: {d['wins']}W/{d['losses']}L ({wr:.0f}%)")
            right_lines.append("")

            # Style performance
            right_lines.append("[bold]By Style[/bold]")
            st = Table(expand=True, show_lines=False, padding=(0, 1))
            st.add_column("Style", width=8)
            st.add_column("K/D", width=6)
            st.add_column("Avg Lv", width=6)
            st.add_column("Games", width=5)
            for style, d in sorted(hist.get("style_stats", {}).items()):
                kd_color = "green" if d["kd_ratio"] > 1.5 else ("red" if d["kd_ratio"] < 0.8 else "yellow")
                st.add_row(style[:8], f"[{kd_color}]{d['kd_ratio']:.1f}[/{kd_color}]",
                           f"{d['avg_level']:.1f}", str(d["games"]))

            # Class performance
            right_lines.append("")
            right_lines.append("[bold]By Class[/bold]")
            ct = Table(expand=True, show_lines=False, padding=(0, 1))
            ct.add_column("Class", width=7)
            ct.add_column("K/D", width=6)
            ct.add_column("Avg Lv", width=6)
            ct.add_column("Games", width=5)
            for cls, d in sorted(hist.get("class_stats", {}).items()):
                kd_color = "green" if d["kd_ratio"] > 1.5 else ("red" if d["kd_ratio"] < 0.8 else "yellow")
                ct.add_row(cls[:7], f"[{kd_color}]{d['kd_ratio']:.1f}[/{kd_color}]",
                           f"{d['avg_level']:.1f}", str(d["games"]))

            right_panel = Panel(
                Group("\n".join(right_lines), st, ct),
                title="[bold]History[/bold]", border_style="green"
            )
        else:
            right_panel = Panel("[dim]No games recorded yet. Play some games![/dim]",
                                title="History", border_style="green")

        layout["left"].update(left_panel)
        layout["mid"].update(mid_panel)
        layout["right"].update(right_panel)
        return layout

    def _render_help(self) -> Panel:
        return Panel("""
[bold]DASHBOARD (view-only)[/bold]

  [bold]Views[/bold] (TAB to cycle)
    fleet     Roster + battlefield + runner log + history
    game      Full game state with every hero
    insights  Strategic commentary
    quant     Game theory: matchups, EV, power curves, history

  [bold]Keys[/bold]
    TAB  Cycle view
    h    Toggle this help
    q    Quit   (also SIGTERMs the ws_runner subprocess)

[bold]RUNNER[/bold]
  ws_runner.py is spawned on dashboard startup. It owns:
    - /api/wallet/connect  (holder buff + skin NFT check)
    - /api/strategy/deployment (deploy + ability + recall + lane)
    - stats.json            (single writer, full schema)
  Dashboard only reads /api/game/state + ws_runner.log.

[bold]GAME TIMING[/bold]
  Early (0:00-1:45) tower buff active; mage farm safely.
  Mid   (1:45-10:00) push weak towers, dragon when 3 down.
  Late  (10:00+) converge on weakest lane, group for sudden death.

[dim]Press h to close[/dim]
""", title="[bold]Help[/bold]", border_style="cyan")

    def run(self):
        old_settings = termios.tcgetattr(sys.stdin)
        try:
            tty.setcbreak(sys.stdin.fileno())
            with Live(self.render(), console=self.console, refresh_per_second=4, screen=True) as live:
                last_poll = 0
                while self.running:
                    if select.select([sys.stdin], [], [], 0.1)[0]:
                        self.handle_key(sys.stdin.read(1))

                    now = time.time()
                    if now - last_poll >= POLL_INTERVAL:
                        # Respawn ws_runner if it died (crash or external kill).
                        if self.runner and self.runner.ensure_alive():
                            self.status_msg = f"ws_runner respawned (#{self.runner._respawn_count})"
                        self.state = api_get("/api/game/state", {"game": self.game})
                        if self.state:
                            self.analytics.update(self.state)
                            my_names = {b.name for b in self.bots}
                            self.events.diff_state(self.state, my_names)
                            # View-only: observe state, never POST. ws_runner
                            # is the sole writer of /api/strategy/deployment
                            # AND of stats.json (see its _record_game).
                            for bot in self.bots:
                                bot.observe(self.state)
                            # StatsTracker reloads so freshly-written rows
                            # from ws_runner appear without dashboard restart.
                            self.stats.load()
                            self.cycle += 1
                        last_poll = now

                    live.update(self.render())
        except KeyboardInterrupt:
            pass
        finally:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
            if self.runner:
                self.runner.stop()
            self.console.print("[yellow]Fleet shutdown. ws_runner terminated.[/yellow]")


# ── Main ───────────────────────────────────────────────────

def main():
    if not os.path.exists(FLEET_FILE):
        print(f"No fleet.json found at {FLEET_FILE}")
        sys.exit(1)

    with open(FLEET_FILE) as f:
        fleet = json.load(f)

    game = fleet.get("game", 3)

    bots = []
    for b in fleet["bots"]:
        bots.append(BotBrain(
            name=b["name"], api_key=b["key"], hero_class=b["class"],
            default_lane=b.get("lane", "mid"), role=b.get("role", "dps"),
            ability_prio=b.get("ability_prio", ["fury", "fortitude"]),
            style=b.get("style", "balanced"),
            game=game,
        ))

    print(f"Fleet: {len(bots)} bots ready for game {game}")
    print(f"Comp: {sum(1 for b in bots if b.hero_class=='melee')}M / "
          f"{sum(1 for b in bots if b.hero_class=='ranged')}R / "
          f"{sum(1 for b in bots if b.hero_class=='mage')}Ma")
    print("Spawning ws_runner (background)...")
    runner = RunnerProcess([sys.executable, "ws_runner.py"], RUNNER_LOG)
    runner.start()
    print(f"ws_runner pid={runner.proc.pid}  log={RUNNER_LOG}")
    print("Launching dashboard (view-only)...")

    dashboard = Dashboard(bots=bots, game=game, runner=runner)
    try:
        dashboard.run()
    finally:
        runner.stop()


if __name__ == "__main__":
    main()
