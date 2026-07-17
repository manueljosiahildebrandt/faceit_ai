from faceit_ai.inference.providers import device_kind, resolve_onnx_providers


def test_resolve_onnx_providers_auto_skips_coreml() -> None:
    out = resolve_onnx_providers(("auto",))
    assert out[0] in ("CUDAExecutionProvider", "CPUExecutionProvider")
    assert "CoreMLExecutionProvider" not in out


def test_resolve_onnx_providers_explicit_cpu() -> None:
    assert resolve_onnx_providers(("CPUExecutionProvider",)) == ("CPUExecutionProvider",)


def test_resolve_onnx_providers_drops_explicit_coreml() -> None:
    out = resolve_onnx_providers(("CoreMLExecutionProvider",))
    assert out == ("CPUExecutionProvider",)


def test_device_kind_cpu_vs_gpu() -> None:
    assert device_kind(("CPUExecutionProvider",)) == "CPU"
    assert device_kind(("CUDAExecutionProvider",)) == "GPU"
    assert device_kind(("CUDAExecutionProvider", "CPUExecutionProvider")) == "GPU"
