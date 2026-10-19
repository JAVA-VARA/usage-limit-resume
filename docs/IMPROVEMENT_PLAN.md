# usage-limit-resume 개선 계획 (v0.2.0 → v0.3.0)

- 작성일: 2026-10-19
- 기준 커밋: `153cfa8` (v0.2.0)
- 범위: 분석과 계획만 다룬다. 이 문서를 쓰면서 코드는 고치지 않았다.
- 목표: 지금 있는 기능은 그대로 두고, 실제 환경에서 믿고 쓸 수 있게 만든다. 종합 작업 관리 도구로 키우지 않는다.

분류 표기

| 표기 | 뜻 |
|---|---|
| **FIX** | 결함이 확인된 항목 |
| **TEST** | 구현은 있지만 검증이 필요한 항목 |
| **IMPROVE** | 신뢰성을 높이는 개선 |
| **DEFER** | 지금 단계에서는 필요 없는 항목 |

---

## 0. 실사용 근거 (2026-10-19)

v0.2.0 이 실제로 한도에 걸렸다가 재개하는 흐름을 처음 끝까지 돌았다. 이 문서의 판단은 아래 기록을 바탕으로 한다.

- 13:21~14:14: 작업 6개가 한도에 걸렸다(`resets 2:50pm (Asia/Seoul)`). 훅이 매번 `schedule 2026-10-19 14:52:00` 을 다시 걸었다. 예약은 하나를 계속 덮어써서 중복 등록이 없었다
- 14:52:01: 예약 작업이 `run` 을 실행했다. 후보 6개를 3~9초 간격으로 차례로 `respawn` 했고 모두 `rc=0` 이었다
- 각 작업의 `timeline.jsonl` 을 보면 6개 모두 `respawn` 뒤 1분 안에 `working` 으로 바뀌어 실제로 이어서 일했다
- `history.json` 에는 작업마다 1회씩 기록됐다
- 로그의 메시지 `·` 가 `쨌` 로 깨져 있었다. → **FIX-1**

---

## 1. 현재 코드 구조와 기능

```
.claude-plugin/plugin.json, marketplace.json   매니페스트(같은 저장소가 marketplace 도 겸함)
hooks/hooks.json → hooks/register.ts            Claude Code 안에서 도는 얇은 층
hooks/register.test.ts                          위 모듈 테스트 3건
scripts/usage_limit_resume.py                   실제 로직(약 570줄, 표준 라이브러리만 씀)
```

`register.ts` 가 하는 일
- `classic.StopFailure` 에서 `error === 'rate_limit'` 이면 이벤트 JSON 을 stdin 으로 넘겨 `python … hook` 을 실행한다. `python` 이 없으면(종료 코드 9009·127) `python3` 로 다시 시도한다. 실패해도 세션을 막지 않는다(`.catch`)
- `/usage-resume` 명령을 등록한다. 실행하면 `python … status` 의 결과를 보여 준다

`usage_limit_resume.py` 의 명령

| 명령 | 하는 일 |
|---|---|
| `hook` | 메시지에서 리셋 시각을 읽는다. 대화형 세션(`CLAUDE_JOB_DIR` 없음)이면 `interactive.json` 에 기록한다. 그다음 (이 리셋, 다른 막힌 작업, 대화형 대기분 중 가장 이른 시각) + 2분으로 OS 예약을 덮어쓴다 |
| `run` | lock 을 잡고 대상을 찾는다. 대상은 한도로 막힌 백그라운드 작업과, 대기 중 백그라운드로 옮겨진 대화형 세션이다. 리셋이 지났고 24시간 상한(3회)에 걸리지 않은 작업은 `queuedPrompt` 를 쓴 뒤 `claude respawn <id>` 를 실행한다. 남은 대기분이 있으면 다시 예약하고, 없으면 예약을 지운다 |
| `status` / `scan` | 다음 예약 시각, 찾은 `claude` 경로, `run` 을 모의 실행한 결과를 출력한다 |

요청서 2장의 "유지할 기능" 10개를 코드와 대조한 결과

