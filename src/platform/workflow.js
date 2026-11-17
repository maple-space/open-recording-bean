import { api, jobFailure, terminalStatuses } from './api.js'

export function visibleRefreshedRecords(records, trashedIds, startedWith) {
  return records.filter((record) => {
    if (!trashedIds.has(record.id)) return true
    // A GET begun after this trash operation can only contain a recreated
    // archive. Older requests must not resurrect a record just removed here.
    if (startedWith.get(record.id) !== trashedIds.get(record.id)) return false
    trashedIds.delete(record.id)
    return true
  })
}

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

// Batch sync uses compact results; single-record callers still use trackJobResult.
export async function runDownloadJob({
  label, body, endpoint = '/api/devices/feishu/actions', includeSkipped = false,
  run, onUpdate, setRecordId, setNotice, showError, refresh,
  request = api, isMounted = () => true, wait,
}) {
  const job = await run(label, async () => {
    const job = await request(endpoint, { method: 'POST', body })
    onUpdate(job)
    return job
  })
  if (!job) return null
  try {
    const result = await trackJobResult(job, { request, onUpdate, isMounted, wait, hydrateRecord: false })
    if (!result || !isMounted()) return null
    const lastRecordId = result.record_id || result.record?.id
    if (lastRecordId) setRecordId(lastRecordId)
    const reused = includeSkipped && result.skipped_ids?.length ? `，其中 ${result.skipped_ids.length} 条复用已有归档` : ''
    const missing = result.missing_ids?.length ? `已跳过 ${result.missing_ids.length} 条设备目录中不存在的录音（${result.missing_ids.join('、')}）。` : ''
    setNotice({ type: 'success', text: `${result.count || 0} 条录音已核验并保存在本机资料库${reused}；设备原件已保留。${missing}`, recordId: lastRecordId })
    return result
  } catch (error) {
    if (isMounted()) showError(error)
    return null
  } finally {
    if (isMounted()) refresh()
  }
}

// A completed resume settles the same original request through its ancestor chain.
export function settledDownloadJobs(jobs) {
  const byId = new Map(jobs.map((job) => [job.id, job]))
  const settled = new Set()
  const isDownload = (job) => job?.type === 'download' && job.device_type === 'feishu'
  for (const completed of jobs) {
    if (!isDownload(completed) || completed.status !== 'completed') continue
    const request = completed.retry_request
    if (!request?.device_id || !request.ids?.length) continue
    const ids = new Set(request.ids)
    const seen = new Set([completed.id])
    let parentId = completed.resumed_from
    while (parentId && !seen.has(parentId)) {
      seen.add(parentId)
      const parent = byId.get(parentId)
      const original = parent?.retry_request
      if (!isDownload(parent) || original?.device_id !== request.device_id ||
          original.ids?.length !== ids.size || !original.ids.every((id) => ids.has(id))) break
      settled.add(parentId)
      parentId = parent.resumed_from
    }
  }
  return settled
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
