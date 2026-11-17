import test from 'node:test'
import assert from 'node:assert/strict'
import * as workflow from './workflow.js'

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function harness(overrides = {}) {
  const submission = deferred(), completion = deferred(), polling = deferred()
  const state = { busy: false, notices: [], errors: [], updates: [], recordIds: [], requests: [], refreshes: 0 }
  const refresh = async () => { state.refreshes += 1 }
  const showError = (error) => state.errors.push(error.message)
  const options = {
    label: '同步录音到本机', body: { action: 'download', device_id: 'bean-a', ids: ['123', '124'] },
    endpoint: '/api/devices/feishu/actions', includeSkipped: true,
    run: async (label, action) => {
      if (state.busy) return null
      state.busy = label
      try { return await action() } catch (error) { showError(error); return null }
      finally { state.busy = false; await refresh() }
    },
    request: async (path, init) => {
      state.requests.push({ path, init, busy: state.busy })
      if (init?.method === 'POST') return submission.promise
      assert.equal(path, '/api/jobs/job', 'batch tracking never fetches a complete recording')
      polling.resolve()
      return completion.promise
    },
    wait: async () => {},
    onUpdate: (job) => state.updates.push(job),
    setRecordId: (id) => state.recordIds.push(id),
    setNotice: (notice) => state.notices.push(notice),
    showError, refresh,
    ...overrides,
  }
  const start = () => {
    assert.equal(typeof workflow.runDownloadJob, 'function', 'downloads expose submission and background tracking separately')
    return workflow.runDownloadJob(options)
  }
  return { state, submission, completion, polling, start, options }
}

for (const resume of [false, true]) {
  test(`${resume ? 'resumed' : 'new'} download releases busy after POST and preserves its completion notice and refresh`, async () => {
    const setup = harness(resume ? { label: '继续同步录音', endpoint: '/api/jobs/old%2Fjob/resume', body: undefined, includeSkipped: false } : {})
    const { state, submission, completion, polling, options } = setup
    let settled = false
    const tracking = setup.start().then((value) => { settled = true; return value })
    assert.equal(state.busy, options.label, 'submission is still guarded')
    submission.resolve({ id: 'job', status: 'queued' })
    await polling.promise
    assert.equal(state.busy, false, 'library actions are available while this batch keeps running')
    assert.equal(state.requests[1].busy, false, 'polling is outside the guarded action')
    assert.equal(settled, false)
    assert.deepEqual(state.notices, [])
    assert.equal(state.refreshes, 1, 'submission refreshes the job list')
    assert.deepEqual(state.requests[0], { path: options.endpoint, init: { method: 'POST', body: options.body }, busy: options.label })
    const result = { count: 2, record_ids: ['first', 'last'], record_id: 'last', skipped_ids: ['123'] }
    completion.resolve({ id: 'job', status: 'completed', result })
    assert.equal(await tracking, result)
    assert.deepEqual(state.recordIds, ['last'])
    assert.deepEqual(state.notices, [{ type: 'success', recordId: 'last', text: `2 条录音已核验并保存在本机资料库${resume ? '' : '，其中 1 条复用已有归档'}；设备原件已保留。` }])
    assert.deepEqual(state.errors, [])
    assert.equal(state.refreshes, 2, 'completion refreshes the library after the busy guard has already ended')
    assert.equal(state.updates.length, 2)
  })
}

for (const status of ['failed', 'cancelled']) {
  test(`background ${status} download reports the existing error and refreshes without success`, async () => {
    const { state, submission, completion, polling, start } = harness()
    const tracking = start()
    submission.resolve({ id: 'job', status: 'running' })
    await polling.promise
    assert.equal(state.busy, false)
    completion.resolve({ id: 'job', status, error: '已完成 1/2 条；完整性冲突', result: { record_id: 'partial' } })
    assert.equal(await tracking, null)
    assert.deepEqual(state.errors, [status === 'failed' ? '已完成 1/2 条；完整性冲突' : '任务已取消。'])
    assert.deepEqual(state.notices, [])
    assert.equal(state.refreshes, 2)
  })
}

test('download submission failure releases busy and does not start tracking', async () => {
  const { state, submission, start } = harness()
  const tracking = start()
  submission.reject(new Error('录音豆正在执行后台任务'))
  assert.equal(await tracking, null)
  assert.equal(state.busy, false)
  assert.deepEqual(state.errors, ['录音豆正在执行后台任务'])
  assert.equal(state.requests.length, 1)
  assert.equal(state.refreshes, 1)
})

test('download refused by the busy guard starts neither submission nor tracking', async () => {
  const { state, start } = harness()
  state.busy = 'other action'
  assert.equal(await start(), null)
  assert.deepEqual(state.requests, [])
  assert.deepEqual(state.notices, [])
  assert.equal(state.refreshes, 0)
})

test('leaving the page during a download does not show a false completion notice', async () => {
  let mounted = true
  const { state, submission, completion, polling, start } = harness({ isMounted: () => mounted })
  const tracking = start()
  submission.resolve({ id: 'job', status: 'running' })
  await polling.promise
  mounted = false
  completion.resolve({ id: 'job', status: 'completed', result: { count: 1, record_id: 'last' } })
  assert.equal(await tracking, null)
  assert.deepEqual(state.notices, [])
  assert.deepEqual(state.recordIds, [])
  assert.equal(state.refreshes, 1)
})

test('resumed download completion reports recordings missing from the device', async () => {
  const { state, submission, completion, polling, start } = harness({ includeSkipped: false })
  const tracking = start()
  submission.resolve({ id: 'job', status: 'running' })
  await polling.promise
  completion.resolve({ id: 'job', status: 'completed', result: {
    count: 1, record_id: 'last', missing_ids: ['123', '125'], missing_count: 2,
  } })
  await tracking
  assert.match(state.notices[0].text, /已跳过 2 条设备目录中不存在的录音（123、125）/)
})
