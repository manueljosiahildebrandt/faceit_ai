from faceit_ai.inference import providers as providers_mod
from faceit_ai.inference.providers import device_kind, resolve_onnx_providers


def test_resolve_onnx_providers_auto_skips_coreml() -> None:
    out = resolve_onnx_providers(("auto",))
    assert out[0] in (
        "CUDAExecutionProvider",
        "CPUExecutionProvider",
    )
    assert "CoreMLExecutionProvider" not in out


def test_resolve_onnx_providers_auto_prefers_cuda_over_cpu(monkeypatch) -> None:
    monkeypatch.setattr(providers_mod, "onnxruntime_is_healthy", lambda: True)
    monkeypatch.setattr(
        providers_mod,
        "available_onnx_providers",
        lambda: ("CUDAExecutionProvider", "CPUExecutionProvider"),
    )
    assert resolve_onnx_providers(("auto",)) == ("CUDAExecutionProvider",)


def test_resolve_onnx_providers_auto_uses_cpu_when_only_cpu(monkeypatch) -> None:
    monkeypatch.setattr(providers_mod, "onnxruntime_is_healthy", lambda: True)
    monkeypatch.setattr(
        providers_mod,
        "available_onnx_providers",
        lambda: ("CPUExecutionProvider",),
    )
    assert resolve_onnx_providers(("auto",)) == ("CPUExecutionProvider",)


def test_resolve_onnx_providers_auto_ignores_dml_for_auto(monkeypatch) -> None:
    """Stock ORT path: auto prefers CUDA/CPU, not DirectML."""
    monkeypatch.setattr(providers_mod, "onnxruntime_is_healthy", lambda: True)
    monkeypatch.setattr(
        providers_mod,
        "available_onnx_providers",
        lambda: ("DmlExecutionProvider", "CPUExecutionProvider"),
    )
    assert resolve_onnx_providers(("auto",)) == ("CPUExecutionProvider",)


def test_resolve_onnx_providers_explicit_cpu() -> None:
    assert resolve_onnx_providers(("CPUExecutionProvider",)) == ("CPUExecutionProvider",)


def test_resolve_onnx_providers_drops_explicit_coreml() -> None:
    out = resolve_onnx_providers(("CoreMLExecutionProvider",))
    assert out == ("CPUExecutionProvider",)


def test_device_kind_cpu_vs_gpu() -> None:
    assert device_kind(("CPUExecutionProvider",)) == "CPU"
    assert device_kind(("CUDAExecutionProvider",)) == "GPU"
    assert device_kind(("DmlExecutionProvider",)) == "GPU"
    assert device_kind(("CUDAExecutionProvider", "CPUExecutionProvider")) == "GPU"
