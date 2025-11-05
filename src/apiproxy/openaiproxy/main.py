# Copyright (c) OpenMMLab. All rights reserved.
import asyncio
import copy
import json
import os
import os.path as osp
import random
import threading
import time
from collections import deque
from http import HTTPStatus
from typing import Deque, Dict, List, Optional
from contextlib import asynccontextmanager
from openaiproxy.services.utils import initialize_services, teardown_services
from rich import print as rprint
from pathlib import Path

import numpy as np
import requests
import uvicorn
import yaml
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel, Field
from requests.exceptions import RequestException

from openaiproxy.logging import logger
from openaiproxy.logging.logger import configure

from openaiproxy.api import (
    apiproxy_v1_router,
    nodemanager_router,
    health_check_router,
    openai_docs_router,
)

def get_lifespan(*, fix_migration=False):

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        configure(async_file=True)
        try:
            await initialize_services(fix_migration=fix_migration)
            yield
        except Exception as exc:
            if "apiproxy migration --fix" not in str(exc):
                logger.exception(exc)
            raise
        finally:
            # Clean shutdown
            logger.info("退出时清理资源...")
            await teardown_services()
            await logger.complete()
            # Final message
            rprint("[bold red]已停止大模型接口服务代理引擎[/bold red]")

    return lifespan

def create_app():
    """Create the FastAPI app and include the router."""
    from .utils.version import get_version_info

    __version__ = get_version_info()["version"]

    rprint(rf'''
 _____     _       _ _____ _
|_   _|_ _(_)_   _(_)  ___| | _____      __
  | |/ _` | | | | | | |_  | |/ _ \ \ /\ / /
  | | (_| | | |_| | |  _| | | (_) \ V  V /
  |_|\__,_|_|\__, |_|_|   |_|\___/ \_/\_/
             |___/
  :: Snz1DP ::            ApiProxy [bold green]v{__version__}[/bold green]
''')

    configure()

    lifespan = get_lifespan()
    app = FastAPI(
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        swagger_ui_oauth2_redirect_url=None,
        title="大模型接口服务代理引擎",
        version=__version__
    )
    origins = ["*"]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 添加v1版路由
    app.include_router(apiproxy_v1_router)
    # 添加节点管理路由
    app.include_router(nodemanager_router)
    # 添加健康检查路由
    app.include_router(health_check_router)
    # 自定义接口文档路由
    app.include_router(openai_docs_router)

    return app

def setup_static_files(app: FastAPI, static_files_dir: Path) -> None:
    """Setup the static files directory.

    Args:
        app (FastAPI): FastAPI app.
        static_files_dir (str): Path to the static files directory.
    """
    app.mount(
        "/",
        StaticFiles(directory=static_files_dir, html=True),
        name="static",
    )


def get_static_files_dir():
    """Get the static files directory relative to Taiyiflow's main.py file."""
    frontend_path = Path(__file__).parent
    return frontend_path / "html"

def setup_app(static_files_dir: Path | None = None, *, backend_only: bool = False) -> FastAPI:
    """Setup the FastAPI app."""
    # get the directory of the current file
    if not static_files_dir:
        static_files_dir = get_static_files_dir()

    if not backend_only and (not static_files_dir or not static_files_dir.exists()):
        msg = f"静态文件目录“{static_files_dir}”不存在"
        raise RuntimeError(msg)
    app = create_app()
    if not backend_only and static_files_dir is not None:
        setup_static_files(app, static_files_dir)
    return app

if __name__ == '__main__':
    from openaiproxy.utils.async_helpers import get_number_of_workers    
    configure()
    uvicorn.run(
        "openaiproxy.main:setup_app",
        host="127.0.0.1",
        port=8008,
        workers=get_number_of_workers(),
        log_level="error",
        reload=True,
        loop="asyncio",
    )
