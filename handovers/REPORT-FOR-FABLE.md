# Report for Fable — 3 fixes to integrate

**From:** the asus session (branch `feat/dashboard-spa`, `archie-prog` fork on GitHub).
**To:** Fable, on the live badlaptop branch `overhaul/grantgunner-hardening`.
**Status:** all 3 implemented + tested on asus (360 pass / 9 skip; +6 new tests). Re-apply onto your branch, adapting to your file state.
*(Line numbers below are asus-relative — grep, don't trust exact lines on your branch.)*

## Context / divergence
- Common ancestor: **`aed9b0d`**. Your branch diverged via `ba0fbcd → b793fc2 → 2b62552 → d0abdcc` (CSRF hardening, Settings/Guide, dead-code removal, cap-default merge, billing/SiteConfig, migrations 0030–0031).
- **Bottom line:** only **Fix 2** is a guaranteed code change on your branch. **Fix 1** depends on which `like.py` capture you carry (grep first). **Fix 3** is already wired — verify, don't rebuild.
- **Migrations:** none of these add one. Your branch has 0030/0031; any new migration must be **0032+**.

---

## Fix 1 — liked-post permalink (the "View liked post ↗" link must open the actual post)

**Problem (verified in code):** the old capture used `like.locator("xpath=ancestor::*[@data-urn][1]")` (closest ancestor) and pasted the **whole** urn. The closest `data-urn` to the Like button is usually a `socialDetail`/reactions sub-node, so it either (a) silently fell back to the recent-activity page, or (b) produced a **broken** `/feed/update/urn:li:fsd_socialDetail:(...)/` link.

**Fix (committed on asus, `linkedin/actions/like.py`):** a `_capture_post_url(page, like, fallback)` helper that:
1. scans **all** `data-urn` ancestors **outermost-first** (`reversed(.all())`) and extracts the activity id with `_ACTIVITY_RE = re.compile(r"urn:li:activity:(\d+)")` → `https://www.linkedin.com/feed/update/urn:li:activity:<id>/`;
2. else uses the card's `/feed/update/` or `/posts/` permalink anchor (strips `?…`);
3. else falls back to the recent-activity page.
The `_like()` capture block becomes `post_url = _capture_post_url(page, like, fallback=url)`.

**Apply to your branch — check first:**
```bash
grep -n "_ACTIVITY_RE\|_capture_post_url\|ancestor::\*\[@data-urn\]\[1\]" linkedin/actions/like.py
```
- Old `[@data-urn][1]` + no `_ACTIVITY_RE` → apply the rewrite (helper + the one-line capture replacement).
- `_capture_post_url` already present → nothing to do.
*(Per the branch analysis, your `like.py` is identical to `aed9b0d` = the OLD capture, so you almost certainly need this.)*

**No changes elsewhere:** `executor._handle_like_post` already forwards `result["post_url"]` to `ActionLog.target_url` (and only logs on success); `views.py` (`api_activity`, `api_campaign_detail`) and `dashboard.html` (`activityRow`, gated on `like_post` && truthy `post_url`) already surface it. `target_url` is 600 chars — no truncation.

**Test added (asus):** `tests/actions/test_like_capture.py` — activity urn → permalink, composite urn → extracted id, outermost preferred over inner socialDetail, anchor fallback, clean degradation. Bring it across.

**Gap:** the one historical 8-Jun test-like predates capture → permanently no link. Not backfillable.

---

## Fix 2 — Senders table connect cap = today's EFFECTIVE (randomised) cap  ← the only guaranteed change

**Problem:** `api_senders` shows `Connect/day` denominator from raw `daily_caps_json` (fixed 25), not `cap_for(a,"connect")` (today's per-day random value, e.g. 28 for a 20–30 range). The numerator (`0`) is correct — `daily_count` resets at local midnight. Only the **denominator** is wrong. (Your `api_accounts` already returns `connect_today: cap_for(...)` correctly — `api_senders` is just the endpoint that wasn't updated.)

**Fix (committed on asus, `linkedin/dashboard/views.py` `api_senders`):**
```python
# import
from linkedin.accounts.limits import cap_for, daily_count   # was: daily_count
# usage dict
"usage": {
    k: {"used": daily_count(a, k), "cap": cap_for(a, "connect") if k == "connect" else v}
    for k, v in caps.items()
},
```
Use the **targeted** form (`cap_for` only for `connect`) — safest, changes only the connect denominator. `cap_for` falls through to `daily_caps_json` when randomisation is off, so random-OFF accounts are unchanged. **No HTML change** — `dashboard.html` renders `${u.connect.cap}` as a pass-through, so the table shows `0/28` automatically. No migration.

---

## Fix 3 — "View Profile" sequence step  ← verify present, no rebuild

**Confirmed fully wired on both branches** (b793fc2 deleted only legacy daemon/pipeline/agent code, not `_handle_profile_visit`):
- `models.py`: `SequenceStep.StepType.PROFILE_VISIT = "profile_visit","View Profile"`; matching `ActionLog.ActionType`; default cap `profile_visit: 100`.
- `executor.py`: `_STEP_ACTION` + `_HANDLERS` map it; `_handle_profile_visit` → `visit_profile` (`linkedin_cli.actions.search.visit_profile`); browser ensured; `_log` attributes it to the lead.
- `views.py` `_STEP_DEFAULTS["profile_visit"] = {}`; `dashboard.html` `STEP_TYPES` includes `["profile_visit","View Profile"]` (selectable in add-step + change-type), icon `👁️`, activity label "👁️ Viewed profile".

**Verify:** `grep -n profile_visit linkedin/sequences/executor.py linkedin/templates/dashboard/dashboard.html linkedin/dashboard/views.py linkedin/models.py` (every layer hits). In the UI: Flows → add step / change-type shows **View Profile**.

**Use:** add it as a step (or change a node's type to it); no config; single linear continuation. The worker navigates to the lead's profile and logs `profile_visit`.

**Privacy caveat (surface to the user — not a code issue):** the "X viewed your profile" notification only reaches the lead if **that account's** LinkedIn Settings → Visibility → "Profile viewing options" is **non-anonymous** (name + headline). In Private/anonymous mode the visit still happens and is logged, but the named notification is suppressed. The tool cannot change that account-level setting.

**Test added (asus):** `tests/tasks/test_sequence_executor.py::test_profile_visit_step_visits_lead_and_logs`.

---

## Apply order + verification
1. **Fix 1** (`like.py`): grep; apply rewrite if old.
2. **Fix 2** (`views.py` `api_senders`): apply the targeted edit. **(guaranteed change)**
3. **Fix 3**: verify-only.
```bash
python manage.py check
pytest tests/actions/test_like_capture.py tests/tasks/test_sequence_executor.py tests/dashboard/test_dashboard_api.py -q
python manage.py showmigrations linkedin   # 0030/0031 applied; nothing new from these
# restart oo-web (+ oo-worker if you touched like.py/executor)
```
**Manual:** Senders connect column shows today's effective cap (matches "today N" on Accounts tab); a freshly-liked post's link opens `/feed/update/urn:li:activity:<id>/`; Flows shows "View Profile".

**Files:** `like.py` (conditional), `views.py` `api_senders` (certain). All else verify-only.
