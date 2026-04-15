import os

os.environ.setdefault("FAST_API_ENV", "test")
os.environ.setdefault("DB_INIT_ON_STARTUP", "False")
os.environ.setdefault("REDIS_INIT_ON_STARTUP", "False")
