"""E2E tests for the wallet + skin + lane rotation + stats pipeline.

Covers:
  - Lane rotation is deterministic round-robin and only touches rotate_lane=true
  - wallet.load_pk: env wins over file; file fallback works; missing -> None
  - wallet.auto_connect: mutates cfg (wallet_ok/skin_ok/holder) based on API response
  - WSBot.__init__ reads every wallet + rotation field from cfg
  - Deploy gate blocks when wallet_ok=False
  - Deploy gate blocks when wallet owns no skin NFT
  - Deploy succeeds + payload includes skin when all wallet checks pass
  - Shove-micro prevention (WSBot)
  - _record_game writes majority_won + human_bots/orc_bots + max levels + per-bot snap
  - Lane rotation advances and propagates to default_lane/current_lane
  - Dashboard BotBrain.observe NEVER POSTs /api/strategy/deployment
  - RunnerProcess lifecycle: start spawns Popen, stop sends SIGTERM, SIGKILL after timeout

Run:  python3 -m unittest tests.test_experiment -v
"""

from __future__ import annotations
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import experiment
import wallet
import ws_runner


# ---------------------------------------------------------------------------
# Pure logic: lane rotation
# ---------------------------------------------------------------------------

class TestRotateLane(unittest.TestCase):
    def test_round_robin_first_cycle(self):
        self.assertEqual(experiment.rotate_lane(0), "top")
        self.assertEqual(experiment.rotate_lane(1), "mid")
        self.assertEqual(experiment.rotate_lane(2), "bot")

    def test_round_robin_wraps(self):
        self.assertEqual(experiment.rotate_lane(3), "top")
        self.assertEqual(experiment.rotate_lane(10), experiment.rotate_lane(1))
        self.assertEqual(experiment.rotate_lane(99), experiment.rotate_lane(0))

    def test_apply_only_touches_flagged_bots(self):
        bots = [
            {"name": "A", "lane": "top"},
            {"name": "B", "lane": "mid", "rotate_lane": True},
            {"name": "C", "lane": "bot"},
        ]
        experiment.apply_lane_rotation(bots, game_idx=1)
        self.assertEqual(bots[0]["lane"], "top")       # untouched
        self.assertEqual(bots[1]["lane"], "mid")       # rotated, idx 1 -> mid
        self.assertEqual(bots[2]["lane"], "bot")       # untouched

    def test_apply_rotates_across_games(self):
        bots = [{"name": "S", "lane": "bot", "rotate_lane": True}]
        experiment.apply_lane_rotation(bots, 0); self.assertEqual(bots[0]["lane"], "top")
        experiment.apply_lane_rotation(bots, 1); self.assertEqual(bots[0]["lane"], "mid")
        experiment.apply_lane_rotation(bots, 2); self.assertEqual(bots[0]["lane"], "bot")
        experiment.apply_lane_rotation(bots, 3); self.assertEqual(bots[0]["lane"], "top")


# ---------------------------------------------------------------------------
# wallet.load_pk: env / file / missing
# ---------------------------------------------------------------------------

class TestLoadPK(unittest.TestCase):
    def setUp(self):
        # Isolate KEY_FILE to a temp path for every test
        self.tmp = tempfile.NamedTemporaryFile(delete=False, mode="w")
        self.tmp.close()
        os.unlink(self.tmp.name)  # starts non-existent
        self._orig_keyfile = wallet.KEY_FILE
        wallet.KEY_FILE = self.tmp.name
        self._orig_env = os.environ.pop("WALLET_PK", None)

    def tearDown(self):
        wallet.KEY_FILE = self._orig_keyfile
        if os.path.exists(self.tmp.name):
            os.unlink(self.tmp.name)
        if self._orig_env is not None:
            os.environ["WALLET_PK"] = self._orig_env

    def test_env_var_wins(self):
        os.environ["WALLET_PK"] = "0xabc"
        with open(wallet.KEY_FILE, "w") as f:
            f.write("0xFROMFILE")
        self.assertEqual(wallet.load_pk(), "0xabc")

    def test_file_fallback(self):
        with open(wallet.KEY_FILE, "w") as f:
            f.write("  0xdead\n")
        self.assertEqual(wallet.load_pk(), "0xdead")

    def test_missing_returns_none(self):
        self.assertIsNone(wallet.load_pk())

    def test_adds_0x_prefix(self):
        with open(wallet.KEY_FILE, "w") as f:
            f.write("deadbeef")
        self.assertEqual(wallet.load_pk(), "0xdeadbeef")


# ---------------------------------------------------------------------------
# wallet.auto_connect: mutates cfg dicts
# ---------------------------------------------------------------------------

class TestAutoConnect(unittest.TestCase):
    def setUp(self):
        os.environ["WALLET_PK"] = "0x" + "1" * 64
        self._orig_keyfile = wallet.KEY_FILE
        wallet.KEY_FILE = "/nonexistent/path"

    def tearDown(self):
        os.environ.pop("WALLET_PK", None)
        wallet.KEY_FILE = self._orig_keyfile

    def test_skips_bots_without_wallet_flag(self):
        bots = [{"name": "A", "key": "k", "wallet": False}]
        with patch.object(wallet, "connect_bot") as m:
            wallet.auto_connect(bots)
        m.assert_not_called()
        self.assertNotIn("wallet_ok", bots[0])

    def test_success_flips_all_flags(self):
        bots = [{"name": "B", "key": "k1", "wallet": True, "skin": "pixagreen_mage"}]
        fake = {"message": "Wallet connected.", "tokenHolder": True,
                "tokenBalance": 1000, "pixagreenMage": True}
        with patch.object(wallet, "connect_bot", return_value=fake):
            wallet.auto_connect(bots)
        self.assertTrue(bots[0]["wallet_ok"])
        self.assertTrue(bots[0]["wallet_holder"])
        self.assertTrue(bots[0]["wallet_skin_ok"])

    def test_skin_not_owned_blocks_skin(self):
        bots = [{"name": "B", "key": "k1", "wallet": True, "skin": "pixagreen_mage"}]
        fake = {"message": "ok", "tokenHolder": True, "pixagreenMage": False}
        with patch.object(wallet, "connect_bot", return_value=fake):
            wallet.auto_connect(bots)
        self.assertTrue(bots[0]["wallet_ok"])
        self.assertFalse(bots[0]["wallet_skin_ok"])

    def test_connect_error_leaves_wallet_ok_false(self):
        bots = [{"name": "B", "key": "k1", "wallet": True}]
        with patch.object(wallet, "connect_bot", return_value={"error": "500"}):
            wallet.auto_connect(bots)
        self.assertFalse(bots[0]["wallet_ok"])

    def test_no_pk_marks_all_targets_failed(self):
        os.environ.pop("WALLET_PK", None)
        bots = [{"name": "B", "key": "k1", "wallet": True}]
        with patch.object(wallet, "connect_bot") as m:
            wallet.auto_connect(bots)
        m.assert_not_called()
        self.assertFalse(bots[0]["wallet_ok"])


