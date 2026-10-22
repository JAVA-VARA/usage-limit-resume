# context-guard

A Claude Code plugin that **keeps sessions from growing too large**. When a session gets big, it blocks the prompt once and hands the work off to a fresh background session with a handoff prompt it writes for you. Messages are in Korean.

[한국어 설명은 아래에 있습니다.](#한국어)

Long sessions re-read their whole context from cache on every turn. In one week of the author's usage, cache reads were 68% of all Claude Code tokens, mostly from sessions kept open for days at 200K–600K tokens. Replaying that week with a 150K cutoff instead of 250K used about 20% fewer tokens.

## How it works

Context size is the last assistant response's `input + cache_read + cache_creation` tokens. On every prompt (`UserPromptSubmit`):

1. At `warn_tokens` (120K) or more: a one-line warning, the prompt goes through
2. At `block_tokens` (150K) or more: the prompt is blocked once
3. Idle for more than `idle_seconds` (1 hour) since the last response with `idle_tokens` (100K) or more: blocked once (the cache has expired, so the next turn rewrites all of it)

Sending the same prompt again lets it through, and the session is not blocked again until it grows another `reblock_step` (50K).

When it blocks:

1. It writes a handoff prompt from the transcript: session title, folder, branch, the last 5 requests, the last answer, the blocked prompt, and the transcript path. Saved to `~/.claude/context-guard/handoff/<session>.md`
2. It starts a background session with that prompt (`claude --bg`) in the current folder. If that folder was never trusted (a subfolder you moved into), it retries in the folder the session was started from (`CLAUDE_PROJECT_DIR`); the handoff prompt names the real working folder
3. Interactive session on Windows: it opens `claude attach <id>` in a new Windows Terminal tab. Otherwise it copies that command to the clipboard
4. It closes the blocked session a few seconds later: the interactive process is ended (its transcript is already saved), a background job is stopped with `claude stop` (not `rm`, which would delete its worktree)

If the background session cannot be started, the handoff prompt itself goes to the clipboard and into the block message.

Never blocked:

- `/` commands
- Background task notifications (`<task-notification>`), so a delegated job's result still reaches the session that started it
- Resume prompts from the `usage-limit-resume` plugin
- Prompts while a delegated process (`busy_words`, default `codex`) is still running under the session. It only warns; the session that wrote the instructions reviews the result, and the next prompt after it finishes is handled normally

Any error lets the prompt through.

## Install

```
claude plugin marketplace add JAVA-VARA/usage-limit-resume
claude plugin install context-guard@usage-limit-resume
```

Requirements: Python 3.9+ on `PATH` as `python` or `python3`, Claude Code with background sessions (`claude --bg`, tested on 2.1.30x). Windows Terminal (`wt`) for the new tab.

## Settings

Put overrides in `~/.claude/context-guard/config.json`. Do not edit the plugin files; updates replace them.

```json
{ "warn_tokens": 120000, "block_tokens": 150000, "auto_close": false }
```

| Key | Default | Meaning |
|---|---|---|
| `warn_tokens` / `block_tokens` | 120000 / 150000 | Warning and block thresholds |
| `idle_tokens` / `idle_seconds` | 100000 / 3600 | Block a large session resumed after this long idle |
| `reblock_step` | 50000 | Growth needed after an override before blocking again |
| `recent_prompts` / `prompt_chars` / `answer_chars` | 5 / 300 / 800 | Handoff prompt size |
| `auto_spawn` | true | Start the background session |
| `auto_attach` | true | Open it in a new Windows Terminal tab |
| `auto_close` | true | Close the blocked session |
| `close_delay_seconds` | 3 | Wait before closing |
| `parent_name` | `claude` | The blocked session's process name must contain this before it is closed |
| `busy_words` | `["codex"]` | Don't block while a process under the session has one of these in its command line |

## Status

| Scenario | Windows | macOS |
|---|---|---|
| Warn, block, resend to override | Verified | Supported |
| Handoff prompt, clipboard | Verified | Supported (`pbcopy`) |
| Background session started | Verified | Supported |
| New tab with `claude attach` | Verified (Windows Terminal) | Not supported (clipboard instead) |
| Blocked session closed | Verified | Background jobs only (interactive sessions stay open) |
| Deferred while Codex runs | Verified | Supported (`ps`) |

## Things to know

- Each handoff starts a new session, which costs its base context (about 60K tokens in the author's setup)
- It relies on Claude Code internals: transcript fields, `claude --bg` output, `CLAUDE_PID`, `CLAUDE_JOB_DIR`. An update can break it
- State: `~/.claude/context-guard/<session>.json`, handoffs in `handoff/`. Nothing leaves your machine
- If another hook writes session titles to `~/.claude/session-titles/<session>.txt`, the title goes into the handoff
- Uninstall: `claude plugin uninstall context-guard@usage-limit-resume`

## License

MIT

---

## 한국어

세션이 너무 커지지 않게 막는 Claude Code 플러그인입니다. 커지면 프롬프트를 한 번 막고, 이어받기 프롬프트를 직접 써서 새 백그라운드 세션으로 넘깁니다.

- 120K 경고, 150K 차단, 1시간 넘게 쉰 100K 이상 세션도 차단. 같은 프롬프트를 다시 보내면 통과합니다
- 차단하면: 이어받기 프롬프트 저장 → `claude --bg` 로 새 세션 → Windows Terminal 새 탭에서 `claude attach` → 막힌 세션은 3초 뒤 닫음(백그라운드 작업은 `claude stop`)
- `/` 명령, 백그라운드 작업 완료 알림, usage-limit-resume 재개 프롬프트는 막지 않습니다
- 세션 아래에서 Codex 가 돌고 있으면 막지 않고 경고만 합니다. 결과 리뷰는 지시를 쓴 세션이 끝내고, 그 뒤 다음 프롬프트에서 옮깁니다
- 설정은 `~/.claude/context-guard/config.json` 에 적습니다(위 표의 키). 플러그인 파일은 업데이트 때 덮어써집니다

설치:

```
claude plugin marketplace add JAVA-VARA/usage-limit-resume
claude plugin install context-guard@usage-limit-resume
```
