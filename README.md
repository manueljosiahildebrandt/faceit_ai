# Faceit AI

**Version 0.0.2** — Local GDPR-aware face detection, matching, and consent-based photo decisions. No cloud APIs. Day-to-day work happens in the **browser**; you do not need the command line after setup.

## Requirements

- **Python 3.11–3.13** (recommended). Install once from [python.org](https://www.python.org/downloads/).
  - **Windows:** tick **Add python.exe to PATH** during setup.
- macOS, Windows, or Linux
- **First run only:** network access so InsightFace can download the `buffalo_l` model pack into `~/.insightface` (or `FACEIT_AI_MODEL_ROOT`). After that, you can work offline if models stay on disk.

## Easiest start (Mac / Windows)

1. Clone or download this repository and unzip if needed.
2. Double-click the launcher for your OS:
   - **macOS:** `scripts/Start Faceit AI.command`  
     (First time: Right-click → **Open** if Gatekeeper warns you.)
   - **Windows:** `scripts/Start Faceit AI.bat`
3. The first run creates a virtual environment and installs dependencies (can take several minutes).
4. Your browser opens the web UI. Keep the terminal/console window open while using the app; close it to stop.

On first start, a local `config/default.yaml` is created from `config/default.example.yaml` if missing. **Configure everything in the UI** (Settings / People) — you do not need to edit YAML by hand.

## Configure in the browser

1. Open **Settings**
   - Data folder (optional; default is the project folder)
   - Database URL (leave empty for local SQLite; use Postgres only for multi-PC)
   - Analyze / export / Lightroom / AI model options
2. Open **People** → choose your people folder → scan / register
3. Open **Analyze** → choose a photo folder → Start Analysis

Save settings in the UI; they are stored in your local `config/default.yaml` (not committed to Git).

## Manual install (Linux / advanced)

```bash
git clone <this-repo-url>
cd faceit_ai
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .
cp config/default.example.yaml config/default.yaml
faceit_ai_web
```

Optional shared Postgres driver: `pip install -e ".[postgres]"`.

## Local vs shared database

- **Empty database URL** → local SQLite under `data/` (single machine).
- **Shared multi-PC** → set a SQLAlchemy URL in Settings, preferably with a password in an environment variable:

  ```text
  postgresql+psycopg://facit:${FACIT_DB_PASSWORD}@your-host:5432/facit
  ```

  Then run `init_db` once. Never put real passwords in files that you commit.

## Optional CLI (automation)

```bash
source .venv/bin/activate
register_person ./photos/alice --name "Alice"
analyze_photos ./input --usage social
set_person_consent "Alice" --revoke
```

See [docs/operations-technical.md](docs/operations-technical.md) for matching, export, Lightroom/ExifTool, and multi-PC details.

## What is not in this repository

- People photos and shoot folders
- Databases and face embeddings (`data/` is gitignored)
- Logs
- Local `config/default.yaml` (your machine-specific settings)
- Downloaded InsightFace models (`~/.insightface`)

## Branches

- **`main`** — stable releases
- **`dev`** — ongoing development

## License

MIT
