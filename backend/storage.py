"""File archive and small SQLite catalog; never removes audio or prior versions."""
from __future__ import annotations

import hashlib
import io
import json
import math
import re
import shutil
import sqlite3
import subprocess
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


# Out of the box: recordings are turned into text on this computer, by the model ./start.sh downloads.
DEFAULT_SETTINGS = {'provider': 'api', 'asr': 'local', 'codex_model': '', 'claude_model': '', 'asr_model': 'sensevoice-small'}
EDIT_FIELDS = {'title', 'tags', 'transcript', 'summary', 'summary_template', 'custom_prompt'}
INTERNAL_FIELDS = EDIT_FIELDS | {'raw_transcript', 'segments', 'language', 'duration_ms', 'summary_provider', 'summary_source_transcript', 'generated_summary', 'generated_transcript', 'transcript_provider'}
AUDIO_SUFFIXES = {'.wav', '.ogg', '.opus', '.mp3', '.m4a', '.flac', '.webm', '.mp4', '.aac'}


class Storage:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = self.root / 'catalog.sqlite3'
        with self._connection() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            ''')

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.db, timeout=10)
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            with conn:
                yield conn
        finally:
            conn.close()

    def _read(self, table, key):
        with self._connection() as conn:
            row = conn.execute(f'SELECT data FROM {table} WHERE id=?', (key,)).fetchone()
        if row is None:
            raise KeyError('记录不存在')
        return json.loads(row[0])

    def _write(self, table, key, value):
        with self._connection() as conn:
            conn.execute(f'INSERT OR REPLACE INTO {table} (id,data) VALUES (?,?)', (key, json.dumps(value, ensure_ascii=False)))

    @staticmethod
    def _public(record):
        return {k: v for k, v in record.items() if k != 'audio_file'}

    @staticmethod
    def record_id_for(device_type, device_id, original_id):
        """One stable identity shared by job reservations and archive writes."""
        if device_type not in {'feishu', 'local'}:
            raise ValueError('不支持的设备类型')
        identity = json.dumps([device_type, str(device_id), str(original_id)], ensure_ascii=False)
        return hashlib.sha256(identity.encode()).hexdigest()[:32]

    def get_record(self, record_id):
        record = self._read('records', record_id)
        if record.get('trashed_at'):
            raise KeyError('录音已移到废纸篓')
        return self._public(record)

    def records(self):
        with self._connection() as conn:
            rows = conn.execute('SELECT data FROM records').fetchall()
        records = [self._public(value) for row in rows if not (value := json.loads(row[0])).get('trashed_at')]
        return sorted(records, key=lambda row: str(row.get('recorded_at') or row['created_at']), reverse=True)

    def import_audio(self, path, *, device_type, original_id, device_id='local', title='', recorded_at=None, duration_ms=0, move=False):
        try:
            duration_ms = float(duration_ms)
        except (TypeError, ValueError):
            raise ValueError('录音时长必须为非负有限数值') from None
        if not math.isfinite(duration_ms) or duration_ms < 0:
            raise ValueError('录音时长必须为非负有限数值')
        path = Path(path)
        suffix = path.suffix.lower()
        if suffix not in AUDIO_SUFFIXES or not path.is_file() or path.stat().st_size == 0:
            raise ValueError('请选择非空的音频文件（WAV、Ogg、MP3、M4A、FLAC 等）')
        record_id = self.record_id_for(device_type, device_id, original_id)
        directory = self.root / 'recordings' / record_id
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = directory / f'{uuid.uuid4().hex}{suffix}'
        if move:
            path.rename(target)
        else:
            shutil.copyfile(path, target)
        try:
            previous = self._read('records', record_id)
        except KeyError:
            previous = {}
        record = {
            'id': record_id, 'device_type': device_type, 'device_id': str(device_id),
            'original_id': str(original_id), 'title': title or path.stem,
            'recorded_at': recorded_at or now(), 'created_at': now(),
            'tags': [], 'transcript': '', 'summary': '', 'summary_template': 'meeting', 'custom_prompt': '',
            **previous,
            'size_bytes': target.stat().st_size, 'duration_ms': duration_ms or previous.get('duration_ms', 0),
            'audio_file': str(target.relative_to(self.root)), 'audio_url': f'/api/records/{record_id}/audio',
            'updated_at': now(),
        }
        record.pop('trashed_at', None)
        self._write('records', record_id, record)
        return self._public(record)

    def patch_record(self, record_id, patch, *, internal=False):
        if not isinstance(patch, dict) or set(patch) - (INTERNAL_FIELDS if internal else EDIT_FIELDS):
            raise ValueError('包含不支持的录音字段')
        for key, value in patch.items():
            if key == 'duration_ms' and (not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0):
                raise ValueError('录音时长必须为非负有限数值')
            if key == 'tags':
                if not isinstance(value, list) or len(value) > 30 or any(not isinstance(tag, str) or len(tag) > 100 for tag in value):
                    raise ValueError('标签最多 30 个，每个不超过 100 字')
            elif key in EDIT_FIELDS or key in {'raw_transcript', 'language', 'summary_provider', 'summary_source_transcript', 'generated_summary', 'generated_transcript', 'transcript_provider'}:
                if not isinstance(value, str) or len(value) > 2_000_000:
                    raise ValueError('文字字段无效或超过长度限制')
            if key == 'summary_template' and value not in {'meeting', 'memo', 'interview', 'custom'}:
                raise ValueError('不支持的总结模板')
        record = self._read('records', record_id)
        if record.get('trashed_at'):
            raise KeyError('录音已移到废纸篓')
        record.update(patch)
        record['updated_at'] = now()
        self._write('records', record_id, record)
        return self._public(record)

    def audio_path(self, record_id):
        if not re.fullmatch(r'[a-f0-9]{32}', record_id):
            raise ValueError('无效的录音标识')
        record = self._read('records', record_id)
        if record.get('trashed_at'):
            raise KeyError('录音已移到废纸篓')
        path = (self.root / record['audio_file']).resolve()
        if not path.is_relative_to(self.root / 'recordings') or not path.is_file():
            raise ValueError('归档音频不存在或路径无效')
        return path

    def trash_record(self, record_id):
        # Keep every audio revision and human edit together for Finder recovery.
        self.audio_path(record_id)
        record = self._read('records', record_id)
        directory = (self.root / 'recordings' / record_id).resolve()
        if directory.parent != self.root / 'recordings':
            raise ValueError('归档目录无效')
        (directory / 'metadata.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        (directory / 'transcript.txt').write_text(record.get('transcript', ''), encoding='utf-8')
        (directory / 'summary.md').write_text(record.get('summary', ''), encoding='utf-8')
        try:
            result = subprocess.run(['/usr/bin/trash', str(directory)], capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError('未能移入 macOS 废纸篓，请检查系统权限后重试。') from None
        if result.returncode != 0 or directory.exists():
            raise ValueError('未能移入 macOS 废纸篓，资料库记录已保留。')
        record['trashed_at'] = now()
        self._write('records', record_id, record)
        return {'id': record_id, 'trashed': True}

    def bundle(self, record_id):
        record = self.get_record(record_id)
        audio = self.audio_path(record_id)
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.write(audio, f'audio{audio.suffix}')
            archive.writestr('transcript.txt', record.get('transcript', ''))
            archive.writestr('summary.md', record.get('summary', ''))
            archive.writestr('metadata.json', json.dumps(record, ensure_ascii=False, indent=2))
        return output.getvalue()

    def settings(self):
        try:
            return {**DEFAULT_SETTINGS, **self._read('settings', 1)}
        except KeyError:
            return dict(DEFAULT_SETTINGS)

    def update_settings(self, patch):
        if not isinstance(patch, dict) or set(patch) - DEFAULT_SETTINGS.keys():
            raise ValueError('不支持的设置字段（API Key 不在服务端保存）')
        value = {**self.settings(), **patch}
        if value['provider'] not in {'api', 'codex', 'claude'} or value['asr'] not in {'api', 'local'}:
            raise ValueError('不支持的处理模式')
        if any(not isinstance(value[key], str) or len(value[key]) > 160 for key in ('codex_model', 'claude_model', 'asr_model')):
            raise ValueError('模型名称无效')
        if not value['asr_model'].strip():
            raise ValueError('语音模型名称不能为空')
        self._write('settings', 1, value)
        return value

    def save_job(self, job):
        self._write('jobs', job['id'], job)

    def get_job(self, job_id):
        return self._read('jobs', job_id)

    def jobs(self):
        with self._connection() as conn:
            rows = conn.execute('SELECT data FROM jobs').fetchall()
        return sorted((json.loads(row[0]) for row in rows), key=lambda item: item['created_at'], reverse=True)[:100]

    def recover_jobs(self):
        with self._connection() as conn:
            rows = conn.execute('SELECT data FROM jobs').fetchall()
        for row in rows:
            job = json.loads(row[0])
            if job['status'] in {'running', 'queued'}:
                job.update(status='failed', error='本地服务已重启，任务中断；请重新运行。', finished_at=now())
                self.save_job(job)
