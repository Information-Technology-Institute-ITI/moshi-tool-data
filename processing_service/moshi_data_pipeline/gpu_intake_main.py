from __future__ import annotations

import logging
import os

import uvicorn

from moshi_data_pipeline.gpu_dispatch_protocol import GPU_DISPATCH_PROTOCOL_VERSION
from moshi_data_pipeline.gpu_intake import GpuIntakeSettings, create_gpu_intake_app
from moshi_data_pipeline.logging_utils import configure_logging

LOGGER = logging.getLogger(__name__)


def main() -> None:
    os.umask(0o077)
    settings = GpuIntakeSettings.from_environment()
    log_path = settings.cache_root / "logs" / "gpu-intake.log"
    configure_logging(log_path)
    LOGGER.info(
        "GPU intake protocol %s build %s is starting on %s:%d",
        GPU_DISPATCH_PROTOCOL_VERSION,
        settings.build_id,
        settings.host,
        settings.port,
    )
    uvicorn.run(
        create_gpu_intake_app(settings),
        host=settings.host,
        port=settings.port,
        access_log=True,
        log_config=None,
        proxy_headers=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