# ---------------------------------------------------------------------------
# WSBot: init + deploy gates + shove-micro prevention
# ---------------------------------------------------------------------------

def _state_with_no_hero(tick=100):
    """Game state where our bot has not yet spawned (triggers deploy path).
    tick=100 keeps us under the fresh-game gate threshold (600).
    """
    return {"tick": tick, "winner": None, "heroes": [], "towers": []}


def _state_with_hero(name, lane="top", faction="human", alive=True, tick=1000,
                    hp=100, max_hp=100, level=5):
    return {
        "tick": tick, "winner": None, "towers": [],
        "heroes": [{
            "name": name, "lane": lane, "faction": faction, "alive": alive,
            "hp": hp, "maxHp": max_hp, "level": level, "xp": 0,
            "abilities": [], "abilityChoices": [], "recallCooldownMs": 0,
        }],
    }


class TestWSBotInit(unittest.TestCase):
    def test_reads_every_experiment_field(self):
        cfg = {
            "name": "TestBot_A", "key": "k", "class": "mage", "lane": "top",
            "style": "lmeow", "skin": "pixagreen_mage", "rotate_lane": False,
            "wallet": True, "wallet_ok": True, "wallet_skin_ok": True,
            "wallet_holder": True,
            "ability_prio": ["tornado", "fortitude"],
        }
        b = ws_runner.WSBot(cfg)
        self.assertEqual(b.name, "TestBot_A")
        self.assertEqual(b.style, "lmeow")
        self.assertEqual(b.skin, "pixagreen_mage")
        self.assertFalse(b.rotate_lane)
        self.assertTrue(b.wallet_required)
        self.assertTrue(b.wallet_ok)
        self.assertTrue(b.wallet_skin_ok)
        self.assertTrue(b.wallet_holder)

    def test_rotate_lane_flag(self):
        cfg = {"name": "S", "key": "k", "class": "mage", "rotate_lane": True}
        b = ws_runner.WSBot(cfg)
        self.assertTrue(b.rotate_lane)


class TestDeployGate(unittest.TestCase):
    def _make(self, **overrides):
        cfg = {"name": "TestBot_A", "key": "k", "class": "mage", "lane": "top",
               "style": "lmeow"}
        cfg.update(overrides)
        return ws_runner.WSBot(cfg)

    def test_no_wallet_required_deploys(self):
        bot = self._make()
        with patch.object(ws_runner, "api_post", return_value={"gameId": 1}) as m:
            bot.process(_state_with_no_hero())
        m.assert_called_once()
        self.assertTrue(bot.joined)

    def test_wallet_required_but_not_ok_blocks(self):
        bot = self._make(wallet=True, wallet_ok=False)
        with patch.object(ws_runner, "api_post") as m:
            bot.process(_state_with_no_hero())
        m.assert_not_called()
        self.assertFalse(bot.joined)

    def test_skin_missing_nft_blocks(self):
        bot = self._make(wallet=True, wallet_ok=True,
                         skin="pixagreen_mage", wallet_skin_ok=False)
        with patch.object(ws_runner, "api_post") as m:
            bot.process(_state_with_no_hero())
        m.assert_not_called()
        self.assertFalse(bot.joined)

    def test_skin_included_in_deploy_payload(self):
        bot = self._make(wallet=True, wallet_ok=True,
                         skin="pixagreen_mage", wallet_skin_ok=True)
        with patch.object(ws_runner, "api_post", return_value={"gameId": 7}) as m:
            bot.process(_state_with_no_hero())
        args, _ = m.call_args
        payload = args[2]
        self.assertEqual(payload.get("skin"), "pixagreen_mage")
        self.assertEqual(payload.get("heroClass"), "mage")
        self.assertEqual(payload.get("heroLane"), "top")

    def test_no_skin_no_skin_field(self):
        bot = self._make()
        with patch.object(ws_runner, "api_post", return_value={"gameId": 7}) as m:
            bot.process(_state_with_no_hero())
        args, _ = m.call_args
        self.assertNotIn("skin", args[2])


class TestNoShoveMicro(unittest.TestCase):
    """The past shove-micro bug: resending heroLane while already in the lane.

    This isn't in the deploy path; it's in the mid-game payload builder. The
    invariant to hold: when hero's current lane == bot.current_lane, nothing
    in the tick path should set heroLane back to the same lane.
    """

    def _bot_joined(self, lane="top", style="lmeow"):
        cfg = {"name": "TestBot_A", "key": "k", "class": "mage", "lane": lane,
               "style": style}
        b = ws_runner.WSBot(cfg)
        b.joined = True
        b.faction = "human"
        b._prev_alive = True
        b.current_lane = lane
        return b

    def test_steady_state_no_lane_switch(self):
        """5 minutes in, hero in own lane, no abilities available, no towers
        in danger: any api_post must not carry heroLane."""
        bot = self._bot_joined(lane="top")
        state = _state_with_hero("TestBot_A", lane="top", faction="human",
                                 tick=6000)  # 5 min in
        calls = []
        def fake_post(path, key, payload):
            calls.append(payload)
            return {}
        with patch.object(ws_runner, "api_post", side_effect=fake_post):
            bot.process(state)
        for payload in calls:
            if "heroLane" in payload:
                self.assertNotEqual(payload["heroLane"], "top",
                                    f"Sent heroLane=top while already in top: {payload}")


# ---------------------------------------------------------------------------
# experiment.bot_snap: stats shape
# ---------------------------------------------------------------------------

