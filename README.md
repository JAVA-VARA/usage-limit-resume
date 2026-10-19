# usage-limit-resume

A Claude Code plugin that **automatically resumes background jobs stopped by the usage limit** once the limit resets. Windows and macOS.

[한국어 설명은 아래에 있습니다.](#한국어)

Claude Code's built-in auto-continue (`autoContinueAtUsageLimit`) only covers interactive sessions, and it is cancelled ("Automatic continue cancelled · this session moved to the background") if you move the session to the background while it waits. This plugin covers those cases:

- **Background jobs** (`~/.claude/jobs/*`) blocked by the limit are resumed with `claude respawn <id>`, so the job keeps its ID, transcript and worktree
- **Interactive sessions moved to the background** while waiting are found through the transcript's `continued-in` record and resumed the same way
- It schedules an OS-level one-shot task, so it still fires after the session exits, and it can wake a sleeping machine

## How it works

1. When a turn stops with `StopFailure` = `rate_limit`, the hook reads the reset time from the message ("resets 6:40pm (Asia/Seoul)") and schedules a one-shot task for 2 minutes after it: Windows Task Scheduler (`ClaudeUsageLimitResume`) or a macOS LaunchAgent (`com.github.usage-limit-resume.<time>`)
2. At that time it finds jobs still blocked by the limit, queues a resume prompt into each, and runs `claude respawn <id>`
3. Jobs you already continued by hand are left alone. The same job is auto-resumed at most 3 times in 24 hours

## Install

```
claude plugin marketplace add JAVA-VARA/usage-limit-resume
claude plugin install usage-limit-resume@usage-limit-resume
```

Requirements:

- Windows 10/11 or macOS
- Python 3.9+ on `PATH` as `python` or `python3`
- Claude Code with background jobs (`claude respawn`), tested on 2.1.30x. `claude` is found via `CLAUDE_BIN`, then `PATH`, then common install locations (native installer, Homebrew, npm)

Check it from any session with `/usage-resume`: it shows the next scheduled run, the jobs it would resume, and which `claude` it will call. Log: `~/.claude/usage-limit-resume/log.txt`.

### Optional: wake from sleep

Without this, the resume runs right after the machine wakes up.

**Windows** (admin PowerShell, applies while plugged in):

```
powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1; powercfg /setactive SCHEME_CURRENT
```

**macOS**: waking needs root for `pmset schedule wake`. Allow only that command without a password:

```
echo "$USER ALL=(root) NOPASSWD: /usr/bin/pmset schedule wake *" | sudo tee /etc/sudoers.d/usage-limit-resume
sudo chmod 440 /etc/sudoers.d/usage-limit-resume
```

## Things to know

- Each resume starts a new turn, so it **uses your usage allowance**. That is the point, but keep it in mind
- It relies on Claude Code internals (`~/.claude/jobs/*/state.json` `queuedPrompt`, `claude respawn`, the transcript's `continued-in` record). A Claude Code update can break it. Check the log after updating
- If the machine is **powered off** at the scheduled time: Windows runs it at the next boot; macOS skips it until the next limit hit schedules a new one
- Linux is not supported (scheduling is skipped)
- Uninstall: `claude plugin uninstall usage-limit-resume@usage-limit-resume`. A pending one-shot task removes itself after it runs

## License

MIT

---

## 한국어

세션 한도(usage limit)로 멈춘 **백그라운드 작업**을 한도가 풀리면 자동으로 이어 가게 하는 Claude Code 플러그인입니다. Windows 와 macOS 에서 동작합니다.

- 내장 자동 재개는 대화형 세션만 다룹니다. 게다가 대기 중에 백그라운드로 옮기면 "Automatic continue cancelled" 로 취소됩니다
- 이 플러그인은 한도로 막힌 백그라운드 작업을 `claude respawn <id>` 로 같은 ID 그대로 재개합니다. 대기 중 백그라운드로 옮긴 대화형 세션도 `continued-in` 기록을 따라가 같은 방식으로 재개합니다
- 예약은 OS 에 겁니다(Windows 작업 스케줄러, macOS launchd). 그래서 세션이 끝나도 남고, 설정하면 절전 중인 PC 도 깨웁니다

설치:

```
claude plugin marketplace add JAVA-VARA/usage-limit-resume
claude plugin install usage-limit-resume@usage-limit-resume
```

- 확인: 세션에서 `/usage-resume`, 로그는 `~/.claude/usage-limit-resume/log.txt`
- 필요한 것: Python 3.9 이상(`python` 또는 `python3`), 백그라운드 작업을 지원하는 Claude Code(2.1.30x 에서 시험)
- 절전 깨우기는 위 "Optional: wake from sleep" 의 명령을 한 번 실행합니다
- 주의할 점:
  - 재개하면 그만큼 사용량이 나갑니다. 같은 작업은 24시간에 3번까지만 재개합니다
  - Claude Code 내부 구조에 기대고 있어 업데이트 뒤 깨질 수 있습니다
  - Linux 는 지원하지 않습니다
