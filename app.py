#!/usr/bin/env python3
"""HORDE AGENTS - State of the Art MOBA AI Dashboard
6-tab Textual TUI: Fleet, Game, History, Quant, RL Training, Leaderboard
4-layer learning brain: UCB1 + Adaptive Recall + Behavioral Cloning + Q-Learning
"""

from __future__ import annotations
import json, os, random, math
from datetime import datetime

import requests
from textual.app import App, ComposeResult
from textual.containers import Horizontal, ScrollableContainer
from textual.widgets import Header, Footer, Static, DataTable, TabbedContent, TabPane, RichLog
from textual.reactive import reactive
from textual import work
from rich.text import Text
from rich.table import Table
from rich.console import Group

import quant
from brain import LearningBrain
from rl_engine import RLBrain, RewardCalculator

BASE = "https://wc2-agentic-dev-3o6un.ondigitalocean.app"
DIR = os.path.dirname(os.path.abspath(__file__))
FLEET_FILE = os.path.join(DIR, "fleet.json")
STATS_FILE = os.path.join(DIR, "stats.json")
TICK_RATE = 20
SUDDEN_DEATH_TICKS = 15 * 60 * TICK_RATE
TOWER_BUFF_TICKS = 105 * TICK_RATE
LANES = ["top", "mid", "bot"]
ABILITY_SHORT = {
    "cleave":"clv","thorns":"thr","divine_shield":"dsh","volley":"vol",
    "bloodlust":"blt","critical_strike":"crt","fireball":"fbl","tornado":"trn",
    "raise_skeleton":"skl","fortitude":"frt","fury":"fur","stim_pack":"stm",
}

def api_get(path, params=None):
    try:
        r = requests.get(f"{BASE}{path}", params=params, timeout=10)
        return r.json() if r.status_code == 200 else {}
    except: return {}

