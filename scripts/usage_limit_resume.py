"""세션 한도(usage limit)로 멈춘 백그라운드 작업을 한도가 풀리면 자동으로 재개한다.

흐름
1. StopFailure 훅(matcher: rate_limit)이 `hook` 으로 부른다. 멈춘 메시지의
   "resets 1:40pm (Asia/Seoul)" 을 읽어 그 시각 + 여유분에 1회 예약을 건다
   (Windows 작업 스케줄러, macOS launchd. 훅 프로세스가 죽어도, 작업이 stop 되어도 살아남게).
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
- respawn 이 실패하거나, 띄웠는데 작업이 다시 돌지 않으면(timeline 에 새 기록 없음) 백오프로 다시 시도한다
- 예약은 플러그인 캐시(버전 번호가 든 경로)가 아니라 ~/.claude/usage-limit-resume/runner.py 사본을 부른다

명령: hook | run | scan(무엇을 재개할지 출력만) | status | doctor(설치·권한 점검)
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
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
CLAUDE_PATH_FILE = STATE_DIR / "claude_path.txt"  # 세션 안에서 찾은 claude 위치(스케줄러는 PATH 가 짧다)
RUNNER_PATH = STATE_DIR / "runner.py"  # 예약이 부르는 이 스크립트의 사본(플러그인 업데이트에도 남는다)
ATTEMPTS_PATH = STATE_DIR / "attempts.jsonl"  # 재개 시도 결과(launched·resumed·unconfirmed·failed)
RETRY_PATH = STATE_DIR / "retry.json"  # 작업 ID → 다음 재시도 시각
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
PYTHONW = Path(sys.executable).with_name("pythonw.exe")
TASK_NAME = "ClaudeUsageLimitResume"
LAUNCHD_PREFIX = "com.github.usage-limit-resume."  # 뒤에 예약 시각(YYYYmmddHHMM)을 붙인다
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"

GRACE = timedelta(minutes=2)  # 리셋 직후 바로 부르면 아직 막혀 있을 수 있다
FALLBACK_WAIT = timedelta(minutes=30)  # 리셋 시각을 못 읽었을 때. 재개할 때마다 2배(최대 4배)
RETRY_BASE = timedelta(minutes=10)  # 재개 실패 뒤 다시 시도까지. 실패할 때마다 2배
MOVE_SETTLE = timedelta(minutes=2)  # 백그라운드로 옮긴 직후 작업이 스스로 남기는 기록은 무시
MAX_RESUMES_PER_DAY = 3  # 실패한 시도도 센다
RESPAWN_TIMEOUT = 60  # 초. 작업 수 × 이 값이 아래 실행 제한 안에 들어가야 한다
CONFIRM_WAIT, CONFIRM_POLL = 60, 5  # 초. respawn 뒤 작업이 다시 돌았는지 timeline 으로 확인
TASK_TIME_LIMIT = 30  # 분. 작업 스케줄러가 run 을 강제로 끝내는 시간
LOCK_STALE = (TASK_TIME_LIMIT + 1) * 60  # 초. 이보다 오래된 run.lock 은 죽은 run 이 남긴 것
ATTEMPTS_KEEP = 500  # attempts.jsonl 에 남길 줄 수
RESUME_PROMPT = (
    "The usage limit has reset and this task was resumed automatically. Check what you were "
    "doing right before it stopped and continue. (sent by the usage-limit-resume plugin)"
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


def fallback_wait(tries: int) -> timedelta:
    """리셋 시각을 못 읽었을 때의 대기. 같은 작업을 이미 tries 번 재개했으면 30→60→120분으로 늘린다
    (일찍 깨어 다시 한도에 걸리며 하루 상한을 써 버리지 않게)."""
    return FALLBACK_WAIT * 2 ** min(tries, 2)


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".ulr-tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_timeline(job_dir: Path) -> list[dict]:
    try:
        lines = (job_dir / "timeline.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out


def limit_blocked_at(job_dir: Path, state: dict) -> datetime | None:
    """timeline 에서 마지막 한도 차단 시각을 찾는다. 그 뒤에 다른 기록이 있으면 None."""
    timeline = read_timeline(job_dir)
    if not timeline:
        return parse_iso(state.get("updatedAt"))
    ev = timeline[-1]
    if ev.get("state") == "blocked" and LIMIT_RE.search(ev.get("detail") or ""):
        return parse_iso(ev.get("at"))
    return None  # 마지막 기록이 한도 차단이 아니다


def find_limit_blocked_jobs(history: dict[str, list[float]] | None = None) -> list[dict]:
    history = history or {}
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
        reset = parse_reset(detail, blocked_at) or (blocked_at + fallback_wait(len(history.get(job_dir.name, []))))
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
    for ev in reversed(read_timeline(job_dir)):
        ev_at = parse_iso(ev.get("at"))
        if ev_at and ev_at > at:
            return True
    return False


def running_since(job_dir: Path, at: datetime) -> bool:
    """at 이후 timeline 에 '막힘'이 아닌 기록이 생겼으면(respawn 한 작업이 실제로 다시 돌면) True."""
    for ev in reversed(read_timeline(job_dir)):
        ev_at = parse_iso(ev.get("at"))
        if ev_at and ev_at > at and ev.get("state") != "blocked":
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


def find_candidates(now: datetime, history: dict[str, list[float]], prune: bool) -> tuple[list[dict], list[datetime]]:
    """재개 후보와 판단 대기 중인 대화형 리셋 시각. 옮겨진 대화형 작업이 그 자체로도 한도 차단이면
    한 번만 센다(백그라운드 쪽 판단을 쓰고, 성공하면 원래 대화형 기록도 지운다)."""
    jobs = find_limit_blocked_jobs(history)
    by_id = {j["id"]: j for j in jobs}
    moved, waiting = find_moved_interactive_jobs(now, prune)
    for m in moved:
        if m["id"] in by_id:
            by_id[m["id"]]["origin"] = m["origin"]
        else:
            jobs.append(m)
    return jobs, waiting


def load_history() -> dict[str, list[float]]:
    data = read_json(HISTORY_PATH)
    cutoff = time.time() - 86400
    return {k: [t for t in v if t > cutoff] for k, v in data.items() if isinstance(v, list)}


def save_history(history: dict[str, list[float]]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps({k: v for k, v in history.items() if v}), encoding="utf-8")


def load_retry() -> dict[str, datetime]:
    now = datetime.now().astimezone()
    out = {}
    for k, v in read_json(RETRY_PATH).items():
        at = parse_iso(v) if isinstance(v, str) else None
        if at and now - at < timedelta(hours=24):  # 하루 넘게 지난 재시도는 버린다
            out[k] = at
    return out


def save_retry(retry: dict[str, datetime]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    RETRY_PATH.write_text(json.dumps({k: v.isoformat() for k, v in retry.items()}), encoding="utf-8")


def record_attempt(job: dict, result: str, note: str = "") -> None:
    """재개 시도 한 단계를 attempts.jsonl 에 한 줄로 남긴다."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    row = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), "id": job["id"],
           "name": job["name"], "kind": job["kind"], "result": result}
    if note:
        row["note"] = note
    with ATTEMPTS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    lines = ATTEMPTS_PATH.read_text(encoding="utf-8").splitlines()
    if len(lines) > ATTEMPTS_KEEP * 2:
        ATTEMPTS_PATH.write_text("".join(f"{x}\n" for x in lines[-ATTEMPTS_KEEP:]), encoding="utf-8")


