"""ONNX Runtime execution provider selection for InsightFace."""

from __future__ import annotations

# CoreML is intentionally omitted: InsightFace SCRFD (buffalo_l det_10g) hits a
# known ONNX Runtime CoreML shape-rank mismatch at detect time and aborts the run.
# Preference: NVIDIA CUDA when present, else Windows DirectML (any DX12 GPU), else CPU.
_PREFERENCE = (
    "CUDAExecutionProvider",
    "DmlExecutionProvider",
    "CPUExecutionProvider",
)

_COREML = "CoreMLExecutionProvider"
_CPU = "CPUExecutionProvider"
_GPU_PROVIDERS = frozenset(
    {
        "CUDAExecutionProvider",
        "TensorrtExecutionProvider",
        "ROCMExecutionProvider",
        "DmlExecutionProvider",
        "CoreMLExecutionProvider",  # Apple Neural Engine / GPU path (not used for SCRFD)
    }
)

_BROKEN_ORT_HINT = (
    "onnxruntime is installed but broken (missing InferenceSession). "
    "On Windows, repair with:\n"
    "  python -m pip uninstall -y onnxruntime onnxruntime-gpu onnxruntime-directml\n"
    "  python -m pip install \"onnxruntime-directml>=1.17\"\n"
    "Then restart Faceit AI."
)


class OnnxRuntimeBrokenError(RuntimeError):
    """Raised when the onnxruntime package imports but is unusable."""


def onnxruntime_is_healthy() -> bool:
    """True if onnxruntime exposes InferenceSession (real wheel, not a broken stub)."""
    try:
        import onnxruntime as ort

        return hasattr(ort, "InferenceSession") and callable(ort.get_available_providers)
    except Exception:
        return False


def available_onnx_providers() -> tuple[str, ...]:
    try:
        import onnxruntime as ort

        if not hasattr(ort, "InferenceSession"):
            return ()
        return tuple(ort.get_available_providers())
    except Exception:
        return ()


def device_kind(providers: tuple[str, ...] | list[str]) -> str:
    """Return ``GPU`` or ``CPU`` for the primary ONNX provider."""
    for p in providers:
        if p in _GPU_PROVIDERS:
            return "GPU"
        if p == _CPU:
            return "CPU"
    return "CPU"


def resolve_onnx_providers(requested: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Map YAML ``providers`` to a usable ONNX Runtime provider list.

    - ``auto`` (or empty) picks CUDA, then DirectML, else CPU.
      CoreML is skipped (incompatible with InsightFace SCRFD dynamic shapes).
    - Explicit names are kept when available; CoreML is dropped with a warning;
      falls back to CPU if nothing usable remains.
    """
    if not onnxruntime_is_healthy():
        # Still return CPU so callers can log device; load will fail with a clear error.
        return (_CPU,)

    avail = set(available_onnx_providers())
    if not avail:
        return (_CPU,)

    req = [str(p).strip() for p in requested if str(p).strip()]
    if not req or req == ["auto"]:
        for name in _PREFERENCE:
            if name in avail:
                return (name,)
        return (_CPU,)

    dropped_coreml = False
    kept: list[str] = []
    for p in req:
        if p == _COREML:
            dropped_coreml = True
            continue
        if p in avail:
            kept.append(p)
    if dropped_coreml:
        import logging

        logging.getLogger("faceit_ai").warning(
            "CoreMLExecutionProvider is not used with InsightFace (SCRFD/CoreML shape bug). "
            "Using %s instead.",
            ", ".join(kept) if kept else _CPU,
        )
    if kept:
        return tuple(kept)
    return (_CPU,)


def require_healthy_onnxruntime() -> None:
    """Raise ``OnnxRuntimeBrokenError`` with repair steps if ORT is unusable."""
    if onnxruntime_is_healthy():
        return
    raise OnnxRuntimeBrokenError(_BROKEN_ORT_HINT)
