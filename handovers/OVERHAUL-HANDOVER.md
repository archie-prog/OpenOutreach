# Grantgunner overhaul — handover & build plan

**Audience:** the next Opus session continuing this work.
**Written:** 2026-06-11, after a hardening + dead-code + 2FA + commercial-pages pass.
**Branch:** `overhaul/grantgunner-hardening` (off `feat/dashboard-spa`) on **badlaptop**.

This file tells you (a) exactly where things stand, (b) how to work on this system,
(c) what is left to build and how. Read it top to bottom before touching anything.

---

## 0. TL;DR — what to do next (in order)

1. **Re-authenticate the LinkedIn account** (see §4 — this is the #1 live blocker; the
   worker is stopped because its session 401s). Nothing in the campaign engine can run
   until this is fixed. Best fix: set the account's **TOTP secret** in the dashboard
   Accounts tab, then restart `oo-worker`.
2. **Commit + deploy the commercial-pages work** if not already (see §2 — it's finished
   and verified in the mirror, may already be committed by the time you read this).
3. **Drop the empty legacy tables** (task in §5.A) — Task, SearchKeyword, Deal,
   ChatMessage, unused Campaign columns. All 0 rows; needs migrations across two apps.
4. **Work the remaining audit findings** worth fixing (§6).
5. **Update CLAUDE.md / ARCHITECTURE.md** to match the new (post-dead-code) reality, then
   **push to GitHub** (badlaptop has no push creds — see §3).

---

## 1. Where everything lives

### The live machine — badlaptop
- Host: `fedora-badlaptop`, tailnet `100.79.139.20`. SSH: `ssh linkedinautomation@fedora-badlaptop`
  (Tailscale SSH — **no key needed**; the user is `linkedinautomation`, NOT `archie`).
- Repo: `~/OpenOutreach`, branch `overhaul/grantgunner-hardening`.
- Runs as **rootless podman**, two containers, both `restart=always`:
  - `oo-web` → `python manage.py runserver 0.0.0.0:8000` (dashboard + admin), bind-mounts the repo at `/app`.
  - `oo-worker` → `bash -lc 'rm -f /tmp/.X99-lock; Xvfb :99 …; DISPLAY=:99 python manage.py run_worker --interval 90'`
    — **currently STOPPED by me** (see §4).
- DB: **SQLite** in the `openoutreach-data` podman volume → `/app/data/db.sqlite3`.
- Dashboard: **http://100.79.139.20:8000/** — login **`archie` / `Grantgunner2026`**.
  New routes: `/` welcome, `/login/` branded login, `/dashboard/` app, `/admin/` Django admin.
- VNC into the worker's browser (to clear a LinkedIn checkpoint by hand): the worker container
  runs Xvfb on `:99`; `local.yml` exposes noVNC on `6080` / VNC on `5900` for the compose `app`
  service. For the live `oo-worker` container check `podman port oo-worker`.

### The editing surface — this Mac
- **Mirror** (where you edit, with full Read/Edit/Write tooling): `~/OpenOutreach-badlaptop/mirror`.
- **Backups** (full, pre-overhaul): `~/OpenOutreach-badlaptop/backups/20260609-232550/`
  (also on the box at `~/backups/20260609-232550/`) — repo tar, git bundle, **sqlite snapshot**,
  and the data-volume tar (cookies + db). Restore from here if anything goes wrong.
- **Audit** (raw findings, 225 items, 10 areas): `~/OpenOutreach-badlaptop/audit/audit-raw.json`
  (+ `audit-raw-run1.json`). Parse with `python3 -c "import json; d=json.load(open(...))['result']"`.

---

## 2. What was DONE this pass (commits on the branch)

Three commits (all **local on badlaptop — not pushed**, see §3):

- **`ba0fbcd`** — *Fix silent-failure bugs + reliable LinkedIn 2FA.* The big correctness pass.
- **`b793fc2`** — *Remove dead upstream code* (~7.1k lines, 59 files).
- **(commercial pages)** — welcome/login/settings/guide. May be committed by the time you read
  this; if `git status` shows them uncommitted, commit them (see message at the end of §2).

### 2.1 Correctness fixes (already done — do NOT redo)
- **Frontend `j()` fetch helper** (`dashboard.html`): was `fetch().then(r=>r.json())` with zero
  error handling → any 500 / expired session killed the whole tab silently (the user's #1
  complaint). Now surfaces every failure as a **toast**, detects expired-session redirects, sends
  **CSRF token**, and a global `unhandledrejection` backstop. Mutating endpoints are now CSRF-
  protected (`ensure_csrf_cookie` on the page; the blanket `@csrf_exempt` was removed from ~20 views).
