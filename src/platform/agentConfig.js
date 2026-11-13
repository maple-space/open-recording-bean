// What to paste into an agent so that it can read the saved text (the read-only MCP window).
// Kept apart from the page so it can be tested without a browser.

const PLACEHOLDER = '/你的路径/open-recording-bean/start.sh'

// Where the start script is. The service says so; an older service that cannot is helped by the folder
// of the text records, which lies inside the project unless the data folder was moved; failing both,
// a placeholder to be replaced by hand.
export function startScript(info, recordsDirectory) {
  if (info && info.command) return { command: info.command, env: info.env || {}, known: true }
  const inside = /^(.+)\/\.local\/data\/live-transcripts\/?$/.exec(recordsDirectory || '')
  if (inside) return { command: `${inside[1]}/start.sh`, env: {}, known: true }
  return { command: PLACEHOLDER, env: {}, known: false }
}

// A path as one shell word: left as it is when plain, otherwise in single quotes.
export function shellWord(text) {
  return /^[A-Za-z0-9_\/.:@%+=,-]+$/.test(text) ? text : `'${String(text).replaceAll("'", "'\\''")}'`
}

export function agentSnippets({ command, env = {} }) {
  const pairs = Object.entries(env)
  const claude = ['claude mcp add --scope user', ...pairs.map(([key, value]) => `-e ${key}=${shellWord(value)}`),
    'open-recording-bean --', shellWord(command), 'mcp'].join(' ')
  const codex = ['[mcp_servers.open-recording-bean]', `command = ${JSON.stringify(command)}`, 'args = ["mcp"]',
    ...(pairs.length ? [`env = { ${pairs.map(([key, value]) => `${key} = ${JSON.stringify(value)}`).join(', ')} }`] : [])].join('\n')
  const server = { command, args: ['mcp'], ...(pairs.length ? { env } : {}) }
  const json = JSON.stringify({ mcpServers: { 'open-recording-bean': server } }, null, 2)
  return { claude, codex, json }
}

export const agentTools = [
  ['list_days', '哪些天有文字，每天多少句'],
  ['read_text', '读某一天或其中一段时间的实时文字，每句带时间和说话人，内容多时分页'],
  ['latest_text', '读最近几分钟，回答“刚才说了什么”'],
  ['search_text', '在实时文字和录音的文字里找词'],
  ['list_recordings', '列出资料库里的录音'],
  ['read_recording', '读一条录音的文字和总结'],
]
