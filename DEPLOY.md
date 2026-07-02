# Deploying QuantResearch on Ubuntu

Tested on **Ubuntu 22.04 LTS** with Python 3.11 + Nginx + Let's Encrypt.
Should work on 20.04 LTS and 24.04 LTS with minor command tweaks.

---

## 0. What you'll have running

```
Internet  ──HTTPS──>  Nginx (:80, :443)  ──HTTP──>  Gunicorn (:5050)  ──>  Flask app
                                                          │
                                                          ├──>  cache/auth.db    (users, devices, plans, payments)
                                                          ├──>  cache/screener.db (scan results, portfolios)
                                                          └──>  Angel One WebSocket (live prices)
```

- Public traffic goes through Nginx → Gunicorn (one worker, in-memory scan state).
- HTTPS terminated by Nginx via Let's Encrypt; the upstream is plain HTTP.
- Flask's `ProxyFix` already trusts `X-Forwarded-Proto` so OAuth callback URLs come out as `https://...`.

---

## 1. Pre-flight (do this once, before touching the server)

1. **Domain DNS** — Point an A record (e.g. `app.yourdomain.com`) at your Ubuntu server's public IP. Wait for propagation (`dig app.yourdomain.com +short` should return your IP).

2. **Google Cloud Console** — Open your OAuth 2.0 client → **Authorized redirect URIs** → add:
   ```
   https://app.yourdomain.com/auth/google/callback
   ```
   Save. (Keep the localhost URI too if you still develop locally.)

3. **Fingerprint.com Dashboard** — If you set request filtering on your Public API key, add `https://app.yourdomain.com` to allowed origins.

---

## 2. Provision the server

```bash
ssh root@YOUR_SERVER_IP

# Update + base packages
apt update && apt upgrade -y
apt install -y python3.11 python3.11-venv python3-pip nginx certbot python3-certbot-nginx unzip git ufw

# Firewall — allow SSH + HTTP + HTTPS only
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable

# Create a non-root user to run the app
adduser --disabled-password --gecos "" smart
usermod -aG www-data smart
```

---

## 3. Upload and extract the deploy zip

From your local machine:
```bash
scp /tmp/quantresearch-deploy.zip root@YOUR_SERVER_IP:/tmp/
```

Back on the server:
```bash
mkdir -p /opt/quantresearch
unzip /tmp/quantresearch-deploy.zip -d /opt/quantresearch
rm /tmp/quantresearch-deploy.zip    # remove the zip — it contains secrets
chown -R smart:smart /opt/quantresearch
```

---

## 4. Set up Python environment

```bash
su - smart
cd /opt/quantresearch
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

---

## 5. Configure `.env`

The zip already contains a working `.env`. Open it and **update the redirect URI / domain values** for production:

```bash
nano /opt/quantresearch/.env
```

Verify these match production:
- `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` — should be the same as dev (you registered the prod redirect URI in Google Cloud Console in step 1.2).
- `FLASK_SECRET_KEY` — fine as-is. Or rotate with `python -c "import secrets; print(secrets.token_hex(32))"`.
- `FINGERPRINT_PUBLIC_KEY` / `FINGERPRINT_SECRET_KEY` / `FINGERPRINT_API_REGION` — same as dev.
- `ANGEL_*` — live trading credentials.

Lock the file down:
```bash
chmod 600 /opt/quantresearch/.env
```

---

## 6. Initialize the database and seed the first admin

```bash
cd /opt/quantresearch
source venv/bin/activate

# Create cache/ + run schema migrations (first python import triggers init_db)
python -c "import auth_db, db; auth_db.init_db(); db.init_db()"

# Seed one or more admins. They can add more later via /admin UI.
python scripts/add_admin.py you@yourdomain.com
```

Expected output:
```
ok: you@yourdomain.com  id=1  status=approved  is_admin=1 (stub — awaits first login)
Current admins:
  - you@yourdomain.com  (not yet signed in)
```

---

## 7. Smoke-test the app before fronting it with Nginx

```bash
# Still as 'smart' user, in venv
PORT=5050 python app.py
```

In another terminal:
```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:5050/
# expect: 200
```

If you see boot logs ending in `Running on http://...:5050` and curl returns 200, you're good. **Ctrl-C** to stop — we'll run it via systemd next.