| # | 기능 | 코드 근거 | 판정 |
|---|---|---|---|
| 1 | StopFailure(rate_limit) 감지 | `register.ts` | 실사용에서 확인 |
| 2 | 리셋 시각 파싱 | `parse_reset` | "2:50pm (Asia/Seoul)" 형식만 실사용에서 확인. 날짜가 붙은 형식(주간 한도)은 아직 검증 안 함 → TEST-3 |
| 3 | 작업 스케줄러 / LaunchAgent | `schedule_win` / `schedule_mac` | Windows 는 실사용에서 확인. macOS 는 흉내 테스트만 함 → TEST-5 |
| 4 | Claude Code 종료 뒤 재개 | OS 예약이라 Claude Code 프로세스와 무관 | Windows 에서 확인 |
| 5 | 백그라운드로 옮긴 대화형 세션 추적 | `find_moved_interactive_jobs` | 10-16 사례를 재현해 확인했다. 지금은 그 작업이 `done` 이라 재현 테스트 결과가 0건이다. 고정 fixture 가 필요하다 → TEST-1 |
| 6 | `claude respawn` 으로 재개 | `resume_job` | 실사용에서 확인 |
| 7 | Job ID, transcript, worktree 유지 | `respawn` 이 같은 ID 를 씀 | 실사용에서 확인(같은 ID 의 timeline 에 이어서 기록됨) |
| 8 | 수동으로 재개한 작업 제외 | 백그라운드: 마지막 timeline 기록이 한도 차단인지 확인. 대화형: `touched_after` | 코드만 확인. 자동 테스트 없음 |
| 9 | 중복 실행 방지, 횟수 상한 | `run.lock`, `history.json` | 코드만 확인. 허점 있음 → IMPROVE-3 |
| 10 | `/usage-resume` | `cmd_status` | 확인 |

현재 테스트 현황
- `hooks/register.test.ts` 3건 통과: rate_limit 이면 스크립트를 부르는지, python3 로 대체하는지, 다른 오류는 무시하는지
- **Python 로직의 자동 테스트는 저장소에 0건이다.** v0.2.0 때 돌린 재현 테스트와 Mac 흉내 테스트는 세션 임시 폴더에만 있어서, 작업을 지우면 함께 사라진다
- 커버리지를 측정할 도구가 없다. 저장소 기준 Python 커버리지는 사실상 0%다

---

## 2. 발견한 결함과 근거

### FIX-1. 훅이 stdin 을 OS 기본 인코딩으로 읽는다 (P0)

- **위치:** `cmd_hook` 의 `json.load(sys.stdin)`
- **근거:** 실사용 로그에 `session limit · resets` 가 `session limit 쨌 resets` 로 남았다. 재현해 보면 `echo '{"m":"·"}' | python -c "import sys;print(sys.stdin.encoding)"` 가 Windows 에서 `cp949` 를 출력한다. `register.ts` 는 UTF-8 로 보낸다
- **영향:** 지금은 리셋 시각이 ASCII 라 파싱에 성공한다. 하지만 메시지에 cp949 로 해석할 수 없는 바이트열이 섞이면 `UnicodeDecodeError` 가 난다. 그러면 `payload = {}` 가 되어 다음 일이 생긴다
  - 리셋 시각을 잃고 30분 대체 대기로 넘어간다
  - 대화형 세션은 `session_id` 를 잃어 기록되지 않는다. 그러면 처음 고친 문제(백그라운드로 옮긴 세션이 재개되지 않음)가 조용히 되살아난다
- **수정:** `json.load(io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8"))`. 난이도 S

### FIX-2. respawn 이 실패하면 다시 시도하지 않는다 (P0)

- **위치:** `cmd_run`. `resume_job` 이 False 를 돌려주면 그 작업은 `pending` 에 들어가지 않는다
- **근거(코드 경로):** `claude` 를 찾지 못했거나, 종료 코드가 0이 아니거나, 180초 시간 초과가 나면 `queuedPrompt` 를 되돌리고 끝난다. 남은 대기분이 없으면 `unschedule()` 이 예약을 지운다. 그 작업은 **다음에 한도에 걸려 훅이 다시 돌 때까지** 막힌 채로 남는다. 일시적 실패(예: 부팅 직후 네트워크가 아직 안 붙음) 한 번에 복구 기회가 사라진다
- **재현:** mock `claude` 가 종료 코드 1을 돌려주게 한 뒤 `run` 을 실행하면 로그에 `respawn … rc=1` 다음 예약이 남지 않는다(TEST-1 에 넣는다)
- **수정:** 실패한 작업은 `now + 10분 × 2^(실패 횟수)` 로 `pending` 에 다시 넣는다. 실패도 24시간 상한 횟수에 센다. 난이도 S

### FIX-3. 예약 작업이 버전 번호가 든 플러그인 캐시 경로를 가리킨다 (P0, 위험)

