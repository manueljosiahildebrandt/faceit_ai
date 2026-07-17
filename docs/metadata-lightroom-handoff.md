# Technical handoff: faceit_ai Lightroom / ExifTool metadata

Context for debugging or extending the metadata pipeline (another AI or engineer).

---

## Architecture (relevant parts)

- **Orchestration:** `faceit_ai.services.analyze_photos.run_analyze` calls `MetadataSyncPort.apply(MetadataWriteRequest)` after a successful vision/decision for each file (not when the file is skipped as “already processed”).
- **Writers:** `faceit_ai.integration.metadata_port.build_metadata_sync()` returns either `NoOpMetadataSync`, legacy **`XmpSidecarMetadataSync`** (`writer: xmp_manual`), or **`ExifToolMetadataSync`** (`writer: exiftool`).
- **ExifTool implementation:** `faceit_ai.metadata.exiftool_sync` — reads existing XMP with `exiftool -j`, builds argv (no `shell=True`, no `-m` on writes), writes keywords via `XMP-dc:Subject` / `XMP-lr:HierarchicalSubject` with `-=` / `+=`. If **`basename.xmp`** already exists next to the RAW, ExifTool read/write targets that file (`exiftool_tagging_kind: existing_sidecar`) so edits **merge** into Lightroom’s XMP. If only the RAW exists, the tool targets the RAW (ExifTool creates a **minimal** new sidecar — fine for interoperability, but the file will not look like a full LR export). **Lightroom Classic** saves color as **paired** tags: **`xmp:Label`** and **`photoshop:LabelColor`**. **Star rating:** **`XMP:Rating`** (0–5); see `metadata.write_rating` / `metadata.xmp_rating_by_status`.
- **Config:** `faceit_ai.settings` loads YAML; repo `config/default.yaml` is preferred over packaged `src/faceit_ai/default_config.yaml` when the repo file exists (`resolve_config_path`).

---

## Issue 1: User edits `config/default.yaml` but behavior does not change

**Cause:** Historically `resolve_config_path()` preferred the packaged `default_config.yaml` next to `settings.py`, so edits under `config/default.yaml` were ignored when the package was installed editable and the packaged file existed.

**Fix:** Prefer `config/default.yaml` at the project root when present, then fall back to packaged defaults. `FACEIT_AI_CONFIG` still overrides.

---

## Issue 2: Metadata never runs for “already in DB” images

**Cause:** In `run_analyze`, if `not force` and `assets.is_fully_processed(sha)`, the loop `continue`s before `metadata.apply(...)`.

**Implication:** Re-running analysis without `--force` does not refresh sidecars for cached assets.

**Mitigation:** Run `analyze_photos … --force` for a full re-analysis + metadata pass, **or** run **`sync_metadata <folder>`** to re-apply metadata from SQLite decisions only (no face analysis, no cache skip).

---

## Issue 3: `metadata_sync` fails with `exiftool_not_found` (audit JSON)

**Cause:** `writer: exiftool` shells out to `metadata.exiftool_path` (default `"exiftool"`). If the binary is not on `PATH` for the process that runs the CLI (Terminal vs GUI, minimal env), `shutil.which` fails.

**Mitigation:** Install ExifTool (e.g. Homebrew) and/or set `metadata.exiftool_path` to an absolute path (e.g. `/opt/homebrew/bin/exiftool` on Apple Silicon).

---

## Issue 4: Log line about `write_fields` vs `exiftool_config_path`

**Cause:** These are **different** settings:

- **`exiftool_path`:** Path to the **ExifTool executable**.
- **`exiftool_config_path`:** Optional ExifTool **`-config`** file defining custom XMP namespaces (e.g. project-specific `sola:*` GDPR fields). ExifTool does not invent arbitrary private XMP tags without a Perl config.

**Implication:** With `write_fields: true` and no config file, the code intentionally skips custom `sola:*` fields and logs; **keywords and standard labels still use built-in tags.**

**Mitigation:** Set `write_fields: false` if only LR keywords + labels matter, or supply a valid ExifTool config and set `exiftool_config_path`.

---

## Issue 5: `sqlite3.IntegrityError: UNIQUE constraint failed: asset.path`

**Cause:** `Asset.path` is **unique**. The same bytes (same SHA-256) can appear under two paths (e.g. `…/testing/foo.ARW` and `…/testing/flagged/blocked/foo.ARW`). `mark_processed` used `find_by_sha256(sha256) or find_by_path(path)`, so it could attach to the row for **copy A** and then set `path` to **copy B** while another row still owned `path` B → UNIQUE violation on `UPDATE asset SET path=…`.

