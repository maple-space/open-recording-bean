import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowRight, Bluetooth, CheckCircle2, ChevronRight, FileText, FolderOpen, House, LoaderCircle, MessagesSquare, Mic, RefreshCw, Settings, ShieldCheck, X } from 'lucide-react'
import { summarizeTranscript, transcribeAudio } from './siliconflow.js'
import { api, humanError, initialSettings, jobFailure, terminalStatuses } from './api.js'
import { DevicePanel } from './DevicePanel.jsx'
import { HomePage } from './HomePage.jsx'
import { CommunityDialog } from './CommunityDialog.jsx'
import { Link } from './Link.jsx'
import { project } from './project.js'
import { RecordEditor, RecordWorkspace } from './RecordWorkspace.jsx'
import { ConfirmDialog } from './ConfirmDialog.jsx'
import { matchingArchive, processApiRecord, usesLocalTranscription, canAutoDiscover, buildSelectedArchive, verifyCompleteDeviceBackup } from './workflow.js'
import { JobPanel, PlatformSettings } from './SettingsJobs.jsx'
import { LivePage } from './LivePage.jsx'
import { LiveRecords } from './LiveRecords.jsx'
import './platform.css'

const KIND = 'feishu'      // how the service and the stored records name the recorder
const emptyDevice = { connected: false, files: [], info: {} }
const pages = ['/', '/live', '/device', '/library', '/settings']

// One row of the navigation bar: a white symbol on a coloured rounded square, then the name.
function NavItem({ href, navigate, active, tone, icon: Icon, mark, children }) {
  return <Link href={href} navigate={navigate} className={active ? 'active' : ''} aria-current={active ? 'page' : undefined}>
    <i className={`nav-tile ${tone}`} aria-hidden="true"><Icon size={15} strokeWidth={2.2} /></i><span>{children}</span>{mark}
  </Link>
}

// The address bar's path; '/feishu' is what the recorder's page was called in earlier versions.
function currentRoute() {
  const path = window.location.pathname.replace(/\/$/, '') || '/'
  return path === '/feishu' ? '/device' : path
}

