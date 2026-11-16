import { useEffect, useState } from 'react'
import { ArrowRight, Bluetooth, Check, Download, FileAudio, Mic, Pause, Play, Radio, RefreshCw, Search, Sparkles, Trash2, Unplug } from 'lucide-react'
import { deviceNames, formatBytes, formatDate, formatDuration } from './api.js'
import { matchingArchive } from './workflow.js'
import { Link } from './Link.jsx'

const KIND = 'feishu'      // how the service and the stored records name the recorder

export function HardwareMark({ large = false }) {
  return <div className={`hardware-mark ${large ? 'large' : ''}`} aria-hidden="true"><img src="/assets/recorder.svg" alt="" /></div>
}

export function DevicePanel({ snapshot, browserState, records, scans, busy, disconnecting, online, navigate, onConnect, onScan, onRefresh, onDisconnect, onDownload, onRecord, onOpenFile, onProcessFiles, onExportFiles, onDeleteFiles, settings, selectedRecordId, editor }) {
  const [selected, setSelected] = useState(new Set())
  const [query, setQuery] = useState('')
  const [address, setAddress] = useState('')
  const files = snapshot?.files || []
  const visible = files.filter((file) => `${file.title} ${file.original_id}`.toLowerCase().includes(query.toLowerCase()))
  const connected = Boolean(snapshot?.connected)
  const selectedFiles = files.filter((file) => selected.has(file.original_id))
  const archived = (file) => matchingArchive(records, KIND, snapshot?.device_id, file.original_id)
  const disableConnect = Boolean(busy) || !online
  const disableDisconnect = Boolean(disconnecting) || !online || Boolean(snapshot?.live)
  const info = snapshot?.info || {}
  const battery = info.battery
  const freeBytes = info.freeMemoryKb != null ? info.freeMemoryKb * 1024 : null
  const firmware = info.firmwareVersion
  const providerName = settings.provider === 'codex' ? 'Codex CLI' : settings.provider === 'claude' ? 'Claude -p' : 'API'
  const canProcessSelected = selectedFiles.length > 0 && (connected || selectedFiles.every(archived))
  useEffect(() => { setSelected(new Set()) }, [snapshot?.device_id])

  function toggle(id) {
    setSelected((current) => { const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next })
  }

  return <>
    <section className={`hardware-heading ${editor ? 'has-editor' : ''}`}>
      <div className="hardware-intro"><span className="eyebrow">硬件</span><h1>{deviceNames[KIND]}</h1><p>连接由这台电脑的本机服务保持，刷新或关闭网页不影响已连接的录音豆。<br />适用于 soundcore Work（安克 AI 录音豆、飞书录音豆），型号 D3200。</p>
        <div className={`connection-state ${connected ? 'connected' : ''}`}><span />{online === false ? '本机服务不可达，设备状态待确认' : connected ? `已连接 · ${snapshot.device_name || snapshot.device_id || deviceNames[KIND]}` : snapshot?.connection_status === 'reconnecting' ? '连接已中断，后台正在重连' : '尚未连接'}{connected && battery != null ? ` · 电量 ${battery}%` : ''}</div>
      </div>
      <HardwareMark large />
    </section>
    {(connected || firmware) && <dl className="device-statistics">{battery != null && <div><dt>设备电量</dt><dd>{battery}%</dd></div>}{info.boxBattery != null && <div><dt>充电仓电量</dt><dd>{info.boxBattery}%</dd></div>}{freeBytes != null && <div><dt>可用空间</dt><dd>{formatBytes(freeBytes)}</dd></div>}{firmware && <div><dt>固件</dt><dd>{firmware}</dd></div>}</dl>}
    <section className="device-controls">
      <div className="button-row">
        {connected ? <>
          <button className="p-button quiet" disabled={disableDisconnect} onClick={onDisconnect}><Unplug size={15} />断开连接{disconnecting ? '中…' : ''}</button>
        </> : <>
          <button className="p-button primary" disabled={disableConnect} onClick={onScan}><Search size={16} />搜索附近的录音豆</button>
          {snapshot?.desired_connected && <button className="p-button quiet" disabled={disableDisconnect} onClick={onDisconnect}><Unplug size={15} />停止重连{disconnecting ? '中…' : ''}</button>}
        </>}
      </div>
      <p className="hint">录音豆同一时间只能连一个设备。要让手机上的 App（飞书或 soundcore）接回它，请先在这里点“断开连接”。本机服务退出或电脑休眠时连接也会中断。</p>
      {snapshot?.live && <p className="hint">正在实时输入。请先到“实时输入”页面关闭，再断开连接或管理设备录音。</p>}
      {snapshot?.error && <p className="hint" role="status">{snapshot.error}</p>}
      {!connected && <div className="device-discovery">
        {scans?.length > 0 && <div className="scan-results">{scans.map((device) => <button className="scan-result" key={device.address || device.device_id} disabled={disableConnect} onClick={() => onConnect(device.address || device.device_id)}><Bluetooth size={17} /><span><strong>{device.name || device.device_name || '未命名设备'}</strong><small>{device.address || device.device_id}</small></span><ArrowRight size={16} /></button>)}</div>}
        <details><summary>使用已知设备地址连接</summary><form className="address-form" onSubmit={(event) => { event.preventDefault(); onConnect(address.trim()) }}><label>蓝牙地址 / macOS 设备 UUID<input value={address} onChange={(event) => setAddress(event.target.value)} placeholder="粘贴设备地址" required /></label><button className="p-button" disabled={disableConnect || !address.trim()}>连接设备</button></form></details>
        <p className="hint">搜不到时：把录音豆从充电盒里取出或按一下按键唤醒它，并在手机上退出飞书或 soundcore App（或关掉手机蓝牙）。第一次搜索时系统会询问蓝牙权限，请选择允许。</p>
      </div>}
    </section>
    {connected && <section className="device-next-step" aria-labelledby="device-next-title">
      <div><h2 id="device-next-title">{snapshot?.live ? '正在实时输入' : '已连接，下一步开启实时输入'}</h2><p>对着录音豆说话，在网页实时转成文字，也可传给输入法或会议软件。</p></div>
      <Link href="/live" navigate={navigate} className="p-button primary"><Mic size={16} />{snapshot?.live ? '查看实时输入' : '进入实时输入'}<ArrowRight size={16} /></Link>
    </section>}
    {connected && <details className="device-record-controls">
      <summary>仅在录音豆上录音 <span>{browserState.recordStatus === 1 ? '正在录音' : browserState.recordStatus === 2 ? '已暂停' : '未录音'}</span></summary>
      <p className="hint">遥控录音豆自身的录音，声音保存在录音豆里。这里不会开启网页实时文字或向输入法传声；需要这些功能请进入上方“实时输入”。</p>
      <button className="p-button quiet" disabled={!!busy || !online} onClick={() => onRecord(browserState.recordStatus === 1 ? 'pause' : 'start')}>{browserState.recordStatus === 1 ? <Pause size={15} /> : <Radio size={15} />}{browserState.recordStatus === 1 ? '暂停设备录音' : browserState.recordStatus === 2 ? '继续设备录音' : '开始设备录音'}</button>
    </details>}
    <section className="device-file-panel">
      <div className="device-next-step"><div><h2>把全部录音同步到本机资料库</h2><p>保留设备原件，不自动转写。已归档录音核验后跳过，中断后可继续同步。</p></div><button className="p-button primary" disabled={!connected || !online || !!busy || !files.length} onClick={() => onDownload(files)}><Download size={16} />同步全部录音{files.length ? `（${files.length}）` : ''}</button></div>
      <div className="panel-heading"><div><h2>录音豆里的录音 <span>{files.length}</span></h2><p>点击录音即可在本页播放、转录和总结。首次读取会归档到本机。</p></div><div className="button-row"><button className="p-button" disabled={!connected || !online || !!busy || !selectedFiles.length} onClick={() => onDownload(selectedFiles)}><Download size={15} />保存所选到资料库{selectedFiles.length > 0 ? ` (${selectedFiles.length})` : ''}</button><button className="p-button danger" disabled={!connected || !online || !!busy || !selectedFiles.length} onClick={() => onDeleteFiles(selectedFiles)} title="选择先备份再删除，或输入确认文字后不备份直接删除"><Trash2 size={15} />删除设备录音</button></div></div>
      <div className="device-directory-refresh"><button className="p-button" disabled={!connected || !!busy || !online} onClick={onRefresh}><RefreshCw size={15} />刷新录音列表</button><p className="hint">连接时读取一次列表。录完后点击刷新；先结束录音并关闭实时输入，再保存到资料库，本机已缓存的音频只补传缺失部分。</p></div>
      <div className="device-agent-bar"><button className="p-button primary" disabled={!canProcessSelected || !online || !!busy} onClick={() => onProcessFiles(selectedFiles)}><Sparkles size={15} />转录并交给 Agent 整理{selectedFiles.length ? `（${selectedFiles.length}）` : ''}</button><button className="p-button" disabled={!canProcessSelected || !online || !!busy} onClick={() => onExportFiles(selectedFiles)}><Download size={15} />导出所选资料包</button><span>当前：{providerName} · 已有转录会直接用于总结</span></div>
      <p className="hint">删除前会完整读回本机备份并核对大小；这不代表设备原始传输已经完成逐包完整性验证。大小按录音时长估算（每小时约 29 MB）。</p>
      <div className="file-filters"><label className="checkbox-label"><input type="checkbox" aria-label="选择全部可见录音" checked={visible.length > 0 && visible.every((file) => selected.has(file.original_id))} onChange={(event) => setSelected(event.target.checked ? new Set(visible.map((file) => file.original_id)) : new Set())} />全选</label><label className="p-search"><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索设备录音" aria-label="搜索设备录音" /></label></div>
      {browserState.export?.status === 'downloading' && <div className="transfer-progress" role="status"><span>正在保存音频 · {browserState.export.progress}%{browserState.export.reusedFrames > 0 ? ` · 复用 ${Math.floor(browserState.export.reusedFrames / 50)} 秒` : ''}{browserState.export.receivedBytes != null ? ` · 已接收 ${formatBytes(browserState.export.receivedBytes)}` : ''}</span><progress max="100" value={browserState.export.progress} /></div>}
      {visible.length ? <div className="device-files">{visible.map((file) => { const saved = archived(file); return <div className={`device-file ${saved?.id === selectedRecordId ? 'active' : ''}`} key={file.original_id}>
        <input type="checkbox" aria-label={`选择 ${file.title || file.original_id}`} checked={selected.has(file.original_id)} onChange={() => toggle(file.original_id)} />
        <span className="audio-symbol"><FileAudio size={21} /></span>
        <button className="file-name file-open" disabled={!!busy || !online || (!connected && !saved)} onClick={() => onOpenFile(file)}><strong>{saved?.title || file.title || `录音 ${file.original_id}`}</strong><small>{formatDate(file.recorded_at)} · {formatDuration(saved?.duration_ms || file.duration_ms)} · {formatBytes(file.size_bytes)}</small>{saved && <span className="saved-button"><Check size={12} />{saved.summary ? '已总结' : saved.transcript ? '已转录' : '已归档'}</span>}</button>
        <div className="file-actions"><button className="p-button compact" disabled={!!busy || !online || (!connected && !saved)} onClick={() => onOpenFile(file)}><Play size={13} />{saved ? '播放与整理' : '读取并整理'}</button><button className="p-icon-button danger" aria-label={`删除设备录音 ${file.title || file.original_id}`} disabled={!!busy || !online || !connected} onClick={() => onDeleteFiles([file])}><Trash2 size={14} /></button></div>
      </div> })}</div> : <div className="p-empty"><FileAudio size={30} /><h3>{query ? '没有匹配的录音' : connected ? '设备列表为空' : '连接后读取录音列表'}</h3><p>{query ? '换个关键词再试试。' : connected ? '录制完成后，点击“刷新录音”重新读取。' : '先搜索并连接录音豆，里面的录音会显示在这里。'}</p></div>}
      {!connected && files.length > 0 && <p className="hint stale-note">当前显示上次读取的列表。下载前请重新连接设备。</p>}
    </section>
    <section className="device-editor-panel" aria-label="设备录音整理">{editor || <div className="p-empty"><FileAudio size={28} /><h3>选择一段录音，直接开始整理</h3><p>这里会显示音频播放器、转录、总结和导出选项。</p></div>}</section>
  </>
}
