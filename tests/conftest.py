"""usage_limit_resume 테스트 공용 fixture.

실제 ~/.claude 와 OS 예약은 건드리지 않는다. 경로 상수는 tmp_path 아래로 바꾸고,
schedule·unschedule 은 호출만 기록한다. 시각은 env.clock.now 로 정한다.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "usage_limit_resume.py"
MOCK = Path(__file__).with_name("mock_claude.py")
KST = ZoneInfo("Asia/Seoul")
LIMIT_MSG = "You've hit your session limit · resets 2:50pm (Asia/Seoul)"


def kst(hour: int, minute: int = 0, day: int = 19, month: int = 10, year: int = 2026) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=KST)


def load_module():
    spec = importlib.util.spec_from_file_location("usage_limit_resume", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def point_home(mod, claude_home: Path) -> None:
    """모듈의 경로 상수를 claude_home(가짜 ~/.claude) 아래로 바꾼다."""
    state = claude_home / "usage-limit-resume"
    mod.HOME = claude_home
    mod.JOBS_DIR = claude_home / "jobs"
    mod.PROJECTS_DIR = claude_home / "projects"
    mod.STATE_DIR = state
    mod.LOG_PATH = state / "log.txt"
    mod.HISTORY_PATH = state / "history.json"
    mod.INTERACTIVE_PATH = state / "interactive.json"
    mod.LOCK_PATH = state / "run.lock"
    mod.CLAUDE_PATH_FILE = state / "claude_path.txt"
    mod.RUNNER_PATH = state / "runner.py"
    mod.ATTEMPTS_PATH = state / "attempts.jsonl"
    mod.RETRY_PATH = state / "retry.json"
    mod.LAUNCH_AGENTS = claude_home.parent / "Library" / "LaunchAgents"


def iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class Clock:
    """가짜 현재 시각. 가짜 claude 가 timeline 에 적는 시각(MOCK_CLAUDE_AT)도 30초 뒤로 맞춘다."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, now: datetime) -> None:
        self._monkeypatch = monkeypatch
        self.now = now

    @property
    def now(self) -> datetime:
        return self._now

    @now.setter
    def now(self, value: datetime) -> None:
        self._now = value
        self._monkeypatch.setenv("MOCK_CLAUDE_AT", iso(value + timedelta(seconds=30)))


