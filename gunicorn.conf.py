import faulthandler
import os
import sys


loglevel = "debug"
capture_output = True
accesslog = "-"
errorlog = "-"
timeout = 120


def _log(message):
    print(message, file=sys.stderr, flush=True)


def on_starting(server):
    _log(f"[gunicorn.conf] on_starting pid={os.getpid()}")


def when_ready(server):
    _log(f"[gunicorn.conf] when_ready pid={os.getpid()}")


def pre_fork(server, worker):
    _log(
        f"[gunicorn.conf] pre_fork master_pid={os.getpid()} "
        f"worker_age={getattr(worker, 'age', 'unknown')}"
    )


def post_fork(server, worker):
    _log(
        f"[gunicorn.conf] post_fork master_pid={server.pid} "
        f"worker_pid={worker.pid}"
    )
    faulthandler.enable(file=sys.stderr, all_threads=True)
    faulthandler.dump_traceback_later(20, repeat=True, file=sys.stderr)


def post_worker_init(worker):
    _log(f"[gunicorn.conf] post_worker_init worker_pid={worker.pid}")


def worker_exit(server, worker):
    _log(
        f"[gunicorn.conf] worker_exit worker_pid={worker.pid} "
        f"exitcode={getattr(worker, 'exitcode', 'unknown')}"
    )
