# -*- coding: utf-8 -*-
"""Console 一键拉起 Ollama：探活、启动与边界条件（全部 mock，不碰真实服务）。"""
import json
import os
import sys
import urllib.request
from types import SimpleNamespace

_CONSOLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "pipeline_ui", "console")
sys.path.insert(0, _CONSOLE)
try:
    import envinit  # noqa: E402
finally:
    sys.path.remove(_CONSOLE)
    for _m in ("doctor", "paths", "status"):
        sys.modules.pop(_m, None)


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps({"models": [{"name": "qwen3:8b"}]}).encode("utf-8")


def _config(host="http://127.0.0.1:11434"):
    return {"translate": {"ollama": {"host": host, "model": "qwen3:8b"}}}


def test_start_ollama_is_noop_when_already_alive(monkeypatch):
    monkeypatch.setattr(envinit, "_cfg", _config)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: _Response())
    monkeypatch.setattr(envinit.subprocess, "Popen",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("不应重复启动")))

    res = envinit.start_ollama(wait_s=0)

    assert res["ok"] is True
    assert res["started"] is False


def test_start_ollama_launches_detached_and_waits(monkeypatch, tmp_path):
    monkeypatch.setattr(envinit, "_cfg", _config)
    exe = tmp_path / "ollama.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(envinit, "_ollama_executable", lambda: str(exe))
    monkeypatch.setattr(envinit.time, "sleep", lambda _s: None)
    calls = {"probe": 0, "popen": None}

    def fake_urlopen(*args, **kwargs):
        calls["probe"] += 1
        if calls["probe"] == 1:
            raise OSError("connection refused")
        return _Response()

    def fake_popen(cmd, **kwargs):
        calls["popen"] = (cmd, kwargs)
        return SimpleNamespace(pid=4321)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(envinit.subprocess, "Popen", fake_popen)

    res = envinit.start_ollama(wait_s=1)

    assert res["ok"] is True and res["started"] is True and res["pid"] == 4321
    cmd, kwargs = calls["popen"]
    assert cmd == [str(exe), "serve"]
    assert kwargs["env"]["OLLAMA_HOST"] == "127.0.0.1:11434"
    assert kwargs["stdout"] is envinit.subprocess.DEVNULL
    assert kwargs["stderr"] is envinit.subprocess.DEVNULL


def test_start_ollama_rejects_remote_host(monkeypatch):
    monkeypatch.setattr(envinit, "_cfg", lambda: _config("http://192.168.1.20:11434"))
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("offline")))

    res = envinit.start_ollama(wait_s=0)

    assert res["ok"] is False
    assert "不是本机地址" in res["detail"]


def test_start_ollama_reports_missing_executable(monkeypatch):
    monkeypatch.setattr(envinit, "_cfg", _config)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("offline")))
    monkeypatch.setattr(envinit, "_ollama_executable", lambda: "")

    res = envinit.start_ollama(wait_s=0)

    assert res["ok"] is False
    assert "未找到 ollama.exe" in res["detail"]
