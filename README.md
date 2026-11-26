# parcel-sync

Reads shipping notices from Gmail (read-only) and adds their tracking numbers to [Parcel](https://parcelapp.net) through Parcel's API. Runs every 30 minutes, either in a Docker container or as a systemd timer.

- Finds tracking numbers in carrier links (USPS, UPS, FedEx, DHL), tracking-page links (Narvar, AfterShip, Shopify and similar), well-known number formats, and "Tracking Number: X" labels.
- Mail it can't resolve goes to `needs_manual.log` with a Gmail link, so you can add it by hand.
- Adds at most 15 deliveries per rolling 24 hours (Parcel's API limit is 20 per day, failures included).
- Skips Amazon. Use Parcel's built-in Amazon integration for that.
- Only needs the `gmail.readonly` scope. It never sends, deletes, or modifies mail.

## Requirements

- **Parcel Premium.** The Parcel API is a Premium feature.
- **Somewhere to run it**, one of:
  - **Docker** (with Compose) on any always-on machine: [Option 1](#option-1-docker). Easiest.
  - **A Linux server with systemd**, Python 3.9+ and `python3-venv`: [Option 2](#option-2-systemd).
  - Or just try it on your own machine first: [Quick try](#quick-try-on-your-own-machine).
- **git**, to clone this repo.
- **A Google account** with the Gmail you want to read, and access to the Google Cloud Console (free; no billing needed).

## How it fits together

Two different secrets, don't mix them up:

| Secret | File | Where it comes from |
|---|---|---|
| Google OAuth client | `credentials.json` | You create it in Google Cloud (step A6) |
| Gmail token | `token.json` | Written by the one-time sign-in (D5 or S7) and refreshed automatically |
| Parcel API key | `PARCEL_API_KEY` env var | Parcel's web app (see [Get your Parcel API key](#get-your-parcel-api-key)) |

`credentials.json` and `token.json` live in a data folder outside the code (a mounted volume for Docker), and the Parcel key is passed in as an environment variable. All of them are gitignored and excluded from the Docker build, so nothing secret ends up in the repo or the image.

In the steps below, **laptop** is the computer with your web browser, and **server** is the machine that will run parcel-sync. Replace `server` in commands with its SSH hostname or `user@ip`. If they are the same machine, skip the `scp`/`ssh` parts.

---

## A. Google Cloud setup (laptop, browser)

**A1. Create the project and select it**
1. Open https://console.cloud.google.com
2. Click the project picker at the top left (next to "Google Cloud") > **New project**. Name: `parcel-sync`. Click **Create**.
3. Open the project picker again and click `parcel-sync`.

You should see `parcel-sync` in the top bar. Every step in part A happens in this project, so check the top bar before each one.

**A2. Enable the Gmail API**
1. Menu > **APIs & Services** > **Library**. Search for `Gmail API` and click it.
2. Click **Enable**.

You should see the Gmail API page with a **Manage** button and "API Enabled".

**A3. Create the app (Google Auth Platform)**
1. Menu > **Google Auth Platform** (the old "OAuth consent screen" link opens it too) > **Overview** > **Get started**.
2. App name: `parcel-sync`. User support email: your Gmail address. Click **Next**.
3. Audience: **External**. Click **Next**.
4. Contact information: your Gmail address. Click **Next**, tick the agreement, then **Continue** > **Create**.

You should see the Overview page without the "Get started" button. The left menu shows Branding, Audience, Clients and Data Access.

**A4. Add the read-only Gmail scope**
1. **Data Access** > **Add or remove scopes**.
2. Filter for `gmail.readonly` and tick `.../auth/gmail.readonly`. Click **Update**, then **Save**.

You should see `.../auth/gmail.readonly` under "Your restricted scopes" and nothing else.

**A5. Publish the app**
1. **Audience** > **Publish app** > **Confirm**.

You should see "Publishing status: In production". Don't skip this: while the app is in **Testing**, Google expires the Gmail token after 7 days and the script stops working.

You do not need to submit the app for Google verification. It's only for you, so you'll click through an "unverified app" warning once during sign-in.

**A6. Create the client and download it**
1. **Clients** > **Create client**. Application type: **Desktop app**. Name: `parcel-sync`. Click **Create**.
2. Click **Download JSON** in the dialog that appears.

You should see a file in your Downloads folder named `client_secret_<long id>.apps.googleusercontent.com.json`. Download it right away: Google may not let you view or download the secret again later. If you lose it, create a new client.

### Get your Parcel API key

1. Open https://web.parcelapp.net and sign in.
2. Open the **API** page (gear / settings menu > API) and copy your API key. If you don't see it, your account isn't on Premium.

Keep it handy for the install step. Don't paste it into any file inside the repo other than the gitignored `.env`.

---

## Option 1: Docker

**D1. Clone the repo (server)**

```bash
git clone https://github.com/tonhe/parcel-sync.git
cd parcel-sync
```

**D2. Put the Google client file in the data folder**

From your laptop:

```bash
ssh server mkdir -p parcel-sync/data
scp ~/Downloads/client_secret_*.json server:parcel-sync/data/credentials.json
```

On the server, in the `parcel-sync` folder:

```bash
chmod 700 data && chmod 600 data/credentials.json
```

The container runs as uid 1000. On Linux, if your user isn't uid 1000 (check with `id -u`), also run `sudo chown -R 1000:1000 data`. Docker Desktop on macOS/Windows doesn't need this.

**D3. Set the Parcel key**

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Replace `your-parcel-api-key` with your key. Compose reads `.env` automatically. Other ways to pass the key are under [Supplying the Parcel key](#supplying-the-parcel-key).

**D4. Build the image**

```bash
docker compose build
```

**D5. Gmail sign-in (one time)**

Google's sign-in ends by sending your browser to `http://localhost:8765`. If Docker runs on your laptop, that just works. If it runs on a server, first open a tunnel from your laptop and leave it open:

```bash
ssh -L 8765:localhost:8765 server
```

Then, on the server (inside that SSH session, in the `parcel-sync` folder):

```bash
docker compose run --rm -p 8765:8765 parcel-sync auth --no-browser
```

Open the printed `https://accounts.google.com/...` URL in your laptop's browser and finish the sign-in as in step S7c below (click **Advanced** > **Go to parcel-sync (unsafe)**, allow read access). You should see `Saved /data/token.json`, and `data/token.json` on the server.

**D6. Dry run**

```bash
docker compose run --rm parcel-sync run --dry-run
```

You should see `N candidate mails, N new` and `WOULD ADD ...` lines. Nothing is added to Parcel.

**D7. Start it**

```bash
docker compose up -d
docker compose logs -f
```

It runs immediately, then every 30 minutes (`PARCEL_SYNC_INTERVAL`, in seconds). `restart: unless-stopped` brings it back after reboots.

**Updating:** `git pull && docker compose up -d --build`

**Uninstalling:** `docker compose down`, delete the folder, then revoke access as in [Uninstalling](#uninstalling).

### Supplying the Parcel key

Pick one. Each keeps the key out of the repo and the image.

| Method | How |
|---|---|
| `.env` file (default) | `PARCEL_API_KEY=...` in `.env` next to `compose.yaml`. Gitignored. |
| Shell environment | `export PARCEL_API_KEY=...` before `docker compose up -d`. Nothing on disk, but you must set it again for every `up`. |
| Docker secret file | Put the key alone in `secrets/parcel_api_key` (gitignored, `chmod 600`), then use the compose snippet below. The key never shows up in `docker inspect`. |
| Plain `docker run` | `docker run -d --restart unless-stopped -e PARCEL_API_KEY=... -v "$PWD/data:/data" parcel-sync` (or `--env-file .env`). |

Docker secret version of `compose.yaml`:

```yaml
services:
  parcel-sync:
    build: .
    restart: unless-stopped
    environment:
      PARCEL_API_KEY_FILE: /run/secrets/parcel_api_key
    secrets:
      - parcel_api_key
    volumes:
      - ./data:/data
secrets:
  parcel_api_key:
    file: ./secrets/parcel_api_key
```

The Google files can also live elsewhere: mount the file anywhere and point `PARCEL_SYNC_CREDENTIALS` at it. `token.json` must stay in the writable `/data` volume, because it is refreshed automatically.

---

## Option 2: systemd

### S. Get the code onto the server (server, your sudo user)

**S1. Clone the repo**

```bash
sudo git clone https://github.com/tonhe/parcel-sync.git /opt/parcel-sync
```

**S2. Copy the Google client file over (laptop, Terminal)**

```bash
scp ~/Downloads/client_secret_*.json server:client_secret.json
```

You should see it land in your home directory on the server: `ssh server ls -l client_secret.json`.

---

### Install (server, your sudo user)

**S3. Service user**

```bash
sudo useradd --system --create-home --home-dir /home/parcelsync --shell /usr/sbin/nologin parcelsync
```

Check with `id parcelsync`. "already exists" is fine.

**S4. Python libraries**

```bash
sudo python3 -m venv /opt/parcel-sync/venv
sudo /opt/parcel-sync/venv/bin/pip install -r /opt/parcel-sync/requirements.txt
```

You should see `Successfully installed ... google-api-python-client ...` at the end. If venv says `ensurepip is not available`, run `sudo apt install python3-venv` and repeat.

**S5. Config folder and Google client file**

```bash
sudo install -d -o parcelsync -g parcelsync -m 700 /home/parcelsync/.config /home/parcelsync/.config/parcel-sync
sudo install -o parcelsync -g parcelsync -m 600 ~/client_secret.json /home/parcelsync/.config/parcel-sync/credentials.json
rm ~/client_secret.json
sudo ls -l /home/parcelsync/.config/parcel-sync/
```

You should see `credentials.json` as `-rw------- parcelsync parcelsync`.

**S6. Parcel API key**

Using the key from [Get your Parcel API key](#get-your-parcel-api-key), on the server:

```bash
sudo install -o parcelsync -g parcelsync -m 600 /opt/parcel-sync/parcel-sync.env.example /etc/parcel-sync.env
sudo nano /etc/parcel-sync.env
```

Replace `your-parcel-api-key` with your key: no quotes, no spaces around `=`. Save and exit (Ctrl+O, Enter, Ctrl+X).

Check: `sudo grep -c '^PARCEL_API_KEY=.' /etc/parcel-sync.env` prints `1`, and `sudo ls -l /etc/parcel-sync.env` shows `-rw------- parcelsync`.

---

### Gmail sign-in (laptop + server, one time)

Google's sign-in sends your browser back to `http://localhost:8765` at the end. The SSH tunnel makes your laptop's port 8765 reach the server, so the script on the server receives it.

**S7a. Laptop, Terminal:** open an SSH session that forwards port 8765, and leave it open.

```bash
ssh -L 8765:localhost:8765 server
```

**S7b. Server, inside that SSH session:**

```bash
sudo -u parcelsync env PARCEL_SYNC_DIR=/home/parcelsync/.config/parcel-sync \
  /opt/parcel-sync/venv/bin/python /opt/parcel-sync/parcel_sync.py auth --no-browser
```

It prints a long `https://accounts.google.com/...` URL and waits.

**S7c. Laptop, browser:** open that URL. Pick your Gmail account. On "Google hasn't verified this app", click **Advanced** > **Go to parcel-sync (unsafe)**. Allow read access, then click **Continue**.

You should see "The authentication flow has completed" in the browser, and `Saved /home/parcelsync/.config/parcel-sync/token.json` on the server. Lines like `channel 3: open failed: connect failed` in the laptop terminal are harmless.

**S7d. Server:**

```bash
sudo ls -l /home/parcelsync/.config/parcel-sync/
```

You should see `credentials.json` and `token.json`, both `-rw------- parcelsync`. You can close the tunnel now.

---

### Dry run (server)

```bash
sudo -u parcelsync bash -c 'set -a; . /etc/parcel-sync.env; set +a; /opt/parcel-sync/venv/bin/python /opt/parcel-sync/parcel_sync.py run --dry-run'
```

You should see `N candidate mails, N new`, followed by `WOULD ADD <carrier> <number>`, `MANUAL ...` and `DUP ...` lines. A dry run adds nothing to Parcel and saves no state, so you can repeat it. It does make one read-only call to Parcel's delivery list, which is limited to 20 calls an hour, so don't run it in a tight loop.

`0 candidate mails` just means nothing in the last 3 days matched the search. See `PARCEL_SYNC_QUERY` under [Settings](#settings).

---

### Turn it on (server)

```bash
sudo cp /opt/parcel-sync/parcel-sync.service /opt/parcel-sync/parcel-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now parcel-sync.timer
sudo systemctl start parcel-sync.service
sudo journalctl -u parcel-sync -n 50 --no-pager
```

You should see `ADD ...` lines in the journal, and a Parcel push notification for each new delivery. `systemctl list-timers parcel-sync.timer` shows the next run (within 30 minutes).

Done. To watch it later: `sudo journalctl -u parcel-sync -f`.

---

### Updating

```bash
sudo git -C /opt/parcel-sync pull
sudo /opt/parcel-sync/venv/bin/pip install -r /opt/parcel-sync/requirements.txt
sudo cp /opt/parcel-sync/parcel-sync.service /opt/parcel-sync/parcel-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

### Uninstalling

```bash
sudo systemctl disable --now parcel-sync.timer
sudo rm /etc/systemd/system/parcel-sync.service /etc/systemd/system/parcel-sync.timer /etc/parcel-sync.env
sudo systemctl daemon-reload
sudo rm -r /opt/parcel-sync
sudo userdel -r parcelsync
```

Then revoke Gmail access at https://myaccount.google.com/permissions (remove `parcel-sync`), and optionally delete the Google Cloud project.

## Quick try on your own machine

No server, no service user. Works on macOS or Linux, after part A:

```bash
git clone https://github.com/tonhe/parcel-sync.git && cd parcel-sync
python3 -m venv venv && venv/bin/pip install -r requirements.txt
mkdir -p ~/.config/parcel-sync && chmod 700 ~/.config/parcel-sync
mv ~/Downloads/client_secret_*.json ~/.config/parcel-sync/credentials.json
venv/bin/python parcel_sync.py auth            # opens your browser
venv/bin/python parcel_sync.py run --dry-run   # no Parcel key needed for a dry run
```

Without `PARCEL_API_KEY`, the dry run can't check what's already in Parcel, so it won't show `DUP` lines for those. To actually add deliveries: `PARCEL_API_KEY=... venv/bin/python parcel_sync.py run`.

---

## File map (systemd install)

| Path | Owner, mode | Holds |
|---|---|---|
| `/opt/parcel-sync/` | root | the repo: script, unit files, tests |
| `/opt/parcel-sync/venv/` | root | Python libraries |
| `/etc/parcel-sync.env` | parcelsync, 600 | `PARCEL_API_KEY` and optional settings |
| `/home/parcelsync/.config/parcel-sync/credentials.json` | parcelsync, 600 | Google OAuth client (from A6) |
| `/home/parcelsync/.config/parcel-sync/token.json` | parcelsync, 600 | Gmail refresh token (from S7), renewed automatically |
| `/home/parcelsync/.config/parcel-sync/state.json` | parcelsync, 600 | handled mail IDs, added numbers, add times (kept 60 days) |
| `/home/parcelsync/.config/parcel-sync/needs_manual.log` | parcelsync, 600 | mail with no number found, with Gmail links |
| `/etc/systemd/system/parcel-sync.service` / `.timer` | root, 644 | runs it every 30 minutes |

## File map (Docker install)

| Path (host) | In container | Holds |
|---|---|---|
| `parcel-sync/.env` | (environment) | `PARCEL_API_KEY` |
| `parcel-sync/data/credentials.json` | `/data/credentials.json` | Google OAuth client |
| `parcel-sync/data/token.json` | `/data/token.json` | Gmail refresh token |
| `parcel-sync/data/state.json`, `needs_manual.log` | `/data/...` | state and the manual-review log |

## Settings

For systemd, add these to `/etc/parcel-sync.env`. For Docker, add them under `environment:` in `compose.yaml` (or to `.env` and reference them there). They apply on the next run.

| Variable | Default | What |
|---|---|---|
| `PARCEL_SYNC_PUSH` | `1` | `0` turns off Parcel's push notification for each add |
| `PARCEL_SYNC_MAX_ADDS` | `15` | adds per rolling 24 hours (keep it under Parcel's 20) |
| `PARCEL_SYNC_QUERY` | shipping-related subjects, last 3 days, no Amazon or Promotions | Gmail search used to find candidate mail (same syntax as the Gmail search box) |
| `PARCEL_SYNC_RESOLVE_LINKS` | `0` | `1` follows "click here to track" redirect links when no number is found in the mail. Uses HEAD requests and reads only redirect headers, never page content |
| `PARCEL_SYNC_RESOLVE_HOSTS` | common click-trackers and carrier sites | comma-separated host allowlist for `PARCEL_SYNC_RESOLVE_LINKS` |
| `PARCEL_API_KEY_FILE` | unset | read the Parcel key from this file instead of `PARCEL_API_KEY` (Docker secrets) |
| `PARCEL_SYNC_CREDENTIALS` | `$PARCEL_SYNC_DIR/credentials.json` | path to the Google OAuth client file |
| `PARCEL_SYNC_INTERVAL` | `1800` | Docker only: seconds between runs |
| `PARCEL_SYNC_DIR` | `~/.config/parcel-sync` (`/data` in Docker) | config/state folder. The systemd unit and the image already set this |

## Privacy

- Gmail access is read-only and stays on your server. Nothing is sent anywhere except to Google (to read mail) and Parcel (to add deliveries).
- For each delivery, Parcel receives the tracking number, carrier, and a description made from the sender name and subject line (first 60 characters).
- With `PARCEL_SYNC_RESOLVE_LINKS=1`, the script also sends HEAD requests to tracking links on the allowlisted hosts. Those can count as "clicks" for the sender's analytics.

## Troubleshooting

- **`403 accessNotConfigured` / "Gmail API has not been used in project ..."**: the Gmail API is off in the project that owns your client. Do A2 with that project selected (the number in the error is the project). Wait 2 to 5 minutes, then rerun the dry run.
- **`Address already in use` during sign-in**: something else is on port 8765. Add `--port 8766` to the `auth` command, and use `8766` in the `ssh -L` tunnel (and `-p 8766:8766` for Docker).
- **Docker: `/data is not writable by uid 1000`**: the `data` folder on the host is owned by another user. Run `sudo chown -R 1000:1000 data`.
- **Browser shows "This site can't be reached" at `localhost:8765` during sign-in**: the SSH tunnel isn't open, the `auth` command was started outside that SSH session, or (Docker) you left off `-p 8765:8765`.
- **`channel N: open failed: connect failed: Connection refused`** in the laptop terminal during sign-in: harmless. The browser retries after the sign-in server has stopped.
- **`invalid_grant` / "Token has been expired or revoked"**, often 7 days after sign-in: the app is still in Testing, or access was revoked. Do A5, then delete `token.json` (systemd: `/home/parcelsync/.config/parcel-sync/token.json`; Docker: `data/token.json`) and sign in again.
- **`No .../token.json. Run parcel_sync.py auth first`**: the run looked in the wrong folder or sign-in never finished. Check `sudo ls /home/parcelsync/.config/parcel-sync/`, and sign in again if `token.json` is missing.
- **`Put your OAuth client file at ...` during sign-in**: `credentials.json` isn't where it should be. Redo S5 (systemd) or D2 (Docker).
- **`PARCEL_API_KEY is not set`**: the key wasn't passed in. systemd: check S6 and use the exact dry-run command. Docker: check `.env` is next to `compose.yaml`.
- **`FAIL ... ` with an error from Parcel**: Parcel rejected that number (bad format or unsupported carrier). It won't be retried. Add it by hand if it's real.
- **`daily cap of 15 reached`**: normal on a busy day. The rest is added on later runs.
- **A shipment never showed up**: look in `needs_manual.log` (in the data folder) and add it by hand. If a sender's format keeps getting missed, save the email body and test it with `parse` (below).

## Development

Run the parser tests (no dependencies needed):

```bash
python3 -m unittest discover -s tests -v
```

Test the parser against a saved email (`.txt` or `.html`):

```bash
python3 parcel_sync.py parse saved_email.html
```