- **Stored-XSS**: lead-controlled strings (LinkedIn names/titles/companies/messages) are now
  `esc()`-escaped in every innerHTML sink (Unibox, leads tables, activity, responses).
- **cap_for() zero-stall** (`accounts/limits.py`): a truncated `daily_caps_json` (missing
  inmail/profile_visit/like_post) used to make those caps **0**, silently stalling those steps
  forever. Now falls back to per-action defaults. **This was actively affecting the live account.**
- **Account save** (`api_account_update`): safe int parsing (a cleared field no longer 500s);
  never drops the unexposed cap keys; **editable/clearable TOTP secret + login rotation** added.
- **`awaiting_decision` persistence** (`sequences/executor.py`): the connect-decision phase set it
  `False` in memory but never saved it → stuck `True` forever, breaking any later connect step and
  bypassing its cap gate. Now persisted.
- **`due_states` campaign gate**: only runs leads whose **campaign is ACTIVE** — adding leads to a
  draft/paused campaign no longer starts outreach; pausing halts states created after the pause.
- **`like_post`** only logs (consumes cap / inflates KPI) when the like **actually succeeded**.
- **`render_template`**: now handles `{{jinja}}` AND `{single}` braces, and leaves unknown `{tags}`
  literal instead of blanking them.
- **Connection note**: a note typed in the Flows builder was silently dropped. Wired a real
  app-side **connect-with-note** Playwright flow (`actions/connect_note.py`), used when a note is
  set, falling back to the note-less verb. ⚠️ Like `inmail.py`/`like.py`, the selectors are
  defensively written but **need live verification** (couldn't test without a real connect request).
- **Message step** "if replied" branch removed from the builder (replies stop the sequence by
  design — the engine continues message steps down the FAILURE branch, now labelled "then (next step)").
- **delete-step** re-points in-flight leads onto the spliced successor (was `SET_NULL` → silently COMPLETED).
- **`next_send_time`** handles `send_end_hour=24` (was `ValueError` → bricked leads as STOPPED_ERROR).
- **Manual Unibox sends**: now **accounted** (ActionLog + daily counter → show in activity/KPIs),
  **bounded retries** (3, via new `Message.send_attempts`/`send_error`), **reconciled** against the
  poller's re-fetch (no more duplicate row), and the UI shows a **queued / failed** pill instead of
  a fake "sent". `connections_accepted` KPI now respects the period/account filters.
- **`poll_replies` rotation**: was `order_by(-last_action_at)[:limit]` → completed/stopped leads
  (frozen `last_action_at`) were never re-polled, so replies after a sequence finished never synced.
  Now rotates by **least-recently-polled**.
- **Worker** (`run_worker.py`): full tracebacks (was a 200-char repr that hid the cause), an
  **AuthenticationError → reauthenticate()** hook **throttled to once/30 min** (never hammer logins
  on a real account), and failure backoff.
- New regression tests: `tests/tasks/test_overhaul_fixes.py`, rewritten `tests/auth/test_totp.py`.

### 2.2 LinkedIn 2FA (already done — task complete)
- `auth/totp.py` (RFC-6238, was already there) + **rewritten `auth/login.py`**: waits for
  navigation **before** reading the URL (the old code read `page.url` synchronously after clicking
  Sign in, so the challenge was never detected live — classic "works in the mock, not in prod"),
  verifies success, ticks "remember this device", retries on an expired code, raises
  `TwoFactorLoginError` on failure so a half-auth cookie state is never saved.
- `browser/launch.py` + `browser/session.py`: cookie-expiry and saved-session-invalid paths now
  re-login through the TOTP flow (were dead-ending / relaunching with stale cookies).
- Dashboard Accounts tab: add/edit/clear the TOTP secret + rotate the LinkedIn email/password.

### 2.3 Dead code removed (already done)
~7.1k lines. Everything reachable only via the old `manage.py rundaemon` daemon: `daemon.py`,
`tasks/`, the AI-discovery `pipeline/` (search/qualify/ready_pool/pools/freemium_pool — **kept
`search_keywords.py`**, it's live), `ml/qualifier.py`+`hub.py` (**kept** embeddings/profile_text/
lead_score), `agents/`, `db/{summaries,chat,deals}.py`, `vendor/` (mem0), `onboarding*.py`, `api/`
(newsletter), `setup/{freemium,gdpr,seeds}.py`, `diagnostics.py`, `logging.py`, the
rundaemon/onboard/add_seeds commands, the legacy prompt templates, and ~170 legacy-only tests.
Entry points repointed off `rundaemon` (`manage.py`, `Makefile`, `compose/linkedin/start`).
**Models were intentionally KEPT** (see §5.A — empty-table drops are a separate migration step).

### 2.4 Commercial pages (DONE this pass, verified live)
- `linkedin/urls.py`: `/` welcome, `/login/` (branded `LoginView`), `/logout/`.
- `django_settings.py`: `LOGIN_URL=/login/`, `LOGIN_REDIRECT_URL=/dashboard/`, `LOGOUT_REDIRECT_URL=/`.
- `templates/welcome.html` (branded marketing landing) + `templates/registration/login.html` (branded login).
- `dashboard_page` now `@staff_member_required(login_url="/login/")` (bounces to branded login).
- New dashboard tabs: **Settings** (workspace name + plan/billing card, Stripe-portal deep-link
  stub, this-month usage) and **Guide** (a 6-step how-to-use walkthrough with live counts + a
  responsible-use note). Header shows the workspace name + a **Sign out** link.
- Backend: `SiteConfig` gained `workspace_name`, `billing_email`, `plan`, `billing_status`,
  `stripe_customer_id`, `stripe_portal_url` (migration `0031`). New endpoints `api/settings/`,
  `api/settings/save/`, `api/overview/`.
- **Verified live**: `/`→200, `/login/`→200, `/dashboard/`→302→`/login/`, branded login POST→302,
  then `/dashboard/`, `api/settings/`, `api/overview/` all 200 with real data.

Commit message to use if uncommitted (CLAUDE.md rule: **single-line, no `Co-Authored-By`**):
`Commercial pages: branded welcome + login, Settings (workspace/billing), Guide tab`

---

## 3. How to work on this system (the loop)

**Edit in the mirror → rsync to the box → test in the dev container → restart containers.**

```bash
# 1. EDIT files under ~/OpenOutreach-badlaptop/mirror  (use Read/Edit/Write)

# 2. SYNC to the box (NB: --delete needs the box's __pycache__ cleared first or it
#    leaves empty dirs; exclude pycache on transfer):
rsync -a --exclude='__pycache__' --exclude='*.pyc' \
  ~/OpenOutreach-badlaptop/mirror/linkedin/ linkedinautomation@fedora-badlaptop:OpenOutreach/linkedin/
# (also sync tests/, and root files manage.py/Makefile/compose/linkedin/start as needed)

# 3. The dev image is `openoutreach-dev` = prod image + pytest (built on the box).
#    If it's gone, rebuild:  FROM ghcr.io/eracle/openoutreach:latest + `pip install pytest
#    pytest-django pytest-mock pytest-cov factory-boy`.

# 4. makemigrations / migrate / check / test all run via that image:
ssh linkedinautomation@fedora-badlaptop 'cd ~/OpenOutreach && \
  podman run --rm -v "$PWD":/app:z --userns=keep-id --user $(id -u):$(id -g) \
    -e FASTEMBED_CACHE_DIR=/tmp/fe -e DJANGO_SETTINGS_MODULE=linkedin.django_settings \
    --entrypoint python openoutreach-dev -m pytest tests/ -q'
# Baseline after this pass: ~197 passing, 9 skipped (down from 362 only because ~170
# legacy tests were deleted with the dead code).

# 5. JS sanity (no node on the host — use Playwright's bundled node):
#    extract <script> blocks → /tmp/dash.js, then:
#    --entrypoint /usr/local/lib/python3.12/site-packages/playwright/driver/node openoutreach-dev --check /tmp/dash.js

# 6. DEPLOY: pull migrated files into the live containers (they bind-mount the repo, so
#    a sync is enough) then:
ssh linkedinautomation@fedora-badlaptop 'cd ~/OpenOutreach && \
  podman exec oo-web python manage.py migrate linkedin && \
  podman restart oo-web'        # + oo-worker if executor/poller/worker/limits changed
```
- Template/JS-only change → restart `oo-web` only. Backend change → restart both.
- **CLAUDE.md rules (enforce):** use `.venv/bin/python` locally; **single-line commit messages, no
  `Co-Authored-By`**; deps in `requirements/*.txt`; **no auto-memory system**; crash on unexpected
  errors (try/except only for expected ones); no Python back-compat shims (rename/delete freely, but
  DB schema changes go through migrations).

### Git / pushing
- badlaptop has **no GitHub push creds** (`git push` fails: no username) and **no `gh`**. `origin` =
  `github.com/archie-prog/OpenOutreach` (a public fork). `asus-fedora` (the documented dev box) has
  **sshd down** (port 22 refused) so you can't reach it either.
- → Either ask the user to push from a machine that has creds, or have them add a token/`gh` to
  badlaptop. Until then, **commits are local on the branch** — the backup git bundle is the safety net.

---

## 4. ⚠️ CRITICAL live issue — the LinkedIn account is logged out

The worker was 401-ing **every cycle**: `AuthenticationError('Messaging API 401 (fetch_conversations)')`.
The saved cookies for **`aawilding@gmail.com`** are stale/invalid. Consequences seen live:
- **138 leads are already `stopped_error`** (the executor used to brick a lead on any transient
  error — partly addressed, but the backlog is there).
- Campaign **id 8** (`GrantGunner Investors - Marketing + Grantwriters`) is **active with ~296
  leads** (158 active, 80 archived, 3 paused, 2 completed).

**I STOPPED `oo-worker`** to avoid hammering logins on a real account (my new reauth hook would
otherwise try a fresh login each cycle; the account has **no stored TOTP**, so an unattended login
can't clear a 2FA/checkpoint). The web (`oo-web`) is still up.

**To recover (user-assisted):**
1. Preferred — **set the account's TOTP secret**: in LinkedIn, add an authenticator app to
   `aawilding@gmail.com`, copy the base32 secret, paste it in the dashboard **Accounts** tab (the
   new TOTP field). Then `podman start oo-worker`. The rewritten login flow will sign in unattended.
2. Or — **clear the checkpoint by hand**: `podman start oo-worker`, VNC into the worker's Xvfb
   display, complete the LinkedIn login/checkpoint once; the cookies get saved and the worker proceeds.
3. **Before un-pausing real outreach:** the live account should only message/connect-test the
   **authorised contacts** — *Toby Claxton, Joshua Young, Jess McAllister*. Everyone else in campaign
   8 is real outreach; consider pausing campaign 8 until the user confirms it should run.
- Consider a follow-up: when reauth fails, the worker could auto-**pause** active campaigns + raise
  a dashboard banner, instead of leaving leads to accumulate `stopped_error`.

---

## 5. What's LEFT to build — plan

### 5.A — Drop the empty legacy tables (cleanup; ~1 commit) — *task already filed*
All **0 rows** (verified). Removes the last of the dead schema.
- `linkedin.Task` (+ `TaskQuerySet`) and `linkedin.SearchKeyword`: delete from `models.py`, remove
  `TaskAdmin`/`SearchKeywordAdmin` from `admin.py`, fix `management/commands/reset_data.py`
  (it counts/deletes them).
- `crm.Deal` + `crm.Outcome`: delete `crm/models/deal.py`, fix `crm/models/__init__.py`, remove the
  `Lead.get_labeled_arrays` method (uses Deal) and `Lead.get_embedding` (legacy lazy accessor) plus
  their tests in `tests/ml/test_embeddings.py` + `tests/db/test_lazy_enrichment.py`.
- `chat` app (`ChatMessage`): remove `ChatMessageAdmin` + the import in `admin.py`, drop
  `chat.apps.ChatConfig` from `INSTALLED_APPS`, delete the `chat/` app. (Generate the DeleteModel
  migration **while the app is still installed**, or drop the table manually.)
- Unused `Campaign` columns: `product_docs`, `campaign_objective`, `booking_link`, `is_freemium`
  (1 row has it True — a leftover; safe), `action_fraction`, `seed_public_ids`, `model_blob`.
  **KEEP `Campaign.users`** — `AccountSession.campaigns` uses it. Remove `is_freemium` from
  `CampaignAdmin.list_display`/`list_filter` when you drop the field.
- `makemigrations` for `linkedin` + `crm`, run the full suite, deploy with `migrate`.
- **Backups exist** — this is reversible from `~/backups/20260609-232550/`.

### 5.B — Make connection-with-note + the app-side flows production-real
`actions/connect_note.py`, `actions/inmail.py`, `actions/like.py` are all **mock-verified only** —
the Playwright selectors have never run against live LinkedIn. Verify each against a real page
(using an authorised test contact), fix selectors, and add a screenshot-on-failure diagnostic.

### 5.C — Encrypt secrets at rest (commercial security)
`linkedin_password`, `totp_secret`, `SiteConfig.llm_api_key`, `slack_webhook_url` are **plaintext** in
`db.sqlite3`. For a commercial product, encrypt with Fernet keyed from an env var (e.g.
`OO_SECRET_KEY`), decrypt on read in `launch.py`/`llm.py`. Decide key management with the user first.

### 5.D — Production hardening (the app runs on Django `runserver` with `DEBUG=True`)
`django_settings.py` has `DEBUG=True`, a hardcoded `SECRET_KEY`, `ALLOWED_HOSTS=["*"]`, and is
served by the dev `runserver`. Fine behind Tailscale today; for commercial/public exposure: env-var
`SECRET_KEY`, `DEBUG=False` + real `ALLOWED_HOSTS`, gunicorn/uvicorn + a reverse proxy, static via
whitenoise. (The live-probe found `DEBUG=True` leaks the URLconf on a 404.)

### 5.E — Real billing (only if the user wants in-app payments)
Today billing is a **Stripe-portal deep-link stub** (paste a portal URL in Settings). Full SaaS
billing = Stripe Checkout + webhooks + a Subscription model + plan-gating. Big build; scope with the
user. Multi-tenant workspaces/seats/roles are **not** present (single-tenant today) — needed before
selling seats.

### 5.F — Finish HeyReach parity (from the audit parity table)
Missing vs HeyReach: A/B message variants; multi-account **sender rotation per campaign** (the
`select_account`/`account_pool` seam exists but `Campaign.users` is never populated by the dashboard,
so it returns nothing — wire campaign→accounts in the UI); lead sources (post-engagers, group/event
members); inbox tags/snippets/assignment; reply sentiment/categorisation; webhooks/public API/Zapier;
account warmup mode; proxies-per-account. Pick what matters for the user's go-to-market.

---

## 6. Audit findings still open (the ones worth doing)

Full set: `~/OpenOutreach-badlaptop/audit/audit-raw.json` (225 findings, 10 areas: models,
dash-backend, dash-frontend, worker, executor, auth-2fa, tests, dead-code, heyreach-parity,
live-probe). The high-value ones not yet fixed:

- **Reply direction by display-name string match** (`inbox/poller.py`): `direction="out"` iff the
  message sender == the session user's exact "First Last". A middle name / locale / emoji misclassifies
  OUR messages as inbound → false `stopped_reply` + false Slack pings; a lead sharing the name inverts
  it. Real fix needs `linkedin_cli.get_conversation` to return the sender **URN** (compare to
  `self_profile['urn']`) — an upstream `linkedin-cli` change.
- **Executor bricks a lead on any exception** (`run_due_states` → `STOPPED_ERROR`, terminal, no
  retry, invisible in the dashboard). One Playwright timeout permanently drops a lead; a browser
  crash can mass-brick the due backlog. Add a retry/backoff + a visible error surface.
- **No claim/lock around browser sends** + **SQLite under two writer processes**: a crash between
  the browser action and the DB save can double-send; concurrent web+worker writes can race the
  contact-once guard (the unique constraint is only `(lead, campaign)`). Consider `select_for_update`
  / WAL / a single writer.
- **`api_inbox_threads` N+1** (one query per thread, up to 300, per Unibox refresh) and **heavy-block
  duration** (a big enrichment pass starves the sender for its whole runtime — it's frequency-bounded,
  not time-bounded).
- **Activity feed**: historical `ActionLog` rows (pre-migration 0028) have `lead=NULL` → show "—".
  Forward code is correct; a one-off backfill could recover some names.
- **CSV export formula injection** (lead-controlled fields starting `=`/`+`/`-`/`@`); minor.

---

## 7. Test/verify checklist before you call anything done
1. `manage.py check` → 0 issues.
2. Full suite green (`pytest tests/ -q`).
3. JS `--check` on the extracted dashboard script.
4. Live authenticated smoke: login via `/login/`, GET `/dashboard/` + the endpoints you touched.
5. If you changed the worker/executor/poller: `podman restart oo-worker` and watch
   `podman logs -f oo-worker` for a clean `cycle: …` line (not a traceback).
6. Update **CLAUDE.md** + **ARCHITECTURE.md** (they still describe the deleted daemon as the entry
   point) and the relevant `handovers/*.md`.
