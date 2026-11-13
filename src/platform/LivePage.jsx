import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ArrowRight, BatteryMedium, CheckCircle2, Copy, Download, Eraser, FileText, HardDrive, LoaderCircle, Mic, RefreshCw, Square, Timer, TriangleAlert } from 'lucide-react'
import { api, humanError } from './api.js'
import { recordPage } from './LiveRecords.jsx'
import { PAGE_ROWS, applyTranscript, clockText, countText, dayClockText, durationText, elapsedText, levelPercent, pickOutput, speakerName, speakerTone, statusView, storageText } from './liveInput.js'
import './live.css'

const PREFS = 'recording-platform:live-input'
const HISTORY_ROWS = 30
const recordUrl = (name) => `/api/live/transcripts/${encodeURIComponent(name)}`
const languages = [['zh', '中文'], ['en', '英文'], ['auto', '自动判断']]

function loadPrefs() {
  try { return JSON.parse(localStorage.getItem(PREFS)) || {} } catch { return {} }
}

function savePrefs(value) {
  try { localStorage.setItem(PREFS, JSON.stringify(value)) } catch { /* private browsing: the choice just is not remembered */ }
}

// A drawn stand-in for a settings window: the real labels, with the row to click highlighted.
function Screen({ title, sidebar = [], active, children }) {
  return <figure className="live-screen" aria-label={`${title}示意图`}>
    <div className="live-screen-bar"><i /><i /><i /><span>{title}</span></div>
    <div className="live-screen-body">
      {sidebar.length > 0 && <ul>{sidebar.map((item) => <li key={item} className={item === active ? 'active' : ''}>{item}</li>)}</ul>}
      <div>{children}</div>
    </div>
  </figure>
}

function Row({ label, value, hint, pick }) {
  return <div className={`live-screen-row ${pick ? 'pick' : ''}`}><span>{label}{hint && <small>{hint}</small>}</span><b>{value}</b></div>
}

