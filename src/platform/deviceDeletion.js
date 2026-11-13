export const DEVICE_DELETE_PHRASE = '确认从设备永久删除'
export const DEVICE_BACKUP_DELETE_PHRASE = '确认备份本地后删除'

export function deviceDeletePhrase(mode) {
  if (mode === 'backup') return DEVICE_BACKUP_DELETE_PHRASE
  if (mode === 'direct') return DEVICE_DELETE_PHRASE
  throw new Error('请选择删除方式。')
}

// Keep the confirmation guard in the action, not only in the disabled button.
// Direct mode deliberately never calls archive, flush or verifyBackup.
export async function deleteDeviceFiles(request, { mode, phrase }, actions) {
  const requiredPhrase = deviceDeletePhrase(mode)
  if (phrase !== requiredPhrase) throw new Error(`请输入“${requiredPhrase}”确认。`)
  const files = request.files?.map((file) => ({ ...file })) || []
  if (request.type !== 'device' || !request.deviceId || !files.length) throw new Error('请重新选择设备录音。')
  const ids = files.map((file) => Number(file.original_id))
  if (ids.some((id) => !Number.isSafeInteger(id) || id <= 0 || id > 0xffffffff) || new Set(ids).size !== ids.length) {
    throw new Error('设备录音标识无效，请刷新后重新选择。')
  }
  const ensureDevice = () => {
    const snapshot = actions.getSnapshot()
    if (!snapshot?.connected || snapshot.device_id !== request.deviceId) throw new Error('设备连接已变化，请重新选择录音。')
  }
  let count = 0
  try {
    ensureDevice()
    const backups = []
    if (mode === 'backup') {
      for (const [index, file] of files.entries()) {
        ensureDevice()
        actions.onProgress?.(`备份并核对录音 ${index + 1}/${files.length}`)
        const record = await actions.archive(file, request.deviceId)
        await actions.flush(record)
        await actions.verifyBackup(record, request.deviceId, file)
        backups.push(record)
      }
    }
    for (const [index, file] of files.entries()) {
      ensureDevice()
      actions.onProgress?.(`删除设备录音 ${index + 1}/${files.length}`)
      if (mode === 'backup') await actions.verifyBackup(backups[index], request.deviceId, file)
      ensureDevice()
      await actions.remove(ids[index])
      count += 1
    }
    return count
  } catch (error) {
    throw new Error(`${count ? `已完成 ${count} 条；` : ''}${error.message || String(error)}`)
  }
}
