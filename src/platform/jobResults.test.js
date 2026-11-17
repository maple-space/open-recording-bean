import test from 'node:test'
import assert from 'node:assert/strict'
import * as workflow from './workflow.js'

// This is the tracker used by PortalApp.submitJob, also usable after a separate POST.
function track(job, options = {}) {
  assert.equal(typeof workflow.trackJobResult, 'function', 'job tracking exposes a testable result contract')
  return workflow.trackJobResult(job, { wait: async () => {}, ...options })
}

test('completed compact job hydrates the archive result without enlarging stored jobs', async () => {
  const record = { id: 'a/b', transcript: 'large transcript', summary: 'saved summary' }
  const completed = Object.freeze({ id: 'job', status: 'completed', result: Object.freeze({ record_id: record.id, record_ids: [record.id], count: 1 }) })
  const requests = [], updates = []
  const result = await track({ id: 'job', status: 'queued' }, {
    request: async (path) => { requests.push(path); return path.startsWith('/api/jobs/') ? completed : record },
    onUpdate: (job) => updates.push(job),
  })
  assert.deepEqual(requests, ['/api/jobs/job', '/api/records/a%2Fb'])
  assert.equal(result.record, record, 'archiveFile still receives the saved recording through result.record')
  assert.equal(result.count, 1)
  assert.deepEqual(updates, [completed])
  assert.notEqual(result, completed.result)
  assert.equal(updates[0].result.record, undefined)
  assert.doesNotMatch(JSON.stringify(updates), /large transcript|saved summary/)
})

test('already completed job fetches only its last record', async () => {
  const calls = []
  const result = await track({ status: 'completed', result: { record_ids: ['first', 'last'], record_id: 'last' } }, {
    request: async (path) => { calls.push(path); return { id: 'last' } },
  })
  assert.deepEqual(calls, ['/api/records/last'])
  assert.equal(result.record.id, 'last')
})

test('batch completion can use ids when the last recording was concurrently trashed', async () => {
  const result = { count: 38, record_ids: ['first', 'last'], record_id: 'last' }
  const saved = await track({ status: 'completed', result }, {
    hydrateRecord: false,
    request: () => assert.fail('a completed batch does not depend on its last record still existing'),
  })
  assert.equal(saved, result)
  assert.equal(saved.count, 38)
})

test('legacy embedded record and records remain available without another fetch', async () => {
  const record = { id: 'old', transcript: 'old text' }
  const result = { record_id: 'old', record, records: [record], notice: 'manual edits preserved' }
  assert.equal(await track({ status: 'completed', result }, { request: () => assert.fail('legacy record is already available') }), result)
})

test('failed partial and cancelled jobs retain their errors without fetching a record', async () => {
  for (const status of ['failed', 'cancelled']) {
    const job = { status, error: '已完成 1/2 条；完整性冲突', result: { record_id: 'partial', completed_ids: ['123'] } }
    await assert.rejects(() => track(job, { request: () => assert.fail('do not hydrate unsuccessful jobs') }),
      status === 'failed' ? /已完成 1\/2 条；完整性冲突/ : /任务已取消/)
  }
})

test('scan arrays and empty direct-delete results retain their original shape', async () => {
  for (const result of [[{ address: 'bean' }], { count: 2, record_ids: [], record_id: null }, null]) {
    assert.equal(await track({ status: 'completed', result }, { request: () => assert.fail('no record to fetch') }), result)
  }
})

test('record lookup failure is visible to archive callers and keeps the job compact', async () => {
  const job = { status: 'completed', result: { record_id: 'trashed' } }
  await assert.rejects(() => track(job, { request: async () => { throw new Error('记录或任务不存在') } }), /记录或任务不存在/)
  assert.deepEqual(job.result, { record_id: 'trashed' })
})

test('leaving the page during polling stops before result hydration', async () => {
  const calls = []
  const result = await track({ id: 'job', status: 'running' }, {
    request: async (path) => { calls.push(path); return { status: 'completed', result: { record_id: 'one' } } },
    isMounted: () => false,
    onUpdate: () => assert.fail('unmounted page must not receive updates'),
  })
  assert.equal(result, null)
  assert.deepEqual(calls, ['/api/jobs/job'])
})
