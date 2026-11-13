#!/usr/bin/env python3
"""Download the local models that realtime text needs, and check every byte.

    ./start.sh models          (or: .venv/bin/python scripts/fetch_models.py)

Two models, about 270 MB together, are put under .local/asr/:

- SenseVoice Small (int8 ONNX, as published for sherpa-onnx): turns speech into text.
- CAM++ speaker model: tells voices apart, for the "说话人1、说话人2" labels.

Nothing else is downloaded and nothing is uploaded. Each file is checked against a fixed SHA-256
before it is put in place, so a mirror cannot hand over something different. A download that is
interrupted continues from where it stopped the next time this is run.

Uses only the Python standard library.
"""
import argparse
import hashlib
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
HOME = Path(os.environ.get('RECORDING_ASR_DIR') or PROJECT / '.local' / 'asr').expanduser()
SOURCES = ['https://huggingface.co', 'https://hf-mirror.com']      # the second is reachable from mainland China
SENSEVOICE = 'csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/'
SPEAKER = '3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx'

MODELS = [
    dict(title='SenseVoice Small：把说话变成文字', directory='sensevoice', files=[
        dict(name='model.int8.onnx', size=239233841, path=SENSEVOICE + 'model.int8.onnx',
             sha256='c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51'),
        dict(name='tokens.txt', size=315894, path=SENSEVOICE + 'tokens.txt',
             sha256='f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc')]),
    dict(title='CAM++：区分说话人', directory='speaker', files=[
        dict(name=SPEAKER, size=28281138, path='csukuangfj/speaker-embedding-models/resolve/main/' + SPEAKER,
             sha256='f682b514c05d947ee3fa91cd6ec6c5c7543479a128373fa29b1faedccd21fd11',
             also=['https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/' + SPEAKER])]),
]


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def in_place(target: Path, item: dict) -> bool:
    return target.is_file() and target.stat().st_size == item['size'] and digest(target) == item['sha256']


def addresses(item: dict) -> list:
    chosen = os.environ.get('HF_ENDPOINT', '').rstrip('/')
    bases = [chosen] if chosen else SOURCES
    return [f'{base}/{item["path"]}' for base in bases] + item.get('also', [])


def megabytes(count: int) -> str:
    return f'{count / 1e6:.0f} MB' if count >= 1e6 else f'{count / 1e3:.0f} KB'


def download(url: str, part: Path, item: dict) -> None:
    """Fetch into *part*, continuing a partial file when the server allows it."""
    have = part.stat().st_size if part.exists() else 0
    if have > item['size']:
        have = 0
    request = urllib.request.Request(url, headers={'User-Agent': 'open-recording-bean'})
    if have:
        request.add_header('Range', f'bytes={have}-')
    with urllib.request.urlopen(request, timeout=20) as response:
        resumed = have and response.status == 206
        if not resumed:
            have = 0
        shown = 0.0
        with part.open('ab' if resumed else 'wb') as out:
            while True:
                block = response.read(256 * 1024)
                if not block:
                    break
                out.write(block)
                have += len(block)
                if time.monotonic() - shown >= 0.5:
                    shown = time.monotonic()
                    print(f'\r    {megabytes(have)} / {megabytes(item["size"])}（{have * 100 // item["size"]}%）', end='', flush=True)
    print(f'\r    {megabytes(have)} / {megabytes(item["size"])}（100%）', flush=True)


def fetch(directory: Path, item: dict) -> bool:
    target = directory / item['name']
    part = directory / (item['name'] + '.part')
    for url in addresses(item):
        host = url.split('/')[2]
        print(f'  下载 {item["name"]}（{megabytes(item["size"])}），来源 {host}')
        try:
            download(url, part, item)
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            reason = getattr(error, 'reason', None) or error
            print(f'\n    这个来源没有下载成功（{type(reason).__name__}），换下一个。')
            continue
        if part.stat().st_size == item['size'] and digest(part) == item['sha256']:
            os.replace(part, target)
            return True
        print('    下载到的内容和预期不一致，已丢弃，换下一个来源。')
        part.open('wb').close()                     # start over rather than continue a wrong file
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description='下载实时文字需要的本机模型')
    parser.add_argument('--quiet-if-complete', action='store_true', help='模型已经齐全时不输出任何内容')
    parser.add_argument('--check', action='store_true', help='只检查，不下载')
    options = parser.parse_args()
    missing = [(model, item) for model in MODELS for item in model['files'] if not in_place(HOME / model['directory'] / item['name'], item)]
    if not missing:
        if not options.quiet_if_complete:
            print(f'模型已经齐全，放在 {HOME}')
        return 0
    if options.check:
        for model, item in missing:
            print(f'缺少：{model["title"]}（{item["name"]}）')
        return 1
    total = sum(item['size'] for _, item in missing)
    print(f'\n下载本机识别模型，共 {megabytes(total)}，只需要做一次。放在 {HOME}')
    failed = []
    for model in MODELS:
        wanted = [item for owner, item in missing if owner is model]
        if not wanted:
            continue
        print(f'\n{model["title"]}')
        directory = HOME / model['directory']
        directory.mkdir(parents=True, exist_ok=True)
        failed += [item['name'] for item in wanted if not fetch(directory, item)]
    if failed:
        print('\n没有下载成功：' + '、'.join(failed))
        print('可以稍后再运行 ./start.sh models，已下载的部分会接着下载。')
        print('也可以手动下载这些文件放进上面的文件夹，地址在 scripts/fetch_models.py 的开头。')
        return 1
    print('\n模型已经齐全。回到网页点“重新检测”，不需要重启服务。')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\n已停止。再次运行会接着下载。')
        sys.exit(130)
