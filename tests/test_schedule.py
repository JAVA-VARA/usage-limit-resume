"""FIX-3 고정 경로 실행 사본, FIX-4 PowerShell 인용, macOS launchd 호출 순서.

실제 작업 스케줄러·launchctl 은 부르지 않는다. subprocess 를 가짜로 바꿔 넘긴 명령만 본다.
"""

import plistlib
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from conftest import load_module, point_home


@pytest.fixture
def real(tmp_path, monkeypatch):
    """schedule 계열을 가짜로 바꾸지 않은 모듈. subprocess 호출은 기록만 한다."""
    mod = load_module()
    point_home(mod, tmp_path / "home" / ".claude")
    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    popens: list[list[str]] = []
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mod.subprocess, "Popen", lambda argv, **kw: popens.append(list(argv)))
    monkeypatch.setattr(mod.os, "getuid", lambda: 501, raising=False)
    monkeypatch.delenv("ULR_LABEL", raising=False)
    mod.calls, mod.popens = calls, popens
    return mod


AT = datetime(2026, 10, 19, 14, 52)


def test_runner_copy_is_installed_and_refreshed(real):
    path = real.install_runner()
    assert path == real.RUNNER_PATH
    assert path.read_bytes() == Path(real.__file__).read_bytes()
    path.write_text("old version", encoding="utf-8")
    assert real.runner_is_current() is False
    real.install_runner()
    assert real.runner_is_current() is True


def test_windows_task_calls_runner_not_plugin_cache(real):
    assert real.schedule_win(AT)
    ps = real.calls[0][-1]
    assert str(real.RUNNER_PATH) in ps
    assert "plugins" not in ps
    assert "-Minutes 30" in ps
    assert "-WakeToRun" in ps and "-StartWhenAvailable" in ps


def test_powershell_quotes_apostrophe_paths(real, monkeypatch):
    """FIX-4 C:\\Users\\O'Brien 같은 경로도 작은따옴표 문자열을 깨지 않는다."""
    pythonw = Path("C:/Users/O'Brien/py/pythonw.exe")
    runner = Path("C:/Users/O'Brien/.claude/usage-limit-resume/runner.py")
    monkeypatch.setattr(real, "PYTHONW", pythonw)
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    ps = real.win_task_command("T", AT, runner)
    assert f"-Execute {real.ps_quote(pythonw)}" in ps
    argument = '"' + str(runner) + '" run'
    assert f"-Argument {real.ps_quote(argument)}" in ps
    assert "O''Brien" in ps
    # 작은따옴표를 짝지어 지우면 '' 밖에 남는 ' 가 없어야 한다(문자열이 중간에 끊기지 않음)
    for piece in ps.split(";"):
        assert piece.replace("''", "").count("'") % 2 == 0


def test_ps_quote():
    mod = load_module()
    assert mod.ps_quote("a'b") == "'a''b'"


def test_mac_schedule_writes_plist_for_runner(real):
    assert real.schedule_mac(AT)
    plist_path = real.LAUNCH_AGENTS / f"{real.LAUNCHD_PREFIX}202610191452.plist"
    plist = plistlib.loads(plist_path.read_bytes())
    assert plist["ProgramArguments"][1:] == [str(real.RUNNER_PATH), "run"]
    assert plist["StartCalendarInterval"] == {"Month": 10, "Day": 19, "Hour": 14, "Minute": 52}
    assert plist["AbandonProcessGroup"] is True
    assert ["launchctl", "bootstrap", "gui/501", str(plist_path)] in real.calls
    assert any(c[:4] == ["sudo", "-n", "/usr/bin/pmset", "schedule"] for c in real.calls)


def test_mac_reschedule_replaces_old_entry(real):
    real.schedule_mac(AT)
    real.schedule_mac(datetime(2026, 10, 19, 19, 52))
    labels = sorted(p.stem for p in real.LAUNCH_AGENTS.glob("*.plist"))
    assert labels == [f"{real.LAUNCHD_PREFIX}202610191952"]
    assert ["launchctl", "bootout", f"gui/501/{real.LAUNCHD_PREFIX}202610191452"] in real.calls


def test_mac_own_entry_is_booted_out_later(real, monkeypatch):
    """지금 도는 run 의 예약을 바로 bootout 하면 launchd 가 run 까지 죽인다. 분리된 sh 가 5초 뒤 내린다."""
    real.schedule_mac(AT)
    label = f"{real.LAUNCHD_PREFIX}202610191452"
    monkeypatch.setenv("ULR_LABEL", label)
    real.calls.clear()
    real.remove_launchd(keep=set())
    assert real.calls == []
    assert real.popens == [["/bin/sh", "-c", f"sleep 5; launchctl bootout gui/501/{label}"]]
    assert not list(real.LAUNCH_AGENTS.glob("*.plist"))
