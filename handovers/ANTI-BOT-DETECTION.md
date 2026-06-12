# Anti-Bot-Detection — Handover & Reference

**Audience:** the next session continuing the LinkedIn anti-detection hardening of this kit (OpenOutreach, self-hosted on `fedora-badlaptop`).
**Written:** 2026-06-12. **Branch:** `overhaul/grantgunner-hardening` (local on badlaptop, **not pushed** — no creds on the box).
**Read this top to bottom before touching the browser/automation layer.**

---

## 0. TL;DR / current state

- An account (`aawilding`, pk 1) was **restricted by LinkedIn on 2026-06-09 21:02**. Forensic root cause: a **9pm burst of ~24 connection requests at 13–15s intervals on 2026-06-08** (see §1). Then the 24/7 reply-poller hammered the restricted session **1,038× (Messaging API 401)** over 3 days.
- **The worker (`oo-worker` container) is STOPPED.** Keep it stopped until `aawilding` is manually recovered in LinkedIn (the human must clear the restriction from their normal device, then hit "Test connection" in the Accounts tab).
- Two other accounts: **Toby** (pk 2, no cookies/TOTP — needs login) and **Josh** (pk 3, **connected**, cookies valid, not in any campaign).
- **Most behavioural + fingerprint detection vectors are now fixed** (§3). **Four items remain in progress** (§4): GPU rendering, Voyager headers, CDP input leak, distribution refinements. Two declined by the user: withdraw-stale-invites, acceptance-rate.

## 1. Forensic root cause (what actually got the account flagged)

From `ActionLog` analysis (London local time):
- 83 LinkedIn-visible outbound actions ever (75 connect, 8 message).
- **2026-06-08, 20:46–21:20: ~24 connects, gaps as low as 13s, several in the same minute.** At 9pm. This is the textbook bot burst that caused the flag the next day.
- Pre-fix the send-window/pacing was NOT enforced (it was added between Jun 8 and Jun 11). By Jun 11 actions were inside 09–17 with larger gaps.
- Compounding: `poll_replies` ran every ~90s **24/7** calling the Voyager messaging API; once the account was restricted (401) it kept retrying — 1,038 times — which hardens detection.

## 2. How LinkedIn's detection actually works (researched, sourced)

Confidence: **[E]** established/primary, **[C]** corroborated multi-vendor, **[S]** speculative/single-vendor.

