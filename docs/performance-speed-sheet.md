# faceit_ai speed sheet (reduce seconds per file)

This sheet lists the knobs that change runtime in `analyze_photos`, with practical recommendations for getting below ~2 seconds per file.

---

## What usually costs time per file

In this pipeline, runtime is mostly:

1. **RAW decode** (`rawpy` / LibRaw)  
2. **Face analysis** (InsightFace model inference)  
3. **Metadata writes** (ExifTool process startup + file writes)  
4. **File hashing** (SHA-256 read of each file)  

The biggest wins are usually decode + inference + avoiding unnecessary metadata writes.

---

## Highest-impact knobs (change first)

## 1) RAW decode at half size

Config:

```yaml
pipeline:
  image:
    raw_half_size: true
```

- **Impact:** often large for RAW-heavy folders
- **Trade-off:** lower effective resolution for detection/matching
- **When to use:** speed-sensitive scans where slight recall drop is acceptable

---

## 2) Reduce detector input size

Config:

```yaml
pipeline:
  insightface:
    det_size: [512, 512]   # try 512 first, then 448 if needed
```

- **Impact:** high on CPU inference time
- **Trade-off:** may miss smaller/harder faces
- **When to use:** portraits where faces are reasonably large in frame

---

## 3) Lower image max dimension before inference

Config:

```yaml
pipeline:
  image:
    max_dimension: 1600    # from 2000; test 1400 if still slow
```

- **Impact:** medium to high
- **Trade-off:** less detail for very small faces
- **When to use:** large original files and mostly close/mid shots

---

## 4) Keep metadata verification disabled

Config (already current default):

```yaml
metadata:
  exiftool_verify_after_write: false
```

- **Impact:** medium; removes an extra ExifTool read call
- **Trade-off:** no immediate read-back confirmation in logs
- **Note:** this was a major noise + latency source in your recent runs

---

## 5) If not needed, disable metadata writes during analysis

Config:

```yaml
metadata:
  enabled: false
```

- **Impact:** high if ExifTool writes dominate
- **Trade-off:** no labels/keywords written during `analyze_photos`
- **Workflow:** run `analyze_photos` fast first, then do metadata later with `sync_metadata`

If you still need metadata but want less write overhead:

```yaml
metadata:
  write_keywords: false
  write_color_label: true
  write_photoshop_label_color: false
```

---

## Workflow knobs (often overlooked)

## 6) Do not use `--force` unless needed

- `--force` reprocesses everything (decode + inference + metadata)
- Without `--force`, cached files are skipped after hash check
- Use `--force` only after consent/rule changes that require recompute

---

## 7) Separate analysis from metadata sync

Fast two-step flow:

1. Run `analyze_photos` with metadata disabled (or minimal)
2. Run `sync_metadata <folder>` afterward

This decouples expensive vision work from ExifTool writes and makes bottlenecks easier to tune.

---

## 8) Keep export off while tuning

CLI:

```bash
analyze_photos "<folder>" --usage social --export-flagged off
```

- Copy/move export does extra filesystem work
- Keep it off until speed profile is stable

---

## Hardware/provider knobs

## 9) Use faster inference provider if available

Current config likely:

```yaml
pipeline:
  insightface:
    providers:
      - "CPUExecutionProvider"
```

If your environment supports it, a hardware provider can significantly reduce inference time.  
Only change if provider is correctly installed and stable in your setup.

---

## 10) Keep model warm for batch runs

- First run includes model initialization overhead
- Benchmark on second run (or process enough files) for fair per-file numbers

---

## Suggested tuning profiles

## Profile A: conservative speed-up

```yaml
pipeline:
  insightface:
    det_size: [512, 512]
  image:
    max_dimension: 1800
    raw_half_size: true
metadata:
  exiftool_verify_after_write: false
```

Expected: noticeable speed gain with minimal behavior change.

---

## Profile B: aggressive speed-up

```yaml
pipeline:
  insightface:
    det_size: [448, 448]
  image:
    max_dimension: 1400
    raw_half_size: true
metadata:
  enabled: false
```

Expected: large speed gain; run `sync_metadata` later.

---

## Benchmark method (quick and repeatable)

Use the same folder and compare average seconds/file:

```bash
time analyze_photos "<folder>" --usage social --quiet --export-flagged off
```

Then compute:

- `seconds_per_file = total_elapsed_seconds / files_listed_in_folder`
- Compare baseline vs one knob at a time (avoid changing many at once)

Recommended order:

1. `raw_half_size: true`
2. `det_size` down to `512`
3. `max_dimension` to `1600`
4. metadata off (or reduced writes) if still too slow

---

## Reality check for your current setup

Given your recent logs and behavior, your likely biggest gains are:

1. `raw_half_size: true`
2. lower `det_size` from `640` to `512`
3. lower `max_dimension` from `2000` to `1600`
4. keep `exiftool_verify_after_write: false` (already done)

If you want, next step can be a second sheet with a strict A/B test template (table for baseline and each knob, including accuracy notes).
