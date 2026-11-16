"""Durable raw Opus frames. No Bluetooth, model, or UI dependencies.

One source is owned by one writer (including across service processes). A bounded
background queue decouples live playback from disk latency. Only committed frames
are reusable; an interrupted queue is simply a gap in the next closed download.
"""
import asyncio
import fcntl
import hashlib
import os
import queue
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from bisect import bisect_right
from pathlib import Path

from .feishu_protocol import iter_ogg

AUDIO, WIRE, WITNESS = 160, 166, 4
MAX_FRAMES = 1_080_000  # one six-hour hardware source (zero- or one-based)


def check_cancelled():
    # Older wait_for implementations can swallow cancellation when the awaited
    # response completes in the same turn. Never treat that as permission to save.
    if getattr(asyncio.current_task(), 'cancelling', lambda: False)():
        raise asyncio.CancelledError


def signature(secret):
    return hashlib.sha256(secret[0] + secret[1]).hexdigest()


class FrameCache:
    def __init__(self, root):
        self.root = Path(root)

    def source(self, serial, ident):
        if not serial or not 0 < int(ident) <= 0xffffffff:
            raise ValueError('音频缓存缺少设备或文件身份')
        directory = self.root / hashlib.sha256(serial.lower().encode()).hexdigest()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return Source(directory / f'{int(ident)}.sqlite3')


