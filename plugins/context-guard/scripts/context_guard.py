"""UserPromptSubmit hook: 세션이 너무 커지거나 오래 쉰 큰 세션을 재개하면 새 세션으로 옮긴다.

캐시 읽기가 토큰의 68% 였고, 원인은 며칠씩 이어 쓴 20만~60만 토큰 세션이었다. 7일 시뮬레이션에서
차단 기준을 25만 → 15만으로 내리면 토큰이 약 20% 줄었다(2026-10-20).

규칙(컨텍스트 = 마지막 assistant 응답의 input + cache_read + cache_creation)
1. warn_tokens 이상: 경고 한 줄만 보여 주고 통과
2. block_tokens 이상: 프롬프트를 한 번 막는다
3. 마지막 응답 뒤 idle_seconds 넘게 지났고 idle_tokens 이상: 한 번 막는다(캐시 만료로 전체를 1.25배로 다시 씀)

막힌 프롬프트를 그대로 다시 보내면 통과한다. 통과 뒤에는 컨텍스트가 reblock_step 더 늘 때까지 2번으로 다시 막지 않는다.
`/` 명령, 백그라운드 작업 완료 알림(`<task-notification>`), usage-limit-resume 재개 프롬프트는 막지 않는다.
세션 아래에 위임 작업(busy_words, 기본 codex)이 돌고 있으면 막지 않고 경고만 한다. 결과 회수는 지시를 쓴 세션이 끝낸다.

막을 때
1. 이어받기 프롬프트(세션명·폴더·브랜치·최근 요청·직전 답변·막힌 요청)를 만들어 `handoff/<session>.md` 에 저장한다
2. 그 프롬프트로 백그라운드 새 세션(`claude --bg`)을 띄운다
3. 대화형 세션이면 Windows Terminal 새 탭에서 `claude attach <id>` 를 연다(못 열면 그 명령을 클립보드에 복사)
4. 막힌 세션을 몇 초 뒤 닫는다(대화형은 프로세스 종료, 백그라운드 작업은 `claude stop`)
새 세션을 못 띄우면 이어받기 프롬프트 자체를 클립보드에 복사하고 차단 메시지에 보여 준다.

설정은 `~/.claude/context-guard/config.json` 으로 덮어쓴다(DEFAULTS 의 키). 플러그인 파일은 업데이트 때 덮어써진다.
실패해도 프롬프트를 막지 않는다. 어떤 예외든 조용히 0 으로 끝난다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = Path.home() / ".claude" / "context-guard"
HANDOFF_DIR = STATE_DIR / "handoff"
CONFIG_PATH = STATE_DIR / "config.json"
# 다른 훅이 세션명을 적어 두는 곳(있으면 이어받기 프롬프트에 넣는다)
TITLE_DIR = Path.home() / ".claude" / "session-titles"
TAIL_BYTES = 4 * 1024 * 1024
PASS_MARKERS = ("sent by the usage-limit-resume plugin",)
SPAWN_ID_RE = re.compile(r"backgrounded\s*·\s*([0-9a-f]+)")
# 부모 세션의 표식이 새 세션에 섞이지 않게 이 접두사로 시작하는 환경변수를 뺀다
SPAWN_DROP_ENV = (
    "CLAUDECODE", "CLAUDE_JOB_", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_PLUGIN_", "CLAUDE_CODE_SESSION_",
    "CLAUDE_CODE_CHILD_", "CLAUDE_CODE_MESSAGING_", "CLAUDE_CODE_AGENT", "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_ALT_SCREEN_",
)

DEFAULTS: dict = {
    "warn_tokens": 120_000,
    "block_tokens": 150_000,
    "idle_tokens": 100_000,
    "idle_seconds": 3600,
    "reblock_step": 50_000,
    "recent_prompts": 5,
    "prompt_chars": 300,
    "answer_chars": 800,
    "auto_spawn": True,  # 막을 때 이어받기 프롬프트로 백그라운드 새 세션을 띄운다
    "auto_attach": True,  # 대화형 세션이면 새 세션을 Windows Terminal 새 탭에서 연다
    "auto_close": True,  # 새 세션이 뜨면 막힌 세션을 닫는다
    "close_delay_seconds": 3,
    "parent_name": "claude",  # 닫기 전에 부모 프로세스 이름에 이 낱말이 있는지 확인한다
    "busy_words": ["codex"],  # 세션 아래 이 낱말이 든 프로세스가 있으면 막지 않는다
}
CFG: dict = dict(DEFAULTS)


def load_config() -> dict:
    """DEFAULTS 에 config.json 을 덮어쓴다. 모르는 키·깨진 파일은 무시한다."""
    cfg = dict(DEFAULTS)
    try:
        user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    if isinstance(user, dict):
        cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
    return cfg


def clean_env() -> dict:
    return {k: v for k, v in os.environ.items() if not k.startswith(SPAWN_DROP_ENV)}


def read_tail(path: Path) -> list[str]:
    """transcript 끝부분 줄만 읽는다(큰 파일 전체를 읽지 않으려고)."""
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - TAIL_BYTES))
        data = f.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if size > TAIL_BYTES else lines


def last_usage(lines: list[str]) -> tuple[int, datetime | None]:
    """마지막 assistant 응답의 컨텍스트 토큰 수와 그 응답 시각.

    새 프롬프트가 hook 보다 먼저 transcript 에 적힐 수 있어 휴지 기준은 assistant 응답 시각으로 잡는다.
    """
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = (entry.get("message") or {}).get("usage") if entry.get("type") == "assistant" else None
        if usage:
            tokens = sum(usage.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            stamp = entry.get("timestamp")
            return tokens, datetime.fromisoformat(stamp.replace("Z", "+00:00")) if stamp else None
    return 0, None


def clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def entry_texts(entry: dict) -> list[str]:
    """transcript 한 줄의 message 에서 text 블록만 뽑는다(tool_result·tool_use 는 뺀다)."""
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return [content]
    return [b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]


def build_handoff(lines: list[str], session_id: str, transcript: Path, prompt: str) -> str:
    """새 세션에 그대로 붙여 넣을 이어받기 프롬프트를 transcript 끝부분에서 만든다."""
    cwd = branch = ""
    prompts: list[str] = []
    answer = ""
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        cwd = entry.get("cwd") or cwd
        branch = entry.get("gitBranch") or branch
        kind = entry.get("type")
        if kind == "user" and not entry.get("isMeta") and not entry.get("isCompactSummary"):
            # 명령 출력·시스템 알림·훅 주입은 "<" 로 시작한다
            prompts += [t.strip() for t in entry_texts(entry) if t.strip() and not t.lstrip().startswith("<")]
        elif kind == "assistant":
            text = "\n".join(t for t in entry_texts(entry) if t.strip())
            answer = text or answer
    title_path = TITLE_DIR / f"{session_id}.txt"
    title = title_path.read_text(encoding="utf-8").strip() if title_path.is_file() else ""
    # /compact 처럼 인자 없는 명령은 맥락이 없어 뺀다
    recent = [p for p in prompts if p != prompt.strip() and not (p.startswith("/") and " " not in p)]
    recent = recent[-CFG["recent_prompts"]:]
    out = [
        "이전 세션에서 하던 작업을 이어서 한다. 이전 세션은 컨텍스트가 커져서 끊었다.",
        "",
        f"- 이전 세션명: {title or '(없음)'}",
        f"- 작업 폴더: {cwd or '(모름)'}",
        f"- 브랜치: {branch or '(모름)'}",
        f"- 이전 transcript: {transcript} (아래로 모자랄 때만 끝부분을 읽는다. 전체를 읽지 않는다)",
        "",
        "## 이전 세션의 최근 요청(오래된 순)",
        *(f"{i}. {clip(p, CFG['prompt_chars'])}" for i, p in enumerate(recent, 1)),
        "",
        "## 직전 답변 요지",
        clip(answer, CFG["answer_chars"]) if answer else "(없음)",
        "",
        "## 이번에 할 일",
        prompt.strip() or "(위 작업을 이어서 진행)",
        "",
        "먼저 git status 와 관련 파일로 현재 상태를 확인한 뒤 이어서 진행한다.",
    ]
    return "\n".join(out)


def copy_to_clipboard(text: str) -> bool:
    try:
        if sys.platform == "win32":
            # clip.exe 는 한글을 깨거나 BOM 을 같이 붙여 넣어서 Set-Clipboard 로 넘긴다(환경변수는 유니코드 그대로 간다)
            env = {**os.environ, "CLIP_TEXT": text}
            subprocess.run(["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value $env:CLIP_TEXT"], env=env, check=True, timeout=10)
        elif sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True, timeout=5)
        else:
            return False
        return True
    except Exception:
        return False


def save_handoff(lines: list[str], session_id: str, transcript: Path, prompt: str) -> tuple[str, Path] | None:
    """이어받기 프롬프트를 만들어 저장한다. 실패하면 None."""
    try:
        text = build_handoff(lines, session_id, transcript, prompt)
        HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
        path = HANDOFF_DIR / f"{session_id}.md"
        path.write_text(text, encoding="utf-8")
        return text, path
    except Exception:
        return None


def spawn_session(text: str, cwd: str) -> str | None:
    """이어받기 프롬프트로 백그라운드 새 세션을 띄우고 그 id 를 돌려준다. 실패하면 None.

    작업 폴더가 신뢰 승인을 안 받은 하위 폴더면 `claude --bg` 가 "Workspace not trusted" 로 거절한다
    (2026-10-22, 신뢰된 projects 에서 시작해 projects/assistant 로 옮겨 간 세션). 그때는 세션을 시작한
    폴더(CLAUDE_PROJECT_DIR, 늘 신뢰됨)에서 다시 띄운다. 이어받기 프롬프트에 작업 폴더가 적혀 있다.
    """
    if not CFG["auto_spawn"]:
        return None
    candidates = [c for c in dict.fromkeys([cwd, os.environ.get("CLAUDE_PROJECT_DIR") or ""]) if c and Path(c).is_dir()]
    for folder in candidates:
        try:
            done = subprocess.run(
                ["claude", "--bg", text], cwd=folder, env=clean_env(), capture_output=True, timeout=60,
                encoding="utf-8", errors="replace",
            )
        except Exception:
            return None  # 시간 초과면 세션이 이미 떴을 수 있어 다른 폴더에서 또 띄우지 않는다
        match = SPAWN_ID_RE.search(done.stdout or "")
        if done.returncode == 0 and match:
            return match.group(1)
        if "not trusted" not in (done.stderr or ""):
            return None
    return None


def open_attach_tab(new_id: str, cwd: str) -> bool:
    """대화형 세션이면 Windows Terminal 새 탭에서 새 세션을 연다. 백그라운드 작업(CLAUDE_JOB_DIR)은 볼 화면이 없어 건너뛴다."""
    if not CFG["auto_attach"] or sys.platform != "win32" or os.environ.get("CLAUDE_JOB_DIR"):
        return False
    try:
        # -w 0: 가장 최근 창에 탭을 붙인다. wt 는 탭을 띄우고 바로 끝난다
        subprocess.Popen(["wt", "-w", "0", "nt", "-d", cwd, "claude", "attach", new_id], env=clean_env())
        return True
    except Exception:
        return False


def list_processes() -> list[dict]:
    """모든 프로세스의 {pid, ppid, cmd}. 실패하면 예외를 그대로 올린다."""
    if sys.platform == "win32":
        command = "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress"
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True, timeout=20, encoding="utf-8", errors="replace",
        ).stdout
        return [
            {"pid": p.get("ProcessId"), "ppid": p.get("ParentProcessId"), "cmd": p.get("CommandLine") or ""}
            for p in json.loads(out)
        ]
    out = subprocess.run(
        ["ps", "-A", "-o", "pid=,ppid=,command="], capture_output=True, timeout=20, encoding="utf-8", errors="replace",
    ).stdout
    procs = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            procs.append({"pid": int(parts[0]), "ppid": int(parts[1]), "cmd": parts[2] if len(parts) > 2 else ""})
    return procs


def has_busy_children(pid: str) -> bool:
    """pid 아래 자손 중 위임 작업(Codex 등)이 돌고 있으면 True. 확인을 못 하면 안전하게 True."""
    try:
        procs = list_processes()
    except Exception:
        return True
    children: dict[int, list[dict]] = {}
    for p in procs:
        children.setdefault(p["ppid"], []).append(p)
    words = [w.lower() for w in CFG["busy_words"]]
    stack = list(children.get(int(pid), []))
    while stack:
        p = stack.pop()
        if any(word in p["cmd"].lower() for word in words):
            return True
        stack += children.get(p["pid"], [])
    return False


def parent_matches(pid: str) -> bool:
    """엉뚱한 프로세스를 닫지 않게 pid 의 프로세스 이름에 parent_name 이 있는지 본다."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, timeout=10, encoding="utf-8", errors="replace",
            ).stdout
            name = out.split(",")[0].strip('"') if out.startswith('"') else ""
        else:
            name = subprocess.run(
                ["ps", "-p", pid, "-o", "comm="], capture_output=True, timeout=10, encoding="utf-8", errors="replace",
            ).stdout.strip()
    except Exception:
        return False
    return bool(name) and CFG["parent_name"].lower() in Path(name).name.lower()


