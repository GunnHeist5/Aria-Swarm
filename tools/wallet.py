"""tools/wallet.py — embedded programmatic wallet for the swarm (USDC over Base).

Built on ``coinbase-agentkit`` 0.7.x (CDP v2 server-wallet model). Three jobs:

  1. ``initialize_wallet``       — hydrate-or-create a persistent CDP EVM wallet.
  2. ``get_wallet_balance_usdc`` — live on-chain USDC balance as a float.
  3. ``execute_secure_transfer`` — gasless USDC transfer behind the MPC
     session-key guardrails (5 USDC/call, 50 USDC/day; fail-closed + HITL).

API reality (vs. the classic AgentKit "export wallet string" model):
``coinbase-agentkit`` 0.7.4 ships ``CdpEvmWalletProvider`` whose accounts are
*server-side* at CDP. There is no exported private-key blob to persist; instead
a wallet is re-hydrated by passing its **address** (+ network) back into the
config, with the API/wallet *secrets supplied only via environment variables*.
So ``~/.automaton/wallet_data.json`` stores nothing more sensitive than the
public address and network id — never key material. (The legacy class name
``CdpWalletProvider`` maps to ``CdpEvmWalletProvider`` here.)

Heavy SDK imports are deferred into the functions that need them so this module
imports — and the security-critical guardrail path is unit-testable — without
the on-chain stack installed or any live credentials.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from state import BusinessState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Network & token configuration
# ---------------------------------------------------------------------------

MAINNET_NETWORK_ID = "base-mainnet"
TESTNET_NETWORK_ID = "base-sepolia"

# Canonical USDC (6 decimals) contract per Base network.
USDC_CONTRACTS = {
    MAINNET_NETWORK_ID: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    TESTNET_NETWORK_ID: "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
}

# Wallet persistence (re-hydration material only — no secrets, no keys).
WALLET_DIR = Path.home() / ".automaton"
WALLET_FILE = WALLET_DIR / "wallet_data.json"


def _resolve_network_id() -> str:
    """Return the active Base network id (testnet gated by ``TESTNET=true``)."""

    return (
        TESTNET_NETWORK_ID
        if os.environ.get("TESTNET", "").lower() == "true"
        else MAINNET_NETWORK_ID
    )


# ---------------------------------------------------------------------------
# Provider construction seam (isolated so tests can stub the on-chain SDK)
# ---------------------------------------------------------------------------


def _build_provider(network_id: str, address: str | None) -> Any:
    """Instantiate a ``CdpEvmWalletProvider``.

    Secrets are read from the environment by the provider itself
    (``CDP_API_KEY_ID`` / ``CDP_API_KEY_SECRET`` / ``CDP_WALLET_SECRET``).
    Passing ``address`` re-hydrates an existing CDP account; ``None`` creates a
    fresh one. This is the only function that imports the SDK.
    """

    from coinbase_agentkit import CdpEvmWalletProvider, CdpEvmWalletProviderConfig

    return CdpEvmWalletProvider(
        CdpEvmWalletProviderConfig(network_id=network_id, address=address)
    )


# ---------------------------------------------------------------------------
# 1. Initialization & persistence
# ---------------------------------------------------------------------------


def initialize_wallet() -> Any:
    """Hydrate the persisted wallet if present, else create and persist one.

    On first run, creates a CDP account on the active network and writes its
    re-hydration material to ``~/.automaton/wallet_data.json`` with strict
    ``0600`` permissions inside a ``0700`` directory. On subsequent runs, reads
    the stored address and rebuilds the provider against the same account.
    """

    network_id = _resolve_network_id()

    if WALLET_FILE.exists():
        data = json.loads(WALLET_FILE.read_text())
        address = data.get("address")
        stored_network = data.get("network_id", network_id)
        logger.info("Hydrating wallet %s on %s", address, stored_network)
        return _build_provider(stored_network, address)

    # First run: create a fresh account, then persist its public material.
    logger.info("No wallet backup found — initializing fresh wallet on %s", network_id)
    provider = _build_provider(network_id, None)
    _persist_wallet(provider, network_id)
    return provider


def _persist_wallet(provider: Any, network_id: str) -> None:
    """Write public re-hydration material to disk with fail-closed permissions.

    Stores only the wallet address and network id — never API keys or wallet
    secrets (those live in the environment). The file is created ``0600`` inside
    a ``0700`` directory.
    """

    WALLET_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Tighten dir perms even if it pre-existed with looser bits.
    os.chmod(WALLET_DIR, 0o700)

    payload = {"address": provider.get_address(), "network_id": network_id}
    # Create with 0600 from the start: open via os.open with the mode mask.
    fd = os.open(WALLET_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(payload, fh)
    os.chmod(WALLET_FILE, 0o600)
    logger.info("Persisted wallet address to %s (0600)", WALLET_FILE)


# ---------------------------------------------------------------------------
# 2. Live USDC balance
# ---------------------------------------------------------------------------


def get_wallet_balance_usdc(wallet_provider: Any) -> float:
    """Return the wallet's on-chain USDC balance as a float (whole USDC).

    Reads the USDC ERC-20 contract for the active network via the AgentKit
    token helper, which already accounts for the token's 6 decimals.
    """

    from coinbase_agentkit.action_providers.erc20.utils import get_token_details

    network_id = wallet_provider.get_network().network_id
    usdc_contract = USDC_CONTRACTS[network_id]

    details = get_token_details(wallet_provider, usdc_contract)
    if details is None:
        logger.warning("Could not fetch USDC token details on %s", network_id)
        return 0.0
    return float(details.formatted_balance)


# ---------------------------------------------------------------------------
# 3. MPC session-key guardrails — the 5/50 rule
# ---------------------------------------------------------------------------


def _usdc_transfer(
    wallet_provider: Any,
    destination_address: str,
    amount_usdc: float,
    gasless: bool,
) -> str:
    """Execute the on-chain USDC transfer (the single on-chain seam).

    On Base, USDC transfers from a CDP wallet are Paymaster-sponsored — i.e.
    gasless — by design, so ``gasless`` is the contract this wrapper enforces
    rather than a toggle that could silently fall back to a gas-paying path.
    """

    if not gasless:
        # Internal ledger is strictly USDC with no native-gas leakage; refuse a
        # gas-paying path rather than silently spending ETH.
        raise ValueError("Only gasless (Paymaster-routed) USDC transfers are permitted")

    from coinbase_agentkit.action_providers.erc20.erc20_action_provider import (
        ERC20ActionProvider,
    )

    network_id = wallet_provider.get_network().network_id
    usdc_contract = USDC_CONTRACTS[network_id]

    return ERC20ActionProvider().transfer(
        wallet_provider,
        {
            "amount": str(amount_usdc),  # whole units, e.g. "2.5" USDC
            "contract_address": usdc_contract,
            "destination_address": destination_address,
        },
    )


def execute_secure_transfer(
    wallet_provider: Any,
    destination_address: str,
    amount_usdc: float,
    state: BusinessState,
    gasless: bool = True,
) -> dict:
    """Guardrailed USDC transfer. Fail-closed on any spend-cap breach.

    Reads the cumulative daily spend and caps from ``state['financials']`` (the
    MPC session-key policy: 5 USDC/call, 50 USDC/day). A breach NEVER clamps and
    proceeds — it halts autonomous execution by flagging HITL and returns
    without touching the chain.

    Returns a status dict: ``{"status": "blocked"|"sent", ...}``.
    """

    fin = state["financials"]
    per_call_cap = fin["spend_per_call_cap_usdc"]
    per_day_cap = fin["spend_per_day_cap_usdc"]
    spent_today = fin["spent_today_usdc"]

    def _halt(reason: str) -> dict:
        logger.warning(
            "Spend cap breach (%s): amount=%.6f spent_today=%.6f "
            "per_call=%.2f per_day=%.2f -> freezing for HITL",
            reason, amount_usdc, spent_today, per_call_cap, per_day_cap,
        )
        hitl = state["hitl"]
        hitl["requires_auth"] = True       # the prompt's literal halt flag
        hitl["hitl_pending"] = True        # the flag the graph freeze logic consults
        hitl["hitl_reason"] = "wallet_spend_cap"
        return {
            "status": "blocked",
            "reason": reason,
            "amount_usdc": amount_usdc,
            "destination": destination_address,
        }

    # --- Fail-closed guardrails (checked BEFORE any on-chain call) ---
    if amount_usdc > per_call_cap:
        return _halt("per_call_cap_exceeded")
    if spent_today + amount_usdc > per_day_cap:
        return _halt("per_day_cap_exceeded")

    # --- Valid under-cap transfer: gasless via Paymaster ---
    result = _usdc_transfer(wallet_provider, destination_address, amount_usdc, gasless)

    # Bookkeeping stays strictly in USDC.
    fin["spent_today_usdc"] = spent_today + amount_usdc
    fin["wallet_balance_usdc"] = get_wallet_balance_usdc(wallet_provider)

    logger.info(
        "Sent %.6f USDC to %s (gasless); spent_today now %.6f",
        amount_usdc, destination_address, fin["spent_today_usdc"],
    )
    return {
        "status": "sent",
        "amount_usdc": amount_usdc,
        "destination": destination_address,
        "result": result,
    }
