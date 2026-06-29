"""bootstrap.py — the deterministic first-run initialization daemon.

On its *very first* boot a swarm claims its digital sovereignty before any
normal agent loop runs. This is a deterministic 6-step daemon (`CLAUDE.md`
§Sovereign Autonomy & Bootstrapping Wizard):

  1. Cryptographic identity generation (EVM keypair -> ~/.automaton/wallet.json).
  2. Infrastructure provisioning via Sign-In-With-Ethereum (SIWE).
  3. Environment detection (sandbox container vs bare metal).
  4. Creator handshake (hardcode the Creator Audit Key).
  5. Operational wallet bootstrap (CDP wallet).
  6. Finalize — unfreeze and mark first-run complete.

It is idempotent (a no-op once ``first_run_complete``) and offline-safe: the two
network/secret-dependent steps (SIWE provisioning, CDP wallet) degrade to a
"pending" marker rather than aborting, so the scaffold initializes without
secrets and goes live once they are injected.

Security: the private key is written to ``~/.automaton/wallet.json`` at ``0600``
and is NEVER logged, printed, or placed in ``BusinessState``. Only the public
address is surfaced.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

WALLET_DIR = Path("~/.automaton").expanduser()
KEY_FILE = WALLET_DIR / "wallet.json"  # private identity (distinct from wallet_data.json)

PLACEHOLDER_CREATOR_KEY = "0x000000000000000000000000000000000000dead"


# ---------------------------------------------------------------------------
# Step 1 — Cryptographic identity
# ---------------------------------------------------------------------------


def _generate_identity() -> str:
    """Generate (or load) the swarm's EVM keypair; return its public address.

    Idempotent: if ``wallet.json`` already exists, the existing address is
    returned and no new key is minted. The key file is created ``0600`` inside a
    ``0700`` directory. When ``BOOTSTRAP_KEYSTORE_PASSWORD`` is set the key is
    stored as an encrypted keystore; otherwise it is stored raw with a loud
    warning. The private key never leaves this function.
    """

    WALLET_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(WALLET_DIR, 0o700)

    if KEY_FILE.exists():
        data = json.loads(KEY_FILE.read_text())
        # Encrypted keystore stores the address bare; raw stores it too.
        return data.get("address") or "0x" + data.get("crypto", {}).get("address", "")

    try:
        from eth_account import Account

        acct = Account.create()
        address = acct.address
        password = os.environ.get("BOOTSTRAP_KEYSTORE_PASSWORD")
        if password:
            payload = Account.encrypt(acct.key, password)  # standard keystore JSON
        else:
            logger.warning(
                "BOOTSTRAP_KEYSTORE_PASSWORD unset — storing key UNENCRYPTED at 0600."
            )
            payload = {"address": address, "private_key": acct.key.hex()}
    except Exception as exc:  # eth_account missing — raw entropy fallback
        logger.warning("eth_account unavailable (%s); using raw-entropy key.", exc)
        raw = os.urandom(32)
        address = "0x" + raw[-20:].hex()  # not a real EVM address; placeholder
        payload = {"address": address, "private_key": raw.hex(), "raw_fallback": True}

    fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(payload, fh)
    os.chmod(KEY_FILE, 0o600)
    return address


# ---------------------------------------------------------------------------
# Step 2 — SIWE infrastructure provisioning (hook)
# ---------------------------------------------------------------------------


def _provision_infrastructure(address: str) -> bool:
    """Authenticate to decentralized providers via SIWE and provision API keys.

    Builds and locally signs a Sign-In-With-Ethereum message; the actual provider
    API-key provisioning needs network + credentials and is a documented hook.
    Returns True only when real provisioning succeeded (False = pending).
    """

    # The SIWE statement that a provider would verify against `address`.
    _statement = (
        f"Aria-Swarm {address} requests API provisioning. "
        "Sign-In-With-Ethereum (EIP-4361)."
    )
    # TODO(infra): sign `_statement` with the wallet key and exchange the
    # signature for provider API keys (Akash / CDP / Conway). Needs network.
    return False


# ---------------------------------------------------------------------------
# Step 3 — Environment detection
# ---------------------------------------------------------------------------


def _detect_environment() -> tuple[str, str]:
    """Return (environment, compute_tier): containerized vs bare metal."""

    containerized = (
        Path("/.dockerenv").exists()
        or os.environ.get("KUBERNETES_SERVICE_HOST") is not None
        or _cgroup_indicates_container()
    )
    if containerized:
        return "sandbox_container", "constrained"
    return "bare_metal", "full"


def _cgroup_indicates_container() -> bool:
    try:
        text = Path("/proc/1/cgroup").read_text()
        return any(m in text for m in ("docker", "kubepods", "containerd", "lxc"))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Step 5 — Operational wallet (hook)
# ---------------------------------------------------------------------------


def _bootstrap_operational_wallet() -> str:
    """Initialize the CDP operational wallet; 'ready' or 'pending' (no creds)."""

    try:
        from tools.wallet import initialize_wallet

        initialize_wallet()
        return "ready"
    except Exception as exc:  # missing CDP creds / network — degrade gracefully
        logger.warning("operational wallet bootstrap pending: %s", exc)
        return "pending"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_bootstrap(state: dict) -> dict:
    """Run the 6-step first-run daemon once. Idempotent on ``first_run_complete``.

    Mutates ``state`` in place and returns it. Safe to call every boot.
    """

    if state.get("first_run_complete"):
        return state

    flags = state["operational_flags"]

    # 1. Cryptographic identity.
    address = _generate_identity()
    flags["wallet_address"] = address

    # 2. SIWE infrastructure provisioning (hook).
    flags["infra_provisioned"] = _provision_infrastructure(address)

    # 3. Environment detection.
    environment, compute_tier = _detect_environment()
    state["environment"] = environment
    flags["compute_tier"] = compute_tier

    # 4. Creator handshake — read-only audit grant (never a swarm signing key;
    #    an attempted fund pull by this key is a panic event at the wallet layer).
    creator_key = os.environ.get("CREATOR_AUDIT_KEY", PLACEHOLDER_CREATOR_KEY)
    state["capital"]["creator_audit_key"] = creator_key
    flags["creator_audit_key_set"] = creator_key != PLACEHOLDER_CREATOR_KEY

    # 5. Operational wallet bootstrap (hook).
    flags["operational_wallet"] = _bootstrap_operational_wallet()

    # 6. Finalize — unfreeze and mark complete.
    state["first_run_complete"] = True
    state["frozen"] = False
    state["error_log"].append(f"BOOTSTRAP complete: identity={address} env={environment}")

    _print_banner(state)
    return state


def _print_banner(state: dict) -> None:
    flags = state["operational_flags"]
    print(
        "\n"
        "================= 🌱 SWARM BOOTSTRAP (first run) 🌱 =================\n"
        f"  1. identity      : {flags.get('wallet_address')}\n"
        f"  2. infra (SIWE)  : {'provisioned' if flags.get('infra_provisioned') else 'pending'}\n"
        f"  3. environment   : {state.get('environment')} ({flags.get('compute_tier')})\n"
        f"  4. creator key   : {'set' if flags.get('creator_audit_key_set') else 'placeholder'}\n"
        f"  5. op. wallet    : {flags.get('operational_wallet')}\n"
        f"  6. finalize      : first_run_complete=True, frozen=False\n"
        "====================================================================\n"
    )
