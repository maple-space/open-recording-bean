# Recording Bean Audio 2ch

双声道 macOS 虚拟音频驱动，配套 open-recording-bean 使用。

源码基于第三方 GPL-3.0 音频驱动修改，来源、版权、改动和品牌边界详见
[NOTICE.md](NOTICE.md) 与 [LICENSE](LICENSE)。采用独立产品名称、标识和系统默认图标。

在项目根目录构建：

```bash
python3 tools/recording-bean-audio/build.py
```

需要 macOS、Python 3.10+、Xcode 或带 macOS SDK 的 Command Line Tools。
此目录与 `tools/recording-bean-audio` 合起来是该版本完整的对应源码与构建工具。
更多安装、发布和签名说明见 [构建说明](../../tools/recording-bean-audio/README.md)。