def last_attempts(n: int) -> list[dict]:
    try:
        lines = ATTEMPTS_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def remember_claude() -> None:
    """세션 안(PATH 가 온전할 때)에서 찾은 claude 위치를 적어 둔다."""
    found = shutil.which("claude")
    if found:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        CLAUDE_PATH_FILE.write_text(found, encoding="utf-8")


def find_claude() -> str | None:
    """CLAUDE_BIN → PATH → 세션에서 적어 둔 위치 → 흔한 설치 위치 순으로 찾는다."""
    try:
        saved = CLAUDE_PATH_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        saved = None
    local_bin = Path.home() / ".local" / "bin"
    cands = [os.environ.get("CLAUDE_BIN"), shutil.which("claude"), saved,
             str(local_bin / "claude.exe"), str(local_bin / "claude"),
             "/opt/homebrew/bin/claude", "/usr/local/bin/claude"]
    if IS_WIN and os.environ.get("APPDATA"):
        cands.append(str(Path(os.environ["APPDATA"]) / "npm" / "claude.cmd"))
    return next((c for c in cands if c and Path(c).is_file()), None)


def install_runner() -> Path:
    """예약이 부를 고정 경로에 이 스크립트를 복사한다. 플러그인 캐시 경로에는 버전 번호가 들어 있어
    예약이 걸린 채 플러그인을 업데이트하면 사라지거나 옛 코드가 돌 수 있다."""
    src = Path(__file__).resolve()
    if src == RUNNER_PATH.resolve():
        return RUNNER_PATH  # 예약된 run 이 다시 예약하는 경우
    data = src.read_bytes()
    if not RUNNER_PATH.is_file() or RUNNER_PATH.read_bytes() != data:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = RUNNER_PATH.with_suffix(".py.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, RUNNER_PATH)
    return RUNNER_PATH


def runner_is_current() -> bool | None:
    """runner.py 가 지금 플러그인의 스크립트와 같은가. 아직 예약을 건 적 없으면 None."""
    if not RUNNER_PATH.is_file():
        return None
    return RUNNER_PATH.read_bytes() == Path(__file__).resolve().read_bytes()


def ps_quote(value: object) -> str:
    """PowerShell 작은따옴표 문자열. 안의 ' 는 '' 로 쓴다(C:\\Users\\O'Brien 같은 경로)."""
    return "'" + str(value).replace("'", "''") + "'"


def schedule(at: datetime) -> bool:
    at_local = at.astimezone().replace(tzinfo=None)
    if IS_MAC:
        return schedule_mac(at_local)
    if not IS_WIN:
        log(f"schedule 건너뜀: 지원하지 않는 OS {sys.platform}")
        return False
    return schedule_win(at_local)


def win_task_command(task_name: str, at_local: datetime, script: Path) -> str:
    """작업 스케줄러에 1회 작업을 (덮어)등록하는 PowerShell. 로캘에 상관없게 cmdlet 으로 등록한다."""
    python = PYTHONW if PYTHONW.is_file() else Path(sys.executable)  # pythonw 가 없는 배포판도 있다
    argument = f'"{script}" run'
    return (
        f"$a = New-ScheduledTaskAction -Execute {ps_quote(python)} -Argument {ps_quote(argument)};"
        f"$t = New-ScheduledTaskTrigger -Once -At ([datetime]'{at_local.isoformat(timespec='seconds')}');"
        "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries "
        f"-DontStopIfGoingOnBatteries -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Minutes {TASK_TIME_LIMIT});"
        f"Register-ScheduledTask -TaskName {ps_quote(task_name)} -Action $a -Trigger $t -Settings $s -Force | Out-Null"
    )


def powershell(command: str) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                          capture_output=True, text=True, timeout=60,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def schedule_win(at_local: datetime) -> bool:
    r = powershell(win_task_command(TASK_NAME, at_local, install_runner()))
    if r.returncode != 0:
        log(f"schedule 실패 {at_local}: {r.stderr.strip()[:300]}")
        return False
    log(f"schedule {at_local}")
    return True


def launchd_plist(label: str, at_local: datetime, script: Path) -> dict:
    """at_local 에 1번 `run` 을 부르는 LaunchAgent. 잠자던 중이면 깨어난 직후 돈다."""
    return {
        "Label": label,
        "ProgramArguments": [sys.executable, str(script), "run"],
        "StartCalendarInterval": {"Month": at_local.month, "Day": at_local.day,
                                  "Hour": at_local.hour, "Minute": at_local.minute},
        # launchd 의 PATH 는 /usr/bin:/bin 뿐이라 npm 으로 깐 claude(node)를 못 찾는다
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", ""), "ULR_LABEL": label},
        "AbandonProcessGroup": True,  # remove_launchd 가 띄운 지연 bootout 이 run 과 함께 죽지 않게
        "StandardOutPath": str(STATE_DIR / "launchd.log"),
        "StandardErrorPath": str(STATE_DIR / "launchd.log"),
    }


def remove_launchd(keep: set[str]) -> None:
    """keep 밖의 예약을 내린다. 지금 도는 run 의 예약(ULR_LABEL)을 바로 내리면 launchd 가
    이 프로세스까지 죽인다. 그래서 run 이 끝날 즈음 내리도록 분리된 프로세스에 맡긴다
    (그대로 두면 StartCalendarInterval 에 연도가 없어 1년 뒤 같은 시각에 다시 돈다)."""
    me = os.environ.get("ULR_LABEL")
    for p in LAUNCH_AGENTS.glob(f"{LAUNCHD_PREFIX}*.plist"):
        label = p.stem
        if label in keep:
            continue
        target = f"gui/{os.getuid()}/{label}"
        if label == me:
            subprocess.Popen(["/bin/sh", "-c", f"sleep 5; launchctl bootout {target}"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        else:
            subprocess.run(["launchctl", "bootout", target], capture_output=True, text=True, timeout=30)
        p.unlink(missing_ok=True)


def schedule_mac(at_local: datetime) -> bool:
    label = LAUNCHD_PREFIX + at_local.strftime("%Y%m%d%H%M")
    path = LAUNCH_AGENTS / f"{label}.plist"
    remove_launchd(keep=set())
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        plistlib.dump(launchd_plist(label, at_local, install_runner()), f)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0 and label != os.environ.get("ULR_LABEL"):
        log(f"schedule 실패 {at_local}: {(r.stderr or r.stdout).strip()[:300]}")
        return False
    # 잠자는 Mac 을 깨우려면 root 가 필요하다. sudoers 에 pmset 을 허용해 둔 경우에만 된다(README)
    w = subprocess.run(["sudo", "-n", "/usr/bin/pmset", "schedule", "wake",
                        at_local.strftime("%m/%d/%y %H:%M:%S")],
                       capture_output=True, text=True, timeout=30)
    log(f"schedule {at_local} (wake {'예약' if w.returncode == 0 else '안 함'})")
    return True


def unschedule() -> None:
    if IS_MAC:
        remove_launchd(keep=set())
        return
    if not IS_WIN:
        return
    powershell(f"Unregister-ScheduledTask -TaskName {ps_quote(TASK_NAME)} -Confirm:$false -ErrorAction SilentlyContinue")


def next_scheduled() -> str:
    if IS_MAC:
        labels = sorted(p.stem for p in LAUNCH_AGENTS.glob(f"{LAUNCHD_PREFIX}*.plist"))
        if not labels:
            return ""
        t = datetime.strptime(labels[0][len(LAUNCHD_PREFIX):], "%Y%m%d%H%M")
        return t.strftime("%Y-%m-%d %H:%M")
    if not IS_WIN:
        return f"(지원하지 않는 OS: {sys.platform})"
    r = powershell(f"(Get-ScheduledTask -TaskName {ps_quote(TASK_NAME)} -ErrorAction SilentlyContinue | "
                   "Get-ScheduledTaskInfo | Select-Object -ExpandProperty NextRunTime)")
    return r.stdout.strip()


def clear_queued(state_path: Path) -> None:
    """우리가 적은 queuedPrompt 를 되돌린다. 남겨 두면 다음 run 이 '사용자 답장 대기'로 보고 영영 건너뛴다."""
    state = read_json(state_path)
    if state.get("queuedPrompt") == RESUME_PROMPT:
        state.pop("queuedPrompt")
        write_json_atomic(state_path, state)


def resume_job(job: dict) -> str | None:
    """queuedPrompt 를 적고 respawn 한다. 'launched'·'failed', 그 사이 상태가 바뀌었으면 None."""
    state = read_json(job["state_path"])
    if job["kind"] == "interactive":
        if state.get("queuedPrompt") or touched_after(job["state_path"].parent, job["cancelled_at"]):
            return None
    elif state.get("state") != "blocked" or not LIMIT_RE.search(state.get("detail") or ""):
        return None
    state["queuedPrompt"] = RESUME_PROMPT
    write_json_atomic(job["state_path"], state)
    claude = find_claude()
    try:
        if not claude:
            raise FileNotFoundError("claude 를 찾지 못함(CLAUDE_BIN 으로 지정 가능)")
        r = subprocess.run([claude, "respawn", job["id"]],
                           cwd=job["cwd"] if job["cwd"] and Path(job["cwd"]).is_dir() else None,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=RESPAWN_TIMEOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        rc, out = r.returncode, (r.stdout + r.stderr).strip().replace("\n", " | ")[:300]
    except (OSError, subprocess.TimeoutExpired) as e:
        rc, out = -1, repr(e)[:300]
    log(f"respawn {job['id']} ({job['name']}) rc={rc}: {out}")
    if rc != 0:
        clear_queued(job["state_path"])
        return "failed"
    return "launched"


def confirm_running(launched: list[tuple[dict, datetime]]) -> list[tuple[dict, str]]:
    """respawn 한 작업이 CONFIRM_WAIT 안에 timeline 에 새 기록을 남겼는지 본다. 'resumed'·'unconfirmed'."""
    done: set[str] = set()
    for i in range(CONFIRM_WAIT // CONFIRM_POLL + 1):
        for job, started in launched:
            if job["id"] not in done and running_since(job["state_path"].parent, started):
                done.add(job["id"])
        if len(done) == len(launched):
            break
        if i < CONFIRM_WAIT // CONFIRM_POLL:
            time.sleep(CONFIRM_POLL)
    return [(job, "resumed" if job["id"] in done else "unconfirmed") for job, _ in launched]


def plan_retry(job: dict, history: dict[str, list[float]], retry: dict[str, datetime], now: datetime) -> datetime | None:
    """실패한 작업의 다음 시도 시각(10→20→40분…). 24시간 상한에 닿았으면 None."""
    tries = len(history.get(job["id"], []))
    if tries >= MAX_RESUMES_PER_DAY:
        retry.pop(job["id"], None)
        log(f"retry 안 함 {job['id']}: 24시간 {MAX_RESUMES_PER_DAY}회 시도함")
        return None
    at = now + RETRY_BASE * 2 ** max(tries - 1, 0)
    retry[job["id"]] = at
    log(f"retry {job['id']} at {at:%m-%d %H:%M}")
    return at


def cmd_run(dry: bool = False) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not dry:
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
        except FileExistsError:
            if time.time() - LOCK_PATH.stat().st_mtime < LOCK_STALE:
                log("run: 다른 run 이 진행 중이라 건너뜀")
                return 0
            LOCK_PATH.touch()
    try:
        now = datetime.now().astimezone()
        history = load_history()
        retry = load_retry()
        jobs, waiting = find_candidates(now, history, prune=not dry)
        pending: list[datetime] = list(waiting)
        launched: list[tuple[dict, datetime]] = []
        if not dry:
            moved = sum(j["kind"] == "interactive" for j in jobs)
            log(f"run: 후보 {len(jobs)}개(백그라운드로 옮겨진 대화형 {moved}), 판단 대기 대화형 {len(waiting)}")
        for job in sorted(jobs, key=lambda j: j["due"]):
            due = max(job["due"], retry.get(job["id"], job["due"]))
            tag = f"{job['id']} ({job['name']}) due {due:%m-%d %H:%M}"
            if due > now:
                pending.append(due)
                print(f"대기  {tag}{' (재시도)' if job['id'] in retry else ''}")
                continue
            if len(history.get(job["id"], [])) >= MAX_RESUMES_PER_DAY:
                print(f"상한  {tag} — 24시간 {MAX_RESUMES_PER_DAY}회 자동 재개함, 건너뜀")
                if not dry:
                    log(f"skip {tag}: 24시간 재개 상한")
                continue
            print(f"재개  {tag}")
            if dry:
                continue
            started = datetime.now().astimezone()
            result = resume_job(job)
            if result is None:
                continue  # 그 사이 사람이 이어 갔거나 상태가 바뀌었다
            history.setdefault(job["id"], []).append(time.time())
            save_history(history)  # 중간에 끊겨도 상한 기록은 남게
            record_attempt(job, result)
            if result == "launched":
                launched.append((job, started))
            else:
                at = plan_retry(job, history, retry, now)
                if at:
                    pending.append(at)
            time.sleep(3)  # 동시에 여러 개를 띄우지 않는다
        for job, result in confirm_running(launched):
            record_attempt(job, result)
            if result == "resumed":
                retry.pop(job["id"], None)
                if job.get("origin"):
                    entries = load_interactive()
                    entries.pop(job["origin"], None)
                    save_interactive(entries)
            else:
                log(f"unconfirmed {job['id']}: respawn 뒤 {CONFIRM_WAIT}초 안에 timeline 기록 없음")
                clear_queued(job["state_path"])
                at = plan_retry(job, history, retry, now)
                if at:
                    pending.append(at)
        if not dry:
            save_retry(retry)
            if pending:
                schedule(min(pending))
            else:
                unschedule()
        return 0
    finally:
        if not dry:
            LOCK_PATH.unlink(missing_ok=True)


def read_hook_payload() -> dict:
    """register.ts 는 UTF-8 로 보낸다. sys.stdin 은 OS 기본 인코딩(한국어 Windows 는 cp949)이라 바이트로 읽는다."""
    stream = getattr(sys.stdin, "buffer", None)
    raw = stream.read() if stream is not None else sys.stdin.read().encode("utf-8")
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def cmd_hook() -> int:
    payload = read_hook_payload()
    msg = " ".join(str(payload.get(k) or "") for k in ("last_assistant_message", "error_details"))
    job_id = Path(os.environ["CLAUDE_JOB_DIR"]).name if os.environ.get("CLAUDE_JOB_DIR") else None
    log(f"hook error={payload.get('error')} job={job_id} session={payload.get('session_id')} msg={msg[:120]!r}")
    remember_claude()
    now = datetime.now().astimezone()
    history = load_history()
    reset = parse_reset(msg, now) or (now + fallback_wait(len(history.get(job_id or "", []))))
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
    jobs, waiting = find_candidates(now, history, prune=False)
    retry = load_retry()
    dues = ([reset + GRACE] + [max(j["due"], retry.get(j["id"], j["due"])) for j in jobs if j["id"] != job_id]
            + waiting)
    schedule(max(min(dues), now + timedelta(minutes=1)))
    return 0


def cmd_status() -> int:
    print(f"로그: {LOG_PATH}")
    print(f"다음 예약: {next_scheduled() or '없음'}")
    print(f"claude: {find_claude() or '못 찾음 — CLAUDE_BIN 환경변수로 지정'}")
    for a in last_attempts(3):
        print(f"최근 시도: {a.get('at', '')[:16]} {a.get('id')} {a.get('result')}")
    return cmd_run(dry=True)


def run_quiet(argv: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, repr(e)


def check_scheduler() -> tuple[bool, str]:
    """1년 뒤 시각으로 시험 예약을 걸었다가 바로 지운다. 실제 예약은 건드리지 않는다."""
    if IS_WIN:
        name = TASK_NAME + "Doctor"
        at = datetime.now().replace(microsecond=0) + timedelta(days=365)
        r = powershell(win_task_command(name, at, Path(__file__).resolve()))
        powershell(f"Unregister-ScheduledTask -TaskName {ps_quote(name)} -Confirm:$false -ErrorAction SilentlyContinue")
        return r.returncode == 0, "작업 스케줄러 등록 가능" if r.returncode == 0 else r.stderr.strip()[:200]
    if IS_MAC:
        rc, out = run_quiet(["launchctl", "print", f"gui/{os.getuid()}"])
        wake, _ = run_quiet(["sudo", "-n", "-l", "/usr/bin/pmset", "schedule", "wake"])
        note = "launchd 사용 가능" if rc == 0 else out[:200]
        return rc == 0, f"{note}; 잠자기 깨우기(pmset) {'허용됨' if wake == 0 else '미설정(README 참고)'}"
    return False, f"지원하지 않는 OS: {sys.platform}"


def writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".ulr-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def cmd_doctor() -> int:
    """설치·권한·예약 상태를 한 번에 점검한다. 고칠 것이 있으면 [!!] 로 표시한다."""
    rows: list[tuple[bool, str, str]] = []

    def check(ok: bool, label: str, detail: str = "") -> None:
        rows.append((ok, label, detail))

    check(sys.version_info >= (3, 9), "Python", f"{sys.version.split()[0]} ({sys.executable})")
    try:
        ZoneInfo("Asia/Seoul")
        check(True, "시간대 데이터", "zoneinfo 사용 가능")
    except Exception:
        check(False, "시간대 데이터",
              "zoneinfo 데이터 없음 — 메시지의 시간대 대신 PC 시간대로 계산함. `pip install tzdata`")
    if IS_WIN:
        check(PYTHONW.is_file(), "pythonw.exe", str(PYTHONW) if PYTHONW.is_file() else "없음 — 예약 실행 때 콘솔 창이 잠깐 뜸")
    claude = find_claude()
    if claude:
        rc, out = run_quiet([claude, "respawn", "--help"])
        check(rc == 0 and "respawn" in out, "claude respawn", f"{claude}" + ("" if rc == 0 else f" (rc={rc}: {out[:120]})"))
    else:
        check(False, "claude", "못 찾음 — CLAUDE_BIN 환경변수로 지정")
    ok, detail = check_scheduler()
    check(ok, "예약 권한", detail)
    check(JOBS_DIR.is_dir() and os.access(JOBS_DIR, os.R_OK), "작업 폴더 읽기", str(JOBS_DIR))
    check(writable(STATE_DIR), "상태 폴더 쓰기", str(STATE_DIR))
    current = runner_is_current()
    scheduled = next_scheduled()
    if current is None:
        check(True, "예약 실행 사본", "아직 예약을 건 적 없음")
    else:
        check(current or not scheduled, "예약 실행 사본",
              str(RUNNER_PATH) if current else "옛 버전 — 다음 한도 훅이 새로 복사함")
    check(True, "다음 예약", scheduled or "없음")
    try:
        last_run = next((x for x in reversed(LOG_PATH.read_text(encoding="utf-8").splitlines()) if " run:" in x), "")
    except OSError:
        last_run = ""
    check(True, "마지막 run", last_run or "기록 없음")
    last = last_attempts(1)
    check(True, "마지막 재개 시도", f"{last[0].get('at')} {last[0].get('id')} {last[0].get('result')}" if last else "기록 없음")

    for ok, label, detail in rows:
        print(f"[{'OK' if ok else '!!'}] {label}: {detail}")
    bad = sum(not ok for ok, _, _ in rows)
    print("문제 없음" if not bad else f"확인할 항목 {bad}개")
    return 0


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
        if cmd == "doctor":
            return cmd_doctor()
    except Exception as e:  # 훅은 세션을 막지 않는다
        log(f"{cmd} 오류: {e!r}")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