**Fingerprinting (first-party JS):**
- LinkedIn ships **FingerprintJS** incl. **`getHasLiedOs`** (UA vs `navigator.platform` vs `oscpu` vs touch) and **`getHasLiedLanguages`** (`navigator.language` vs `languages[0]`). Incoherent spoofing trips these. **[E]** (Castle: https://blog.castle.io/detecting-forged-browser-fingerprints-for-bot-detection-lessons-from-linkedin/)
- `navigator.webdriver` (hide via `--disable-blink-features=AutomationControlled`). **[E]**
- Playwright artifacts: `window.__playwright__binding__`, `__pwInitScripts`, `exposeFunction` `__installed` property. Stealth does NOT remove these. **[E]** (Castle). *We checked — none currently leak on `window`.*
- CDP detection: classic `.stack`-getter trick died in V8 May 2025; **live leak = CDP-dispatched input has `pageX==screenX && pageY==screenY`**. **[E]** (CDP-Patches: https://github.com/Kaliiiiiiiiii-Vinyzu/CDP-Patches)
- Canvas/WebGL/AudioContext: **headless/Xvfb/SwiftShader software rendering is distinguishable** from real GPU. **[E]**
- `deviceMemory` (Chrome caps at 8; missing/odd = tamper). **[E]** `navigator.plugins` length 0 = headless. permissions consistency. Client Hints (`Sec-CH-UA-*`) must match UA. **[E]**
- `playwright_stealth` covers webdriver/plugins/languages/permissions/chrome.* /WebGL-vendor — but does it INCOHERENTLY (see §3 fingerprint fix), and does NOT cover `__pw*` globals, CDP leaks, canvas/WebGL render hashes, AudioContext, TLS/JA3. **[E]**

**Behavioural ML:**
- LinkedIn Engineering anti-abuse blog: they compute **ratio + log** features and look at **distribution shape** — legit = exponential decay; bots = one dominating value OR abnormally **uniform** counts. Explicitly target **low-frequency (slow) automation**. So slow is necessary but NOT sufficient — avoid uniform/correlated distributions. **[E]** (https://www.linkedin.com/blog/engineering/trust-and-safety/leveraging-behavior-analytic-computation-for-anti-abuse-defenses)
- Mouse: humans = hundreds of mousemove, variable accel, micro-jitter; Playwright `mouse.move` = straight even steps. **[E]** Typing: `fill()` = no keydown/keyup (detectable); even `type()` with **uniform** delays is flagged (FCaptcha checks log-normal fit, uniformity, autocorrelation). **[E]** Circadian: 24/7 / no weekend dips = bot. **[E]** (Akamai)

**Voyager internal API:**
- Real web client sends on `/voyager/api/...`: `csrf-token` (= JSESSIONID minus quotes), `x-restli-protocol-version: 2.0.0`, `x-li-lang`, **`x-li-track`** (JSON: clientVersion/mpName=voyager-web/osName/timezone/displayDensity/...), **`x-li-page-instance`** (`urn:li:page:<pageKey>;<uuid>`), `accept: application/vnd.linkedin.normalized+json+2.1`, plus browser `referer`/`sec-fetch-*`. **[C]** Calling from **in-page `fetch()`** (same-origin) is SAFEST (authentic cookies/TLS/headers); calling from an external http client is far worse (JA3/HTTP2 fingerprint). **clientVersion rotates every ~4–8 weeks — never hardcode; read live.** **[C]**

**Network/account:** datacenter IP flagged; **residential/ISP/mobile much safer**; geo must match; concurrent sessions / impossible travel flagged; `li_at`(~1yr)+`JSESSIONID` bound to device+IP. **[C/E]**

**Rate limits / escalation:** ~100/week invites (rolling 7-day), acceptance <~25% tightens limits, **no official "Trust Score"** (SSI is real but no documented causal link to limits). Escalation: soft limit → checkpoint (**app-push / email-PIN / Arkose FunCaptcha / ID**) → ban. **[C/E]**

**APFC / "BrowserGate" (Apr 2026, BleepingComputer-corroborated, source has anti-LinkedIn bias):** a JS collector gathers **~48 device traits**, RSA-encrypts them (`apfcDfPK`), and **injects the blob as a header on every API request**; also **actively probes ~6,200 browser-extension IDs** and DOM-scans for `chrome-extension://` refs. Our Playwright (no-extension) approach is correct on the extension axis; but software-rendered canvas/WebGL/audio in the 48 traits look virtualized. **[C]**

## 3. What is FIXED (commits on the branch, all verified)

Each verified by a probe/test this session.

1. **Fingerprint coherence** (`216b868`, `linkedin/browser/launch.py::_launch_fingerprinted` + `_COHERENCE_JS`). THE biggest fix. `playwright_stealth` was spoofing `platform→Win32`, `languages→en-US`, `WebGL→Mac Intel` on a Linux/en-GB box — tripping `getHasLiedOs`/`getHasLiedLanguages`. We keep stealth's good evasions but add a post-stealth init script forcing **coherent** values: `platform=Linux x86_64`, `languages=['en-GB']`, `deviceMemory=8`, WebGL renderer → a plausible **Linux Intel** string, with `getParameter.toString()` preserved as `[native code]`. Verified: `liedOS: coherent`, `liedLang: coherent`.
2. **Launch without automation switches** (`aa774bc`): `args=['--disable-blink-features=AutomationControlled']`, `ignore_default_args=['--enable-automation']`. `navigator.webdriver=False` even without stealth.
3. **Human typing not `fill()`** (`aa774bc`): inmail subject/body, connect-note, TOTP login creds + 2FA codes now use `human_type` (per-char delays). NOTE: package's `human_type` uses **uniform** delay — item §4.4 will improve to log-normal.
4. **Working-hours-only sessions** (`74bbf42`, `limits.py::in_session/daily_session_window`, `run_worker.py`): each account opens a session at a **random time in the first 30 min of its window** (e.g. 09:19), closes near the end (e.g. 16:48); **browser closed off-hours; nothing overnight/weekends**. Verified.
5. **Random pacing across the window** (`5a483e2`, `limits.py::next_action_at`): wide jitter `uniform(-0.7,0.7)*spacing` + ~8-min min-gap floor; connects spread irregularly across 09:00–17:00. (Jitter is uniform — §4.4 to improve.)
6. **Auto-pause + alert on auth failure** (`74bbf42`): on `AuthenticationError` (401/restriction) the worker sets `LinkedInProfile.auto_paused_at`, records reason, Slack-alerts (if webhook set), and **stops touching the account**. Cleared by a successful "Test connection". No more 1,038× hammering.
7. **Weekly connect cap** (`2523273`/`7fc002d`, `limits.py::weekly_count/has_capacity`, field `connect_weekly_limit` default 100): freezes near ~100/week on top of daily 25. Editable in Accounts tab.
8. **Spintax message variation** (`2523273`, `executor.py::_spin/render_template`): `{Hi|Hello|Hey} {first_name}` → varied per recipient.
9. **Humanize before actions** (`2523273`, `linkedin/browser/humanize.py`): scroll + mouse-move + 3–8s dwell before connect/like/inmail/profile-visit. (Mouse is straight-line — §4.4 to improve to Bézier.)
10. **Search pagination pause** (`5a483e2`): 6–12s between people-search result pages.
11. **Region-correct context**: `locale=en-GB`, `timezone_id=Europe/London`, viewport 1536×864.

**Confirmed-good (no change needed):** residential UK IP `81.135.37.244` via normal routing (Tailscale only carries SSH — no exit node); no browser extension; in-page `fetch()` for Voyager (right approach).

## 4. REMAINING WORK (user approved 1–4; declined 5–6)

### 4.1 GPU rendering (HIGHEST leverage) — IN PROGRESS (task #14)
Browser runs under **Xvfb (DISPLAY=:99)** in `oo-worker` → **SwiftShader software WebGL/canvas** → looks virtualized to the deep collector. We mask the renderer *string* but the actual pixel output is still software.
**Approach:** pass the host GPU (`/dev/dri/*`, `render`/`video` groups) into the rootless podman `oo-worker` container; add Chromium flags (`--use-gl=angle --use-angle=gl --enable-gpu --ignore-gpu-blocklist` or `--use-gl=egl`) in `_launch_fingerprinted`; verify `WEBGL_debug_renderer_info` reports the real Intel GPU (not SwiftShader). May need the real X display rather than Xvfb, or VirtualGL. **Check first:** `ls /dev/dri` on host + inside container; `podman inspect oo-worker` for device mounts; the container run/compose definition.

### 4.2 Voyager headers (fork/patch `linkedin_cli`) — TODO (task: create)
In-page `fetch()` is right, but missing **`x-li-track`** (live `clientVersion`) and **`x-li-page-instance`**. `linkedin_cli/api/client.py::__init__` builds only csrf-token/accept/x-li-lang/x-restli.
**Approach (durable, no real fork needed):** at Django app-ready, **monkeypatch** `linkedin_cli.api.client.PlaywrightLinkedinAPI` to inject the missing headers. Read the **live clientVersion** from the page (capture it from a real authenticated session — use **Josh's** session, since aawilding is restricted: hook `page.on("request")` on a `/voyager/api/` call and copy the real `x-li-track`/`x-li-page-instance` the app sends, OR read `clientVersion` from the page bundle/config). Set `x-li-page-instance` to a pageKey matching the page actually navigated. DO NOT hardcode clientVersion (rotates ~monthly). NOTE: editing the installed package files directly is non-durable (lost on image rebuild) — prefer the monkeypatch in app code.

### 4.3 CDP input-coordinate leak — TODO (riskiest)
Playwright dispatches clicks via CDP → `pageX==screenX`. Fix needs OS-level input: **CDP-Patches** (https://github.com/Kaliiiiiiiiii-Vinyzu/CDP-Patches, uses xdotool on Linux) or **rebrowser-patches** (patched Playwright). Route clicks/typing through it. HIGH RISK (could break interactions); niche vector. Attempt carefully, behind verification, last.

### 4.4 Distribution refinements (clean app code) — TODO
- **Typing:** replace package `human_type`'s **uniform** delay with a **log-normal** inter-key distribution (write app-side `human_type` in `linkedin/browser/humanize.py`, use it instead of `linkedin_cli...human_type`). Add occasional backspace/correction.
- **Mouse:** `humanize.py` — replace straight `mouse.move` with **Bézier path + jitter + variable velocity**; ensure mousemoves fire before clicks.
- **Pacing:** `limits.py::next_action_at` — replace **uniform** jitter with a **log-normal/exponential** gap distribution (LinkedIn flags flat/uniform). Keep the min-gap floor + window bounds.

### Declined by user
- **5. Withdraw stale pending invites** — NO.
- **6. Acceptance-rate / targeting** (~18%, <25% threshold) — NO.

## 5. Key code map

- `linkedin/browser/launch.py` — `_launch_fingerprinted` (launch args, context options, stealth, `_COHERENCE_JS`), `start_browser_session`, `verify_account` (non-blocking connection test).
- `linkedin/browser/session.py` — `AccountSession`, `close_browser()`.
- `linkedin/browser/humanize.py` — `humanize_page` (scroll/dwell/mouse).
- `linkedin/accounts/limits.py` — caps, `is_send_time`, `in_session`, `daily_session_window`, `next_action_at`, `weekly_count`, `has_capacity`.
- `linkedin/sequences/executor.py` — sequence engine; `_handle_*`, `send_connection_request`, `send_message`, `render_template`/`_spin`, `due_states`, `due_states_by_account`, `active_sending_accounts`, `run_states`.
- `linkedin/management/commands/run_worker.py` — the loop: verify-requests → per-account in-session sending → replies/heavy on default → auto-pause on auth error.
- `linkedin/actions/{like,inmail,connect_note}.py` — app-side browser actions.
- `linkedin/auth/login.py` — TOTP login (`login_with_totp`).
- `linkedin_cli` (PyPI dependency, NOT in repo) — `api/client.py` (Voyager fetch), `browser/login.py` (launch_browser/human_type), `browser/nav.py` (`human_type`), `actions/{connect,message,search,status}.py`.

## 6. How to verify (probe pattern)

Browser tests need a display, so they run in `oo-worker` with `DISPLAY=:99`. Pattern used this session:
```
podman start oo-worker; sleep 3
scp probe.py to box; podman cp probe.py oo-worker:/app/probe.py
podman exec -e DISPLAY=:99 oo-worker python manage.py shell -c 'exec(open("/app/probe.py").read())'
podman stop oo-worker   # keep it stopped — account restricted
```
Useful evaluations: `navigator.webdriver`, `navigator.platform`, `navigator.languages`, `navigator.deviceMemory`, WebGL `UNMASKED_RENDERER_WEBGL` (+ `gl.getParameter.toString()`), `Object.getOwnPropertyNames(window).filter(n=>n.startsWith('__pw'))`, notification permission consistency. The fingerprint must be **coherent** (Linux+Linux+en-GB) — do NOT reintroduce incoherent spoofing.

## 7. Operational notes / gotchas

- **DB:** SQLite in podman volume `openoutreach-data` → `/app/data/db.sqlite3`. Several `db.bak-*` snapshots from today's cleanups.
- **Migrations applied today:** 0036–0040 (sending_account FK→M2M, leadcampaignstate.sending_account, verify fields, auto_paused_at, connect_weekly_limit).
- **Tailscale SSH:** `ssh linkedinautomation@fedora-badlaptop` (no key needed; user is `linkedinautomation`, NOT archie).
- **Dashboard:** http://fedora-badlaptop:8000/ (login archie / Grantgunner2026). `oo-web` runs it.
- **CLAUDE.md rule:** never use the auto-memory system for this project; all context goes in CLAUDE.md/ARCHITECTURE.md/handovers.
- Editing installed `linkedin_cli` files in the container is **non-durable** (lost on rebuild) — patch from app code instead.
- The browser tests start `oo-worker`; with campaign 8 paused it does nothing against LinkedIn (empty schedule), so starting it briefly is safe — but **always stop it again**.

---

## UPDATE (later, same session) — items resolved + corrections

**Anti-detection roadmap §4 is now resolved:**
- **① GPU rendering — CLOSED, not achievable.** The laptop's GPUs are Ivy Bridge (2012, NO Vulkan) + an old AMD; modern Chromium ANGLE defaults to Vulkan → SwiftShader, and Xvfb's GLX path is software (llvmpipe). Tested 9 flag/driver combos; all fell to software or broke WebGL. **Mapped `/dev/dri/renderD128,129` into the `oo-worker` container** (recreated it — devices now present) and added GL flags (`--use-gl=angle --use-angle=gl`) so it uses **Mesa llvmpipe** (real Linux software stack) rather than SwiftShader. The fix was to make it **honest**: WebGL now truthfully reports the llvmpipe renderer (matching the actual canvas pixels), instead of stealth's incoherent Mac-GPU spoof. Real-GPU would need newer hardware or running on the laptop's real desktop X session (DISPLAY=:0) — deferred, not blocking. (commit f7abf8b)
- **② Voyager headers — DONE.** `AccountSession.attach_voyager_capture()` listens to LinkedIn's own voyager/api requests and stores the live `x-li-track` (real clientVersion, e.g. 1.13.44770 — rotates ~monthly, captured not hardcoded) + `x-li-page-instance`; `launch._patch_voyager_headers()` monkeypatches `PlaywrightLinkedinAPI.__init__` to inject them. Verified end-to-end on Josh's session: our API client now sends x-li-track + x-li-page-instance + csrf-token + x-restli, matching the web client. (commit 3ebc228)
- **③ CDP input-coordinate leak — SKIPPED (deliberate).** Research showed the OS-level-input fix (CDP-Patches/rebrowser) targets **Cloudflare/DataDome**, NOT LinkedIn; LinkedIn's stack is fingerprint+behavioural+Voyager+APFC. Low confirmed value for LinkedIn + high integration risk (routing all clicks through xdotool). Revisit only if concrete evidence emerges that LinkedIn checks `pageX==screenX`.
- **④ Distribution refinements — DONE.** `humanize.py`: `type_humanly` (log-normal keystroke timing + occasional typo/correction) replaces uniform-delay typing; `_bezier_mouse` (curved path + jitter + variable velocity) replaces straight-line moves; `limits.next_action_at` jitter switched uniform→Gaussian (LinkedIn flags flat distributions). (commit 92b4b70)

**Also committed after the original doc (all verified):**
- `human_type` instead of `.fill()` for inmail/connect-note/TOTP-login inputs (instant injection was a tell). (aa774bc)
- Launch Chromium WITHOUT automation switches: `--disable-blink-features=AutomationControlled` + `ignore_default_args=['--enable-automation']`. (aa774bc)
- **Fingerprint COHERENCE — the biggest fix.** playwright_stealth was spoofing platform→Win32, languages→en-US, WebGL→Mac on this Linux/en-GB box, tripping FingerprintJS's getHasLiedOs/getHasLiedLanguages. `_COHERENCE_JS` (in `_launch_fingerprinted`) forces coherent Linux/en-GB values + deviceMemory=8 + honest llvmpipe WebGL, with getParameter.toString() preserved as native. (216b868 / f7abf8b)
- Pacing now spreads connects RANDOMLY across 09:00–17:00 (wide jitter + ~8-min floor). (5a483e2)

**Corrections to the original doc / earlier claims:**
- **HeyReach was NOT banned — it is still running.** The "ban" claims were competitor "best HeyReach alternatives" FUD + a biased source. Their working model: cloud + **dedicated residential proxy per account** + real-browser behaviour + warm-up + human-scale limits + content variation. Valid blueprint.
- **Egress IP CONFIRMED:** LinkedIn sees the **home residential IP 81.135.37.244** (UK BT), NOT Tailscale. No exit node; route is enp3s0→192.168.1.254; the container egresses the same IP. Tailscale only carries SSH. No proxy needed at 3 accounts; the real IP risk is **concurrent sessions** (an account used by its owner elsewhere while the kit runs it).
- **Company-page-takedown vs account-ban are separate:** the former is LinkedIn enforcing ToS against a *visible automation vendor* (brand/legal); the latter is per-account *detection*. Good per-account anti-detection keeps customer accounts safe even if LinkedIn dislikes the vendor.
- **Like action is confirmed fixed + verified** (really likes + re-reads reaction state to confirm). Zero current likes is only because the worker is stopped / aawilding restricted.

**Still open:** recover aawilding (human, restricted); Toby login (TOTP/approval); onboarding flow (parked); assign Josh to a campaign; push branch (no creds). ~22 commits, local on `overhaul/grantgunner-hardening`.
