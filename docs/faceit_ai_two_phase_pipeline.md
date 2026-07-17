# Technical Requirement: Two-Phase Processing Pipeline + Conservative Performance Optimization (Faceit AI)

## Objective

Refactor the pipeline to:

1. **Decouple analysis from metadata writing**
2. Improve performance with **low-risk, conservative optimizations**
3. Maintain or slightly improve detection reliability
4. Enable scalable batch workflows for Lightroom integration

---

## PART 1: Two-Phase Processing Architecture

## Goal

Separate the pipeline into:

### Phase 1 — Analysis (fast, no side effects)
### Phase 2 — Metadata Sync (batch, selective)

---

## Phase 1: Analyze Only

### Behavior

- Perform:
  - RAW decode
  - Face detection
  - Face embedding
  - Matching
  - Decision logic

- Do NOT by default:
  - write metadata
  - call ExifTool
  - export files

### Output

Persist results in:
- SQLite DB (`asset_decision`)
- audit log
- optional JSON

### Config

```yaml
metadata:
  enabled: false
```

### CLI Example

```bash
analyze_photos "<folder>" --usage social --export-flagged off
```

Metadata writes can be explicitly enabled per run with:

```bash
analyze_photos "<folder>" --usage social --sync-metadata
```

---

## Phase 2: Metadata Sync

### CLI

```bash
sync_metadata "<folder>" --statuses blocked --statuses review
```

### Behavior

- Load assets from DB
- Filter by:
  - `status IN (blocked, review)` by default
- Apply metadata via ExifTool

### Rules

| Status   | Action |
|----------|--------|
| blocked  | apply color label + keywords |
| review   | apply color label + keywords |
| ok       | skip (default filter excludes it) |

### Metadata Sync notes

- Idempotent behavior is preserved
- Status filtering is configurable with `--statuses`
- Summary now reports `skipped_status` in addition to DB misses/errors

---

## PART 2: Conservative Performance Optimization

Applied with minimal expected accuracy impact.

### 1. RAW Half-Size Decode

```yaml
pipeline:
  image:
    raw_half_size: true
```

### 2. Reduce Detection Input Size

```yaml
pipeline:
  insightface:
    det_size: [512, 512]
```

### 3. Moderate Image Downscaling

```yaml
pipeline:
  image:
    max_dimension: 1800
```

### 4. Disable Metadata During Analysis by default

- Config default remains `metadata.enabled: false`
- `analyze_photos` now also defaults to `--no-sync-metadata`

### 5. Keep ExifTool Verification Disabled

```yaml
metadata:
  exiftool_verify_after_write: false
```

### 6. Disable Export During Analysis

CLI default/expected:

```bash
--export-flagged off
```

---

## PART 3: Expected Performance Impact

## Before

```text
RAW decode -> full res
-> detection (640)
-> metadata write per file
-> optional verify
-> export
```

## After

```text
RAW half-size
-> resized to ~1800px
-> detection (512)
-> no metadata by default in analyze step
-> no export
```

### Expected Gains

- Meaningful seconds/file reduction on RAW-heavy batches
- Lower CPU + I/O usage
- More predictable runtime

---

## PART 4: Workflow Recommendation

## Step 1 — Analyze

```bash
analyze_photos "<folder>" --usage social --export-flagged off
```

## Step 2 — Sync Metadata

```bash
sync_metadata "<folder>" --statuses blocked --statuses review
```

## Step 3 — Lightroom

- Select images
- Run: **Metadata -> Read Metadata from File**

---

## PART 5: Design Principles

- **Separation of concerns**
  - Vision pipeline independent from metadata pipeline

- **DB as source of truth**
  - Decisions stored centrally
  - Metadata is a projection layer

- **Batch over per-file side effects**
  - Avoid expensive operations inside main loop

- **Conservative optimization**
  - Improve speed without breaking detection quality

---

## Acceptance Criteria

1. `analyze_photos` runs without invoking metadata writes by default
2. Performance improves measurably (seconds/file reduced)
3. `sync_metadata` can update only `blocked` and `review` images
4. Lightroom shows correct labels after metadata reload
5. No regression in core face recognition accuracy for typical images

---

## Future Extensions (Not Required Now)

- GPU inference support
- Parallel processing of files
- Metadata-only sync from DB without re-analysis
- Smart batching of ExifTool calls
- Per-person or per-event tuning profiles

---

## Summary

This establishes a cleaner baseline:

- Fast analysis loop
- Controlled metadata application
- Better performance
- Easier debugging and iteration
