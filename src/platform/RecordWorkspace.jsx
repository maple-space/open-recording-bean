import { useEffect, useRef, useState } from 'react'
import { Download, FileAudio, FileText, FolderOpen, Search, Sparkles, Trash2, Upload } from 'lucide-react'
import { deviceNames, formatBytes, formatDate, formatDuration, humanError, localModelName } from './api.js'
import { textFilename, usesLocalTranscription } from './workflow.js'
import { fetchRecordDownload } from './downloads.js'

export function RecordEditor({ record, settings, apiKey, busy, lockedField, online, onSave, onTranscribe, onSummarize, onProcess, onTrash, onRegisterFlush, onError }) {
  const [draft, setDraft] = useState(record)
  const [tagText, setTagText] = useState((record.tags || []).join(', '))
  const [tab, setTab] = useState('transcript')
  const [saveStatus, setSaveStatus] = useState('已保存到本机')
  const [exporting, setExporting] = useState('')
  const exportLock = useRef(false)
  const pending = useRef({})
  const inFlight = useRef({})
  const tagsFocused = useRef(false)
  const timer = useRef(null)
  const saving = useRef(null)
  const mounted = useRef(true)
  const saveCallback = useRef(onSave)
  saveCallback.current = onSave
  const localAsr = usesLocalTranscription(settings)
  const providerName = settings.provider === 'codex' ? 'Codex CLI' : settings.provider === 'claude' ? 'Claude -p' : 'API'

  useEffect(() => {
    setDraft((current) => ({ ...current, ...record, ...inFlight.current, ...pending.current }))
    if (!tagsFocused.current && !pending.current.tags && !inFlight.current.tags) setTagText((record.tags || []).join(', '))
  }, [record])
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; clearTimeout(timer.current); flush().catch(onError) }
  }, [])
  useEffect(() => {
    onRegisterFlush?.(record.id, flush)
    return () => onRegisterFlush?.(record.id, null)
  }, [record.id, onRegisterFlush])

  async function flush() {
    clearTimeout(timer.current)
    if (saving.current) await saving.current
    if (!Object.keys(pending.current).length) return
    const patch = pending.current
    pending.current = {}
    inFlight.current = patch
    if (mounted.current) setSaveStatus('正在保存…')
    const request = saveCallback.current(record.id, patch)
    saving.current = request
    try {
      await request
      if (mounted.current) setSaveStatus(Object.keys(pending.current).length ? '有待保存的更改' : '已保存到本机')
    } catch (error) {
      pending.current = { ...patch, ...pending.current }
      if (mounted.current) setSaveStatus('保存失败，请重试')
      throw error
    } finally { saving.current = null; inFlight.current = {} }
    if (Object.keys(pending.current).length) await flush()
  }

  function change(field, value) {
    setDraft((current) => ({ ...current, [field]: value }))
    pending.current = { ...pending.current, [field]: value }
    setSaveStatus('有待保存的更改')
    clearTimeout(timer.current)
    timer.current = setTimeout(() => flush().catch(onError), 850)
  }

  async function generate(kind) {
    try {
      await flush()
      if (kind === 'transcript') { await onTranscribe({ ...record, ...draft }); setTab('transcript') }
      else if (kind === 'process') { await onProcess({ ...record, ...draft }); setTab('summary') }
      else { await onSummarize({ ...record, ...draft }); setTab('summary') }
    } catch (error) { onError(error) }
  }

  async function exportText(field) {
    try {
      await flush()
      const link = document.createElement('a')
      const url = URL.createObjectURL(new Blob([draft[field] || ''], { type: 'text/plain;charset=utf-8' }))
      link.href = url; link.download = textFilename(draft.title, field === 'transcript' ? 'txt' : 'md')
      link.click()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (error) { onError(error) }
  }

  async function exportFile(kind) {
    if (exportLock.current) return
    exportLock.current = true
    setExporting(kind)
    try {
      await flush()
      const { blob, filename } = await fetchRecordDownload({ ...record, title: draft.title }, { kind })
      const url = URL.createObjectURL(blob)
      try {
        const link = document.createElement('a')
        link.href = url
        link.download = filename
        link.hidden = true
        document.body.appendChild(link)
        link.click()
        link.remove()
      } finally {
        // Keep the object alive while the browser opens its Save dialog.
        setTimeout(() => URL.revokeObjectURL(url), 60_000)
      }
    } catch (error) { onError(error) }
    finally {
      exportLock.current = false
      if (mounted.current) setExporting('')
    }
  }

  return <article className="record-editor">
    <div className="editor-heading"><span className="eyebrow">已归档录音</span><label className="title-field"><span className="sr-only">录音标题</span><input value={draft.title || ''} onChange={(event) => change('title', event.target.value)} placeholder="为录音命名" /></label><p>{deviceNames[record.device_type] || record.device_type} · {formatDate(record.recorded_at)} · {formatDuration(record.duration_ms)} · {formatBytes(record.size_bytes)}</p></div>
    <div className="archived-player"><audio controls controlsList="nodownload" preload="metadata" src={record.audio_url || `/api/records/${encodeURIComponent(record.id)}/audio`} onError={() => onError(new Error('无法播放这条音频。请确认本机服务在线，或下载原音频检查格式。'))} aria-label={`播放 ${record.title}`} /><button className="p-button compact" disabled={!!exporting || online === false} onClick={() => exportFile('audio')}><Download size={14} />{exporting === 'audio' ? '正在准备…' : '下载音频'}</button></div>
    <label className="tag-field">标签<input value={tagText} onFocus={() => { tagsFocused.current = true }} onBlur={() => { tagsFocused.current = false }} onChange={(event) => { setTagText(event.target.value); change('tags', event.target.value.split(/[,，]/).map((tag) => tag.trim()).filter(Boolean)) }} placeholder="用逗号分隔，例如：会议, 项目" /></label>
    <div className="agent-action"><div><strong>当前方式：{providerName}</strong><small>{settings.provider === 'api' ? '按设置转录，再生成总结。已有人工转录会直接使用。' : '先用本地 Whisper 识别，再交给 Agent 校订和总结；已有转录直接使用。'}</small></div><button className="p-button primary" disabled={!!busy || online === false || (settings.provider === 'api' && !apiKey.trim())} onClick={() => generate('process')}><Sparkles size={15} />转录并交给 Agent 整理</button></div>
    <div className="record-tabs" role="tablist" aria-label="录音文字"><button role="tab" aria-selected={tab === 'transcript'} onClick={() => setTab('transcript')} className={tab === 'transcript' ? 'active' : ''}><FileText size={16} />转录原文</button><button role="tab" aria-selected={tab === 'summary'} onClick={() => setTab('summary')} className={tab === 'summary' ? 'active' : ''}><Sparkles size={16} />智能整理</button></div>
    {tab === 'transcript' ? <div className="text-workspace" role="tabpanel" aria-label="转录原文">
      <div className="text-toolbar"><div><strong>语音转文字</strong><small>{localAsr ? `本地 ${localModelName(settings.asr_model)}${settings.provider === 'api' ? '' : ` → ${providerName} 校订`}` : '硅基流动 API · 音频将发送至服务商'}</small></div><div className="button-row"><button className="p-button compact" disabled={!draft.transcript} onClick={() => exportText('transcript')}><Download size={13} />TXT</button><button className="p-button primary" disabled={!!busy || online === false || (!localAsr && !apiKey.trim())} onClick={() => generate('transcript')}>{draft.transcript ? '重新转录' : '开始转录'}</button></div></div>
      {!localAsr && !apiKey.trim() && <p className="inline-help">请在“模型与设置”中输入本次使用的 API Key，或切换到本地语音转文字。</p>}
      {lockedField === 'transcript' && <p className="inline-help">正在生成转录，完成后可以继续编辑这段文字。</p>}
      <textarea aria-label="转录原文" readOnly={lockedField === 'transcript'} value={draft.transcript || ''} onChange={(event) => change('transcript', event.target.value)} placeholder="在这里生成转录，或直接粘贴已有文字。修改后自动保存，可用于 Codex / Claude 整理。" />
      {record.raw_transcript && record.raw_transcript !== draft.transcript && <details className="alternate-result"><summary>查看本地语音识别原稿</summary><pre>{record.raw_transcript}</pre><button className="p-button compact" disabled={lockedField === 'transcript'} onClick={() => change('transcript', record.raw_transcript)}>用原稿替换当前文字</button></details>}
      {record.generated_transcript && record.generated_transcript !== draft.transcript && <details className="alternate-result"><summary>查看另存的转录或校订结果</summary><pre>{record.generated_transcript}</pre><button className="p-button compact" disabled={lockedField === 'transcript'} onClick={() => change('transcript', record.generated_transcript)}>用生成结果替换当前文字</button></details>}
    </div> : <div className="text-workspace" role="tabpanel" aria-label="智能整理">
      <div className="text-toolbar"><div><strong>把录音整理成可用内容</strong><small>{settings.provider === 'api' ? '硅基流动 API' : settings.provider === 'codex' ? 'Codex CLI · 使用本机登录' : 'Claude -p · 使用本机登录'}</small></div><div className="button-row"><button className="p-button compact" disabled={!draft.summary} onClick={() => exportText('summary')}><Download size={13} />MD</button><button className="p-button primary" disabled={!!busy || online === false || !draft.transcript?.trim() || (settings.provider === 'api' && !apiKey.trim())} onClick={() => generate('summary')}><Sparkles size={14} />{draft.summary ? '重新整理' : '开始整理'}</button></div></div>
      <div className="template-controls"><label>整理模板<select value={draft.summary_template || 'meeting'} onChange={(event) => change('summary_template', event.target.value)}><option value="meeting">会议纪要</option><option value="memo">口述笔记</option><option value="interview">访谈整理</option><option value="custom">自定义</option></select></label>{draft.summary_template === 'custom' && <label className="custom-prompt-field">整理要求<input value={draft.custom_prompt || ''} onChange={(event) => change('custom_prompt', event.target.value)} placeholder="例如：提炼客户需求和待确认问题" /></label>}</div>
      {!draft.transcript?.trim() && <p className="inline-help">请先生成或粘贴转录原文，再整理文字。</p>}
      {settings.provider === 'api' && !apiKey.trim() && <p className="inline-help">请先在设置中输入 API Key。Codex 和 Claude 模式使用各自本机登录。</p>}
      {lockedField === 'summary' && <p className="inline-help">正在生成整理结果，完成后可以继续编辑。</p>}
      <textarea aria-label="整理结果" readOnly={lockedField === 'summary'} value={draft.summary || ''} onChange={(event) => change('summary', event.target.value)} placeholder="整理结果会显示在这里。可继续编辑并保存。" />
      {record.generated_summary && record.generated_summary !== draft.summary && <details className="alternate-result"><summary>查看另存的模型整理结果</summary><pre>{record.generated_summary}</pre><button className="p-button compact" disabled={lockedField === 'summary'} onClick={() => change('summary', record.generated_summary)}>用生成结果替换当前文字</button></details>}
    </div>}
    <div className="editor-save-row"><span role="status">{saveStatus}</span><div className="button-row"><button className="p-button compact" onClick={() => flush().catch(onError)}>保存文字</button><button className="p-button compact" disabled={!!exporting || online === false} onClick={() => exportFile('bundle')}><Download size={14} />{exporting === 'bundle' ? '正在准备…' : '导出资料包'}</button><button className="p-button compact danger" disabled={!!busy || online === false} onClick={() => onTrash([record.id])}><Trash2 size={14} />本机资料移到废纸篓</button></div></div>
  </article>
}

