# usage-limit-resume

Claude Code 세션 한도(usage limit)로 멈춘 **백그라운드 작업**을 한도가 풀리면 자동으로 다시 이어 가게 하는 플러그인. Windows 전용(작업 스케줄러·PowerShell 사용).

## 하는 일

1. 한도로 턴이 멈추면(`StopFailure` = `rate_limit`) 메시지의 "resets 6:40pm (Asia/Seoul)" 을 읽어 리셋 2분 뒤에 Windows 작업 스케줄러 1회 작업 `ClaudeUsageLimitResume` 을 등록한다
2. 그 시각에 `~/.claude/jobs/*` 중 한도로 막힌 작업에 재개 메시지를 넣고 `claude respawn <id>` 로 같은 작업 ID 그대로 다시 띄운다
3. 대화형 세션은 내장 자동 재개가 맡는다. 다만 한도 대기 중 백그라운드로 옮기면 내장 기능이 "Automatic continue cancelled" 로 취소되므로, 이어받은 작업(`continued-in`)을 찾아 같은 방식으로 재개한다
4. 사람이 먼저 손으로 이어 간 작업은 건드리지 않는다. 같은 작업은 24시간에 3번까지만 자동 재개한다

## 설치

같은 PC에서(폴더를 marketplace 로 등록):

```
claude plugin marketplace add C:\Users\rha00\projects\usage-limit-resume
claude plugin install usage-limit-resume@seokju-local
```

다른 PC에서는 이 폴더를 복사한 뒤 위 경로만 바꿔 같은 두 줄을 실행한다. 필요한 것: `python`(3.9+, PATH 에 있을 것), Claude Code 가 `~/.local/bin/claude.exe` 에 설치돼 있을 것.

### 절전 중에도 재개하려면(선택, 관리자 PowerShell)

```
powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1; powercfg /setactive SCHEME_CURRENT
```

전원이 연결된 동안만 PC를 깨운다. 켜지 않으면 PC가 깨어난 직후 실행된다.

## 확인

- 세션에서 `/usage-resume` — 다음 예약 시각과 재개 대상 작업
- 로그: `~/.claude/usage-limit-resume/log.txt`

## 한계

`queuedPrompt`·`claude respawn`·`continued-in` 은 Claude Code 2.1.30x 의 내부 구조다. 버전이 바뀌면 깨질 수 있으니 업데이트 뒤 로그를 한 번 확인한다.