def api_post(path, key, payload):
    try:
        r = requests.post(f"{BASE}{path}", headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"}, json=payload, timeout=10)
        return r.json() if r.status_code == 200 else {"error": str(r.status_code)}
    except Exception as e: return {"error": str(e)[:60]}

def is_our_bot(name): return name.startswith("ExH") or name.startswith("ExHuman")

def parse_dur(v):
    if isinstance(v,(int,float)): return float(v)
    if isinstance(v,str) and ":" in v:
        p=v.split(":"); return int(p[0])*60+int(p[1])
    return 0

# ── Stats ──────────────────────────────────────────────────

class Stats:
    def __init__(self):
        self.games = []
        if os.path.exists(STATS_FILE):
            try:
                with open(STATS_FILE) as f: self.games = json.load(f)
            except: pass

    def record(self, winner, state, bots):
        fc = {"human":0,"orc":0}
        for b in bots:
            if b.faction in fc: fc[b.faction] += 1
        maj = "human" if fc["human"]>fc["orc"] else "orc" if fc["orc"]>fc["human"] else max(fc, key=lambda f: sum(
            b.find_hero(state).get("level",1) for b in bots if b.faction==f and b.find_hero(state)) or 0)
        snaps = []
        for b in bots:
            h = b.find_hero(state)
            s = {"name":b.name,"class":b.hero_class,"style":b.style,"faction":b.faction,
                 "kills_est":b.kills_est,"deaths":b.deaths,"won":b.faction==winner}
            if h:
                s["level"]=h.get("level",1)
                s["abilities"]=[{"id":a["id"],"level":a["level"]} for a in h.get("abilities",[])]
                s["hp"]=h.get("hp",0); s["maxHp"]=h.get("maxHp",0)
                s["lane"]=h.get("lane","?"); s["xp"]=h.get("xp",0)
            snaps.append(s)
        self.games.append({
            "time":datetime.now().isoformat(),"winner":winner,
            "majority_faction":maj,"majority_won":winner==maj,
            "human_bots":fc["human"],"orc_bots":fc["orc"],
            "tick":state.get("tick",0),"game_time":state.get("tick",0)/TICK_RATE,
            "human_max_level":max((h.get("level",1) for h in state.get("heroes",[]) if h["faction"]=="human"),default=1),
            "orc_max_level":max((h.get("level",1) for h in state.get("heroes",[]) if h["faction"]=="orc"),default=1),
            "human_base_hp":state.get("bases",{}).get("human",{}).get("hp",0),
            "orc_base_hp":state.get("bases",{}).get("orc",{}).get("hp",0),
            "bots":snaps,
        })
        with open(STATS_FILE,"w") as f: json.dump(self.games,f,indent=2)

    @property
    def wins(self): return sum(1 for g in self.games if g.get("majority_won",g.get("won")))
    def summary(self):
        t=len(self.games)
        return f"{self.wins}W/{t-self.wins}L ({100*self.wins//t}%)" if t else "0 games"


# ── Bot Brain ──────────────────────────────────────────────

class Bot:
    def __init__(self, cfg, game):
        self.name=cfg["name"]; self.api_key=cfg["key"]; self.hero_class=cfg["class"]
        self.default_lane=cfg.get("lane","mid"); self.role=cfg.get("role","mage")
        self.ability_prio=cfg.get("ability_prio",["raise_skeleton","fireball","fortitude"])
        self.style=cfg.get("style","defensive"); self.game=game
        self.joined=False; self.faction=None; self.current_lane=self.default_lane
        self.last_action=""; self.kills_est=0; self.deaths=0; self.recalls=0
        self.decisions=0; self.errors=0
        self._prev_alive=True; self._prev_xp=0; self._lane_tick=0; self._prev_hero=None
        self.brain=None; self.rl=None
        self.strategy_override=None
        self.target=cfg.get("target")  # counter target (e.g. "lmeow")

    def find_hero(self, s):
        for h in s.get("heroes",[]):
            if h.get("name")==self.name:
                self.faction=h.get("faction"); return h
        return None

    def _count(self, s, lane, faction):
        return sum(1 for h in s.get("heroes",[]) if h.get("lane")==lane and h.get("faction")==faction and h.get("alive"))

    def pick_ability(self, hero):
        choices=hero.get("abilityChoices",[])
        if not choices: return None
        for a in self.ability_prio:
            if a in choices: return a
        return choices[0]

    def pick_lane(self, state, hero, all_bots):
        """ZEN MODE: Pick best lane ONCE at game start, STAY FOREVER.
        ExH_Mage2 (unmanaged, never switches) hits L14. Our managed bots hit L7.
        Every lane switch = 216 XP lost. Solution: stop switching.
        Only move for: manual override, 1v4+ extreme danger, last 90s endgame.
        RL still learns passively (data collection) for future improvements.
        """
        so=self.strategy_override
        if so and so.startswith("converge:"): return so.split(":")[1]
        if so in LANES: return so

        faction=self.faction or ""; enemy="orc" if faction=="human" else "human"
        tick=state.get("tick",0)
        lane_enemies={l:self._count(state,l,enemy) for l in LANES}

        # you can add custom lane logic here (e.g. counter specific players)

        # INITIAL PICK (once per game)
        if self._lane_tick==0:
            avoid=set()
            for b in all_bots:
                if b.name!=self.name and b.faction==enemy: avoid.add(b.current_lane)
            bot_lanes=set()
            for b in all_bots:
                if b.name!=self.name and b.faction==faction: bot_lanes.add(b.current_lane)
            best,best_s=self.default_lane,-999
            for lane in LANES:
                if lane in avoid: continue
                s=0
                if lane in bot_lanes: s-=20
                s-=lane_enemies.get(lane,0)*5
                for t in state.get("towers",[]):
                    if t["faction"]==faction and t["lane"]==lane and t.get("alive"): s+=15
                if s>best_s: best_s=s; best=lane
            self._lane_tick=tick; self.current_lane=best; return best

        cur=self.current_lane
        game_secs = tick / TICK_RATE
        level = hero.get("level", 1)

        # EMERGENCY: 1v4+ = certain death
        cur_e=lane_enemies.get(cur,0); cur_a=self._count(state,cur,faction)
        if cur_e>=cur_a+3:
            for lane in LANES:
                if lane!=cur and lane_enemies.get(lane,0)<2:
                    self._lane_tick=tick; return lane

        # === TOWER-PUSH STATE MACHINE ===
        # Break zen mode ONLY for strategic tower snipes (one-time switch)

        # Find enemy tower HP per lane
        enemy_towers = {}
        our_tower_count = 0
        enemy_tower_count = 0
        for t in state.get("towers", []):
            if t["faction"] == enemy:
                enemy_towers[t["lane"]] = t
                if t.get("alive"): enemy_tower_count += 1
            elif t["faction"] == faction and t.get("alive"):
                our_tower_count += 1

        # PHASE 1: PRE-RUSH RECALL (1:20-1:40, recall for full HP before tower buff expires)
        if 80 < game_secs < 100 and hero.get("hp", 0) < hero.get("maxHp", 1) * 0.9:
            # Don't switch lane, just flag for recall (handled in should_recall)
            pass

        # PHASE 2: TOWER SNIPE (any time after 1:45)
        # If enemy tower < 400 HP, converge there (ONE switch, then stay)
        if game_secs > 105 and level >= 5:
            for lane in LANES:
                t = enemy_towers.get(lane, {})
                if t.get("alive") and t.get("hp", 1200) < 400 and lane != cur:
                    # Only switch if we haven't switched recently (avoid shove spam)
                    if tick - self._lane_tick > 60 * TICK_RATE:
                        self._lane_tick = tick; return lane

        # PHASE 3: DRAGON SETUP (2 enemy towers down, converge on 3rd)
        if enemy_tower_count == 1 and level >= 8:
            for lane in LANES:
                t = enemy_towers.get(lane, {})
                if t.get("alive") and lane != cur:
                    if tick - self._lane_tick > 60 * TICK_RATE:
                        self._lane_tick = tick; return lane

        # PHASE 4: ENDGAME (after 12:00, converge for final push)
        if game_secs > 720:
            # Find lane closest to enemy base (fewest structures remaining)
            push_lane = None
            for lane in LANES:
                t = enemy_towers.get(lane, {})
                if not t.get("alive"):  # tower already down, push here
                    push_lane = lane; break
            if not push_lane:
                # All towers up, pick weakest
                weakest = min(enemy_towers.values(), key=lambda t: t.get("hp", 9999)) if enemy_towers else None
                if weakest: push_lane = weakest.get("lane", "mid")
            if push_lane and push_lane != cur and tick - self._lane_tick > 60 * TICK_RATE:
                self._lane_tick = tick; return push_lane

        # PHASE 5: SUDDEN DEATH (last 90s, all-in)
        if tick > SUDDEN_DEATH_TICKS - 90 * TICK_RATE:
            best = max(LANES, key=lambda l: self._count(state, l, faction))
            if best != cur and tick - self._lane_tick > 30 * TICK_RATE:
                self._lane_tick = tick; return best

        # PASSIVE RL (data collection)
        if self.rl and hero.get("alive"):
            allies=self._count(state,cur,faction); enemies=lane_enemies.get(cur,0)
            tower_alive=any(t["faction"]==faction and t["lane"]==cur and t.get("alive") for t in state.get("towers",[]))
            kd=self.kills_est/max(self.deaths,1)
            self.rl.decide_and_learn(hero,state,faction,cur,allies,enemies,tower_alive,kd,self._prev_hero,set())
            self._prev_hero=dict(hero)
        if self.brain:
            xp=sum(200*i for i in range(1,level))+hero.get("xp",0)
            self.brain.update_xp(cur,xp)

        # DEFAULT: ZEN MODE (stay and farm)
        return cur
        return preferred[0] if preferred else self.default_lane

    def should_recall(self, hero, state):
        if hero.get("recallCooldownMs",0)>0 or not hero.get("alive"): return False
        mx=hero.get("maxHp",1)
        if mx<=0: return False
        hp_pct=hero.get("hp",0)/mx
        tick=state.get("tick",0)
        game_secs=tick/TICK_RATE

        # NEVER recall in last 2 minutes (sudden death = bases can't attack, commit)
        if game_secs > 780: return False

        # PRE-RUSH RECALL: at 1:20-1:40, recall to full HP for tower buff expiry push
        if 80 < game_secs < 100 and hp_pct < 0.9:
            return True

        faction=self.faction or ""; enemy="orc" if faction=="human" else "human"
        enemies=self._count(state,hero.get("lane",""),enemy)
        if self.brain:
            enemy_lvls=[h.get("level",1) for h in state.get("heroes",[]) if h.get("faction")==enemy and h.get("alive")]
            enemy_avg=sum(enemy_lvls)/max(len(enemy_lvls),1)
            return self.brain.should_recall(hero.get("level",1),enemy_avg,enemies,hp_pct,tick)
        return hp_pct<0.70

    def tick(self, state, all_bots):
        if state.get("winner"):
            self.joined=False; self.kills_est=0; self.deaths=0; self.recalls=0; self._prev_xp=0
            if self.brain: self.brain.reset_game()
            self._prev_hero=None; return None

        if not self.joined:
            r=api_post("/api/strategy/deployment",self.api_key,{"heroClass":self.hero_class,"heroLane":self.default_lane})
            if "error" not in r: self.joined=True; self.game=r.get("gameId",self.game)
            else: self.errors+=1
            return None

        hero=self.find_hero(state)
        if not hero: return None

        alive=hero.get("alive",False)
        if self._prev_alive and not alive:
            self.deaths+=1
            if self.brain: self.brain.on_death(state.get("tick",0))
        # DEATH ROTATION (from thebestpizza): respawn = free lane change
        # Reset lane_tick so pick_lane recalculates optimal lane on respawn
        if not self._prev_alive and alive:
            self._lane_tick = 0  # triggers fresh lane pick on next cycle
        self._prev_alive=alive

        level=hero.get("level",1)
        xp=sum(200*i for i in range(1,level))+hero.get("xp",0)
        if self._prev_xp>0 and xp-self._prev_xp>=180:
            nk=(xp-self._prev_xp)//180; self.kills_est+=nk
            if self.brain:
                for _ in range(nk): self.brain.on_kill()
        self._prev_xp=xp

        if not alive: self.last_action="dead"; return None

        payload={}
        lane=self.pick_lane(state,hero,all_bots)
        # CRITICAL FIX (from lmeow, #1 player):
        # Sending heroLane when already in that lane triggers "shove micro"
        # which makes the hero stop attacking and charge forward into enemies.
        # Only send heroLane when ACTUALLY switching lanes.
        hero_lane = hero.get("lane", self.current_lane)
        if lane != hero_lane:
            payload["heroLane"] = lane
        self.current_lane = lane

        ab=self.pick_ability(hero)
        if ab: payload["abilityChoice"]=ab

        if self.should_recall(hero,state):
            payload["action"]="recall"; self.last_action="recall"; self.recalls+=1
        else: self.last_action=lane

        # SMART PING: coordinate pushes + defend calls
        faction=self.faction or ""; enemy="orc" if faction=="human" else "human"
        pinged = False
        # Ping weak enemy tower (coordinate push with teammates)
        for t in state.get("towers", []):
            if t["faction"]==enemy and t.get("alive") and t.get("hp",1200)<400:
                payload["ping"]=t["lane"]; pinged=True; break
        # Ping lane under heavy pressure
        if not pinged:
            for cl in LANES:
                if self._count(state,cl,enemy)>=self._count(state,cl,faction)+3:
                    payload["ping"]=cl; pinged=True; break
        # Ping base if critical
        base_hp=state.get("bases",{}).get(faction,{}).get("hp",1500)
        if base_hp<500: payload["ping"]="base"

        hp_pct=int(100*hero.get("hp",0)/max(hero.get("maxHp",1),1))
        payload["message"]=f"L{level} {lane} {hp_pct}%"
        r=api_post("/api/strategy/deployment",self.api_key,payload)
        self.decisions+=1
        if "error" in r: self.errors+=1
        return hero


# ── App ────────────────────────────────────────────────────

class HordeApp(App):
    CSS = """
    Screen { layout: vertical; }
    #status { height: 1; background: $primary-darken-3; padding: 0 1; }
    #battlefield { height: auto; max-height: 6; padding: 0 1; }
    TabbedContent { height: 1fr; }
    DataTable { height: 1fr; }
    #event-log { height: 1fr; border: solid $warning; min-height: 6; }
    .bottom-row { height: 12; }
    .detail-panel { width: 1fr; border: solid $success; padding: 0 1; }
    .history-panel { width: 1fr; border: solid $accent; padding: 0 1; }
    """
    BINDINGS = [
        ("1","set_strat('smart')","Smart"),("2","set_strat('push')","Push"),
        ("3","set_strat('defend')","Defend"),("4","set_strat('converge:top')","ConvT"),
        ("5","set_strat('converge:mid')","ConvM"),("6","set_strat('converge:bot')","ConvB"),
        ("q","quit","Quit"),
    ]
    TITLE = "HORDE AGENTS"
    current_strat = reactive("smart")

    def __init__(self):
        super().__init__()
        with open(FLEET_FILE) as f: fleet=json.load(f)
        self.game_id=fleet.get("game",3)
        self.bots=[Bot(b,self.game_id) for b in fleet["bots"]]
        for bot in self.bots:
            bot.brain=LearningBrain(); bot.rl=RLBrain()
        self.stats=Stats(); self.state={}; self.cycle=0
        self._last_winner=None; self._prev_heroes={}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="status")
        yield Static(id="battlefield")
        with TabbedContent("Fleet","Game","History","Quant","RL Training","Leaderboard"):
            with TabPane("Fleet"):
                yield DataTable(id="roster")
                with Horizontal(classes="bottom-row"):
                    yield RichLog(id="event-log",wrap=True,markup=True,max_lines=200)
                    yield Static(id="detail-panel",classes="detail-panel")
                    yield Static(id="history-mini",classes="history-panel")
            with TabPane("Game"):
                yield ScrollableContainer(Static(id="game-panel"))
            with TabPane("History"):
                yield ScrollableContainer(Static(id="history-panel"))
            with TabPane("Quant"):
                yield ScrollableContainer(Static(id="quant-panel"))
            with TabPane("RL Training"):
                yield ScrollableContainer(Static(id="rl-panel"))
            with TabPane("Leaderboard"):
                yield ScrollableContainer(Static(id="lb-panel"))
        yield Footer()

    def on_mount(self):
        t=self.query_one("#roster",DataTable)
        for col in ["#","Name","Sty","Cls","Lv","HP","Lane","DPS","K/D","Abilities","Act"]:
            t.add_column(col)
        t.cursor_type="row"
        self.set_interval(3,self._poll)
        self._update_history(); self._update_leaderboard()

    @work(thread=True)
    def _poll(self):
        s=api_get("/api/game/state",{"game":self.game_id})
        if not s: return
        winner=s.get("winner")
        if winner and winner!=self._last_winner:
            self.stats.record(winner,s,self.bots)
            hb=sum(1 for b in self.bots if b.faction=="human")
            ob=sum(1 for b in self.bots if b.faction=="orc")
            for bot in self.bots:
                if bot.brain: bot.brain.retrain()
                if bot.rl: bot.rl.end_game(bot.faction==winner); bot.rl.reset_game()
            self.call_from_thread(self._log,f"[bold]GAME OVER[/bold] {winner} wins ({hb}H/{ob}O)")
            self._last_winner=winner
        elif not winner: self._last_winner=None
        self._detect_events(s)
        for bot in self.bots: bot.tick(s,self.bots)
        self.state=s; self.cycle+=1
        self.call_from_thread(self._refresh)

    def _detect_events(self, s):
        for h in s.get("heroes",[]):
            n=h["name"]; p=self._prev_heroes.get(n)
            mine=is_our_bot(n)
            tag=f"[bold green]{n}[/bold green]" if mine else f"{n}"
            if p:
                if p.get("alive") and not h.get("alive"):
                    self.call_from_thread(self._log,f"[red]KILL[/red] {tag} died ({h.get('lane','')})")
                if not p.get("alive") and h.get("alive"):
                    self.call_from_thread(self._log,f"[green]SPAWN[/green] {tag}")
                if h.get("level",1)>p.get("level",1):
                    self.call_from_thread(self._log,f"[yellow]LVL[/yellow] {tag}->L{h['level']}")
                if h.get("lane")!=p.get("lane") and h.get("alive"):
                    self.call_from_thread(self._log,f"[cyan]MOVE[/cyan] {tag} {p.get('lane','')}->{h.get('lane','')}")
                pa={a["id"] for a in p.get("abilities",[])}
                ca={a["id"] for a in h.get("abilities",[])}
                for ab in ca-pa:
                    self.call_from_thread(self._log,f"[magenta]SKILL[/magenta] {tag} +{ab}")
            self._prev_heroes[n]={"alive":h.get("alive"),"level":h.get("level",1),"lane":h.get("lane"),"abilities":h.get("abilities",[])}

    def _log(self, msg):
        try:
            ts=datetime.now().strftime("%H:%M:%S")
            self.query_one("#event-log",RichLog).write(Text.from_markup(f"[dim]{ts}[/dim] {msg}"))
        except: pass

    # ── Refresh all panels ─────────────────────────────────

    def _refresh(self):
        s=self.state
        if not s: return
        self._update_status(s)
        self._update_battlefield(s)
        self._update_roster(s)
        self._update_detail(s)
        self._update_game(s)
        self._update_history()
        self._update_quant(s)
        self._update_rl()
        if self.cycle%10==0: self._update_leaderboard()

    def _update_status(self, s):
        tick=s.get("tick",0); gt=f"{int(tick/TICK_RATE)//60}:{int(tick/TICK_RATE)%60:02d}"
        sd_r=max(0,SUDDEN_DEATH_TICKS-tick)
        sd=f"{int(sd_r/TICK_RATE)//60}:{int(sd_r/TICK_RATE)%60:02d}" if sd_r>0 else "SD!"
        status=f"[bold red]{s['winner']} WINS[/bold red]" if s.get("winner") else "[green]LIVE[/green]"
        hb=sum(1 for b in self.bots if b.faction=="human")
        ob=sum(1 for b in self.bots if b.faction=="orc")
        alive=sum(1 for b in self.bots if (h:=b.find_hero(s)) and h.get("alive"))
        tk=sum(b.kills_est for b in self.bots); td=sum(b.deaths for b in self.bots)
        kd=tk/max(td,1); now=datetime.now().strftime("%H:%M:%S")
        self.query_one("#status").update(Text.from_markup(
            f" G{self.game_id} {gt} {status} SD:{sd} [cyan]{hb}H[/cyan]/[red]{ob}O[/red] "
            f"{alive}up K/D:{tk}/{td}({kd:.1f}) [{self.current_strat.upper()}] "
            f"{self.stats.summary()} C{self.cycle} {now}"))

    def _update_battlefield(self, s):
        faction=self.bots[0].faction if self.bots else None
        enemy="orc" if faction=="human" else "human" if faction else "orc"
        bases=s.get("bases",{}); hh=int(bases.get("human",{}).get("hp",0)); oh=int(bases.get("orc",{}).get("hp",0))
        tw={};
        for t in s.get("towers",[]): tw[(t["faction"],t["lane"])]=t
        lines=[]
        hc="green" if hh>750 else ("yellow" if hh>375 else "red")
        oc="green" if oh>750 else ("yellow" if oh>375 else "red")
        lines.append(f"[cyan]H[/cyan]:[{hc}]{hh}[/{hc}] [red]O[/red]:[{oc}]{oh}[/{oc}]  "
                     f"{'Lane':4} {'Twr':>4} {'Us':>3} {'Front':^10} {'Them':>4} {'Twr':>4}  Heroes")
        for lane in LANES:
            ld=s.get("lanes",{}).get(lane,{})
            fl=ld.get("frontline",0)
            ou=ld.get(faction,0) if faction else 0; eu=ld.get(enemy,0)
            w=4; pos=max(-w,min(w,int(fl/25)))
            if faction=="orc": pos=-pos
            bar=f"[green]{'>'*max(0,pos)}[/green]|[red]{'<'*max(0,w-pos)}[/red]{fl:+d}" if pos>=0 else f"[green]{'>'*max(0,w+pos)}[/green]|[red]{'<'*max(0,-pos)}[/red]{fl:+d}"
            ot=tw.get((faction,lane),{}) if faction else {}
            et=tw.get((enemy,lane),{})
            ots=f"{int(ot['hp']):4}" if ot.get("alive") else "[dim]  X [/dim]"
            ets=f"{int(et['hp']):4}" if et.get("alive") else "[dim]  X [/dim]"
            heroes=[]
            for h in s.get("heroes",[]):
                if h.get("lane")!=lane or not h.get("alive"): continue
                nm=h["name"][:6]; lv=h.get("level",1)
                if h.get("faction")==faction:
                    st="[bold green]" if is_our_bot(h["name"]) else "[cyan]"
                    heroes.append(f"{st}{nm}L{lv}[/]")
                else: heroes.append(f"[red]{nm}L{lv}[/red]")
            lines.append(f"  [bold]{lane.upper():4}[/bold] {ots} {ou:3} {bar:^10} {eu:4} {ets}  {' '.join(heroes) or '[dim]--[/dim]'}")
        self.query_one("#battlefield").update(Text.from_markup("\n".join(lines)))

    def _update_roster(self, s):
        t=self.query_one("#roster",DataTable); t.clear()
        SC={"offensive":"red","defensive":"cyan","balanced":"yellow","random":"magenta"}
        for i,b in enumerate(self.bots):
            h=b.find_hero(s); sc=SC.get(b.style,"white")
            if h and h.get("alive"):
                hp,mx=h.get("hp",0),h.get("maxHp",1); pct=int(100*hp/max(mx,1))
                c="green" if pct>50 else ("yellow" if pct>25 else "red")
                hp_s=Text.from_markup(f"[{c}]{int(hp)}/{int(mx)}[/{c}]")
                dps=f"{quant.hero_stats(h['class'],h['level'],h.get('abilities',[]))['dps']:.0f}"
                lv=str(h.get("level",1))
                ab=" ".join(f"{ABILITY_SHORT.get(a['id'],a['id'][:3])}:{a['level']}" for a in h.get("abilities",[]))
                if h.get("abilityChoices"): ab+=" [yellow]UP![/yellow]"
                act=b.last_action
            elif h:
                hp_s=Text("DEAD",style="red"); dps="-"; lv=str(h.get("level",1)); ab=""; act="dead"
            else: hp_s="-"; dps="-"; lv="-"; ab=""; act="..."
            t.add_row(str(i),b.name[:12],Text.from_markup(f"[{sc}]{b.style[:4]}[/{sc}]"),
                      b.hero_class[:4],lv,hp_s,b.current_lane,dps,f"{b.kills_est}/{b.deaths}",ab,act)

    def _update_detail(self, s):
        try: idx=self.query_one("#roster",DataTable).cursor_row
        except: idx=0
        if idx<0 or idx>=len(self.bots): idx=0
        b=self.bots[idx]; h=b.find_hero(s)
        sty_c={"defensive":"cyan","balanced":"yellow","offensive":"red"}.get(b.style,"white")
        lines=[f"[bold]{b.name}[/bold] {b.hero_class} [{sty_c}]{b.style}[/{sty_c}]",
               f"Faction: {b.faction or '?'}  Lane: {b.default_lane}->{b.current_lane}",
               f"Prio: {'>'.join(a[:4] for a in b.ability_prio[:5])}",""]
        if h and h.get("alive"):
            hp,mx=h.get("hp",0),h.get("maxHp",1)
            st=quant.hero_stats(h["class"],h["level"],h.get("abilities",[]))
            lines.append(f"HP: {int(hp)}/{int(mx)} ({int(100*hp/max(mx,1))}%)")
            lines.append(f"Lv{h['level']} XP:{h.get('xp',0)}/{h.get('xpToNext',0)}")
            lines.append(f"DPS:{st['dps']:.1f}")
            # RL reward info
            if b.rl:
                rs=b.rl.get_reward_summary()
                lines.append(f"RL: {rs.get('cycles',0)} cycles reward:{rs.get('total',0):.1f}")
            if b.brain:
                thr=b.brain.get_recall_threshold(h["level"],5,0,s.get("tick",0))
                lines.append(f"Recall at: {thr:.0%} HP")
            for a in h.get("abilities",[]): lines.append(f"  {a['id']} L{a['level']}")
        elif h: lines.append(f"[red]DEAD[/red] L{h.get('level',1)}")
        lines.append(f"K/D:{b.kills_est}/{b.deaths} Recalls:{b.recalls} Dec:{b.decisions}")
        self.query_one("#detail-panel").update("\n".join(lines))

        # Mini history
        games=self.stats.games
        if not games: self.query_one("#history-mini").update("[dim]No history[/dim]"); return
        w=self.stats.wins; l=len(games)-w; wr=100*w/len(games) if games else 0
        wc="green" if wr>50 else ("yellow" if wr>30 else "red")
        last5=" ".join("[green]W[/green]" if g.get("majority_won",g.get("won")) else "[red]L[/red]" for g in games[-5:])
        sty={}
        for g in games:
            for bb in g.get("bots",[]):
                st=bb.get("style","?"); sty.setdefault(st,{"k":0,"d":0})
                sty[st]["k"]+=bb.get("kills_est",0); sty[st]["d"]+=bb.get("deaths",0)
        rlines=[f"[bold]{w}W/{l}L[/bold] [{wc}]{wr:.0f}%[/{wc}]",f"Last: {last5}","",f"[bold]Style KD[/bold]"]
        for st,d in sorted(sty.items(),key=lambda x:-x[1]["k"]/max(x[1]["d"],1)):
            kd=d["k"]/max(d["d"],1); c="green" if kd>5 else ("yellow" if kd>3 else "red")
            rlines.append(f"  {st[:7]} [{c}]{kd:.1f}[/{c}]")
        self.query_one("#history-mini").update("\n".join(rlines))

    def _update_game(self, s):
        my={b.name for b in self.bots}|{h["name"] for h in s.get("heroes",[]) if is_our_bot(h["name"])}
        t=Table(expand=True,show_lines=True,title="All Heroes")
        for col in ["Name","Faction","Class","Lv","Lane","HP","DPS","Abilities","Yours?"]:
            t.add_column(col)
        for h in sorted(s.get("heroes",[]),key=lambda x:(x.get("faction",""),x.get("lane",""))):
            mine=h["name"] in my; alive=h.get("alive",False)
            style="bold green" if mine else ("dim" if not alive else "")
            f_s="[cyan]Human[/cyan]" if h["faction"]=="human" else "[red]Orc[/red]"
            hp_s=f"{int(h.get('hp',0))}/{int(h.get('maxHp',0))}" if alive else "[dim]DEAD[/dim]"
            dps=f"{quant.hero_stats(h['class'],h['level'],h.get('abilities',[]))['dps']:.0f}" if alive else "-"
            ab=" ".join(f"{ABILITY_SHORT.get(a['id'],a['id'][:3])}:{a['level']}" for a in h.get("abilities",[]))
            yours="[green]YES[/green]" if mine else ""
            t.add_row(Text(h["name"],style=style),f_s,h.get("class","?"),str(h.get("level",1)),
                      h.get("lane","?"),hp_s,dps,ab,yours)
        tw_t=Table(expand=True,title="Towers")
        for col in ["Lane","Human","Orc"]: tw_t.add_column(col)
        tw_m={}
        for tw in s.get("towers",[]): tw_m[(tw["faction"],tw["lane"])]=tw
        for lane in LANES:
            ht=tw_m.get(("human",lane),{}); ot=tw_m.get(("orc",lane),{})
            hs=f"{int(ht['hp'])}/{ht['maxHp']}" if ht.get("alive") else "[red]DESTROYED[/red]"
            os_=f"{int(ot['hp'])}/{ot['maxHp']}" if ot.get("alive") else "[red]DESTROYED[/red]"
            tw_t.add_row(lane.upper(),hs,os_)
        self.query_one("#game-panel").update(Group(t,"",tw_t))

    def _update_history(self):
        games=self.stats.games
        if not games:
            self.query_one("#history-panel").update("[bold]Game History[/bold]\n\nNo games recorded yet."); return
        t=Table(expand=True,show_lines=True,title=f"Game Log ({len(games)} games, {self.stats.summary()})")
        for col in ["#","Time","Winner","Split","Result","H.Lv","O.Lv","H.Base","O.Base","Dur"]:
            t.add_column(col)
        for i,g in enumerate(games):
            won=g.get("majority_won",g.get("won"))
            result=Text("W",style="bold green") if won else Text("L",style="bold red")
            hb=g.get("human_bots","?"); ob=g.get("orc_bots","?")
            dur=parse_dur(g.get("game_time",0)); m,sec=divmod(int(dur),60)
            t.add_row(str(i+1),g.get("time","")[:16],g.get("winner","?"),f"{hb}H/{ob}O",result,
                      str(g.get("human_max_level","?")),str(g.get("orc_max_level","?")),
                      str(int(g.get("human_base_hp",0))),str(int(g.get("orc_base_hp",0))),f"{m}:{sec:02d}")
        # Per-bot lifetime
        bt=Table(expand=True,title="Per-Bot Lifetime");
        for col in ["Bot","Class","Kills","Deaths","K/D","Avg Lv","Games"]: bt.add_column(col)
        ba={}
        for g in games:
            for b in g.get("bots",[]):
                n=b["name"]; ba.setdefault(n,{"k":0,"d":0,"lvl":0,"g":0,"cls":b.get("class","?")})
                ba[n]["k"]+=b.get("kills_est",0); ba[n]["d"]+=b.get("deaths",0)
                ba[n]["lvl"]+=b.get("level",1); ba[n]["g"]+=1
        for n,d in sorted(ba.items(),key=lambda x:-x[1]["k"]/max(x[1]["d"],1)):
            kd=d["k"]/max(d["d"],1); c="green" if kd>5 else ("yellow" if kd>3 else "red")
            bt.add_row(n,d["cls"],str(d["k"]),str(d["d"]),Text(f"{kd:.2f}",style=c),
                       f"{d['lvl']/max(d['g'],1):.1f}",str(d["g"]))
        self.query_one("#history-panel").update(Group(t,"",bt))

    def _update_quant(self, s):
        faction=self.bots[0].faction if self.bots else None
        if not faction: self.query_one("#quant-panel").update("[dim]Waiting...[/dim]"); return
        enemy="orc" if faction=="human" else "human"
        analysis=quant.game_state_analysis(s,faction)
        lines=[]
        adv=analysis["advantage_score"]; ac="green" if adv>10 else ("red" if adv<-10 else "yellow")
        lines.append(f"[bold]Advantage:[/bold] [{ac}]{adv:+.0f}[/{ac}]  Phase: [bold]{analysis['phase']}[/bold]  SD: {analysis['sudden_death_in']:.0f}s")
        lines.append(f"DPS: {analysis['our_total_dps']:.0f} vs {analysis['enemy_total_dps']:.0f}  Alive: {analysis['our_alive']} vs {analysis['enemy_alive']}")
        lines.append("")
        # Lane matchups
        mt=Table(expand=True,title="Lane Matchups")
        for col in ["Lane","Win%","Action","Us","Them","Our TTW","Their TTW"]: mt.add_column(col)
        for lane in LANES:
            m=analysis["matchups"][lane]
            wc="green" if m["win_prob"]>.6 else ("red" if m["win_prob"]<.4 else "yellow")
            rc={"push":"green","hold":"yellow","retreat":"red"}.get(m["recommendation"],"white")
            mt.add_row(lane.upper(),Text(f"{m['win_prob']:.0%}",style=wc),Text(m["recommendation"].upper(),style=rc),
                       f"{m['our_count']}h/{m['our_units']}u",f"{m['enemy_count']}h/{m['enemy_units']}u",
                       f"{m['our_ttw']:.1f}s",f"{m['enemy_ttw']:.1f}s")
        lines.append("")
        # Kill EV
        lines.append("[bold]Kill EV[/bold] [dim](need 70%+ win prob for positive EV)[/dim]")
        kt=Table(expand=True)
        for col in ["Enemy","Lv","EV","Bounty","Verdict"]: kt.add_column(col)
        our_avg=sum(h.get("level",1) for h in s.get("heroes",[]) if h["faction"]==faction and h.get("alive"))/max(analysis["our_alive"],1)
        for h in s.get("heroes",[]):
            if h["faction"]==enemy and h.get("alive"):
                diff=our_avg-h["level"]; prob=max(.1,min(.9,.5+diff*.05))
                ev=quant.kill_ev(int(our_avg),h["level"],prob)
                ec="green" if ev>0 else "red"
                verdict="FIGHT" if ev>50 else ("MAYBE" if ev>0 else "AVOID")
                vc="green" if verdict=="FIGHT" else ("yellow" if verdict=="MAYBE" else "red")
                kt.add_row(h["name"],str(h["level"]),Text(f"{ev:+.0f}",style=ec),
                           str(quant.kill_xp_value(h["level"])),Text(verdict,style=vc))
        # Death cost
        lines.append(""); lines.append("[bold]Death Cost by Level[/bold]")
        dt=Table(expand=True)
        for col in ["Level","Bounty","Respawn","Total"]: dt.add_column(col)
        for lv in [1,5,10,15,20]:
            dc=quant.death_cost(lv); c="green" if dc["total_xp_cost"]<300 else ("yellow" if dc["total_xp_cost"]<500 else "red")
            dt.add_row(f"L{lv}",f"{dc['xp_given']}xp",f"{dc['respawn_sec']:.0f}s",Text(f"{dc['total_xp_cost']:.0f}xp",style=c))
        self.query_one("#quant-panel").update(Group("\n".join(lines[:3]),mt,"\n".join(lines[3:5]),kt,"\n".join(lines[5:]),dt))

    def _update_rl(self):
        lines=[]
        lines.append("[bold underline]REINFORCEMENT LEARNING ENGINE[/bold underline]")
        lines.append("[dim]Reward-shaped Q-learning. Learns optimal lane choice from rewards/penalties.[/dim]")
        lines.append("")

        # Reward weights
        lines.append("[bold]Reward Weights[/bold] [dim](tune in rl_engine.py RewardCalculator.WEIGHTS)[/dim]")
        wt=Table(expand=True)
        for col in ["Signal","Weight","Type"]: wt.add_column(col)
        for k,v in RewardCalculator.WEIGHTS.items():
            c="green" if v>0 else "red"
            wt.add_row(k,Text(f"{v:+.1f}",style=c),"reward" if v>0 else "penalty")

        # Per-bot RL stats
        lines.append("")
        lines.append("[bold]Per-Bot RL Status[/bold]")
        bt=Table(expand=True)
        for col in ["Bot","RL Cycles","Reward Total","Q-States","Deaths(3min)"]: bt.add_column(col)
        for b in self.bots:
            if b.rl:
                rs=b.rl.get_reward_summary()
                qs=b.rl.q_agent.stats()
                rc=b.rl.reward_calc.recall if hasattr(b.rl.reward_calc,'recall') else None
                bt.add_row(b.name[:12],str(rs.get("cycles",0)),
                           f"{rs.get('total',0):.1f}",str(qs.get("states_learned",0)),
                           str(len(b.rl.reward_calc.recent_deaths) if hasattr(b.rl.reward_calc,'recent_deaths') else 0))

        # Q-table top actions
        lines.append("")
        lines.append("[bold]Top Learned Strategies[/bold] [dim](from Q-table, highest confidence)[/dim]")
        # Merge all bot Q-tables for display
        all_top=[]
        for b in self.bots:
            if b.rl:
                all_top.extend(b.rl.q_agent.top_actions(5))
        all_top.sort(key=lambda x:-x["confidence"])

        qt=Table(expand=True)
        for col in ["State","Best Action","Q-Value","Confidence"]: qt.add_column(col)
        for entry in all_top[:10]:
            parts=entry["state"].split("|")
            state_str=f"{parts[0]} {parts[1]} {parts[2]} {parts[4]} {parts[6]}" if len(parts)>=7 else entry["state"]
            qt.add_row(state_str,Text(entry["best_action"],style="bold"),
                       f"{entry['q_value']:.2f}",f"{entry['confidence']:.2f}")

        self.query_one("#rl-panel").update(Group("\n".join(lines[:4]),wt,"\n".join(lines[4:6]),bt,"\n".join(lines[6:]),qt))

    @work(thread=True)
    def _update_leaderboard(self):
        try:
            r=requests.get(f"{BASE}/api/leaderboard",timeout=10)
            if r.status_code!=200: return
            data=r.json()
        except: return

        # Stats summary
        total=len(data)
        avg_mmr=sum(p.get("mmr",0) for p in data)//max(total,1)
        avg_wr=sum(100*p.get("games_won",0)//max(p.get("games_played",1),1) for p in data)//max(total,1)
        top_mmr=data[0] if data else {}
        top_wr=max(data, key=lambda p: p.get("games_won",0)/max(p.get("games_played",1),1) if p.get("games_played",0)>20 else 0) if data else {}

        # Sort by MMR (default from API)
        by_mmr=sorted(data, key=lambda p: -p.get("mmr",0))

        # Our bots
        our_ranks=[]
        for i,p in enumerate(by_mmr):
            if is_our_bot(p.get("name","")): our_ranks.append((i+1,p))

        summary=[]
        summary.append(f"[bold]LEADERBOARD[/bold] {total} players  avg MMR:{avg_mmr}  avg WR:{avg_wr}%")
        summary.append(f"[bold]Top MMR:[/bold] {top_mmr.get('name','?')} {top_mmr.get('mmr',0)}  "
                       f"[bold]Top WR:[/bold] {top_wr.get('name','?')} {100*top_wr.get('games_won',0)//max(top_wr.get('games_played',1),1)}%")
        if our_ranks:
            summary.append("")
            summary.append("[bold green]OUR BOTS:[/bold green]")
            for rank,p in our_ranks:
                w=p.get("games_won",0); g=p.get("games_played",0)
                wr=100*w//g if g else 0
                summary.append(f"  #{rank} {p['name']} MMR:{p['mmr']} {w}W/{g-w}L ({wr}%)")

        # Platform breakdown
        platforms={}
        for p in data:
            pt=p.get("player_type","?")
            platforms.setdefault(pt,0)
            platforms[pt]+=1
        summary.append("")
        summary.append("[bold]Platforms:[/bold] " + " ".join(f"{k}:{v}" for k,v in sorted(platforms.items(),key=lambda x:-x[1])))

        # Top 10 by MMR
        t_mmr=Table(expand=True,show_lines=True,title="Top 20 by MMR")
        for col in ["#","Name","Type","MMR","Wins","Losses","Games","WR"]: t_mmr.add_column(col)
        for i,p in enumerate(by_mmr[:20]):
            name=p.get("name","?"); w=p.get("games_won",0); g=p.get("games_played",0)
            wr=f"{100*w//g}%" if g else "0%"
            mine=is_our_bot(name)
            style="bold green" if mine else ""
            wr_val=100*w//g if g else 0
            wr_style="green" if wr_val>60 else ("yellow" if wr_val>50 else "red")
            t_mmr.add_row(str(i+1),Text(name,style=style),p.get("player_type","?"),
                         str(p.get("mmr",0)),str(w),str(g-w),str(g),Text(wr,style=wr_style))

        # Top 10 by WR (min 30 games)
        qualified=[p for p in data if p.get("games_played",0)>=30]
        by_wr=sorted(qualified, key=lambda p: -p.get("games_won",0)/max(p.get("games_played",1),1))
        t_wr=Table(expand=True,show_lines=True,title="Top 10 by Win Rate (30+ games)")
        for col in ["#","Name","MMR","Wins","Losses","WR"]: t_wr.add_column(col)
        for i,p in enumerate(by_wr[:10]):
            name=p.get("name","?"); w=p.get("games_won",0); g=p.get("games_played",0)
            wr=f"{100*w//g}%" if g else "0%"
            mine=is_our_bot(name)
            style="bold green" if mine else ""
            t_wr.add_row(str(i+1),Text(name,style=style),str(p.get("mmr",0)),
                         str(w),str(g-w),Text(wr,style="green"))

        # Volume leaders (most games)
        by_games=sorted(data, key=lambda p: -p.get("games_played",0))
        t_vol=Table(expand=True,show_lines=True,title="Top 10 by Volume (most games)")
        for col in ["#","Name","Games","Wins","WR","MMR"]: t_vol.add_column(col)
        for i,p in enumerate(by_games[:10]):
            name=p.get("name","?"); w=p.get("games_won",0); g=p.get("games_played",0)
            wr=f"{100*w//g}%" if g else "0%"
            t_vol.add_row(str(i+1),name,str(g),str(w),wr,str(p.get("mmr",0)))

        self.call_from_thread(lambda: self.query_one("#lb-panel").update(
            Group("\n".join(summary),"",t_mmr,"",t_wr,"",t_vol)))

    def action_set_strat(self, strat):
        self.current_strat=strat
        for b in self.bots: b.strategy_override=strat
        self._log(f"[bold]STRATEGY -> {strat.upper()}[/bold]")

if __name__=="__main__": HordeApp().run()
