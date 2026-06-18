# Grantgunner — System Map, Anti-Detection Posture & Operations Runbook

> ⚠️ **READ THIS FIRST.** This kit drives **real LinkedIn accounts**. LinkedIn actively detects and bans automation. One careless change — an un-stealthed browser, a uniform/too-fast delay, a reset cap, or acting on a flagged session — can get a real person's account **permanently restricted**. **Every** change that touches the browser, login, pacing, caps, message generation, or account lifecycle **must preserve the anti-detection invariants in §3.** When unsure: do less, reuse the existing stealthed paths, and ask before shipping.

_Last mapped: 2026-06-15, live branch `overhaul/grantgunner-hardening`. File:line refs are from that snapshot — grep to confirm before relying on exact lines._

## 1. What it is & where it runs
- **Grantgunner** = fork of `eracle/OpenOutreach`: Django + Django-admin + a single-page dashboard, plus Playwright browser automation driving real LinkedIn accounts (Voyager API + headed browser). LinkedIn mechanics live in the external `linkedin_cli` package; CRM/campaign/scheduling logic lives in this repo.
- **Live box:** `fedora-badlaptop` (tailnet `100.79.139.20`), ssh user `linkedinautomation`, branch `overhaul/grantgunner-hardening`. ⚠️ This branch is **only on badlaptop**, with **uncommitted WIP** — not on GitHub or the asus dev box. Edits to the live app happen directly in `~/OpenOutreach` on badlaptop.
- **Runtime:** rootless **podman** — `oo-web` (Django `runserver 0.0.0.0:8000`, bound to the Tailscale IP) + `oo-worker` (`manage.py run_worker`, the single process that owns the browsers). Repo bind-mounted into both.
- **Dashboard:** `http://100.79.139.20:8000/` (plain HTTP, raw tailnet IP — NOT https, NOT localhost). Login `archie` / `Grantgunner2026`.

## 2. How it works (subsystem map)

### 2.1 An "account" = `LinkedInProfile`
- `linkedin/models.py:212`, one-to-one with an auto-created staff Django `User`. Holds: creds (`linkedin_username`, `linkedin_password` — **plaintext**), optional `totp_secret` (**plaintext**), `cookie_data` (the persisted Playwright session), all per-account caps/schedule/randomization, and a small lifecycle cluster: `active`, `auto_paused_at`, `verify_requested` + `last_verify_ok/last_verify_error/last_verified_at`.
- There is **no** "status" enum and **no** "needs login" flag — "needs login" is *inferred* from `cookie_data` empty + `last_verify_ok=False`, shown as a red badge string.
- The codebase is **soft-delete everywhere** (LeadList/Sequence/Campaign archive; several models documented "never deleted"). `LinkedInProfile` itself has only an `active` on/off toggle — no delete/archive path.

### 2.2 The worker (`manage.py run_worker`, `run_worker.py`)
- A single in-process loop (~120s/cycle). Each cycle: (1) drains "Test connection" jobs (runs anytime, even off-hours), (2) enrolls leads, (3) for each **active, not-auto-paused, in-session** account, acquires its **one** browser session and runs its due steps; plus inbox polling + heavy enrichment on the default account.
- **Single session per account:** an in-process registry dict keyed by profile pk (`registry.py:8`). Only the worker drives browsers — the dashboard never does.
- A 401 / restriction anywhere → `auto_pause()` → stop touching the account + Slack alert (once).

### 2.3 Login / session
- Login is **worker-driven & lazy**, never from the dashboard. On first use the worker restores `cookie_data`, or runs a fresh login:
  - **With a TOTP secret** → fully automatic: `login_with_totp` (`auth/login.py:69`) types creds + auto-fills the RFC-6238 2FA code, ticks "remember device", saves cookies.
  - **Without TOTP** → `linkedin_cli.authenticate` — **human-in-the-loop**, blocks at the 2FA/checkpoint waiting for a person at the headed browser.
- "Test connection" (`api_account_verify`) only sets `verify_requested`; the worker runs `verify_account` (`browser/launch.py:173`), which uses **cookies-or-TOTP only and never opens an interactive login**.

### 2.4 Campaigns & sending
- `Campaign.sending_accounts` (M2M) = which accounts send a campaign. `select_account` (`limits.py:339`) round-robins the least-recently-used **active** account with capacity; `LeadCampaignState.sending_account` makes the assignment **sticky per lead** — a lead's whole sequence stays on the one identity that made its connection.

### 2.5 The "needs login" dead-end
- An account with **password but no TOTP and no cookies** (e.g. `tmwclaxton`) cannot be logged in remotely or automatically: the worker *refuses* it ("no saved session and no TOTP secret — needs a one-time manual login", `run_worker.py:51`), `verify_account` refuses interactive login, and the live worker runs headed on `DISPLAY=:0` with **no VNC** wired, so the only manual path (noVNC at `:6080`) is unreachable remotely. **Fix:** add a TOTP secret (→ automatic stealthed login), or do a one-time interactive login at the machine.

## 3. ⚠️ ANTI-DETECTION POSTURE — invariants that must NEVER be weakened

This is the heart of the kit. The mechanisms below exist deliberately; **do not remove, loosen, or bypass any of them**, and route every new browser/login/action through the existing stealthed paths.

**Volume caps**
- Per-action daily caps; default connect=25 ("ban-safe target"), message=50, inmail=5, visit/like=100 (`models.py:19`, `limits.py:20/83`).
- Randomized daily connect cap in `[min,max]`, deterministic per (account,day) so volume varies but is restart-stable (`limits.py:25`).
- Weekly connect ceiling ~100/wk (LinkedIn invite limit), freezes until Monday (`models.py:233`, `limits.py:76`).
- Monthly InMail allowance (`limits.py:102`).