function Guide({ card, hasCard, outputsLoaded, onRecheck, checking }) {
  const [app, setApp] = useState('doubao')
  const [copied, setCopied] = useState(false)
  const name = card || 'BlackHole 2ch'
  const shortName = name.replace(/\s*\(2ch\)$/, '')      // Typeless lists "Soundflower (2ch)" as "Soundflower"
  const copyCommand = async () => {
    try { await navigator.clipboard.writeText('brew install blackhole-2ch'); setCopied(true); setTimeout(() => setCopied(false), 2000) } catch { /* select and copy by hand */ }
  }
  return <section className="live-guide" id="live-guide">
    <div className="panel-heading"><div><h2>设置教程：让输入法听见录音豆</h2><p>一共三步。第 1 步每台电脑只做一次，第 3 步每个输入法只做一次。</p></div></div>
    <div className="live-flow" aria-label="声音经过的路径">
      <span>录音豆</span><em>蓝牙</em><span>这台电脑的本机服务</span><em>声音</em><span className="accent">虚拟声卡</span><em>当作麦克风</em><span>豆包输入法 / Typeless / 会议软件</span>
    </div>

    <article className="live-step">
      <header><b>1</b><div><h3>装一块“虚拟声卡”</h3><p>它是一个不出声的声音设备：一头接收本机服务送来的声音，另一头被输入法当成麦克风。输入法只能从声音设备里选麦克风，所以需要它来转接。</p></div></header>
      {hasCard
        ? <p className="live-done"><CheckCircle2 size={16} />这台电脑已经装好：<strong>{card}</strong>。直接做第 2 步。</p>
        : <>
          <ol>
            <li>打开“终端”（按 <kbd>⌘</kbd> + <kbd>空格</kbd>，输入“终端”回车），粘贴下面这行并回车，按提示输入开机密码：
              <div className="live-command"><code>brew install blackhole-2ch</code><button type="button" className="p-button compact" onClick={copyCommand}><Copy size={13} />{copied ? '已复制' : '复制'}</button></div>
            </li>
            <li>如果提示找不到 brew：到 <strong>existential.audio/blackhole</strong> 下载 “BlackHole 2ch” 安装包，双击按提示安装。它是免费的开源软件。</li>
            <li>安装程序要求重启就重启一次，然后回到本页点“重新检测”。</li>
          </ol>
          <button type="button" className="p-button" onClick={onRecheck} disabled={checking}>{checking ? <LoaderCircle className="p-spin" size={14} /> : <RefreshCw size={14} />}重新检测</button>
          {outputsLoaded && <span className="live-step-note">还没有检测到虚拟声卡。</span>}
        </>}
    </article>

    <article className="live-step">
      <header><b>2</b><div><h3>在本页开启实时输入</h3><p>在上面勾选“送到虚拟声卡”，设备选 <strong>{name}</strong>，点“开启实时输入”。状态变成“正在传声”后对着录音豆说话，音量条会跟着跳动。</p></div></header>
      <Screen title="实时输入">
        <Row label="送到虚拟声卡，给输入法当麦克风" value={name} pick />
        <Row label="状态" value="● 正在传声" />
        <div className="live-screen-meter"><i style={{ width: '62%' }} /></div>
      </Screen>
    </article>

    <article className="live-step">
      <header><b>3</b><div><h3>把输入法的麦克风改成虚拟声卡</h3><p>你用哪个就设置哪个。设置好以后，把光标放进任意输入框，按输入法的语音快捷键，对着录音豆说话即可。</p></div></header>
      <div className="live-tabs" role="tablist">
        <button type="button" role="tab" aria-selected={app === 'doubao'} className={app === 'doubao' ? 'active' : ''} onClick={() => setApp('doubao')}>豆包输入法</button>
        <button type="button" role="tab" aria-selected={app === 'typeless'} className={app === 'typeless' ? 'active' : ''} onClick={() => setApp('typeless')}>Typeless</button>
      </div>
      {app === 'doubao' ? <div className="live-pair">
        <ol>
          <li>点屏幕右上角菜单栏里的输入法图标，打开豆包输入法的设置。窗口左上角写着<strong>“豆包输入法”</strong>。</li>
          <li>左边选<strong>“语音输入”</strong>。</li>
          <li>点<strong>“麦克风选择”</strong>右边的按钮，在弹出的“选择麦克风”里点 <strong>{name}</strong>，不要选“自动检测”。</li>
          <li>把光标放进任意输入框：按住 <kbd>fn</kbd> 说话，松手结束；或者按一下右边的 <kbd>⌥ Option</kbd> 开始说话。</li>
        </ol>
        <Screen title="豆包输入法" sidebar={['语音输入', '键盘输入', '常用语和剪贴板', '超级互传', '外观']} active="语音输入">
          <Row label="语音输入模式" hint="长按模式：按住说话，松手结束" value="fn" />
          <Row label="麦克风选择" hint="如果说话后指示条没有反应，请尝试其他麦克风。" value={`${name}`} pick />
          <Row label="智能文字整理" value="启用" />
        </Screen>
      </div> : <div className="live-pair">
        <ol>
          <li>打开 Typeless 主窗口，点左下角的<strong>齿轮图标</strong>。也可以点屏幕顶部菜单栏的 “Typeless” → <strong>“设置…”</strong>。</li>
          <li>在“设置”页面往下滚动到<strong>“音频”</strong>一节，点<strong>“麦克风”</strong>右边的方框。</li>
          <li>在弹出的“麦克风”窗口里点 <strong>{shortName}</strong>，不要选“自动检测”。它右边的小竖条跟着声音跳动就对了，点右上角的 × 关闭。</li>
          <li>之后如果 Typeless 提示“检测到更好的麦克风”，点<strong>“取消”</strong>，否则它会换回电脑自带的麦克风。</li>
          <li>把光标放进任意输入框，按“语音输入”快捷键（默认是左边的 Option 键）说话。</li>
        </ol>
        <Screen title="Typeless · 设置" sidebar={['账户', '设置', '个性化', '关于', '帮助']} active="设置">
          <div className="live-screen-section">音频</div>
          <Row label="麦克风" hint="选择您首选的麦克风，以便Typeless捕捉您的声音。" value={`${shortName} ›`} pick />
          <Row label="交互声音" value="●" />
          <Row label="语音输入时静音" value="●" />
        </Screen>
      </div>}
    </article>

    <article className="live-step">
      <header><b>✓</b><div><h3>用完以后</h3><p>回到本页点“关闭实时输入”，录音豆会暂停录音。再把输入法的麦克风改回原来的选项（豆包输入法和 Typeless 里都叫“自动检测”）。不改回去的话，实时输入关闭时输入法听不到任何声音。</p></div></header>
    </article>

    <div className="live-facts">
      <h3>需要知道的几件事</h3>
      <ul>
        <li>开启期间录音豆一直在录音，录音同时存在录音豆里，每小时约 29 MB。用完可以到“录音豆”页面下载或删除。</li>
        <li>开启期间这台电脑独占录音豆的蓝牙：手机上的飞书或 soundcore App 连不上它；本页面的刷新、下载、删除要先关闭实时输入。</li>
        <li>录音豆放在充电盒里可以一直开着，充电盒要接着电源。实测连续 8 小时没有中断，没有丢失声音。</li>
        <li>本页的文字由这台电脑上的模型识别，不联网。输入法自己怎么识别，由输入法决定。</li>
        <li>每次开启的文字单独存成一个文本文件，句数没有上限。页面上只显示本次最近 {countText(PAGE_ROWS)} 句，更早的到“文字记录”里打开。</li>
      </ul>
      <h3>没有反应时这样查</h3>
      <ul>
        <li><strong>本页音量条不动</strong>：声音没到电脑。看状态一行的提示，确认录音豆在电脑旁边、指示灯亮着。</li>
        <li><strong>音量条在动，输入法没有字</strong>：输入法的麦克风没有选成 {name}，或者上面“送到虚拟声卡”选错了设备。</li>
        <li><strong>文字一顿一顿</strong>：蓝牙信号弱，把录音豆放近一些。</li>
      </ul>
    </div>
  </section>
}

