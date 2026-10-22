"""context_guard 테스트. 합성 transcript 만 쓰고, 실제 ~/.claude·프로세스·클립보드는 건드리지 않는다."""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "context_guard.py"


@pytest.fixture
def cg(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("context_guard", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.STATE_DIR = tmp_path / "state"
    mod.HANDOFF_DIR = mod.STATE_DIR / "handoff"
    mod.CONFIG_PATH = mod.STATE_DIR / "config.json"
    mod.TITLE_DIR = tmp_path / "titles"
    # 실제 클립보드·프로세스·새 세션에 닿지 않게 기본으로 막아 둔다
    mod.copy_to_clipboard = lambda text: False
    mod.list_processes = lambda: []
    mod.spawn_session = lambda text, cwd: None
    for key in ("CLAUDE_JOB_DIR", "CLAUDE_PID"):
        monkeypatch.delenv(key, raising=False)
    return mod


def assistant(tokens: int, text: str = "답", when: datetime | None = None) -> dict:
    stamp = (when or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")
    return {
        "type": "assistant", "timestamp": stamp, "cwd": "C:/work", "gitBranch": "feat/x",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}],
                    "usage": {"input_tokens": 10, "cache_read_input_tokens": tokens - 10, "cache_creation_input_tokens": 0}},
    }


def user(text: str, **extra) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}, **extra}


