const AUDIO_EXTENSIONS = new Map([
  ['audio/ogg', 'ogg'], ['application/ogg', 'ogg'],
  ['audio/wav', 'wav'], ['audio/x-wav', 'wav'], ['audio/wave', 'wav'], ['audio/vnd.wave', 'wav'],
  ['audio/mpeg', 'mp3'], ['audio/mp3', 'mp3'],
  ['audio/mp4', 'm4a'], ['audio/x-m4a', 'm4a'], ['video/mp4', 'mp4'],
  ['audio/flac', 'flac'], ['audio/x-flac', 'flac'],
  ['audio/webm', 'webm'], ['audio/aac', 'aac'], ['audio/x-aac', 'aac'], ['audio/opus', 'opus'],
])
const ZIP_TYPES = new Set(['application/zip', 'application/x-zip-compressed'])

function filename(title, extension) {
  let stem = String(title || '录音').normalize('NFC').trim()
  stem = stem.replace(/(?:\.(?:wav|ogg|opus|mp3|m4a|flac|webm|mp4|aac|zip))+$/i, '')
  stem = stem.replace(/[\\/:*?"<>|\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/g, '_')
    .replace(/^[.\s]+|[.\s]+$/g, '').slice(0, 120).replace(/[.\s]+$/g, '')
  return `${stem || '录音'}.${extension}`
}

async function responseError(response, type) {
  let detail = ''
  if (type === 'application/json' || type.endsWith('+json')) {
    try {
      const body = await response.json()
      if (typeof body?.detail === 'string') {
        detail = body.detail.replace(/[\u0000-\u001f\u007f-\u009f]/g, ' ').trim().slice(0, 300)
      }
    } catch { /* Keep a useful HTTP error when the error body cannot be read. */ }
  }
  return new Error(`下载失败（HTTP ${response.status}）${detail ? `：${detail}` : '，请重试。'}`)
}

// Fetch in the page's same-origin context before starting a browser download.
// A direct download navigation can otherwise save an API error as audio.json.
export async function fetchRecordDownload(record, { kind = 'audio', fetchImpl = fetch } = {}) {
  if (typeof record?.id !== 'string' || !/^[a-f0-9]{32}$/.test(record.id)) {
    throw new Error('录音标识无效，无法下载。')
  }
  if (kind !== 'audio' && kind !== 'bundle') throw new Error('不支持的下载类型。')

  let response
  try {
    response = await fetchImpl(`/api/records/${record.id}/${kind}`, { cache: 'no-store', credentials: 'same-origin' })
  } catch {
    throw new Error('无法连接本地服务，音频下载未完成，请重试。')
  }
  const type = (response.headers.get('Content-Type') || '').split(';')[0].trim().toLowerCase()
  if (response.status === 206 || response.headers.has('Content-Range')) {
    throw new Error('服务未返回完整文件，已停止下载，请重试。')
  }
  if (response.status !== 200) throw await responseError(response, type)

  const extension = kind === 'bundle' ? 'zip' : AUDIO_EXTENSIONS.get(type)
  if (kind === 'bundle' ? !ZIP_TYPES.has(type) : !extension) {
    throw new Error(kind === 'bundle' ? '服务未返回 ZIP 资料包，已停止下载。' : '服务未返回支持的音频格式，已停止下载。')
  }
  const lengthHeader = response.headers.get('Content-Length')
  let expectedLength = null
  if (lengthHeader !== null) {
    expectedLength = Number(lengthHeader)
    if (!/^\d+$/.test(lengthHeader) || !Number.isSafeInteger(expectedLength)) {
      throw new Error('服务返回的文件长度无效，已停止下载。')
    }
  }

  let blob
  try {
    blob = await response.blob()
  } catch {
    throw new Error('文件读取中断，下载未完成，请重试。')
  }
  if (blob.size === 0) throw new Error('下载的文件为空，请重新读取录音后重试。')
  if (expectedLength !== null && blob.size !== expectedLength) {
    throw new Error('下载的文件不完整，实收大小与服务声明不一致，请重试。')
  }
  const archivedSize = Number(record.size_bytes)
  if (kind === 'audio' && Number.isFinite(archivedSize) && archivedSize > 0 && blob.size !== archivedSize) {
    throw new Error('下载的音频大小与资料库记录不一致，已停止下载，请刷新后重试。')
  }
  if (kind === 'bundle') {
    const magic = new Uint8Array(await blob.slice(0, 4).arrayBuffer())
    if (magic[0] !== 0x50 || magic[1] !== 0x4b || !((magic[2] === 3 && magic[3] === 4) || (magic[2] === 5 && magic[3] === 6))) {
      throw new Error('下载内容不是有效的 ZIP 资料包，已停止下载。')
    }
  }
  return { blob, filename: filename(record.title, extension) }
}
