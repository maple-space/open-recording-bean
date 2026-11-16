# 同仓源码与声卡 Release

发布入口分为两部分：网站从本仓库克隆后运行 `./start.sh`，声卡从本仓库 Release 安装一次。Intel 与 Apple Silicon 共用一个 Universal 声卡包，网站的 Python/Node.js 依赖由各电脑首次启动时按本机架构安装。

## 当前版本与附件

当前正式版本为声卡 **1.0.1**，Git 标签 **`audio-v1.0.1`**，支持 Intel 与 Apple Silicon，两个平台均已完成功能验证。标签保留该版本的网站适配和完整声卡源码快照，网站后续更新在主分支持续维护。声卡有自己的版本号，但不另建仓库。

| 附件 | 用途 |
| --- | --- |
| `RecordingBeanAudio2ch-1.0.1-release.zip` | 推荐给普通用户；安装器、安装/启用入口、源码及许可一起提供。 |
| `RecordingBeanAudio2ch-1.0.1-universal.pkg` | 单独的通用声卡安装器，Intel / Apple Silicon 共用。 |
| `RecordingBeanAudio2ch-1.0.1-source.tar.gz` | 该驱动完整对应源码，含构建、安装、验证脚本与 GPL 文本。 |
| `SHA256SUMS`、`verification.json` | 下载完整性、架构及实际签名/公证/探针验证结果。 |

GitHub 的 `Source code (zip/tar.gz)` 自动提供标签源码；它们不是上述已编译的声卡安装包。模型权重不上传 Release，由首次启动脚本下载并校验。此流程没有打包免 Node/Python 的桌面 App。

## 准备与核验

1. 将网站、声卡、许可证及用户文档放到同一次待发布提交。保留 `LICENSE`、`LICENSING.md`、`THIRD_PARTY_NOTICES.md` 以及两处声卡源码目录。个人数据、模型、`.venv`、`node_modules`、`.local` 和内部运行记录不提交。
2. 检查测试与网页构建；构建到独立输出目录，避免替换正在使用的网站。确认 `docs/releases/audio-v1.0.1.md` 中的验证范围和实际结果一致。
3. 使用 macOS SDK 构建通用声卡（以下输出目录必须尚不存在）：

   ```bash
   python3 tools/recording-bean-audio/build.py \
     --output .local/releases/audio-v1.0.1/RecordingBeanAudio2ch-1.0.1
   ```

   安装 ZIP 位于 `.local/releases/audio-v1.0.1/RecordingBeanAudio2ch-1.0.1-release.zip`；单独附件在同名目录内。构建不安装驱动、不重启音频服务，保留中间文件和验证日志。若要重新构建，换一个输出目录，不覆盖先前产物。
4. 核对 `verification.json` 的双架构及签名状态，运行 ZIP 内的 `shasum -a 256 -c SHA256SUMS`；检查源码包包含 `third_party/recording-bean-audio` 和 `tools/recording-bean-audio`。安装 ZIP 本身的 SHA-256 可另写到发布说明。
5. 使用发布者自己的证书签名/公证时，按声卡构建 README 提供参数；未签名/未公证时如实标明。构建检查之外，功能验证需覆盖 Intel 与 Apple Silicon 的安装、蓝牙传声、本机识别和目标应用输入。构建清单保留当时的自动检查结果，后续人工验证反映在 Release 支持说明中。

## 发布新版本

以下文件名与标签以 1.0.1 为例；下一次发版应统一替换为新版本号。

1. 推送已经检查的源码提交；在该提交创建 `audio-v1.0.1` 标签。不要移动已发布标签。
2. 在 GitHub 本仓库的 Releases → Draft a new release 选择标签，标题使用 `Recording Bean Audio 2ch 1.0.1 — macOS Universal` 这样的产品名、版本与平台格式，正文参考 [发布说明](releases/audio-v1.0.1.md)。
3. 上传上表 5 个附件，确认文件名与网站链接、README 一致。先保存草稿，检查完整后再公开。尚处于验证阶段的版本标为 Pre-release；完成预定平台验证后发布为正式 Release。
4. 发布后以未登录访问验证下载链接，并下载附件核对 SHA-256。再在新目录从远端克隆，确认代码、许可及文档齐全。
5. 发布文案面向使用者，说明兼容平台、下载选择、安装步骤、变化与已知限制。内部测试记录不放入用户指南；二进制或安装工具有修改时发布新版本，不覆盖旧附件或移动标签。仅说明文字变化可更新 Release 正文及主分支文档。

## 发布资料检查

- README 中英文、macOS 指南、声卡说明和 Release 的平台、版本号、安装方法保持一致。
- 保留独立项目声明、组件许可及第三方来源；署名和商标归属不因改名而改变。
- 安装包标明实际签名与公证状态；模型作为第三方下载内容说明，不作为本项目源码打包。
- README 底部的用户群二维码能加载，群码过期时可通过 Issues 提醒更新；更新图片前确认是可公开发布的版本。
- 以公开访问检查文档、图片和下载链接，不提交个人运行数据或内部状态记录。

GitHub Release 支持上传二进制附件及草稿/预发布状态，参见 [GitHub 官方文档](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository)。Universal 二进制包含两种架构，参见 [Apple 文档](https://developer.apple.com/documentation/apple-silicon/building-a-universal-macos-binary)。
