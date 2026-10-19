"""v0.3.0 에서 고친 결함(FIX-1·2·5)과 신뢰성 개선(IMPROVE-1·3·4)의 재현 테스트."""

import json
from datetime import timedelta

from conftest import LIMIT_MSG, kst


def attempts(env) -> list[tuple[str, str]]:
    lines = env.mod.ATTEMPTS_PATH.read_text(encoding="utf-8").splitlines()
    return [(x["id"], x["result"]) for x in map(json.loads, lines)]


# ---- FIX-1 stdin 인코딩

def test_hook_reads_utf8_even_when_stdin_is_cp949(env):
    """한국어 Windows 는 stdin 을 cp949 로 연다. '·' 가 '쨌' 로 깨지거나 디코딩 실패로 세션 기록이 사라지면 안 된다."""
    env.clock.now = kst(13, 21)
    env.hook({"error": "rate_limit", "last_assistant_message": LIMIT_MSG, "session_id": "sess-1"},
             encoding="cp949")
    entry = json.loads(env.mod.INTERACTIVE_PATH.read_text(encoding="utf-8"))["sess-1"]
    assert "limit · resets" in entry["msg"]
    assert "쨌" not in env.log_text()


def test_hook_survives_bytes_cp949_cannot_decode(env):
    """cp949 로는 디코딩이 안 되는 UTF-8(이모지 등)이 섞여도 session_id 를 잃지 않는다."""
    env.clock.now = kst(13, 21)
    env.hook({"error": "rate_limit", "last_assistant_message": LIMIT_MSG + " 🚀", "session_id": "sess-2"},
             encoding="cp949")
    assert "sess-2" in json.loads(env.mod.INTERACTIVE_PATH.read_text(encoding="utf-8"))


def test_hook_with_garbage_stdin_does_not_crash(env, monkeypatch):
    import io
    import sys
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"not json"), encoding="utf-8"))
    assert env.mod.cmd_hook() == 0


# ---- FIX-2 실패하면 백오프로 다시 시도

def test_failed_respawn_is_retried_with_backoff(env):
    env.blocked_job("aaaa1111", kst(13, 21))
    env.mock_claude(rc=1)
    env.mod.cmd_run()
    assert env.claude_calls() == [["respawn", "aaaa1111"]]
    assert "queuedPrompt" not in env.state("aaaa1111")  # 되돌려야 다음 run 이 다시 고른다
    assert env.calls.schedule == [kst(15, 10)]  # 10분 뒤
    assert attempts(env) == [("aaaa1111", "failed")]

    # 재시도 시각 전에 다른 훅이 run 을 앞당겨도 그 작업은 기다린다
    env.clock.now = kst(15, 5)
    env.mod.cmd_run()
    assert len(env.claude_calls()) == 1
    assert env.calls.schedule[-1] == kst(15, 10)

    # 두 번째 실패는 20분 뒤, 세 번째 실패로 상한(3회)에 닿으면 더 예약하지 않는다
    env.clock.now = kst(15, 10)
    env.mod.cmd_run()
    assert env.calls.schedule[-1] == kst(15, 30)
    env.clock.now = kst(15, 30)
    env.mod.cmd_run()
    assert len(env.claude_calls()) == 3
    assert env.calls.unschedule == 1
    assert "retry 안 함" in env.log_text()


def test_retry_then_success_clears_retry(env):
    env.blocked_job("aaaa1111", kst(13, 21))
    env.mock_claude(rc=1)
    env.mod.cmd_run()
    env.clock.now = kst(15, 10)
    env.mock_claude(rc=0)
    env.mod.cmd_run()
    assert attempts(env) == [("aaaa1111", "failed"), ("aaaa1111", "launched"), ("aaaa1111", "resumed")]
    assert json.loads(env.mod.RETRY_PATH.read_text(encoding="utf-8")) == {}
    assert env.calls.unschedule == 1


def test_missing_claude_counts_as_failure(env, monkeypatch):
    monkeypatch.setattr(env.mod, "find_claude", lambda: None)
    env.blocked_job("aaaa1111", kst(13, 21))
    env.mod.cmd_run()
    assert attempts(env) == [("aaaa1111", "failed")]
    assert "claude 를 찾지 못함" in env.log_text()
    assert env.calls.schedule == [kst(15, 10)]