export default function PortalApp() {
  const [route, setRoute] = useState(currentRoute)
  const [records, setRecords] = useState([])
  const [selectedId, setSelectedId] = useState(new URLSearchParams(window.location.search).get('record'))
  const [settings, setSettings] = useState(initialSettings)
  const [health, setHealth] = useState(null)
  const [online, setOnline] = useState(null)
  const [jobs, setJobs] = useState([])
  const [device, setDevice] = useState(emptyDevice)
  const [scans, setScans] = useState([])
  const [community, setCommunity] = useState(false)
  const [apiKey, setApiKey] = useState('')
  const [apiModels, setApiModels] = useState({ summary: 'Qwen/Qwen3-8B', asr: 'FunAudioLLM/SenseVoiceSmall' })
  const [busy, setBusy] = useState('')
  const [apiTask, setApiTask] = useState(null)
  const [notice, setNotice] = useState(null)
  const [deviceRecordId, setDeviceRecordId] = useState(null)
  const [deleteRequest, setDeleteRequest] = useState(null)
  const [autoDiscoverEnabled, setAutoDiscoverEnabled] = useState(() => {
    try { return localStorage.getItem('recording-platform:auto-discover') !== 'false' } catch { return true }
  })
  const [autoDiscovering, setAutoDiscovering] = useState(false)
  const [autoDiscoverError, setAutoDiscoverError] = useState('')
  const deleteRequestRef = useRef(deleteRequest)
  deleteRequestRef.current = deleteRequest
  const editorFlushers = useRef(new Map())
  const trashedIds = useRef(new Set())
  const recordsRef = useRef(records)
  recordsRef.current = records
  const jobsRef = useRef(jobs)
  jobsRef.current = jobs
  const busyRef = useRef(false)
  const pollRef = useRef(false)
  const mounted = useRef(true)
  const settingsLoaded = useRef(false)
  const seenScanJobs = useRef(new Set())

  const deviceRef = useRef(device)
  deviceRef.current = device
  const browserState = { recordStatus: device.recordStatus || 0, export: device.export }
  const onDevicePage = route === '/device'
  const recordsRoute = route === '/live/records' || route.startsWith('/live/records/')
  let recordName = ''
  if (recordsRoute) { try { recordName = decodeURIComponent(route.slice('/live/records/'.length)) } catch { recordName = '' } }
  const activeDeviceJobs = jobs.some((job) => !terminalStatuses.has(job.status) && (job.device_type === KIND || records.some((record) => record.id === job.record_id && record.device_type === KIND)))
  const activeRecordJobs = jobs.some((job) => job.record_id === selectedId && !terminalStatuses.has(job.status))
  const effectiveBusy = busy || (autoDiscovering ? '自动发现录音' : '')

  const showError = useCallback((error) => setNotice({ type: 'error', text: humanError(error) }), [])

  function navigate(href) {
    window.history.pushState({}, '', href)
    setRoute(currentRoute())
    const record = new URLSearchParams(window.location.search).get('record')
    if (record) setSelectedId(record)
    window.scrollTo({ top: 0, behavior: 'instant' })
    // "/live#live-guide" opens the page at that section, once the page has been drawn.
    const anchor = decodeURIComponent(window.location.hash.slice(1))
    if (anchor) setTimeout(() => document.getElementById(anchor)?.scrollIntoView({ block: 'start' }), 80)
  }

  function openRecord(id) { setSelectedId(id); navigate(`/library?record=${encodeURIComponent(id)}`) }

  useEffect(() => {
    const handle = () => {
      setRoute(currentRoute())
      const record = new URLSearchParams(window.location.search).get('record')
      if (record) setSelectedId(record)
    }
    window.addEventListener('popstate', handle)
    return () => window.removeEventListener('popstate', handle)
  }, [])

  const refresh = useCallback(async () => {
    if (pollRef.current) return
    pollRef.current = true
    try {
      const results = await Promise.allSettled([
        api('/api/records'), api('/api/jobs'), api('/api/devices/feishu'),
        settingsLoaded.current ? Promise.resolve(null) : api('/api/settings'),
      ])
      if (!mounted.current) return
      const [recordResult, jobResult, deviceResult, settingResult] = results
      setOnline(recordResult.status === 'fulfilled' && jobResult.status === 'fulfilled')
      if (recordResult.status === 'fulfilled') {
        setRecords(recordResult.value.filter((record) => !trashedIds.current.has(record.id)))
        setSelectedId((current) => current || recordResult.value[0]?.id || null)
      }
      if (jobResult.status === 'fulfilled') {
        setJobs(jobResult.value)
        for (const job of [...jobResult.value].sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)))) {
          if (job.type === 'scan' && job.status === 'completed' && job.device_type && !seenScanJobs.current.has(job.id)) {
            seenScanJobs.current.add(job.id)
            if (Array.isArray(job.result)) setScans(job.result)
          }
        }
      }
      if (deviceResult.status === 'fulfilled') setDevice(deviceResult.value)
      if (settingResult.status === 'fulfilled' && settingResult.value) { setSettings(settingResult.value); settingsLoaded.current = true }
    } finally { pollRef.current = false }
  }, [])

  useEffect(() => {
    mounted.current = true
    refresh()
    api('/api/health').then(setHealth).catch(() => {})
    const timer = setInterval(refresh, 2500)
    const healthTimer = setInterval(() => api('/api/health').then(setHealth).catch(() => {}), 30000)
    return () => { mounted.current = false; clearInterval(timer); clearInterval(healthTimer) }
  }, [refresh])

  useEffect(() => {
    const timer = setInterval(async () => {
      const hasDeviceJob = jobsRef.current.some((job) => job.device_type && !terminalStatuses.has(job.status))
      if (!canAutoDiscover({ enabled: autoDiscoverEnabled, connected: deviceRef.current.connected && !deviceRef.current.live, busy: busyRef.current || deleteRequestRef.current, hasDeviceJob })) return
      // Own the same lock as manual downloads/deletion until listFiles settles.
      busyRef.current = true
      setAutoDiscovering(true)
      try {
        await submitJob({ type: 'refresh', device_type: KIND })
        if (mounted.current) setAutoDiscoverError('')
      } catch (error) {
        if (mounted.current) setAutoDiscoverError(humanError(error))
      } finally {
        busyRef.current = false
        if (mounted.current) setAutoDiscovering(false)
      }
    }, 12000)
    return () => clearInterval(timer)
  }, [autoDiscoverEnabled])

  function toggleAutoDiscover(value) {
    setAutoDiscoverEnabled(value)
    try { localStorage.setItem('recording-platform:auto-discover', String(value)) } catch { /* optional browser preference */ }
  }

  async function run(label, action) {
    if (busyRef.current) return null
    busyRef.current = true; setBusy(label); setNotice(null)
    try { return await action() } catch (error) { showError(error); return null }
    finally { busyRef.current = false; setBusy(''); setApiTask(null); refresh() }
  }

  function updateJob(job) { setJobs((current) => [job, ...current.filter((item) => item.id !== job.id)].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))) }

  async function submitJob(request, endpoint = '/api/jobs') {
    let job = await api(endpoint, { method: 'POST', body: request })
    updateJob(job)
    while (!terminalStatuses.has(job.status)) {
      await new Promise((resolve) => setTimeout(resolve, 1000))
      job = await api(`/api/jobs/${encodeURIComponent(job.id)}`)
      if (!mounted.current) return null
      updateJob(job)
    }
    if (job.status === 'failed') throw jobFailure(job)
    if (job.status === 'cancelled') throw new Error('任务已取消。')
    if (job.result?.record) upsertRecord(job.result.record)
    if (job.result?.notice) setNotice({ type: 'info', text: job.result.notice, recordId: job.result.record?.id })
    return job.result
  }

  function upsertRecord(record) {
    if (!record || trashedIds.current.has(record.id)) return
    recordsRef.current = [record, ...recordsRef.current.filter((item) => item.id !== record.id)]
    setRecords(recordsRef.current)
  }

  const registerEditor = useCallback((id, flush) => {
    if (flush) editorFlushers.current.set(id, flush)
    else editorFlushers.current.delete(id)
  }, [])

  async function saveRecord(id, patch) {
    const record = await api(`/api/records/${encodeURIComponent(id)}`, { method: 'PATCH', body: patch })
    upsertRecord(record)
    return record
  }

  async function importFile(file) {
    return run('导入本地音频', async () => {
      const form = new FormData()
      form.append('file', file); form.append('device_type', 'local'); form.append('device_id', 'local-import')
      form.append('original_id', crypto.randomUUID()); form.append('title', file.name.replace(/\.[^.]+$/, ''))
      const record = await api('/api/records/import', { method: 'POST', body: form })
      upsertRecord(record); openRecord(record.id)
      setNotice({ type: 'success', text: '音频已导入本机资料库。' })
      return record
    })
  }

  function connectDevice(address) {
    return run('连接录音豆', async () => {
      await submitJob({ type: 'connect', device_type: KIND, address })
      setNotice({ type: 'success', text: '录音豆已连接，可以读取并下载录音，也可以开启实时输入。' })
    })
  }

  function scanDevices() {
    return run('搜索附近的录音豆', async () => {
      const result = await submitJob({ type: 'scan', device_type: KIND })
      setScans(Array.isArray(result) ? result : [])
      if (!result?.length) setNotice({ type: 'info', text: '这次没有搜到录音豆。请确认它已唤醒、电脑蓝牙已打开，并且手机上的 App 没有连着它。' })
    })
  }

  async function archiveFile(file, expectedDeviceId) {
    const snapshot = deviceRef.current
    if (!snapshot?.device_id || (expectedDeviceId && expectedDeviceId !== snapshot.device_id)) throw new Error('设备标识已变化，请重新选择录音。')
    const saved = matchingArchive(recordsRef.current, KIND, snapshot.device_id, file.original_id)
    if (saved) return saved
    if (!snapshot.connected) throw new Error('这条录音尚未归档，请先连接设备。')
    const result = await submitJob({ action: 'download', device_id: snapshot.device_id, ids: [String(file.original_id)] }, '/api/devices/feishu/actions')
    const record = result?.record
    if (!record) throw new Error('录音未成功归档，请重试。')
    trashedIds.current.delete(record.id)
    upsertRecord(record)
    return record
  }

  function downloadFiles(files) {
    const deviceId = deviceRef.current?.device_id
    return run('下载录音到本机', async () => {
      const result = await submitJob({ action: 'download', device_id: deviceId, ids: files.map((file) => String(file.original_id)) }, '/api/devices/feishu/actions')
      for (const record of result?.records || []) upsertRecord(record)
      const lastRecord = result?.record
      if (lastRecord) setDeviceRecordId(lastRecord.id)
      setNotice({ type: 'success', text: `${files.length} 条录音已保存在本机资料库。`, recordId: lastRecord?.id })
      return lastRecord
    })
  }

  function openDeviceFile(file) {
    return run('准备录音', async () => {
      const record = await archiveFile(file)
      setDeviceRecordId(record.id)
      return record
    })
  }

  async function saveGeneratedResult(record, field, text) {
    // A source edit can still be waiting on the editor's autosave timer when
    // the remote API returns. Persist it before the server compares baselines.
    await editorFlushers.current.get(record.id)?.()
    const result = await api(`/api/records/${encodeURIComponent(record.id)}/generated-result`, {
      method: 'POST',
      body: {
        field, text,
        baseline_transcript: record.transcript || '', baseline_summary: record.summary || '',
        baseline_summary_template: record.summary_template || 'meeting', baseline_custom_prompt: record.custom_prompt || '',
      },
    })
    upsertRecord(result.record)
    setNotice({ type: 'success', text: result.notice || (field === 'transcript' ? '转录已保存到本机。' : '整理结果已保存到本机。') })
    return result.record
  }

  async function transcribeAction(record) {
      if (usesLocalTranscription(settings)) {
        const result = await submitJob({ type: 'transcribe', record_id: record.id })
        return result?.record
      }
      if (!apiKey.trim()) throw new Error('请先在设置中输入本次使用的 API Key。')
      setApiTask({ recordId: record.id, field: 'transcript' })
      const response = await fetch(record.audio_url || `/api/records/${encodeURIComponent(record.id)}/audio`, { cache: 'no-store' })
      if (!response.ok) throw new Error('无法读取本机归档音频，请检查本机服务。')
      const blob = await response.blob()
      const disposition = response.headers.get('content-disposition') || ''
      const filename = disposition.match(/filename="?([^";]+)/)?.[1] || `recording.${blob.type.includes('wav') ? 'wav' : blob.type.includes('ogg') ? 'ogg' : blob.type.includes('mpeg') ? 'mp3' : 'm4a'}`
      const transcript = await transcribeAudio({ apiKey, blob, fileName: filename, model: apiModels.asr || undefined })
      return saveGeneratedResult(record, 'transcript', transcript)
  }

  async function summarizeAction(record) {
      if (!record.transcript?.trim()) throw new Error('请先生成或粘贴转录文字。')
      if (settings.provider !== 'api') {
        const result = await submitJob({ type: 'summarize', record_id: record.id })
        return result?.record
      }
      if (!apiKey.trim()) throw new Error('请先在设置中输入本次使用的 API Key。')
      if (record.summary_template === 'custom' && !record.custom_prompt?.trim()) throw new Error('请填写自定义整理要求。')
      setApiTask({ recordId: record.id, field: 'summary' })
      const summary = await summarizeTranscript({ apiKey, transcript: record.transcript, template: record.summary_template || 'meeting', customPrompt: record.custom_prompt || '', model: apiModels.summary || undefined })
      if (!summary.trim()) throw new Error('模型未返回整理结果，请重试。')
      return saveGeneratedResult(record, 'summary', summary)
  }

  function transcribe(record) { return run('语音转文字', () => transcribeAction(record)) }
  function summarize(record) { return run('整理转录文字', () => summarizeAction(record)) }

  async function processAction(record) {
    if (record.summary_template === 'custom' && !record.custom_prompt?.trim()) throw new Error('请填写自定义整理要求。')
    if (settings.provider !== 'api') {
      const result = await submitJob({ type: 'process', record_id: record.id })
      return result?.record
    }
    if (!apiKey.trim()) throw new Error('请先在设置中输入本次使用的 API Key。')
    return processApiRecord(record, { transcribe: transcribeAction, summarize: summarizeAction })
  }

  function processRecord(record) { return run('转录并交给 Agent 整理', () => processAction(record)) }

  function processDeviceFiles(files) {
    const deviceId = deviceRef.current?.device_id
    return run('转录并交给 Agent 整理', async () => {
      let result
      for (let index = 0; index < files.length; index += 1) {
        setBusy(`处理录音 ${index + 1}/${files.length}`)
        let record = await archiveFile(files[index], deviceId)
        await editorFlushers.current.get(record.id)?.()
        record = recordsRef.current.find((item) => item.id === record.id) || record
        setDeviceRecordId(record.id)
        result = await processAction(record)
      }
      return result
    })
  }

  async function exportRecordsAction(ids) {
    for (const id of ids) await editorFlushers.current.get(id)?.()
    const selected = ids.map((id) => recordsRef.current.find((record) => record.id === id))
    if (selected.some((record) => !record)) throw new Error('部分录音已不在资料库中，请重新选择。')
    const output = await buildSelectedArchive(selected, async (record) => {
      const response = await fetch(`/api/records/${encodeURIComponent(record.id)}/bundle`, { cache: 'no-store' })
      if (!response.ok) throw new Error(`无法读取“${record.title || record.id}”的资料包，已停止导出。`)
      return new Uint8Array(await response.arrayBuffer())
    }, (index, count) => setBusy(`打包资料 ${index}/${count}`))
    const url = URL.createObjectURL(new Blob([output], { type: 'application/zip' }))
    const link = document.createElement('a')
    link.href = url; link.download = `录音资料-${new Date().toISOString().slice(0, 10)}-${selected.length}条.zip`
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
    setNotice({ type: 'success', text: `已生成 ${selected.length} 条录音的完整资料包，浏览器已开始下载。` })
    return true
  }

  function exportRecords(ids) { return run('导出所选资料包', () => exportRecordsAction(ids)) }

  function exportDeviceFiles(files) {
    const deviceId = deviceRef.current?.device_id
    return run('归档并导出所选资料包', async () => {
      const ids = []
      for (let index = 0; index < files.length; index += 1) {
        setBusy(`归档录音 ${index + 1}/${files.length}`)
        const record = await archiveFile(files[index], deviceId)
        ids.push(record.id)
      }
      return exportRecordsAction(ids)
    })
  }

  function requestTrash(ids) {
    return run('准备移到废纸篓', async () => {
      for (const id of ids) await editorFlushers.current.get(id)?.()
      const selected = recordsRef.current.filter((record) => ids.includes(record.id))
      if (selected.length) setDeleteRequest({ type: 'local', records: selected })
      return true
    })
  }

  async function verifyBackup(record, deviceId, file) {
    return verifyCompleteDeviceBackup(record, deviceId, file, {
      loadRecords: () => api('/api/records'),
      loadAudio: (id) => fetch(`/api/records/${encodeURIComponent(id)}/audio`, { cache: 'no-store' }),
    })
  }

  function requestDeviceDelete(files) {
    return run('准备删除设备录音', async () => {
      const snapshot = deviceRef.current
      if (!snapshot.connected) throw new Error('请先连接录音豆。')
      if (files.length) setDeleteRequest({ type: 'device', deviceId: snapshot.device_id, files: files.map((file) => ({ ...file })) })
    })
  }

  function confirmDelete(options) {
    const request = deleteRequest
    if (!request) return
    if (request.type === 'device') {
      return run('删除设备原录音', async () => {
        // run 已取得操作锁；确认框只负责确认，后续进度显示在主页面。
        setDeleteRequest(null)
        for (const file of request.files) {
          const saved = matchingArchive(recordsRef.current, KIND, request.deviceId, file.original_id)
          if (saved) await editorFlushers.current.get(saved.id)?.()
        }
        const result = await submitJob({ action: 'delete', device_id: request.deviceId, ids: request.files.map((file) => String(file.original_id)), ...options }, '/api/devices/feishu/actions')
        const count = result?.count || 0
        for (const record of result?.records || []) upsertRecord(record)
        setNotice({ type: 'success', text: options.mode === 'direct'
          ? `已直接删除 ${count} 条设备原录音；本次未创建备份，已有本机资料未改动。`
          : `已删除 ${count} 条设备原录音，本机备份已保留。` })
        return true
      })
    }
    return run('移到废纸篓', async () => {
      setDeleteRequest(null)
      let count = 0
      try {
        for (let index = 0; index < request.records.length; index += 1) {
          const record = request.records[index]
          await editorFlushers.current.get(record.id)?.()
          await api(`/api/records/${encodeURIComponent(record.id)}/trash`, { method: 'POST' })
          trashedIds.current.add(record.id)
          recordsRef.current = recordsRef.current.filter((item) => item.id !== record.id)
          setRecords(recordsRef.current)
          setSelectedId((current) => current === record.id ? recordsRef.current[0]?.id || null : current)
          count += 1
        }
        setNotice({ type: 'success', text: `${count} 条本机资料已移到 Mac 废纸篓。` })
        return true
      } catch (error) {
        throw new Error(`${count ? `已完成 ${count} 条；` : ''}${humanError(error)}`)
      }
    })
  }

  const selectedDeviceRecord = records.find((record) => record.id === deviceRecordId && record.device_type === KIND && record.device_id === device.device_id)
  const selectedDeviceBusy = jobs.some((job) => job.record_id === selectedDeviceRecord?.id && !terminalStatuses.has(job.status))
  const editorProps = {
    settings, apiKey, online, onSave: saveRecord, onTranscribe: transcribe, onSummarize: summarize,
    onProcess: processRecord, onTrash: requestTrash, onError: showError, onRegisterFlush: registerEditor,
  }

  return <div className="portal-app">
    <aside className="portal-sidebar"><Link href="/" navigate={navigate} className="portal-brand"><img src="/favicon.svg" alt="" width="40" height="40" /><div><strong>{project.title}</strong><small>{project.name}</small></div></Link>
      <nav aria-label="主导航">
        <NavItem href="/" navigate={navigate} tone="blue" icon={House} active={route === '/'}>开始使用</NavItem>
        <NavItem href="/live" navigate={navigate} tone="green" icon={Mic} active={route === '/live'} mark={device.live && <i className="nav-online" title="正在传声" />}>实时输入</NavItem>
        <NavItem href="/live/records" navigate={navigate} tone="orange" icon={FileText} active={recordsRoute}>文字记录</NavItem>
        <NavItem href="/device" navigate={navigate} tone="indigo" icon={Bluetooth} active={onDevicePage} mark={device.connected && <i className="nav-online" title="已连接" />}>录音豆</NavItem>
        <div className="nav-divider" />
        <NavItem href="/library" navigate={navigate} tone="slate" icon={FolderOpen} active={route === '/library'} mark={<small>{records.length || ''}</small>}>本机资料库</NavItem>
        <NavItem href="/settings" navigate={navigate} tone="grey" icon={Settings} active={route === '/settings'}>模型与设置</NavItem>
      </nav>
      <button type="button" className="sidebar-community" onClick={() => setCommunity(true)}><i className="nav-tile mint" aria-hidden="true"><MessagesSquare size={15} strokeWidth={2.2} /></i><span>加入用户群</span><ChevronRight size={15} /></button>
      <div className="sidebar-bottom"><ShieldCheck size={15} /><p>声音和文字留在本机。使用 AI 整理时，按所选模式处理。</p></div>
    </aside>
    <main className="portal-main"><header className="portal-topbar"><span>{onDevicePage ? '录音豆 / 连接与录音文件' : route === '/library' ? '本机资料库' : route === '/settings' ? '模型与设置' : route === '/live' ? '实时输入' : recordsRoute ? '实时输入 / 文字记录' : '开始使用'}</span><div className={`service-state ${online === true ? 'online' : ''}`}><i />{online === null ? '正在连接本机服务' : online ? '本机服务已连接' : '本机服务未连接'}</div></header>
      {online === false && <div className="p-notice error" role="alert"><span>无法连接本机服务。请在项目文件夹里运行 ./start.sh；蓝牙连接、实时输入和后台任务都需要它在运行。</span><button className="p-button compact" onClick={refresh}><RefreshCw size={14} />重试</button></div>}
      {notice && <div className={`p-notice ${notice.type}`} role={notice.type === 'error' ? 'alert' : 'status'}><span>{notice.type === 'success' && <CheckCircle2 size={16} />}{notice.text}</span>{notice.recordId && <button className="text-link" onClick={() => openRecord(notice.recordId)}>在资料库打开</button>}<button className="p-icon-button" aria-label="关闭提示" onClick={() => setNotice(null)}><X size={16} /></button></div>}
      {busy && <div className="busy-indicator" role="status"><LoaderCircle className="p-spin" size={15} />{busy}… <span>蓝牙与设备任务由本机服务保持，刷新或关闭网页不会取消；可在下方查看或取消任务。</span></div>}
      <div className="portal-page">
        {route === '/' && <HomePage device={device} records={records} online={online} navigate={navigate} onOpen={openRecord} onCommunity={() => setCommunity(true)} />}
        {route === '/live' && <LivePage online={online} navigate={navigate} />}
        {recordsRoute && <LiveRecords name={recordName} navigate={navigate} />}
        {onDevicePage && device.live && <div className="p-notice" role="status"><span>实时输入正在进行，录音豆的蓝牙由它独占。刷新、下载、删除和录音控制要先关闭实时输入。</span><Link href="/live" navigate={navigate} className="text-link">去实时输入 <ArrowRight size={14} /></Link></div>}
        {onDevicePage && <DevicePanel snapshot={device} browserState={browserState} records={records} scans={scans} busy={effectiveBusy || (activeDeviceJobs ? 'device-job' : '') || (device.live ? 'live-input' : '')} online={online} onConnect={connectDevice} onScan={scanDevices}
          onRefresh={() => run('刷新录音列表', () => submitJob({ type: 'refresh', device_type: KIND }))}
          onDisconnect={() => run('断开录音豆', () => api('/api/devices/feishu/disconnect', { method: 'POST' }))}
          onDownload={downloadFiles}
          onRecord={(action) => run(action === 'pause' ? '暂停录音' : '开始录音', () => submitJob({ action, device_id: deviceRef.current.device_id }, '/api/devices/feishu/actions'))}
          onOpenLibrary={openRecord} onOpenFile={openDeviceFile} onProcessFiles={processDeviceFiles} onExportFiles={exportDeviceFiles} onDeleteFiles={requestDeviceDelete} settings={settings} selectedRecordId={selectedDeviceRecord?.id}
          autoDiscoverEnabled={autoDiscoverEnabled} autoDiscovering={autoDiscovering} autoDiscoverError={autoDiscoverError} onAutoDiscover={toggleAutoDiscover}
          editor={selectedDeviceRecord ? <RecordEditor key={selectedDeviceRecord.id} record={selectedDeviceRecord} {...editorProps} busy={effectiveBusy || (selectedDeviceBusy ? 'record-job' : '')} lockedField={apiTask?.recordId === selectedDeviceRecord.id ? apiTask.field : null} /> : null} />}
        {route === '/library' && <RecordWorkspace records={records} selectedId={selectedId} onSelect={(id) => { setSelectedId(id); window.history.replaceState({}, '', `/library?record=${encodeURIComponent(id)}`) }} settings={settings} apiKey={apiKey} busy={effectiveBusy || (activeRecordJobs ? 'record-job' : '')} lockedField={apiTask?.recordId === selectedId ? apiTask.field : null} online={online} onImport={importFile} onSave={saveRecord} onTranscribe={transcribe} onSummarize={summarize} onProcess={processRecord} onExport={exportRecords} onTrash={requestTrash} onRegisterFlush={registerEditor} onError={showError} />}
        {route === '/settings' && <PlatformSettings settings={settings} apiKey={apiKey} onKeyChange={setApiKey} onSave={(value) => run('保存模式设置', async () => { const result = await api('/api/settings', { method: 'PATCH', body: value }); setSettings(result); setNotice({ type: 'success', text: '模式设置已保存到本机。' }); return result })} online={online} busy={effectiveBusy} health={health} apiModels={apiModels} onApiModels={setApiModels} />}
        {!pages.includes(route) && !recordsRoute && <div className="p-empty"><h1>没有这个页面</h1><Link href="/" navigate={navigate} className="p-button">返回首页</Link></div>}
        <JobPanel jobs={jobs} busy={busy} onCancel={async (id) => { try { const job = await api(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: 'POST' }); updateJob(job); refresh() } catch (error) { showError(error) } }} />
      </div>
      {deleteRequest && <ConfirmDialog key={`${deleteRequest.type}-${deleteRequest.deviceId || ''}-${(deleteRequest.files || deleteRequest.records).map((item) => item.original_id || item.id).join('-')}`} request={deleteRequest} busy={!!busy} onCancel={() => setDeleteRequest(null)} onConfirm={confirmDelete} />}
      {community && <CommunityDialog onClose={() => setCommunity(false)} />}
      <footer className="portal-footer">{project.name} · 非官方开源项目，与安克、soundcore、飞书没有关联 · 声音和文字留在本机</footer>
    </main>
  </div>
}
