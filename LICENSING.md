# 仓库许可与发布范围 / Licensing

open-recording-bean 的网站、后端及 Recording Bean Audio 2ch 配套声卡在**同一个仓库维护**。这是包含不同许可组件的项目，各部分按自己的许可发布。

## 组件范围

| 目录或内容 | 适用许可 | 说明 |
| --- | --- | --- |
| `src/`、`backend/`、`scripts/`、`start.sh`、通用配置与文档 | [MIT](LICENSE)，另有明确声明的文件除外 | 保留本项目及上游版权声明；协议研究来源见第三方声明。 |
| `third_party/recording-bean-audio/` | [GNU GPL v3](third_party/recording-bean-audio/LICENSE) | 基于 BlackHole v0.7.1 修改；来源、版权和具体改动见 [NOTICE.md](third_party/recording-bean-audio/NOTICE.md)。 |
| `tools/recording-bean-audio/` | GNU GPL v3 或后续版本（[完整文本](tools/recording-bean-audio/LICENSE)） | 本项目编写的声卡构建、检测、安装及测试工具，文件保留 SPDX 声明。 |
| 下载的模型、pip/npm 依赖及其附带组件 | 各自的上游许可 | 不因与本项目配套使用而重新许可；来源见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。 |

根目录 `package.json` 的 `license: MIT` 描述网站的 Node.js 包，不覆盖 GPL 声卡目录或第三方模型。各组件文件内的版权和许可声明必须保留。新增贡献沿用所在组件的许可，贡献者保留其贡献的版权。

## 声卡来源与产品名称

对外介绍可使用以下表述：

> Recording Bean Audio 2ch 是本项目维护的专属虚拟声卡，基于 BlackHole 开源代码进行兼容性适配，以 GPL-3.0 发布。

本项目贡献包括设备标识、Typeless 兼容适配、构建与安装工具等；底层音频处理来自上游，不能将整个驱动描述成从零原创或改标为 MIT。上游名称仅用于准确的来源和版权说明；修改版使用 Recording Bean Audio 2ch 名称，不使用上游 Logo、品牌图片、官方安装包设计或签名。[上游许可证与品牌声明](https://github.com/ExistentialAudio/BlackHole/blob/v0.7.1/LICENSE)

网站通过操作系统音频接口向声卡输出声音；声卡作为独立的 macOS 驱动安装，不链接进网站或 Python 后端。同仓管理不改变各组件的许可。以后若将 GPL 实现直接合并或链接进其他程序，需要按实际组合方式重新核对许可；拆分仓库本身不能改变这项义务。参见上游许可证第 5、6 节。

## 配套发布

1. 网站源码与两个声卡目录一起维护、提交；新电脑的系统声卡仍需单独安装一次，日常只启动网站。
2. 发布声卡安装包时，在同一版本发布页提供该安装包的完整对应源码包、GPL 文本、来源与修改说明、构建和安装脚本。源码包应能独立构建对应版本，不以仅指向上游或持续变化的主分支链接代替。
3. 使用 `python3 tools/recording-bean-audio/build.py` 生成成套产物，并保留 `SHA256SUMS` 和 `verification.json`。声卡源码包同时包含 `third_party/recording-bean-audio` 与 `tools/recording-bean-audio`。
4. 模型权重不放入源码仓库或默认源码发布包；首次运行按固定地址下载并校验。若另做包含模型或依赖的离线包，应保留对应许可证并按各自条款分发。
5. 不分发 `.local/data/`、录音、文字记录、个人设置、凭证或本机虚拟环境。测试通过、签名和公证状态按实际结果说明，Intel 验证不替代 Apple Silicon 真机验证。

## English

The website, backend and companion audio driver are maintained in one repository,
with separate component licences. The root MIT licence covers the website,
backend, general scripts and documentation, except separately identified works.
`third_party/recording-bean-audio/` is a modified BlackHole v0.7.1 driver under
GNU GPL v3. The project's tools in `tools/recording-bean-audio/` are licensed under
GNU GPL v3 or, at your option, any later version. Contributors retain copyright
in their contributions. Third-party model weights and dependencies retain their
own licences; they are not claimed as original project code.

Preserve copyright, licence and modification notices. Distribute each driver
installer with its complete corresponding source, build and installation scripts,
and GPL text. Use the independent Recording Bean Audio branding; upstream names
are retained only for attribution. Repository layout does not itself determine
whether programs form a combined work. The driver is installed separately and
the application uses the operating system's audio interfaces.
