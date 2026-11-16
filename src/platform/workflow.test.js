import test from 'node:test'
import assert from 'node:assert/strict'
import { matchingArchive, processApiRecord, settingsForProvider, usesLocalTranscription, textFilename, buildSelectedArchive } from './workflow.js'
import { strToU8, strFromU8, zipSync, unzipSync } from 'fflate'

test('archive reuse requires all three identity fields', () => {
  const record = { id: 'saved', device_type: 'feishu', device_id: 'device-a', original_id: '1' }
  assert.equal(matchingArchive([record], 'feishu', 'device-a', 1), record)
  assert.equal(matchingArchive([record], 'local', 'device-a', 1), null)
  assert.equal(matchingArchive([record], 'feishu', 'device-b', 1), null)
  assert.equal(matchingArchive([record], 'feishu', '', 1), null)
})

test('one-click processing preserves an existing corrected transcript', async () => {
  const record = { transcript: '人工更正后的逐字稿' }
  const result = await processApiRecord(record, {
    transcribe: () => assert.fail('must not retranscribe'),
    summarize: async (source) => ({ ...source, summary: '总结' }),
  })
  assert.equal(result.transcript, record.transcript)
})

test('one-click processing stops on empty ASR and passes a new transcript to summary', async () => {
  await assert.rejects(processApiRecord({}, { transcribe: async () => ({ transcript: '' }), summarize: () => assert.fail() }), /未得到可用转录/)
  const seen = []
  const result = await processApiRecord({}, { transcribe: async () => { seen.push('asr'); return { transcript: '新转录' } }, summarize: async (source) => { seen.push(source.transcript); return { ...source, summary: '完成' } } })
  assert.deepEqual(seen, ['asr', '新转录'])
  assert.equal(result.summary, '完成')
})

test('CLI processing uses local ASR even with older API ASR settings', () => {
  assert.equal(usesLocalTranscription({ provider: 'codex', asr: 'api' }), true)
  assert.equal(usesLocalTranscription({ provider: 'claude', asr: 'api' }), true)
  assert.equal(settingsForProvider({ asr: 'api' }, 'codex').asr, 'local')
  assert.equal(usesLocalTranscription({ provider: 'api', asr: 'api' }), false)
})

test('text export filename cannot introduce path separators', () => {
  assert.equal(textFilename('会议/客户:一', 'txt'), '会议_客户_一.txt')
})


function bundle(record, overrides = {}) {
  return zipSync({ 'audio.ogg': strToU8('OggS test audio'), 'transcript.txt': strToU8(record.transcript || ''), 'summary.md': strToU8('summary'), 'metadata.json': strToU8(JSON.stringify(record)), ...overrides })
}

test('selected ZIP keeps same-title recordings in separate ID folders with their text', async () => {
  const records = [{ id: 'a'.repeat(32), title: '同名录音', transcript: '第一条' }, { id: 'b'.repeat(32), title: '同名录音', transcript: '第二条' }]
  const zip = unzipSync(await buildSelectedArchive(records, async (record) => bundle(record)))
  assert.equal(Object.keys(zip).length, 8)
  for (const record of records) {
    assert.equal(strFromU8(zip[`${record.id}/transcript.txt`]), record.transcript)
    assert.equal(JSON.parse(strFromU8(zip[`${record.id}/metadata.json`])).id, record.id)
  }
})

test('selected ZIP rejects a missing download or mismatched bundle instead of returning partial success', async () => {
  const records = [{ id: 'a'.repeat(32) }, { id: 'b'.repeat(32) }]
  await assert.rejects(buildSelectedArchive(records, async (record) => { if (record.id === records[1].id) throw new Error('下载失败'); return bundle(record) }), /下载失败/)
  await assert.rejects(buildSelectedArchive(records.slice(0, 1), async () => bundle(records[1])), /不匹配/)
  await assert.rejects(buildSelectedArchive(records.slice(0, 1), async (record) => bundle(record, { 'audio.ogg': new Uint8Array() })), /不完整/)
})
