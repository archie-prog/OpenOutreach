# Handover — Legacy table & dead-column cleanup

- **Branch:** `chore/drop-legacy-tables` @ `f6ab041` (off `a1fa22a`, the live tip of `overhaul/grantgunner-hardening`)
- **Box:** badlaptop, `/home/linkedinautomation/OpenOutreach`; built in isolated worktree `~/oo-cleanup`
- **Status:** BUILT + VALIDATED on the branch. **NOT applied to the live DB** (awaiting GO).

## Dropped (all verified 0 rows on the LIVE DB before removal)
- **Models:** `Task` (+`TaskQuerySet`), `SearchKeyword`, `Deal` (+`Outcome` enum, +`Lead.get_labeled_arrays`), `ChatMessage` (chat app kept for migration history).
  - Kept the live pydantic `SearchKeywords` (lead discovery) — different symbol from the dropped Django `SearchKeyword`.
- **Campaign columns (6 dead):** `product_docs`, `campaign_objective`, `booking_link`, `action_fraction`, `seed_public_ids`, `model_blob`. **KEPT `is_freemium`.**
- **Collateral:** `admin.py` registrations, `reset_data.py`, stale `db/leads.py` comment, `DealFactory` + `get_labeled_arrays` tests, doc catalog entries.
- **Migrations:** `linkedin 0032`, `crm 0015`, `chat 0003` (DeleteModel auto-drops the chat M2M join tables).

## Why Deal/Outcome/get_labeled_arrays went too (judgment call — vetoable)
The live worker's lead scoring (`linkedin/ml/lead_score.py::score_pending_leads`) is **LLM-only** (`SiteConfig.ai_context` → Claude). The GPR/BALD/Deal-labelled qualification path is dead here (removed in `b793fc2`). `Deal` had no live consumers; `get_labeled_arrays` was called only by tests. To keep `Outcome`/`get_labeled_arrays`, trim the commit.

## Validation
- `manage.py check`: 0 issues
- `makemigrations --check` (unscoped): **No changes detected**
- `migrate` from scratch: OK
- **`migrate` FORWARD on a COPY of the live DB:** all 3 apply OK → 6 legacy tables gone, 6 columns gone, `is_freemium` kept, **data intact** (crm_lead 352, campaign 8, actionlog 70, messagethread 206)
- Full test suite (dev image): **194 passed, 9 skipped, 0 failed**

## Deploy (destructive — RA-gated, run only on GO)
Fresh pre-change backup: `~/backups/db-20260611-085106.sqlite3`
```
cd ~/OpenOutreach && git merge --ff-only chore/drop-legacy-tables
podman exec oo-web python manage.py migrate
podman restart oo-web oo-worker
```
- **Revert:** restore the backup over the `openoutreach-data` volume `db.sqlite3`.
- Merges cleanly onto `overhaul/grantgunner-hardening` (1 commit, direct descendant of `a1fa22a`) unless Fable has since committed touching the same files.

## Docs caveat (separate task)
The `CLAUDE.md`/`ARCHITECTURE.md` catalog entries were synced for this change. Both docs remain **broadly stale** from the `b793fc2` purge — they still describe deleted subsystems (`daemon.py`, `tasks/scheduler.py`, `pipeline/qualify.py`, `db/deals.py`, `db/summaries.py`, `ml/qualifier.py`, GPR/BALD). A full docs-sync is recommended as its own pass.