- **위치:** `schedule_win` 과 `launchd_plist` 는 `Path(__file__).resolve()` 를 쓴다. 이 경로는 `~/.claude/plugins/cache/usage-limit-resume/usage-limit-resume/0.2.0/scripts/…` 이다
- **영향:** 예약이 걸린 상태에서 플러그인을 0.3.0 으로 업데이트하면 위험하다. Claude Code 가 옛 버전 폴더를 지우면 예약 시각에 스크립트가 없어 조용히 실패한다. 실패 기록은 작업 스케줄러 기록에만 남고 우리 로그에는 남지 않는다. 옛 폴더가 남는 경우에도 옛 코드가 돈다
- **확인 필요:** Claude Code 가 업데이트할 때 옛 캐시 폴더를 언제 지우는지는 문서로 확인하지 못했다. 그래서 "위험"으로 분류했다
- **수정:** 예약할 때 스크립트를 `~/.claude/usage-limit-resume/runner.py` 로 복사하고, 예약은 그 고정 경로를 부르게 한다. 난이도 S

### FIX-4. PowerShell 명령 문자열에 경로를 그대로 끼워 넣는다 (P1)

- **위치:** `schedule_win`. `'{PYTHONW}'` 와 `\"{script}\"` 를 PowerShell 문자열 안에 그대로 넣는다
- **영향:** 사용자 이름이나 Python 설치 경로에 `'` 가 있으면(예: `C:\Users\O'Brien`) 예약 등록이 실패한다. 경로는 본인 계정이 정하는 값이라 보안 위험은 낮다. 하지만 공개 배포물에서는 실패할 수 있는 지점이다
- **수정:** 경로 안의 `'` 를 `''` 로 바꾼다. 또는 `-EncodedCommand` 와 인자 배열로 넘긴다. 난이도 S

---

## 3. 테스트되지 않은 시나리오

| ID | 시나리오 | 분류 | 실제 한도가 필요한가 |
|---|---|---|---|
| TEST-1 | 전체 흐름: hook → 예약 → run → 대상 찾기 → `queuedPrompt` → respawn → 예약 정리 | TEST | 아니요(mock 으로 가능) |
| TEST-2 | 수동으로 이어 간 작업, `done` 작업, `queuedPrompt` 가 이미 있는 작업은 제외 | TEST | 아니요 |
| TEST-3 | 리셋 시각 형식: 날짜 없음, `Oct 22, 9am`, 연도 포함, 자정 넘김, 시간대 없음, DST 전환일(America/New_York), 파싱 실패 | TEST | 아니요. 단 주간 한도 메시지의 실제 형식 샘플은 한 번 받아 둬야 한다 |
| TEST-4 | 여러 작업의 리셋 시각이 서로 다를 때 가장 이른 시각으로 예약하고, 남은 작업은 다시 예약 | TEST | 아니요 |
| TEST-5 | macOS 실기: LaunchAgent 등록·실행, 잠자기 중 예약 뒤 깨어날 때 실행, `pmset` wake, 로그아웃·재부팅 | TEST | 실기 필요(맥미니). 한도는 가짜 시각으로 대체 가능 |
| TEST-6 | Windows: 꺼진 PC 를 다시 켰을 때(`StartWhenAvailable`), 예약 시각에 로그오프 상태일 때, 다른 사용자가 로그인해 있을 때 | TEST | 실기 필요. 한도는 불필요 |
| TEST-7 | 대화형 세션을 대기 중 백그라운드로 옮긴 경우(10-16 사례). 지금의 재현 테스트는 실데이터에 기대고 있어 이미 0건이 나온다 | TEST | 아니요. transcript·timeline fixture 로 고정 |
| TEST-8 | npm 설치 `claude.cmd`, `CLAUDE_BIN`, `claude_path.txt` 를 고르는 순서 | TEST | 아니요 |
| TEST-9 | 실제 한도에서의 전체 흐름 | TEST | **예.** 10-19 에 Windows 에서 1회 통과. 릴리스마다 수동 확인 1회 |

---

## 4. 경쟁 도구 대비 차별화

2026-10-19 기준 공개 README·소개 페이지에서 **확인한 것만** 적는다. 표의 "?"는 문서에서 확인하지 못했다는 뜻이며, 없다는 뜻이 아니다.