export function RecordWorkspace({ records, selectedId, onSelect, settings, apiKey, busy, lockedField, online, onImport, onSave, onTranscribe, onSummarize, onProcess, onExport, onTrash, onRegisterFlush, onError }) {
  const [query, setQuery] = useState('')
  const [source, setSource] = useState('all')
  const [checked, setChecked] = useState(new Set())
  const fileInput = useRef(null)
  const visible = records.filter((record) => (source === 'all' || record.device_type === source) && [record.title, record.transcript, ...(record.tags || [])].join(' ').toLowerCase().includes(query.toLowerCase()))
  const selected = records.find((record) => record.id === selectedId)
  const checkedIds = records.filter((record) => checked.has(record.id)).map((record) => record.id)
  function toggle(id) { setChecked((current) => { const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next }) }
  return <>
    <div className="page-heading"><div><span className="eyebrow">你的录音资产</span><h1>本机资料库</h1><p>音频、转录和整理结果，集中在一起。设备断开后仍可使用。</p></div><button className="p-button" disabled={!!busy || !online} onClick={() => fileInput.current?.click()}><Upload size={15} />导入本地音频</button><input ref={fileInput} type="file" accept="audio/*,.wav,.mp3,.m4a,.ogg,.opus,.flac,.aac" hidden onChange={async (event) => { const file = event.target.files?.[0]; event.target.value = ''; if (file) await onImport(file) }} /></div>
    <div className="library-layout"><section className="library-browser" aria-label="归档录音列表">
      <div className="library-filters"><label className="p-search"><Search size={16} /><input aria-label="搜索资料库" placeholder="搜索标题、标签或转录" value={query} onChange={(event) => setQuery(event.target.value)} /></label><select aria-label="按来源筛选" value={source} onChange={(event) => setSource(event.target.value)}><option value="all">全部来源 · {records.length}</option>{Object.entries(deviceNames).map(([key, name]) => <option key={key} value={key}>{name}</option>)}</select></div>
      <div className="library-bulk"><label className="checkbox-label"><input type="checkbox" aria-label="选择全部可见归档" checked={visible.length > 0 && visible.every((record) => checked.has(record.id))} onChange={(event) => setChecked(event.target.checked ? new Set(visible.map((record) => record.id)) : new Set())} />{checkedIds.length ? `已选 ${checkedIds.length} 条` : '全选'}</label><div className="button-row"><button className="p-button compact" disabled={!!busy || !online || !checkedIds.length} onClick={() => onExport(checkedIds)}><Download size={13} />导出所选资料包</button><button className="p-button compact danger" disabled={!!busy || !online || !checkedIds.length} onClick={() => onTrash(checkedIds)}><Trash2 size={13} />移到废纸篓</button></div></div>
      {visible.length > 0 ? <div className="library-records">{visible.map((record) => <div key={record.id} className={`library-record-row ${selectedId === record.id ? 'active' : ''}`}><input type="checkbox" aria-label={`选择归档 ${record.title}`} checked={checked.has(record.id)} onChange={() => toggle(record.id)} /><button className="library-record" onClick={() => onSelect(record.id)}><FileAudio size={21} /><div><strong>{record.title || '未命名录音'}</strong><small>{deviceNames[record.device_type] || record.device_type} · {formatDate(record.recorded_at)}</small><span>{record.summary ? '已整理' : record.transcript ? '已转录' : '音频已归档'}{record.tags?.length ? ` · ${record.tags.join(' / ')}` : ''}</span></div></button></div>)}</div> : <div className="p-empty"><FolderOpen size={30} /><h3>{records.length ? '没有匹配的内容' : '从第一段录音开始'}</h3><p>{records.length ? '试试其他关键词或来源。' : '前往设备工作台下载录音，或导入电脑上的音频。'}</p></div>}
      <p className="library-footnote">本地导入仅加入资料库，不会上传到硬件。</p>
    </section>
    {selected ? <RecordEditor key={selected.id} record={selected} settings={settings} apiKey={apiKey} busy={busy} lockedField={lockedField} online={online} onSave={onSave} onTranscribe={onTranscribe} onSummarize={onSummarize} onProcess={onProcess} onTrash={onTrash} onRegisterFlush={onRegisterFlush} onError={(error) => onError(humanError(error))} /> : <div className="p-empty editor-empty"><FileText size={36} /><h3>给每段录音一个下一步</h3><p>选择左侧录音，即可播放、转录、整理与导出。</p></div>}
    </div>
  </>
}
