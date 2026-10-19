import type { Register } from 'claude-code'

// 실제 일은 동봉한 scripts/usage_limit_resume.py 가 한다(작업 스케줄러·launchd 예약, respawn).
// 이 모듈은 한도 실패를 그 스크립트에 넘기고, 상태 확인 명령을 단다.
const script = (root: string) => `${root}/scripts/usage_limit_resume.py`

// Windows 는 보통 python, macOS 는 python3 만 있다. 9009·127 은 "명령 없음"
// (Windows 스토어 안내 바로가기 포함)이라 다음 이름으로 넘어간다.
const PYTHONS = ['python', 'python3']
const NOT_FOUND = new Set([9009, 127])
let found: string | undefined

type Run = { exitCode: number; stdout: string; stderr: string }

async function runPython($: any, args: string[], init: { stdin?: string; timeoutMs: number }): Promise<Run> {
  let last: unknown
  for (const py of found ? [found] : PYTHONS) {
    try {
      const r: Run = await $.process.run([py, script($.plugin.root), ...args], init)
      if (NOT_FOUND.has(r.exitCode)) {
        last = `${py}: exit ${r.exitCode}`
        continue
      }
      found = py
      return r
    } catch (err) {
      last = err
    }
  }
  throw new Error(`python 을 찾지 못함 (${String(last)})`)
}

export const register: Register = on => {
  on('classic.StopFailure', async ($, e, next) => {
    if (e.error === 'rate_limit') {
      try {
        const r = await runPython($, ['hook'], { stdin: JSON.stringify(e), timeoutMs: 120_000 })
        if (r.exitCode !== 0) $.ui.log(`${$.plugin.name}: hook exit ${r.exitCode} ${r.stderr.slice(0, 300)}`, { to: 'debug' })
      } catch (err) {
        $.ui.log(`${$.plugin.name}: hook failed: ${String(err)}`, { to: 'debug' })
      }
    }
    return next(e)
  }).catch(($, e, next) => next(e))

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'usage-resume',
      description: 'Usage-limit auto-resume: show the next scheduled run and jobs to resume (`doctor` checks the setup)',
    })
    return next(e)
  })

  on('command.run', { command: 'usage-resume' }, async ($, e) => {
    const sub = (e.args ?? '').trim() === 'doctor' ? 'doctor' : 'status'
    try {
      const r = await runPython($, [sub], { timeoutMs: 90_000 })
      return { text: (r.stdout + r.stderr).trim() || '(출력 없음)' }
    } catch (err) {
      return { text: String(err) }
    }
  })
}
