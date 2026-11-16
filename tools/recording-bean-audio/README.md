# Recording Bean Audio 2ch 构建与发布

本项目配套的双声道虚拟音频驱动，macOS 11+，Intel / Apple Silicon 通用。
完整对应源码在 `third_party/recording-bean-audio`，来源与 GPL 许可证见其中的 NOTICE.md、LICENSE。

设备名称为 **Recording Bean Audio 2ch**；Bundle ID、UID、厂商名、工厂 UUID、安装包界面均为独立标识；使用系统默认音频图标，不包含上游品牌图标或安装包设计。设备类型为 Unknown，以适配 Typeless 2.11.0 的麦克风列表过滤。

## 直接下载安装

普通用户从 [open-recording-bean 的声卡 Release](https://github.com/maple-space/open-recording-bean/releases/tag/audio-v1.0.1) 下载 **`RecordingBeanAudio2ch-1.0.1-release.zip`**，解压后运行 `安装并启用.command`。这是声卡发布包，Intel 和 Apple Silicon 共用，无需编译；网站用 `git clone` 下载后运行 `./start.sh`。

## 本机构建

需要 macOS、Python 3.10+ 和 Xcode 或带 macOS SDK 的 Command Line Tools：

```bash
python3 tools/recording-bean-audio/build.py
```

若使用发布包里的 `RecordingBeanAudio2ch-1.0.1-source.tar.gz`，先解压并进入 `recording-bean-audio-source/`，再运行同一条构建命令；其中已经包含所需源码、脚本及完整许可证。

也可用 `--output /你的路径/recording-bean-audio-release` 指定尚不存在的输出目录。
脚本保留构建中间文件，不安装驱动、不下载代码、不修改系统默认设备、不重启任何进程。
默认产物的驱动为 ad-hoc 签名，安装包未作 Developer ID 签名或 Apple 公证；真实状态记录在 verification.json。

## 安装

1. 解压发布包，双击 **`安装并启用.command`**。按提示完成本机管理员认证，通常无需重启电脑。
2. 脚本安装驱动后，自动检查系统是否已经识别它；如果已经可用，直接完成。
3. 如果系统尚未加载，脚本提示“是否重新加载系统音频服务”。输入 `y` 才会重新加载 CoreAudio，声音会短暂停顿；回车或输入其他内容则保留安装文件、暂不启用。它不会退出输入法或其他应用。
4. 检测到声卡后，网页输出和 Typeless/豆包麦克风都选择 **Recording Bean Audio 2ch**，即可测试电平和听写。

也可直接安装 `.pkg`；安装后先查看设备列表。如果设备没有出现，再运行上述安装入口检测。若系统拒绝重新加载音频服务或声卡仍未出现，保存工作后重启 macOS，再检查设备列表。

升级或更换声卡后，请在网站与输入法中各选择一次 **Recording Bean Audio 2ch**。

## 卸载

1. 关闭网站的实时输入，把输入法或会议软件的麦克风改回原来的设备。
2. 把 `/Library/Audio/Plug-Ins/HAL/RecordingBeanAudio2ch.driver` 移到废纸篓：在访达里用“前往 → 前往文件夹…”打开这个位置，移动时需要管理员认证。
3. 重启 macOS。之后设备列表里不再有 **Recording Bean Audio 2ch**。

卸载只移除这一个驱动，不影响其他声卡。

## 用自己的证书签名与公证

已有自己的 Apple Developer ID Application、Developer ID Installer 证书时：

```bash
python3 tools/recording-bean-audio/build.py \
  --application-identity 'Developer ID Application: 你的签名名称 (TEAMID)' \
  --installer-identity 'Developer ID Installer: 你的签名名称 (TEAMID)' \
  --notary-profile '你已配置的 notarytool 钥匙串配置名称'
```

两种身份必须一起提供；`--notary-profile` 可省略，此时仅签名、不公证。脚本不索取或储存密码、证书私钥或 Apple API 密钥。钥匙串授权由系统处理。公证成功后脚本装订票据，再计算安装包校验和。不要复用上游签名，或为安装关闭 Gatekeeper/SIP。

## 一起发布的文件

默认构建输出文件名带时间戳；正式上传 Release 时统一命名为 `RecordingBeanAudio2ch-1.0.1-release.zip`，包含：

- `RecordingBeanAudio2ch-1.0.1-universal.pkg`
- `RecordingBeanAudio2ch-1.0.1-source.tar.gz`：本版本完整源码、构建及测试脚本、版权和许可证
- `README.md`、`LICENSE`、`verification.json`、`SHA256SUMS`
- `安装并启用.command`（只读检测工具随安装包内的驱动一起安装）

源码目录与 `tools/recording-bean-audio` 随网站仓库一起提交；安装包和完整源码包作为同一次 Release 的附件提供。独立产品名称不改变上游 GPL 许可及源码分发要求。

## 验证

`probe.c` 在独立进程加载真实驱动二进制，检查名称、UID、厂商、Unknown 类型、无自定义图标属性、48 kHz 双声道，以及 1400 个不同大小块/1,312,000 帧跨缓冲边界的逐样本一致性。

构建还验证架构、代码签名、包内文件哈希、无后台重启的 pkg 脚本、无原版二进制标识及品牌图标。单入口脚本的六个隔离测试覆盖设备已就绪、拒绝刷新、输入结束、明确同意、刷新失败及查询错误；这些测试使用替身，不重启真实服务。输出的源码包可独立解压重建。

1.0.1 已在 Intel 与 Apple Silicon 上完成功能验证。`verification.json` 记录构建时的自动检查结果；其中系统安装和应用交互字段不随后续人工验证改写，版本支持状态以对应 Release 说明为准。维护新版本时，除构建检查外，还需验证实际安装、网站传声与目标应用输入。

## 许可

本目录工具和文档由 open-recording-bean contributors 维护，采用 GPL-3.0-or-later：你可以按照 GNU GPL 第 3 版或自行选择的后续版本复制、修改及分发，不附带任何担保。完整许可文本见 [LICENSE](LICENSE)。驱动本身的上游版权和 GPL v3 许可保持不变，见源码目录中的 LICENSE 和 NOTICE.md。

这些工具与网站在同一仓库维护，根目录的 MIT 许可不覆盖本目录。发布安装包时，必须同时提供该版本完整的对应源码和构建、安装工具。
