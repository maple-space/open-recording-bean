import test from 'node:test'
import assert from 'node:assert/strict'
import { fetchRecordDownload } from './downloads.js'

const record = { id: 'a'.repeat(32), title: '会议录音.ogg', size_bytes: 12 }
function response(body = new Uint8Array(12), type = 'audio/ogg', options = {}) {
  return new Response(body, { status: 200, ...options, headers: { 'Content-Type': type, ...options.headers } })
}
function download(value, entry = record, options = {}) {
  return fetchRecordDownload(entry, { fetchImpl: async () => value, ...options })
}

test('audio is fetched from a fixed same-origin URL with credentials and no cache', async () => {
  const result = await fetchRecordDownload({ ...record, audio_url: 'https://example.com/untrusted' }, {
    fetchImpl: async (url, options) => {
      assert.equal(url, `/api/records/${record.id}/audio`)
      assert.deepEqual(options, { cache: 'no-store', credentials: 'same-origin' })
      return response()
    },
  })
  assert.equal(result.blob.size, 12)
  assert.equal(result.filename, '会议录音.ogg')
})

test('invalid record IDs and download kinds are rejected before making a request', async () => {
  const fetchImpl = () => assert.fail('invalid request must not be sent')
  for (const id of ['', '../settings', 'a/b', 'https://example.com', 'a'.repeat(31), null]) {
    await assert.rejects(fetchRecordDownload({ ...record, id }, { fetchImpl }), /录音标识/)
  }
  await assert.rejects(fetchRecordDownload(record, { kind: 'settings', fetchImpl }), /下载类型/)
})

test('403 JSON is reported as a readable error and never read as a downloadable blob', async () => {
  const denied = response(JSON.stringify({ detail: '拒绝非本地页面访问' }), 'application/json', { status: 403 })
  denied.blob = () => assert.fail('must never download an error body')
  await assert.rejects(download(denied), /403.*拒绝非本地页面访问/)
})

test('error detail is bounded and malformed or non-JSON errors remain readable', async () => {
  await assert.rejects(download(response(JSON.stringify({ detail: 'x'.repeat(1000) }), 'application/json', { status: 500 })), (error) => error.message.length < 400)
  await assert.rejects(download(response('<html>error</html>', 'text/html', { status: 500 })), /下载失败.*500/)
  await assert.rejects(download(response('{broken', 'application/json', { status: 404 })), /下载失败.*404/)
})

test('200 JSON, HTML and unknown binary content are rejected instead of saved as audio', async () => {
  for (const type of ['application/json', 'application/problem+json', 'text/html', 'application/octet-stream']) {
    await assert.rejects(download(response(new Uint8Array(12), type)), /音频格式/)
  }
})

test('partial responses and Content-Range are rejected even when sizes match', async () => {
  await assert.rejects(download(response(undefined, 'audio/ogg', { status: 206 })), /完整文件/)
  await assert.rejects(download(response(undefined, 'audio/ogg', { headers: { 'Content-Range': 'bytes 0-11/12' } })), /完整文件/)
})

test('empty audio and mismatches against transport or archived sizes are rejected', async () => {
  await assert.rejects(download(response(new Uint8Array(0))), /为空/)
  await assert.rejects(download(response(undefined, 'audio/ogg', { headers: { 'Content-Length': '13' } })), /不完整/)
  await assert.rejects(download(response(undefined, 'audio/ogg', { headers: { 'Content-Length': 'bad' } })), /长度/)
  await assert.rejects(download(response(), { ...record, size_bytes: 13 }), /资料库/)
  assert.equal((await download(response(undefined, 'audio/ogg', { headers: { 'Content-Length': '12' } }))).blob.size, 12)
})

test('network errors and interrupted response streams do not return a file', async () => {
  await assert.rejects(fetchRecordDownload(record, { fetchImpl: async () => { throw new TypeError('Failed to fetch') } }), /连接/)
  const stream = new ReadableStream({ start(controller) {
    controller.enqueue(new Uint8Array(3))
    controller.error(new Error('connection lost'))
  } })
  await assert.rejects(download(response(stream)), /中断/)
})

test('actual response MIME determines Get Ogg and Feishu WAV extensions', async () => {
  for (const [type, extension] of [['audio/ogg; codecs=opus', 'ogg'], ['application/ogg', 'ogg'], ['audio/x-wav', 'wav'], ['audio/wav', 'wav'], ['audio/mpeg', 'mp3'], ['audio/mp4', 'm4a'], ['video/mp4', 'mp4']]) {
    const result = await download(response(undefined, type), { ...record, title: '设备录音.ogg' })
    assert.equal(result.filename, `设备录音.${extension}`)
  }
})

test('filenames preserve Chinese, remove unsafe characters and repeated audio extensions', async () => {
  const result = await download(response(), { ...record, title: ' ../客户:会\\议\u0000.OGG.ogg ' })
  assert.equal(result.filename, '_客户_会_议_.ogg')
  assert.equal((await download(response(), { ...record, title: '... ' })).filename, '录音.ogg')
  assert.equal((await download(response(), { ...record, title: undefined })).filename, '录音.ogg')
  assert.ok((await download(response(), { ...record, title: '会'.repeat(200) })).filename.length <= 124)
})

test('bundle requires ZIP MIME and signature and uses its own size rather than audio size', async () => {
  const bytes = new Uint8Array([0x50, 0x4b, 0x03, 0x04, 1])
  const result = await fetchRecordDownload(record, { kind: 'bundle', fetchImpl: async (url) => {
    assert.equal(url, `/api/records/${record.id}/bundle`)
    return response(bytes, 'application/zip', { headers: { 'Content-Length': '5' } })
  } })
  assert.equal(result.filename, '会议录音.zip')
  assert.equal(result.blob.size, 5)
  for (const invalid of [response(bytes, 'application/json'), response(new Uint8Array(5), 'application/zip')]) {
    await assert.rejects(download(invalid, record, { kind: 'bundle' }), /ZIP/)
  }
  await assert.rejects(download(response(bytes, 'application/zip', { headers: { 'Content-Length': '6' } }), record, { kind: 'bundle' }), /不完整/)
})
