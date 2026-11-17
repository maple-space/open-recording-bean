import test from 'node:test'
import assert from 'node:assert/strict'
import * as visibility from './workflow.js'

test('a new library refresh displays an archive restored by batch sync in the same tab', () => {
  const trashed = new Map([['restored', Symbol('trash')], ['still-trashed', Symbol('trash')]])
  const startedWith = new Map(trashed)
  const records = [{ id: 'restored', title: '重新同步的录音' }, { id: 'other' }]
  assert.deepEqual(visibility.visibleRefreshedRecords(records, trashed, startedWith), records)
  assert.equal(trashed.has('restored'), false, 'restored archives can be edited again')
  assert.equal(trashed.has('still-trashed'), true, 'late save responses cannot restore absent archives')
})

test('a library request started before trash cannot bring back its stale record', () => {
  const trashed = new Map()
  const startedWith = new Map(trashed)
  trashed.set('removed', Symbol('trash'))
  assert.deepEqual(visibility.visibleRefreshedRecords([{ id: 'removed' }, { id: 'other' }], trashed, startedWith), [{ id: 'other' }])
  assert.equal(trashed.has('removed'), true)
})

test('trashing a restored archive again invalidates an earlier library request', () => {
  const trashed = new Map([['again', Symbol('first trash')]])
  const startedWith = new Map(trashed)
  trashed.delete('again')
  trashed.set('again', Symbol('second trash'))
  assert.deepEqual(visibility.visibleRefreshedRecords([{ id: 'again' }], trashed, startedWith), [])
  assert.equal(trashed.has('again'), true)
})
