import { useCallback, useEffect, useState } from 'react'
import { ArrowRight, Bluetooth, Check, FileAudio, FileText, FolderOpen, LoaderCircle, MessagesSquare, RefreshCw } from 'lucide-react'
import { api, deviceNames, formatDate } from './api.js'
import { HardwareMark } from './DevicePanel.jsx'
import { Link } from './Link.jsx'

const stateText = { done: '已完成', todo: '待完成', checking: '正在检测' }

function Step({ number, state, title, children }) {
  return <li className={`home-step ${state}`}>
    <span className="home-step-mark" aria-hidden="true">{state === 'done' ? <Check size={15} /> : number}</span>
    <div><h3>{title}<small>{stateText[state]}</small></h3>{children}</div>
  </li>
}

// The first page: what this is, and the four things that must be in place before the recorder
// works as a microphone, each checked against what this computer actually has.
export function HomePage({ device, records, online, navigate, onOpen, onCommunity }) {
  const [outputs, setOutputs] = useState(undefined)     // undefined: not asked yet; null: the service did not answer
  const [engine, setEngine] = useState(undefined)
  const [checking, setChecking] = useState(false)

  const check = useCallback(async () => {
    setChecking(true)
    const [cards, speech] = await Promise.allSettled([api('/api/live/outputs'), api('/api/realtime/status')])
    setOutputs(cards.status === 'fulfilled' ? cards.value : null)
    setEngine(speech.status === 'fulfilled' ? speech.value : null)
    setChecking(false)
  }, [])

  useEffect(() => { if (online !== false) check() }, [check, online])

  const stateOf = (done, known) => (done ? 'done' : known ? 'todo' : 'checking')
  const connected = Boolean(device?.connected)
  const battery = device?.info?.battery
  const card = outputs?.suggested || ''
  const engineLabel = engine?.engines?.find((row) => row.id === engine.default_model)?.label
  const speakers = Boolean(engine?.speakers?.available)
  const recommended = engine?.default_model === 'sensevoice-small'     // not a slower fallback model
  const live = Boolean(device?.live)

  return <>
    <section className="home-hero">
      <div><span className="eyebrow">非官方开源工具 · 适用于 soundcore Work / 飞书录音豆（D3200）</span>
        <h1>把录音豆变成电脑的麦克风</h1>
        <p>说话的声音实时送进任何输入法和会议软件，同时在这台电脑上变成带说话人的文字。录音豆里的录音也可以下载到本机播放、转写和整理。声音和文字都不经过云端。</p>
      </div>
      <HardwareMark />
    </section>

    <section aria-labelledby="home-steps-title">
      <div className="panel-heading"><div><h2 id="home-steps-title">四步用起来</h2><p>前三步每台电脑只做一次。下面的状态是这台电脑现在的实际情况。</p></div>
        <button type="button" className="p-button compact" onClick={check} disabled={checking || online === false}>{checking ? <LoaderCircle className="p-spin" size={13} /> : <RefreshCw size={13} />}重新检测</button></div>
      <ol className="home-steps">
        <Step number={1} state={stateOf(connected || live, online !== null)} title="连接录音豆">
          <p>{connected || live ? `已连接：${device.device_name || deviceNames.feishu}${battery != null ? `，电量 ${battery}%` : ''}。`
            : device?.connection_status === 'reconnecting' ? '连接中断了，本机服务正在自动重连。'
              : '把录音豆放在电脑旁边，在手机上退出飞书或 soundcore App，然后搜索并连接。'}</p>
          <Link href="/device" navigate={navigate} className="text-link">{connected || live ? '查看录音豆' : '去连接'} <ArrowRight size={14} /></Link>
        </Step>
        <Step number={2} state={stateOf(Boolean(card), outputs !== undefined)} title="装一块虚拟声卡">
          <p>{card ? `已装好：${card}。输入法会把它当成麦克风。`
            : outputs && outputs.available === false ? outputs.message
              : '输入法只能从声音设备里选麦克风，所以需要它来转接。免费的 BlackHole 一条命令就能装好。只想看文字的话可以跳过这一步。'}</p>
          {!card && <Link href="/live#live-guide" navigate={navigate} className="text-link">看安装方法 <ArrowRight size={14} /></Link>}
        </Step>
        <Step number={3} state={stateOf(Boolean(engine?.ready), engine !== undefined)} title="准备本机识别模型">
          {engine?.ready
            ? <p>{engineLabel || '本机模型'} 已就绪，识别时不联网。{recommended && speakers ? '区分说话人的模型也在。'
              : <>{recommended ? '区分说话人的模型还没有装' : '推荐换成更快、中文更准的 SenseVoice Small'}，运行 <code>./start.sh models</code> 可以补上。</>}</p>
            : <p>实时文字由这台电脑上的模型识别。在终端进入项目文件夹运行 <code>./start.sh models</code>，下载约 270 MB，完成后回来点“重新检测”。</p>}
        </Step>
        <Step number={4} state={stateOf(live, online !== null)} title="开启实时输入">
          <p>{live ? '正在传声。把输入法的麦克风选成虚拟声卡，就能对着录音豆打字。' : '在“实时输入”页面点一下开启，对着录音豆说话，文字会出现在页面上。页面下方有输入法的设置教程。'}</p>
          <Link href="/live" navigate={navigate} className="text-link">{live ? '查看实时文字' : '去开启'} <ArrowRight size={14} /></Link>
        </Step>
      </ol>
    </section>

    <section className="home-more" aria-label="还能做什么">
      <Link href="/live/records" navigate={navigate}><FileText size={20} /><span><strong>文字记录</strong><small>每次实时输入的文字单独存成一个文件，句数没有上限，可以按时间翻看和搜索。</small></span></Link>
      <Link href="/device" navigate={navigate}><Bluetooth size={20} /><span><strong>录音豆里的录音</strong><small>下载到本机，播放、转成文字、整理成纪要，也可以从录音豆上删除。</small></span></Link>
      <Link href="/library" navigate={navigate}><FolderOpen size={20} /><span><strong>本机资料库</strong><small>下载过的录音和导入的音频都在这里，设备断开也能用。</small></span></Link>
    </section>

    <section className="home-community">
      <MessagesSquare size={26} />
      <div><strong>遇到问题，或者想交流用法？</strong><p>扫码加入用户群。哪一步没成功，把页面上的提示发到群里就行。</p></div>
      <button type="button" className="p-button" onClick={onCommunity}>加入用户群</button>
    </section>

    <section className="recent-panel"><div className="panel-heading"><div><h2>最近归档</h2><p>设备断开，已下载的录音依然可用。</p></div><Link href="/library" navigate={navigate} className="text-link">查看资料库 <ArrowRight size={15} /></Link></div>
      {records.length
        ? <div className="recent-records">{records.slice(0, 4).map((record) => <button key={record.id} onClick={() => onOpen(record.id)}><span className="audio-symbol"><FileAudio size={21} /></span><div><strong>{record.title}</strong><small>{deviceNames[record.device_type] || record.device_type} · {formatDate(record.recorded_at)}</small></div><span className="recent-status">{record.summary ? '已整理' : record.transcript ? '已转录' : '已归档'}</span><ArrowRight size={16} /></button>)}</div>
        : <div className="overview-empty"><FolderOpen size={28} /><div><strong>资料库已准备好</strong><p>连接录音豆下载录音，或导入现有音频开始整理。</p></div><Link href="/library" navigate={navigate} className="p-button">导入音频<ArrowRight size={14} /></Link></div>}
    </section>
  </>
}
