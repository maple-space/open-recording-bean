# 第三方来源与许可

本仓库采用按组件区分的许可：网站、后端及通用脚本采用 [MIT](LICENSE)；配套声卡和专用工具采用 GPL。完整目录范围见 [LICENSING.md](LICENSING.md)。下列第三方来源各自保留版权和许可，根目录 MIT 许可不替代它们。

## 代码来源

### Recording Bean Web

- 来源：[Shawn-TKD/recording-bean-web](https://github.com/Shawn-TKD/recording-bean-web)
- 许可：MIT，`Copyright (c) 2026 Recording Bean Local contributors`
- 关系：本项目在它的源码基础上开发，不是从零独立实现。原版权声明保留在根目录的 [LICENSE](LICENSE) 里。

### Soundcore 协议研究

- 来源：[tacshi/Soundcore](https://github.com/tacshi/Soundcore)
- 许可：MIT，`Copyright (c) 2026 Rocky Shi`。完整许可文本随仓库提供：[licenses/tacshi-Soundcore-MIT.txt](licenses/tacshi-Soundcore-MIT.txt)
- 关系：录音豆的通信协议、加密和音频封装参考了这个项目公开的研究，涉及 `backend/feishu_protocol.py`、`backend/feishu.py`、`backend/feishu_live.py`。本项目没有直接运行它的代码，实时传声部分的行为是在固件 04.92 的真机上重新测量的。分发本项目的源码或构建产物时，请一并保留这份版权声明和许可文本。

## 运行时安装的组件

这些组件不在本仓库里，由 `./start.sh` 通过 pip 和 pnpm（或 npm）安装，各自保持自己的许可：

| 组件 | 用途 | 许可 |
| --- | --- | --- |
| [FastAPI](https://github.com/fastapi/fastapi)、[Uvicorn](https://github.com/encode/uvicorn) | 本机服务 | MIT、BSD-3-Clause |
| [bleak](https://github.com/hbldh/bleak) | 蓝牙 | MIT |
| [cryptography](https://github.com/pyca/cryptography) | 密钥交换和解密 | Apache-2.0 或 BSD-3-Clause |
| [NumPy](https://github.com/numpy/numpy) | 音频计算 | BSD-3-Clause |
| [PyAV](https://github.com/PyAV-Org/PyAV) | 解码录音豆的 Opus 音频（安装包内含 FFmpeg 的库） | BSD-3-Clause；FFmpeg 为 LGPL/GPL |
| [python-sounddevice](https://github.com/spatialaudio/python-sounddevice) | 向虚拟声卡播放（内含 PortAudio） | MIT |
| [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) | 运行识别模型和说话人模型（内含 ONNX Runtime） | Apache-2.0；ONNX Runtime 为 MIT |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | 给 Agent 用的只读接口 | MIT |
| [React](https://github.com/facebook/react)、[Vite](https://github.com/vitejs/vite) | 网页 | MIT |
| [Lucide](https://github.com/lucide-icons/lucide)、[fflate](https://github.com/101arrowz/fflate) | 图标、打包导出 | ISC、MIT |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper)（可选） | 用 Whisper 模型转写录音 | MIT |

这不是完整的依赖清单，间接依赖见 `pnpm-lock.yaml` 和 pip 的安装记录。

## 模型

模型文件不在本仓库里，也不作为本项目原创代码或自有模型发布。首次 `./start.sh`（或单独运行 `./start.sh models`）从公开地址下载到使用者电脑上，并逐个核对 SHA-256。下载、使用或另行分发模型时仍需遵守模型自身的许可；本项目的 MIT/GPL 许可不替代模型许可。

| 模型 | 用途 | 来源和许可 |
| --- | --- | --- |
| SenseVoice Small | 把说话变成文字 | [官方模型卡](https://huggingface.co/FunAudioLLM/SenseVoiceSmall) 指定 [FunASR MODEL_LICENSE](https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE)；不要用推理代码的许可代替模型许可。下载的是 [sherpa-onnx 的 ONNX 版本](https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17)。 |
| CAM++（`speech_campplus_sv_zh-cn_16k-common`） | 区分说话人 | 来源为 [modelscope/3D-Speaker](https://github.com/modelscope/3D-Speaker)，模型说明与许可见 [对应的官方模型页](https://www.modelscope.cn/models/damo/speech_campplus_sv_zh-cn_16k-common)。使用的是 sherpa-onnx 发布的 ONNX 版本，精确文件和校验值在 `scripts/fetch_models.py` 中。 |
| Fun-ASR-Nano（可选，需自行准备） | 转写录音 | [FunAudioLLM](https://github.com/FunAudioLLM)，许可见其模型页面。 |
| Whisper 模型（可选，需自行准备） | 转写录音 | [OpenAI Whisper](https://github.com/openai/whisper)，MIT。 |

## 配套虚拟声卡

配套驱动名称为 **Recording Bean Audio 2ch**，完整对应源码位于 [`third_party/recording-bean-audio`](third_party/recording-bean-audio)。它基于 [BlackHole v0.7.1](https://github.com/ExistentialAudio/BlackHole/tree/v0.7.1) 的 GPL-3.0 源码修改，原作者为 Existential Audio Inc.；上游版权与完整许可证保留在 [LICENSE](third_party/recording-bean-audio/LICENSE)。网站/后端的 MIT 许可不替代驱动许可。

修改包括：独立产品和设备名称、Bundle ID、UID、厂商名、工厂 UUID；去除品牌图标并使用系统默认图标；将 Box/Device transport 改为 Unknown，以适配 Typeless 2.11.0。音频处理逻辑保持不变。没有分发上游 Logo、品牌图片、安装包设计或官方编译二进制，也没有使用上游签名。这里的上游名称仅用于版权和来源说明，不是修改版的产品名。详见 [NOTICE.md](third_party/recording-bean-audio/NOTICE.md)。

专用构建及测试脚本位于 `tools/recording-bean-audio`，采用 GPL-3.0-or-later，完整文本见其 [LICENSE](tools/recording-bean-audio/LICENSE)。声卡源码、构建工具和网站在本仓库共同维护；独立的声卡安装包与其完整对应源码包、许可证和修改说明一起发布。默认产物为 ad-hoc 签名驱动和未公证安装包；可由发布者提供自己的 Developer ID 身份和 notarytool 配置进行签名、公证，以生成产物的 verification.json 为准。

Soundflower（MIT）仍可由使用者自行安装，本仓库不包含其源码或安装包。

## 商标和图片

- “soundcore”“Anker”“安克”“飞书”是各自所有者的商标。本项目提到它们只是为了说明适用的硬件，与这些公司没有关联。
- 网页里的录音豆图案（`public/assets/recorder.svg`、`public/favicon.svg`）是为本项目画的示意图，不是产品照片，随本项目按 MIT 许可发布。
- `docs/images/` 里的截图内容是虚构的对话。
