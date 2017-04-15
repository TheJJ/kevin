"""
Job queuing for Kevin.
"""

import asyncio
import functools
import logging

from .config import CFG


class Queue:
    """
    Job queue to manage pending builds and jobs.
    """

    def __init__(self, max_running=1):
        # all builds that are pending
        self.pending_builds = set()

        # build_id -> build
        self.build_ids = dict()

        # jobs that should be run
        self.job_queue = asyncio.Queue(maxsize=CFG.max_jobs_queued)

        # running job futures
        # job -> job_future
        self.jobs = dict()

        # was the execution of the queue cancelled
        self.cancelled = False

        # number of jobs running in parallel
        self.max_running = max_running

    def add_build(self, build):
        """
        Add a build to be processed.
        Called from where a new build was created and should now be run.
        """

        logging.info("[queue] added build: [\x1b[33m%s\x1b[m] @ %s",
                     build.commit_hash,
                     build.clone_url)

        if build in self.pending_builds:
            return

        self.pending_builds.add(build)
        self.build_ids[build.commit_hash] = build

        # send signal to build so it can notify its jobs to add themselves!
        build.enqueue_actions(self)

    def remove_build(self, build):
        """ Remove a finished build """
        del self.build_ids[build.commit_hash]
        self.pending_builds.remove(build)

    def abort_build(self, build_id):
        """ Abort a running build by aborting all pending jobs """

        build = self.build_ids.get(build_id)

        if build:
            if not build.completed:
                build.abort()

    def is_pending(self, commit_hash):
        """ Test if a commit hash is currently being built """
        # TODO: what if a second project wants the same hash?
        #       we can't reuse the build then!
        return commit_hash in self.build_ids.keys()

    def add_job(self, job):
        """ Add a job to the queue """

        if job.completed:
            # don't enqueue completed jobs.
            return

        try:
            # place the job into the pending list.
            self.job_queue.put_nowait(job)

        except asyncio.QueueFull:
            job.error("overloaded; job was dropped.")

    async def process_jobs(self):
        """ process jobs from the queue forever """

        while not self.cancelled:

            if self.job_queue.empty():
                logging.info("[queue] \x1b[32mWaiting for job...\x1b[m")

            # fetch new job from the queue
            job = await self.job_queue.get()

            logging.info("[queue] \x1b[32mProcessing job\x1b[m %s.%s for "
                         "[\x1b[34m%s\x1b[m]...",
                         job.build.project.name,
                         job.name,
                         job.build.commit_hash)

            job_fut = asyncio.get_event_loop().create_task(job.run())

            self.jobs[job] = job_fut

            # register the callback when the job is done
            job_fut.add_done_callback(functools.partial(
                self.job_done, job=job))

            # wait for jobs to complete if there are too many running
            # this can be done very dynamically in the future.
            if len(self.jobs) >= self.max_running or self.cancelled:
                logging.warning("[queue] runlimit of %d reached, "
                                "waiting for completion...", self.max_running)

                # wait until a "slot" is available, then the next job
                # can be processed.
                await asyncio.wait(
                    self.jobs.values(),
                    return_when=asyncio.FIRST_COMPLETED)

    def job_done(self, task, job):
        """ callback for finished jobs """
        del task  # unused

        logging.info("[queue] Job %s.%s finished for [\x1b[34m%s\x1b[m].",
                     job.build.project.name,
                     job.name,
                     job.build.commit_hash)

        try:
            del self.jobs[job]
        except KeyError:
            # TODO: why is the same job callback called?
            logging.error("\x1b[31mBUG\x1b[m: job %s not in running set",
                          job)

    async def cancel(self):
        """ cancel all running jobs """

        to_cancel = len(self.jobs)
        self.cancelled = True

        if to_cancel == 0:
            return

        logging.info("[queue] cancelling running jobs...")

        for job_fut in self.jobs.values():
            job_fut.cancel()

        # wait until all jobs were cancelled
        results = await asyncio.gather(*self.jobs.values(),
                                       return_exceptions=True)

        cancels = [res for res in results if
                   isinstance(res, asyncio.CancelledError)]

        logging.info("[queue] cancelled %d/%d job%s",
                     len(cancels),
                     to_cancel,
                     "s" if to_cancel > 1 else "")

    def cancel_job(self, job):
        """ cancel the given job by accessing its future """

        if job not in self.jobs:
            logging.error("[queue] tried to cancel unknown job: %s", job)
        else:
            self.jobs[job].cancel()
