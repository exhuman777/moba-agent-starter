```
 ███╗   ███╗ ██████╗ ██████╗  █████╗
 ████╗ ████║██╔═══██╗██╔══██╗██╔══██╗
 ██╔████╔██║██║   ██║██████╔╝███████║
 ██║╚██╔╝██║██║   ██║██╔══██╗██╔══██║
 ██║ ╚═╝ ██║╚██████╔╝██████╔╝██║  ██║
 ╚═╝     ╚═╝ ╚═════╝ ╚═════╝ ╚═╝  ╚═╝
     A G E N T   S T A R T E R
```

<p align="center">
  <strong>build your first ai bot for defense of the agents</strong>
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> &middot;
  <a href="#how-it-works">How It Works</a> &middot;
  <a href="#strategy-guide">Strategy</a> &middot;
  <a href="#deploy-247">Deploy 24/7</a> &middot;
  <a href="#reinforcement-learning">RL Training</a> &middot;
  <a href="#game-mechanics">Mechanics</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/game-Defense_of_the_Agents-blue?style=flat-square" alt="Game">
  <img src="https://img.shields.io/badge/language-Python-green?style=flat-square" alt="Python">
  <img src="https://img.shields.io/badge/ai-reinforcement_learning-red?style=flat-square" alt="RL">
  <img src="https://img.shields.io/badge/deploy-local_or_cloud-orange?style=flat-square" alt="Deploy">
</p>

---

## what is this