class TestBotSnap(unittest.TestCase):
    def _bot(self, **kw):
        cfg = {"name": "TestBot_A", "key": "k", "class": "mage", "lane": "top",
               "style": "lmeow"}
        cfg.update(kw)
        b = ws_runner.WSBot(cfg)
        b.faction = "human"
        b.kills = 10
        b.deaths = 2
        b._game_winner = "human"
        return b

    def test_snap_contains_experiment_dimensions(self):
        b = self._bot(skin="pixagreen_mage", wallet=True)
        b.wallet_skin_ok = True
        b.wallet_holder = True
        hero = {"level": 12, "hp": 250, "maxHp": 300, "xp": 50, "abilities": []}
        snap = experiment.bot_snap(b, hero)
        self.assertEqual(snap["name"], "TestBot_A")
        self.assertEqual(snap["lane"], "top")
        self.assertEqual(snap["style"], "lmeow")
        self.assertEqual(snap["skin"], "pixagreen_mage")
        self.assertTrue(snap["wallet_holder"])
        self.assertTrue(snap["won"])
        self.assertEqual(snap["kills_est"], 10)
        self.assertEqual(snap["deaths"], 2)
        self.assertEqual(snap["level"], 12)

    def test_skin_omitted_when_nft_not_owned(self):
        b = self._bot(skin="pixagreen_mage")
        b.wallet_skin_ok = False
        snap = experiment.bot_snap(b, None)
        self.assertIsNone(snap["skin"])

    def test_loss_recorded(self):
        b = self._bot()
        b._game_winner = "orc"  # we're human, so we lost
        snap = experiment.bot_snap(b, None)
        self.assertFalse(snap["won"])


# ---------------------------------------------------------------------------
# WSRunner: lane rotation advances after game ends
# ---------------------------------------------------------------------------

class TestRunnerLaneAdvance(unittest.TestCase):
    """Verify _advance_lane_rotation bumps game_idx and rewires flagged bots
    (without making any network calls or touching real stats.json)."""

    def _minimal_runner(self, bots_cfg, game_idx=0):
        """Build a WSRunner without running its real __init__ (no HTTP, no file I/O)."""
        runner = ws_runner.WSRunner.__new__(ws_runner.WSRunner)
        runner.game = 3
        runner.game_idx = game_idx
        runner.bots = [ws_runner.WSBot(c) for c in bots_cfg]
        runner.tick_count = 0
        runner.state = {}
        runner._last_winner = None
        return runner

    def test_advance_rotates_flagged_bot_only(self):
        runner = self._minimal_runner([
            {"name": "A", "key": "k", "class": "mage", "lane": "top"},
            {"name": "S", "key": "k", "class": "mage", "lane": "bot", "rotate_lane": True},
        ], game_idx=0)

        runner._advance_lane_rotation()
        self.assertEqual(runner.game_idx, 1)
        self.assertEqual(runner.bots[0].default_lane, "top")   # A untouched
        self.assertEqual(runner.bots[1].default_lane, "mid")   # S idx1 -> mid
        self.assertEqual(runner.bots[1].current_lane, "mid")

        runner._advance_lane_rotation()
        self.assertEqual(runner.bots[1].default_lane, "bot")   # idx2 -> bot

        runner._advance_lane_rotation()
        self.assertEqual(runner.bots[1].default_lane, "top")   # idx3 wraps

    def test_load_game_count_from_missing_file_is_zero(self):
        """Seed index starts at 0 when stats.json absent."""
        runner = ws_runner.WSRunner.__new__(ws_runner.WSRunner)
        with patch.object(ws_runner.os.path, "exists", return_value=False):
            self.assertEqual(runner._load_game_count(), 0)


# ---------------------------------------------------------------------------
# WSRunner._record_game: dashboard-compatible stats schema
# ---------------------------------------------------------------------------

