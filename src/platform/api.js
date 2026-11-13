export async function api(path, options = {}) {
  const isForm = options.body instanceof FormData
  let response
  try {
    response = await fetch(path, {
      cache: 'no-store',
      ...options,
      headers: { ...(options.body && !isForm ? { 'Content-Type': 'application/json' } : {}), ...options.headers },
      body: options.body && !isForm ? JSON.stringify(options.body) : options.body,
    })
  } catch {
    throw new Error('无法连接本机服务。请确认本地平台已启动，然后重试。')
  }
  const data = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = data?.detail || data?.error
    throw new Error(typeof detail === 'string' ? detail : detail?.message || `请求未完成（HTTP ${response.status}），请检查输入后重试。`)
  }
  if (data == null) throw new Error('本机服务返回了无效数据，请检查服务是否正确启动。')
  return data
}

export function jobFailure(job) {
  return new Error(job.error || '任务执行失败，请检查设置后重试。')
}

// 'feishu' is how the service and the stored records name the recorder (soundcore Work, also sold as 飞书录音豆).
export const deviceNames = { feishu: '录音豆', local: '本地导入' }
export const jobNames = { device_delete: '删除设备录音', record_start: '开始录音', record_pause: '暂停录音', scan: '搜索设备', connect: '连接设备', refresh: '读取列表', download: '下载录音', transcribe: '语音转文字', summarize: '整理文字', process: '转录并交给 Agent 整理' }
export const terminalStatuses = new Set(['completed', 'failed', 'cancelled'])
export const initialSettings = { provider: 'api', asr: 'local', codex_model: '', claude_model: '', asr_model: 'sensevoice-small' }

// The setting value a local speech model is stored under. The short spellings of one model are that model;
// nothing chosen means the model that ./start.sh downloads.
export function localModelKey(value) {
  const id = String(value || '').trim()
  if (!id || ['sensevoice-small', 'sensevoice', 'sense-voice'].includes(id.toLowerCase())) return 'sensevoice-small'
  return ['funasr-nano', 'fun-asr-nano', 'nano'].includes(id.toLowerCase()) ? 'funasr-nano' : id
}

const regularWhisper = new Set(['tiny', 'tiny.en', 'base', 'base.en', 'small', 'small.en', 'medium', 'medium.en', 'large-v1', 'large-v2', 'large-v3'])

// What a local speech model is called on screen: its full name, which is the name of the model repository
// it comes from (SenseVoiceSmall, Fun-ASR-Nano-GGUF, faster-whisper-small) and nothing else. The local service reports the
// exact name (`names`, from /api/health); the rule below gives the same name when it does not.
export function localModelName(value, names) {
  const id = localModelKey(value)
  if (names?.[id]?.name) return names[id].name
  if (id === 'sensevoice-small') return 'SenseVoiceSmall'
  if (id === 'funasr-nano') return 'Fun-ASR-Nano-GGUF'
  if (/^[~./\\]|\\|^[A-Za-z]:/.test(id)) return id        // a model directory: shown as typed
  if (id.includes('/')) return id.split('/').pop()           // organisation/repository: the repository is the model's name
  return regularWhisper.has(id) ? `faster-whisper-${id}` : id
}

// The models offered in the settings dropdown: what this computer has, plus the saved one if it is something else.
export function localModelChoices(installed, current) {
  const list = [...new Set((installed || []).map(localModelKey))]
  const saved = String(current || '').trim() ? localModelKey(current) : ''
  return saved && !list.includes(saved) ? [...list, saved] : list
}

export function formatBytes(value) {
  if (!Number.isFinite(Number(value)) || value == null) return '大小未知'
  if (Number(value) < 1024) return `${value} B`
  if (Number(value) < 1024 * 1024) return `${(Number(value) / 1024).toFixed(1)} KB`
  if (Number(value) < 1024 * 1024 * 1024) return `${(Number(value) / 1024 / 1024).toFixed(1)} MB`
  return `${(Number(value) / 1024 / 1024 / 1024).toFixed(1)} GB`
}

export function formatDuration(value) {
  if (!value) return '时长未知'
  const seconds = Math.round(Number(value) / 1000)
  return seconds < 3600 ? `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}` : `${Math.floor(seconds / 3600)}:${String(Math.floor(seconds / 60) % 60).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`
}

export function formatDate(value) {
  if (!value) return '时间未知'
  const numeric = Number(value)
  const date = new Date(Number.isFinite(numeric) ? numeric * (numeric < 1e12 ? 1000 : 1) : value)
  if (Number.isNaN(date.getTime())) return '时间未知'
  return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(date)
}

export function humanError(value) {
  const message = typeof value === 'string' ? value : value?.message || '操作未完成，请重试。'
  if (/Traceback \(most recent call last\)|^\s*File "/m.test(message)) return '本机任务执行失败，请查看本机服务日志了解详情。'
  return message.slice(0, 700)
}