**Timing & pacing**
- Per-account send window + weekdays + bank-holiday skip (`limits.py:150/135`).
- Randomized daily session window (opens a random minute in the first 30 of the window, closes a random minute in the last 30; stable per account/day) — `in_session()` gates **all** activity; nothing runs overnight; browser closed off-hours (`limits.py:163`, `run_worker.py:118`).
- Paced "drip": each action's daily cap spread evenly across the window with **wide clamped Gaussian (NOT uniform)** jitter + a min-gap floor — LinkedIn flags flat/uniform inter-action distributions (`limits.py:285-329`).
- "Wait N days" = N **working** days at a random time, never exactly 24h (`limits.py:241`).

**Browser fingerprint (`browser/launch.py`)**
- **Headed only** (never headless). Drops `--enable-automation` + `AutomationControlled` (kills `navigator.webdriver`/infobar). `playwright_stealth` applied, THEN `_COHERENCE_JS` re-aligns stealth's incoherent Win32/en-US/Mac lies back to coherent **Linux / en-GB / real Intel WebGL** so LinkedIn's lie-detectors don't trip. Real-GPU ANGLE flags (not SwiftShader). Locale en-GB + account timezone + fixed viewport. **Deliberately does NOT spoof the User-Agent** (a UA disagreeing with the real engine is itself a signal).
- Voyager header mimicry: captures LinkedIn's own `x-li-track`/`x-li-page-instance` and replays them on API calls (`session.py:80`, `launch.py:233`).

**Human behaviour (`browser/humanize.py`)**
- Bezier-curve mouse paths, read-scroll + dwell before actions, **log-normal** per-keystroke typing with occasional typo+backspace (NOT uniform). Spintax message variation so each recipient gets a structurally different message (`executor.py:630`).

**Session hygiene & flagged-account safety**
- One session per account (`registry.py`); never two browsers on one `li_at`.
- HTTP-200 **restriction/checkpoint detection** (`session.py:28/128`) → raises → **auto-pause** (`run_worker.py:57`): stop all activity, Slack-alert once, exclude from scheduling; cleared ONLY by a passing Test connection.
- Re-auth cooldown 1800s so a failing account isn't login-hammered (`run_worker.py:40`).
- Reply-before-send (`executor.py:473`); connection-provenance guard — only messages connections the kit itself made (`executor.py:440`); claim-lease/double-send guard (`executor.py:170`).

**Rules for ANY future code**
1. Never open a browser for a LinkedIn account except via `start_browser_session`/`_launch_fingerprinted` (stealth + coherence). No ad-hoc Playwright.
2. Never bypass `in_session`, the caps (`has_capacity`/`cap_for`), or `next_action_at` pacing. No uniform/fixed delays.
3. Never act on an `auto_paused_at` account, and never auto-clear `auto_paused_at` on a mere login — a human must resolve the restriction in LinkedIn first.
4. One live session per account pk; respect the re-auth cooldown.
5. Never reset/zero an identity's accrued daily/weekly/monthly counters (a removed-then-readded account must not get fresh headroom → instant burst). Prefer `active=False` soft-disable over delete.
6. Never migrate a lead mid-sequence to a different sending account (cross-identity correlation signal).
7. First login from this infra is the riskiest moment — expect a challenge; prefer TOTP so it's automatic and clean.

## 4. Operations runbook

### 4.1 Connect via SSH (from asus)
- badlaptop uses **Tailscale SSH** (no traditional sshd on :22) — connect by tailnet identity, no password:
  `tailscale ssh linkedinautomation@fedora-badlaptop`  (or `root@…`).
- ⚠️ **asus route gotcha:** if `ssh`/`curl` to `100.x` time out while `tailscale ping` works, asus's tailscaled dropped its peer routes. Fix: `sudo systemctl restart tailscaled` (repopulates routing table 52 in ~2s). Symptom: `ip route get 100.79.139.20` shows `via 192.168.1.254 dev wlp5s0` instead of `dev tailscale0`.

### 4.2 Dashboard
- `http://100.79.139.20:8000/` (http, raw IP). Login `archie` / `Grantgunner2026`.

### 4.3 Log an account in
- **Has authenticator (TOTP) 2FA:** put the base32 secret in the account's Security block (sets `totp_secret`), then hit **Test connection** → the worker logs in automatically via the stealthed `login_with_totp` and saves the session. Lowest risk.
- **SMS/email 2FA (no TOTP):** needs a one-time interactive login at the box's screen (`DISPLAY :0`) — no remote/automatic path today.
- **Dead-end:** password but no TOTP and no cookies → "No saved session — connect the account…". The worker won't touch it until logged in.

### 4.4 Add an account to a campaign
- Campaign editor → "Sending accounts" checkboxes (`Campaign.sending_accounts`). An account only actually sends once it is **active AND logged in** (has a session).

### 4.5 Deploy / restart (live branch, no CI)
- Edit `~/OpenOutreach` on badlaptop directly. `podman restart oo-web` for template/view changes; restart `oo-worker` too for worker/executor/browser changes. Migrations: `podman exec oo-web python manage.py migrate`.

## 5. Known gaps / risks
- No remove-account UI (only `active` toggle); the Django admin CAN hard-delete a `LinkedInProfile` and would cascade-destroy ActionLog + AccountDailyCounter (audit + cap counters) and orphan in-flight leads — avoid; use soft-disable.
- No in-dashboard interactive login → no-TOTP accounts can't be logged in remotely.
- `linkedin_password` and `totp_secret` are stored **plaintext** (encrypt-at-rest is a TODO).
- Live branch is uncommitted WIP only on badlaptop — back it up / commit.