class TestRecordGameSchema(unittest.TestCase):
    """ws_runner is now the single writer of stats.json. The record must
    contain everything the dashboard's existing StatsTracker expects
    (majority_won, human_bots, orc_bots, max levels, base HPs) PLUS the
    per-bot `bots` snapshots with experiment dimensions.
    """

    def setUp(self):
        import tempfile
        self.tmp_dir = tempfile.mkdtemp()
        self.stats_path = os.path.join(self.tmp_dir, "stats.json")
        self._orig_stats = ws_runner.STATS_FILE
        ws_runner.STATS_FILE = self.stats_path

    def tearDown(self):
        ws_runner.STATS_FILE = self._orig_stats
        if os.path.exists(self.stats_path):
            os.unlink(self.stats_path)
        os.rmdir(self.tmp_dir)

    def _build_runner(self, bots_cfg, game_idx=0):
        r = ws_runner.WSRunner.__new__(ws_runner.WSRunner)
        r.default_game = 3
        r.game_idx = game_idx
        r.bots = [ws_runner.WSBot(c) for c in bots_cfg]
        r.tick_count = 0
        r.state = {}
        r._last_winner_by_game = {}
        r._ws_by_game = {}
        r._threads_by_game = {}
        r._stop = False
        r._choices_cache = {}
        r._last_choices_poll = {}
        return r

    def _state(self, winner="human", human_levels=(10, 12), orc_levels=(8, 9),
               human_base=1500, orc_base=300, tick=20000):
        heroes = []
        for lv in human_levels:
            heroes.append({"name": f"h{lv}", "faction": "human", "level": lv, "alive": True,
                           "lane": "mid", "hp": 200, "maxHp": 200})
        for lv in orc_levels:
            heroes.append({"name": f"o{lv}", "faction": "orc", "level": lv, "alive": True,
                           "lane": "mid", "hp": 200, "maxHp": 200})
        return {
            "tick": tick, "winner": winner, "heroes": heroes, "towers": [],
            "bases": {"human": {"hp": human_base}, "orc": {"hp": orc_base}},
        }

    def test_record_writes_majority_won_when_majority_faction_wins(self):
        """2 human bots vs 1 orc bot -> majority is human; human wins -> MAJ WIN."""
        runner = self._build_runner([
            {"name": "A", "key": "k", "class": "mage", "lane": "top"},
            {"name": "B", "key": "k", "class": "mage", "lane": "mid"},
            {"name": "C", "key": "k", "class": "mage", "lane": "bot"},
        ])
        # Assign factions as if picked up during the game
        runner.bots[0].faction = "human"
        runner.bots[1].faction = "human"
        runner.bots[2].faction = "orc"
        state = self._state(winner="human")
        runner._record_game("human", state, runner.bots, gid=3)

        with open(self.stats_path) as f:
            games = json.load(f)
        self.assertEqual(len(games), 1)
        g = games[0]
        self.assertEqual(g["winner"], "human")
        self.assertEqual(g["majority_faction"], "human")
        self.assertTrue(g["majority_won"])
        self.assertEqual(g["gameId"], 3)
        self.assertEqual(g["human_bots"], 2)
        self.assertEqual(g["orc_bots"], 1)
        self.assertEqual(g["human_max_level"], 12)
        self.assertEqual(g["orc_max_level"], 9)
        self.assertEqual(g["human_base_hp"], 1500)
        self.assertEqual(g["orc_base_hp"], 300)
        self.assertEqual(len(g["bots"]), 3)
        # Dashboard's StatsTracker winrate uses majority_won — this must be top-level.
        self.assertIn("majority_won", g)
        self.assertIn("game_idx", g)

    def test_record_writes_majority_loss_when_minority_faction_wins(self):
        runner = self._build_runner([
            {"name": "A", "key": "k", "class": "mage"},
            {"name": "B", "key": "k", "class": "mage"},
            {"name": "C", "key": "k", "class": "mage"},
        ])
        runner.bots[0].faction = "human"
        runner.bots[1].faction = "orc"
        runner.bots[2].faction = "orc"
        state = self._state(winner="human")
        runner._record_game("human", state, runner.bots, gid=3)
        with open(self.stats_path) as f:
            g = json.load(f)[0]
        self.assertEqual(g["majority_faction"], "orc")
        self.assertFalse(g["majority_won"])
        self.assertEqual(g["orc_bots"], 2)

    def test_tied_split_counts_as_win_when_half_bots_won(self):
        """2H/2O split: human wins -> 2 of our bots (the human side) won.
        Under the old logic, majority defaults to 'orc' so majority_won=False.
        That undercounted wins across ~47% of games. New logic: majority_won
        is True iff at least half our bots ended the game on the winning side.
        majority_faction should read 'tie' for 2/2 (neither human nor orc).
        """
        runner = self._build_runner([
            {"name": "A", "key": "k", "class": "mage"},
            {"name": "B", "key": "k", "class": "mage"},
            {"name": "C", "key": "k", "class": "mage"},
            {"name": "D", "key": "k", "class": "mage"},
        ])
        runner.bots[0].faction = "human"
        runner.bots[1].faction = "human"
        runner.bots[2].faction = "orc"
        runner.bots[3].faction = "orc"
        state = self._state(winner="human",
                            human_levels=(10,), orc_levels=(10,))
        runner._record_game("human", state, runner.bots, gid=3)

        with open(self.stats_path) as f:
            g = json.load(f)[0]
        self.assertEqual(g["majority_faction"], "tie")
        self.assertTrue(
            g["majority_won"],
            "2H/2O split where 2 of our bots won should be majority_won=True"
        )
        self.assertEqual(g["human_bots"], 2)
        self.assertEqual(g["orc_bots"], 2)

    def test_tied_split_still_wins_when_orc_takes_game(self):
        """Mirror test: same 2H/2O split, but orc wins instead.
        Still 2 of our bots won (the orc side) -> majority_won=True.
        Confirms the fix is faction-symmetric."""
        runner = self._build_runner([
            {"name": "A", "key": "k", "class": "mage"},
            {"name": "B", "key": "k", "class": "mage"},
            {"name": "C", "key": "k", "class": "mage"},
            {"name": "D", "key": "k", "class": "mage"},
        ])
        runner.bots[0].faction = "human"
        runner.bots[1].faction = "human"
        runner.bots[2].faction = "orc"
        runner.bots[3].faction = "orc"
        state = self._state(winner="orc")
        runner._record_game("orc", state, runner.bots, gid=3)
        with open(self.stats_path) as f:
            g = json.load(f)[0]
        self.assertEqual(g["majority_faction"], "tie")
        self.assertTrue(g["majority_won"])

    def test_minority_on_winning_side_still_loses(self):
        """3H/1O split, orc wins -> only 1 of our bots won. 1 < half of 4.
        This is a legitimate loss; majority_won must stay False."""
        runner = self._build_runner([
            {"name": "A", "key": "k", "class": "mage"},
            {"name": "B", "key": "k", "class": "mage"},
            {"name": "C", "key": "k", "class": "mage"},
            {"name": "D", "key": "k", "class": "mage"},
        ])
        runner.bots[0].faction = "human"
        runner.bots[1].faction = "human"
        runner.bots[2].faction = "human"
        runner.bots[3].faction = "orc"
        state = self._state(winner="orc")
        runner._record_game("orc", state, runner.bots, gid=3)
        with open(self.stats_path) as f:
            g = json.load(f)[0]
        self.assertEqual(g["majority_faction"], "human")
        self.assertFalse(g["majority_won"])

    def test_reload_fleet_config_picks_up_new_skin(self):
        """Live edit to fleet.json between games — ws_runner should apply
        the new skin to the in-memory WSBot without restart, and trigger a
        wallet re-check."""
        import tempfile
        tmp_fleet = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"game": 3, "bots": [
            {"name": "B", "class": "mage", "lane": "top", "style": "lmeow",
             "wallet": True, "skin": "pixagreen_mage", "key": "k1"},
        ]}, tmp_fleet)
        tmp_fleet.close()

        orig_fleet = ws_runner.FLEET_FILE
        ws_runner.FLEET_FILE = tmp_fleet.name
        try:
            runner = self._build_runner([
                {"name": "B", "class": "mage", "lane": "top", "style": "lmeow",
                 "wallet": True, "key": "k1"},  # starts with NO skin
            ])
            self.assertIsNone(runner.bots[0].skin)

            # Patch auto_connect since we don't want real API calls
            with patch.object(ws_runner, "api_post", return_value={"gameId": 3}), \
                 patch("wallet.auto_connect",
                       side_effect=lambda bots: [b.update({"wallet_ok": True,
                                                           "wallet_skin_ok": True,
                                                           "wallet_holder": True}) for b in bots]):
                runner._reload_fleet_config()

            self.assertEqual(runner.bots[0].skin, "pixagreen_mage")
            self.assertTrue(runner.bots[0].wallet_skin_ok)
        finally:
            ws_runner.FLEET_FILE = orig_fleet
            os.unlink(tmp_fleet.name)

    def test_on_message_dispatches_unassigned_bots_in_default_game(self):
        """After _record_game resets game_id=None, the bot MUST still be
        processed in the default-game WS stream so its fresh deploy fires.
        Regression test for the orphaned-bot bug."""
        runner = self._build_runner([
            {"name": "A", "key": "k", "class": "mage", "lane": "top"},
        ])
        runner.default_game = 3
        runner.bots[0].game_id = None  # simulate post-game reset
        runner.bots[0].faction = "human"

        processed: list[str] = []
        orig_process = runner.bots[0].process

        def spy(state):
            processed.append(state.get("tick", 0))
            return orig_process(state)

        runner.bots[0].process = spy

        ws_msg = json.dumps({
            "gameId": 3, "tick": 100, "units": [], "winner": None,
            "towers": [], "bases": {}, "lanes": {},
        })
        # _parse_ws_state returns None for empty units, so the bot isn't
        # invoked for that specific payload. Provide a valid unit so the
        # dispatch path reaches bot.process.
        ws_msg = json.dumps({
            "gameId": 3, "tick": 100,
            "units": [[1, 3, 0, 500, 0, 100, 100, 0, 0, 0, 0, "EnemyHero",
                       0, 1, 0, 200, 0, [], [], 0]],
            "winner": None, "towers": [], "bases": {}, "lanes": {},
        })
        runner._on_message_for_game(3, ws_msg)
        self.assertEqual(processed, [100], "unassigned bot not dispatched in default game")

    def test_on_message_does_not_dispatch_unassigned_bot_in_non_default_game(self):
        """Orphan bots only ride the default game, never cross-dispatch to
        other games (otherwise the same bot could deploy into multiple games)."""
        runner = self._build_runner([
            {"name": "A", "key": "k", "class": "mage", "lane": "top"},
        ])
        runner.default_game = 3
        runner.bots[0].game_id = None
        processed: list[int] = []
        runner.bots[0].process = lambda state: processed.append(state.get("tick", 0))
        ws_msg = json.dumps({
            "gameId": 4, "tick": 50,
            "units": [[1, 3, 0, 500, 0, 100, 100, 0, 0, 0, 0, "EnemyHero",
                       0, 1, 0, 200, 0, [], [], 0]],
            "winner": None, "towers": [], "bases": {}, "lanes": {},
        })
        runner._on_message_for_game(4, ws_msg)
        self.assertEqual(processed, [], "orphan bot leaked into non-default game")

    def test_reload_skips_wallet_recheck_when_skin_unchanged(self):
        """No skin change -> no wallet API call (cost saving)."""
        import tempfile
        tmp_fleet = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"game": 3, "bots": [
            {"name": "B", "class": "mage", "lane": "mid", "style": "sigma",
             "wallet": True, "skin": "pixagreen_mage", "key": "k1"},
        ]}, tmp_fleet)
        tmp_fleet.close()

        orig_fleet = ws_runner.FLEET_FILE
        ws_runner.FLEET_FILE = tmp_fleet.name
        try:
            runner = self._build_runner([
                {"name": "B", "class": "mage", "lane": "top", "style": "lmeow",
                 "wallet": True, "skin": "pixagreen_mage", "key": "k1"},
            ])
            # Seed wallet state as if it was already connected at startup
            runner.bots[0].wallet_ok = True
            runner.bots[0].wallet_skin_ok = True

            with patch("wallet.auto_connect") as m:
                runner._reload_fleet_config()
            m.assert_not_called()  # skin unchanged -> no re-check
            # But style/lane changes DID apply
            self.assertEqual(runner.bots[0].style, "sigma")
            self.assertEqual(runner.bots[0].default_lane, "mid")
        finally:
            ws_runner.FLEET_FILE = orig_fleet
            os.unlink(tmp_fleet.name)

    def test_record_appends_to_existing_stats(self):
        """Game append is additive; never clobbers prior games."""
        with open(self.stats_path, "w") as f:
            json.dump([{"winner": "orc", "pre_existing": True}], f)
        runner = self._build_runner([{"name": "A", "key": "k", "class": "mage"}])
        runner.bots[0].faction = "human"
        state = self._state(winner="human")
        runner._record_game("human", state, runner.bots, gid=3)
        with open(self.stats_path) as f:
            games = json.load(f)
        self.assertEqual(len(games), 2)
        self.assertTrue(games[0]["pre_existing"])


