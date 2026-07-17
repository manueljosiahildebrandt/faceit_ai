# Faceit AI — Technical operations reference

Dense notes from production-style use: install pitfalls, consent refresh, log semantics, storage layout, and SQL/audit introspection. Implementation paths refer to this repository’s tree.

---

## 1. Editable install and console scripts

### 1.1 Correct install invocation

```bash
cd /path/to/faceit_ai
source .venv/bin/activate
python -m pip install -e .
```

- Prefer `python -m pip` so the active interpreter’s `pip` is used unambiguously.
- Do **not** paste trailing shell comments on the same physical line as `pip` arguments in environments where paste or line breaks can leave a stray `#` token; pip will error with:
  - `Invalid requirement: '#': Expected package name at the start of dependency specifier`

### 1.2 Entry points (`pyproject.toml` → `[project.scripts]`)

| Console command       | Import target                          |
|----------------------|----------------------------------------|
| `analyze_photos`     | `faceit_ai.cli:analyze_photos_cli`      |
| `register_person`    | `faceit_ai.cli:register_person_cli`     |
| `audit_people_portraits` | `faceit_ai.cli:audit_people_portraits_cli` |
| `set_person_consent` | `faceit_ai.cli:set_person_consent_cli` |
| `report_decisions`   | `faceit_ai.cli:report_decisions_cli`    |
| `init_db`            | `faceit_ai.cli:init_db_cli`             |
| `migrate_sqlite_to_db` | `faceit_ai.cli:migrate_sqlite_to_db_cli` |

After a successful `pip install -e .`, wrappers land in `.venv/bin/<name>`. If the shell reports `command not found`, the editable install failed or a different venv is active. Verify with:

```bash
which set_person_consent
```

---

## 2. Data directory and SQLite

### 2.1 Resolution order

- **`FACEIT_AI_DATA_DIR`** — If set, relative paths in config (e.g. `database.sqlite_relative_path`, `paths.log_relative_dir`) join under this directory.
- If unset, defaults align with the **current working directory** when commands run (typical: repo root `faceit_ai/`).

Default DB path (from `config/default.yaml`):

- `database.sqlite_relative_path: "data/consent.db"`  
  → resolved file: `{FACEIT_AI_DATA_DIR or cwd}/data/consent.db`

### 2.2 What is **not** stored in the repo

`analyze_photos` takes a **folder argument** pointing at **your** image tree (e.g. camera card copy, production RAW folder). The pipeline **reads** those files in place. It does **not** mirror the full image set into `faceit_ai/` unless you opt into export (see §7).

### 2.3 Configurable data folder and database URL

Two config keys control where data lives (`config/default.yaml` / packaged `default_config.yaml`), both settable in the web UI **Settings → Data & Database**:

- `paths.data_dir` — folder holding local data (SQLite file when no server URL is set, plus logs). Resolution precedence: `FACEIT_AI_DATA_DIR` env var > `paths.data_dir` > current working directory (`src/faceit_ai/settings.py` `_resolve_data_root`).
- `database.url` — a full SQLAlchemy URL. **Empty = local SQLite** (`database.sqlite_relative_path`). Set it to move to a shared server DB. `${ENV}` placeholders are expanded (`os.path.expandvars`), so the DB password can stay in an environment variable rather than the shared YAML, e.g.:

```yaml
database:
  url: "postgresql+psycopg://facit:${FACIT_DB_PASSWORD}@synology.local:5432/facit"
```

### 2.4 Shared database for multiple PCs (PostgreSQL on Synology)

A single SQLite file on a NAS share is **not** safe for simultaneous writers. For several PCs analyzing at once, use a real database server. Recommended: **PostgreSQL in Synology Container Manager**.

**One-time Synology setup (Container Manager / Docker):**

1. Install **Container Manager** in DSM, pull image `postgres:16`.
2. Create a container with:
   - Environment: `POSTGRES_USER=facit`, `POSTGRES_PASSWORD=<strong-secret>`, `POSTGRES_DB=facit`.
   - Port: map container `5432` → host `5432`.
   - Volume: map a folder on a **fast** volume (SSD cache or SSD volume; avoid a slow HDD-only share) to `/var/lib/postgresql/data`.