| 비교 항목 | usage-limit-resume | 내장 autoContinue | TaskWake | resumer | PTY 래퍼류(claude-auto-resume·auto-retry·auto-continue) |
|---|---|---|---|---|---|
| 같은 백그라운드 Job ID 그대로 재개 | O (`respawn`) | X (대화형만) | X (`--resume -p` 로 새로 실행) | ? | X |
| transcript·worktree 유지 | O | O (대화형) | transcript O, 작업 상태 X | ? | 세션 유지 |
| 여러 Job 을 각각 복구 | O (작업마다 상한 따로) | 세션별 | 세션별 대기 프로세스 | ? | 터미널 하나 |
| Claude Code 를 끈 뒤에도 재개 | O (OS 예약) | X | O (대기 프로세스) | ? | X (래퍼 터미널이 떠 있어야 함) |
| 절전 깨우기 | O (Windows RTC wake, macOS `pmset`, 별도 설정 필요) | X | X (문서에 없음) | ? | X |
| 재부팅 뒤 | Windows O, macOS X | X | Windows O | ? | X |
| 상주 데몬 없음 | O | 해당 없음 | O | ? | 래퍼 프로세스가 상주 |
| 수동 재개 감지·횟수 상한 | O | 해당 없음 | ? | ? | ? |

- Noctis, no-limit, usage-watchdog: 2026-10-19 검색에서 찾지 못해 비교하지 않았다. 저장소 주소를 알면 다시 확인한다
- claude-usage-guard: 한도 **전에** 작업을 멈추게 하는 도구라 목적이 다르다

**실질적인 차별점은 세 가지다**
1. 백그라운드 Job 을 원래 ID, transcript, worktree 그대로 되살린다
2. 내장 기능이 취소하는 경우, 즉 대기 중 백그라운드로 옮긴 세션까지 이어 간다
3. 상주 프로세스 없이 OS 예약 하나로 Claude Code 종료, 절전, (Windows) 재부팅을 견딘다

---

## 5. 개선 항목과 우선순위

| ID | 항목 | 분류 | 우선순위 | 난이도 | 비고 |
|---|---|---|---|---|---|
| FIX-1 | stdin 을 UTF-8 로 읽기 | FIX | P0 | S | 한 줄 |
| FIX-2 | 실패한 respawn 다시 시도(백오프, 상한에 포함) | FIX | P0 | S | |
| FIX-3 | 예약은 고정 경로의 실행 사본을 부르게 | FIX | P0 | S | 업데이트 안전성 |
| TEST-1·2·4·7·8 | Python 통합 테스트 스위트 | TEST | P0 | M | 아래 7장 |
| IMPROVE-1 | 재개 결과를 단계별로 기록(Scheduled / Launched / Resumed / Failed) | IMPROVE | P0 | M | 아래 설명 |
| IMPROVE-2 | `/usage-resume doctor` | IMPROVE | P0 | S~M | 아래 설명 |
| IMPROVE-3 | 작업 하나 끝날 때마다 `history.json` 저장 | IMPROVE | P1 | S | 지금은 `run` 이 끝날 때 한 번만 저장한다. 작업 스케줄러 10분 제한이나 강제 종료로 중간에 끊기면 상한 기록이 사라진다 |
| FIX-4 | PowerShell 경로 이스케이프 | FIX | P1 | S | |
| TEST-3 | 리셋 시각 형식·DST·자정 테스트 | TEST | P1 | S | |
| IMPROVE-4 | 파싱 실패 시 대체 대기를 30분 고정에서 점증(30→60→120분)으로 | IMPROVE | P1 | S | 지금은 일찍 깨서 다시 한도에 걸리는 일을 반복하며 24시간 상한 3회를 써 버릴 수 있다 |
| IMPROVE-5 | `run` 의 respawn 시간 초과 합계를 작업 스케줄러 제한(10분) 안으로 | IMPROVE | P1 | S | 작업 수 × 180초가 10분을 넘을 수 있다. 시간 초과를 60초로 줄이거나 제한을 30분으로 늘린다 |
| TEST-5·6 | macOS·Windows 실기 시나리오 | TEST | P1 | M (수동) | 체크리스트로 진행 |
| P2-1 | `/usage-resume cancel` (예약 지우고 대기 기록 비우기) | IMPROVE | P2 | S | 비용이 작아 권장 |
| P2-2 | `/usage-resume history` (최근 성공·실패 N건) | IMPROVE | P2 | S | IMPROVE-1 의 기록을 그대로 보여 줌 |
| P2-3 | `/usage-resume test` (가짜 Job 으로 1분 뒤 예약까지 왕복) | IMPROVE | P2 | M | doctor 와 겹치는 부분이 많다. doctor 를 만든 뒤 판단 |
| D-1 | Linux(systemd-run) 지원 | DEFER | | M | 요청이 생기면 |
| D-2 | 웹 UI, 상주 데몬, LLM 요약, 작업 대기열 관리 | DEFER | | | 목표 밖 |
| D-3 | `queuedPrompt` 쓰기 경쟁 대비 파일 잠금 | DEFER | | M | 근거 아래 |

