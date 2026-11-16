"""Browser-independent, persisted job status with cancellation."""
import asyncio
import uuid

from .storage import now


class JobManager:
    def __init__(self, storage):
        self.storage = storage
        self.tasks = {}
        self.storage.recover_jobs()

    def get(self, job_id):
        return self.storage.get_job(job_id)

    def submit(self, kind, work, metadata=None):
        job = {'id': uuid.uuid4().hex, 'type': kind, 'status': 'queued', 'message': '等待处理',
               'result': None, 'error': None, 'created_at': now(), **(metadata or {})}
        self.storage.save_job(job)
        async def progress(value):
            details = value if isinstance(value, dict) else {'message': str(value)}
            job.update({key: details[key] for key in ('message', 'progress', 'stage') if key in details})
            self.storage.save_job(job)
        async def run():
            try:
                job.update(status='running', message='正在处理')
                self.storage.save_job(job)
                job['result'] = await work(progress)
                job.update(status='completed', message='已完成')
            except asyncio.CancelledError:
                job.update(status='cancelled', message='已取消')
            except Exception as error:
                job.update(status='failed', error=str(error)[:2000] or type(error).__name__, message='处理失败')
            finally:
                job['finished_at'] = now()
                self.storage.save_job(job)
        self.tasks[job['id']] = asyncio.create_task(run())
        self.tasks[job['id']].add_done_callback(lambda task: self.tasks.pop(job['id'], None))
        return dict(job)

    async def cancel(self, job_id):
        job = self.get(job_id)
        task = self.tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # A queued task may be cancelled before its coroutine starts.
            job = self.get(job_id)
            if job['status'] in {'queued', 'running'}:
                job.update(status='cancelled', message='已取消', finished_at=now())
                self.storage.save_job(job)
        return self.get(job_id)

    async def shutdown(self):
        for job_id in list(self.tasks):
            await self.cancel(job_id)