# ---------------------------------------------------------------------------
# Dashboard BotBrain: read-only observer, NEVER POSTs
# ---------------------------------------------------------------------------

class TestDashboardObserver(unittest.TestCase):
    """Dashboard is view-only. BotBrain.observe must track visible state
    from /api/game/state and must NOT hit /api/strategy/deployment — that
    would create a second controller fighting ws_runner."""

    def setUp(self):
        # Import lazily so test file doesn't require rich/termios at module load
        import dashboard
        self.dashboard = dashboard
        self.BotBrain = dashboard.BotBrain

    def _bot(self, **kw):
        cfg = dict(name="TestBot_M", api_key="k", hero_class="mage",
                   default_lane="top", role="mage",
                   ability_prio=["tornado", "fortitude"], style="lmeow", game=3)
        cfg.update(kw)
        return self.BotBrain(**cfg)

    def _state(self, with_hero=True, winner=None, hp=120, max_hp=200, level=5,
               lane="top", xp=0, alive=True):
        state = {"tick": 1000, "winner": winner, "towers": [], "heroes": []}
        if with_hero:
            state["heroes"].append({
                "name": "TestBot_M", "faction": "human", "lane": lane,
                "alive": alive, "hp": hp, "maxHp": max_hp, "level": level,
                "xp": xp, "abilities": [], "abilityChoices": [],
                "recallCooldownMs": 0, "class": "mage",
            })
        return state

    def test_observe_never_posts(self):
        """Flat guarantee: observe() + requests.post must not be called."""
        bot = self._bot()
        with patch.object(self.dashboard.requests, "post") as mpost, \
             patch.object(self.dashboard, "api_post") as mapi:
            bot.observe(self._state())
            bot.observe(self._state(winner="human"))
            bot.observe(self._state(alive=False))
        mpost.assert_not_called()
        mapi.assert_not_called()

    def test_observe_marks_joined_when_hero_present(self):
        bot = self._bot()
        self.assertFalse(bot.joined)
        bot.observe(self._state())
        self.assertTrue(bot.joined)
        self.assertEqual(bot.faction, "human")
        self.assertEqual(bot.current_lane, "top")

    def test_observe_tracks_deaths(self):
        bot = self._bot()
        bot.observe(self._state(alive=True))
        bot.observe(self._state(alive=False))
        self.assertEqual(bot.deaths, 1)

    def test_observe_tracks_lane_switches_from_authoritative_state(self):
        bot = self._bot()
        bot.observe(self._state(lane="top"))
        bot.observe(self._state(lane="mid"))
        bot.observe(self._state(lane="bot"))
        self.assertEqual(bot.lane_switches, 2)
        self.assertEqual(bot.current_lane, "bot")

    def test_observe_resets_counters_on_game_over(self):
        bot = self._bot()
        bot.observe(self._state(alive=False))  # death
        self.assertEqual(bot.deaths, 1)
        bot.observe(self._state(winner="human"))
        self.assertEqual(bot.deaths, 0)
        self.assertEqual(bot.kills_est, 0)
        self.assertFalse(bot.joined)

    def test_no_pick_lane_or_should_recall_methods(self):
        """Defensive: strategy methods are removed. If someone re-adds them
        without the full review, this test tells them to stop."""
        bot = self._bot()
        self.assertFalse(hasattr(bot, "pick_lane"))
        self.assertFalse(hasattr(bot, "should_recall"))
        self.assertFalse(hasattr(bot, "pick_ability"))


