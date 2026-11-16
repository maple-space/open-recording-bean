# Recording Bean Audio 2ch 1.0.1 — macOS Universal

为 open-recording-bean 配套的双声道虚拟声卡，将录音豆的实时声音送入 Typeless、豆包输入法及可选择麦克风的会议软件。

**支持 Intel 与 Apple Silicon，两个平台均已完成功能验证。** 一个 Universal 安装包包含 `x86_64` 与 `arm64`，适用于 macOS 11 及以上版本，无需分别下载或自行编译。声音由录音豆采集，无需电脑内置麦克风。

## 下载与安装

- **普通用户：`RecordingBeanAudio2ch-1.0.1-release.zip`**。内含通用 `.pkg` 和 `安装并启用.command`，解压后双击后者，完成本机管理员认证。
- `RecordingBeanAudio2ch-1.0.1-universal.pkg` 是同一安装器的单独附件。
- `RecordingBeanAudio2ch-1.0.1-source.tar.gz` 是声卡完整对应源码与构建、安装工具。`SHA256SUMS` / `verification.json` 提供文件校验和构建验证信息。
- GitHub 自动生成的 `Source code` 附件包含该标签的网站及声卡源码，不是桌面应用安装器。

安装后会检测声卡是否已就绪，通常无需重启电脑；未加载时会询问是否重新加载音频服务。如果系统拒绝重新加载或设备仍未出现，保存工作后重启 macOS，再检查设备列表。

当前驱动为 **ad-hoc 签名**，安装包**未使用 Developer ID 签名或 Apple 公证**。开发者验证提示、设备选择及无声排查见 [macOS 安装与使用指南](https://github.com/maple-space/open-recording-bean/blob/main/docs/macos-setup.md)。

## 网站从源码运行

网站和声卡源码在同一仓库维护。上述安装包只安装声卡；网站按以下方式运行：

提前安装 Node.js 22.12+（或 20.x 的 20.19+）和 Python 3.10–3.13（或 uv），并满足这些运行环境的系统要求，首次保持联网：

```bash
git clone https://github.com/maple-space/open-recording-bean.git
cd open-recording-bean
./start.sh
```

启动时自动安装应用依赖、构建网页、下载并校验约 270 MB 的语音识别和说话人模型。不需 API Key，也不需手填模型目录。详见 [首次运行指南](https://github.com/maple-space/open-recording-bean/blob/main/docs/first-run.md)。

连接录音豆后进入“实时输入”，网页输出和目标应用麦克风均选择 **Recording Bean Audio 2ch**，再开启传声。录音豆同一时间只能连接一个客户端，切换前请在原客户端断开。

GitHub 自动生成的 `Source code` 附件是发布标签的源码快照；上面的 Git 克隆方式获取持续更新的网站源码。

## 版本内容

- 新增 Recording Bean Audio 2ch 独立品牌声卡，兼容 Typeless 2.11.0 的设备列表；网站识别并优先推荐该设备。
- 提供安装后设备检测；仅在设备未加载且使用者同意时重新加载音频服务。
- 实时音频缓存；录音结束后保存到资料库时复用本机已有音频，只补传缺口并核验。停录后仍需手动保存，手机缓存不会自动迁到电脑。
- 配套提供 macOS 安装、模型下载、设备选择与故障排查说明。
- 明确同仓许可边界：网站/后端 MIT，声卡 GPL v3，专用工具 GPL-3.0-or-later，模型保留自己的许可。

网站与后端使用 MIT 许可；声卡驱动保留上游 GPL v3 许可，专用工具使用 GPL-3.0-or-later；模型及其他依赖遵循各自许可。详见 [组件许可与来源](https://github.com/maple-space/open-recording-bean/blob/main/LICENSING.md)。

## 下载校验

`RecordingBeanAudio2ch-1.0.1-release.zip` 的 SHA-256：

```text
5bac1006e4113696107ce9d641573b611be3f3e9b53ebde5990f384477a843d2
```

解压后可在发布包目录运行 `shasum -a 256 -c SHA256SUMS` 校验包内文件。`verification.json` 保留构建时的检查记录，后续功能验证与支持状态以本页说明为准。
