"""IMPROVE-2 doctor. 실제 예약은 걸지 않는다(시험 예약 함수를 가짜로 바꾼다)."""

import json


def test_doctor_reports_ok_and_problems(env, monkeypatch, capsys):
    env.mock_claude()
    monkeypatch.setattr(env.mod, "check_scheduler", lambda: (True, "가짜 예약기"))
    env.mod.JOBS_DIR.mkdir(parents=True)
    env.mod.install_runner()
    env.mod.STATE_DIR.mkdir(parents=True, exist_ok=True)
    env.mod.LOG_PATH.write_text("2026-10-19 14:52:01 run: 후보 6개(백그라운드로 옮겨진 대화형 0), 판단 대기 대화형 0\n",
                                encoding="utf-8")
    env.mod.ATTEMPTS_PATH.write_text(json.dumps({"at": "2026-10-19T14:52:30+09:00", "id": "aaaa1111",
                                                 "result": "resumed"}) + "\n", encoding="utf-8")
    assert env.mod.cmd_doctor() == 0
    out = capsys.readouterr().out
    assert "[OK] claude respawn" in out
    assert "[OK] 예약 권한: 가짜 예약기" in out
    assert "run: 후보 6개" in out
    assert "aaaa1111 resumed" in out


def test_doctor_flags_missing_claude_and_stale_runner(env, monkeypatch, capsys):
    monkeypatch.setattr(env.mod, "find_claude", lambda: None)
    monkeypatch.setattr(env.mod, "check_scheduler", lambda: (False, "권한 없음"))
    monkeypatch.setattr(env.mod, "next_scheduled", lambda: "2026-10-19 14:52")
    env.mod.STATE_DIR.mkdir(parents=True)
    env.mod.RUNNER_PATH.write_text("old", encoding="utf-8")
    env.mod.cmd_doctor()
    out = capsys.readouterr().out
    assert "[!!] claude: 못 찾음" in out
    assert "[!!] 예약 권한: 권한 없음" in out
    assert "[!!] 예약 실행 사본: 옛 버전" in out
    assert "[!!] 작업 폴더 읽기" in out
