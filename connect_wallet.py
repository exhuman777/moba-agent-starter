#!/usr/bin/env python3
"""Connect a wallet to bot API keys.

PK source (same as ws_runner):
  1. env var WALLET_PK
  2. file ./wallet.key (chmod 600)

Usage:
  python3 connect_wallet.py             # all bots with wallet=true in fleet.json
  python3 connect_wallet.py MyBot_Top   # one bot only (by name)
"""

import json, os, sys
from wallet import load_pk, connect_bot

DIR = os.path.dirname(os.path.abspath(__file__))

pk = load_pk()
if not pk:
    sys.exit("no PK found: set WALLET_PK env var or create wallet.key")

with open(os.path.join(DIR, "fleet.json")) as f:
    fleet = json.load(f)

targets = sys.argv[1:]
if targets:
    bots = [b for b in fleet["bots"] if b["name"] in targets]
else:
    bots = [b for b in fleet["bots"] if b.get("wallet")]

if not bots:
    sys.exit("no matching bots (check names or add wallet=true in fleet.json)")

from eth_account import Account
print(f"wallet: {Account.from_key(pk).address}")

for b in bots:
    r = connect_bot(b["name"], b["key"], pk)
    print(f"{b['name']}: {r}")
