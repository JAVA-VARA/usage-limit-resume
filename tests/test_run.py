"""TEST-1·2·4·7: hook → 예약 → run → respawn → 예약 정리 흐름."""

import json

from conftest import LIMIT_MSG, kst


def test_hook_then_run_resumes_blocked_job(env):
    """TEST-1 전체 흐름: 백그라운드 작업이 한도에 걸림 → 리셋+2분 예약 → run 이 respawn."""
    env.clock.now = kst(13, 21)
    env.blocked_job("aaaa1111", kst(13, 21))
    env.hook({"error": "rate_limit", "last_assistant_message": LIMIT_MSG, "session_id": "s1"}, job="aaaa1111")
    assert env.calls.schedule == [kst(14, 52)]

    env.clock.now = kst(14, 52)
    env.mock_claude()
    assert env.mod.cmd_run() == 0
    assert env.claude_calls() == [["respawn", "aaaa1111"]]
    assert env.state("aaaa1111")["queuedPrompt"] == env.mod.RESUME_PROMPT
    assert env.calls.unschedule == 1
    assert list(json.loads(env.mod.HISTORY_PATH.read_text(encoding="utf-8"))) == ["aaaa1111"]
    assert not env.mod.LOCK_PATH.exists()


def test_excluded_jobs_are_not_touched(env):
    """TEST-2 손으로 이어 간 작업, done, 답장 대기, 한도가 아닌 차단은 건너뛴다."""
    env.add_job("manual01", timeline=[(kst(13, 21), "blocked", LIMIT_MSG), (kst(13, 40), "working", "")])
    env.add_job("done0001", state="done", timeline=[(kst(13, 21), "done", "")])
    env.add_job("queued01", queuedPrompt="사용자가 남긴 답장", timeline=[(kst(13, 21), "blocked", LIMIT_MSG)])
    env.add_job("perm0001", detail="Waiting for permission", timeline=[(kst(13, 21), "blocked", "perm")])
    env.mock_claude()
    env.mod.cmd_run()
    assert env.claude_calls() == []
    assert env.state("queued01")["queuedPrompt"] == "사용자가 남긴 답장"
    assert env.calls.unschedule == 1


def test_only_due_jobs_resume_and_rest_is_rescheduled(env):
    """TEST-4 리셋 시각이 다른 작업: 지난 것만 재개하고 남은 것의 시각으로 다시 예약."""
    env.blocked_job("early001", kst(13, 21))
    env.blocked_job("late0001", kst(14, 55), "You've hit your session limit · resets 7:50pm (Asia/Seoul)")
    env.mock_claude()
    env.mod.cmd_run()
    assert env.claude_calls() == [["respawn", "early001"]]
    assert env.calls.schedule == [kst(19, 52)]
    assert env.calls.unschedule == 0


def test_hook_keeps_earliest_pending_reset(env):
    """TEST-4 예약은 하나뿐이라, 늦게 걸린 작업의 훅이 먼저 걸린 작업의 이른 리셋을 밀어내지 않는다."""
    env.clock.now = kst(14)
    env.blocked_job("early001", kst(13, 21))
    late = "You've hit your session limit · resets 7:50pm (Asia/Seoul)"
    env.blocked_job("late0001", kst(14), late)
    env.hook({"error": "rate_limit", "last_assistant_message": late}, job="late0001")
    assert env.calls.schedule == [kst(14, 52)]


def test_daily_cap(env):
    env.blocked_job("aaaa1111", kst(13, 21))
    now = env.mod.time.time()
    env.mod.STATE_DIR.mkdir(parents=True)
    env.mod.HISTORY_PATH.write_text(json.dumps({"aaaa1111": [now - 60] * env.mod.MAX_RESUMES_PER_DAY}))
    env.mock_claude()
    env.mod.cmd_run()
    assert env.claude_calls() == []
    assert "24시간 재개 상한" in env.log_text()


def test_dry_run_changes_nothing(env):
    env.blocked_job("aaaa1111", kst(13, 21))
    env.mock_claude()
    env.mod.cmd_status()
    assert env.claude_calls() == []
    assert "queuedPrompt" not in env.state("aaaa1111")
    assert env.calls.schedule == [] and env.calls.unschedule == 0


def test_moved_interactive_session_is_resumed(env):
    """TEST-7 (10-16 사례) 대화형 세션을 한도 대기 중 백그라운드로 옮겨 내장 재개가 취소된 경우."""
    env.interactive_moved("5762c745-0000", "92997cc2-1111", kst(13, 21), cancelled_at=kst(13, 30))
    env.add_job("92997cc2", state="blocked", detail=LIMIT_MSG,
                timeline=[(kst(13, 31), "working", ""), (kst(13, 31), "blocked", LIMIT_MSG)])
    env.mock_claude()
    env.mod.cmd_run()
    assert env.claude_calls() == [["respawn", "92997cc2"]]
    assert "5762c745-0000" not in json.loads(env.mod.INTERACTIVE_PATH.read_text(encoding="utf-8"))


def test_moved_interactive_session_touched_by_user_is_skipped(env):
    env.interactive_moved("5762c745-0000", "92997cc2-1111", kst(13, 21), cancelled_at=kst(13, 30))
    env.add_job("92997cc2", state="working", detail="", timeline=[(kst(13, 45), "working", "")])
    env.mock_claude()
    env.mod.cmd_run()
    assert env.claude_calls() == []


def test_interactive_session_still_waiting_is_rescheduled(env):
    """아직 리셋 전이고 옮겨지지 않은 대화형 세션은 그 리셋 시각에 다시 본다."""
    env.clock.now = kst(14)
    env.interactive_moved("5762c745-0000", "92997cc2-1111", kst(13, 21), cancelled_at=None)
    env.mod.cmd_run()
    assert env.calls.schedule == [kst(14, 52)]


def test_interactive_hook_records_session(env):
    env.clock.now = kst(13, 21)
    env.hook({"error": "rate_limit", "last_assistant_message": LIMIT_MSG, "session_id": "sess-1",
              "transcript_path": "C:/t.jsonl"})
    entry = json.loads(env.mod.INTERACTIVE_PATH.read_text(encoding="utf-8"))["sess-1"]
    assert entry["due"] == kst(14, 52).isoformat()
    assert env.calls.schedule == [kst(14, 52)]