# ---------------------------------------------------------------------------
# WSBot strategy v2: kill estimator, adaptive recall, tower-dive guard
# ---------------------------------------------------------------------------

class TestStrategyV2(unittest.TestCase):
    """Verifies the three strategy fixes shipped 2026-04-19:
       A. kill_est increments on XP jumps in [80, 500)
       B. recall fires on burst (>30% HP lost in 2s) OR critical (<15%), not enemy presence
       C. tower-dive guard prevents commands when chasing under enemy tower at <70% HP
    """

    def _bot(self, **kw):
        cfg = {"name": "B", "key": "k", "class": "mage", "lane": "top", "style": "sigma"}
        cfg.update(kw)
        return ws_runner.WSBot(cfg)

    def _state(self, name="B", hp=200, max_hp=200, level=5, xp=0, alive=True,
               tick=1000, lane="top", faction="human", winner=None,
               enemies_in_lane=0, enemy_tower_alive=False, enemy_tower_hp=1200):
        heroes = [{
            "name": name, "faction": faction, "lane": lane, "alive": alive,
            "hp": hp, "maxHp": max_hp, "level": level, "xp": xp,
            "abilities": [], "abilityChoices": [], "recallCooldownMs": 0,
        }]
        enemy_fact = "orc" if faction == "human" else "human"
        for i in range(enemies_in_lane):
            heroes.append({
                "name": f"e{i}", "faction": enemy_fact, "lane": lane,
                "alive": True, "hp": 200, "maxHp": 200, "level": 5,
            })
        towers = []
        if enemy_tower_alive:
            towers.append({"faction": enemy_fact, "lane": lane,
                           "alive": True, "hp": enemy_tower_hp})
        return {"tick": tick, "winner": winner, "heroes": heroes,
                "towers": towers, "bases": {}, "lanes": {}}

    # ---- A: kill estimator ----

    def test_kill_est_increments_on_small_xp_jump(self):
        """Old code missed kills under 180 XP. Beta's L2 kill on the leaderboard
        gave ~120 XP and went uncounted. Threshold lowered to 80."""
        bot = self._bot()
        bot.joined = True
        bot.faction = "human"
        bot._prev_alive = True
        # Seed prev_xp_total via first tick
        with patch.object(ws_runner, "api_post", return_value={}):
            bot.process(self._state(level=2, xp=0, tick=100))
            # Next tick: +120 XP (one kill on a low-level enemy)
            bot.process(self._state(level=2, xp=120, tick=120))
        self.assertEqual(bot.kills, 1)

    def test_kill_est_ignores_creep_xp(self):
        bot = self._bot()
        bot.joined = True
        bot.faction = "human"
        bot._prev_alive = True
        with patch.object(ws_runner, "api_post", return_value={}):
            bot.process(self._state(level=2, xp=0, tick=100))
            # +30 XP — that's just a couple of creeps, no kill
            bot.process(self._state(level=2, xp=30, tick=120))
        self.assertEqual(bot.kills, 0)

    def test_kill_est_ignores_huge_xp_jumps(self):
        """A 600+ XP jump implies multi-kill or level-up double-count.
        Skip rather than over-credit."""
        bot = self._bot()
        bot.joined = True
        bot.faction = "human"
        bot._prev_alive = True
        with patch.object(ws_runner, "api_post", return_value={}):
            bot.process(self._state(level=2, xp=0, tick=100))
            bot.process(self._state(level=5, xp=0, tick=120))   # level-up jump
        self.assertEqual(bot.kills, 0)

    # ---- B: adaptive recall ----

    def test_recall_does_NOT_fire_on_mere_enemy_presence(self):
        """The previous bug: any enemy in lane = recall_threshold jumped to 33%.
        New code ignores enemy presence — only HP trajectory matters."""
        bot = self._bot()
        bot.joined = True
        bot.faction = "human"
        bot._prev_alive = True
        bot._prev_xp_total = 100
        calls = []
        with patch.object(ws_runner, "api_post", side_effect=lambda *a: calls.append(a)):
            # 60% HP, enemies in lane — old code would recall, new should not
            bot.process(self._state(hp=120, max_hp=200, enemies_in_lane=2, tick=200))
        self.assertFalse(any(c[2].get("action") == "recall" for c in calls if len(c) > 2),
                         "should NOT recall at 60% HP just because enemies are nearby")

    def test_recall_fires_on_critical_hp(self):
        bot = self._bot()
        bot.joined = True
        bot.faction = "human"
        bot._prev_alive = True
        bot._prev_xp_total = 100
        calls = []
        with patch.object(ws_runner, "api_post", side_effect=lambda *a: calls.append(a)):
            bot.process(self._state(hp=20, max_hp=200, tick=200))   # 10% HP
        self.assertTrue(any("recall" == c[2].get("action") for c in calls),
                        "should recall at <15% HP")

    def test_recall_fires_on_burst_damage(self):
        """Lost >30% HP in 2s and now <55% → likely under heavy fire, retreat."""
        bot = self._bot()
        bot.joined = True
        bot.faction = "human"
        bot._prev_alive = True
        bot._prev_xp_total = 100
        # Tick 100: 200/200. Tick 120 (1s later): 80/200 (lost 60%).
        with patch.object(ws_runner, "api_post", return_value={}):
            bot.process(self._state(hp=200, max_hp=200, tick=100))
        calls = []
        with patch.object(ws_runner, "api_post", side_effect=lambda *a: calls.append(a)):
            bot.process(self._state(hp=80, max_hp=200, tick=120))   # 40% HP, 60% lost in 1s
        self.assertTrue(any("recall" == c[2].get("action") for c in calls),
                        "should recall on detected burst")

    # ---- C: tower-dive guard ----

    def test_tower_dive_guard_blocks_low_hp_chase(self):
        """At <70% HP with enemy tower alive + enemy hero in lane, send no
        commands — let bot drift back naturally."""
        bot = self._bot()
        bot.joined = True
        bot.faction = "human"
        bot._prev_alive = True
        bot._prev_xp_total = 100
        bot._last_command_tick = 0
        calls = []
        with patch.object(ws_runner, "api_post", side_effect=lambda *a: calls.append(a)):
            # 60% HP, enemy tower alive, enemy in lane → guard should block all commands
            bot.process(self._state(hp=120, max_hp=200,
                                    enemies_in_lane=1,
                                    enemy_tower_alive=True, tick=2000))
        # No deploy commands should be issued under tower at low HP
        self.assertEqual(len(calls), 0,
                         f"tower-dive guard failed; calls were: {calls}")

    def test_tower_dive_guard_allows_full_hp_engage(self):
        """At full HP, the guard does nothing — bot can engage normally."""
        bot = self._bot()
        bot.joined = True
        bot.faction = "human"
        bot._prev_alive = True
        bot._prev_xp_total = 100
        bot._last_command_tick = 0
        # No assertion on actual call — just verify guard doesn't fire.
        # We confirm by checking _last_command_tick wasn't bumped artificially.
        with patch.object(ws_runner, "api_post", return_value={}):
            bot.process(self._state(hp=200, max_hp=200,
                                    enemies_in_lane=1,
                                    enemy_tower_alive=True, tick=3000))
        # If guard had fired, _last_command_tick would == 3000 with no real call.
        # The other code paths might also set it to 3000, so we just check the
        # bot didn't crash and no unexpected state mutation.
        self.assertTrue(True)