**Fix (implemented):** Prefer `find_by_path(path)` then `find_by_sha256`; if another row already owns `path`, delete the stale row (cascade faces/decision); then delete any **other** rows with the same `sha256` so one row remains per content hash.

**File:** `faceit_ai.persistence.repository.AssetRepository.mark_processed`.

---

## Issue 6: Keywords visible in Lightroom, color label not matching expectations

**Authoritative field for Lightroom Classic:** **`XMP:Label`** must contain the **exact label text** from the catalog’s active color label set (Adobe documents that the configured name is what gets written to metadata). Do not infer strings from UI language alone—**calibrate** (below).

**Optional:** `Photoshop:LabelColor` (lowercase tokens like `red`) can be enabled with `metadata.write_photoshop_label_color: true` for experiments; it is **not** required for Lightroom and must not be treated as the primary success signal.

**Earlier bug (fixed):** With `overwrite_color_labels: false`, label writes were skipped too aggressively when only one of XMP vs Photoshop differed. **Skip** now compares what we actually write: if `write_photoshop_label_color` is false, only **`XMP:Label`** must match to skip.

**Verification (current default):** Post-write read-back verification is now **opt-in** via `metadata.exiftool_verify_after_write`. Default is **`false`** to avoid an extra ExifTool call per file and reduce false mismatches (commonly `Photoshop:LabelColor` read as empty while `XMP:Label` is correct). When enabled, the pipeline reads back `XMP:Label`, optional `Photoshop:LabelColor`, `Subject`, and `HierarchicalSubject`, and logs `verify` / `verify_ok`.

### Calibrating `XMP:Label` strings (required for reliable LR)

**Why `Red` vs `Rot` matters:** `color_labels.blocked: red` only picks the **hue**; the text inside **`<xmp:Label>`** must match your **catalog language**. A **German** Classic catalog expects **`Rot`**, **`Lila`**, etc. If facit writes **`Red`**, the XML looks valid but **Lightroom will not show the red label** until the string matches (your own test: change only `Red` → `Rot` and it works).

`build_metadata_payload` uses **`lightroom.xmp_label_values[status]`** when that key exists; otherwise it falls back to **`metadata.color_labels`** / hue shorthands (English **title-case** `Red`, `Purple`, …). So a German catalog **must** set `xmp_label_values` in the **YAML file that is actually loaded** (see config resolution below).

1. In Lightroom Classic, pick a test RAW already in the target catalog.
2. Assign the desired color labels (e.g. blocked → red, review → purple).
3. **Metadata → Save Metadata to File.**
4. On the file or sidecar, run:  
   `exiftool -a -G1 -XMP:Label yourfile.ARW`
5. Copy the **exact** returned strings into config, for example:

```yaml
lightroom:
  xmp_label_values:
    blocked: "Red"      # English catalog
    review: "Purple"
    ok: ""               # empty = clear label
    # German catalog example:
    # blocked: "Rot"
    # review: "Lila"
    # ok: ""
```

6. **Config file in use:** Resolution order is `FACEIT_AI_CONFIG` → `{FACEIT_AI_DATA_DIR}/config/default.yaml` → walk **up** from the current working directory for `config/default.yaml` → editable-install legacy path → packaged `default_config.yaml`. If you run the CLI from another folder and use a **German** catalog, point `FACEIT_AI_CONFIG` at a YAML that contains **`Rot`** or put that file under `{DATA_DIR}/config/default.yaml`.
7. In Lightroom, use **Metadata → Read Metadata From File** to pick up external changes (LR does not auto-refresh).

---

## Issue 8: Where XMP lives for RAW (embedded vs sidecar)

**Default (current):** `metadata.exiftool_raw_target: embedded` — ExifTool writes labels/keywords/rating into **the camera file** (e.g. ARW) with **`-overwrite_original_in_place`**, same idea as your working embedded XMP block. Audit `mode` is **`embedded_in_raw`**; `metadata_touch` is **`embedded`**.

**Optional sidecar:** Set **`exiftool_raw_target: sidecar`** to use **only** an adjacent **`basename.xmp`** (never writes XMP into the RAW). That restores the earlier sidecar behavior:

1. **Canonical sidecar name:** **`{sidecar_stem}.xmp`** where **`sidecar_stem`** drops a trailing **`_original`** (any case).
2. **Merge vs create:** If that **canonical** `.xmp` exists, ExifTool updates it **in place** with **`-overwrite_original_in_place`**.
3. **Legacy sidecar:** If only **`{raw_stem}.xmp`** exists, facit reads it and writes **`-o {canonical}.xmp`**.
4. **New sidecar:** No file yet → **`-o {canonical}.xmp`** from the RAW (minimal XMP until LR **Save Metadata to File**).