class Source:
    def __init__(self, path):
        self.guard = Path(str(path) + '.lock').open('a+b')
        try:
            fcntl.flock(self.guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.guard.close()
            raise ValueError('这段录音缓存正在使用，请等待保存完成') from None
        self.db = None
        self._coverage = None
        try:
            self.db = sqlite3.connect(path)
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('CREATE TABLE IF NOT EXISTS frames (seq INTEGER PRIMARY KEY, data BLOB NOT NULL, digest BLOB NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            self.db.commit()
            if self.meta('conflict'):
                raise ValueError('音频缓存存在完整性冲突，请保留原件供核查')
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        self.guard.close()

    def meta(self, key):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO meta VALUES (?, ?)', (key, str(value)))

    def conflict(self, message):
        self.db.rollback()
        self.set_meta('conflict', message)
        raise ValueError(message)

    def get(self, seq):
        row = self.db.execute('SELECT data,digest FROM frames WHERE seq=?', (seq,)).fetchone()
        if row and hashlib.sha256(row[0]).digest() != row[1]:
            self.conflict('音频缓存校验冲突，未归档')
        return row[0] if row else None

    def put_many(self, frames, fingerprint):
        prior = self.meta('signature')
        if prior is not None and prior != fingerprint:
            self.conflict('同一录音的文件头身份冲突，未归档')
        added = 0
        inserted = []
        try:
            self.db.execute('INSERT OR IGNORE INTO meta VALUES (?, ?)', ('signature', fingerprint))
            for seq, data in frames:
                if not 0 <= seq <= MAX_FRAMES or len(data) != AUDIO:
                    raise ValueError('音频帧长度或序号无效')
                known = self.get(seq)
                if known is not None:
                    if known != data:
                        self.conflict('同一录音的音频帧内容冲突，未归档')
                    continue
                self.db.execute('INSERT INTO frames VALUES (?, ?, ?)', (seq, data, hashlib.sha256(data).digest()))
                added += 1
                inserted.append(seq)
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        if self._coverage is not None:
            for seq in inserted:
                self._cover(seq)
        return added

    def _cover(self, seq):
        ranges = self._coverage
        at = bisect_right(ranges, (seq, MAX_FRAMES + 1))
        start = end = seq
        if at and ranges[at - 1][1] >= seq - 1:
            at -= 1
            start, end = ranges.pop(at)
            end = max(end, seq)
        if at < len(ranges) and ranges[at][0] <= end + 1:
            end = max(end, ranges.pop(at)[1])
        ranges.insert(at, (start, end))

    def coverage(self):
        if self._coverage is None:
            ranges = []
            for (seq,) in self.db.execute('SELECT seq FROM frames ORDER BY seq'):
                if ranges and seq == ranges[-1][1] + 1:
                    ranges[-1] = (ranges[-1][0], seq)
                else:
                    ranges.append((seq, seq))
            self._coverage = ranges
        return self._coverage

    def sequences(self):
        return {row[0] for row in self.db.execute('SELECT seq FROM frames')}

    def count(self):
        return self.db.execute('SELECT COUNT(*) FROM frames').fetchone()[0]

    def bind(self, item):
        duration = item['duration_ms']
        count = duration // 20
        if duration % 20 or not 0 < count <= MAX_FRAMES:
            raise ValueError('闭合录音长度无效')
        identity = f"{duration}:{item.get('ended_at', '')}"
        if self.meta('catalog') not in (None, identity):
            self.conflict('设备目录长度或结束时间冲突，未归档')
        self.set_meta('catalog', identity)
        return count

    def plan(self, count):
        ranges = self.coverage()
        base = self.meta('base')
        if base is None:
            return 0, min((ranges[0][0] if ranges else count) + WITNESS - 1, count - 1), None
        base = int(base)
        end = base + count
        if base not in (0, 1) or (ranges and (ranges[0][0] < base or ranges[-1][1] >= end)):
            self.conflict('本地音频序号与闭合录音范围冲突，未归档')
        cursor = base
        for first, last in ranges:
            if first > cursor:
                return max(base, cursor - WITNESS), min(end - 1, first - 1 + WITNESS), base
            cursor = last + 1
        if cursor < end:
            return max(base, cursor - WITNESS), end - 1, base
        if self.meta('tail') != str(end):
            return max(base, end - WITNESS), end - 1, base
        return None

    def export(self, path, count, cancelled):
        if self.plan(count) is not None:
            raise ValueError('音频缺口或尾部未核验，未归档')
        base = int(self.meta('base'))

        def frames():
            cursor = self.db.execute('SELECT seq,data,digest FROM frames ORDER BY seq')
            try:
                for index, (seq, data, digest) in enumerate(cursor):
                    if cancelled.is_set():
                        raise ValueError('音频归档已取消')
                    if seq != base + index or len(data) != AUDIO or hashlib.sha256(data).digest() != digest:
                        cursor.close()
                        self.conflict('音频缓存校验冲突，未归档')
                    yield data
            finally:
                cursor.close()

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        partial = path.with_suffix('.ogg.part')
        with partial.open('xb') as out:
            for page in iter_ogg(frames(), count):
                out.write(page)
            out.flush()
            os.fsync(out.fileno())
        if cancelled.is_set():
            raise ValueError('音频归档已取消')
        partial.rename(path)
        return path


class SourceWorker:
    """All SQLite operations, including open/close, run on one disk thread.

    Shielding keeps queued writes alive after job cancellation. Exit drains them
    before releasing the source lock; export checks cancellation between frames.
    """
    def __init__(self, cache, serial, ident):
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='bean-cache')
        self.cancelled = threading.Event()
        self.opening = self.pool.submit(cache.source, serial, ident)

    async def __aenter__(self):
        try:
            await asyncio.shield(asyncio.wrap_future(self.opening))
        except BaseException:
            await self.__aexit__()
            raise
        return self

    async def call(self, name, *args):
        check_cancelled()
        def run():
            return getattr(self.opening.result(), name)(*args)
        future = asyncio.wrap_future(self.pool.submit(run))
        try:
            result = await asyncio.shield(future)
            check_cancelled()
            return result
        except asyncio.CancelledError:
            future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            raise

    async def __aexit__(self, *exc):
        self.cancelled.set()
        def close():
            if self.opening.exception() is None:
                self.opening.result().close()
        try:
            await asyncio.shield(asyncio.wrap_future(self.pool.submit(close)))
        finally:
            self.pool.shutdown(wait=False)


class CaptureWriter:
    """Never wait for a disk on the audio loop; overflow is reported, then repaired."""
    def __init__(self, cache, serial, stats, capacity=2000):
        self.cache, self.serial, self.stats = cache, serial, stats
        self.queue = queue.Queue(maxsize=capacity)
        self.closing = threading.Event()
        stats.update(cache_saved_frames=0, cache_dropped_frames=0, cache_error=None)
        self.thread = threading.Thread(target=self._run, name='bean-live-cache', daemon=True)
        self.thread.start()

    def put(self, ident, seq, data, fingerprint):
        if self.stats['cache_error'] or self.closing.is_set():
            self.stats['cache_dropped_frames'] += 1
            return
        try:
            self.queue.put_nowait((ident, seq, data, fingerprint))
        except queue.Full:
            self.stats['cache_dropped_frames'] += 1

    def _run(self):
        source, source_id = None, None
        try:
            while not self.closing.is_set() or not self.queue.empty():
                try:
                    first = self.queue.get(timeout=.1)
                except queue.Empty:
                    continue
                batch = [first]
                for _ in range(127):
                    try:
                        batch.append(self.queue.get_nowait())
                    except queue.Empty:
                        break
                # A hardware rollover can occur in one disk batch. Keep identities separate.
                start = 0
                while start < len(batch):
                    ident, _, _, fingerprint = batch[start]
                    end = start + 1
                    while end < len(batch) and (batch[end][0], batch[end][3]) == (ident, fingerprint):
                        end += 1
                    if source_id != ident:
                        if source:
                            source.close()
                        source = None
                        source = self.cache.source(self.serial, ident)
                        source_id = ident
                    self.stats['cache_saved_frames'] += source.put_many([(s, d) for _, s, d, _ in batch[start:end]], fingerprint)
                    start = end
        except Exception as error:
            self.stats['cache_error'] = str(error) if isinstance(error, ValueError) else '本机音频缓存写入失败，请检查磁盘空间。'
        finally:
            if source:
                source.close()

    def finish(self):
        self.closing.set()

    async def close(self):
        self.finish()
        await asyncio.to_thread(self.thread.join)
