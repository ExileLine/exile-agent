# -*- coding: utf-8 -*-
# @Time    : 2026-04-15 17:34:38
# @Author  : yangyuexiong
# @File    : config.py

import os
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from loguru import logger
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_ENV = "development"
ENV_FILE_ENV_VAR = "ENV_FILE"


def normalize_env(env: Optional[str]) -> str:
    value = (env or "").strip().lower()
    mapping = {
        "dev": "development",
        "development": "development",
        "test": "test",
        "testing": "test",
        "prod": "production",
        "production": "production",
        "stage": "staging",
        "staging": "staging",
    }
    return mapping.get(value, value or DEFAULT_ENV)


ENV_FILE_MAP = {
    "development": ".env.development",
    "test": ".env.test",
    "production": ".env.production",
    "staging": ".env.staging",
}


def resolve_env_files(env_name: str) -> List[Path]:
    if env_name not in ENV_FILE_MAP:
        raise ValueError(f"Unsupported ENV: {env_name}")

    paths: List[Path] = []

    explicit = os.getenv(ENV_FILE_ENV_VAR)
    if explicit:
        explicit_path = Path(explicit)
        if not explicit_path.is_absolute():
            explicit_path = BASE_DIR / explicit_path
        if explicit_path.exists():
            paths.append(explicit_path)
        else:
            raise FileNotFoundError(f"{ENV_FILE_ENV_VAR} 指定的文件不存在: {explicit_path}")

    base_env = BASE_DIR / ".env"
    if base_env.exists():
        paths.append(base_env)

    env_specific = BASE_DIR / ENV_FILE_MAP[env_name]
    if env_specific.exists():
        paths.append(env_specific)

    if paths:
        return paths

    raise FileNotFoundError(
        f"未找到配置文件，请创建 {ENV_FILE_MAP.get(env_name)} 或设置 {ENV_FILE_ENV_VAR}"
    )


BASE_SETTINGS_CONFIG = SettingsConfigDict(
    env_file_encoding="utf-8",
    case_sensitive=False,
    extra="ignore",
)


class BaseConfig(BaseSettings):
    model_config = BASE_SETTINGS_CONFIG

    DOCS_TITLE: str = "exile-agent"
    DOCS_DESCRIPTION: str = "description"
    DOCS_SUMMARY: str = "I hope every day is fulfilling, and everything follows best practices."
    DOCS_VERSION: str = "1.0.0"
    DOCS_OPENAPI_URL: str = "/api/v1/openapi.json"

    APP_NAME: str = "yangyuexiong"
    ENV: str = "development"
    DB_BACKEND: str = "mysql"
    SECRET_KEY: str = "change-me"
    DEBUG: bool = True
    RUN_HOST: str = "0.0.0.0"
    RUN_PORT: int = 8000

    MYSQL_HOSTNAME: str = "127.0.0.1"
    MYSQL_USERNAME: str = "root"
    MYSQL_PASSWORD: str = "password"
    MYSQL_PORT: int = 3306
    MYSQL_DATABASE: str = "exile_agent"

    POSTGRESQL_HOSTNAME: str = "127.0.0.1"
    POSTGRESQL_USERNAME: str = "postgres"
    POSTGRESQL_PASSWORD: str = "password"
    POSTGRESQL_PORT: int = 5432
    POSTGRESQL_DATABASE: str = "exile_agent"

    REDIS_HOST: str = "127.0.0.1"
    REDIS_PORT: int = 6379
    REDIS_PWD: str = ""
    REDIS_DB: int = 0
    DECODE_RESPONSES: bool = True
    CELERY_BROKER_URL: Optional[str] = None
    CELERY_RESULT_BACKEND: Optional[str] = None
    CELERY_TASK_QUEUE: str = "exile_agent_task_queue"

    SENSITIVE_HEADERS: str = "authorization,cookie,set-cookie,x-api-key"

    @property
    def ENV_NAME(self) -> str:
        return normalize_env(self.ENV)

    @property
    def IS_DEV(self) -> bool:
        return self.ENV_NAME == "development"

    @property
    def IS_TEST(self) -> bool:
        return self.ENV_NAME == "test"

    @property
    def IS_PROD(self) -> bool:
        return self.ENV_NAME == "production"

    @property
    def IS_STAGING(self) -> bool:
        return self.ENV_NAME == "staging"

    @property
    def MASK_SENSITIVE_HEADERS(self) -> bool:
        return self.IS_PROD or self.IS_STAGING

    @property
    def redis_url(self) -> str:
        auth = f":{self.REDIS_PWD}@" if self.REDIS_PWD else ""
        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}?decode_responses={self.DECODE_RESPONSES}"

    @property
    def redis_transport_url(self) -> str:
        auth = f":{self.REDIS_PWD}@" if self.REDIS_PWD else ""
        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    @property
    def celery_broker_url(self) -> str:
        return self.CELERY_BROKER_URL or self.redis_transport_url

    @property
    def celery_result_backend(self) -> str:
        return self.CELERY_RESULT_BACKEND or self.redis_transport_url

    @property
    def mysql_url(self) -> str:
        return "mysql://{}:{}@{}:{}/{}".format(
            self.MYSQL_USERNAME,
            self.MYSQL_PASSWORD,
            self.MYSQL_HOSTNAME,
            self.MYSQL_PORT,
            self.MYSQL_DATABASE
        )

    @property
    def mysql_async_url(self) -> str:
        return "mysql+asyncmy://{}:{}@{}:{}/{}".format(
            self.MYSQL_USERNAME,
            self.MYSQL_PASSWORD,
            self.MYSQL_HOSTNAME,
            self.MYSQL_PORT,
            self.MYSQL_DATABASE
        )

    @property
    def pg_url(self) -> str:
        return "postgres://{}:{}@{}:{}/{}".format(
            self.POSTGRESQL_USERNAME,
            self.POSTGRESQL_PASSWORD,
            self.POSTGRESQL_HOSTNAME,
            self.POSTGRESQL_PORT,
            self.POSTGRESQL_DATABASE
        )

    @property
    def pg_async_url(self) -> str:
        return "postgresql+asyncpg://{}:{}@{}:{}/{}".format(
            self.POSTGRESQL_USERNAME,
            self.POSTGRESQL_PASSWORD,
            self.POSTGRESQL_HOSTNAME,
            self.POSTGRESQL_PORT,
            self.POSTGRESQL_DATABASE
        )

    @property
    def sqlalchemy_database_url(self) -> str:
        backend = (self.DB_BACKEND or "mysql").lower()
        if backend == "mysql":
            return self.mysql_async_url
        if backend in {"postgres", "postgresql"}:
            return self.pg_async_url
        raise ValueError(f"Unsupported DB_BACKEND: {self.DB_BACKEND}")


@lru_cache
def get_config(env: Optional[str] = None) -> BaseConfig:
    fast_api_env = normalize_env(env or os.getenv("FAST_API_ENV", DEFAULT_ENV))
    env_files = resolve_env_files(fast_api_env)
    logger.info(f"加载配置: env={fast_api_env}, files={[str(p) for p in env_files]}")
    conf = BaseConfig(_env_file=[str(p) for p in env_files])
    if conf.ENV_NAME != fast_api_env:
        logger.warning(f"ENV 不一致：FAST_API_ENV={fast_api_env}, 配置 ENV={conf.ENV_NAME}")
    return conf


if __name__ == "__main__":
    dev_config = get_config("development")
    print(dev_config.model_dump())
    print(dev_config.APP_NAME)
    print(dev_config.MYSQL_HOSTNAME)

    pro_config = get_config("production")
    print(pro_config.model_dump())