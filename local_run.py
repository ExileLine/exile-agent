# -*- coding: utf-8 -*-
# @Time    : 2026-04-15 17:34:38
# @Author  : yangyuexiong
# @File    : local_run.py

import uvicorn


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=7769, reload=True)