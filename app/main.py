# -*- coding: utf-8 -*-
# @Time    : 2026-04-15 17:34:38
# @Author  : yangyuexiong
# @File    : main.py

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.router import api_router
from app.core.config import get_config
from app.core.exception_handlers import register_exception_handlers
from app.core.lifespan import lifespan
from app.core.middleware import MyMiddleware

project_config = get_config()


def create_app():
    debug = project_config.DEBUG
    kw = {
        "debug": debug
    }

    app = FastAPI(
        title=project_config.DOCS_TITLE,
        description=project_config.DOCS_DESCRIPTION,
        summary=project_config.DOCS_SUMMARY,
        version=project_config.DOCS_VERSION,
        openapi_url=project_config.DOCS_OPENAPI_URL,
        lifespan=lifespan,  # 事件注册(应用启动前与关闭后执行的事件处理器)
        **kw
    )

    # 跨域: 如果 allow_credentials=True，则 allow_origins 不能设置为 ["*"]，必须明确指定允许的域名。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"]
    )

    app.add_middleware(
        MyMiddleware,
        log_headers=debug,
        log_body=debug,
        exclude_paths=["/docs", "/openapi.json", "/redoc", "/static*"],
        sensitive_headers=project_config.SENSITIVE_HEADERS,
        mask_sensitive_headers=project_config.MASK_SENSITIVE_HEADERS,
    )

    # 异常处理器注册
    register_exception_handlers(app, debug)

    # 路由注册
    app.include_router(api_router)

    # 静态资源(生产环境通过配置获取路径)
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/")
    async def root_redirect():
        return {
            "status": "ok",
        }

    return app


app = create_app()