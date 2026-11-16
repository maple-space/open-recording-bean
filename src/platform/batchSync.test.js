import test, { before, after } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

let vite, DevicePanel, JobPanel
before(async () => {
  vite = await createServer({ server: { middlewareMode: true, watch: null }, appType: 'custom' })
  ;({ DevicePanel } = await vite.ssrLoadModule('/src/platform/DevicePanel.jsx'))
  ;({ JobPanel } = await vite.ssrLoadModule('/src/platform/SettingsJobs.jsx'))
})
after(async () => { await vite?.close() })

const device = { connected: true, device_id: 'bean-a', files: [{ original_id: '123' }, { original_id: '124' }] }
const job = { id: 'job', type: 'download', device_type: 'feishu', status: 'failed', created_at: '',
  retry_request: { device_id: 'bean-a', ids: ['123', '124'] }, total: 2, current_index: 2,
  completed_count: 1, completed_ids: ['123'], failed_items: [{ original_id: '124', error: '完整性冲突' }], file_progress: 37 }
const renderJobs = (jobs, overrides = {}) => renderToStaticMarkup(createElement(JobPanel, {
  jobs, device, online: true, onResume() {}, onCancel() {}, ...overrides,
}))

test('device page exposes sync-all without requiring a selection', () => {
  const html = renderToStaticMarkup(createElement(DevicePanel, { snapshot: device, browserState: {}, records: [], settings: {}, online: true }))
  const syncButton = [...html.matchAll(/<button([^>]*)>([\s\S]*?)<\/button>/g)].find((match) => match[2].includes('同步全部录音'))
  assert.ok(syncButton, 'sync-all is visible even with no file selected')
  assert.match(syncButton[1], /class="p-button primary"/)
  assert.doesNotMatch(syncButton[1], /disabled/)
  assert.match(html, /保留设备原件/)
})

test('download does not block disconnect, but an in-flight disconnect or live input does', () => {
  for (const connected of [true, false]) {
    const label = connected ? '断开连接' : '停止重连'
    const props = { snapshot: { ...device, connected, desired_connected: true }, browserState: {}, records: [], settings: {}, online: true, busy: '同步录音到本机' }
    const button = (overrides = {}) => {
      const html = renderToStaticMarkup(createElement(DevicePanel, { ...props, ...overrides }))
      const found = [...html.matchAll(/<button([^>]*)>([\s\S]*?)<\/button>/g)].find((match) => match[2].includes(label))
      assert.ok(found, `${label} stays visible during a download`)
      return found[1]
    }
    assert.doesNotMatch(button(), /disabled/)
    assert.match(button({ disconnecting: true }), /disabled/)
    assert.match(button({ online: false }), /disabled/)
    assert.match(button({ snapshot: { ...props.snapshot, live: true } }), /disabled/)
  }
})

test('only failed or cancelled download requests offer resume', () => {
  assert.match(renderJobs([job]), /继续同步/)
  assert.match(renderJobs([{ ...job, status: 'cancelled' }]), /继续同步/)
  for (const altered of [{ status: 'completed' }, { status: 'running' }, { type: 'device_delete' }, { retry_request: null }]) {
    assert.doesNotMatch(renderJobs([{ ...job, ...altered }]), /继续同步/)
  }
})

test('resume is disabled until the original device is connected and idle', () => {
  for (const overrides of [{ device: { connected: false } }, { device: { ...device, device_id: 'other' } }, { online: false }, { busy: 'working' }, { device: { ...device, live: true } }]) {
    assert.match(renderJobs([job], overrides), /<button[^>]*disabled=""[^>]*>[^<]*继续同步<\/button>/)
  }
})

test('batch job shows current progress and separate completed and failed lists', () => {
  const html = renderJobs([job])
  assert.match(html, /第 2\/2 条/)
  assert.match(html, /当前文件 37%/)
  assert.match(html, /已完成：123/)
  assert.match(html, /124：完整性冲突/)
})
