# -*- coding: utf-8 -*-
# @Time    : 2026-04-15 17:34:38
# @Author  : yangyuexiong
# @File    : tasks.py

from app.core.config import get_config


async def test_async_task(*args, **kwargs):
    import os
    os.environ["yyx"] = "okc"
    print(f"test_async_task: {os.getenv('yyx')}", args, kwargs)


def test_sync_task(*args, **kwargs):
    import os
    print(f"test_sync_task: {os.getenv('yyx')}", args, kwargs)