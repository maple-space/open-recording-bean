// What has to be typed before recordings are deleted from the recorder. The page only shows the
// words and waits for them; the local service asks for the same words again and does the backup,
// the checks and the deletion itself (backend/feishu_jobs.py).
export const DEVICE_DELETE_PHRASE = '确认从设备永久删除'
export const DEVICE_BACKUP_DELETE_PHRASE = '确认备份本地后删除'

export function deviceDeletePhrase(mode) {
  if (mode === 'backup') return DEVICE_BACKUP_DELETE_PHRASE
  if (mode === 'direct') return DEVICE_DELETE_PHRASE
  throw new Error('请选择删除方式。')
}
