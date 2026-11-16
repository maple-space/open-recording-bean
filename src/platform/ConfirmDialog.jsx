import { useEffect, useRef, useState } from 'react'
import { AlertTriangle, Trash2 } from 'lucide-react'
import { deviceDeletePhrase } from './deviceDeletion.js'

export function ConfirmDialog({ request, busy, onCancel, onConfirm }) {
  const [phrase, setPhrase] = useState('')
  const [mode, setMode] = useState('backup')
  const cancel = useRef(null)
  const dialog = useRef(null)
  const permanent = request.type === 'device'
  const requiredPhrase = deviceDeletePhrase(mode)
  const items = permanent ? request.files.map((file) => ({ id: file.original_id, title: file.title || file.original_id })) : request.records
  useEffect(() => {
    const previous = document.activeElement
    cancel.current?.focus()
    return () => { if (previous?.isConnected) previous.focus() }
  }, [])
  function handleKey(event) {
    if (event.key === 'Escape' && !busy) onCancel()
    if (event.key !== 'Tab') return
    const fields = [...dialog.current.querySelectorAll('button:not(:disabled), input:not(:disabled)')]
    if (!fields.length) { event.preventDefault(); return }
    if (event.shiftKey && document.activeElement === fields[0]) { event.preventDefault(); fields.at(-1).focus() }
    else if (!event.shiftKey && document.activeElement === fields.at(-1)) { event.preventDefault(); fields[0].focus() }
  }
  return <div className="dialog-backdrop" onKeyDown={handleKey}>
    <section ref={dialog} className="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="delete-dialog-title">
      <AlertTriangle size={25} />
      <h2 id="delete-dialog-title">{permanent ? '从录音豆永久删除原录音' : '移到 Mac 废纸篓'}</h2>
      <p>{permanent ? '设备没有回收站，删除后无法恢复。已有本机音频、转录和总结会保留。' : '音频、文字和元数据会一起移到 Mac 废纸篓。设备中的原录音不受影响。'}</p>
      <ul>{items.map((record) => <li key={record.id}>{record.title || '未命名录音'}</li>)}</ul>
      {permanent && <>
        <fieldset className="device-delete-options" disabled={busy}>
          <legend>删除方式</legend>
          <label><input type="radio" name="device-delete-mode" value="backup" checked={mode === 'backup'} onChange={() => { setMode('backup'); setPhrase('') }} />先备份到本机，再删除（默认）</label>
          <label><input type="radio" name="device-delete-mode" value="direct" checked={mode === 'direct'} onChange={() => { setMode('direct'); setPhrase('') }} />不备份，直接删除设备录音</label>
        </fieldset>
        <p role="note">{mode === 'direct' ? '本次不会下载或创建备份。未另行保存的录音将永久丢失。' : '确认后先下载并校验全部所选录音；备份失败会停止，不会直接删除。'}</p>
        <label>输入“{requiredPhrase}”确认<input disabled={busy} autoComplete="off" value={phrase} onChange={(event) => setPhrase(event.target.value)} placeholder={requiredPhrase} /></label>
      </>}
      <div className="button-row"><button ref={cancel} className="p-button" disabled={busy} onClick={onCancel}>取消</button><button className="p-button danger" disabled={busy || (permanent && phrase !== requiredPhrase)} onClick={() => onConfirm({ mode, phrase })}><Trash2 size={14} />{permanent ? `${mode === 'direct' ? '不备份，永久删除' : '备份后永久删除'}（${items.length} 条）` : `移到废纸篓（${items.length} 条）`}</button></div>
    </section>
  </div>
}
