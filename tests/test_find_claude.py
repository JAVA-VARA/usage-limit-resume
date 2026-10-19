"""TEST-8 claude 를 고르는 순서: CLAUDE_BIN → PATH → 세션에서 적어 둔 위치 → 흔한 설치 위치."""

import os

import pytest


def touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def test_claude_bin_wins(env, monkeypatch):
    other = touch(env.tmp / "which" / "claude")
    monkeypatch.setattr(env.mod.shutil, "which", lambda name: str(other))
    bin_ = touch(env.tmp / "bin" / "claude")
    monkeypatch.setenv("CLAUDE_BIN", str(bin_))
    assert env.mod.find_claude() == str(bin_)


def test_saved_path_used_when_path_is_short(env, monkeypatch):
    """작업 스케줄러·launchd 는 PATH 가 짧아 which 가 실패한다. 훅이 적어 둔 위치를 쓴다."""
    saved = touch(env.tmp / "npm" / "claude")
    monkeypatch.setattr(env.mod.shutil, "which", lambda name: None)
    env.mod.STATE_DIR.mkdir(parents=True)
    env.mod.CLAUDE_PATH_FILE.write_text(str(saved), encoding="utf-8")
    assert env.mod.find_claude() == str(saved)


def test_remember_claude_saves_which_result(env, monkeypatch):
    found = touch(env.tmp / "npm" / "claude")
    monkeypatch.setattr(env.mod.shutil, "which", lambda name: str(found))
    env.mod.remember_claude()
    assert env.mod.CLAUDE_PATH_FILE.read_text(encoding="utf-8") == str(found)


def test_local_bin(env, monkeypatch):
    monkeypatch.setattr(env.mod.shutil, "which", lambda name: None)
    exe = touch(env.home / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude"))
    assert env.mod.find_claude() == str(exe)


@pytest.mark.skipif(os.name != "nt", reason="npm 의 claude.cmd 는 Windows 만")
def test_npm_cmd_on_windows(env, monkeypatch):
    monkeypatch.setattr(env.mod.shutil, "which", lambda name: None)
    exe = touch(env.home / "AppData" / "npm" / "claude.cmd")
    assert env.mod.find_claude() == str(exe)


def test_not_found(env, monkeypatch):
    monkeypatch.setattr(env.mod.shutil, "which", lambda name: None)
    if any(os.path.isfile(p) for p in ("/opt/homebrew/bin/claude", "/usr/local/bin/claude")):
        pytest.skip("이 기계에 claude 가 설치돼 있음")
    assert env.mod.find_claude() is None