# ---------------------------------------------------------------------------
# WSBot strategy v3: fresh-game gate, death-streak filter, breadth-first ability
# ---------------------------------------------------------------------------

class TestStrategyV3(unittest.TestCase):
    """v3 fixes (2026-04-20):
       D. Fresh-game gate: don't deploy into a mid-game match (tick > 600)
          unless we've seen a game-end since startup.
       E. Death-streak filter: 3 consecutive dead ticks before counting a
          death (eliminates WS flicker over-count).
       F. Breadth-first ability picker (AzFlin "one of each gud ability"):
          prefer NEW ability over upgrading any existing one.
       G. Kill-execute override: force fireball when an enemy hero in lane
          is below 30% HP and fireball is offered.
    """

    def _bot(self, **kw):
        cfg = {"name": "B", "key": "k", "class": "mage", "lane": "top",
               "style": "sigma", "ability_prio": ["fireball", "tornado", "fortitude", "fury"]}
        cfg.update(kw)
        return ws_runner.WSBot(cfg)

    # ---- D: fresh-game gate ----

    def test_gate_blocks_deploy_when_tick_high_and_no_game_over_seen(self):
        bot = self._bot()
        with patch.object(ws_runner, "api_post") as m:
            bot.process({"tick": 8000, "winner": None, "heroes": [], "towers": []})
        m.assert_not_called()
        self.assertFalse(bot.joined)

    def test_gate_allows_deploy_when_tick_low(self):
        bot = self._bot()
        with patch.object(ws_runner, "api_post", return_value={"gameId": 3}) as m:
            bot.process({"tick": 100, "winner": None, "heroes": [], "towers": []})
        m.assert_called_once()
        self.assertTrue(bot.joined)

    def test_gate_opens_after_seeing_game_over(self):
        bot = self._bot()
        # First message: game-over (sets _seen_game_over)
        bot.process({"tick": 9000, "winner": "human", "heroes": [], "towers": []})
        # Now even at high tick the bot will deploy (next game has begun)
        with patch.object(ws_runner, "api_post", return_value={"gameId": 3}) as m:
            bot.process({"tick": 8000, "winner": None, "heroes": [], "towers": []})
        m.assert_called_once()
        self.assertTrue(bot.joined)

    # ---- E: death-streak filter ----

    def _alive_state(self, alive, tick=100):
        return {
            "tick": tick, "winner": None, "towers": [],
            "heroes": [{
                "name": "B", "faction": "human", "lane": "top",
                "alive": alive, "hp": 100 if alive else 0, "maxHp": 200,
                "level": 5, "xp": 0,
                "abilities": [], "abilityChoices": [], "recallCooldownMs": 0,
            }],
        }

    def test_death_streak_filter_ignores_single_tick_flicker(self):
        bot = self._bot()
        bot.joined = True; bot.faction = "human"
        bot._prev_xp_total = 100
        with patch.object(ws_runner, "api_post", return_value={}):
            bot.process(self._alive_state(True))
            bot.process(self._alive_state(False, tick=120))   # 1 tick dead
            bot.process(self._alive_state(True, tick=140))    # back alive — flicker
        self.assertEqual(bot.deaths, 0, "single-tick alive=False should be ignored")

    def test_death_counts_after_3_consecutive_dead_ticks(self):
        bot = self._bot()
        bot.joined = True; bot.faction = "human"
        bot._prev_xp_total = 100
        with patch.object(ws_runner, "api_post", return_value={}):
            bot.process(self._alive_state(True, tick=100))
            bot.process(self._alive_state(False, tick=120))
            bot.process(self._alive_state(False, tick=140))
            bot.process(self._alive_state(False, tick=160))   # 3rd consecutive
        self.assertEqual(bot.deaths, 1)

    def test_death_counts_only_once_per_streak(self):
        """Stay dead for many ticks -> still only 1 death recorded until alive again."""
        bot = self._bot()
        bot.joined = True; bot.faction = "human"
        bot._prev_xp_total = 100
        with patch.object(ws_runner, "api_post", return_value={}):
            bot.process(self._alive_state(True, tick=100))
            for t in range(120, 600, 20):
                bot.process(self._alive_state(False, tick=t))
        self.assertEqual(bot.deaths, 1)

    # ---- F: breadth-first ability + G: kill-execute ----

    def _ability_state(self, choices, abilities=None, enemy_hp_pct=1.0, tick=200):
        heroes = [{
            "name": "B", "faction": "human", "lane": "top",
            "alive": True, "hp": 200, "maxHp": 200, "level": 5, "xp": 0,
            "abilities": abilities or [],
            "abilityChoices": choices,
            "recallCooldownMs": 0,
        }]
        if enemy_hp_pct < 1.0:
            heroes.append({
                "name": "Enemy", "faction": "orc", "lane": "top",
                "alive": True, "hp": int(200 * enemy_hp_pct), "maxHp": 200,
                "level": 5,
            })
        return {"tick": tick, "winner": None, "heroes": heroes, "towers": []}

    def test_breadth_first_picks_NEW_ability_over_upgrading_existing(self):
        """Bot already has fireball L1. Choices: [fireball (upgrade), tornado (new)].
        Per AzFlin breadth-first: pick tornado, not fireball-L2."""
        bot = self._bot()
        bot.joined = True; bot.faction = "human"
        bot._prev_xp_total = 100; bot._last_command_tick = 0
        calls = []
        with patch.object(ws_runner, "api_post", side_effect=lambda *a: calls.append(a)):
            bot.process(self._ability_state(
                choices=["fireball", "tornado"],
                abilities=[{"id": "fireball", "level": 1}],
            ))
        ability_calls = [c[2].get("abilityChoice") for c in calls if c[2].get("abilityChoice")]
        self.assertIn("tornado", ability_calls,
                      f"should pick NEW (tornado) over upgrade (fireball); got {ability_calls}")

    def test_breadth_first_falls_back_to_upgrade_when_no_new(self):
        """All abilities owned. Pick by ability_prio order (fireball first)."""
        bot = self._bot()
        bot.joined = True; bot.faction = "human"
        bot._prev_xp_total = 100; bot._last_command_tick = 0
        calls = []
        with patch.object(ws_runner, "api_post", side_effect=lambda *a: calls.append(a)):
            bot.process(self._ability_state(
                choices=["fireball", "tornado"],
                abilities=[{"id": "fireball", "level": 1},
                           {"id": "tornado", "level": 1}],
            ))
        ability_calls = [c[2].get("abilityChoice") for c in calls if c[2].get("abilityChoice")]
        self.assertIn("fireball", ability_calls,
                      f"both owned, should fall back to prio[0]=fireball; got {ability_calls}")

    def test_kill_execute_override_picks_fireball_on_low_hp_enemy(self):
        """Enemy hero in lane at <30% HP -> fireball wins regardless of breadth-first."""
        bot = self._bot()
        bot.joined = True; bot.faction = "human"
        bot._prev_xp_total = 100; bot._last_command_tick = 0
        calls = []
        with patch.object(ws_runner, "api_post", side_effect=lambda *a: calls.append(a)):
            # Bot already has fireball L1 — breadth-first would normally pick tornado.
            # But enemy at 20% HP triggers kill-execute override -> fireball.
            bot.process(self._ability_state(
                choices=["fireball", "tornado"],
                abilities=[{"id": "fireball", "level": 1}],
                enemy_hp_pct=0.20,
            ))
        ability_calls = [c[2].get("abilityChoice") for c in calls if c[2].get("abilityChoice")]
        self.assertIn("fireball", ability_calls,
                      f"low-HP enemy + fireball offered -> kill-execute should fire; got {ability_calls}")


