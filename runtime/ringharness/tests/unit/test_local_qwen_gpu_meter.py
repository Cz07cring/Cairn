"""本机连接器：loopback 判定与 gpu_seconds 格式化。"""

from control_api.connectors.local_qwen import (
    format_gpu_seconds,
    is_loopback_inference_base,
)


def test_is_loopback_inference_base():
    assert is_loopback_inference_base("http://127.0.0.1:8001") is True
    assert is_loopback_inference_base("http://localhost:8001/v1") is True
    assert is_loopback_inference_base("http://[::1]:8001") is True
    assert is_loopback_inference_base("https://api.deepseek.com") is False
    assert is_loopback_inference_base("http://192.168.1.2:8001") is False


def test_format_gpu_seconds():
    assert format_gpu_seconds(0) is None
    assert format_gpu_seconds(-1) is None
    assert format_gpu_seconds(1.25) == "1.25"
    assert format_gpu_seconds(2.0) == "2"
    assert format_gpu_seconds(0.001) == "0.001"