**IMPROVE-1 재개 결과를 단계별로 기록**
- 지금은 `respawn` 종료 코드가 0이면 성공으로 본다. 10-19 실사용에서는 6건 모두 실제로 재개됐다. 하지만 "프로세스를 띄웠다"와 "작업이 다시 돌았다"는 다른 사실이다
- 제안:
  1. 상태 파일 `~/.claude/usage-limit-resume/attempts.jsonl` 에 한 줄씩 남긴다
  2. `respawn` 이 0을 돌려주면 `launched` 로 적는다
  3. 그 뒤 60초 안에 그 작업의 `timeline.jsonl` 에 respawn 시각 이후 기록(`working` 등)이 생기면 `resumed` 로 적는다
  4. 기록이 생기지 않으면 `unconfirmed` 로 적고 FIX-2 의 재시도 대상에 넣는다
- `Completed` 는 추적하지 않는다. 작업이 끝났는지는 Claude Code 의 영역이고, 재개 플러그인이 판단할 일이 아니다

**IMPROVE-2 `/usage-resume doctor` 확인 항목**
1. Python 버전(3.9 이상), `zoneinfo` 사용 가능 여부, Windows 의 `pythonw.exe` 존재 여부
2. `claude` 경로와 `claude respawn --help` 의 종료 코드
3. 예약 권한: 1년 뒤 시각으로 시험 예약을 걸었다가 바로 지움
4. `~/.claude/jobs` 읽기 권한, 상태 폴더 쓰기 권한
5. 현재 예약 상태, 마지막 `run` 시각과 결과
6. FIX-3 이후: 실행 사본이 현재 플러그인 버전과 같은지

**D-3 를 미루는 근거:** `queuedPrompt` 는 한도로 막혀 멈춰 있는 작업에만 쓴다. 쓰기는 임시 파일에 쓴 뒤 `os.replace` 로 바꿔 넣는 원자적 교체다. 쓰기 직전에 상태를 다시 읽어 확인하기도 한다. 같은 순간 Claude Code 가 그 파일을 쓸 가능성은 낮고, Claude Code 가 쓰는 잠금 방식도 알 수 없다. 실제 충돌 사례가 나오면 다시 본다.

---

## 6. 보안·개인정보 검토

| 대상 | 현재 | 판단 |
|---|---|---|
| 로그 `log.txt` | 메시지 앞 120자, 작업 이름, respawn 출력 300자 | 로컬 전용. 작업 이름에 민감한 내용이 들어갈 수 있다는 점을 README 에 한 줄 적는다(IMPROVE, S) |
| `interactive.json` | 세션 ID, transcript 경로, 메시지 200자 | 로컬 전용. 24시간 지나면 정리. 문제 없음 |
| transcript 읽기 | `continued-in`, `Automatic continue cancelled` 줄만 찾음 | 내용을 밖으로 보내지 않음 |
| 외부 명령 | `claude`(인자 배열), `powershell.exe -Command`(문자열), `/bin/sh -c`(라벨 문자열), `launchctl`, `sudo -n pmset` | 문자열로 넘기는 두 곳은 FIX-4 로 처리. 라벨은 코드가 만든 값이라 안전. `sudo -n` 은 sudoers 에서 `pmset schedule wake *` 만 허용하라고 안내함 |
| `claude_path.txt`, `CLAUDE_BIN` | 이 파일이나 환경변수에 적힌 실행 파일을 실행 | 이 값을 바꿀 수 있는 사람은 이미 사용자 계정을 가진 사람이라 새 위험은 아님 |
| 네트워크 | 없음 | 해당 없음 |

---

## 7. 테스트 전략과 통과 기준

### 7.1 자동 테스트 (P0, 저장소에 추가)

`tests/` 아래에 pytest 로 둔다. 표준 라이브러리 밖의 의존성은 pytest 하나뿐이다.

