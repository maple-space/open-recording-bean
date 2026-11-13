import test from 'node:test'
import assert from 'node:assert/strict'
import { DEVICE_DELETE_PHRASE, deleteDeviceFiles } from './deviceDeletion.js'

const request = { type: 'device', deviceId: 'bean-a', files: [{ original_id: '123', title: '录音一' }, { original_id: '456', title: '录音二' }] }
function harness(overrides = {}) {
  const calls = []
  const actions = {
    getSnapshot: () => ({ connected: true, device_id: 'bean-a' }),
    archive: async (file) => { calls.push(`archive:${file.original_id}`); return { id: file.original_id } },
    flush: async (record) => { calls.push(`flush:${record.id}`) },
    verifyBackup: async (record) => { calls.push(`verify:${record.id}`) },
    remove: async (id) => { calls.push(`delete:${id}`) },
    ...overrides,
  }
  return { calls, actions }
}

test('direct deletion uses only the delete protocol, with no backup or download', async () => {
  const { calls, actions } = harness()
  const count = await deleteDeviceFiles(request, { mode: 'direct', phrase: '确认从设备永久删除' }, actions)
  assert.equal(count, 2)
  assert.deepEqual(calls, ['delete:123', 'delete:456'])
})

for (const phrase of ['', '永久删除', '确认从设备永久删除 ', '确认从设备删除']) {
  test(`inexact confirmation ${JSON.stringify(phrase)} cannot start backup or deletion`, async () => {
    const { calls, actions } = harness()
    await assert.rejects(deleteDeviceFiles(request, { mode: 'direct', phrase }, actions), /确认从设备永久删除/)
    assert.deepEqual(calls, [])
  })
}

test('backup mode verifies every backup before the first deletion and again before each delete', async () => {
  const { calls, actions } = harness()
  await deleteDeviceFiles(request, { mode: 'backup', phrase: '确认备份本地后删除' }, actions)
  assert.deepEqual(calls, ['archive:123', 'flush:123', 'verify:123', 'archive:456', 'flush:456', 'verify:456', 'verify:123', 'delete:123', 'verify:456', 'delete:456'])
})

test('a failed backup never silently falls back to direct deletion', async () => {
  const { calls, actions } = harness({ verifyBackup: async () => { throw new Error('备份读取失败') } })
  await assert.rejects(deleteDeviceFiles(request, { mode: 'backup', phrase: '确认备份本地后删除' }, actions), /备份读取失败/)
  assert.ok(!calls.some((value) => value.startsWith('delete:')))
})

test('device change before direct deletion prevents every delete command', async () => {
  const { calls, actions } = harness({ getSnapshot: () => ({ connected: true, device_id: 'bean-b' }) })
  await assert.rejects(deleteDeviceFiles(request, { mode: 'direct', phrase: DEVICE_DELETE_PHRASE }, actions), /设备连接已变化/)
  assert.deepEqual(calls, [])
})

test('device change during final backup verification also prevents deletion', async () => {
  let deviceId = 'bean-a'
  let verifications = 0
  const { calls, actions } = harness({
    getSnapshot: () => ({ connected: true, device_id: deviceId }),
    verifyBackup: async () => { if (++verifications === 3) deviceId = 'bean-b' },
  })
  await assert.rejects(deleteDeviceFiles(request, { mode: 'backup', phrase: '确认备份本地后删除' }, actions), /设备连接已变化/)
  assert.ok(!calls.some((value) => value.startsWith('delete:')))
})

test('partial failure reports confirmed count and does not retry a destructive command', async () => {
  const removed = []
  const { actions } = harness({ remove: async (id) => { removed.push(id); if (id === 456) throw new Error('设备拒绝删除') } })
  await assert.rejects(deleteDeviceFiles(request, { mode: 'direct', phrase: DEVICE_DELETE_PHRASE }, actions), /已完成 1 条.*设备拒绝删除/)
  assert.deepEqual(removed, [123, 456])
})

test('unknown mode cannot bypass backup checks', async () => {
  const { calls, actions } = harness()
  await assert.rejects(deleteDeviceFiles(request, { mode: 'unknown', phrase: DEVICE_DELETE_PHRASE }, actions), /删除方式/)
  assert.deepEqual(calls, [])
})

for (const [mode, phrase] of [
  ['backup', '确认从设备永久删除'],
  ['direct', '确认备份本地后删除'],
  ['backup', '确认备份本地后删除 '],
  ['backup', ''],
]) {
  test(`mode ${mode} rejects mismatched confirmation ${JSON.stringify(phrase)}`, async () => {
    const { calls, actions } = harness()
    await assert.rejects(deleteDeviceFiles(request, { mode, phrase }, actions), /请输入/)
    assert.deepEqual(calls, [])
  })
}