---

## 8. systemd service (auto-start, auto-restart)

As **root** (exit the `smart` shell first):

```bash
cat > /etc/systemd/system/quantresearch.service <<'EOF'
[Unit]
Description=QuantResearch (Flask + Gunicorn)
After=network.target

[Service]
Type=simple
User=smart
Group=smart
WorkingDirectory=/opt/quantresearch
EnvironmentFile=/opt/quantresearch/.env
ExecStart=/opt/quantresearch/venv/bin/gunicorn \
    --workers 1 \
    --threads 4 \
    --bind 127.0.0.1:5050 \
    --timeout 120 \
    --access-logfile /var/log/quantresearch.access.log \
    --error-logfile /var/log/quantresearch.error.log \
    app:app
Restart=always
RestartSec=5
StandardOutput=append:/var/log/quantresearch.stdout.log
StandardError=append:/var/log/quantresearch.stderr.log

[Install]
WantedBy=multi-user.target
EOF

# Create log files with right ownership
touch /var/log/quantresearch.{access,error,stdout,stderr}.log
chown smart:smart /var/log/quantresearch.*.log

systemctl daemon-reload
systemctl enable --now quantresearch
systemctl status quantresearch --no-pager
```

**Important:** `--workers 1` is intentional. The in-memory `scan_state` and the Angel One WebSocket connection aren't shared across workers. Don't increase it without first moving state to Redis/DB.

---

## 9. Nginx reverse proxy

```bash
cat > /etc/nginx/sites-available/quantresearch <<'EOF'
server {
    listen 80;
    listen [::]:80;
    server_name app.yourdomain.com;

    # Larger uploads for payment screenshots (5 MB) + QR images (2 MB)
    client_max_body_size 8M;

    location / {
        proxy_pass http://127.0.0.1:5050;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_redirect off;
        proxy_buffering off;
        # WebSocket / SSE friendly
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 120s;
    }
}
EOF

# Replace the placeholder domain
sed -i 's/app\.yourdomain\.com/REAL_DOMAIN_HERE/g' /etc/nginx/sites-available/quantresearch

# Activate
ln -sf /etc/nginx/sites-available/quantresearch /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
```

Test plain HTTP first:
```bash
curl -s -o /dev/null -w "%{http_code}\n" http://app.yourdomain.com/
# expect: 200
```

---

## 10. SSL with Let's Encrypt

```bash
certbot --nginx -d app.yourdomain.com --redirect --agree-tos -m you@yourdomain.com -n
```

This rewrites the Nginx config to:
- Add `listen 443 ssl;`
- Mount the Let's Encrypt certs
- 301-redirect all HTTP → HTTPS

Cert auto-renews via the systemd timer that Certbot installs (`systemctl list-timers | grep cert`).

Test:
```bash
curl -sI https://app.yourdomain.com/ | head -1   # expect: HTTP/2 200
```

---

## 11. Final verification

Open `https://app.yourdomain.com/` in a browser:

| Step | Expected |
|---|---|
| Landing page loads with logo + "Sign in" / "Get started" buttons | ✓ |
| Click "Sign in with Google" | Google account picker, then redirect back to the dashboard |
| Sign in as the admin email you seeded | Lands on the dashboard directly (admin auto-approved, no trial gate) |
| Visit `/admin/` | 4 tabs: Payments / Users / Plans / Settings / Admins |
| Sign in as a **non-admin** Google account | Redirects to `/auth/device/verify` → FingerprintJS Pro fingerprints → either dashboard (first device) or pending/conflict |
| Trial countdown banner visible at top of dashboard for non-admins | ✓ |

---

## 12. Day-to-day operations

```bash
# Tail live logs
tail -f /var/log/quantresearch.stdout.log
tail -f /var/log/quantresearch.error.log

# Restart after .env change
systemctl restart quantresearch

# Check status / failure cause
systemctl status quantresearch
journalctl -u quantresearch -n 100 --no-pager

# Add another admin
sudo -iu smart bash -c "cd /opt/quantresearch && source venv/bin/activate && python scripts/add_admin.py new-admin@example.com"

# Backup the DB (do this on a cron — auth.db has all users/payments/devices)
sqlite3 /opt/quantresearch/cache/auth.db ".backup '/var/backups/auth-$(date +%Y%m%d).db'"
```

