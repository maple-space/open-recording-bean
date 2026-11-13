import test from 'node:test'
import assert from 'node:assert/strict'
import { PAGE_ROWS, applyTranscript, countText, dayClockText, durationText, elapsedText, groupDays, hourAsText, hourHolding, hourText, levelPercent, markParts, needsSpace, paragraphsOf, pickOutput, recordTitle, searchTerms, speakerName, speakerTone, statusView, storageText } from './liveInput.js'
import { initialSettings, localModelChoices, localModelKey, localModelName } from './api.js'

const row = (index) => ({ index, at: 1794514851 + index, text: `第 ${index} 句` })

test('new sentences are appended once, in order', () => {
  let state = { segments: [], cursor: 0 }
  state = applyTranscript(state, { segments: [row(0), row(1)], next_cursor: 2 })
  assert.deepEqual(state.segments.map((item) => item.index), [0, 1])
  // The same rows arriving again (a poll that raced a previous one) are not duplicated.
  state = applyTranscript(state, { segments: [row(1), row(2)], next_cursor: 3 })
  assert.deepEqual(state.segments.map((item) => item.index), [0, 1, 2])
  assert.equal(state.cursor, 3)
})

test('an unchanged transcript keeps the same object, so the page does not re-render', () => {
  const state = { segments: [row(0)], cursor: 1 }
  assert.equal(applyTranscript(state, { segments: [], next_cursor: 1 }), state)
  assert.equal(applyTranscript(state, null), state)
})

test('a restarted service is noticed and the transcript starts over', () => {
  const state = { segments: [row(0), row(1), row(2)], cursor: 3 }
  assert.deepEqual(applyTranscript(state, { segments: [], next_cursor: 1 }), { segments: [], cursor: 0, session: null })
})

test('switching realtime input on again starts a new record on the page', () => {
  const first = { started: 1794573202.5 }
  let state = applyTranscript({ segments: [], cursor: 0 }, { segments: [row(0), row(1)], next_cursor: 2, session: first })
  assert.equal(state.session, first.started)
  state = applyTranscript(state, { segments: [row(2)], next_cursor: 3, session: first })
  assert.deepEqual(state.segments.map((item) => item.index), [0, 1, 2])
  // Switched off: still the same record, the text stays.
  assert.equal(applyTranscript(state, { segments: [], next_cursor: 3, session: first }), state)
  // Switched on again. A slow tab may first hear of it when new sentences already exist: drop the old
  // rows and ask from the start, rather than gluing the two records together.
  const second = { started: 1794576900.1 }
  const restarted = applyTranscript(state, { segments: [row(3), row(4)], next_cursor: 5, session: second })
  assert.deepEqual(restarted, { segments: [], cursor: 0, session: second.started })
  const filled = applyTranscript(restarted, { segments: [row(3), row(4)], next_cursor: 5, session: second })
  assert.deepEqual(filled.segments.map((item) => item.index), [3, 4])
})

test('the page keeps only the newest sentences, however long the session runs', () => {
  let state = { segments: [], cursor: 0, session: 1 }
  const many = Array.from({ length: PAGE_ROWS + 30 }, (_, index) => row(index))
  state = applyTranscript(state, { segments: many, next_cursor: many.length, session: { started: 1 } })
  assert.equal(state.segments.length, PAGE_ROWS)
  assert.equal(state.segments[0].index, 30)
  state = applyTranscript(state, { segments: [row(many.length)], next_cursor: many.length + 1, session: { started: 1 } })
  assert.equal(state.segments.length, PAGE_ROWS)
  assert.equal(state.segments.at(-1).index, many.length)
  assert.equal(state.cursor, many.length + 1)
})

test('clearing on the server moves the cursor without bringing old rows back', () => {
  const cleared = { segments: [], cursor: 3 }
  const state = applyTranscript(cleared, { segments: [row(3)], next_cursor: 4 })
  assert.deepEqual(state.segments.map((item) => item.index), [3])
})

test('the output choice is remembered, otherwise a virtual sound card, never the speakers', () => {
  const outputs = { suggested: 'BlackHole 2ch', devices: [{ name: 'BlackHole 2ch' }, { name: 'MacBook Pro扬声器' }] }
  assert.equal(pickOutput(outputs, 'MacBook Pro扬声器'), 'MacBook Pro扬声器')
  assert.equal(pickOutput(outputs, 'Soundflower (2ch)'), 'BlackHole 2ch')   // the remembered device is gone
  assert.equal(pickOutput({ suggested: null, devices: [{ name: 'MacBook Pro扬声器' }] }, ''), '')
  assert.equal(pickOutput(null, 'x'), '')
})

test('status wording follows the service state and carries the reason while retrying', () => {
  assert.equal(statusView(null).tone, 'idle')
  assert.deepEqual(statusView({ status: 'streaming' }), { tone: 'on', text: '正在传声' })
  assert.match(statusView({ status: 'retrying', error: '蓝牙连接已中断，实时音频已停止。' }).text, /自动重试…（蓝牙连接已中断/)
  assert.equal(statusView({ status: 'something-new' }).text, '未开启')
})