def schedule_close(action: list[str]) -> bool:
    """close_delay_seconds 뒤 action 을 실행하도록 분리된 프로세스로 예약한다(훅이 먼저 끝나야 차단 메시지가 나간다)."""
    delay = int(CFG["close_delay_seconds"])
    try:
        if sys.platform == "win32":
            # DETACHED_PROCESS 로 띄운 powershell 은 아무것도 안 하고 끝난다(2026-10-20 확인). 창 없는 콘솔로 띄운다
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            command = f"Start-Sleep {delay}; & {' '.join(action)}"
            subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", command],
                creationflags=flags, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            subprocess.Popen(
                ["sh", "-c", f'sleep {delay}; exec "$@"', "sh", *action],
                start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        return True
    except Exception:
        return False


def close_old_session(tab_opened: bool) -> bool:
    """막힌 세션을 닫도록 예약한다.

    대화 기록은 transcript 에 이미 저장돼 있어 잃는 것이 없다. 백그라운드 작업은 rm 이 워크트리까지
    지우므로 stop 만 한다. 대화형은 새 탭이 열렸을 때만 닫는다(못 열었으면 사용자가 attach 해야 한다).
    """
    if not CFG["auto_close"]:
        return False
    job_dir = os.environ.get("CLAUDE_JOB_DIR")
    if job_dir:
        return schedule_close(["claude", "stop", Path(job_dir).name])
    pid = os.environ.get("CLAUDE_PID") or ""
    if not tab_opened or not pid.isdigit() or not parent_matches(pid):
        return False
    if sys.platform == "win32":
        return schedule_close(["Stop-Process", "-Id", pid, "-Force"])
    return schedule_close(["kill", pid])


def block_message(header: str, handoff: tuple[str, Path] | None, cwd: str) -> str:
    """차단 메시지 본문. 새 세션을 띄웠으면 여는 법을, 못 띄웠으면 붙여 넣을 프롬프트를 보여 준다."""
    resend = "꼭 이 세션에서 이어야 하면 같은 프롬프트를 그대로 다시 보내면 통과합니다"
    if handoff is None:
        return f"{header} 새 세션(또는 /clear)에서 시작하세요. {resend}."
    text, path = handoff
    new_id = spawn_session(text, cwd)
    if new_id:
        attach = f"claude attach {new_id}"
        tab_opened = open_attach_tab(new_id, cwd)
        if tab_opened:
            opened = "- 새 터미널 탭에서 열었습니다\n"
        else:
            copied = " (클립보드에 복사함)" if copy_to_clipboard(attach) else ""
            opened = f"- 열기: 이 세션을 닫고 `{attach}`{copied}\n"
        if close_old_session(tab_opened):
            stay = f"- 이 세션은 {CFG['close_delay_seconds']}초 뒤 자동으로 닫힙니다(대화 기록은 남습니다)\n"
        else:
            stay = f"- 이 세션은 닫지 않았습니다. {resend}. 그때는 `claude stop {new_id}` 로 새 세션을 멈추세요\n"
        return (
            f"{header} 이어받기 프롬프트로 새 세션 {new_id} 를 백그라운드에서 시작했습니다.\n"
            f"{opened}{stay}"
            f"- 이어받기 프롬프트: {path}"
        )
    copied = "클립보드에 복사했고 " if copy_to_clipboard(text) else ""
    return (
        f"{header} 새 세션(또는 /clear)에서 시작하세요. {resend}.\n\n"
        f"새 세션에 붙여 넣을 이어받기 프롬프트를 {copied}{path} 에 저장했습니다:\n\n{text}"
    )


def decide(prompt: str, tokens: int, idle: float | None, state: dict) -> tuple[str, str, dict]:
    """("pass"|"warn"|"block", 메시지, 새 상태)를 돌려준다."""
    # 백그라운드 작업(Codex 등) 완료 알림도 이 훅을 거친다. 막으면 결과 회수가 끊긴다
    if prompt.lstrip().startswith(("/", "<task-notification")) or any(m in prompt for m in PASS_MARKERS):
        return "pass", "", state
    digest = hashlib.sha256(prompt.strip().encode("utf-8")).hexdigest()
    if state.get("blocked_digest") == digest:
        return "pass", "", {"override_tokens": tokens}
    k = tokens // 1000
    if idle is not None and idle > CFG["idle_seconds"] and tokens >= CFG["idle_tokens"]:
        reason = f"{idle / 3600:.1f}시간 쉰 세션이고 컨텍스트가 {k}K 토큰이라 캐시가 만료돼 전체를 다시 씁니다"
    elif tokens >= CFG["block_tokens"] and tokens >= state.get("override_tokens", 0) + CFG["reblock_step"]:
        reason = f"컨텍스트가 {k}K 토큰입니다(기준 {CFG['block_tokens'] // 1000}K)"
    elif tokens >= CFG["warn_tokens"]:
        return "warn", f"[context-guard] 컨텍스트 {k}K 토큰. 주제가 바뀌면 새 세션을 여세요.", state
    else:
        return "pass", "", state
    return "block", f"[context-guard] {reason}.", {**state, "blocked_digest": digest}


def read_payload() -> dict:
    """훅 입력 JSON. Windows 는 stdin 기본 인코딩이 cp949 라 한글 프롬프트가 깨지므로(2026-10-29 "진행"→"吏꾪뻾")
    바이트로 읽어 UTF-8 로 푼다."""
    stream = getattr(sys.stdin, "buffer", None)
    return json.loads(stream.read().decode("utf-8") if stream else sys.stdin.read())


def main() -> None:
    CFG.update(load_config())
    payload = read_payload()
    transcript = Path(payload.get("transcript_path") or "")
    session_id = payload.get("session_id") or ""
    if not session_id or not transcript.is_file():
        return
    lines = read_tail(transcript)
    tokens, last_time = last_usage(lines)
    idle = (datetime.now(timezone.utc) - last_time).total_seconds() if last_time else None
    state_path = STATE_DIR / f"{session_id}.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    prompt = payload.get("prompt") or ""
    action, message, new_state = decide(prompt, tokens, idle, state)
    parent = os.environ.get("CLAUDE_PID") or ""
    if action == "block" and parent.isdigit() and has_busy_children(parent):
        # 위임한 Codex 가 돌고 있으면 결과 회수·리뷰는 지시를 쓴 이 세션이 끝내게 두고, 끝난 뒤 다음 프롬프트에서 옮긴다
        action, new_state = "warn", state
        message = f"{message} 다만 이 세션이 맡긴 위임 작업이 돌고 있어 이번에는 통과시킵니다. 끝난 뒤 다음 프롬프트에서 새 세션으로 옮깁니다."
    if new_state != state:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(new_state), encoding="utf-8")
    if action == "block":
        handoff = save_handoff(lines, session_id, transcript, prompt)
        message = block_message(message, handoff, payload.get("cwd") or "")
        print(json.dumps({"decision": "block", "reason": message}, ensure_ascii=False))
    elif action == "warn":
        print(json.dumps({"systemMessage": message}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 기본 cp949 로는 한글이 깨진다
        main()
    except Exception:
        pass
    sys.exit(0)