---

## 13. Deploying an update

```bash
# Locally: build a new zip the same way you built the first one
scp /tmp/quantresearch-deploy.zip root@YOUR_SERVER_IP:/tmp/

# On the server:
systemctl stop quantresearch
unzip -o /tmp/quantresearch-deploy.zip -d /opt/quantresearch
chown -R smart:smart /opt/quantresearch
sudo -iu smart bash -c "cd /opt/quantresearch && source venv/bin/activate && pip install -r requirements.txt"
systemctl start quantresearch
rm /tmp/quantresearch-deploy.zip
```

The DB and uploaded files in `cache/` are not overwritten — the zip doesn't contain `cache/`. Schema changes apply automatically via the idempotent migration in `auth_db.init_db()`.

---

## 14. Common gotchas

| Problem | Fix |
|---|---|
| Google rejects sign-in with `redirect_uri_mismatch` | Add the exact production URI to GCP OAuth client (step 1.2). Match HTTPS scheme. |
| User stuck on "Verifying your device" forever | Ad blocker is blocking `fpjscdn.net`. Open in a clean browser to confirm. Long-term fix: set up Subdomain Integration on fingerprint.com to proxy the JS through your own domain. |
| `/admin` returns 403 | The signed-in user isn't an admin. Either seed them via `add_admin.py` or have an existing admin add them via `/admin → Admins → + Add admin`. |
| Live prices not updating | Angel One credentials wrong or WebSocket connection failing. Check `journalctl -u quantresearch` for `Angel One login failed`. |
| Trial banner shows wrong days | The DB stores UTC timestamps. If the user's clock is way off, the calculation can look weird. Check `python -c "from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat())"` matches server time. |
| Suspended admin can't get back in | Currently admins can't be suspended (the SQL has `AND is_admin=0`). But if you manually `UPDATE users SET status='suspended' WHERE id=X` and lock yourself out, you'll need to SSH in and `UPDATE users SET status='approved' WHERE id=X`. |
| App OOMs / dies under load | You're hitting the single-worker limit. The architecture trades horizontal scaling for in-memory simplicity. To scale: move `scan_state` and live-price cache to Redis, then bump workers. Non-trivial — talk to me first. |

---

## 15. Security checklist (review before going live)

- [ ] `.env` is `chmod 600` (only readable by `smart` user)
- [ ] Deploy zip deleted from `/tmp/` after extraction
- [ ] UFW enabled, only ports 22/80/443 open
- [ ] HTTPS via Let's Encrypt active, HTTP→HTTPS redirect verified
- [ ] Google OAuth consent screen is "External" + Testing mode → only your listed test users can sign in (or publish the app if you want anyone with Google to sign up)
- [ ] At least 2 admins seeded (avoid lockout if one loses their device)
- [ ] First daily DB backup scheduled (cron + `sqlite3 .backup`)
- [ ] `FLASK_SECRET_KEY` rotated from the dev value (optional but recommended)
- [ ] If anyone's seen the dev `GOOGLE_CLIENT_SECRET` or `FINGERPRINT_SECRET_KEY`, rotate them before exposing the prod URL publicly

---

## Quick reference: ports + paths

| What | Where |
|---|---|
| App dir | `/opt/quantresearch/` |
| Virtualenv | `/opt/quantresearch/venv/` |
| `.env` | `/opt/quantresearch/.env` (chmod 600) |
| DBs | `/opt/quantresearch/cache/auth.db`, `screener.db` |
| Uploads | `/opt/quantresearch/cache/uploads/qr/`, `cache/uploads/payments/` |
| Logs | `/var/log/quantresearch.{access,error,stdout,stderr}.log` |
| systemd unit | `/etc/systemd/system/quantresearch.service` |
| Nginx vhost | `/etc/nginx/sites-available/quantresearch` |
| Gunicorn bind | `127.0.0.1:5050` (internal) |
| Public ports | 80 (redirect), 443 (HTTPS) |
| Run as user | `smart` |