- **격리:**
  - `HOME`·`USERPROFILE` 을 `tmp_path` 로 바꾼다. 모듈 상수(`HOME`, `JOBS_DIR`, `STATE_DIR` 등)는 함수에서 읽도록 조금 리팩터링하거나, fixture 가 모듈 속성을 바꿔 끼운다
  - 실제 `~/.claude` 와 실제 OS 예약에는 손대지 않는다
- **mock `claude`:** 받은 인자를 파일에 적고, 지정한 종료 코드를 돌려주는 작은 스크립트를 쓰고 `CLAUDE_BIN` 으로 넘긴다. respawn 을 흉내 낼 때는 timeline 에 `working` 한 줄을 덧붙인다
- **가짜 시각:** `datetime` 을 바꿔 끼우는 fixture
- **가짜 예약기:** `schedule`·`unschedule`·`next_scheduled` 를 기록만 하는 가짜로 바꾼다. macOS 경로는 `subprocess.run`·`Popen` 을 가짜로 바꿔 launchctl 호출 순서를 검증한다(v0.2.0 때 쓴 흉내 테스트를 옮겨 옴)
- **fixture:** 한도로 막힌 Job, 수동으로 재개한 Job, `done` Job, `queuedPrompt` 가 있는 Job, `continued-in` 이 든 transcript

통과 기준
1. TEST-1·2·4·7·8 과 FIX-1~3 재현 케이스가 모두 통과한다
2. Windows 와 macOS 에서 같은 스위트가 통과한다(CI: GitHub Actions `windows-latest`, `macos-latest`)
3. 테스트를 돌린 전후로 실제 `~/.claude` 의 내용이 바뀌지 않는다
4. `claude plugin test`·`claude plugin validate` 가 통과한다

### 7.2 수동 실기 체크리스트 (릴리스마다)

1. Windows: 1분 뒤 시각으로 가짜 Job 을 예약 → 절전 → 깨어난 뒤 로그 확인. PC 를 끈 상태로 예약 시각을 넘긴 뒤 부팅해서 실행되는지 확인
2. macOS(맥미니): 같은 순서. 추가로 `pmset` wake 설정 유무에 따른 차이, 로그아웃 뒤 동작
3. 실제 한도: 다음에 자연스럽게 한도에 걸릴 때 로그와 `attempts.jsonl` 을 확인(TEST-9)

### 7.3 실제 한도가 필요한 테스트를 따로 다루는 원칙

실제 한도를 소진해야만 확인되는 것은 두 가지뿐이다. 그래서 자동화하지 않고 실사용 관찰로 확인한다.
- 실제 메시지 형식
- Claude Code 가 `respawn` 을 실제로 받아 주는지

나머지는 모두 위 mock 으로 덮는다.

---

## 8. 단계별 로드맵

**v0.3.0: 정확성 (권장 범위, 예상 1~2일)**
1. Python 테스트 스위트와 fixture 를 먼저 만든다. 지금 동작을 고정하는 회귀 테스트다
2. FIX-1, FIX-2, FIX-3 을 고치고, 각각 재현 테스트를 붙인다
3. IMPROVE-1(`attempts.jsonl`, 재개 확인)과 IMPROVE-3(history 즉시 저장)
4. IMPROVE-2 `/usage-resume doctor`
5. FIX-4, IMPROVE-4, IMPROVE-5. 작고 위험이 낮아 함께 넣는다
6. GitHub Actions(Windows·macOS)에서 테스트를 돌린다
7. README 를 고친다: "지원"과 "실기 검증 완료"를 OS·시나리오별 표로 나눈다

**v0.3.x: 실기 검증**
- TEST-5(macOS 맥미니), TEST-6(Windows 꺼짐·로그오프)을 체크리스트로 진행하고 README 표를 갱신한다

**v0.4.0: 편의 (필요할 때)**
- `/usage-resume cancel`, `/usage-resume history`. `test` 는 doctor 를 써 본 뒤 판단한다

---

## 9. 하지 않을 것 (오버엔지니어링 방지)

- 상주 데몬, 트레이 앱, 웹 대시보드
- LLM 으로 작업을 요약하거나 재개 메시지를 상황에 맞게 지어 주는 기능
- 작업 대기열·우선순위 관리, 여러 계정 지원
- `Completed` 추적(Claude Code 의 영역)
- `state.json` 파일 잠금(D-3. 실제 충돌 사례가 나오기 전까지)
- Linux 지원(D-1. 요청이 생기기 전까지)
- 내장 autoContinue 를 대신하는 대화형 세션 재개. 내장 기능이 하는 일은 그대로 맡긴다
