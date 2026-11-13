export function matchingArchive(records, kind, deviceId, fileId) {
  if (!deviceId) return null
  return records.find((record) => record.device_type === kind && record.device_id === deviceId && String(record.original_id) === String(fileId)) || null
}

// The catalog size describes the saved WAV/Ogg, not the device's encrypted
// source size. Read every saved byte before allowing device deletion.
export async function verifyCompleteDeviceBackup(record, deviceId, file, { loadRecords, loadAudio }) {
  const fail = (reason) => new Error(`本机音频备份${reason}，已停止删除。`)
  function activeRecord(values) {
    if (!Array.isArray(values)) throw fail('状态无法确认')
    const current = values.find((item) => item.id === record.id)
    if (!current || current.trashed_at || !matchingArchive([current], 'feishu', deviceId, file.original_id)) throw fail('已失效或与设备录音不匹配')
    if (!Number.isSafeInteger(current.size_bytes) || current.size_bytes <= 0) throw fail('保存大小无效')
    return current
  }
  if (!/^[a-f0-9]{32}$/.test(record.id) || !matchingArchive([record], 'feishu', deviceId, file.original_id)) throw fail('与设备录音不匹配')
  const current = activeRecord(await loadRecords())
  const response = await loadAudio(current.id)
  const length = response.headers.get('content-length')
  if (response.status !== 200 || response.headers.has('content-range') || !/^\d+$/.test(length || '') || Number(length) !== current.size_bytes) {
    await response.body?.cancel().catch(() => {})
    throw fail('未返回与保存记录一致的完整文件')
  }
  const reader = response.body?.getReader()
  if (!reader) throw fail('无法读取')
  let received = 0
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      received += value.byteLength
      if (received > current.size_bytes) throw fail('实收大小超过保存记录')
    }
    if (received !== current.size_bytes) throw fail('实收大小与保存记录不一致')
  } catch (error) {
    if (error?.message?.startsWith('本机音频备份')) throw error
    throw fail('读取中断')
  } finally {
    await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
  // A different page may move the record to Trash while this stream is open.
  const after = activeRecord(await loadRecords())
  if (after.size_bytes !== current.size_bytes) throw fail('在核对期间发生变化，请重新确认')
  return { record: after, size_bytes: received }
}

export function usesLocalTranscription(settings) {
  return settings.provider !== 'api' || settings.asr === 'local'
}

export function settingsForProvider(settings, provider) {
  return { ...settings, provider, ...(provider === 'api' ? {} : { asr: 'local' }) }
}

// One-click processing must use an existing, possibly hand-corrected transcript.
export async function processApiRecord(record, { transcribe, summarize }) {
  const source = record.transcript?.trim() ? record : await transcribe(record)
  if (!source?.transcript?.trim()) throw new Error('未得到可用转录，暂不生成总结。')
  return summarize(source)
}

export function textFilename(title, extension) {
  return `${String(title || '录音').replace(/[\\/:*?"<>|\u0000-\u001f]/g, '_').slice(0, 120)}.${extension}`
}
import { strFromU8, unzipSync, zipSync } from 'fflate'


export function canAutoDiscover({ enabled, connected, busy, hasDeviceJob }) {
  return Boolean(enabled && connected && !busy && !hasDeviceJob)
}

export async function buildSelectedArchive(records, loadBundle, progress) {
  const unique = [...new Map(records.map((record) => [record.id, record])).values()]
  if (!unique.length) throw new Error('请先选择录音。')
  const output = {}
  for (let index = 0; index < unique.length; index += 1) {
    const record = unique[index]
    if (!/^[a-f0-9]{32}$/.test(record.id)) throw new Error('录音标识无效，无法导出。')
    progress?.(index + 1, unique.length)
    const files = unzipSync(await loadBundle(record))
    const audio = Object.keys(files).filter((name) => /^audio\.[a-z0-9]+$/.test(name))
    if (audio.length !== 1 || !files[audio[0]].length || !['transcript.txt', 'summary.md', 'metadata.json'].every((name) => Object.hasOwn(files, name))) {
      throw new Error(`“${record.title || record.id}”的资料包不完整，已停止导出。`)
    }
    let metadata
    try { metadata = JSON.parse(strFromU8(files['metadata.json'])) } catch { throw new Error('资料包元数据无效，已停止导出。') }
    if (metadata.id !== record.id) throw new Error('资料包与所选录音不匹配，已停止导出。')
    for (const name of [...audio, 'transcript.txt', 'summary.md', 'metadata.json']) output[`${record.id}/${name}`] = files[name]
  }
  // Audio is already compressed. Storing it avoids a long recompression pass.
  return zipSync(output, { level: 0 })
}
