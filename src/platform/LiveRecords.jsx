import { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowLeft, ChevronLeft, ChevronRight, Copy, Download, FileText, FolderOpen, LoaderCircle, Search, X } from 'lucide-react'
import { api, humanError } from './api.js'
import { countText, dayClockText, durationText, groupDays, hourAsText, hourHolding, hourText, markParts, needsSpace, paragraphsOf, recordTitle, searchTerms, speakerName, speakerTone } from './liveInput.js'
import './live.css'

const MODE = 'recording-platform:live-reader-mode'
const recordUrl = (name) => `/api/live/transcripts/${encodeURIComponent(name)}`
export const recordPage = (name) => `/live/records/${encodeURIComponent(name)}`

function loadMode() {
  try { return localStorage.getItem(MODE) === 'sentences' ? 'sentences' : 'paragraphs' } catch { return 'paragraphs' }
}

function Speaker({ number }) {
  return number ? <b className={`spk spk-${speakerTone(number)}`}>{speakerName(number)}</b> : null
}

function Marked({ text, terms }) {
  if (!terms.length) return text
  return markParts(text, terms).map((part, at) => (part.hit ? <mark key={at}>{part.text}</mark> : part.text))
}

// Reads one saved record: pick it on the left, move through it hour by hour, search it, jump to a sentence.
export function LiveRecords({ name, navigate }) {
  const [list, setList] = useState(null)
  const [outline, setOutline] = useState(null)
  const [hour, setHour] = useState(0)
  const [lines, setLines] = useState(null)
  const [mode, setMode] = useState(loadMode)
  const [query, setQuery] = useState('')
  const [found, setFound] = useState(null)
  const [searching, setSearching] = useState(false)
  const [view, setView] = useState('read')            // 'read' the hour, or the list of what was 'found'
  const [target, setTarget] = useState(null)          // a sentence to scroll to once its hour is on screen
  const [flash, setFlash] = useState(null)
  const [problem, setProblem] = useState('')
  const [copied, setCopied] = useState(false)
  const topRef = useRef(null)

  const sessions = list?.sessions || []
  const selected = name || sessions[0]?.name || ''
  const go = (href) => (event) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
    event.preventDefault()
    navigate(href)
  }

  useEffect(() => {
    let stale = false
    api('/api/live/transcripts').then((data) => { if (!stale) setList(data) }).catch(() => { if (!stale) setList({ sessions: [], directory: null }) })
    return () => { stale = true }
  }, [name])

  useEffect(() => {
    setOutline(null); setHour(0); setLines(null); setQuery(''); setFound(null); setView('read'); setTarget(null); setProblem('')
    if (!selected) return undefined
    let stale = false
    let timer = 0
    const load = async () => {
      try {
        const data = await api(`${recordUrl(selected)}/outline`)
        if (stale) return
        setOutline(data)
        if (data.active) timer = setTimeout(load, 10000)        // still being written: keep the totals and the last hour current
      } catch (error) { if (!stale) setProblem(humanError(error)) }
    }
    load()
    return () => { stale = true; clearTimeout(timer) }
  }, [selected])

  const bucket = outline?.hours?.[hour]
  const first = bucket?.first
  const size = bucket?.sentences
  useEffect(() => {
    if (!selected || first == null || !size) return undefined
    let stale = false
    ;(async () => {
      try {
        const rows = []
        while (rows.length < size) {
          const data = await api(`${recordUrl(selected)}/lines?first=${first + rows.length}&count=${Math.min(4000, size - rows.length)}`)
          if (stale) return
          if (!data.lines.length) break
          rows.push(...data.lines)
        }
        if (!stale) setLines(rows)
      } catch (error) { if (!stale) setProblem(humanError(error)) }
    })()
    return () => { stale = true }
  }, [selected, first, size])

  const terms = useMemo(() => searchTerms(query), [query])
  const wanted = terms.join(' ')
  useEffect(() => {
    if (!selected || !wanted) { setFound(null); setSearching(false); setView('read'); return undefined }
    let stale = false
    setSearching(true)
    const timer = setTimeout(async () => {
      try {
        const data = await api(`${recordUrl(selected)}/search?q=${encodeURIComponent(wanted)}&limit=300`)
        if (!stale) { setFound(data); setView('found') }
      } catch (error) { if (!stale) setProblem(humanError(error)) } finally { if (!stale) setSearching(false) }
    }, 300)
    return () => { stale = true; clearTimeout(timer) }
  }, [selected, wanted])

  useEffect(() => {
    if (view !== 'read' || target == null || !lines) return
    const node = document.querySelector(`[data-sentence="${target}"]`)
    if (!node) return
    node.scrollIntoView({ block: 'center' })
    setFlash(target)
    setTarget(null)
  }, [view, target, lines])

  useEffect(() => {
    if (flash == null) return undefined
    const timer = setTimeout(() => setFlash(null), 2600)
    return () => clearTimeout(timer)
  }, [flash])

  const openHour = (at, toTop) => {
    if (at !== hour) { setLines(null); setHour(at) }
    setView('read')
    if (toTop) topRef.current?.scrollIntoView({ block: 'start' })
  }
  const jump = (match) => {
    const at = hourHolding(outline.hours, match.index)
    if (at !== hour) { setLines(null); setHour(at) }
    setTarget(match.index)
    setView('read')
  }
  const pickMode = (value) => {
    setMode(value)
    try { localStorage.setItem(MODE, value) } catch { /* private browsing: the choice just is not remembered */ }
  }
  const copyHour = async () => {
    try { await navigator.clipboard.writeText(hourAsText(lines, mode)); setCopied(true); setTimeout(() => setCopied(false), 2000) } catch { setProblem('浏览器不允许写入剪贴板，请手动选中文字复制。') }
  }
  const reveal = async () => {
    setProblem('')
    try { await api(`${recordUrl(selected)}/reveal`, { method: 'POST' }) } catch (error) { setProblem(humanError(error)) }
  }

  const days = useMemo(() => groupDays(outline?.hours), [outline?.hours])
  const peak = useMemo(() => Math.max(1, ...(outline?.hours || []).map((item) => item.characters)), [outline?.hours])
  const paragraphs = useMemo(() => (mode === 'paragraphs' ? paragraphsOf(lines) : []), [lines, mode])
  const hours = outline?.hours || []
  const before = hours[hour - 1]
  const after = hours[hour + 1]
  const span = bucket ? `${bucket.day} ${hourText(bucket.hour)} 起的一小时` : ''

  return <div className="reader" ref={topRef}>
    <aside className="reader-list" aria-label="全部文字记录">
      <a className="text-link" href="/live" onClick={go('/live')}><ArrowLeft size={14} />返回实时输入</a>
      <h2>文字记录<span>{sessions.length ? `${sessions.length} 份` : ''}</span></h2>
      {sessions.length > 0 && <ul>{sessions.map((row) => {
        const current = row.name === selected && outline?.active ? outline : null
        return <li key={row.name}><a href={recordPage(row.name)} className={row.name === selected ? 'active' : ''} aria-current={row.name === selected ? 'page' : undefined} onClick={go(recordPage(row.name))}>
          <strong>{dayClockText(row.started)}</strong>
          <small>{row.active ? <em>正在记录</em> : durationText(row.seconds)} · {countText(current ? current.sentences : row.sentences)} 句</small>
        </a></li>
      })}</ul>}
      {list?.directory && <p className="reader-folder">每次开启存成一个文本文件，都在这个文件夹里：<code>{list.directory}</code></p>}
    </aside>

    <section className="reader-main">
      {!selected && <p className="reader-empty">{list ? '还没有文字记录。开启实时输入并说话后，这里会出现第一份。' : '正在读取…'}</p>}
      {selected && <>
        <header className="reader-head">
          <div><h1>{recordTitle(selected)} 的记录{outline?.active && <em>正在记录</em>}</h1>
            <p>{outline ? [outline.seconds == null ? '' : `时长 ${durationText(outline.seconds)}`, `${countText(outline.sentences)} 句`, `${countText(outline.characters)} 字`, outline.speakers ? `分出 ${outline.speakers} 位说话人` : '', outline.device, outline.model].filter(Boolean).join(' · ') : (problem ? '' : '正在读取…')}</p></div>
          <div className="button-row">
            <a className="p-button compact" href={recordUrl(selected)} download={selected}><Download size={13} />下载全文</a>
            <a className="p-button compact" href={recordUrl(selected)} target="_blank" rel="noreferrer"><FileText size={13} />原文件</a>
            <button type="button" className="p-button compact" onClick={reveal}><FolderOpen size={13} />在访达中显示</button>
          </div>
        </header>
        {problem && <div className="p-notice error" role="alert"><span>{problem}</span></div>}

        {outline && hours.length > 0 && <>
          <div className="reader-tools">
            <label className="reader-search"><Search size={14} />
              <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="在这份记录里搜索，多个词用空格隔开" aria-label="搜索这份记录" maxLength={100} />
              {searching && <LoaderCircle className="p-spin" size={13} />}
              {query && <button type="button" aria-label="清除搜索" onClick={() => setQuery('')}><X size={14} /></button>}
            </label>
            <div className="live-tabs" role="tablist" aria-label="显示方式">
              <button type="button" role="tab" aria-selected={mode === 'paragraphs'} className={mode === 'paragraphs' ? 'active' : ''} onClick={() => pickMode('paragraphs')}>按分钟成段</button>
              <button type="button" role="tab" aria-selected={mode === 'sentences'} className={mode === 'sentences' ? 'active' : ''} onClick={() => pickMode('sentences')}>逐句</button>
            </div>
          </div>

          <nav className="reader-hours" aria-label="按时间跳转">
            {days.map((day) => <div key={`${day.day}-${day.hours[0].at}`}><span>{day.day.slice(5)}</span><p>
              {day.hours.map((item) => <button key={item.at} type="button" className={item.at === hour && view === 'read' ? 'active' : ''} style={{ '--fill': (item.characters / peak).toFixed(2) }}
                title={`${hourText(item.hour)} 起的一小时 · ${countText(item.sentences)} 句 · ${countText(item.characters)} 字`} onClick={() => openHour(item.at)}>{hourText(item.hour)}</button>)}
            </p></div>)}
          </nav>

          {view === 'found' && found ? <div className="reader-found">
            <p className="reader-bar"><span>“{found.query}” 找到 <strong>{countText(found.total)}</strong> 处{found.total > found.matches.length ? `，下面是最早的 ${found.matches.length} 处。把词写得更具体可以缩小范围。` : found.total ? '。点一句跳到它在原文里的位置。' : '。'}</span><button type="button" className="p-button compact" onClick={() => setQuery('')}>清除搜索</button></p>
            {found.matches.length > 0
              ? <ul>{found.matches.map((match) => <li key={match.index}><button type="button" onClick={() => jump(match)}><time>{match.day.slice(5)} {match.time}</time><Speaker number={match.speaker} /><span><Marked text={match.text} terms={terms} /></span></button></li>)}</ul>
              : <p className="reader-empty">这份记录里没有同时包含这些词的句子。</p>}
          </div> : <>
            <p className="reader-bar"><span>{span} · {countText(bucket.sentences)} 句 · {countText(bucket.characters)} 字</span>
              {found && wanted && <button type="button" className="p-button compact" onClick={() => setView('found')}>回到 {countText(found.total)} 处搜索结果</button>}
              <button type="button" className="p-button compact" onClick={copyHour} disabled={!lines?.length}><Copy size={13} />{copied ? '已复制' : '复制这一小时'}</button></p>
            {!lines ? <p className="reader-empty"><LoaderCircle className="p-spin" size={14} /> 正在读取这一小时…</p>
              : mode === 'paragraphs'
                ? <div className="reader-text">{paragraphs.map((paragraph) => <p key={paragraph.key}><time>{paragraph.minute}</time><Speaker number={paragraph.speaker} /><span>{paragraph.lines.map((line, at) => <span key={line.index} data-sentence={line.index} className={flash === line.index ? 'flash' : undefined}><Marked text={line.text} terms={terms} />{at < paragraph.lines.length - 1 && needsSpace(line.text) ? ' ' : ''}</span>)}</span></p>)}</div>
                : <div className="reader-text sentences">{lines.map((line) => <p key={line.index} data-sentence={line.index} className={flash === line.index ? 'flash' : undefined}><time>{line.time}</time><Speaker number={line.speaker} /><span><Marked text={line.text} terms={terms} /></span></p>)}</div>}
            <div className="reader-turn">
              {before ? <button type="button" className="p-button" onClick={() => openHour(hour - 1, true)}><ChevronLeft size={15} />上一小时 {hourText(before.hour)}</button> : <span />}
              {after ? <button type="button" className="p-button" onClick={() => openHour(hour + 1, true)}>下一小时 {hourText(after.hour)}<ChevronRight size={15} /></button> : <span>{outline.active ? '这份记录还在写入，新的句子每 10 秒读取一次。' : '已经是最后一小时。'}</span>}
            </div>
          </>}
        </>}
        {outline && hours.length === 0 && <p className="reader-empty">这份记录里还没有句子。</p>}
      </>}
    </section>
  </div>
}
