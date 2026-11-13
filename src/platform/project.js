// Everything that names this project or points at it lives here, so a fork changes one file.
export const project = {
  name: 'open-recording-bean',
  title: '开源录音豆',
  repo: 'https://github.com/maple-space/open-recording-bean',
  // The picture people scan to join the user group. Put it at public/community/wechat-qr.png and build again.
  wechatQr: '/community/wechat-qr.png',
  wechatNote: '群二维码 7 天内有效，过期后项目主页上会更新。',
}

export function repoUrl(path = '') {
  return project.repo + path
}