# ---- IMPROVE-1 띄운 것과 다시 돈 것을 나눠 기록

def test_launched_but_not_running_is_unconfirmed_and_retried(env):
    env.blocked_job("aaaa1111", kst(13, 21))
    env.mock_claude(rc=0, timeline=False)  # respawn 은 0 인데 작업이 아무 기록도 안 남김
    env.mod.cmd_run()
    assert attempts(env) == [("aaaa1111", "launched"), ("aaaa1111", "unconfirmed")]
    assert "queuedPrompt" not in env.state("aaaa1111")
    assert env.calls.schedule == [kst(15, 10)]


def test_blocked_again_is_not_resumed(env, monkeypatch):
    """respawn 직후 또 한도에 걸려 blocked 만 남으면 다시 돈 것이 아니다."""
    env.blocked_job("aaaa1111", kst(13, 21))
    env.mock_claude(rc=0, timeline=False)
    real = env.mod.resume_job

    def resume_and_block(job):
        result = real(job)
        env.append_timeline("aaaa1111", kst(15, 0) + timedelta(seconds=20), state="blocked")
        return result

    monkeypatch.setattr(env.mod, "resume_job", resume_and_block)
    env.mod.cmd_run()
    assert attempts(env)[-1] == ("aaaa1111", "unconfirmed")


# ---- IMPROVE-3 history 를 작업마다 저장

def test_history_saved_before_next_job(env, monkeypatch):
    """run 이 중간에 강제로 끝나도(작업 스케줄러 시간 제한) 이미 재개한 작업의 횟수는 남는다."""
    env.blocked_job("aaaa1111", kst(13, 21))
    env.blocked_job("bbbb2222", kst(13, 22))
    env.mock_claude()
    calls = []

    def stop_on_second(job):
        calls.append(job["id"])
        if len(calls) == 2:
            raise KeyboardInterrupt  # 강제 종료 흉내
        return real(job)

    real = env.mod.resume_job
    monkeypatch.setattr(env.mod, "resume_job", stop_on_second)
    try:
        env.mod.cmd_run()
    except KeyboardInterrupt:
        pass
    assert list(json.loads(env.mod.HISTORY_PATH.read_text(encoding="utf-8"))) == calls[:1]
    assert not env.mod.LOCK_PATH.exists()


# ---- IMPROVE-4 리셋 시각을 못 읽으면 대기를 점증

def test_fallback_wait_grows_with_tries(env):
    assert [env.mod.fallback_wait(n) for n in range(4)] == [
        timedelta(minutes=30), timedelta(minutes=60), timedelta(minutes=120), timedelta(minutes=120)]


def test_unparsable_reset_uses_growing_fallback(env):
    env.blocked_job("aaaa1111", kst(14, 0), detail="You've hit your session limit")
    env.mod.STATE_DIR.mkdir(parents=True)
    env.mod.HISTORY_PATH.write_text(json.dumps({"aaaa1111": [env.mod.time.time() - 60]}))
    env.clock.now = kst(14, 10)
    env.mod.cmd_run()
    assert env.calls.schedule == [kst(15, 2)]  # 14:00 + 60분 + 2분


# ---- FIX-5 옮겨진 대화형 작업을 두 번 세지 않는다

def test_moved_job_that_is_also_limit_blocked_counts_once(env):
    env.interactive_moved("5762c745-0000", "92997cc2-1111", kst(13, 21), cancelled_at=kst(13, 30))
    env.add_job("92997cc2", timeline=[(kst(13, 31), "blocked", LIMIT_MSG)])
    env.mock_claude()
    env.mod.cmd_run()
    assert env.claude_calls() == [["respawn", "92997cc2"]]
    assert attempts(env) == [("92997cc2", "launched"), ("92997cc2", "resumed")]
    assert json.loads(env.mod.INTERACTIVE_PATH.read_text(encoding="utf-8")) == {}


# ---- IMPROVE-5 시간 예산

def test_time_budget_fits_task_limit(env):
    """작업 10개를 모두 시간 초과로 기다려도 작업 스케줄러 제한 안에 끝난다."""
    worst = 10 * (env.mod.RESPAWN_TIMEOUT + 3) + env.mod.CONFIRM_WAIT
    assert worst < env.mod.TASK_TIME_LIMIT * 60 < env.mod.LOCK_STALE