test('durations, storage and level are shown in everyday units', () => {
  assert.equal(elapsedText(0), '0:00:00')
  assert.equal(elapsedText(3 * 3600 + 5 * 60 + 9.8), '3:05:09')
  assert.equal(storageText(7434240), '7.1 GB')
  assert.equal(storageText(512 * 1024), '512 MB')
  assert.equal(storageText(undefined), '未知')
  assert.equal(levelPercent(0), 0)
  assert.equal(levelPercent(0.04), 20)
  assert.equal(levelPercent(4), 100)
  assert.equal(countText(1403520), '1,403,520')
  assert.equal(countText(undefined), '0')
  assert.equal(durationText(20), '不到 1 分钟')
  assert.equal(durationText(59 * 60 + 59), '59 分钟')
  assert.equal(durationText(2 * 3600), '2 小时')
  assert.equal(durationText(96 * 3600 + 5 * 60), '96 小时 5 分')
  assert.equal(dayClockText(new Date(2026, 10, 13, 20, 33, 22).getTime() / 1000), '2026-11-13 20:33')
  assert.equal(dayClockText('x'), '')
})

test('local speech models are shown by their full names, the repository names, and Fun-ASR-Nano is the default', () => {
  assert.equal(initialSettings.asr_model, 'sensevoice-small')
  assert.equal(localModelName('sensevoice-small'), 'SenseVoiceSmall')
  assert.equal(localModelName('funasr-nano'), 'Fun-ASR-Nano-GGUF')
  assert.equal(localModelName(''), 'SenseVoiceSmall')   // nothing chosen: the model that is installed by default
  assert.equal(localModelName('Nano'), 'Fun-ASR-Nano-GGUF')
  assert.equal(localModelName('small'), 'faster-whisper-small')
  assert.equal(localModelName('large-v3'), 'faster-whisper-large-v3')
  assert.equal(localModelName('turbo'), 'turbo')                              // its repository is not a regular one: no guess
  assert.equal(localModelName('Systran/faster-whisper-medium'), 'faster-whisper-medium')
  assert.equal(localModelName('/models/whisper-ct2'), '/models/whisper-ct2')  // a directory is shown as typed
  assert.equal(localModelName('~/models/whisper-ct2'), '~/models/whisper-ct2')
  assert.equal(localModelName('C:\\models\\whisper'), 'C:\\models\\whisper')
  // What the local service reports wins: it knows the exact repository.
  const names = { turbo: { name: 'faster-whisper-large-v3-turbo' }, 'funasr-nano': { name: '服务给出的全称' } }
  assert.equal(localModelName('turbo', names), 'faster-whisper-large-v3-turbo')
  assert.equal(localModelName('nano', names), '服务给出的全称')
  assert.equal(localModelName('small', names), 'faster-whisper-small')
})

test('the settings dropdown offers what this computer has, and never hides the saved choice', () => {
  assert.equal(localModelKey(' Fun-ASR-Nano '), 'funasr-nano')
  assert.equal(localModelKey(''), 'sensevoice-small')
  assert.equal(localModelKey(' SenseVoice '), 'sensevoice-small')
  assert.equal(localModelKey('small'), 'small')
  assert.deepEqual(localModelChoices(['funasr-nano', 'small'], 'funasr-nano'), ['funasr-nano', 'small'])
  assert.deepEqual(localModelChoices(['funasr-nano', 'small'], 'nano'), ['funasr-nano', 'small'])
  assert.deepEqual(localModelChoices(['funasr-nano', 'small'], 'medium'), ['funasr-nano', 'small', 'medium'])
  assert.deepEqual(localModelChoices(['small'], '/models/whisper-ct2'), ['small', '/models/whisper-ct2'])
  assert.deepEqual(localModelChoices(undefined, 'funasr-nano'), ['funasr-nano'])      // before the service has answered
  assert.deepEqual(localModelChoices([], ''), [])
})

const said = (index, day, time, text) => ({ index, day, time, text })

test('a record is titled by when it began, and its hours are grouped by day', () => {
  assert.equal(recordTitle('2026-11-13_20-46-57.txt'), '2026-11-13 20:46')
  assert.equal(recordTitle('2026-11-13_20-46-57-2.txt'), '2026-11-13 20:46')
  assert.equal(recordTitle('notes.txt'), 'notes.txt')
  assert.equal(hourText(7), '07:00')
  const hours = [
    { day: '2026-11-13', hour: 22, first: 0, sentences: 2, characters: 30 },
    { day: '2026-11-13', hour: 23, first: 2, sentences: 60, characters: 900 },
    { day: '2026-11-14', hour: 0, first: 62, sentences: 60, characters: 800 },
  ]
  const days = groupDays(hours)
  assert.deepEqual(days.map((day) => [day.day, day.hours.map((hour) => [hour.at, hour.hour])]), [
    ['2026-11-13', [[0, 22], [1, 23]]], ['2026-11-14', [[2, 0]]]])
  assert.deepEqual(groupDays(undefined), [])
  assert.equal(hourHolding(hours, 0), 0)
  assert.equal(hourHolding(hours, 1), 0)
  assert.equal(hourHolding(hours, 2), 1)
  assert.equal(hourHolding(hours, 61), 1)
  assert.equal(hourHolding(hours, 62), 2)
  assert.equal(hourHolding(hours, 9999), 2)
  assert.equal(hourHolding([], 5), 0)
})

