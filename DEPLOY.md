# Deploying the swarm to your VPS

This runs the swarm 24/7 on your own Linux box, where outbound network is open
(unlike the dev sandbox, which blocks Together/Coinbase). Commands assume
**Ubuntu 22.04/24.04**; adjust `apt` for other distros.

> Time: ~15 min. You need: SSH access to the VPS, your filled-in `.env` values,
> and a little USDC on **Base** to fund the wallet.

---

## 1. System prep (as root or sudo)

```bash
apt update && apt install -y python3 python3-venv python3-pip git ufw
# Dedicated non-root user so the swarm never runs as root:
adduser --disabled-password --gecos "" swarm
usermod -aG sudo swarm        # optional; remove if you don't want sudo
# Lock down inbound (the swarm needs NO inbound ports):
ufw allow OpenSSH && ufw --force enable
```

## 2. Get the code (as the `swarm` user)

```bash
su - swarm
git clone https://github.com/GunnHeist5/Aria-Swarm.git
cd Aria-Swarm
git checkout claude/autonomous-swarm-langgraph-f9l56h
python3 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip
pip install .            # installs the pinned deps from pyproject.toml
pip install solana       # fixes the CDP wallet import (operational_wallet -> ready)
```

## 3. Secrets (`.env` on the box — never committed)

```bash
cp .env.example .env
nano .env                # paste your real values (Together, CDP, creator, etc.)
chmod 600 .env           # readable only by the swarm user
```
Set `SEED_CAPITAL_USDC=0` for now — we set the real number after funding (step 5).

## 4. First boot — generate the wallet identity

```bash
python main.py --cron
```
This runs the 6-step bootstrap and prints the **wallet address** (and persists an
*encrypted* keystore to `~/.automaton/wallet.json`, `0600`). The cycle itself may
exit non-zero on this first unfunded run — that's expected. **Copy the address.**

## 5. Fund the wallet, then set the seed

- Send the USDC you're willing to risk (start tiny — e.g. **$20–50**) to that
  address **on the Base network** (USDC on Base, not Ethereum mainnet).
- Once it lands, set the seed to match and clear the first snapshot so genesis
  re-reads the funded balance:
```bash
sed -i 's/^SEED_CAPITAL_USDC=.*/SEED_CAPITAL_USDC=50/' .env   # match what you sent
rm -f ~/.automaton/state_snapshot.json
python main.py --cron     # should now run a full funded cycle (Infancy)
```
On a funded wallet the genesis on-chain balance sync reflects the real balance,
so the swarm boots in **Infancy** (not extinction).

## 6. Run it on a schedule (systemd timer — recommended)

One cycle per tick, durable via the JSON snapshot, survives reboots.

`/etc/systemd/system/swarm.service`:
```ini
[Unit]
Description=Aria-Swarm one cycle
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=swarm
WorkingDirectory=/home/swarm/Aria-Swarm
ExecStart=/home/swarm/Aria-Swarm/.venv/bin/python main.py --cron
```

`/etc/systemd/system/swarm.timer`:
```ini
[Unit]
Description=Run Aria-Swarm every 30 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=30min
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now swarm.timer
systemctl list-timers swarm.timer        # confirm it's scheduled
journalctl -u swarm.service -f           # watch live cycle output
```

> Prefer a continuous loop instead? `python main.py --auto --max-cycles 50`
> runs back-to-back cycles until the budget/freeze/ceiling — but the cron timer
> is safer and easier to babysit. `--auto` only spends while
> `AUTO_MODE_BUDGET_USD > 0`.

## 7. Booking real revenue (the 10% per deal)

When a wholesaling deal closes and the swarm's 10% lands as USDC in the wallet,
record it so the treasury/economics reflect it:
```bash
cd /home/swarm/Aria-Swarm && source .venv/bin/activate
python -m tools.revenue --deal-id <your-deal-id> --assignment-fee 12000
# -> books 10% ($1,200) into revenue + treasury; idempotent per deal-id
```
A CRM/Zapier webhook can call this same command on a "deal closed" trigger. See
`WHOLESALING.md` for the fiat→USDC routing and the full pipeline plan.

---

## Health & safety

- **State**: `cat ~/.automaton/state_snapshot.json | python3 -m json.tool` —
  watch `metabolic_state`, `financials`, `capital.capital_phase`.
- **Frozen for HITL?** If a cycle prints the `SWARM FROZEN` banner, a human
  decision is required. Resume with:
  `python resume.py swarm_production_v1 approve|reject`.
- **Keys**: `~/.automaton/wallet.json` (encrypted keystore) and `.env` are both
  `0600`. Keep `BOOTSTRAP_KEYSTORE_PASSWORD` somewhere safe — losing it loses the
  identity key. Neither is ever committed (`.gitignore`).
- **Spend caps** are enforced (5 USDC/call, 50/day); real withdrawals and the
  dividend/replication money-moves are still HITL/simulated — do not treat
  on-chain payouts as live until that's wired (see `WHOLESALING.md` → Out of scope).
- **Updating**: `git pull`, `pip install .`, restart the timer.