For sidecar mode, audit `exiftool_tagging_kind` is **`sidecar_inplace`**, **`sidecar_new_from_raw`**, or **`sidecar_migrate_from_legacy`**; **`exiftool_sidecar_canonical`** is the intended **`basename.xmp`**.

**LR “full” XMP:** Lightroom’s huge XMP ( **`crs:`**, History, …) still comes from **Metadata → Save Metadata to File** (or catalog sync). Embedded mode updates the **same tag namespaces** facit controls; it does not replace develop settings that only exist in a fat sidecar until LR has written them somewhere.

**`xmpMM:DocumentID` / `InstanceID`:** ExifTool can otherwise mint **new** UUIDs when it rewrites embedded XMP, which breaks Lightroom’s idea of “same document” (`DocumentID` should stay stable; `InstanceID` often changes when metadata is edited). With **`metadata.exiftool_preserve_xmpmm_document_id: true`** (default), facit **re-applies** the pre-write **`xmpMM:DocumentID`** and **`OriginalDocumentID`** (if present). Set **`exiftool_preserve_xmpmm_instance_id: true`** only if you need the **InstanceID** frozen too (unusual; off by default).

Some **EXIF** time stamps or maker-note layout can still shift when the RAW is repacked, even if we only intend to change XMP—that’s the container rewrite, not the keywords themselves. Compare with `exiftool -a -G1 -EXIF:DateTime* -XMP:CreateDate yourfile.ARW` before/after if you care.

**Verify:** `exiftool -a -G1 -XMP:Label -Photoshop:LabelColor -XMP:Rating -XMP-xmpMM:DocumentID yourfile.ARW`

### Finder “Kind”: “XMP Sidecar” vs “Dokument” (or “XML”)—cosmetic

On macOS, the **Art / Kind** column is **not** a precise file-format label. It comes from **Spotlight / UTIs** and **which apps registered the `.xmp` extension** on *your* machine. Lightroom may show something like **XMP Sidecar**; the same valid sidecar processed by facit/ExifTool might show **Dokument** (German for generic “Document”) or **XML** if the system falls back to a broad type like `public.xml`. ExifTool still writes a normal **Adobe XMP packet** (`<?xpacket …?>`, `x:xmpmeta`, `xmlns:x='adobe:ns:meta/'`); **Lightroom reads and writes these sidecars the same way.**

To see what the system thinks the type is (compare LR-created vs facit-updated files):

```bash
mdls -name kMDItemContentType -name kMDItemKind /path/to/file.xmp
```

Installing tools that register a dedicated `.xmp` UTI (e.g. Lightroom, Bridge, some Affinity apps) can change the Kind string **without changing XMP semantics**. There is nothing facit needs to “fix” in the bytes for compatibility—only Finder cosmetics.

---

## Issue 7: `-m` removed from ExifTool writes

Writes no longer use **`-m`**, so minor errors are not silently ignored when setting label tags. Check audit `stderr` on failure.

---

## Audit / observability

Successful or failed metadata attempts emit **`event: metadata_sync`** in `faceit_ai.audit` (`logging_setup.log_metadata_sync`), including `writer`, `mode`, `success`, and `extra` (e.g. `requested_xmp_label`, `actual_xmp_label`, `exiftool_tagging_target` (ExifTool source file), `exiftool_sidecar_canonical`, `exiftool_tagging_kind`, `metadata_touch`, `verify`, `verify_ok`, `stderr` on failure). If `metadata.exiftool_verify_after_write` is `false`, `verify`/`verify_ok` can be null/omitted because no read-back is performed.

---

## Key files

| Area | Path |
|------|------|
| Config resolution | `src/faceit_ai/settings.py` (`resolve_config_path`, `MetadataIntegrationSettings`) |
| Analyze loop + metadata call | `src/faceit_ai/services/analyze_photos.py` |
| Metadata-only sync (DB decisions) | `src/faceit_ai/services/sync_metadata.py`, CLI `sync_metadata` |
| DB asset upsert | `src/faceit_ai/persistence/repository.py` (`mark_processed`) |
| ExifTool sync | `src/faceit_ai/metadata/exiftool_sync.py` |
| Payload / keywords | `src/faceit_ai/metadata/keyword_builder.py` |
| Legacy XMP XML | `src/faceit_ai/integration/xmp_sidecar.py`, `metadata_port.py` |

---

This document should be enough to continue debugging (especially label text calibration, sidecars, or ExifTool tag names) without re-reading the original conversation.
