# -*- coding: utf-8 -*-
# @Time    : 2026-04-15 17:34:38
# @Author  : yangyuexiong
# @File    : lifespan.py

import datetime
import os
import platform
import threading
from contextlib import asynccontextmanager, suppress

from apscheduler.schedulers.base import SchedulerNotRunningError
from fastapi import FastAPI
from loguru import logger

from app.ai.runtime import init_ai_runtime, shutdown_ai_runtime
import app.db.redis_client as redis_module
from app.core.config import get_config
from app.db.redis_client import close_redis_connection_pool, create_redis_connection_pool
from app.db.session import close_db, init_db
from app.tasks.scheduler import scheduler, scheduler_init

project_config = get_config()


def _log_startup_info() -> None:
    fast_api_env = os.getenv("FAST_API_ENV", "development")
    logger.info(">>> startup")
    logger.info("<" + "-" * 66 + ">")
    logger.info(f"时间: {datetime.datetime.now()}")
    logger.info(f"操作系统: {platform.system()}")
    logger.info(f"项目路径: {os.getcwd()}")
    logger.info(f"当前环境: {fast_api_env} (config: {project_config.ENV})")
    logger.info(f"父进程id: {os.getppid()}")
    logger.info(f"子进程id: {os.getpid()}")
    logger.info(f"线程id: {threading.get_ident()}")
    logger.info("<" + "-" * 66 + ">")


async def _init_db() -> None:
    if not project_config.DB_INIT_ON_STARTUP:
        logger.info(">>> 跳过数据库连接池初始化")
        return
    logger.info(">>> Mysql连接池初始化")
    await init_db()


async def _init_redis() -> None:
    if not project_config.REDIS_INIT_ON_STARTUP:
        logger.info(">>> 跳过 Redis 连接池初始化")
        return
    logger.info(">>> Redis连接池初始化")
    await create_redis_connection_pool()
    logger.debug(f"redis_pool: {redis_module.redis_pool!r}")
    logger.info(">>> Redis 连接池初始化完成")


async def _init_scheduler() -> None:
    await scheduler_init()
    logger.info(">>> 定时任务初始化")


async def _shutdown_scheduler() -> None:
    if getattr(scheduler, "running", False):
        try:
            with suppress(SchedulerNotRunningError):
                scheduler.shutdown(wait=False)
            logger.info(">>> 定时任务已关闭")
        except Exception:
            logger.exception(">>> 定时任务关闭失败")
    else:
        logger.info(">>> 定时任务未启动，跳过关闭")


async def _shutdown_redis() -> None:
    if not project_config.REDIS_INIT_ON_STARTUP:
        return
    try:
        await close_redis_connection_pool()
        logger.info(">>> Redis 连接池已关闭")
    except Exception:
        logger.exception(">>> Redis 连接池关闭失败")


async def _shutdown_db() -> None:
    if not project_config.DB_INIT_ON_STARTUP:
        return
    try:
        await close_db()
        logger.info(">>> 数据库连接已关闭")
    except Exception:
        logger.exception(">>> 数据库连接关闭失败")


async def startup_event(app: FastAPI) -> None:
    _log_startup_info()
    logger.info(f">>> Config初始化: {project_config.ENV}")

    try:
        await _init_db()
        await _init_redis()
        await init_ai_runtime(app, project_config)
        # await _init_scheduler()
    except Exception:
        logger.exception("应用启动失败，开始回收资源")
        await shutdown_ai_runtime(app)
        await _shutdown_scheduler()
        await _shutdown_redis()
        await _shutdown_db()
        raise


async def shutdown_event(app: FastAPI) -> None:
    logger.info(">>> shutdown")
    await shutdown_ai_runtime(app)
    # await _shutdown_scheduler()
    await _shutdown_redis()
    await _shutdown_db()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await startup_event(app)
    try:
        yield
    finally:
        await shutdown_event(app)
