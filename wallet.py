"""Wallet connect helper. Loads PK locally, signs offline, posts signature only.

PK source order:
  1. env var WALLET_PK
  2. file ~/defense-bot/wallet.key (single line, 0x-prefixed hex)

The PK never leaves this machine. Only signature + address + timestamp hit the API.
Call is idempotent, safe to run on every startup.
"""

from __future__ import annotations
import logging, os, time
import requests

log = logging.getLogger("wallet")

BASE = "https://wc2-agentic-dev-3o6un.ondigitalocean.app"
DIR = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(DIR, "wallet.key")


def load_pk() -> str | None:
    pk = os.environ.get("WALLET_PK")
    if not pk and os.path.exists(KEY_FILE):
        with open(KEY_FILE) as f:
            pk = f.read().strip()
    if not pk:
        return None
    return pk if pk.startswith("0x") else "0x" + pk


def connect_bot(bot_name: str, bot_key: str, pk: str) -> dict:
    from eth_account import Account
    from eth_account.messages import encode_defunct

    acct = Account.from_key(pk)
    ts = int(time.time() * 1000)
    msg = f"I am connecting my wallet to Defense of the Agents.\n\nAddress: {acct.address}\nTimestamp: {ts}"
    sig = Account.sign_message(encode_defunct(text=msg), private_key=pk).signature.hex()
    if not sig.startswith("0x"):
        sig = "0x" + sig
    try:
        r = requests.post(
            f"{BASE}/api/wallet/connect",
            headers={"Authorization": f"Bearer {bot_key}", "Content-Type": "application/json"},
            json={"address": acct.address, "source": "injected", "signature": sig, "timestamp": ts},
            timeout=10,
        )
        return r.json() if r.status_code == 200 else {"error": f"{r.status_code} {r.text[:120]}"}
    except Exception as e:
        return {"error": str(e)[:120]}


def auto_connect(bots: list[dict]) -> None:
    """Connect every bot in `bots` that has wallet=true. Called once at startup.

    Mutates each target dict with:
      wallet_ok (bool) - connect succeeded
      wallet_holder (bool|None) - $DOTA holder buff active
      wallet_skin_ok (bool) - if bot requests a skin, wallet owns the NFT
    Bots with wallet=true but wallet_ok=False must refuse to deploy.
    """
    targets = [b for b in bots if b.get("wallet")]
    if not targets:
        return
    for b in targets:
        b["wallet_ok"] = False
        b["wallet_holder"] = None
        b["wallet_skin_ok"] = True  # assume ok if no skin requested

    pk = load_pk()
    if not pk:
        log.warning(f"WALLET_PK not set (env or {KEY_FILE}), no auto-connect for {[b['name'] for b in targets]}")
        return
    try:
        import eth_account  # noqa
    except ImportError:
        log.warning("eth-account not installed (pip install eth-account), no auto-connect")
        return

    SKIN_FIELDS = {"pixagreen_mage": "pixagreenMage", "space_marine": "spaceMarine"}

    for b in targets:
        r = connect_bot(b["name"], b["key"], pk)
        if "error" in r:
            log.warning(f"wallet {b['name']}: FAIL {r['error']} (bot will NOT deploy)")
            continue
        b["wallet_ok"] = True
        b["wallet_holder"] = r.get("tokenHolder")
        skin = b.get("skin")
        if skin:
            field = SKIN_FIELDS.get(skin, skin)
            owns = bool(r.get(field)) or bool(r.get(skin))
            b["wallet_skin_ok"] = owns
            if not owns:
                log.warning(f"wallet {b['name']}: connected but does NOT own NFT '{skin}' (bot will NOT deploy)")
        bal = r.get("tokenBalance")
        perks = {k: v for k, v in r.items() if k not in ("message", "address", "tokenBalance", "tokenHolder")}
        log.info(f"wallet {b['name']}: ok holder={b['wallet_holder']} bal={bal} perks={perks}")
