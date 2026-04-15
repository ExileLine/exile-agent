# -*- coding: utf-8 -*-
# @Time    : 2026-04-15 17:34:38
# @Author  : yangyuexiong
# @File    : enums.py

from enum import Enum


class UserStatus(str, Enum):
    normal = "正常"
    disable = "禁用"