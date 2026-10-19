"""테스트용 가짜 claude. 받은 인자를 MOCK_CLAUDE_LOG 에 한 줄씩 적고 MOCK_CLAUDE_RC 로 끝난다.

respawn 이고 MOCK_CLAUDE_TIMELINE=1 이면 진짜 respawn 처럼 그 작업 timeline 에 working 한 줄을 덧붙인다.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

args = sys.argv[1:]
with open(os.environ["MOCK_CLAUDE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(args) + "\n")
if args == ["respawn", "--help"]:
    print("Usage: claude respawn <id>|--all")
    sys.exit(0)
if args[:1] == ["respawn"] and os.environ.get("MOCK_CLAUDE_TIMELINE") == "1":
    timeline = Path(os.environ["MOCK_CLAUDE_JOBS"]) / args[1] / "timeline.jsonl"
    with timeline.open("a", encoding="utf-8") as f:
        at = os.environ.get("MOCK_CLAUDE_AT") or datetime.now(timezone.utc).isoformat()
        f.write(json.dumps({"at": at, "state": "working"}) + "\n")
sys.exit(int(os.environ.get("MOCK_CLAUDE_RC", "0")))