class Env:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp_path
        self.monkeypatch = monkeypatch
        self.mod = load_module()
        self.home = tmp_path / "home"
        self.claude_home = self.home / ".claude"
        point_home(self.mod, self.claude_home)
        for var in ("HOME", "USERPROFILE"):
            monkeypatch.setenv(var, str(self.home))
        monkeypatch.setenv("APPDATA", str(self.home / "AppData"))
        for var in ("CLAUDE_JOB_DIR", "CLAUDE_BIN", "ULR_LABEL"):
            monkeypatch.delenv(var, raising=False)

        self.clock = clock = Clock(monkeypatch, kst(15))

        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock.now.astimezone(tz) if tz else clock.now.astimezone().replace(tzinfo=None)

        self.mod.datetime = FakeDatetime
        monkeypatch.setattr(self.mod.time, "sleep", lambda s: None)

        self.calls = SimpleNamespace(schedule=[], unschedule=0)

        def fake_schedule(at: datetime) -> bool:
            self.calls.schedule.append(at)
            return True

        def fake_unschedule() -> None:
            self.calls.unschedule += 1

        self.mod.schedule = fake_schedule
        self.mod.unschedule = fake_unschedule
        self.mod.next_scheduled = lambda: ""
        self.claude_log = tmp_path / "claude_calls.jsonl"

    # ---- 가짜 작업·세션 만들기
    def add_job(self, job_id: str, *, state: str = "blocked", detail: str = LIMIT_MSG,
                timeline: list[tuple[datetime, str, str]] | None = None, **extra) -> Path:
        job_dir = self.mod.JOBS_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        data = {"state": state, "detail": detail, "name": f"job {job_id}", "cwd": str(self.tmp), **extra}
        (job_dir / "state.json").write_text(json.dumps(data), encoding="utf-8")
        lines = [json.dumps({"at": iso(at), "state": st, "detail": d}) for at, st, d in timeline or []]
        (job_dir / "timeline.jsonl").write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
        return job_dir

    def blocked_job(self, job_id: str, blocked_at: datetime, detail: str = LIMIT_MSG) -> Path:
        return self.add_job(job_id, detail=detail, timeline=[
            (blocked_at.replace(minute=0), "working", ""), (blocked_at, "blocked", detail)])

    def state(self, job_id: str) -> dict:
        return json.loads((self.mod.JOBS_DIR / job_id / "state.json").read_text(encoding="utf-8"))

    def append_timeline(self, job_id: str, at: datetime, state: str = "working") -> None:
        with (self.mod.JOBS_DIR / job_id / "timeline.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": iso(at), "state": state}) + "\n")

    def interactive_moved(self, sid: str, new_sid: str, hit_at: datetime, cancelled_at: datetime | None) -> Path:
        """한도에 걸린 대화형 세션 sid 와, cancelled_at 에 백그라운드(new_sid)로 옮긴 transcript."""
        transcript = self.mod.PROJECTS_DIR / "proj" / f"{sid}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        lines = [{"type": "user", "timestamp": iso(hit_at), "message": "hello"}]
        if cancelled_at:
            lines += [
                {"type": "system", "timestamp": iso(cancelled_at),
                 "content": "Automatic continue cancelled (session moved to background)"},
                {"type": "continued-in", "timestamp": iso(cancelled_at), "continuedInSessionId": new_sid},
            ]
        transcript.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
        entries = json.loads(self.mod.INTERACTIVE_PATH.read_text(encoding="utf-8")) \
            if self.mod.INTERACTIVE_PATH.exists() else {}
        entries[sid] = {"hit_at": hit_at.isoformat(), "due": hit_at.replace(hour=14, minute=52).isoformat(),
                        "transcript": str(transcript), "msg": LIMIT_MSG}
        self.mod.STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.mod.INTERACTIVE_PATH.write_text(json.dumps(entries), encoding="utf-8")
        return transcript

    # ---- 실행
    def hook(self, payload: dict, *, job: str | None = None, encoding: str = "utf-8") -> int:
        """register.ts 처럼 UTF-8 바이트로 stdin 을 넘긴다. encoding 은 파이썬이 stdin 을 여는 인코딩."""
        if job:
            self.monkeypatch.setenv("CLAUDE_JOB_DIR", str(self.mod.JOBS_DIR / job))
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw), encoding=encoding))
        return self.mod.cmd_hook()

    def mock_claude(self, rc: int = 0, timeline: bool | None = None) -> Path:
        """CLAUDE_BIN 으로 쓸 가짜 claude 실행 파일을 만든다. timeline 기본값: 성공(rc=0)일 때만 기록을 남긴다."""
        if timeline is None:
            timeline = rc == 0
        bindir = self.tmp / "bin"
        bindir.mkdir(exist_ok=True)
        if os.name == "nt":
            exe = bindir / "claude.cmd"
            exe.write_text(f'@"{sys.executable}" "{MOCK}" %*\r\n@exit /b %errorlevel%\r\n', encoding="utf-8")
        else:
            exe = bindir / "claude"
            exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{MOCK}" "$@"\n', encoding="utf-8")
            exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        self.monkeypatch.setenv("CLAUDE_BIN", str(exe))
        self.monkeypatch.setenv("MOCK_CLAUDE_LOG", str(self.claude_log))
        self.monkeypatch.setenv("MOCK_CLAUDE_RC", str(rc))
        self.monkeypatch.setenv("MOCK_CLAUDE_TIMELINE", "1" if timeline else "0")
        self.monkeypatch.setenv("MOCK_CLAUDE_JOBS", str(self.mod.JOBS_DIR))
        return exe

    def claude_calls(self) -> list[list[str]]:
        if not self.claude_log.exists():
            return []
        return [json.loads(x) for x in self.claude_log.read_text(encoding="utf-8").splitlines()]

    def log_text(self) -> str:
        return self.mod.LOG_PATH.read_text(encoding="utf-8") if self.mod.LOG_PATH.exists() else ""


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    return Env(tmp_path, monkeypatch)
