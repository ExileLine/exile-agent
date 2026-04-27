# -*- coding: utf-8 -*-
# @Time    : 2026-04-15 17:34:38
# @Author  : yangyuexiong
# @File    : __init__.py

# 通用业务模型在这里导入，供 Alembic 自动发现。
# AI 控制面模型位于 app.ai.config_store.models，后续应在 Alembic env 中显式导入，避免包初始化循环。
from app.models.admin import Admin
from app.models.aps_task import ApsTask

__all__ = [
    "Admin",
    "ApsTask",
]