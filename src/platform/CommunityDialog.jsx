import { useEffect, useRef, useState } from 'react'
import { BookOpen, Bug, ExternalLink, MessagesSquare, X } from 'lucide-react'
import { project, repoUrl } from './project.js'

// The user group: a picture to scan, and where else to ask. The picture is a file of this project
// (public/community/wechat-qr.png), so nothing is fetched from the network to show it.
export function CommunityDialog({ onClose }) {
  const [missing, setMissing] = useState(false)
  const dialog = useRef(null)
  const close = useRef(null)
  const home = repoUrl()
  useEffect(() => {
    const previous = document.activeElement
    close.current?.focus()
    return () => { if (previous?.isConnected) previous.focus() }
  }, [])
  function handleKey(event) {
    if (event.key === 'Escape') onClose()
    if (event.key !== 'Tab') return
    const fields = [...dialog.current.querySelectorAll('button, a[href]')]
    if (event.shiftKey && document.activeElement === fields[0]) { event.preventDefault(); fields.at(-1).focus() }
    else if (!event.shiftKey && document.activeElement === fields.at(-1)) { event.preventDefault(); fields[0].focus() }
  }
  return <div className="dialog-backdrop" onKeyDown={handleKey} onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
    <section ref={dialog} className="confirm-dialog community-dialog" role="dialog" aria-modal="true" aria-labelledby="community-title">
      <button ref={close} type="button" className="p-icon-button community-close" aria-label="关闭" onClick={onClose}><X size={17} /></button>
      <MessagesSquare size={25} />
      <h2 id="community-title">加入用户群</h2>
      <p>用法交流、问题反馈、新功能试用都在微信群里。用微信扫下面的二维码。{project.wechatNote}</p>
      {missing
        ? <p className="community-missing">二维码图片还没有放进来。{home ? '请到项目主页查看。' : ''}</p>
        : <img className="community-qr" src={project.wechatQr} alt="用户群的微信二维码" onError={() => setMissing(true)} />}
      <p className="hint">扫不了或者提示过期？{home ? <a className="text-link" href={`${home}#社群`} target="_blank" rel="noreferrer">到项目主页看最新的二维码 <ExternalLink size={12} /></a> : '项目主页上的二维码是最新的。'}</p>
      {home && <ul className="community-links">
        <li><a href={`${home}/issues`} target="_blank" rel="noreferrer"><Bug size={15} /><span><strong>报告问题</strong><small>哪一步不成功、页面上的提示是什么，写在 GitHub Issues 里</small></span><ExternalLink size={13} /></a></li>
        <li><a href={`${home}#readme`} target="_blank" rel="noreferrer"><BookOpen size={15} /><span><strong>使用说明</strong><small>安装、输入法设置、常见问题</small></span><ExternalLink size={13} /></a></li>
      </ul>}
    </section>
  </div>
}
