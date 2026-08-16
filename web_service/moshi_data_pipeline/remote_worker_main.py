from __future__ import annotations

import logging
import os
import signal
import socket
import threading
from pathlib import Path
from time import sleep
from uuid import uuid4

from moshi_data_pipeline.logging_utils import configure_logging
from moshi_data_pipeline.remote_worker import HttpWorkerApi, RemoteWorker, WorkerIdentity
from moshi_data_pipeline.studio.execution_runtime import ContextJobExecutor

LOGGER = logging.getLogger(__name__)


def main() -> None:
    configure_logging()
    base_url = os.environ.get("MOSHI_WEB_INTERNAL_URL", "").strip()
    token = os.environ.get("MOSHI_WORKER_TOKEN", "")
    if not base_url or not token:
        raise RuntimeError("MOSHI_WEB_INTERNAL_URL and MOSHI_WORKER_TOKEN are required")
    identity = WorkerIdentity(
        worker_id=os.environ.get("MOSHI_WORKER_ID", socket.gethostname()),
        boot_id=os.environ.get("MOSHI_WORKER_BOOT_ID", uuid4().hex),
        build_id=os.environ.get("MOSHI_BUILD_ID", "development"),
    )
    worker = RemoteWorker(
        HttpWorkerApi(base_url, token),
        ContextJobExecutor,
        Path(os.environ.get("MOSHI_WORKER_CACHE", "/cache")),
        identity,
    )
    stopped = threading.Event()

    def request_stop(*_: object) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    LOGGER.info("Remote worker %s build %s is starting", identity.worker_id, identity.build_id)
    while not stopped.is_set():
        processed = worker.run_once()
        if not processed:
            sleep(0.75)


if __name__ == "__main__":
    main()
