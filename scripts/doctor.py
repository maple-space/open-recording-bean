#!/usr/bin/env python3
"""Say what this computer still lacks, and how to get it.     ./start.sh doctor

Reads only: nothing is installed, downloaded or changed, and the recorder is not touched.
"""
import importlib.util
import json
import os
import platform
import subprocess
import sys
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
PORT = os.environ.get('RECORDING_PORT') or '8875'
problems = 0


def line(ok, title, detail='', fix=''):
    global problems
    mark = '✓' if ok else '·' if ok is None else '✗'
    print(f'  {mark} {title}' + (f'：{detail}' if detail else ''))
    if not ok and fix:
        print(f'      {fix}')
    if ok is False:
        problems += 1


def main() -> int:
    print('\n开源录音豆 · 自检\n')
    try:        # platform.mac_ver() answers 10.16 on some Python builds; ask the system itself
        release = subprocess.run(['sw_vers', '-productVersion'], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        release = ''
    line(bool(release), '系统', f'macOS {release}（{platform.machine()}）' if release else f'{platform.system()}，目前只在 macOS 上验证过')

    version = sys.version_info
    in_venv = (PROJECT / '.venv').is_dir() and Path(sys.prefix).resolve() == (PROJECT / '.venv').resolve()
    line(in_venv and (3, 10) <= version[:2] <= (3, 13), 'Python 环境',
         f'{version.major}.{version.minor}.{version.micro}' + ('' if in_venv else '（项目自己的环境还没有建好）'), '运行 ./start.sh，会自动准备。')

    needed = [('fastapi', 'fastapi'), ('uvicorn', 'uvicorn'), ('bleak', 'bleak（蓝牙）'), ('cryptography', 'cryptography'),
              ('numpy', 'numpy'), ('av', 'PyAV（解码录音豆的声音）'), ('sounddevice', 'sounddevice（送声音到虚拟声卡）'),
              ('sherpa_onnx', 'sherpa-onnx（运行识别模型）')]
    absent = [label for module, label in needed if importlib.util.find_spec(module) is None]
    line(not absent, '需要的组件', '都已安装' if not absent else '缺少 ' + '、'.join(absent), '运行 ./start.sh，会自动安装。')

    line((PROJECT / 'dist' / 'index.html').is_file(), '网页', '已构建' if (PROJECT / 'dist' / 'index.html').is_file() else '还没有构建',
         '运行 ./start.sh，会自动构建（需要 Node.js 20 或更新）。')

    home = Path(os.environ.get('RECORDING_ASR_DIR') or PROJECT / '.local' / 'asr').expanduser()
    speech = (home / 'sensevoice' / 'model.int8.onnx').is_file() and (home / 'sensevoice' / 'tokens.txt').is_file()
    line(speech, '实时识别模型', 'SenseVoice Small 已就位' if speech else '没有找到', '运行 ./start.sh models 下载（约 240 MB）。')
    speaker = any((home / 'speaker').glob('*.onnx')) if (home / 'speaker').is_dir() else False
    line(speaker or None, '说话人模型', 'CAM++ 已就位' if speaker else '没有找到（实时文字照常能用，只是不标说话人）', '运行 ./start.sh models 下载（约 28 MB）。')

    try:
        from backend.audio_bridge import list_outputs
        outputs = list_outputs()
    except Exception:
        outputs = None
    card = outputs and outputs.get('suggested')
    line(bool(card) or None, '虚拟声卡', card or '没有找到（只看文字可以不装；要给输入法当麦克风就需要）',
         '安装 BlackHole：brew install blackhole-2ch，或到 existential.audio/blackhole 下载。装好后不用重启本服务。')

    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{PORT}/api/health', timeout=3) as response:
            health = json.load(response)
    except Exception:
        health = None
    if health:
        recorder = health.get('devices', {}).get('feishu', {})
        line(True, '本机服务', f'正在运行（http://127.0.0.1:{PORT}），' + ('录音豆已连接' if recorder.get('connected') else '录音豆还没有连接'))
    else:
        line(None, '本机服务', '没有在运行', '运行 ./start.sh 启动。')

    print()
    if problems:
        print(f'有 {problems} 项需要处理，见上面带 ✗ 的行。\n')
    else:
        print('必需的东西都在。带 · 的是可选项。\n')
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