export function LivePage({ online, navigate }) {
  const [live, setLive] = useState(null)
  const [outputs, setOutputs] = useState(null)
  const [engine, setEngine] = useState(null)
  const [transcript, setTranscript] = useState({ segments: [], cursor: 0 })
  const [prefs, setPrefs] = useState(loadPrefs)
  const [pending, setPending] = useState('')
  const [checking, setChecking] = useState(false)
  const [problem, setProblem] = useState('')
  const [copied, setCopied] = useState(false)
  const [history, setHistory] = useState(null)
  const transcriptRef = useRef(transcript)
  const listRef = useRef(null)
  const pinned = useRef(true)

  const remember = useCallback((patch) => setPrefs((current) => { const next = { ...current, ...patch }; savePrefs(next); return next }), [])

  const loadOutputs = useCallback(async () => {
    setChecking(true)
    try { setOutputs(await api('/api/live/outputs')) } catch { setOutputs({ available: false, devices: [], suggested: null, message: '读取声音设备失败，请确认本机服务正在运行。' }) } finally { setChecking(false) }
  }, [])

  useEffect(() => {
    loadOutputs()
    api('/api/realtime/status').then(setEngine).catch(() => setEngine(null))
  }, [loadOutputs])

  useEffect(() => {
    let stopped = false
    let timer = 0
    let quick = false
    const tick = async () => {
      if (!document.hidden) {
        try {
          const data = await api(`/api/live?cursor=${transcriptRef.current.cursor}`)
          if (stopped) return
          setLive(data)
          quick = Boolean(data.enabled)
          const next = applyTranscript(transcriptRef.current, data.transcript)
          if (next !== transcriptRef.current) { transcriptRef.current = next; setTranscript(next) }
        } catch { /* the page header already reports a lost service */ }
      }
      // Words should appear as they are recognised: ask often while it is on, rarely while it is off.
      if (!stopped) timer = setTimeout(tick, quick ? 300 : 1000)
    }
    tick()
    return () => { stopped = true; clearTimeout(timer) }
  }, [])

  useEffect(() => {
    const list = listRef.current
    if (list && pinned.current) list.scrollTop = list.scrollHeight
  }, [transcript, live?.transcript?.partial])

  // The list of saved records changes when a session starts, gets its file, or ends.
  const sessionFile = live?.transcript?.session?.file
  const sessionOpen = live?.transcript?.session?.open
  useEffect(() => {
    let stale = false
    api('/api/live/transcripts').then((data) => { if (!stale) setHistory(data) }).catch(() => { /* the list is a convenience; the text itself is unaffected */ })
    return () => { stale = true }
  }, [sessionFile, sessionOpen])

  const devices = outputs?.devices || []
  const card = outputs?.suggested || ''
  const output = useMemo(() => pickOutput(outputs, prefs.output), [outputs, prefs.output])
  const chosen = devices.find((device) => device.name === output)
  const toCard = prefs.toCard ?? true
  const toText = prefs.toText ?? true
  const canTellSpeakers = Boolean(engine?.speakers?.available)     // the speaker model is in place on this computer
  const toSpeakers = prefs.toSpeakers ?? true
  const language = prefs.language || 'zh'
  const enabled = Boolean(live?.enabled)
  const view = statusView(live)
  const connected = Boolean(live?.device?.connected)
  const reconnecting = live?.device?.connection_status === 'reconnecting'    // e.g. the few seconds after switching off
  const engineLabel = engine?.engines?.find((row) => row.id === engine.default_model)?.label
  const canStart = online !== false && live?.available !== false && (connected || reconnecting) && (toText || (toCard && output)) && !pending

  const act = async (kind, request) => {
    setPending(kind)
    setProblem('')
    try { setLive(await request()) } catch (error) { setProblem(humanError(error)) } finally { setPending('') }
  }
  const start = () => act('start', () => api('/api/live/start', { method: 'POST', body: { output: toCard && output ? output : null, transcribe: toText, language, ...(canTellSpeakers ? { speakers: toText && toSpeakers } : {}) } }))
  const stop = () => act('stop', () => api('/api/live/stop', { method: 'POST' }))
  const setText = (on) => act('text', () => api('/api/live/transcribe', { method: 'POST', body: { on, language } }))
  const clear = async () => {
    try {
      const data = await api('/api/live/transcript/clear', { method: 'POST' })
      transcriptRef.current = { segments: [], cursor: data.transcript.next_cursor, session: data.transcript.session?.started ?? null }
      setTranscript(transcriptRef.current)
    } catch (error) { setProblem(humanError(error)) }
  }
  const copyAll = async () => {
    try { await navigator.clipboard.writeText(transcript.segments.map((row) => row.text).join('\n')); setCopied(true); setTimeout(() => setCopied(false), 2000) } catch { setProblem('浏览器不允许写入剪贴板，请手动选中文字复制。') }
  }

  const running = live?.options || {}
  const elapsed = enabled && live?.since ? Date.now() / 1000 - live.since : 0
  const textOn = enabled ? Boolean(running.transcribe) : false
  const session = live?.transcript?.session
  const earlier = Math.max(0, (session?.sentences || 0) - transcript.segments.length)     // cleared from the page, or beyond what it shows
  const records = history?.sessions || []
  const previous = records.find((row) => !row.active && row.name !== session?.file)      // the record before this one, newest first
  const lines = useMemo(() => transcript.segments.map((row) => <p key={row.index}><time>{clockText(row.at)}</time>{row.speaker ? <b className={`spk spk-${speakerTone(row.speaker)}`}>{speakerName(row.speaker)}</b> : null}<span>{row.text}</span></p>), [transcript.segments])
  const speakers = live?.transcript?.speakers
  const read = (name) => (event) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
    event.preventDefault()
    navigate(recordPage(name))
  }

  return <>
    <div className="page-heading"><div><span className="eyebrow">录音豆</span><h1>实时输入</h1><p>把录音豆当成这台电脑的麦克风：说话的声音实时送进输入法，同时在这里变成文字。</p></div><Mic size={46} strokeWidth={1.2} /></div>

    <section className={`live-card ${view.tone}`}>
      <div className="live-state" role="status">
        <i />
        <div><strong>{view.text}</strong>
          {enabled && <div className="live-meter" aria-label="音量"><i style={{ width: `${levelPercent(live.audio?.level)}%` }} /></div>}
        </div>
        {enabled
          ? <button type="button" className="p-button danger" onClick={stop} disabled={!!pending}>{pending === 'stop' ? <LoaderCircle className="p-spin" size={15} /> : <Square size={14} />}关闭实时输入</button>
          : <button type="button" className="p-button primary" onClick={start} disabled={!canStart}>{pending === 'start' ? <LoaderCircle className="p-spin" size={15} /> : <Mic size={15} />}{pending === 'start' ? '正在开启…' : '开启实时输入'}</button>}
      </div>

      {problem && <div className="p-notice error" role="alert"><span>{problem}</span></div>}
      {!enabled && live?.notice && <div className="p-notice"><span>{live.notice}</span></div>}
      {live && live.available === false && <div className="p-notice error"><span>{live.unavailable_reason || '这个服务没有启用录音豆的蓝牙连接。'}</span></div>}
      {live && live.available !== false && !connected && !reconnecting && !enabled && <div className="p-notice"><span>录音豆还没有连接。先到“录音豆”页面连接设备，再回来开启。</span><button type="button" className="p-button compact" onClick={() => navigate('/device')}>去连接<ArrowRight size={13} /></button></div>}
      {live && !connected && reconnecting && !enabled && <p className="hint">录音豆正在保存刚才的录音并重新连接，十几秒后恢复。现在也可以直接再次开启。</p>}

      {enabled ? <dl className="live-facts-row">
        <div><dt><Timer size={13} />已开启</dt><dd>{elapsedText(elapsed)}</dd></div>
        <div><dt><BatteryMedium size={13} />录音豆电量</dt><dd>{live.device?.battery == null ? '未知' : `${live.device.battery}%`}{live.device?.charging ? ' · 充电中' : ''}</dd></div>
        <div><dt><HardDrive size={13} />录音豆剩余空间</dt><dd>{storageText(live.device?.free_kb)}</dd></div>
        <div><dt>送到</dt><dd>{[running.output, running.transcribe ? '本页文字' : ''].filter(Boolean).join(' + ')}</dd></div>
        {speakers?.on && <div><dt>说话人</dt><dd>{speakers.count ? `已分出 ${speakers.count} 位` : '还没听到完整的一句'}</dd></div>}
        {(live.audio?.lost_frames > 0 || live.audio?.retries > 0) && <div><dt>中断</dt><dd>重连 {live.audio.retries} 次 · 丢失 {(live.audio.lost_frames * 0.02).toFixed(1)} 秒</dd></div>}
      </dl> : <div className="live-options">
        <label className="live-option"><input type="checkbox" checked={toCard} onChange={(event) => remember({ toCard: event.target.checked })} />
          <span><strong>送到虚拟声卡，给输入法当麦克风</strong>
            {devices.length > 0
              ? <select value={output} disabled={!toCard} onChange={(event) => remember({ output: event.target.value })} aria-label="输出设备">
                {!output && <option value="">请选择设备</option>}
                {devices.map((device) => <option key={device.name} value={device.name}>{device.name}{device.virtual ? '（虚拟声卡）' : ''}</option>)}
              </select>
              : <small>{outputs ? outputs.message : '正在读取声音设备…'}</small>}
          </span>
        </label>
        {toCard && outputs && !card && <p className="inline-help"><TriangleAlert size={13} />没有找到虚拟声卡。先按下面教程的第 1 步安装，装好后点 <button type="button" className="text-link" onClick={loadOutputs}>重新检测</button>。</p>}
        {toCard && chosen && !chosen.virtual && <p className="inline-help"><TriangleAlert size={13} />“{chosen.name}”不是虚拟声卡，声音会直接从它播放出来，输入法收不到。</p>}
        <label className="live-option"><input type="checkbox" checked={toText} onChange={(event) => remember({ toText: event.target.checked })} />
          <span><strong>在本页显示文字</strong>
            <small>{engine && !engine.ready ? engine.message : `本机模型${engineLabel ? ` ${engineLabel}` : ''}识别，不联网。`}
              <select value={language} disabled={!toText} onChange={(event) => remember({ language: event.target.value })} aria-label="识别语言">{languages.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select>
            </small>
          </span>
        </label>
        {canTellSpeakers && <label className="live-option sub"><input type="checkbox" checked={toText && toSpeakers} disabled={!toText} onChange={(event) => remember({ toSpeakers: event.target.checked })} />
          <span><strong>区分说话人</strong>
            <small>每句前面标“说话人1、说话人2…”，同时写进文字记录。够长的句子靠嗓音判断是谁；一两个字的短词和碎句声音太短，直接算作前一个说话的人。只知道是不是同一个人，不知道姓名。</small>
          </span>
        </label>}
        <p className="hint">开启后录音豆会开始录音并持续传声，直到你在这里关闭。期间在录音豆上按一下可以暂停，再按一下继续。</p>
      </div>}
      {enabled && live.output?.error && <div className="p-notice error"><span>{live.output.error}</span></div>}
      {live?.transcript?.error && <div className="p-notice error"><span>{live.transcript.error}</span></div>}
    </section>

    <section className="live-text">
      <div className="panel-heading"><div><h2>实时文字<span>{session?.sentences ? `本次 ${countText(session.sentences)} 句 · ${countText(session.characters)} 字` : ''}</span></h2><p>录音豆听到的话会在这里变成文字，说话时先出灰色草稿，停顿后定稿。每一句定稿后立刻写进本次的文本文件，句数没有上限。这里只显示本次开启以来的内容，再次开启或本机服务重启后从空开始，之前的都在下面的“文字记录”里。</p></div>
        <div className="button-row">{enabled && textOn && <button type="button" className="p-button compact" onClick={() => setText(false)} disabled={!!pending}>关闭文字</button>}<button type="button" className="p-button compact" onClick={copyAll} disabled={!transcript.segments.length}><Copy size={13} />{copied ? '已复制' : '复制页面文字'}</button><button type="button" className="p-button compact" onClick={clear} disabled={!transcript.segments.length} title="只清空页面，文件里的记录保留"><Eraser size={13} />清空页面</button></div>
      </div>
      {session?.file && <p className="live-record"><FileText size={14} /><span>{session.open ? '本次的完整记录正在写入' : '本次的完整记录'} <strong>{session.file}</strong></span><a className="p-button compact" href={recordPage(session.file)} onClick={read(session.file)}>查看</a><a className="p-button compact" href={recordUrl(session.file)} download={session.file}><Download size={13} />下载</a></p>}
      {session?.error && <div className="p-notice error"><span>{session.error}</span></div>}
      {enabled && !textOn && <div className="p-notice"><span>现在只把声音送到虚拟声卡，本页文字没有开启。</span><button type="button" className="p-button compact primary" onClick={() => setText(true)} disabled={!!pending}>{pending === 'text' ? <LoaderCircle className="p-spin" size={13} /> : null}开启文字</button></div>}
      <div className="live-lines" ref={listRef} onScroll={(event) => { const el = event.currentTarget; pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40 }}>
        {previous && <p className="empty">上一次的内容没有丢：{dayClockText(previous.started)} 开始，{countText(previous.sentences)} 句，在文字记录里。<a className="text-link" href={recordPage(previous.name)} onClick={read(previous.name)}>查看</a></p>}
        {earlier > 0 && session?.file && <p className="empty">更早的 {countText(earlier)} 句不在页面上，都在完整记录里。<a className="text-link" href={recordPage(session.file)} onClick={read(session.file)}>查看完整记录</a></p>}
        {lines}
        {live?.transcript?.partial && <p className="partial"><time>…</time><span>{live.transcript.partial}</span></p>}
        {!transcript.segments.length && !live?.transcript?.partial && <p className="empty">{enabled ? (textOn ? '正在听。对着录音豆说一句话试试。' : '点上面的“开启文字”，录音豆听到的话就会显示在这里。') : '开启实时输入后，录音豆听到的话会显示在这里。'}</p>}
      </div>
    </section>

    <section className="live-history">
      <div className="panel-heading"><div><h2>文字记录<span>{records.length ? `${records.length} 份` : ''}</span></h2><p>每次开启实时输入，文字单独存成一个文本文件，不会互相覆盖，句数没有上限，重启服务也还在。点“查看”可以按时间翻看和搜索。{history?.directory && <>文件夹：<code>{history.directory}</code></>}</p></div></div>
      {records.length > 0
        ? <ul>{records.slice(0, HISTORY_ROWS).map((row) => <li key={row.name} className={row.active ? 'active' : ''}>
          <span className="when"><strong>{dayClockText(row.started)}</strong>{row.active ? <em>正在记录</em> : <small>{durationText(row.seconds)}</small>}</span>
          <span className="size">{countText(row.active ? session?.sentences ?? row.sentences : row.sentences)} 句 · {countText(row.active ? session?.characters ?? row.characters : row.characters)} 字</span>
          <span className="button-row"><a className="p-button compact primary" href={recordPage(row.name)} onClick={read(row.name)}>查看</a><a className="p-button compact" href={recordUrl(row.name)} download={row.name}><Download size={13} />下载</a></span>
        </li>)}</ul>
        : <p className="empty">{history ? '还没有记录。开启实时输入并说话后，这里会出现第一份。' : '正在读取…'}</p>}
      {records.length > HISTORY_ROWS && <p className="hint">这里列出最近 {HISTORY_ROWS} 份，更早的 {records.length - HISTORY_ROWS} 份在上面的文件夹里。</p>}
    </section>

    <Guide card={card} hasCard={Boolean(card)} outputsLoaded={Boolean(outputs)} onRecheck={loadOutputs} checking={checking} />
  </>
}
