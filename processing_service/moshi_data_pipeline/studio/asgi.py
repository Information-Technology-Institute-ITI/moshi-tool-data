from __future__ import annotations

import os
from pathlib import Path

from moshi_data_pipeline.config import load_config
from moshi_data_pipeline.studio.lifecycle import Ec2LifecycleProvider, LocalLifecycleProvider
from moshi_data_pipeline.studio.server import create_studio_app


def build_app():
    workspace = Path(os.environ.get("MOSHI_WORKSPACE", "/data/studio_workspace"))
    config_value = os.environ.get("MOSHI_CONFIG")
    config_path = Path(config_value) if config_value else None
    instance_id = os.environ.get("MOSHI_GPU_INSTANCE_ID")
    provider = (
        Ec2LifecycleProvider(
            instance_id,
            region_name=os.environ.get("AWS_REGION"),
        )
        if instance_id
        else LocalLifecycleProvider()
    )
    return create_studio_app(
        workspace,
        load_config(config_path),
        start_worker=False,
        start_lifecycle=True,
        lifecycle_provider=provider,
        deployment_generation=os.environ.get("MOSHI_DEPLOYMENT_GENERATION", "local"),
    )


app = build_app()