3. Note the NAS hostname/IP; ensure the firewall allows port 5432 from the worker PCs (LAN only).

**On each PC:**

1. Install the driver: `python -m pip install -e ".[postgres]"` (adds `psycopg`).
2. Set the same `database.url` (password via env var; put `export FACIT_DB_PASSWORD=…` in your shell profile).
3. Run **once from any single PC**: `init_db` (creates the schema on Postgres).
4. Optional data move: `migrate_sqlite_to_db --source data/consent.db` (defaults target to the configured `database.url`). Refuses to run if the target already has people.

**Concurrency safeguards (all in the shared DB, no lock files):**

- **Folder claims** (`processing_run` table, `src/faceit_ai/services/processing_runs.py`): `analyze_photos` claims the resolved folder path on start and releases it on finish. A second PC pointed at the same folder exits with a "already being analyzed by <host>" message (exit code 3). A partial unique index (`finished_at IS NULL`) enforces one active run per folder; crashed runs older than `STALE_AFTER` (6h) are auto-reaped.
- **Active-run visibility**: the web UI **Current Status → "Machines running now"** lists active runs across all machines (throttled query, `/api/status`).
- **Duplicate person guard**: partial unique index `uq_person_active_name` prevents two PCs registering the same active name.
- **Retry helper**: `run_with_retry` (`src/faceit_ai/persistence/session.py`) retries transient lock/serialization errors with backoff.
- **Engine tuning**: one cached engine per process; SQLite gets WAL + `busy_timeout`; server backends get `pool_pre_ping` + a connection pool.

**Recommended workflow (each user, own folder, check at once):** each PC copies its SD card into its **own** NAS folder, then runs `analyze_photos` on that folder. Because folders don't overlap, all PCs proceed in parallel against the shared DB; the claim table only blocks accidental double-runs of the *same* folder.

---

## 3. Consent changes without re-registration

### 3.1 `set_person_consent`

Implementation: `src/faceit_ai/services/set_consent.py` → repository `update_consent_for_person_name`.

CLI (`src/faceit_ai/cli.py`):

- Exactly one of `--revoke` or `--grant`.
- `--revoke` sets `consent_given=false` for the **active** person row matching `name`.

Re-registration **does not** unconditionally overwrite consent; use this command to flip flags for an existing gallery person.

### 3.2 Refreshing decisions after consent change

`AssetDecision` rows are keyed by processed asset. After changing consent, run:

```bash
analyze_photos "<your image root>" --usage social --force
```

- **`--force`**: bypasses the “already in DB” short-circuit and recomputes faces + decisions for each listed file (see `src/faceit_ai/services/analyze_photos.py` cache logic).

Without `--force`, images whose SHA-256 is already associated with a decision may be **skipped**, so consent changes would not appear in new decisions.

---

## 4. Console summary vs per-person introspection

### 4.1 Run summary vs database-wide totals

After a batch, the console shows two blocks separated by ASCII banners:

1. **`THIS SCAN | this folder only`** — counts for **only** the directory you passed to `analyze_photos` (newly analyzed, skipped, decode errors, files listed), now printed as separate lines with prefix `this_scan`.
2. **`DATABASE TOTALS | entire SQLite file`** — lines prefixed **`db_all_time`** with **`total_assets_with_decision`** and per-status counts for **the whole database** (every path ever stored), not just this folder.

Example shape:

```text
------------------------------------------------------------
      THIS SCAN | this folder only (not the whole database)
------------------------------------------------------------
this_scan |   newly analyzed: …
this_scan |   skipped (already in DB): …
this_scan |   decode errors: …
this_scan |   files listed in folder: …
------------------------------------------------------------
   DATABASE TOTALS | entire SQLite file (every path ever recorded)
------------------------------------------------------------
db_all_time | total_assets_with_decision=N (entire SQLite DB — all folders ever) | by_status={…}
db_all_time |   blocked: …
db_all_time |   ok: …
```

These lines are plain `INFO` (neutral terminal color), not phase-colored. Paths are omitted at INFO. Source: `src/faceit_ai/reporting.py` (`query_decision_summary`, `samples_per_status=0` after the scan) and `src/faceit_ai/services/analyze_photos.py`. For sample paths, use **`report_decisions`**.

