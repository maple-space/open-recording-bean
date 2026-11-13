# 第三方来源与许可

本项目自己的代码按 [MIT](LICENSE) 许可发布。下面这些来源各有自己的版权和许可，MIT 许可不替代它们。

## 代码来源

### Recording Bean Web

- 来源：[Shawn-TKD/recording-bean-web](https://github.com/Shawn-TKD/recording-bean-web)
- 许可：MIT，`Copyright (c) 2026 Recording Bean Local contributors`
- 关系：本项目在它的源码基础上开发，不是从零独立实现。原版权声明保留在根目录的 [LICENSE](LICENSE) 里。

### Soundcore 协议研究

- 来源：[tacshi/Soundcore](https://github.com/tacshi/Soundcore)
- 许可：MIT，`Copyright (c) 2026 Rocky Shi`。完整许可文本随仓库提供：[licenses/tacshi-Soundcore-MIT.txt](licenses/tacshi-Soundcore-MIT.txt)
- 关系：录音豆的通信协议、加密和音频封装参考了这个项目公开的研究，涉及 `backend/feishu_protocol.py`、`backend/feishu.py`、`backend/feishu_live.py`。本项目没有直接运行它的代码，实时传声部分的行为是在固件 04.92 的真机上重新测量的。分发本项目的源码或构建产物时，请一并保留这份版权声明和许可文本。

### openbean

[imraywang/openbean](https://github.com/imraywang/openbean)（Apache-2.0）是同一款硬件上的另一个开源项目。本项目把它当作参照，没有使用它的代码。

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
| [React](https://github.com/facebook/react)、[Vite](https://github.com/vitejs/vite) | 网页 | MIT |
| [Lucide](https://github.com/lucide-icons/lucide)、[fflate](https://github.com/101arrowz/fflate) | 图标、打包导出 | ISC、MIT |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper)（可选） | 用 Whisper 模型转写录音 | MIT |

这不是完整的依赖清单，间接依赖见 `pnpm-lock.yaml` 和 pip 的安装记录。

## 模型

模型文件不在本仓库里。`./start.sh models` 从公开地址下载到你自己的电脑上，并逐个核对 SHA-256。

| 模型 | 用途 | 来源和许可 |
| --- | --- | --- |
| SenseVoice Small | 把说话变成文字 | [FunAudioLLM/SenseVoice](https://github.com/FunAudioLLM/SenseVoice)，许可见 [FunASR 模型许可](https://github.com/modelscope/FunASR?tab=readme-ov-file#license)。使用的是 sherpa-onnx 发布的 ONNX 版本。 |
| CAM++（`speech_campplus_sv_zh-cn_16k-common`） | 区分说话人 | [modelscope/3D-Speaker](https://github.com/modelscope/3D-Speaker)，许可见其模型页面。使用的是 sherpa-onnx 发布的 ONNX 版本。 |
| Fun-ASR-Nano（可选，需自行准备） | 转写录音 | [FunAudioLLM](https://github.com/FunAudioLLM)，许可见其模型页面。 |
| Whisper 模型（可选，需自行准备） | 转写录音 | [OpenAI Whisper](https://github.com/openai/whisper)，MIT。 |

## 虚拟声卡

虚拟声卡由你自己安装，本项目不包含也不分发它们：[BlackHole](https://github.com/ExistentialAudio/BlackHole)（GPL-3.0）或 Soundflower（MIT）。

## 商标和图片

- “soundcore”“Anker”“安克”“飞书”是各自所有者的商标。本项目提到它们只是为了说明适用的硬件，与这些公司没有关联。
- 网页里的录音豆图案（`public/assets/recorder.svg`、`public/favicon.svg`）是为本项目画的示意图，不是产品照片，随本项目按 MIT 许可发布。
- `docs/images/` 里的截图内容是虚构的对话。
