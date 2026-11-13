// Everything that names this project or points at it lives here, so a fork changes one file.
export const project = {
  name: 'open-recording-bean',
  title: '开源录音豆',
  repo: 'https://github.com/maple-space/open-recording-bean',
  // The author's own WeChat code, shown in the app because it does not expire (a group code lasts 7 days
  // and is kept on the project page instead). The picture is public/community/wechat-personal.png.
  wechatQr: '/community/wechat-personal.png',
  wechatNote: '加好友时请备注“录音豆”，我拉你进用户群。',
}

export function repoUrl(path = '') {
  return project.repo + path
}
