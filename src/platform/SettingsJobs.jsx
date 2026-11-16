import { useEffect, useState } from 'react'
import { AlertCircle, CheckCircle2, Circle, Clock3, KeyRound, LoaderCircle, Settings2, X } from 'lucide-react'
import { deviceNames, formatDate, humanError, jobNames, localModelChoices, localModelKey, localModelName, terminalStatuses } from './api.js'
import { settingsForProvider } from './workflow.js'

export function JobPanel({ jobs, onCancel, busy }) {
  const [expanded, setExpanded] = useState(false)
  const active = jobs.filter((job) => !terminalStatuses.has(job.status))
  const shown = expanded ? jobs : jobs.slice(0, 3)
  if (!jobs.length) return null
  const statuses = { queued: '排队中', running: '处理中', completed: '已完成', failed: '失败', cancelled: '已取消' }
  return <section className="jobs-panel" aria-label="后台任务"><div className="jobs-heading"><h2><Clock3 size={16} />后台任务 {active.length > 0 && <span>{active.length} 进行中</span>}</h2><button onClick={() => setExpanded(!expanded)}>{expanded ? '收起' : `查看最近 ${jobs.length} 项`}</button></div>
    <div className="jobs-list">{shown.map((job) => <div key={job.id} className={`job-row ${job.status}`}>
      {job.status === 'running' || job.status === 'queued' ? <LoaderCircle className="p-spin" size={17} /> : job.status === 'completed' ? <CheckCircle2 size={17} /> : job.status === 'failed' ? <AlertCircle size={17} /> : <Circle size={17} />}
      <div className="job-copy"><strong>{jobNames[job.type] || job.type}{job.device_type && ` · ${deviceNames[job.device_type] || job.device_type}`}</strong><span>{humanError(job.error || job.message || statuses[job.status])}</span></div>
      <small>{formatDate(job.created_at)}</small><span className="job-status">{statuses[job.status] || job.status}</span>
      {!terminalStatuses.has(job.status) && <button className="p-icon-button" title="取消任务" aria-label={`取消${jobNames[job.type] || '任务'}`} disabled={busy === `cancel-${job.id}`} onClick={() => onCancel(job.id)}><X size={15} /></button>}
    </div>)}</div>
  </section>
}

function availability(value, absentText) {
  if (value === undefined || value === null) return '状态未知，执行时检查'
  if (typeof value === 'boolean') return value ? '已安装，可尝试运行' : absentText
  if (value.available === true || value.installed === true) return value.message || '已安装；登录状态在执行时检查'
  if (value.available === false || value.installed === false) return value.message || absentText
  return typeof value === 'string' ? value : value.message || '执行时检查可用性'
}

