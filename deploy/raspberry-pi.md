# Running Mudate on a Raspberry Pi (headless)

Mudate works on a Raspberry Pi with **no desktop environment**. Chromium runs
headless, so no X server, VNC, or display manager is needed.

The recommended setup is **Raspberry Pi OS Lite (64-bit)** with a native Python
install and a systemd service. Docker also works; see the last section.

---

## 0. What you need

| | Minimum | Recommended |
|---|---|---|
| Board | Pi 4 (2 GB) | Pi 4 (4 GB+) or Pi 5 |
| OS | Raspberry Pi OS Lite, **64-bit** | same, Bookworm or Trixie |
| Storage | 8 GB SD | 32 GB+ SSD/USB 3 (the DB is rewritten on every scrape) |
| Swap | 1 GB | 2 GB (Chromium is memory hungry) |
| Network | LAN | LAN or Tailscale for remote access |

> **The OS must be 64-bit.** Playwright's Chromium is only published for
> `aarch64`. A 32-bit install (`armv7l`) cannot run it.

Check before going further:

```bash
uname -m          # must print: aarch64
getconf LONG_BIT   # must print: 64
```

If either says 32-bit, reflash with **Raspberry Pi OS Lite (64-bit)**.

---

## 1. Install system packages

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git python3 python3-venv python3-pip
```

You do **not** need `xvfb`, a desktop, or `chromium` at this point — the next
steps install Playwright's own headless-capable Chromium.

---

## 2. Clone and create the virtualenv

```bash
git clone https://github.com/YOUR_USERNAME/mudate.git ~/mudate
cd ~/mudate/backend

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

---

## 3. Install Chromium and its libraries

Run the two commands separately. The first downloads the browser **as your
user** (so the service can find it later); the second installs the OS
libraries **as root**:

```bash
cd ~/mudate/backend
.venv/bin/playwright install chromium
sudo .venv/bin/playwright install-deps chromium
```

`install-deps` is the step that installs the shared libraries Chromium needs
(`libnss3`, `libgbm1`, `libasound2`, …). It does not install a desktop or an
X server.

> On Raspberry Pi OS **Trixie** the ALSA package is `libasound2t64`. Let
> `install-deps` choose it; don't install browser libraries by hand.

### If Playwright's Chromium will not install on your board

If the download fails with *"Playwright does not support chromium"* or a 404
(older Playwright releases, or an OS version Playwright does not recognize),
use the distro Chromium instead:

```bash
sudo apt install -y chromium
export PLAYWRIGHT_EXECUTABLE_PATH=/usr/bin/chromium
```

Keep that variable in the service environment (step 5) so it survives reboots.
The bundled Playwright build is still preferred: it is version-pinned and
better tested.

---

## 4. Test it manually

```bash
mkdir -p ~/mudate/data
cd ~/mudate/backend

PLAYWRIGHT_HEADLESS=1 \
DB_PATH=$HOME/mudate/data/db.json \
.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8010
```

Open `http://<pi-ip>:8010` from another device, create a session, and run one
scrape. Watch the terminal for errors before setting up the service.

- `PLAYWRIGHT_HEADLESS=1` is **required**: without it Zonaprop tries to open a
  window and the scrape fails with a missing-display error.
- `DB_PATH` must point into an existing directory. `storage.py` creates the
  file but not the folder.
- Optional: add `OPENCAGE_API_KEY=...` for better geocoding.

Press `Ctrl+C` when the test scrape finishes.

---

## 5. Run it at boot with systemd

This repo ships a user unit at [`deploy/mudate.service`](mudate.service). It
assumes the project is at `~/mudate`; edit the paths if yours is elsewhere.

```bash
mkdir -p ~/.config/systemd/user
cp ~/mudate/deploy/mudate.service ~/.config/systemd/user/mudate.service

# Optional secrets/overrides (OPENCAGE_API_KEY, PLAYWRIGHT_EXECUTABLE_PATH, ...)
install -m 600 /dev/null ~/.config/mudate.env

systemctl --user daemon-reload
systemctl --user enable --now mudate

# Start at boot even when nobody logs in — essential on a headless Pi:
sudo loginctl enable-linger "$USER"
```

Useful commands:

```bash
systemctl --user status mudate     # is it running?
journalctl --user -u mudate -f     # follow logs
systemctl --user restart mudate    # after a config change
systemctl --user stop mudate
```

The unit already sets `PLAYWRIGHT_HEADLESS=1` and
`DB_PATH=%h/mudate/data/db.json`. It also reads `~/.config/mudate.env`, whose
format is `KEY=value`, one per line:

```env
OPENCAGE_API_KEY=your_key_here
# PLAYWRIGHT_EXECUTABLE_PATH=/usr/bin/chromium
```

After `enable --now`, check the health endpoint:

```bash
curl -s http://localhost:8010/api/scheduler
```

---

