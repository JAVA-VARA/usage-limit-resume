import type { Register } from 'claude-code'

// 실제 일은 동봉한 scripts/usage_limit_resume.py 가 한다(작업 스케줄러 예약·respawn).
// 이 모듈은 한도 실패를 그 스크립트에 넘기고, 상태 확인 명령을 단다.
const script = (root: string) => `${root}/scripts/usage_limit_resume.py`

export const register: Register = on => {
  on('classic.StopFailure', async ($, e, next) => {
    if (e.error === 'rate_limit') {
      try {
        const r = await $.process.run(['python', script($.plugin.root), 'hook'], {
          stdin: JSON.stringify(e),
          timeoutMs: 120_000,
        })
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
      description: '한도 자동 재개: 다음 예약과 재개 대상 작업을 보여 준다',
    })
    return next(e)
  })

  on('command.run', { command: 'usage-resume' }, async $ => {
    const r = await $.process.run(['python', script($.plugin.root), 'status'], { timeoutMs: 90_000 })
    return { text: (r.stdout + r.stderr).trim() || '(출력 없음)' }
  })
}