### 4.2 Where matched persons appear

Per-face identity and score are attached to audit events and optional JSON.

| Sink | Path / mechanism | Person field |
|------|------------------|--------------|
| Audit log | `{log dir}/audit.log` (JSON lines) | `audit.faces[]` entries include `"person"` and `"confidence"` (see `log_decision` payloads from `analyze_photos`) |
| Per-image JSON | `--json-out DIR` | Each `*.json` has `"faces": [ {"person": …, "confidence": …}, … ]` (see `src/faceit_ai/services/analyze_photos.py`, `spec_payload`) |
| SQLite | `asset_face.match_person_id` → `person.id` | Join to `person.name` |

**Decision engine shape** (`src/faceit_ai/decision/engine.py`): each face in `faces_out` uses keys `person` and `confidence` (scaled match score from the matcher, including sub–review-threshold unknowns).

### 4.3 Match tiers and image decisions

Config: `matching.match_score_scale`, `matching.match_threshold_strong`, `matching.match_threshold_review` (`src/faceit_ai/settings.py`, `src/faceit_ai/vision/matcher.py`). Legacy YAML may keep `strong_match_min` / `uncertain_min` (interpreted as **cosine** thresholds with scale **1**).

Per face, after picking the best gallery cosine (score = cosine × scale, default scale **512**):

| Condition | Matcher result |
|-----------|----------------|
| scaled score \< review threshold (default **200**) | unknown (`person_id` null) |
| review ≤ scaled \< strong (default **200–250**) | uncertain (identity kept) |
| scaled ≥ strong (default **250**) | strong match |

**Image aggregation** (`src/faceit_ai/decision/engine.py`) — precedence:

1. Strong match to Blocked / missing consent / usage denied → **`blocked`**
2. Uncertain match to the same disallowed person → **`review`** (“might be them”)
3. Unknown faces → `decision.unknown_face_status` (default **`ok`**; set to `review` to send all strangers to Review)
4. Matches to Allowed people (strong *or* uncertain) → contribute to **`ok`**
5. No faces → **`ok`** / `no_faces`