## 6. Memory, swap, and SD-card wear

Chromium plus a few MB of JSON is comfortable on 4 GB, tight on 2 GB. Before
the first real scrape, add a 2 GB swap file:

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h
```

(Raspberry Pi OS also ships `dphys-swapfile`; editing
`/etc/dphys-swapfile` and running `sudo systemctl restart dphys-swapfile`
is equivalent. Use one method, not both.)

Tips:

- Run **one scrape at a time**. The daily scheduler (08:00 Argentina time) and
  a manual run can overlap; avoid starting another while one is in progress.
- Zonaprop already blocks images/fonts in headless mode, which is the biggest
  saving.
- Keep `db.json` on an SSD rather than the SD card: the whole file is rewritten
  on every scrape, and SD cards wear out.
- If the Pi becomes unresponsive, cap the service in
  `~/.config/systemd/user/mudate.service` by uncommenting `MemoryMax=3G`, then
  `systemctl --user daemon-reload && systemctl --user restart mudate`.

---

## 7. Reach it from other devices

On the LAN:

```text
http://raspberrypi.local:8010
```

or use the address printed by `hostname -I`.

For access outside your network, prefer a private mesh such as Tailscale over
opening a port:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
# then browse to http://<pi-name>:8010 from any device in your tailnet
```

> **Mudate has no authentication** — usernames only separate data. Do not
> port-forward it to the public internet. If you need a public URL, put Caddy
> or nginx in front with HTTPS and basic auth.

---

## 8. Updating

```bash
cd ~/mudate
git pull

cd backend
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium   # only needed when Playwright was upgraded

systemctl --user restart mudate
```

Back up `~/mudate/data/db.json` and its sibling `db.json.geocache.json`
(or use **⋯ → Export DB** in the UI) before upgrades.

---

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| `Executable doesn't exist at .../ms-playwright/...` | Run `.venv/bin/playwright install chromium` as the **same user** that runs the service, not with `sudo` (sudo puts the browser in `/root/.cache`). |
| `Host system is missing dependencies` | `sudo .venv/bin/playwright install-deps chromium` |
| `Missing X server or $DISPLAY` | `PLAYWRIGHT_HEADLESS=1` is not set in the service environment. |
| `error while loading shared libraries: libasound.so.2` | Run `install-deps` (step 3). On Trixie the package is `libasound2t64`. |
| `No matching distribution found for playwright` / browser 404 | `pip` may succeed on a 32-bit OS but the browser cannot run. Check `uname -m` is `aarch64`; if it is, use the `/usr/bin/chromium` fallback from step 3. |
| Scrape says the portal blocked it | Delete the headless profile and retry: `rm -rf ~/.mudate_browser_headless`, then restart the service. |
| Service dies right after boot | `journalctl --user -u mudate -n 100 --no-pager`; usually a wrong path, missing `data/` directory, or no linger enabled. |
| `playwright: command not found` | Use the full path: `.venv/bin/playwright`. |
| Geocoding stalls | Nominatim is rate-limited (~1 req/s). Add `OPENCAGE_API_KEY` for a fallback. |
| Pi runs out of memory mid-scrape | Add swap (step 6), cap `MemoryMax`, or run fewer sessions. |

---

## 10. Docker alternative

If you prefer containers, the existing `Dockerfile` builds on arm64 and sets
headless mode automatically (it detects `/.dockerenv`):

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"   # log out and back in
git clone https://github.com/YOUR_USERNAME/mudate.git ~/mudate
cd ~/mudate

# optional but recommended
printf 'OPENCAGE_API_KEY=%s\n' 'your_key_here' > .env

docker compose up -d --build
docker compose logs -f
```

Notes:

- The first build compiles nothing but downloads Python packages, OS
  libraries, and Chromium: expect **20–40 minutes** on a Pi and ~2 GB of disk.
- `restart: unless-stopped` already starts the container at boot, so no
  systemd unit is needed.
- Data lives in `~/mudate/data/db.json` on the host.
- Do not run the Docker container and the native service at the same time:
  two writers on one `db.json` will conflict.

---

## Quick copy-paste summary

```bash
# 1. packages
sudo apt update && sudo apt install -y git python3 python3-venv python3-pip

# 2. project + venv
git clone https://github.com/YOUR_USERNAME/mudate.git ~/mudate
cd ~/mudate/backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. browser
.venv/bin/playwright install chromium
sudo .venv/bin/playwright install-deps chromium

# 4. data dir + smoke test
mkdir -p ~/mudate/data
PLAYWRIGHT_HEADLESS=1 DB_PATH=$HOME/mudate/data/db.json \
  .venv/bin/uvicorn main:app --host 0.0.0.0 --port 8010

# 5. service
mkdir -p ~/.config/systemd/user
cp ~/mudate/deploy/mudate.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now mudate
sudo loginctl enable-linger "$USER"
```
