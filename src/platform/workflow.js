import { api, jobFailure, terminalStatuses } from './api.js'

// Track separately from submission so callers can choose when to await the result.
export async function trackJobResult(job, {
  request = api, onUpdate = () => {}, isMounted = () => true,
  wait = () => new Promise((resolve) => setTimeout(resolve, 1000)), hydrateRecord = true,
} = {}) {
  while (!terminalStatuses.has(job.status)) {
    await wait()
    job = await request(`/api/jobs/${encodeURIComponent(job.id)}`)
    if (!isMounted()) return null
    onUpdate(job)
  }
  if (job.status === 'failed') throw jobFailure(job)
  if (job.status === 'cancelled') throw new Error('任务已取消。')
  const result = job.result
  if (hydrateRecord && result?.record_id && !result.record) {
    const record = await request(`/api/records/${encodeURIComponent(result.record_id)}`)
    // Never attach full text to the job already held in the page's job list.
    return { ...result, record }
  }
  return result
}

export function matchingArchive(records, kind, deviceId, fileId) {
  if (!deviceId) return null
  return records.find((record) => record.device_type === kind && record.device_id === deviceId && String(record.original_id) === String(fileId)) || null
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