> **Note:** Older docs described “any unknown/uncertain → review”. That is **not** current behavior. Operator-facing summary: [README.md §5](../README.md#5-matching--decision-tuning).

---

## 5. SQLite query: distinct assets that matched a person

Schema: `src/faceit_ai/persistence/models.py` — `Asset`, `AssetFace`, `Person`, `AssetDecision`.

Example (replace DB path if `FACEIT_AI_DATA_DIR` is used elsewhere):

```sql
SELECT DISTINCT a.path
FROM asset a
JOIN asset_face af ON af.asset_id = a.id
JOIN person p ON p.id = af.match_person_id
WHERE p.name = 'alice'
ORDER BY a.path;
```

`match_person_id` is nullable on `AssetFace`; this query only returns faces that **matched** a gallery embedding (not “unknown” rows with no id).

---

## 6. Audit log as JSONL

Each line is one JSON object. Relevant fields for tooling:

- `message` — often `"decision"` for image outcomes
- Nested `audit.event` — e.g. `asset_decision`
- `audit.asset_path` — absolute path to the source image
- `audit.faces` — list of `{ "person": "<gallery name>", "confidence": <number> }` (or analogous for errors)

Filter example:

```bash
grep '"person": "alice"' logs/audit.log
```

Paths in grep output are still inside JSON; for robust parsing use `jq -c` per line.

---

## 7. Exporting flagged files (`--export-flagged`)

- CLI default **`off`**: no **`flagged/`** tree is created.
- Pass **`--export-flagged copy`** or **`move`** to enable exports for that run.
- **`from-config`**: reads **`export.flagged`** in YAML (`off` | `copy` | `move`). Packaged / sample default is **`off`**.

When `copy` or `move`:

- Files are placed under **`<input folder>/flagged/blocked/`** or **`…/flagged/review/`** according to `AssetDecision.status`, mirroring relative paths under the input root (`src/faceit_ai/services/flagged_export.py`).
- Sources already under **`flagged/`** are skipped (no re-export loops).
- **Idempotent:** if the destination file already exists with the **same size** as the source, the export step skips and writes an audit event `event=asset_export`, `action=skip_identical`.
- **Prune before export:** when export is enabled, analyze runs `prune_stale_flagged_exports` first — files under `flagged/blocked/` or `flagged/review/` whose DB decision is no longer that tier (including `ok`) are removed. **Copy** mode deletes stale copies; **move** mode restores the file outside `flagged/` and repoints `Asset.path`.
- Each successful copy/move writes **`log_export_audit`** (`event=asset_export`, `action=copy|move`) to `faceit_ai.audit`.

YAML (optional):

```yaml
export:
  flagged: off # or copy | move when using analyze_photos --export-flagged from-config
  flagged_status:
    - blocked
    - review
```

`export.flagged_status` is reserved for future alignment with CLI defaults; **`--flagged-status`** currently controls which statuses are exported.

### People-folder collect (`collect.people_root` / `--collect-to`)

After analyze, strong face matches (score ≥ `matching.match_threshold_strong`) can be copied into `<people_root>/<person>/` for later manual `register_person`. This is separate from flagged export (blocked/review → `flagged/` under the scan folder).

Optional **`collect.crop_portrait: true`** (off by default; Settings → *Crop portraits for people-folder collect*, or `--collect-crop`) saves face-centered **JPEG portraits** using stored `AssetFace.bbox` coordinates (analyze-space, same decode size as the pipeline). The crop helper **tightens padding** until InsightFace sees exactly one face in the crop; if that fails, the step **falls back to copying the full source file**. RAW-heavy collects add decode time per collected file; JPEG collects are much faster.

### Audit / fix existing people-folder portraits (`audit_people_portraits`)

Use this when portraits were collected before single-face cropping, when collect fell back to full files, or after manual uploads into `people/<name>/`.

Implementation: `src/faceit_ai/services/audit_people_portraits.py` → CLI `audit_people_portraits`.

**People root resolution:** `collect.people_root` if set, else `paths.people_dir` from YAML (web GUI uses the same key).

```bash
source .venv/bin/activate
pip install -e .

# Report: list files where detected face count ≠ 1 (det_score ≥ 0.5)
audit_people_portraits

# One workflow: scan, then re-crop files with ≥ --min-faces (default 2)
audit_people_portraits --fix --dry-run   # preview
audit_people_portraits --fix             # overwrite people-folder JPEGs in place
```

| Flag | Effect |
|------|--------|
| `--people-root PATH` | Override configured people folder |
| `--fix` | After scan, re-crop multi-face portraits |
| `--dry-run` | With `--fix`, print planned actions only |
| `--min-faces N` | Re-crop when ≥ N faces detected (default **2**) |
| `--quiet` | Suppress progress bar; print problems / fix rows only |

**Re-crop logic:** lookup `collected_photo` for destination path → prefer original `source_path` + `AssetFace.bbox` for that person/asset → else detect on source or local file and pick the face best matching stored embeddings → `write_single_face_portrait` (same helper as collect/review).

**Exit codes:** `0` OK; `1` config/IO error; `2` problems remain (audit) or some fixes failed (`--fix`).

**After `--fix`:** run **Re-register** for affected people in the UI so gallery embeddings match the new crops.

**Performance:** full-folder scan on CPU is ~2 minutes per ~500 JPEGs; `--fix` runs detection again on each problem file. Progress bars use `tqdm`; InsightFace model load lines are not the end of the run.

### Archive before analyze (`ingest` / `--ingest-to`)

Separate from **Export flagged** (§7 above): optional **full-folder copy** from source (e.g. SD card) to a NAS destination **before** face analysis runs.

- **Copy-only** — source files are never deleted or moved.
- **Analyze runs on the NAS copy** — DB `Asset.path`, flagged export, people collect, and metadata sync all use `{destination_root}/{source_folder_basename}/`.
- **Idempotent** — destination files with the same size as the source are skipped (audit `event=folder_ingest`, `action=skip_identical`).
- Files under **`flagged/`** on the source are not copied (avoids re-archiving prior exports).
- Enable via Settings checkbox (**Archive copy**) or CLI **`--ingest-to PATH`**. The destination is chosen **per analyze run** on the Analyze page.
- **Order** (`ingest.order` / `--ingest-order`):
  - **`copy_then_analyze`** (default): copy source to archive first, then analyze the archive copy. DB paths, metadata sync, and **`flagged/`** export all use the **archive copy** path.
  - **`analyze_then_copy`**: analyze the **source** first ( **`flagged/`** created on source), then copy the **entire** tree—including `flagged/`—to the archive destination.

YAML:

```yaml
ingest:
  enabled: false
  order: copy_then_analyze   # or analyze_then_copy
```

Example (`copy_then_analyze`): source `/path/to/source-folder` with `--ingest-to /path/to/destination-folder` → archive `/path/to/destination-folder/source-folder/` → analyze and `flagged/` on the archive copy.

Example (`analyze_then_copy`): analyze on source → `flagged/` under source → full tree copied to `/path/to/destination-folder/source-folder/` (includes `flagged/`).

Implementation: `src/faceit_ai/services/folder_ingest.py`. Folder claims on the shared DB use the **post-ingest** scan path so two PCs do not analyze the same NAS folder in parallel.

---

## 8. Batch accounting line

```text
This run batch: newly analyzed=A, skipped (already in DB)=B, decode errors=C, total listed=D
```

- **newly analyzed** — files that went through vision + DB write in this invocation (subject to `--force` and decode success).
- **skipped** — cache hit: asset already decided and not forced.
- **decode errors** — `ImageDecodeError` (RAW/OpenCV); see audit `reason: decode_failed`.
- **total listed** — files discovered by the folder walk (extensions + ignore rules in config).

`db_all_time | total_assets_with_decision` can differ from the batch line **`files listed in folder`** because it counts **all** rows in `asset_decision` for that database, not only this folder.

---

## 9. Example session (shoot folder)

Example layout:

```text
/path/to/photos/2025_Event_Day1/Camera_A
```

Typical sequence:

```bash
source .venv/bin/activate
set_person_consent "alice" --revoke
analyze_photos "/path/to/photos/2025_Event_Day1/Camera_A" --usage social --force
```

Images stay in the shoot folder; project artifacts stay under `faceit_ai/data/`, `faceit_ai/logs/`, etc., per §2.

---

## 10. Review gallery (web UI)

**Page:** `Review` in the web UI (`/review`).

1. Enter the same shoot folder used for **Analyze** and click **Load photos**.
2. Switch **Review** / **Blocked** tabs (counts from DB for that folder).
3. Click a thumbnail to open the gallery viewer; use **Previous / Next** or arrow keys to step through photos.
4. For each face on a **Review** photo, pick a person from the dropdown, or leave **— Unknown person —** if the face is a stranger.
5. **Move to OK (unknown OK)** (Review tab): clears review → `ok`, `reason = cleared_from_review`, `manual_override = true` — use when unknown/stranger faces are acceptable.
6. **Move to blocked** (Review tab): pick a known person for each face you want to block, then confirm:
   - Writes a **portrait crop** to `people/<person>/` (same crop settings as collect),
   - Adds the stored face **embedding** to the reference gallery,
   - Sets `AssetDecision.status = blocked`, `reason = manual_confirm`, `manual_override = true`,
   - Optionally copies/moves to `flagged/blocked/` when `export.flagged` is `copy` or `move`,
   - Optionally applies Lightroom metadata when metadata sync is enabled.

**Multi-face:** One photo can assign several faces to different people in a single confirm; duplicate person names on the same photo use `_f2`, `_f3`, … filename suffixes.

**Re-analyze:** `manual_override` decisions are **not** overwritten by `mark_processed` or consent redecide; face rows are still refreshed on force re-analyze.

---

## 11. SSL / pip in restricted environments

If `pip install -e .` fails while **installing build dependencies** with `SSLCertVerificationError` (or similar), the failure is environmental (corporate proxy, custom CA, sandboxed runner without system trust store). Fix trust store / proxy; using the same shell outside an isolated runner often succeeds. This is not specific to `faceit-ai` package metadata.

---

*Document scope: operational behavior and troubleshooting patterns validated against this codebase; not a duplicate of product specification or GDPR policy text.*
