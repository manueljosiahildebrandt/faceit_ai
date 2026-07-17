# Technical Handoff: Lightroom-Compatible Metadata via ExifTool (Faceit AI)

## Objective

Stabilize and simplify the metadata pipeline to ensure **reliable Lightroom Classic compatibility**, focusing on:

- Correct color label application (visual flags)
- Keyword-based filtering
- Minimal, robust XMP writing via ExifTool

This phase explicitly avoids complex or custom XMP schemas and prioritizes **deterministic Lightroom behavior**.

---

## Key Principles

1. **Use ExifTool exclusively**
   - Do not manually generate XMP XML
   - Avoid custom writers except for debugging

2. **Minimize metadata scope**
   - Only write fields Lightroom reliably understands
   - Disable custom namespaces (`sola:*`) for now

3. **Verify writes immediately**
   - Never assume metadata was written correctly
   - Always read back with ExifTool after write

4. **Prefer correctness over completeness**
   - Color labels + keywords are sufficient for workflow
   - Additional metadata can be added later

---

## Required Configuration

```yaml
metadata:
  enabled: true
  writer: exiftool
  write_fields: false
  write_keywords: true
  write_color_label: true

lightroom:
  enable: true
```

---

## Supported Metadata Fields (Phase 1)

### Color Labels

Write BOTH:

- `XMP:Label` → localized label name (e.g. "Rot", "Lila")
- `Photoshop:LabelColor` → stable internal value

Allowed values:

| Status  | XMP:Label (DE) | Photoshop:LabelColor |
|--------|---------------|----------------------|
| blocked | Rot           | red                  |
| review  | Lila          | purple               |
| ok      | (none)        | (none)               |

---

### Keywords

Write:

- `XMP-dc:Subject`
- `XMP-lr:HierarchicalSubject`

Example:

```
sola/status/blocked
sola|status|blocked
```

---

## ExifTool Write Command (Reference)

Example for `blocked`:

```bash
exiftool \
  -XMP:Label="Rot" \
  -Photoshop:LabelColor=red \
  -XMP-dc:Subject+="sola/status/blocked" \
  -XMP-lr:HierarchicalSubject+="sola|status|blocked" \
  file.ARW
```

---

## Critical Implementation Notes

### 1. Remove `-m` Flag

Do NOT use:

```bash
-m
```

Reason:

- Suppresses important write errors
- Can silently skip `Photoshop:LabelColor`

---

### 2. Always Verify After Write

Immediately run:

```bash
exiftool -a -G1 -XMP:Label -Photoshop:LabelColor file.ARW
```

Log result in audit system.

**In `faceit_ai`:** After every successful ExifTool write, `metadata/exiftool_sync.py` runs a read-back (`exiftool -j -a -XMP:Label -Photoshop:LabelColor`) and logs `verify` + `verify_ok` on the `metadata_sync` audit event. `verify_ok` is set when label tags on disk match the payload (when label arguments were written). For manual debugging, the `-a -G1` command above is still the clearest output.

---

### 3. Handle Cached Assets

Metadata is NOT written for skipped files.

Use:

```bash
analyze_photos <folder> --force
```

Or implement:

- metadata-only sync pass from DB (future improvement)

---

### 4. Lightroom Reload Required

After writing metadata:

- User must run:
  - `Metadata → Read Metadata from File`

Lightroom does NOT auto-refresh.

---

### 5. Sidecar vs Embedded Behavior

RAW files may contain:

- embedded XMP
- sidecar `.xmp`

ExifTool must handle both correctly.

Avoid:

- manual XML edits
- direct RAW modification via text tools

---

## Debug Strategy

### Step 1: Ground Truth

In Lightroom:

- Manually set color label (Red, Purple)

### Step 2: Inspect with ExifTool

```bash
exiftool -a -G1 test.ARW
```

Identify:

- exact tag names
- exact values

### Step 3: Match Output

Ensure pipeline writes IDENTICAL tags.

---

## Known Failure Modes

| Issue | Cause |
|------|------|
| Keywords appear, no color | `Photoshop:LabelColor` not written |
| No change in LR | Metadata not reloaded |
| Works for new files only | Missing `--force` |
| Silent failure | `-m` suppresses errors (pipeline writes omit `-m`) |
| `verify_ok: false` in audit | Disk labels differ from intended after write |
| Wrong label text | Localization mismatch |

---

## Implementation status (this repo)

- **No `-m` on writes:** `ExifToolMetadataSync` builds write argv without `-m`.
- **Post-write verify:** After `returncode == 0`, `_verify_labels_read` runs; audit `metadata_sync` includes `verify` (stdout preview, parsed fields) and `verify_ok` when label args were written.
- **Mismatch:** `verify_ok` false logs a WARNING with expected vs read label values.

---

---

## Future Extensions (Not Now)

- Custom `sola:*` namespace via ExifTool config
- Lightroom plugin integration
- Automatic metadata reload via plugin
- Blur flags and additional GDPR metadata

---

## Summary

To achieve reliable Lightroom integration:

- Use **ExifTool only**
- Write **only standard tags**
- Remove silent failure paths
- Verify every write
- Match Lightroom's own metadata exactly

This ensures predictable behavior and eliminates most integration issues.
