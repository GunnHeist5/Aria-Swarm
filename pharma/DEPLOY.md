# Going live: browser access via Cloudflare Tunnel

This puts the pharma app on a real HTTPS address (e.g. `https://pharma.yourdomain.com`)
so the trainer can log in from any browser — no SSH tunnel, no terminal. The app
keeps listening only on the server's localhost; Cloudflare's tunnel carries
traffic in, so **no inbound ports are opened**.

> Time: ~15 min. You need: your VPS, a free Cloudflare account, and a domain
> added to Cloudflare (its nameservers pointed at Cloudflare).

All commands run **on the VPS**, from `~/Aria-Swarm/pharma`, after `git pull`.

---

## 1. Run the app as a service (instead of `make dev`)

`make dev` dies when your terminal closes and hot-reloads on file changes —
fine for development, wrong for your dad. Install the systemd service:

```bash
sudo cp deploy/pharma.service /etc/systemd/system/pharma.service
# If your clone is not at /root/Aria-Swarm, edit the two paths in the file:
#   sudo nano /etc/systemd/system/pharma.service
sudo systemctl daemon-reload
sudo systemctl enable --now pharma
systemctl status pharma          # should say "active (running)"
```

Watch logs any time with `journalctl -u pharma -f`. After a `git pull`, apply
updates with `sudo systemctl restart pharma`.

(Stop any old `make dev` window first — two copies can't share port 8100.)

## 2. Expose it through the Cloudflare tunnel

### Path A — the server already runs a tunnel (likely: the deal desk uses one)

```bash
cloudflared tunnel list                 # note the tunnel name/ID
sudo nano /etc/cloudflared/config.yml   # the existing tunnel config
```

Add this entry to the `ingress:` list, **above** the final
`- service: http_status:404` line (see `deploy/cloudflared-ingress.yml` for a
full example):

```yaml
  - hostname: pharma.YOURDOMAIN.com
    service: http://127.0.0.1:8100
```

Then point DNS at the tunnel and restart it:

```bash
cloudflared tunnel route dns <TUNNEL-NAME> pharma.YOURDOMAIN.com
sudo systemctl restart cloudflared
```

### Path B — no existing tunnel

```bash
# Install cloudflared if missing (Ubuntu):
curl -L https://pkg.cloudflare.com/cloudflared-linux-amd64.deb -o /tmp/cf.deb && sudo dpkg -i /tmp/cf.deb

cloudflared tunnel login                       # opens a browser link — pick your domain
cloudflared tunnel create pharma
cloudflared tunnel route dns pharma pharma.YOURDOMAIN.com
sudo mkdir -p /etc/cloudflared
sudo cp deploy/cloudflared-ingress.yml /etc/cloudflared/config.yml
sudo nano /etc/cloudflared/config.yml          # fill in tunnel ID + credentials path + hostname
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

### Verify

Open `https://pharma.YOURDOMAIN.com/login` in a browser — the sign-in page
should load with a padlock. From another machine, `http://YOUR_SERVER_IP:8100`
should NOT load (the app only listens on localhost — that's correct).

## 3. Turn on HTTPS-only cookies

Now that logins happen over HTTPS, make session cookies refuse plain HTTP:

```bash
nano .env        # add the line: PHARMA_COOKIE_SECURE=1
sudo systemctl restart pharma
```

## 4. Before the trainer logs in

- **Issue him a fresh trainer token.** The seeded tokens have been pasted into
  chats — treat them as burned. Sign in to `/admin` with your admin token →
  Issue key → role `trainer` → copy the token it shows once. (Or on the server:
  `.venv/bin/python -m app.cli issue-key trainer`.)
- **Send the token safely** — password manager share or Signal, not email/SMS.
- His bookmark: `https://pharma.YOURDOMAIN.com/login` → paste token → he lands
  in the Trainer console, with the Training gym in the top nav.

## 5. Optional but recommended: Cloudflare Access

A free second lock: visitors must pass an email allow-list check before they
even see the login page.

1. Cloudflare dashboard → **Zero Trust** → Access → Applications → *Add an
   application* → Self-hosted.
2. Application domain: `pharma.YOURDOMAIN.com`.
3. Add a policy: Action *Allow*, Include → Emails → your email and your dad's.

Anyone else gets a Cloudflare block page; you two get a one-time email code,
then the normal token login.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Cloudflare 502 Bad Gateway | App not running — `systemctl status pharma`, `journalctl -u pharma -n 50` |
| Sign-in returns "server not configured" (503) | `PHARMA_SECRET_KEY` missing from `.env` — set it, restart pharma |
| Login loops back to the sign-in page | `PHARMA_COOKIE_SECURE=1` set but you're browsing over plain HTTP — use the https URL |
| Hostname doesn't resolve | DNS route missing — re-run `cloudflared tunnel route dns …`, wait a minute |
| Page loads but analysis fails | `ANTHROPIC_API_KEY` missing/invalid in `.env` |
