from __future__ import print_function

import itertools
import os
import socket
import time
from Queue import Empty
from datetime import timedelta

from haydi.base.exception import HaydiException, TimeoutException

try:
    from distributed import Client, LocalCluster
    from distributed.http import HTTPScheduler

    from .scheduler import JobScheduler
    from .util import haydi_logger, ResultSaver, ProgressLogger, TimeoutManager

    distributed_import_error = None
except Exception as e:
    distributed_import_error = e


class DistributedComputation(object):
    def __init__(self, scheduler, timeout):
        self.scheduler = scheduler
        self.timeout_mgr = TimeoutManager(timeout) if timeout else None
        self.callbacks = []

    def add_callback(self, fn):
        self.callbacks.append(fn)

    def iterate_jobs(self):
        self.scheduler.start()

        jobs = []
        try:
            while not (self.scheduler.completed or self.scheduler.canceled):
                if self._is_timeouted():
                    raise TimeoutException()

                try:
                    job = self.scheduler.job_queue.get(block=False)
                    jobs.append(job)

                    self._on_job_completed(job)
                except Empty:
                    time.sleep(3)

            # extract remaining jobs
            while True:
                try:
                    jobs.append(self.scheduler.job_queue.get(block=False))
                except Empty:
                    break

        except KeyboardInterrupt:
            pass
        except TimeoutException:
            haydi_logger.info("Run timeouted after {} seconds".format(
                self.timeout_mgr.get_time_from_start()))

        self.scheduler.stop()

        # order the results
        jobs.sort(key=lambda job: job.start_index)

        return jobs

    def _on_job_completed(self, job):
        for cb in self.callbacks:
            cb(self.scheduler, job)

    def _is_timeouted(self):
        return self.timeout_mgr and self.timeout_mgr.is_finished()


class DistributedContext(object):
    """
    Parallel context that uses the
    `distributed <http://distributed.readthedocs.io>`_ library to distribute
    work amongst workers in a cluster to speed up the computation.

    It can either connect to an already running cluster or create a local one.
    If a local cluster is created, every worker will be spawned in a single
    process with one thread.

    Partial results can be saved to disk during the computation
    to avoid losing all results if the program ends abruptly.

    Args:
        ip (string): IP address of a distributed cluster
        port (int): TCP port of a distributed cluster
        spawn_workers (int):
            - If `spawn_workers` is ``0``
                - connect to an existing cluster located at (ip, port)
            - If `spawn_workers` is ``n``
                - create a local cluster with ``n`` workers
        write_partial_results (int):
            - If `write_partial_results` is ``None``
                - no partial results can be saved
            - If `write_partial_results` is ``n``
                - partial results are saved after every ``n-th`` job
    """

    def __init__(self,
                 ip="127.0.0.1",
                 port=8787,
                 spawn_workers=0,
                 write_partial_results=None):
        """

        :type ip: string
        :param ip: IP of distributed scheduler
        :type port: int
        :param port: port of distributed scheduler
        :type spawn_workers: int
        :param spawn_workers: True if a computation cluster should be spawned
        :type write_partial_results: int
        :param write_partial_results:
            n -> every n jobs a temporary result will be saved to disk
            None -> no temporary results will be stored
        """

        if distributed_import_error:
            raise HaydiException("distributed must be properly installed in"
                                 "order to use the DistributedContext\n"
                                 "Error:\n{}"
                                 .format(distributed_import_error))

        self.worker_count = spawn_workers
        self.ip = ip
        self.port = port
        self.active = False
        self.write_partial_results = write_partial_results
        self.execution_count = 0

        if spawn_workers > 0:
            self.cluster = LocalCluster(ip=ip,
                                        scheduler_port=port,
                                        n_workers=spawn_workers,
                                        threads_per_worker=1,
                                        diagnostics_port=None,
                                        services={
                                            ("http", port + 1): HTTPScheduler
                                        })
            self.executor = Client(self.cluster)
        else:
            self.executor = Client((ip, port))

    def run(self, domain,
            worker_reduce_fn, worker_reduce_init,
            global_reduce_fn, global_reduce_init,
            timeout=None):
        size = domain.steps

        name = "{} (pid {})".format(socket.gethostname(), os.getpid())
        start_msg = "Starting run with size {} and worker count {} on {}".\
            format(size, self._get_worker_count(), name)

        haydi_logger.info(start_msg)

        scheduler = JobScheduler(self.executor,
                                 self._get_worker_count(),
                                 timeout, domain,
                                 worker_reduce_fn, worker_reduce_init)

        computation = DistributedComputation(scheduler, timeout)

        if self.write_partial_results is not None:
            result_saver = ResultSaver(self.execution_count,
                                       self.write_partial_results)
            computation.add_callback(result_saver.handle_job)

        progress_logger = ProgressLogger(timedelta(seconds=10))
        computation.add_callback(progress_logger.handle_job)

        jobs = computation.iterate_jobs()
        self.execution_count += 1

        results = [job.result for job in jobs]

        if worker_reduce_fn is None:
            results = list(itertools.chain.from_iterable(results))

        if size:
            results = results[:domain.size]  # trim results to required size

        haydi_logger.info("Finished run with size {}".format(domain.size))
        haydi_logger.info("Iterated {} elements".format(
            sum([job.size for job in jobs])))

        if global_reduce_fn is None or len(results) == 0:
            return results
        else:
            if global_reduce_init is None:
                return reduce(global_reduce_fn, results)
            else:
                return reduce(global_reduce_fn, results, global_reduce_init())

    def _get_worker_count(self):
        workers = 0
        for name, value in self.executor.ncores().items():
            workers += value

        if workers == 0:
            raise HaydiException("There are no workers")

        return workers
