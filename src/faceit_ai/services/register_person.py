"""Register known persons: extract embeddings from a folder of reference photos."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from sqlalchemy.orm import sessionmaker
from tqdm import tqdm

from faceit_ai.logging_setup import (
    PHASE_CHECK,
    PHASE_END,
    PHASE_START,
    format_elapsed,
    log_run_phase,
)
from faceit_ai.persistence.repository import ConsentRepository
from faceit_ai.persistence.session import session_scope
from faceit_ai.settings import Settings
from faceit_ai.vision.image_loader import (
    ImageDecodeError,
    list_scannable_image_paths,
    load_image_for_pipeline,
)
from faceit_ai.vision.insightface_backend import InsightFaceBackend


def run_register(
    *,
    photo_folder: Path,
    name: str,
    settings: Settings,
    session_factory: sessionmaker[Any],
    backend: InsightFaceBackend,
    consent_given: bool = True,
    usage_social: bool = True,
    usage_web: bool = True,
    usage_internal: bool = True,
    usage_print: bool = True,
    show_progress: bool = True,
) -> int:
    """Returns number of embeddings added."""

    img = settings.pipeline.image
    paths = list_scannable_image_paths(
        photo_folder,
        extensions=img.scan_extensions(),
        ignore_filename_substrings=img.ignore_filename_substrings,
    )
    if not paths:
        raise FileNotFoundError(f"No images under {photo_folder} with supported extensions")

    log = logging.getLogger("faceit_ai")
    t0 = time.perf_counter()
    log_run_phase(
        log,
        PHASE_START,
        "register_person — starting | name=%r | files=%d | folder=%s",
        name,
        len(paths),
        photo_folder,
    )
    count = 0
    path_iter = tqdm(paths, desc="Register", unit="file", disable=not show_progress)
    with session_scope(session_factory) as session:
        repo = ConsentRepository(session)
        person = repo.upsert_person_with_consent(
            name=name,
            consent_given=consent_given,
            usage_social=usage_social,
            usage_web=usage_web,
            usage_internal=usage_internal,
            usage_print=usage_print,
        )
        log_run_phase(
            log,
            PHASE_CHECK,
            "Person record ready (id=%s). Scanning images for faces…",
            person.id,
        )
        for path in path_iter:
            path_iter.set_postfix_str(
                path.name[:36] + ("…" if len(path.name) > 36 else ""), refresh=False
            )
            try:
                loaded_load = load_image_for_pipeline(path, settings.pipeline.image)
            except ImageDecodeError as err:
                log.warning("skip unreadable registration file %s — %s", path, err)
                continue
            faces = backend.analyze(loaded_load.bgr)
            confident = [fd for fd in faces if fd.det_score >= 0.5]
            if len(confident) != 1:
                log.warning(
                    "skip registration file %s — expected 1 face, found %d",
                    path,
                    len(confident),
                )
                continue
            repo.add_embedding(person.id, confident[0].embedding)
            count += 1

    elapsed = time.perf_counter() - t0
    log_run_phase(
        log,
        PHASE_END,
        "register_person — finished in %s | person_id=%s name=%r embeddings_added=%d",
        format_elapsed(elapsed),
        person.id,
        name,
        count,
    )

    if count == 0:
        raise RuntimeError(
            "No embeddings were extracted; provide clearer face photos or lower det threshold in code."
        )
    return count