export function PlatformSettings({ settings, apiKey, onKeyChange, onSave, online, busy, health, apiModels, onApiModels }) {
  const [draft, setDraft] = useState(settings)
  const [dirty, setDirty] = useState(false)
  const [showKey, setShowKey] = useState(false)
  const [customModel, setCustomModel] = useState(false)       // typing a model name or directory instead of picking one
  useEffect(() => { if (!dirty) setDraft(settings) }, [settings, dirty])
  const asrStatus = health?.asr?.local ?? health?.asr
  const modelNames = asrStatus?.names
  const installedModels = (asrStatus?.models || []).map(localModelKey)
  const modelKey = localModelKey(draft.asr_model)
  const modelChoices = localModelChoices(installedModels, draft.asr_model)
  const typedModel = /[\\/]/.test(modelKey)                    // a repository name or a directory: cannot be checked here
  const asrHeadline = !asrStatus || !asrStatus.available ? availability(asrStatus, '尚未准备本地语音模型')
    : installedModels.includes(modelKey) ? `本地转写可用：${localModelName(modelKey, modelNames)}。模型只从本机文件加载，不自动下载。`
      : typedModel ? '将按填写的仓库名或目录加载模型。只从本机文件加载，不自动下载。'
        : `这台电脑没有“${localModelName(modelKey, modelNames)}”的模型文件，请改选下面“本机可用”里的模型。`
  function change(field, value) { setDraft((current) => field === 'provider' ? settingsForProvider(current, value) : { ...current, [field]: value }); setDirty(true) }
  async function save(event) { event.preventDefault(); const result = await onSave(draft); if (result) setDirty(false) }
  return <>
    <div className="page-heading"><div><span className="eyebrow">整理方式由你选择</span><h1>模型与设置</h1><p>API、Codex CLI、Claude -p，选择你的录音处理方式。</p></div><Settings2 size={29} /></div>
    <form onSubmit={save} className="settings-form">
      <section className="setting-block"><div className="setting-copy"><h2>处理模式</h2><p>Codex 和 Claude 模式先用本地模型转录，再交给所选 Agent 校订和总结。已有人工转录会直接使用。</p></div><div className="setting-fields">
        <label>使用方式<select value={draft.provider} onChange={(event) => change('provider', event.target.value)}><option value="api">硅基流动 API</option><option value="codex">Codex CLI</option><option value="claude">Claude -p</option></select></label>
        {draft.provider === 'api' ? <label>API 文字模型<input value={apiModels.summary} onChange={(event) => onApiModels({ ...apiModels, summary: event.target.value })} placeholder="Qwen/Qwen3-8B" /><small>此模型选项仅保留到当前页面关闭。</small></label> : <>
          <label>{draft.provider === 'codex' ? 'Codex 模型' : 'Claude 模型'}<input value={draft.provider === 'codex' ? draft.codex_model : draft.claude_model} onChange={(event) => change(draft.provider === 'codex' ? 'codex_model' : 'claude_model', event.target.value)} placeholder="留空使用该 CLI 的默认模型" /></label>
          <div className="setting-note"><strong>{availability(health?.providers?.[draft.provider], `本机未检测到 ${draft.provider === 'codex' ? 'Codex' : 'Claude'} CLI`)}</strong><p>使用已在本机登录的账号。无需填写下方 API Key；文字会交给相应云服务处理。</p></div>
        </>}
      </div></section>
      <section className="setting-block"><div className="setting-copy"><h2>语音转文字</h2><p>选择音频的识别方式。CLI 文字模型不能直接代替语音识别。</p></div><div className="setting-fields">
        <label>识别方式<select value={draft.provider === 'api' ? draft.asr : 'local'} disabled={draft.provider !== 'api'} onChange={(event) => change('asr', event.target.value)}><option value="api">硅基流动 API</option><option value="local">本地模型</option></select></label>
        {draft.provider === 'api' && draft.asr === 'api' ? <><label>API 语音模型<input value={apiModels.asr} onChange={(event) => onApiModels({ ...apiModels, asr: event.target.value })} placeholder="FunAudioLLM/SenseVoiceSmall" /><small>此模型选项仅保留到当前页面关闭。</small></label><p className="hint">转录时音频发送至硅基流动，单文件上限 50 MB。</p></> : <><label>本地语音模型
            <select value={customModel ? 'custom' : modelKey} onChange={(event) => { if (event.target.value === 'custom') { setCustomModel(true) } else { setCustomModel(false); change('asr_model', event.target.value) } }}>
              {modelChoices.map((model) => <option key={model} value={model}>{localModelName(model, modelNames)}{installedModels.includes(model) || /[\\/]/.test(model) ? '' : '（这台电脑没有它的模型文件）'}</option>)}
              <option value="custom">其他：填写模型名称或模型目录…</option>
            </select>
            {customModel && <input value={draft.asr_model} onChange={(event) => change('asr_model', event.target.value)} placeholder="例如 medium、Systran/faster-whisper-medium，或模型所在的目录" aria-label="模型名称或模型目录" />}
            {customModel && <small>可以填 Whisper 模型名称、仓库名，或 CTranslate2 模型所在的目录。</small>}
          </label><div className="setting-note"><strong>{asrHeadline}</strong><p>只使用本机已有的模型文件，不自动下载。请按项目说明先准备依赖和模型，缺少时任务会明确提示。</p>{draft.provider !== 'api' && <p>此模式无需 API Key；音频留在本机，转录文字发送给所选 Agent。</p>}{installedModels.length > 0 && <p>本机可用：{installedModels.map((model) => localModelName(model, modelNames)).join('、')}</p>}</div></>}
      </div></section>
      <section className="setting-block"><div className="setting-copy"><h2><KeyRound size={17} />API Key</h2><p>仅 API 模式需要。保留在当前页面内存，刷新后清空，不写入本机设置。</p></div><div className="setting-fields"><label>硅基流动 API Key<div className="key-input"><input type={showKey ? 'text' : 'password'} autoComplete="off" value={apiKey} onChange={(event) => onKeyChange(event.target.value)} placeholder="输入本次使用的 API Key" /><button type="button" onClick={() => setShowKey(!showKey)}>{showKey ? '隐藏' : '显示'}</button></div></label></div></section>
      <div className="settings-save"><span>{dirty ? '有未保存的设置' : '当前模式设置已保存'}</span><button className="p-button primary" disabled={!online || !!busy || !dirty}>保存模式设置</button></div>
    </form>
    <div className="settings-storage"><h2>本机保存，按需使用 AI</h2><p>下载的音频、标题、标签和文字写入本机资料库。设备身份仅在服务运行期间有效。使用 API 或 CLI 整理时，所需内容会发送到你选择的模型服务。</p></div>
  </>
}
