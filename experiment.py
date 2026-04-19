"""Experiment helpers: lane rotation + per-bot stats extension.

Kept pure / side-effect free so E2E tests can exercise the logic without
spinning up a WebSocket or making HTTP calls.
"""

from __future__ import annotations
from typing import Iterable

LANES = ("top", "mid", "bot")


def rotate_lane(game_idx: int) -> str:
    """Deterministic lane for game N under round-robin rotation.

    game_idx 0 -> top, 1 -> mid, 2 -> bot, 3 -> top, ...
    """
    return LANES[game_idx % len(LANES)]


def apply_lane_rotation(bots: Iterable[dict], game_idx: int, key: str = "rotate_lane") -> None:
    """Mutate `lane` on every bot flagged `rotate_lane=true` to match current game_idx.

    Only touches dicts with key=True. Others untouched (fixed-lane control bots).
    """
    for b in bots:
        if b.get(key):
            b["lane"] = rotate_lane(game_idx)


def bot_snap(bot, hero: dict | None) -> dict:
    """Per-bot snapshot for stats.json. Includes experiment dimensions
    (lane, style, skin, wallet_holder) so downstream analysis can slice cleanly.
    """
    snap = {
        "name": bot.name,
        "class": bot.hero_class,
        "style": getattr(bot, "style", None),
        "lane": getattr(bot, "current_lane", None),
        "skin": getattr(bot, "skin", None) if getattr(bot, "wallet_skin_ok", True) else None,
        "wallet_holder": getattr(bot, "wallet_holder", None),
        "faction": getattr(bot, "faction", None),
        "kills_est": getattr(bot, "kills", 0),
        "deaths": getattr(bot, "deaths", 0),
        "won": bot.faction == getattr(bot, "_game_winner", None) if bot.faction else False,
    }
    if hero:
        snap["level"] = hero.get("level", 1)
        snap["abilities"] = hero.get("abilities", [])
        snap["hp"] = hero.get("hp", 0)
        snap["maxHp"] = hero.get("maxHp", 0)
        snap["xp"] = hero.get("xp", 0)
    return snap
