// Small pure helpers for the realtime input page, kept apart so they can be tested without a browser.

const statuses = {
  off: { tone: 'idle', text: '未开启' },
  starting: { tone: 'busy', text: '正在让录音豆开始录音…' },
  streaming: { tone: 'on', text: '正在传声' },
  paused: { tone: 'wait', text: '已在录音豆上暂停。再按一下录音豆的按键就会继续。' },
  waiting_device: { tone: 'wait', text: '录音豆的蓝牙断开了，正在等它重新连上…' },
  retrying: { tone: 'wait', text: '传声中断，正在自动重试…' },
}

export function statusView(live) {
  if (!live) return { tone: 'idle', text: '正在读取状态…' }
  const view = statuses[live.status] || statuses.off
  return live.status === 'retrying' && live.error ? { ...view, text: `${view.text}（${live.error}）` } : view
}

// The page shows the newest sentences of one session; the complete text is in that session's file.
export const PAGE_ROWS = 5000

// The server returns only rows at or after the cursor we send. Its numbering restarts when the
// local service restarts (a cursor ahead of the server means exactly that), and every time
// realtime input is switched on a new record begins. Either way the page starts over and asks again.
export function applyTranscript(state, transcript) {
  if (!transcript) return state
  const session = transcript.session?.started ?? null
  const tracked = state.session !== undefined
  if (transcript.next_cursor < state.cursor || (tracked && state.session !== session)) return { segments: [], cursor: 0, session }
  const known = state.segments.length ? state.segments[state.segments.length - 1].index : -1
  const fresh = (transcript.segments || []).filter((row) => row.index > known)
  if (!fresh.length && transcript.next_cursor === state.cursor) return state
  const segments = [...state.segments, ...fresh]
  return { segments: segments.length > PAGE_ROWS ? segments.slice(-PAGE_ROWS) : segments, cursor: transcript.next_cursor, session }
}

// Prefer what the person chose last time, then a virtual sound card; never silently fall back to speakers.
export function pickOutput(outputs, remembered) {
  const devices = outputs?.devices || []
  if (remembered && devices.some((device) => device.name === remembered)) return remembered
  return outputs?.suggested || ''
}

export function elapsedText(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0))
  const pad = (value) => String(value).padStart(2, '0')
  return `${Math.floor(total / 3600)}:${pad(Math.floor(total / 60) % 60)}:${pad(total % 60)}`
}

export function storageText(kilobytes) {
  if (!Number.isFinite(Number(kilobytes)) || kilobytes == null) return '未知'
  const megabytes = Number(kilobytes) / 1024
  return megabytes >= 1024 ? `${(megabytes / 1024).toFixed(1)} GB` : `${Math.round(megabytes)} MB`
}

// A level of 0.02 is a quiet room and 0.3 is close speech; a square root makes both visible on one bar.
export function levelPercent(level) {
  return Math.max(0, Math.min(100, Math.round(Math.sqrt(Math.max(0, Number(level) || 0)) * 100)))
}

export function countText(value) {
  return Math.max(0, Math.round(Number(value) || 0)).toLocaleString('en-US')
}

export function durationText(seconds) {
  const minutes = Math.floor(Math.max(0, Number(seconds) || 0) / 60)
  if (minutes < 1) return '不到 1 分钟'
  if (minutes < 60) return `${minutes} 分钟`
  return minutes % 60 ? `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分` : `${minutes / 60} 小时`
}

export function dayClockText(epochSeconds) {
  const date = new Date(Number(epochSeconds) * 1000)
  if (Number.isNaN(date.getTime())) return ''
  const pad = (value) => String(value).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

export function clockText(epochSeconds) {
  const date = new Date(Number(epochSeconds) * 1000)
  if (Number.isNaN(date.getTime())) return ''
  return new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(date)
}

// ---- reading a saved record -------------------------------------------------------------------

// "2026-11-13_20-46-57-2.txt" -> "2026-11-13 20:46"
export function recordTitle(name) {
  const match = /^(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-\d{2}/.exec(name || '')
  return match ? `${match[1]} ${match[2]}:${match[3]}` : name || ''
}

export function hourText(hour) {
  return `${String(hour).padStart(2, '0')}:00`
}

// The hours of a record grouped by day; each keeps its position in the outline.
export function groupDays(hours) {
  const days = []
  ;(hours || []).forEach((hour, at) => {
    const last = days[days.length - 1]
    if (last && last.day === hour.day) last.hours.push({ ...hour, at })
    else days.push({ day: hour.day, hours: [{ ...hour, at }] })
  })
  return days
}

// Which hour of the outline holds a sentence.
export function hourHolding(hours, index) {
  const list = hours || []
  for (let at = list.length - 1; at > 0; at -= 1) if (index >= list[at].first) return at
  return 0
}

// A sentence may carry a speaker number (told apart by voice; nobody's name is known).
export function speakerName(number) {
  return number ? `说话人${number}` : ''
}

// One of eight colours, used again beyond eight speakers; 0 means no speaker.
export function speakerTone(number) {
  return number ? ((Number(number) - 1) % 8) + 1 : 0
}

// Sentences said within the same minute by the same speaker read as one paragraph.
export function paragraphsOf(lines) {
  const paragraphs = []
  for (const line of lines || []) {
    const minute = line.time.slice(0, 5)
    const speaker = line.speaker ?? null
    const last = paragraphs[paragraphs.length - 1]
    if (last && last.day === line.day && last.minute === minute && last.speaker === speaker) last.lines.push(line)
    else paragraphs.push({ key: line.index, day: line.day, minute, speaker, lines: [line] })
  }
  return paragraphs
}

// Chinese sentences follow each other directly; after a Latin letter, a digit or a half-width mark a space is needed.
export function needsSpace(text) {
  return /[A-Za-z0-9.,!?;:'")\]]$/.test(text || '')
}

export function hourAsText(lines, mode) {
  const who = (speaker) => (speaker ? `${speakerName(speaker)}：` : '')
  if (mode === 'sentences') return (lines || []).map((line) => `[${line.time}] ${who(line.speaker)}${line.text}`).join('\n')
  return paragraphsOf(lines).map((paragraph) => `${paragraph.minute}  ${who(paragraph.speaker)}${paragraph.lines.map((line, at, all) => line.text + (at < all.length - 1 && needsSpace(line.text) ? ' ' : '')).join('')}`).join('\n\n')
}

export function searchTerms(query) {
  return String(query || '').split(/\s+/).filter(Boolean)
}

// Cuts text at every occurrence of any term, ignoring case: [{ text, hit }].
export function markParts(text, terms) {
  const whole = text || ''
  const wanted = (terms || []).filter(Boolean).map((term) => term.toLowerCase())
  const low = whole.toLowerCase()
  if (!wanted.length || low.length !== whole.length) return [{ text: whole, hit: false }]
  const parts = []
  let at = 0
  while (at < whole.length) {
    let next = -1
    let size = 0
    for (const term of wanted) {
      const found = low.indexOf(term, at)
      if (found !== -1 && (next === -1 || found < next || (found === next && term.length > size))) { next = found; size = term.length }
    }
    if (next === -1) break
    if (next > at) parts.push({ text: whole.slice(at, next), hit: false })
    parts.push({ text: whole.slice(next, next + size), hit: true })
    at = next + size
  }
  if (at < whole.length || !parts.length) parts.push({ text: whole.slice(at), hit: false })
  return parts
}
