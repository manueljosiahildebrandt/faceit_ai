"""ORM models matching the specification schema."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Person(Base):
    __tablename__ = "person"

    # Only one active person may hold a given name (prevents two PCs creating a duplicate).
    # Partial unique index so deactivated (active=false) names can be reused/kept in history.
    __table_args__ = (
        Index(
            "uq_person_active_name",
            "name",
            unique=True,
            sqlite_where=text("active = 1"),
            postgresql_where=text("active = true"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    tags_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    consent: Mapped["Consent | None"] = relationship(back_populates="person")
    embeddings: Mapped[list["FaceEmbedding"]] = relationship(back_populates="person")


class Consent(Base):
    """One consent row per person (person_id primary key for simplicity)."""

    __tablename__ = "consent"

    person_id: Mapped[int] = mapped_column(ForeignKey("person.id"), primary_key=True)
    consent_given: Mapped[bool] = mapped_column(Boolean, nullable=False)
    usage_social: Mapped[bool] = mapped_column(Boolean, nullable=False)
    usage_web: Mapped[bool] = mapped_column(Boolean, nullable=False)
    usage_internal: Mapped[bool] = mapped_column(Boolean, nullable=False)
    usage_print: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    person: Mapped["Person"] = relationship(back_populates="consent")


class FaceEmbedding(Base):
    __tablename__ = "face_embedding"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("person.id"), nullable=False, index=True)
    embedding: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    person: Mapped["Person"] = relationship(back_populates="embeddings")


class Asset(Base):
    __tablename__ = "asset"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    faces: Mapped[list["AssetFace"]] = relationship(back_populates="asset")
    decision: Mapped["AssetDecision | None"] = relationship(back_populates="asset")


class AssetFace(Base):
    __tablename__ = "asset_face"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("asset.id"), nullable=False, index=True)
    bbox: Mapped[str] = mapped_column(Text, nullable=False)  # JSON list [x1,y1,x2,y2]
    embedding: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    match_person_id: Mapped[int | None] = mapped_column(ForeignKey("person.id"))
    match_score: Mapped[float | None] = mapped_column(Float)

    asset: Mapped["Asset"] = relationship(back_populates="faces")


class AssetDecision(Base):
    __tablename__ = "asset_decision"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("asset.id"), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(256), nullable=False)
    usage: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    manual_override: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    asset: Mapped["Asset"] = relationship(back_populates="decision")


class ProcessingRun(Base):
    """A folder-analysis claim/heartbeat so several PCs can coordinate on one shared DB.

    - Prevents two machines from analyzing the same folder at once.
    - Gives the UI a "who is running what" view across machines.
    """

    __tablename__ = "processing_run"

    # At most one unfinished run per folder across all machines.
    __table_args__ = (
        Index(
            "uq_processing_run_active_folder",
            "folder_path",
            unique=True,
            sqlite_where=text("finished_at IS NULL"),
            postgresql_where=text("finished_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    folder_path: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    host: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def embedding_to_blob(vec: Any) -> bytes:
    import numpy as np

    arr = np.asarray(vec, dtype=np.float32).ravel()
    n = float(np.linalg.norm(arr))
    if n > 1e-12:
        arr = (arr / np.float32(n)).astype(np.float32)
    return arr.tobytes()


def blob_to_embedding(blob: bytes, dim: int) -> Any:
    import numpy as np

    return np.frombuffer(blob, dtype=np.float32).reshape(dim)