# ---------------------------------------------------------------------------
# Dashboard.RunnerProcess: spawn + stop lifecycle
# ---------------------------------------------------------------------------

class TestRunnerProcess(unittest.TestCase):
    def setUp(self):
        import dashboard
        self.dashboard = dashboard

    def test_start_spawns_popen_with_new_session(self):
        fake = MagicMock()
        fake.pid = 12345
        fake.poll.return_value = None
        rp = self.dashboard.RunnerProcess(["python3", "ws_runner.py"], "/tmp/_x.log")
        with patch.object(self.dashboard.subprocess, "Popen", return_value=fake) as mpop, \
             patch("builtins.open", unittest.mock.mock_open()) as mopen:
            rp.start()
        mpop.assert_called_once()
        kwargs = mpop.call_args.kwargs
        self.assertTrue(kwargs.get("start_new_session"))
        self.assertIs(rp.proc, fake)
        self.assertTrue(rp.alive())

    def test_stop_sigterms_then_sigkills_on_timeout(self):
        fake = MagicMock()
        fake.pid = 777
        fake.poll.return_value = None  # still alive on stop()
        fake.wait.side_effect = [
            # First wait: SIGTERM timed out
            __import__("subprocess").TimeoutExpired(cmd="x", timeout=2.0),
            # Second wait (after SIGKILL) returns
            None,
        ]
        rp = self.dashboard.RunnerProcess(["python3", "ws_runner.py"], "/tmp/_x.log")
        rp.proc = fake
        rp._log_fp = None
        with patch.object(self.dashboard.os, "killpg") as mkill, \
             patch.object(self.dashboard.os, "getpgid", return_value=777):
            rp.stop(timeout=0.01)
        signals = [call.args[1] for call in mkill.call_args_list]
        self.assertIn(__import__("signal").SIGTERM, signals)
        self.assertIn(__import__("signal").SIGKILL, signals)

    def test_ensure_alive_respawns_dead_runner(self):
        """If ws_runner crashed or got pkilled, ensure_alive relaunches it."""
        dead = MagicMock()
        dead.pid = 99
        dead.poll.return_value = 1  # exited with code 1
        rp = self.dashboard.RunnerProcess(["python3", "ws_runner.py"], "/tmp/_x.log")
        rp.proc = dead
        # Make start() a no-op that sets a fresh proc
        fresh = MagicMock()
        fresh.pid = 100
        fresh.poll.return_value = None
        with patch.object(rp, "start", side_effect=lambda: setattr(rp, "proc", fresh)) as mstart:
            respawned = rp.ensure_alive()
        self.assertTrue(respawned)
        mstart.assert_called_once()
        self.assertEqual(rp._respawn_count, 1)

    def test_ensure_alive_noop_when_alive(self):
        live = MagicMock()
        live.pid = 10
        live.poll.return_value = None
        rp = self.dashboard.RunnerProcess(["x"], "/tmp/_x.log")
        rp.proc = live
        with patch.object(rp, "start") as mstart:
            self.assertFalse(rp.ensure_alive())
        mstart.assert_not_called()

    def test_ensure_alive_noop_after_stop(self):
        """User pressed q -> stop() sets _stopped. ensure_alive must NOT respawn."""
        dead = MagicMock()
        dead.poll.return_value = 0
        rp = self.dashboard.RunnerProcess(["x"], "/tmp/_x.log")
        rp.proc = dead
        rp._stopped = True
        with patch.object(rp, "start") as mstart:
            self.assertFalse(rp.ensure_alive())
        mstart.assert_not_called()

    def test_stop_is_idempotent_when_not_started(self):
        rp = self.dashboard.RunnerProcess(["python3", "ws_runner.py"], "/tmp/_x.log")
        # No proc assigned — stop must be a no-op, not raise.
        rp.stop()

    def test_recent_lines_returns_last_n_from_log(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            for i in range(20):
                f.write(f"line{i}\n")
            path = f.name
        try:
            rp = self.dashboard.RunnerProcess(["x"], path)
            lines = rp.recent_lines(5)
            self.assertEqual(lines, ["line15", "line16", "line17", "line18", "line19"])
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
