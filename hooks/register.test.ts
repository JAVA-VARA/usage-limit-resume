import { test, expect } from 'claude-code/testing'

// 실제 python 은 띄우지 않는다: 테스트의 process.run 훅이 받아서 기록만 한다
test('rate_limit 이면 스크립트에 hook 과 원래 입력을 넘긴다', async ($, on) => {
  const calls: { argv: readonly string[]; stdin?: string }[] = []
  on('process.run', async (_$, e) => {
    calls.push({ argv: e.argv, stdin: e.init?.stdin })
    return { value: { exitCode: 0, stdout: '', stderr: '' } }
  })
  on('classic.StopFailure', async () => ({}))
  await $.classic.StopFailure({
    error: 'rate_limit',
    last_assistant_message: "You've hit your session limit · resets 6:40pm (Asia/Seoul)",
  })
  expect(calls.length).toBe(1)
  expect(calls[0].argv[0]).toBe('python')
  expect(calls[0].argv[1].endsWith('/scripts/usage_limit_resume.py')).toBe(true)
  expect(calls[0].argv[2]).toBe('hook')
  expect(JSON.parse(calls[0].stdin ?? '{}').last_assistant_message).toContain('resets 6:40pm')
})

test('python 이 없으면(9009) python3 로 다시 부른다', async ($, on) => {
  const tried: string[] = []
  on('process.run', async (_$, e) => {
    tried.push(e.argv[0])
    return { value: { exitCode: e.argv[0] === 'python' ? 9009 : 0, stdout: '', stderr: '' } }
  })
  on('classic.StopFailure', async () => ({}))
  await $.classic.StopFailure({ error: 'rate_limit', last_assistant_message: 'resets 6:40pm' })
  expect(tried).toEqual(['python', 'python3'])
})

test('/usage-resume doctor 는 doctor 를, 인자가 없으면 status 를 부른다', async ($, on) => {
  const subs: string[] = []
  on('process.run', async (_$, e) => {
    subs.push(e.argv[2])
    return { value: { exitCode: 0, stdout: 'ok', stderr: '' } }
  })
  await $.command.run({ command: 'usage-resume', args: 'doctor' })
  await $.command.run({ command: 'usage-resume', args: '' })
  expect(subs).toEqual(['doctor', 'status'])
})

test('다른 오류는 스크립트를 부르지 않는다', async ($, on) => {
  let ran = 0
  on('process.run', async () => {
    ran++
    return { value: { exitCode: 0, stdout: '', stderr: '' } }
  })
  on('classic.StopFailure', async () => ({}))
  await $.classic.StopFailure({ error: 'overloaded' })
  expect(ran).toBe(0)
})