def write_transcript(path: Path, entries: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n", encoding="utf-8")
    return path


def run_main(cg, monkeypatch, capsys, transcript: Path, prompt: str, session: str = "s1") -> dict:
    payload = {"transcript_path": str(transcript), "session_id": session, "prompt": prompt, "cwd": str(transcript.parent)}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    cg.main()
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else {}


# --- 토큰 읽기 ---------------------------------------------------------------

def test_last_usage_takes_latest_assistant(cg):
    lines = [json.dumps(assistant(50_000)), json.dumps(user("q")), json.dumps(assistant(70_000)), "깨진 줄"]
    tokens, when = cg.last_usage(lines)
    assert tokens == 70_000 and when is not None


def test_last_usage_without_assistant(cg):
    assert cg.last_usage([json.dumps(user("q"))]) == (0, None)


def test_read_tail_drops_partial_first_line(cg, tmp_path):
    cg.TAIL_BYTES = 10
    path = tmp_path / "t.jsonl"
    path.write_text("0123456789abc\nlast\n", encoding="utf-8")
    assert cg.read_tail(path) == ["last"]


# --- 판정 -------------------------------------------------------------------

def test_decide_thresholds(cg):
    assert cg.decide("hi", 100_000, None, {})[0] == "pass"
    assert cg.decide("hi", 130_000, None, {})[0] == "warn"
    action, message, state = cg.decide("hi", 150_000, None, {})
    assert action == "block" and "150K" in message and "blocked_digest" in state


def test_resend_passes_then_reblocks_after_step(cg):
    _, _, state = cg.decide("hi", 160_000, None, {})
    action, _, state = cg.decide("hi", 160_000, None, state)
    assert action == "pass" and state == {"override_tokens": 160_000}
    assert cg.decide("next", 200_000, None, state)[0] == "warn"
    assert cg.decide("next", 210_000, None, state)[0] == "block"


def test_idle_block(cg):
    action, message, _ = cg.decide("hi", 110_000, 7200, {})
    assert action == "block" and "2.0시간" in message
    assert cg.decide("hi", 90_000, 7200, {})[0] == "pass"


@pytest.mark.parametrize("prompt", [
    "/compact",
    "<task-notification><status>completed</status></task-notification>",
    "Resume. This prompt was sent by the usage-limit-resume plugin.",
])
def test_exempt_prompts_pass(cg, prompt):
    assert cg.decide(prompt, 500_000, 99_999, {})[0] == "pass"


# --- 설정 -------------------------------------------------------------------

def test_load_config_overrides_known_keys(cg):
    cg.STATE_DIR.mkdir(parents=True)
    cg.CONFIG_PATH.write_text(json.dumps({"block_tokens": 200_000, "unknown": 1}), encoding="utf-8")
    cfg = cg.load_config()
    assert cfg["block_tokens"] == 200_000 and "unknown" not in cfg and cfg["warn_tokens"] == 120_000


def test_load_config_broken_file_uses_defaults(cg):
    cg.STATE_DIR.mkdir(parents=True)
    cg.CONFIG_PATH.write_text("{깨짐", encoding="utf-8")
    assert cg.load_config() == cg.DEFAULTS


# --- 이어받기 프롬프트 --------------------------------------------------------

def test_build_handoff_filters_noise(cg, tmp_path):
    cg.TITLE_DIR.mkdir()
    (cg.TITLE_DIR / "s1.txt").write_text("훅 정리\n", encoding="utf-8")
    entries = [
        user("첫 요청"),
        user("요약입니다", isCompactSummary=True),
        user("숨은 줄", isMeta=True),
        user("<command-name>/x</command-name>"),
        user("/compact"),
        user("/review 이 파일"),
        assistant(1000, "옛 답"),
        user("두 번째 요청"),
        assistant(1000, "마지막 답"),
        user("다음 단계"),
    ]
    lines = [json.dumps(e, ensure_ascii=False) for e in entries]
    text = cg.build_handoff(lines, "s1", tmp_path / "t.jsonl", "다음 단계")
    assert "이전 세션명: 훅 정리" in text and "C:/work" in text and "feat/x" in text
    assert "1. 첫 요청\n2. /review 이 파일\n3. 두 번째 요청\n" in text
    for noise in ("요약입니다", "숨은 줄", "command-name", "/compact"):
        assert noise not in text
    assert "마지막 답" in text and "옛 답" not in text
    assert text.split("## 이번에 할 일\n")[1].startswith("다음 단계")


def test_build_handoff_keeps_recent_limit(cg, tmp_path):
    lines = [json.dumps(user(f"요청{i}"), ensure_ascii=False) for i in range(8)]
    text = cg.build_handoff(lines, "s1", tmp_path / "t.jsonl", "새 요청")
    assert "요청2" not in text and "1. 요청3" in text and "5. 요청7" in text


# --- main 흐름 ---------------------------------------------------------------

def test_main_block_without_spawn_shows_handoff(cg, monkeypatch, capsys, tmp_path):
    t = write_transcript(tmp_path / "t.jsonl", [user("첫 요청"), assistant(160_000, "직전 답")])
    out = run_main(cg, monkeypatch, capsys, t, "다음 단계")
    assert out["decision"] == "block"
    assert "이어받기 프롬프트를" in out["reason"] and "직전 답" in out["reason"]
    assert (cg.HANDOFF_DIR / "s1.md").is_file()
    assert "blocked_digest" in json.loads((cg.STATE_DIR / "s1.json").read_text())
    # 같은 프롬프트를 다시 보내면 통과
    assert run_main(cg, monkeypatch, capsys, t, "다음 단계") == {}


def test_main_warn_and_pass(cg, monkeypatch, capsys, tmp_path):
    t = write_transcript(tmp_path / "t.jsonl", [assistant(130_000)])
    assert "systemMessage" in run_main(cg, monkeypatch, capsys, t, "질문")
    t = write_transcript(tmp_path / "t.jsonl", [assistant(10_000)])
    assert run_main(cg, monkeypatch, capsys, t, "질문") == {}


def test_main_config_raises_threshold(cg, monkeypatch, capsys, tmp_path):
    cg.STATE_DIR.mkdir(parents=True)
    cg.CONFIG_PATH.write_text(json.dumps({"warn_tokens": 300_000, "block_tokens": 400_000}), encoding="utf-8")
    t = write_transcript(tmp_path / "t.jsonl", [assistant(200_000)])
    assert run_main(cg, monkeypatch, capsys, t, "질문") == {}


def test_main_busy_children_defers_block(cg, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("CLAUDE_PID", "100")
    cg.list_processes = lambda: [
        {"pid": 200, "ppid": 100, "cmd": "bash -c run"},
        {"pid": 300, "ppid": 200, "cmd": "node codex.js exec"},
    ]
    t = write_transcript(tmp_path / "t.jsonl", [assistant(160_000)])
    out = run_main(cg, monkeypatch, capsys, t, "다음 단계")
    assert "위임 작업이 돌고" in out["systemMessage"]
    assert not (cg.STATE_DIR / "s1.json").exists()


def test_main_spawn_attach_close(cg, monkeypatch, capsys, tmp_path):
    calls = {}
    cg.spawn_session = lambda text, cwd: calls.setdefault("spawn", text) and "abc123"
    cg.open_attach_tab = lambda new_id, cwd: True
    cg.close_old_session = lambda tab_opened: calls.setdefault("close", tab_opened)
    t = write_transcript(tmp_path / "t.jsonl", [assistant(160_000)])
    out = run_main(cg, monkeypatch, capsys, t, "다음 단계")
    assert "새 세션 abc123" in out["reason"] and "새 터미널 탭" in out["reason"] and "자동으로 닫힙니다" in out["reason"]
    assert "## 이번에 할 일\n다음 단계" in calls["spawn"] and calls["close"] is True


def test_main_spawn_without_close_tells_how_to_stop(cg, monkeypatch, capsys, tmp_path):
    cg.spawn_session = lambda text, cwd: "abc123"
    cg.open_attach_tab = lambda new_id, cwd: False
    cg.close_old_session = lambda tab_opened: False
    t = write_transcript(tmp_path / "t.jsonl", [assistant(160_000)])
    reason = run_main(cg, monkeypatch, capsys, t, "다음 단계")["reason"]
    assert "claude attach abc123" in reason and "claude stop abc123" in reason


# --- 프로세스 다루기 ------------------------------------------------------------

def test_has_busy_children(cg):
    cg.list_processes = lambda: [
        {"pid": 2, "ppid": 1, "cmd": "bash"},
        {"pid": 3, "ppid": 2, "cmd": "Codex exec -m x"},
        {"pid": 5, "ppid": 4, "cmd": "codex unrelated"},
    ]
    assert cg.has_busy_children("1") is True
    assert cg.has_busy_children("2") is True
    assert cg.has_busy_children("3") is False


def test_has_busy_children_fails_safe(cg):
    def boom():
        raise OSError("no ps")
    cg.list_processes = boom
    assert cg.has_busy_children("1") is True


def test_close_background_job_stops_it(cg, monkeypatch):
    monkeypatch.setenv("CLAUDE_JOB_DIR", str(Path("jobs") / "ab12cd34"))
    seen = []
    cg.schedule_close = lambda action: seen.append(action) or True
    assert cg.close_old_session(tab_opened=False) is True
    assert seen == [["claude", "stop", "ab12cd34"]]


def test_close_interactive_needs_tab_and_matching_parent(cg, monkeypatch):
    monkeypatch.setenv("CLAUDE_PID", "4242")
    seen = []
    cg.schedule_close = lambda action: seen.append(action) or True
    cg.parent_matches = lambda pid: False
    assert cg.close_old_session(tab_opened=True) is False
    cg.parent_matches = lambda pid: True
    assert cg.close_old_session(tab_opened=False) is False
    assert cg.close_old_session(tab_opened=True) is True
    assert "4242" in seen[0]


def test_close_disabled_by_config(cg, monkeypatch):
    monkeypatch.setenv("CLAUDE_JOB_DIR", "jobs/x")
    cg.CFG["auto_close"] = False
    assert cg.close_old_session(tab_opened=True) is False


def test_spawn_session_parses_id_and_cleans_env(cg, monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("context_guard_fresh", SCRIPT)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent")
    monkeypatch.setenv("CLAUDE_CODE_GIT_BASH_PATH", "keep")
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    seen = {}

    def fake_run(args, **kw):
        seen.update(args=args, env=kw["env"], cwd=kw["cwd"])
        return SimpleNamespace(returncode=0, stdout="backgrounded · 9f8e7d6c\n  claude agents ...")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert fresh.spawn_session("이어받기", str(tmp_path)) == "9f8e7d6c"
    assert seen["args"] == ["claude", "--bg", "이어받기"]
    assert "CLAUDE_CODE_SESSION_ID" not in seen["env"] and seen["env"]["CLAUDE_CODE_GIT_BASH_PATH"] == "keep"

    monkeypatch.setattr(subprocess, "run", lambda args, **kw: SimpleNamespace(returncode=1, stdout="", stderr=""))
    assert fresh.spawn_session("이어받기", str(tmp_path)) is None
    assert fresh.spawn_session("이어받기", str(tmp_path / "없음")) is None


def test_spawn_session_falls_back_to_project_dir(cg, monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("context_guard_fresh", SCRIPT)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    sub = tmp_path / "sub"
    sub.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    tried = []

    def fake_run(args, **kw):
        tried.append(kw["cwd"])
        if kw["cwd"] == str(sub):
            return SimpleNamespace(returncode=1, stdout="", stderr="Workspace not trusted. Run `claude` in ...")
        return SimpleNamespace(returncode=0, stdout="backgrounded · 1a2b3c4d\n", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert fresh.spawn_session("이어받기", str(sub)) == "1a2b3c4d"
    assert tried == [str(sub), str(tmp_path)]

    # 신뢰 거절이 아닌 실패는 다른 폴더에서 다시 띄우지 않는다(세션이 둘 생길 수 있다)
    tried.clear()
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: tried.append(kw["cwd"]) or SimpleNamespace(returncode=1, stdout="", stderr="boom"))
    assert fresh.spawn_session("이어받기", str(sub)) is None
    assert tried == [str(sub)]

    def timeout_run(args, **kw):
        tried.append(kw["cwd"])
        raise subprocess.TimeoutExpired(args, 60)
    tried.clear()
    monkeypatch.setattr(subprocess, "run", timeout_run)
    assert fresh.spawn_session("이어받기", str(sub)) is None
    assert tried == [str(sub)]

    # 작업 폴더가 곧 시작 폴더면 한 번만 시도한다
    tried.clear()
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: tried.append(kw["cwd"]) or SimpleNamespace(returncode=1, stdout="", stderr=""))
    assert fresh.spawn_session("이어받기", str(tmp_path)) is None
    assert tried == [str(tmp_path)]


def test_old_idle_uses_assistant_time(cg, monkeypatch, capsys, tmp_path):
    old = datetime.now(timezone.utc) - timedelta(hours=3)
    t = write_transcript(tmp_path / "t.jsonl", [assistant(110_000, when=old), user("방금 보낸 프롬프트")])
    out = run_main(cg, monkeypatch, capsys, t, "방금 보낸 프롬프트")
    assert out["decision"] == "block" and "시간 쉰 세션" in out["reason"]