test('sentences of the same minute read as one paragraph, and copy the way they are shown', () => {
  const lines = [
    said(10, '2026-11-13', '20:35:02', '今天下午三点我们开个会，'),
    said(11, '2026-11-13', '20:35:40', '讨论一下 release plan.'),
    said(12, '2026-11-13', '20:35:55', '然后重新跑一下测试。'),
    said(13, '2026-11-13', '20:36:01', '下一分钟的话。'),
    said(14, '2026-11-14', '20:36:30', '第二天同一分钟的话。'),
  ]
  const paragraphs = paragraphsOf(lines)
  assert.deepEqual(paragraphs.map((paragraph) => [paragraph.minute, paragraph.lines.map((line) => line.index)]), [
    ['20:35', [10, 11, 12]], ['20:36', [13]], ['20:36', [14]]])
  assert.equal(new Set(paragraphs.map((paragraph) => paragraph.key)).size, 3)
  assert.equal(needsSpace('release plan.'), true)
  assert.equal(needsSpace('开个会，'), false)
  assert.equal(hourAsText(lines.slice(0, 4), 'paragraphs'), '20:35  今天下午三点我们开个会，讨论一下 release plan. 然后重新跑一下测试。\n\n20:36  下一分钟的话。')
  assert.equal(hourAsText(lines.slice(0, 2), 'sentences'), '[20:35:02] 今天下午三点我们开个会，\n[20:35:40] 讨论一下 release plan.')
  assert.deepEqual(paragraphsOf(null), [])
})

test('search words are marked wherever they occur, whatever their case', () => {
  assert.deepEqual(searchTerms('  蓝牙   Bluetooth '), ['蓝牙', 'Bluetooth'])
  assert.deepEqual(searchTerms(''), [])
  assert.deepEqual(markParts('先做一次蓝牙抓包，Bluetooth 的蓝牙。', ['蓝牙', 'bluetooth']), [
    { text: '先做一次', hit: false }, { text: '蓝牙', hit: true }, { text: '抓包，', hit: false },
    { text: 'Bluetooth', hit: true }, { text: ' 的', hit: false }, { text: '蓝牙', hit: true }, { text: '。', hit: false }])
  assert.deepEqual(markParts('发布计划', ['发布', '发布计划']), [{ text: '发布计划', hit: true }])     // the longer word wins
  assert.deepEqual(markParts('没有命中', ['别的']), [{ text: '没有命中', hit: false }])
  assert.deepEqual(markParts('原样返回', []), [{ text: '原样返回', hit: false }])
  assert.deepEqual(markParts('', ['x']), [{ text: '', hit: false }])
})

test('speaker numbers get a name and one of eight colours, and split paragraphs when the speaker changes', () => {
  assert.equal(speakerName(3), '说话人3')
  assert.equal(speakerName(null), '')
  assert.equal(speakerName(undefined), '')
  assert.deepEqual([1, 2, 8, 9, 17, null, 0].map(speakerTone), [1, 2, 8, 1, 1, 0, 0])
  const lines = [
    { index: 0, day: '2026-11-13', time: '23:10:05', text: '我们先看一下进度。', speaker: 1 },
    { index: 1, day: '2026-11-13', time: '23:10:09', text: '后端已经好了。', speaker: 1 },
    { index: 2, day: '2026-11-13', time: '23:10:15', text: '好的，没问题。', speaker: 2 },
    { index: 3, day: '2026-11-13', time: '23:10:19', text: '嗯。', speaker: null },
    { index: 4, day: '2026-11-13', time: '23:10:30', text: '那就这么定了。', speaker: 1 },
    { index: 5, day: '2026-11-13', time: '23:11:02', text: '下一分钟。', speaker: 1 },
  ]
  assert.deepEqual(paragraphsOf(lines).map((paragraph) => [paragraph.minute, paragraph.speaker, paragraph.lines.map((line) => line.index)]), [
    ['23:10', 1, [0, 1]], ['23:10', 2, [2]], ['23:10', null, [3]], ['23:10', 1, [4]], ['23:11', 1, [5]]])
  assert.equal(hourAsText(lines.slice(0, 4), 'paragraphs'), '23:10  说话人1：我们先看一下进度。后端已经好了。\n\n23:10  说话人2：好的，没问题。\n\n23:10  嗯。')
  assert.equal(hourAsText(lines.slice(2, 4), 'sentences'), '[23:10:15] 说话人2：好的，没问题。\n[23:10:19] 嗯。')
  // Records made before speakers were told apart have no speaker field at all.
  assert.deepEqual(paragraphsOf([{ index: 0, day: 'd', time: '10:00:01', text: 'a' }, { index: 1, day: 'd', time: '10:00:09', text: 'b' }]).map((p) => p.lines.length), [2])
})
