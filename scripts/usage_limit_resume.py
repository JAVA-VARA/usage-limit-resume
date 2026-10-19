"""세션 한도(usage limit)로 멈춘 백그라운드 작업을 한도가 풀리면 자동으로 재개한다.

흐름
1. StopFailure 훅(matcher: rate_limit)이 `hook` 으로 부른다. 멈춘 메시지의
   "resets 1:40pm (Asia/Seoul)" 을 읽어 그 시각 + 여유분에 Windows 작업 스케줄러
   1회 작업을 등록한다(훅 프로세스가 죽어도, 작업이 stop 되어도 살아남게).
2. 그 시각에 스케줄러가 `run` 을 부른다. ~/.claude/jobs/*/state.json 을 훑어
   "한도로 blocked" 이고 리셋 시각이 지난 작업만 골라 queuedPrompt 를 적고
   `claude respawn <id>` 로 같은 작업 ID 그대로 다시 띄운다.
3. 아직 리셋 전인 작업이 남으면 가장 이른 시각으로 다시 예약한다.

- 사용자가 먼저 손으로 이어 간 작업은 state 가 바뀌어 있으므로 건드리지 않는다
- 같은 작업은 24시간에 MAX_RESUMES_PER_DAY 번까지만 자동 재개한다(한도 반복 방지)
- 대화형 세션은 내장 autoContinueAtUsageLimit 가 맡는다. 다만 한도 대기 중에 백그라운드로
  옮기면 내장 기능이 "Automatic continue cancelled" 로 취소된다. 그래서 훅이 대화형 세션도
  interactive.json 에 적어 두고, run 이 원래 기록의 continued-in 으로 이어받은 작업을 찾아
  그 뒤 아무도 손대지 않았으면 재개한다

명령: hook | run | scan(무엇을 재개할지 출력만) | status
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HOME = Path.home() / ".claude"
JOBS_DIR = HOME / "jobs"
STATE_DIR = HOME / "usage-limit-resume"
LOG_PATH = STATE_DIR / "log.txt"
HISTORY_PATH = STATE_DIR / "history.json"
INTERACTIVE_PATH = STATE_DIR / "interactive.json"  # 한도에 걸린 대화형 세션
PROJECTS_DIR = HOME / "projects"
LOCK_PATH = STATE_DIR / "run.lock"
CLAUDE_EXE = Path.home() / ".local" / "bin" / "claude.exe"
PYTHONW = Path(sys.executable).with_name("pythonw.exe")
TASK_NAME = "ClaudeUsageLimitResume"

GRACE = timedelta(minutes=2)  # 리셋 직후 바로 부르면 아직 막혀 있을 수 있다
FALLBACK_WAIT = timedelta(minutes=30)  # 리셋 시각을 못 읽었을 때
MOVE_SETTLE = timedelta(minutes=2)  # 백그라운드로 옮긴 직후 작업이 스스로 남기는 기록은 무시
MAX_RESUMES_PER_DAY = 3
RESUME_PROMPT = (
    "세션 한도가 풀려 자동으로 재개되었습니다. 멈추기 직전에 하던 작업을 확인하고 "
    "이어서 진행해. (usage_limit_resume 훅이 보낸 메시지)"
)

LIMIT_RE = re.compile(r"hit your .*limit", re.IGNORECASE)
CANCELLED_RE = re.compile(r"Automatic continue cancelled", re.IGNORECASE)
RESET_RE = re.compile(
    r"resets\s+(?:(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2}),\s+(?:(?P<year>\d{4}),\s+)?)?"
    r"(?P<hour>\d{1,2})(?::(?P<min>\d{2}))?\s*(?P<ampm>am|pm)"
    r"(?:\s*\((?P<tz>[^)]+)\))?",
    re.IGNORECASE,
)
MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def log(msg: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"{stamp} {msg}\n")


def parse_reset(message: str, blocked_at: datetime) -> datetime | None:
    """'resets 1:40pm (Asia/Seoul)' 류를 blocked_at 이후 가장 가까운 시각으로 바꾼다."""
    m = RESET_RE.search(message or "")
    if not m:
        return None
    try:
        tz = ZoneInfo(m["tz"]) if m["tz"] else blocked_at.tzinfo
    except Exception:
        tz = blocked_at.tzinfo
    base = blocked_at.astimezone(tz)
    hour = int(m["hour"]) % 12 + (12 if m["ampm"].lower() == "pm" else 0)
    minute = int(m["min"] or 0)
    if m["mon"]:
        year = int(m["year"]) if m["year"] else base.year
        cand = base.replace(year=year, month=MONTHS.get(m["mon"].title(), base.month),
                            day=int(m["day"]), hour=hour, minute=minute, second=0, microsecond=0)
        if not m["year"] and cand < base - timedelta(days=1):
            cand = cand.replace(year=year + 1)
        return cand
    cand = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if cand <= base:
        cand += timedelta(days=1)
    return cand


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def limit_blocked_at(job_dir: Path, state: dict) -> datetime | None:
    """timeline 에서 마지막 한도 차단 시각을 찾는다. 그 뒤에 다른 기록이 있으면 None."""
    timeline = job_dir / "timeline.jsonl"
    try:
        lines = timeline.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in reversed(lines):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("state") == "blocked" and LIMIT_RE.search(ev.get("detail") or ""):
            return parse_iso(ev.get("at"))
        return None  # 마지막 기록이 한도 차단이 아니다
    return parse_iso(state.get("updatedAt"))


def find_limit_blocked_jobs() -> list[dict]:
    found = []
    if not JOBS_DIR.is_dir():
        return found
    for job_dir in JOBS_DIR.iterdir():
        state_path = job_dir / "state.json"
        if not state_path.is_file():
            continue
        state = read_json(state_path)
        detail = state.get("detail") or ""
        if state.get("state") != "blocked" or not LIMIT_RE.search(detail):
            continue
        if state.get("queuedPrompt"):
            continue  # 이미 사용자 답장이 대기 중이다
        blocked_at = limit_blocked_at(job_dir, state)
        if blocked_at is None:
            continue
        reset = parse_reset(detail, blocked_at) or (blocked_at + FALLBACK_WAIT)
        found.append({
            "id": job_dir.name,
            "name": state.get("name") or job_dir.name,
            "cwd": state.get("cwd"),
            "detail": detail,
            "blocked_at": blocked_at,
            "due": reset + GRACE,
            "state_path": state_path,
            "kind": "job",
        })
    return found


def load_interactive() -> dict[str, dict]:
    data = read_json(INTERACTIVE_PATH)
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def save_interactive(data: dict[str, dict]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    INTERACTIVE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def find_transcript(session_id: str, hint: str | None) -> Path | None:
    if hint and Path(hint).is_file():
        return Path(hint)
    return next(PROJECTS_DIR.glob(f"*/{session_id}.jsonl"), None)


def moved_to_background(transcript: Path, since: datetime) -> tuple[str, datetime] | None:
    """since 이후 내장 자동 재개가 백그라운드 이동으로 취소됐으면 (이어받은 세션 ID, 기준 시각).

    기준 시각 = 이어받은 시각 + MOVE_SETTLE. 이어받은 작업이 만들어지며 남기는 첫 기록을
    사람이 손댄 것으로 오인하지 않게, 그 뒤의 timeline 기록만 '손댐'으로 본다.
    """
    cancelled_at = None
    try:
        lines = transcript.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        at = parse_iso(ev.get("timestamp"))
        if ev.get("type") == "system" and CANCELLED_RE.search(str(ev.get("content") or "")):
            if at and at >= since - timedelta(minutes=1):
                cancelled_at = at
        elif ev.get("type") == "continued-in" and cancelled_at and ev.get("continuedInSessionId"):
            return ev["continuedInSessionId"], max(cancelled_at, at or cancelled_at) + MOVE_SETTLE
    return None


def touched_after(job_dir: Path, at: datetime) -> bool:
    """at 이후 timeline 에 기록이 있으면(사람이 이어 갔거나 이미 돌고 있으면) True."""
    try:
        lines = (job_dir / "timeline.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in reversed(lines):
        try:
            ev_at = parse_iso(json.loads(line).get("at"))
        except ValueError:
            continue
        if ev_at and ev_at > at:
            return True
    return False


def find_moved_interactive_jobs(now: datetime, prune: bool) -> tuple[list[dict], list[datetime]]:
    """한도 대기 중 백그라운드로 옮겨져 내장 재개가 취소된 작업과, 아직 판단 못 한 리셋 시각들."""
    found: list[dict] = []
    waiting: list[datetime] = []
    keep: dict[str, dict] = {}
    for sid, e in load_interactive().items():
        hit_at = parse_iso(e.get("hit_at"))
        due = parse_iso(e.get("due"))
        if not hit_at or not due or now - due > timedelta(hours=24):
            continue  # 오래된 기록은 버린다
        transcript = find_transcript(sid, e.get("transcript"))
        moved = moved_to_background(transcript, hit_at) if transcript else None
        if not moved:
            if due > now:
                waiting.append(due)
                keep[sid] = e  # 아직 대화형으로 대기 중이거나, 나중에 옮겨질 수 있다
            continue  # 리셋이 지났는데 옮겨지지 않았으면 내장 기능이 처리했다
        new_sid, cancelled_at = moved
        job_dir = JOBS_DIR / new_sid[:8]
        state_path = job_dir / "state.json"
        state = read_json(state_path)
        if not state or state.get("queuedPrompt") or state.get("state") == "done":
            continue
        if touched_after(job_dir, cancelled_at):
            continue  # 사람이 먼저 이어 갔다
        keep[sid] = e
        found.append({
            "id": job_dir.name,
            "name": state.get("name") or job_dir.name,
            "cwd": state.get("cwd"),
            "detail": e.get("msg", ""),
            "blocked_at": hit_at,
            "due": due,
            "state_path": state_path,
            "kind": "interactive",
            "cancelled_at": cancelled_at,
            "origin": sid,
        })
    if prune:
        save_interactive(keep)
    return found, waiting


def load_history() -> dict[str, list[float]]:
    data = read_json(HISTORY_PATH)
    cutoff = time.time() - 86400
    return {k: [t for t in v if t > cutoff] for k, v in data.items() if isinstance(v, list)}


def save_history(history: dict[str, list[float]]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps({k: v for k, v in history.items() if v}), encoding="utf-8")


def schedule(at: datetime) -> bool:
    """작업 스케줄러에 1회 작업을 (덮어)등록한다. 로캘에 상관없게 PowerShell 로 등록한다."""
    at_local = at.astimezone().replace(tzinfo=None)
    script = Path(__file__).resolve()
    ps = (
        f"$a = New-ScheduledTaskAction -Execute '{PYTHONW}' -Argument '\"{script}\" run';"
        f"$t = New-ScheduledTaskTrigger -Once -At ([datetime]'{at_local.isoformat(timespec='seconds')}');"
        "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Minutes 10);"
        f"Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $a -Trigger $t -Settings $s -Force | Out-Null"
    )
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, text=True, timeout=60,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        log(f"schedule 실패 {at_local}: {r.stderr.strip()[:300]}")
        return False
    log(f"schedule {at_local}")
    return True


def unschedule() -> None:
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                    f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false -ErrorAction SilentlyContinue"],
                   capture_output=True, text=True, timeout=60,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def resume_job(job: dict) -> bool:
    state = read_json(job["state_path"])
    if job["kind"] == "interactive":
        if state.get("queuedPrompt") or touched_after(job["state_path"].parent, job["cancelled_at"]):
            return False  # 그 사이 바뀌었다
    elif state.get("state") != "blocked" or not LIMIT_RE.search(state.get("detail") or ""):
        return False  # 그 사이 바뀌었다
    state["queuedPrompt"] = RESUME_PROMPT
    tmp = job["state_path"].with_suffix(".json.ulr-tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, job["state_path"])
    try:
        r = subprocess.run([str(CLAUDE_EXE), "respawn", job["id"]],
                           cwd=job["cwd"] if job["cwd"] and Path(job["cwd"]).is_dir() else None,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        rc, out = r.returncode, (r.stdout + r.stderr).strip().replace("\n", " | ")[:300]
    except (OSError, subprocess.TimeoutExpired) as e:
        rc, out = -1, repr(e)[:300]
    log(f"respawn {job['id']} ({job['name']}) rc={rc}: {out}")
    if rc != 0:
        # 남겨 두면 다음 run 이 '사용자 답장 대기'로 보고 영영 건너뛴다
        state = read_json(job["state_path"])
        if state.get("queuedPrompt") == RESUME_PROMPT:
            state.pop("queuedPrompt")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, job["state_path"])
    return rc == 0


def cmd_run(dry: bool = False) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not dry:
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
        except FileExistsError:
            if time.time() - LOCK_PATH.stat().st_mtime < 900:
                log("run: 다른 run 이 진행 중이라 건너뜀")
                return 0
            LOCK_PATH.touch()
    try:
        now = datetime.now().astimezone()
        history = load_history()
        moved, waiting = find_moved_interactive_jobs(now, prune=not dry)
        jobs = find_limit_blocked_jobs() + moved
        pending: list[datetime] = list(waiting)
        if not dry:
            log(f"run: 후보 {len(jobs)}개(백그라운드로 옮겨진 대화형 {len(moved)}), 판단 대기 대화형 {len(waiting)}")
        for job in sorted(jobs, key=lambda j: j["due"]):
            tag = f"{job['id']} ({job['name']}) due {job['due']:%m-%d %H:%M}"
            if job["due"] > now:
                pending.append(job["due"])
                print(f"대기  {tag}")
                continue
            if len(history.get(job["id"], [])) >= MAX_RESUMES_PER_DAY:
                print(f"상한  {tag} — 24시간 {MAX_RESUMES_PER_DAY}회 자동 재개함, 건너뜀")
                if not dry:
                    log(f"skip {tag}: 24시간 재개 상한")
                continue
            print(f"재개  {tag}")
            if dry:
                continue
            if resume_job(job):
                history.setdefault(job["id"], []).append(time.time())
                if job["kind"] == "interactive":
                    entries = load_interactive()
                    entries.pop(job["origin"], None)
                    save_interactive(entries)
            time.sleep(3)  # 동시에 여러 개를 띄우지 않는다
        if not dry:
            save_history(history)
            if pending:
                schedule(min(pending))
            else:
                unschedule()
        return 0
    finally:
        if not dry:
            LOCK_PATH.unlink(missing_ok=True)


def cmd_hook() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    msg = " ".join(str(payload.get(k) or "") for k in ("last_assistant_message", "error_details"))
    job_id = Path(os.environ["CLAUDE_JOB_DIR"]).name if os.environ.get("CLAUDE_JOB_DIR") else None
    log(f"hook error={payload.get('error')} job={job_id} session={payload.get('session_id')} msg={msg[:120]!r}")
    now = datetime.now().astimezone()
    reset = parse_reset(msg, now) or (now + FALLBACK_WAIT)
    if not job_id:
        # 대화형은 내장 기능이 맡지만, 백그라운드로 옮기면 취소되므로 적어 두고 run 이 확인한다
        sid = payload.get("session_id")
        if not sid:
            return 0
        entries = load_interactive()
        entries[sid] = {"hit_at": now.isoformat(), "due": (reset + GRACE).isoformat(),
                        "transcript": payload.get("transcript_path"), "msg": msg[:200]}
        save_interactive(entries)
    # 예약은 하나뿐이라 덮어쓴다. 먼저 걸린 다른 작업의 더 이른 리셋을 밀어내지 않게 최솟값으로 잡는다
    moved, waiting = find_moved_interactive_jobs(now, prune=False)
    dues = ([reset + GRACE] + [j["due"] for j in find_limit_blocked_jobs() + moved if j["id"] != job_id]
            + waiting)
    schedule(max(min(dues), now + timedelta(minutes=1)))
    return 0


def cmd_status() -> int:
    print(f"로그: {LOG_PATH}")
    r = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                        f"(Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue | "
                        "Get-ScheduledTaskInfo | Select-Object -ExpandProperty NextRunTime)"],
                       capture_output=True, text=True, timeout=60)
    print(f"다음 예약: {r.stdout.strip() or '없음'}")
    return cmd_run(dry=True)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    try:
        if cmd == "hook":
            return cmd_hook()
        if cmd == "run":
            return cmd_run()
        if cmd in ("scan", "status"):
            return cmd_status()
    except Exception as e:  # 훅은 세션을 막지 않는다
        log(f"{cmd} 오류: {e!r}")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
