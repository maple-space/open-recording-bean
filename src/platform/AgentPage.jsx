import { useEffect, useState } from 'react'
import { Bot, Check, Copy } from 'lucide-react'
import { api } from './api.js'
import { agentSnippets, agentTools, startScript } from './agentConfig.js'
import { countText } from './liveInput.js'
import './live.css'

const clients = [
  ['claude', 'Claude Code', '在终端里运行一次：'],
  ['codex', 'Codex', '加到 ~/.codex/config.toml 的末尾：'],
  ['json', '其他客户端', 'Claude 桌面版、Cursor 等，在它的 MCP 配置里加：'],
]
const questions = [
  '我今天说了什么？整理成要点。',
  '把今天下午 2 点到 3 点的讨论整理成会议纪要，按说话人分开。',
  '上周有没有提到“报价”？是哪天、谁说的？',
  '刚才那几分钟说了什么？',
]

// How to let an agent read the saved text through the read-only MCP window (./start.sh mcp).
export function AgentPage({ records }) {
  const [info, setInfo] = useState(null)              // null: asking; false: this service cannot say
  const [history, setHistory] = useState(null)
  const [client, setClient] = useState('claude')
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    api('/api/mcp').then(setInfo).catch(() => setInfo(false))
    api('/api/live/transcripts').then(setHistory).catch(() => setHistory({ sessions: [] }))
  }, [])

  const script = startScript(info, history?.directory)
  const snippet = agentSnippets(script)[client]
  const sessions = history?.sessions || []
  const sentences = sessions.reduce((sum, row) => sum + (row.sentences || 0), 0)
  const withText = records.filter((record) => record.transcript?.trim()).length
  const copy = async () => {
    try { await navigator.clipboard.writeText(snippet); setCopied(true); setTimeout(() => setCopied(false), 2000) } catch { /* select and copy by hand */ }
  }

  return <>
    <div className="page-heading"><div><span className="eyebrow">MCP</span><h1>给 Agent 用</h1>
      <p>让 Claude Code、Codex、Cursor 这类 Agent 直接读这台电脑上保存的文字：实时输入的文字记录，和资料库里已经转成文字的录音。<br />只读。不能改，不能删，也不会去连录音豆。</p></div><Bot size={46} strokeWidth={1.2} /></div>

    <dl className="agent-facts" aria-label="现在 Agent 能读到的内容">
      <div><dt>实时文字记录</dt><dd>{history ? `${countText(sessions.length)} 份 · ${countText(sentences)} 句` : '正在读取…'}</dd></div>
      <div><dt>有文字的录音</dt><dd>{countText(withText)} 条</dd></div>
      <div><dt>权限</dt><dd>只读</dd></div>
    </dl>

    <article className="live-step">
      <header><b>1</b><div><h3>把它加到 Agent 里</h3><p>选你用的 Agent，复制下面的内容。每个 Agent 只需要做一次；不加，这个功能就不存在。</p></div></header>
      <div className="live-tabs" role="tablist">
        {clients.map(([key, label]) => <button key={key} type="button" role="tab" aria-selected={client === key} className={client === key ? 'active' : ''} onClick={() => { setClient(key); setCopied(false) }}>{label}</button>)}
      </div>
      <div className="agent-snippet">
        <p>{clients.find(([key]) => key === client)[2]}</p>
        <div className="agent-code"><pre><code>{snippet}</code></pre>
          <button type="button" className="p-button compact" onClick={copy}>{copied ? <Check size={13} /> : <Copy size={13} />}{copied ? '已复制' : '复制'}</button></div>
        {info !== null && !script.known && <p className="inline-help">请把 /你的路径 换成这个项目在你电脑上的位置。</p>}
        {info && info.installed === false && <p className="inline-help">MCP 组件还没有装好。在项目文件夹里运行一次 ./start.sh，会自动装上。</p>}
      </div>
    </article>

    <article className="live-step">
      <header><b>2</b><div><h3>然后直接问它</h3><p>读哪一天、哪一段，由 Agent 自己决定。比如：</p></div></header>
      <ul className="agent-questions">{questions.map((text) => <li key={text}>{text}</li>)}</ul>
    </article>

    <section className="agent-tools">
      <div className="panel-heading"><div><h2>Agent 能用的六个工具</h2><p>全部只读。内容多的时候 Agent 会分几次读。</p></div></div>
      <table><tbody>{agentTools.map(([name, text]) => <tr key={name}><th scope="row"><code>{name}</code></th><td>{text}</td></tr>)}</tbody></table>
    </section>

    <div className="live-facts">
      <h3>需要知道的几件事</h3>
      <ul>
        <li>Agent 读到的文字会交给它背后的模型处理。用云端模型的 Agent，这些文字就会发到那个服务。</li>
        <li>说话人编号只表示嗓音不同，不是姓名，而且每次开启实时输入都重新编号。Agent 会被告知这一点。</li>
        <li>它直接读保存下来的文件，这个网页不开着也能用。</li>
        <li>想收回权限，在 Agent 里把 open-recording-bean 这一项删掉就行。</li>
      </ul>
    </div>
  </>
}
