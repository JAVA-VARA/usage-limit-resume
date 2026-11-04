"""총괄은 respawn 없이 프로젝트 중계를 돌려놓고, 실행자 예약은 보존한다."""
import json
from pathlib import Path

from conftest import LIMIT_MSG, kst


def test_lead_hook_then_scheduled_return(env):
    root = env.tmp / "project"
    script = root / "scripts/lead_control.py"
    script.parent.mkdir(parents=True)
    # 프로젝트 어댑터의 CLI 경계만 대체한다. 실제 잠금·HTTP는 KQIPS에서 시험한다.
    script.write_text('''import json, sys
from pathlib import Path
p = Path(sys.argv[3]) / "owner.json"
payload = json.load(sys.stdin)
state = json.loads(p.read_text(encoding="utf-8"))
lead = payload.get("session_id") == state["claude_session"]
if lead:
    state["owner"] = "claude" if sys.argv[2] == "resume" else "codex"
    p.write_text(json.dumps(state), encoding="utf-8")
print(json.dumps({"changed":lead, "lead":lead}))
''', encoding="utf-8")
    state = root / "owner.json"
    state.write_text(json.dumps({"owner": "claude", "claude_session": "lead-session"}), encoding="utf-8")
    env.clock.now = kst(13, 21)
    env.hook({"error": "rate_limit", "session_id": "lead-session", "cwd": str(root),
              "last_assistant_message": LIMIT_MSG})
    assert json.loads(state.read_text(encoding="utf-8"))["owner"] == "codex"
    assert env.calls.schedule == [kst(14, 52)]
    env.clock.now = kst(14, 52)
    env.mock_claude()
    assert env.mod.cmd_run() == 0
    assert json.loads(state.read_text(encoding="utf-8"))["owner"] == "claude"
    assert env.claude_calls() == []
    assert not env.mod.load_interactive()
    assert env.calls.unschedule == 1
