import test from 'node:test'
import assert from 'node:assert/strict'
import { DEVICE_BACKUP_DELETE_PHRASE, DEVICE_DELETE_PHRASE, deviceDeletePhrase } from './deviceDeletion.js'

test('each way of deleting from the recorder asks for its own words', () => {
  assert.equal(deviceDeletePhrase('backup'), '确认备份本地后删除')
  assert.equal(deviceDeletePhrase('direct'), '确认从设备永久删除')
  assert.equal(deviceDeletePhrase('backup'), DEVICE_BACKUP_DELETE_PHRASE)
  assert.equal(deviceDeletePhrase('direct'), DEVICE_DELETE_PHRASE)
  assert.notEqual(DEVICE_BACKUP_DELETE_PHRASE, DEVICE_DELETE_PHRASE)
})

for (const mode of ['', 'unknown', 'Backup', undefined, null]) {
  test(`an unknown way of deleting (${JSON.stringify(mode)}) has no words that would confirm it`, () => {
    assert.throws(() => deviceDeletePhrase(mode), /删除方式/)
  })
}
