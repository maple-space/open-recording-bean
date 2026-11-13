import assert from 'node:assert/strict'
import test from 'node:test'
import { agentSnippets, agentTools, shellWord, startScript } from './agentConfig.js'

test('the start script is the one the service names, or is worked out, or is a placeholder', () => {
  assert.deepEqual(startScript({ command: '/opt/bean/start.sh', args: ['mcp'], env: {} }, '/elsewhere/live-transcripts'),
    { command: '/opt/bean/start.sh', env: {}, known: true })
  assert.deepEqual(startScript({ command: '/opt/bean/start.sh', env: { RECORDING_DATA_DIR: '/data' } }, ''),
    { command: '/opt/bean/start.sh', env: { RECORDING_DATA_DIR: '/data' }, known: true })
  // an older service: the text records lie inside the project
  assert.deepEqual(startScript(false, '/Users/someone/work/open-recording-bean/.local/data/live-transcripts'),
    { command: '/Users/someone/work/open-recording-bean/start.sh', env: {}, known: true })
  // a data folder that was moved says nothing about where the project is
  assert.equal(startScript(null, '/Volumes/disk/records/live-transcripts').known, false)
  assert.equal(startScript(null, '').command, '/你的路径/open-recording-bean/start.sh')
})

test('a path is one word for the shell', () => {
  assert.equal(shellWord('/Users/someone/work/open-recording-bean/start.sh'), '/Users/someone/work/open-recording-bean/start.sh')
  assert.equal(shellWord('/Users/some one/My Projects/start.sh'), "'/Users/some one/My Projects/start.sh'")
  assert.equal(shellWord("/it's/start.sh"), "'/it'\\''s/start.sh'")
  assert.equal(shellWord('/你的路径/open-recording-bean/start.sh'), "'/你的路径/open-recording-bean/start.sh'")
})

test('the three ways of adding it name the same command', () => {
  const plain = agentSnippets({ command: '/opt/bean/start.sh' })
  assert.equal(plain.claude, 'claude mcp add --scope user open-recording-bean -- /opt/bean/start.sh mcp')
  assert.equal(plain.codex, '[mcp_servers.open-recording-bean]\ncommand = "/opt/bean/start.sh"\nargs = ["mcp"]')
  assert.deepEqual(JSON.parse(plain.json), { mcpServers: { 'open-recording-bean': { command: '/opt/bean/start.sh', args: ['mcp'] } } })

  const moved = agentSnippets({ command: '/my projects/bean/start.sh', env: { RECORDING_DATA_DIR: '/Volumes/disk/my data' } })
  assert.equal(moved.claude, "claude mcp add --scope user -e RECORDING_DATA_DIR='/Volumes/disk/my data' open-recording-bean -- '/my projects/bean/start.sh' mcp")
  assert.equal(moved.codex.split('\n').at(-1), 'env = { RECORDING_DATA_DIR = "/Volumes/disk/my data" }')
  assert.deepEqual(JSON.parse(moved.json).mcpServers['open-recording-bean'].env, { RECORDING_DATA_DIR: '/Volumes/disk/my data' })
})

test('the page lists the six tools of the server', () => {
  assert.deepEqual(agentTools.map(([name]) => name), ['list_days', 'read_text', 'latest_text', 'search_text', 'list_recordings', 'read_recording'])
})
