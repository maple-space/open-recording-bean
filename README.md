# 开源录音豆 · open-recording-bean

**把录音豆变成电脑的无线麦克风，同时在本机实时转成带说话人的文字。**

适用于 soundcore Work（安克 AI 录音豆、飞书录音豆，型号 D3200，固件版本号支持 04.92，固件升级随时可能让它失效）。
这是非官方的开源独立项目，与 Anker、安克创新、soundcore、飞书无关。[English](#english)

![实时输入页面：录音豆的声音正在变成带说话人的文字](docs/images/live.png)

## 它能做什么

| 功能 | 说明 |
| --- | --- |
| **当电脑的麦克风** | 录音豆的声音经蓝牙实时送到电脑，再送进一块虚拟声卡。豆包输入法、Typeless 这类输入法，以及任何能选择麦克风的软件，把虚拟声卡选成麦克风就能用录音豆说话。 |
| **实时文字** | 说话的同时网页上出字：先出灰色草稿，停顿后定稿。由本机的 SenseVoice Small 模型识别，不联网。 |
| **区分说话人** | 每句话前标“说话人1、说话人2…”。只分辨是不是同一个人，不知道姓名，也不保存声纹。 |
| **文字记录** | 每次开启单独存成一个文本文件，句数没有上限，不会互相覆盖。可以按小时翻看、搜索、复制、下载。 |
| **管理录音** | 读取录音豆里的录音，下载到本机，播放、转成文字、整理成纪要、打包导出，也可以从录音豆上删除。 |
| **给 Agent 读** | 通过 MCP，Claude Code、Codex 这类 Agent 可以按日期读你保存的文字、搜索、读“刚才说了什么”。只读，不配置就没有。 |
| **留在本机** | 声音、文字、录音都存在这台电脑上。只有你自己选择用云端模型整理文字，或者让 Agent 来读时，文字才会发出去。 |

![文字记录：按小时翻看，可以搜索](docs/images/records.png)

## 三步开始

```bash
git clone https://github.com/maple-space/open-recording-bean.git
cd open-recording-bean
./start.sh
```

第一次运行会自动做完这些事，然后打开 <http://127.0.0.1:8875>：

1. 准备项目自己的 Python 环境，安装需要的组件。
2. 构建网页。
3. 下载两个本机识别模型，共约 270 MB（国内网络会自动换用镜像，中断后可以续传）。

之后再运行 `./start.sh` 就是直接启动。运行它的终端窗口不要关，关掉服务就停了。

需要提前装好的只有两样：

- **Node.js 20 或更新**：`brew install node`，或到 [nodejs.org](https://nodejs.org) 下载。
- **Python 3.10 到 3.13**：`brew install python@3.12`，或到 [python.org](https://www.python.org/downloads/) 下载。电脑上有 `uv` 的话可以不装，脚本会用它准备。

打开网页后，首页的“四步用起来”会按这台电脑的实际情况告诉你还差哪一步：

![首页：四步用起来，每一步都显示这台电脑现在的状态](docs/images/home.png)

**哪一步不对？** 运行 `./start.sh doctor`，它会列出这台电脑还缺什么、怎么补。

## 让输入法用录音豆说话

输入法只能从“声音设备”里选麦克风，所以要先装一块虚拟声卡来转接。一共三步，前两步每台电脑只做一次。

1. **装虚拟声卡**。推荐免费开源的 BlackHole：

   ```bash
   brew install blackhole-2ch
   ```

   没有 Homebrew 的话，到 [existential.audio/blackhole](https://existential.audio/blackhole/) 下载 “BlackHole 2ch” 安装包。

2. **把输入法的麦克风改成虚拟声卡**。
   - 豆包输入法：设置 → 语音输入 → 麦克风选择 → 选 “BlackHole 2ch”。
   - Typeless：设置 → 往下找到“音频” → 麦克风 → 选 “BlackHole 2ch”。

3. **在网页上开启实时输入**。打开“实时输入”页面，勾选“送到虚拟声卡”，点“开启实时输入”。状态变成“正在传声”后，把光标放进任意输入框，按输入法的语音快捷键，对着录音豆说话。

带示意图的完整教程在网页“实时输入”页面的下方。

用完以后在网页上点“关闭实时输入”，并把输入法的麦克风改回“自动检测”。

## 给 Agent 用（MCP）

让 Claude Code、Codex、Cursor 这类 Agent 直接读你保存的文字，比如“总结我今天下午说了什么”“上周谁提到过报价”。它是只读的：不能改，不能删，也不会去连录音豆。不配置就等于没有这个功能。

网页左侧的“给 Agent 用”页面里有同样的说明，接入命令已经填好这台电脑上的实际路径，可以直接复制。

在 Claude Code 里加一次就行，把路径换成你电脑上项目所在的位置：

```bash
claude mcp add --scope user open-recording-bean -- /你的路径/open-recording-bean/start.sh mcp
```

Codex 写在 `~/.codex/config.toml` 里：

```toml
[mcp_servers.open-recording-bean]
command = "/你的路径/open-recording-bean/start.sh"
args = ["mcp"]
```

其他客户端（Claude 桌面版、Cursor 等）在它的 MCP 配置里加：

```json
{
  "mcpServers": {
    "open-recording-bean": { "command": "/你的路径/open-recording-bean/start.sh", "args": ["mcp"] }
  }
}
```

Agent 能用六个工具：

| 工具 | 作用 |
| --- | --- |
| `list_days` | 哪些天有文字，每天多少句 |
| `read_text` | 读某一天或其中一段时间的实时文字，每句带时间和说话人，内容多时分页 |
| `latest_text` | 读最近几分钟，回答“刚才说了什么” |
| `search_text` | 在实时文字和录音的文字里找词 |
| `list_recordings` | 列出资料库里的录音 |
| `read_recording` | 读一条录音的文字和总结 |

它直接读 `.local/data` 里的文字记录文件和资料库，本机服务不开也能用。MCP 本身不联网。但 Agent 读到的文字会交给它背后的模型处理，用云端模型的 Agent 就意味着这些文字会发到那个服务。

## 实测情况

下面是实际测过的，没测过的也写明了。

- **测试环境**：MacBook Pro（Intel i9），macOS 15.7；录音豆固件 04.92；虚拟声卡 Soundflower (2ch)。BlackHole 2ch 走的是同一条路径，教程按它写，但还没有在这台机器上实测。
- **输入法**：教程里的界面路径按豆包输入法 1.1.0 和 Typeless 2.11.0 核对过。豆包输入法已实际听写出字。
- **连续运行**：实时传声连续 8 小时 12 分，147 万帧，没有丢帧，没有断连。录音豆每 6 小时自动换一个录音文件，传声不中断。
- **说话人**：用公开语音数据拼成的 2 人和 4 人对话（夹杂一两个字的短句）测试，约 94% 的句子标对。弱点见下面的“已知限制”。
- **还没测过**：Apple 芯片的 Mac（依赖都有对应的安装包，理应可用）；固件 04.92 以外的版本；Windows 和 Linux。欢迎反馈。

## 常见问题

- **搜不到录音豆**：录音豆同一时间只能连一个设备。在手机上退出飞书或 soundcore App（或者关掉手机蓝牙），把录音豆从充电盒里取出或按一下按键，再搜索。
- **第一次搜索没反应**：系统会弹出“终端想要使用蓝牙”，要点允许。点了拒绝的话，到“系统设置 → 隐私与安全性 → 蓝牙”里把终端打开。
- **网页上音量条在动，输入法没有字**：输入法的麦克风没有选成虚拟声卡。
- **模型下载不动**：`HF_ENDPOINT=https://hf-mirror.com ./start.sh models` 指定国内镜像；再次运行会接着上次的进度下载。
- **开着实时输入时不能下载录音**：实时输入期间蓝牙被它独占，下载、刷新、删除要先关闭实时输入。
- **想换个端口，或者换个地方存数据**：`RECORDING_PORT=9000 ./start.sh` 换端口；`RECORDING_DATA_DIR=/你的/文件夹 ./start.sh` 换数据文件夹。

## 工作原理

```
录音豆 ──蓝牙──▶ 本机服务（Python）──▶ 解密 ──▶ Opus 解码 ─┬─▶ 虚拟声卡 ──▶ 输入法 / 会议软件
                     │                                      │
                     │                                      └─▶ 切成句子 ──▶ SenseVoice 出字 ──▶ CAM++ 分说话人
                     ▼                                                              │
              网页（React）◀────────────────── 实时文字、文字记录、录音管理 ◀────────┘
```

本机服务只监听 `127.0.0.1`，别的电脑访问不到。蓝牙连接由服务保持，关掉网页不影响传声和后台任务。

代码在这几个地方：

| 位置 | 内容 |
| --- | --- |
| `backend/feishu_protocol.py`、`backend/feishu.py` | 录音豆的协议：分包、密钥交换、解密、录音列表、下载、删除 |
| `backend/feishu_live.py`、`backend/live.py` | 实时传声、送虚拟声卡、文字记录文件 |
| `backend/realtime_asr.py`、`backend/speakers.py` | 实时识别和区分说话人 |
| `backend/asr.py`、`backend/providers.py`、`backend/workflows.py` | 录音的转写和整理 |
| `backend/mcp_server.py`、`backend/text_records.py` | 给 Agent 用的只读接口，读文字记录文件 |
| `backend/app.py`、`backend/storage.py`、`backend/jobs.py` | 本机服务、资料库、后台任务 |
| `src/platform/` | 网页（其中 `AgentPage.jsx` 是“给 Agent 用”页面） |
| `start.sh`、`scripts/` | 安装启动、下载模型、自检 |

## 隐私

- 声音和文字都在本机处理。实时文字用的两个模型下载后离线运行。
- 录音、文字记录、设置都放在项目文件夹的 `.local/` 里，这个文件夹不会被提交到 Git。
- 区分说话人只在一次实时输入期间在内存里比较声音，不保存声纹，也不知道是谁。
- 用“硅基流动 API”“Codex CLI”或“Claude”整理文字时，相应的文字（选 API 转写时还有音频）会发给你选择的那个服务。API Key 只保留在当前网页的内存里，不写入磁盘。
- 给 Agent 用的 MCP 只有你自己配置以后才会被启动。它只读不写，Agent 读到的文字由它背后的模型处理。
- 项目不收集任何使用数据。

## 已知限制

- 只在 macOS 上验证过。把录音移到废纸篓、在访达中显示这些功能依赖 macOS。
- 录音豆同一时间只能连一个设备：电脑连着的时候，手机上的 App 连不上它。
- 实时输入期间录音豆一直在录音，录音同时存在录音豆里，每小时约 29 MB，用完可以在“录音豆”页面删除。
- 区分说话人的弱点：两个人紧挨着说话时，可能被切进同一句；一个新出现的人如果先说了一两个字的短句，这个短句会被算给前一个人。
- 录音豆到电脑走的是低功耗蓝牙，下载长录音比较慢，需要耐心。本项目还没有实现 Wi‑Fi 传输。
- 不做固件升级、解绑、恢复出厂这类操作，也不会去试探没有验证过的指令。固件更新后协议可能变化，功能随时可能失效。

## 参与贡献

欢迎提交问题和代码。没有录音豆也能开发和测试：测试用的是一台按真机实测行为编写的模拟录音豆。

```bash
./start.sh test     # 后端和前端的全部测试
```

提交代码前请先让测试通过。不要提交录音、文字记录、设备序列号或任何密钥；它们都在 `.local/` 里，这个文件夹已经被 Git 忽略。

## 来源与致谢

- 代码基于 [Shawn-TKD/recording-bean-web](https://github.com/Shawn-TKD/recording-bean-web)（MIT）开发，原版权声明保留在 [LICENSE](LICENSE) 里。
- 协议实现参考了社区研究项目 [tacshi/Soundcore](https://github.com/tacshi/Soundcore)（MIT，Copyright (c) 2026 Rocky Shi），并结合本项目在 D3200（固件 04.92）上的实机抓包与验证重新实现。
- 识别用的是 [SenseVoice](https://github.com/FunAudioLLM/SenseVoice)，区分说话人用的是 [3D-Speaker](https://github.com/modelscope/3D-Speaker) 的 CAM++，两者都通过 [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) 运行。

完整的第三方来源和许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 免责声明

- 本项目是独立的开源项目，与 Anker、安克创新（Anker Innovations）、soundcore、飞书没有任何关联，也没有得到它们的认可或赞助。文中出现的「Anker」「安克」「soundcore Work」「飞书」「D3200」「录音豆」等，仅用于描述本项目与之互操作的硬件，相关名称、商标归各自权利人所有。
- 本项目仅用于与你自己拥有、或已获明确授权管理的设备进行互操作（interoperability），帮助设备所有者在自己的电脑上使用自己的设备，并把自己的录音从自己的硬件上取回。
- 本项目不包含、不分发任何官方固件、私钥、设备凭据、账号信息或官方 App（APK/IPA）。其中的协议实现参考了公开的社区研究（见上面的“来源与致谢”），并在自己拥有的设备上实测验证。
- 本项目不提供服务器、不托管任何人的数据、不代为运营。数据都保存在使用者自己的电脑上；只有使用者自己选择用云端模型整理文字时，相应内容才会发给那个服务。
- 本项目以个人学习、研究与互操作为目的发布。使用者应遵守所在地区的法律法规，以及相关设备和服务的使用条款。是否适用、如何使用，由使用者自行判断并承担全部责任。
- 请自行备份重要录音。给他人录音前，请遵守当地法律并取得对方同意。
- 本项目不附带任何明示或默示的担保。因使用或无法使用本软件造成的任何后果，作者与贡献者不承担责任。详见 [MIT 许可](LICENSE)。

## 许可

[MIT](LICENSE)

---

## English

**open-recording-bean turns the recorder into a wireless microphone for your Mac, and turns what is said into text with speaker labels, live and on your own computer.**

For the soundcore Work (Anker's AI recorder, also sold as the Feishu recorder 飞书录音豆; model D3200). Firmware 04.92 is supported; a firmware update may break it at any time.
This is an unofficial, independent open-source project. It is not connected with Anker, Anker Innovations, soundcore or Feishu.

- **A microphone for any app.** Live audio from the recorder goes over Bluetooth to a local service and on to a virtual sound card (BlackHole), which dictation tools and any app that lets you choose a microphone can use.
- **Live text.** Sentences appear in the browser as you speak, recognised on your Mac by SenseVoice Small. Nothing is sent to a cloud.
- **Speakers told apart.** Each sentence is labelled speaker 1, speaker 2 and so on. Only whether it is the same voice is known, never who it is, and no voiceprint is kept.
- **Text records.** Every session is saved as its own text file, with no limit on length, to browse by the hour, search, copy and download.
- **Recordings.** Recordings on the device can be downloaded, played, transcribed, summarised, exported and deleted.
- **For agents.** Through MCP, agents such as Claude Code or Codex can read the saved text by day, search it and read the last few minutes. Read-only, and absent unless you set it up.
- **Kept on your computer.** Sound, text and recordings stay on this computer. Text leaves it only when you choose a cloud model to tidy it up, or let an agent read it.

```bash
git clone https://github.com/maple-space/open-recording-bean.git
cd open-recording-bean
./start.sh          # sets everything up on first run, then opens http://127.0.0.1:8875
```

Requires macOS, Node.js 20 or later and Python 3.10 to 3.13. Tested on an Intel MacBook Pro with macOS 15.7 and recorder firmware 04.92. The interface is in Simplified Chinese.

### For agents (MCP)

Agents such as Claude Code, Codex or Cursor can read the text you have saved: “summarise what I said this afternoon”, “who mentioned the quote last week”. It is read-only: it cannot change or delete anything and never connects to the recorder. Unless you set it up, it does not exist.

The “给 Agent 用” page of the web interface has the same instructions, with the path of your own install already filled in, ready to copy.

```bash
claude mcp add --scope user open-recording-bean -- /path/to/open-recording-bean/start.sh mcp
```

For Codex, in `~/.codex/config.toml`:

```toml
[mcp_servers.open-recording-bean]
command = "/path/to/open-recording-bean/start.sh"
args = ["mcp"]
```

For other clients (Claude Desktop, Cursor and so on), in their MCP configuration:

```json
{
  "mcpServers": {
    "open-recording-bean": { "command": "/path/to/open-recording-bean/start.sh", "args": ["mcp"] }
  }
}
```

| Tool | What it does |
| --- | --- |
| `list_days` | Which days have text, and how many sentences each |
| `read_text` | The realtime text of a day or of a stretch of it, each sentence with its time and speaker, in pages when it is long |
| `latest_text` | The last few minutes: “what was just said” |
| `search_text` | Find words in the realtime text and in the text of recordings |
| `list_recordings` | The recordings in the library |
| `read_recording` | The text and summary of one recording |

It reads the text record files and the library in `.local/data` directly, so it works without the local service running. The MCP itself sends nothing anywhere. What an agent reads is handled by the model behind it, so with a cloud model that text goes to that service.

### Sources and thanks

- The code is built on [Shawn-TKD/recording-bean-web](https://github.com/Shawn-TKD/recording-bean-web) (MIT); its copyright notice is kept in [LICENSE](LICENSE).
- The protocol implementation draws on the community research project [tacshi/Soundcore](https://github.com/tacshi/Soundcore) (MIT, Copyright (c) 2026 Rocky Shi), and was written anew from this project's own packet captures and verification on a real D3200 (firmware 04.92).
- Speech is recognised by [SenseVoice](https://github.com/FunAudioLLM/SenseVoice) and speakers are told apart by CAM++ from [3D-Speaker](https://github.com/modelscope/3D-Speaker), both run through [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx).

All third-party sources and licences are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

### Disclaimer

- This is an independent open-source project. It has no connection with Anker, Anker Innovations, soundcore or Feishu, and is not endorsed or sponsored by them. Names that appear here, such as “Anker”, “安克”, “soundcore Work”, “飞书” (Feishu), “D3200” and “录音豆”, are used only to describe the hardware this project works with. The names and trademarks belong to their respective owners.
- It is meant only for interoperability with devices that you own or are explicitly authorised to manage: to let owners use their own device on their own computer and take their own recordings off their own hardware.
- It does not contain or distribute any official firmware, private keys, device credentials, account information or official apps (APK/IPA). The protocol implementation draws on public community research (see “Sources and thanks” above) and was verified on a device the author owns.
- It provides no server, hosts nobody's data and runs no service. Data is kept on the user's own computer; only when the user chooses a cloud model to tidy up text is that content sent to that service.
- It is published for personal learning, research and interoperability. Users must follow the laws of where they live and the terms of use of the devices and services involved. Whether and how to use it is for each user to judge, at their own responsibility.
- Back up recordings that matter. Before recording other people, follow local law and get their consent.
- It comes with no warranty of any kind, express or implied. The authors and contributors accept no liability for any consequence of using it or of being unable to use it. See the [MIT licence](LICENSE).

### Licence

[MIT](LICENSE)

---

## 社群

用法交流、问题反馈都在微信群里，用微信扫码进群：

<img src="docs/images/wechat-group.png" alt="用户群的微信二维码" width="260">

群二维码 7 天内有效，过期后会在这里更新。扫不了的话，到 Issues 里说一声。

- 报告问题：到 [Issues](https://github.com/maple-space/open-recording-bean/issues) 里写清楚哪一步不成功、页面上的提示是什么。
- 请不要在公开的地方贴出录音、文字记录或设备序列号。

Community: questions and feedback are shared in a WeChat group. Scan the code above with WeChat to join. The code is valid for 7 days and is replaced here when it expires.