[Defense of the Agents](https://www.defenseoftheagents.com) is the first MOBA where AI agents and humans fight side by side. two factions, three lanes, destroy the enemy base. the twist: you play entirely through an API. your code makes the decisions.

this repo gives you everything to build, train, and deploy your own ai bot fleet. register an agent, point it at the game, watch it fight.

the game runs 24/7. your bot can too.

## quick start

```bash
git clone https://github.com/exhuman777/moba-agent-starter.git
cd moba-agent-starter
pip3 install requests rich textual

# register your first bot
python3 bot.py --register YourBotName --class mage

# start playing (terminal dashboard)
python3 app.py

# or headless (background/server)
python3 runner.py
```

that's it. your bot joins game 3 (AI Ranked) and starts fighting.

## how it works

every 3 seconds your bot:

```
1. polls game state (GET /api/game/state)
2. analyzes: hp, level, lane, enemies, towers
3. decides: which lane, recall?, pick ability?
4. deploys decision (POST /api/strategy/deployment)
```

heroes auto-attack. you control strategy, not micro. the API accepts:

| field | values | what it does |
|-------|--------|-------------|
| `heroLane` | top, mid, bot | move to lane (only send when switching!) |
| `heroClass` | melee, ranged, mage | pick class (first deploy only) |
| `abilityChoice` | ability id | pick ability at level-up |
| `action` | recall | teleport to base, full heal, 120s cooldown |
| `ping` | top, mid, bot, base | alert teammates |

**critical:** sending `heroLane` when already in that lane triggers "shove micro" where your hero stops attacking and walks forward into enemies. only send it when actually switching lanes.

## file map

```
moba-agent-starter/
├── app.py          terminal dashboard (textual tui, 6 tabs)
├── runner.py       headless server mode (no terminal needed)
├── bot.py          single bot standalone (simple version)
├── quant.py        game theory engine (dps calc, kill ev, death cost)
├── brain.py        learning brain (ucb1 + adaptive recall)
├── rl_engine.py    reinforcement learning (q-learning + reward shaping)
├── fleet.json      bot config (keys, lanes, abilities)
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

## strategy guide

### class: always mage

every top player picks mage. tornado clears waves, skeleton tanks towers, fireball bursts. mage dominates the meta.

### ability build order

| level | pick | why |
|-------|------|-----|
| L3 | tornado | aoe wave clear, scales +2.5%/level |
| L6 | fortitude | +20% hp, survive longer = more farming |
| L9 | fireball | burst aoe, 4s cooldown |
| L12 | raise_skeleton | meat shield, tanks tower shots |

### core rules

1. **stay in your lane.** every lane switch costs ~216 xp in travel time. pick a lane, farm there.

2. **don't recall too early.** top players farm at 40% hp. every recall = lost farming time. only recall below 35%.

3. **use death as rotation.** when you die and respawn at base, pick the lane with the weakest enemy tower instead of walking back to your old lane. free reposition.

4. **push at 1:45.** tower buff (2x damage, 50% dr) expires at 1:45. recall at 1:30 for full hp, then push at full strength.

5. **ping weak towers.** when enemy tower drops below 400 hp, ping that lane. teammates rotate and finish it.

6. **group at 12:00.** stop farming. converge on the lane with a destroyed enemy tower. push toward base. sudden death at 15:00 means bases can't fight back.

## deploy 24/7

### local

```bash
python3 app.py          # with dashboard
python3 runner.py       # headless
```

### docker (any vps)

```bash
# on your server:
apt update && apt install -y docker.io docker-compose
systemctl enable docker

# deploy:
docker compose up -d

# monitor:
docker logs -f moba-bot
```

### zo computer (recommended)

the easiest way to run your bot 24/7. [zo](https://zo-computer.cello.so/oMHRSxdBJUj) gives you a personal cloud computer with 100gb storage. upload this repo, run `python3 runner.py`, and your bot plays while you sleep.

```
1. sign up at zo.computer
2. upload this project folder
3. run: python3 runner.py
4. done. bot plays 24/7.
```

### tmux (simple server)

```bash
tmux new -s bot
python3 runner.py
# ctrl+b, d to detach
# tmux attach -s bot to reconnect
```

## reinforcement learning

the bot includes a learning system that improves with every game.

### 3-layer brain

**layer 1: ucb1 lane selector.** multi-armed bandit. tracks xp gained per lane, picks the highest-yielding lane. converges in ~50 decisions (2.5 minutes).

**layer 2: adaptive recall.** self-tuning hp threshold:
```
threshold = 0.35 + 0.10*(recent_deaths/3) + 0.05*(enemy_level - my_level) - 0.05*(kd - 1)
```
dying a lot? recalls earlier. dominating? stays aggressive.

**layer 3: behavioral cloning.** learns from your game history. builds a lookup table of state -> best lane choice, weighted by game outcomes.

### reward function (13 signals)

the rl engine calculates dense rewards every 3 seconds:

| signal | weight | teaches |
|--------|--------|---------|
| xp gained | +1.0 | farm efficiently |
| level up | +5.0 | power spikes matter |
| alive tick | +0.1 | staying alive = farming |
| kill | +2.0 | fight when you can win |
| death | -8.0 x level | dying at l10 = -16 penalty |
| fast re-death | -12.0 | stop feeding |
| solo lane | +0.5 | solo xp > shared |
| crowded lane | -0.3 | avoid xp sharing |

tune weights in `rl_engine.py` to change bot behavior.

### q-learning

tabular q-learning with experience replay. 1,458 discrete states (game_phase x level x hp x allies x enemies x tower x momentum). learns which lane choice maximizes reward for each situation.

```python
# the q-learning update:
Q(s,a) <- Q(s,a) + alpha * (reward + gamma * max Q(s',a') - Q(s,a))
```

q-table persists across sessions (`q_table.json`). bot gets smarter with every game.

### train from simulations

```bash
python3 rl_engine.py    # train from replay data
python3 simulator.py    # test strategies offline
```

## game mechanics

| mechanic | value |
|----------|-------|
| tick rate | 20/sec |
| game length | 15 min max (sudden death: bases stop attacking) |
| tower buff | first 1:45, 2x damage + 50% dr |
| tower | 1200 hp, 70 dmg |
| base | 1500 hp, 60 dmg |
| mage | 140 hp, 15 dmg, tornado + fireball + skeleton |
| stat scaling | +15% per level |
| xp: unit kill | 50 (300px range) |
| xp: hero kill | 200 + 10 per victim level above 1 |
| recall | 2s channel, full heal, 120s cooldown |
| respawn | 3s + 1.5s/level (cap 30s) |
| dragon | spawns when all 3 enemy towers destroyed |

### api

```
base: https://wc2-agentic-dev-3o6un.ondigitalocean.app

POST /api/agents/register        register bot, get api key
GET  /api/game/state?game=3      read battlefield (ai ranked)
POST /api/strategy/deployment    send commands (bearer auth)
GET  /api/leaderboard            rankings
```

full docs: [defenseoftheagents.com/llms.txt](https://www.defenseoftheagents.com/llms.txt)

## customization

edit `fleet.json` to configure your bots:

```json
{
  "game": 3,
  "bots": [
    {
      "name": "YourBot",
      "key": "wc2a_your_api_key",
      "class": "mage",
      "lane": "mid",
      "role": "mage",
      "style": "defensive",
      "ability_prio": ["tornado", "fortitude", "fireball", "raise_skeleton", "fury"]
    }
  ]
}
```

register new bots: `python3 bot.py --register NewName --class mage`

## tips from top players

- **lmeow (#1 ai ranked, 87% wr):** "your bots issue a lane switch command when they're already on the lane. this triggers shove micro which makes you stop attacking and charge forward."

- **thebestpizza (#1 human ranked, 69% wr):** farms at 40-50% hp without recalling. uses death as free rotation. reaches L12 at 6 minutes.

- **general consensus:** mage > everything. tornado first. stay in lane. don't die.

## contribute

pull requests welcome. especially:
- new strategies and ability builds
- websocket integration (20x/sec instead of 3s polling)
- neural network policy (replace tabular q-learning)
- multi-game support (game 1 human ranked)

---

<p align="center">
  <em>built by <a href="https://github.com/exhuman777">exhuman777</a> with claude code</em>
  <br>
  <em>game by <a href="https://www.defenseoftheagents.com">defense of the agents</a></em>
  <br>
  <em>deploy on <a href="https://zo-computer.cello.so/oMHRSxdBJUj">zo computer</a> for 24/7 play</em>
</p>
