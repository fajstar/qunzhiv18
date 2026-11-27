# -*- coding: utf-8 -*-
"""v6 评审的内容寻址缓存（纯标准库，不依赖 FastAPI）。

目的：同一份报告重复评审不再受 LLM 采样随机性影响（可复现/可核验）。

设计纪律：
  · 读/写失败一律吞掉（get 返回 None，put 返回 False），绝不影响主流程；
  · 原子写：先写同目录临时文件再 os.replace；
  · 键 = sha256(version|topic|baseline|report)[:24]，文件名只允许 [a-f0-9]{24}.json；
  · 不记录任何用户原文到日志（本模块不写日志）。
"""
import hashlib
import json
import os
import re
import time
import uuid

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "data", "review_v6_cache")

DEFAULT_TTL_SEC = 7 * 86400

_KEY_RE = re.compile(r"[a-f0-9]{24}")


def key_for(report: str, topic: str, baseline: str | None, version: str = "v1") -> str:
    """内容寻址键：sha256(version|topic|baseline|report) 的前 24 位十六进制。

    baseline 的 None 与 "" 视为同一语义（无 baseline）。
    任何异常 → 返回 ""（调用方以空键表示"本次不走缓存"）。
    """
    try:
        parts = (str(version or ""), str(topic or ""),
                 str(baseline or ""), str(report or ""))
        raw = "|".join(parts).encode("utf-8", "surrogatepass")
        return hashlib.sha256(raw).hexdigest()[:24]
    except Exception:
        return ""


def _path_for(key: str) -> str | None:
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
        return None
    return os.path.join(CACHE_DIR, key + ".json")


def get(key: str, ttl_sec: int = DEFAULT_TTL_SEC) -> dict | None:
    """命中且未过期 → payload dict；否则 None。读失败一律 None。"""
    try:
        path = _path_for(key)
        if path is None:
            return None
        age = time.time() - os.stat(path).st_mtime
        if ttl_sec <= 0 or age >= ttl_sec:
            return None
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def put(key: str, payload: dict) -> bool:
    """原子写入（临时文件 + os.replace）；失败返回 False，绝不抛异常。"""
    tmp = None
    try:
        path = _path_for(key)
        if path is None or not isinstance(payload, dict):
            return False
        os.makedirs(CACHE_DIR, exist_ok=True)
        data = json.dumps(payload, ensure_ascii=False)
        tmp = "%s.%s.tmp" % (path, uuid.uuid4().hex[:8])
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            if tmp:
                os.remove(tmp)
        except Exception:
            pass
        return False


def age_sec(key: str) -> int | None:
    """缓存文件的年龄（秒，向下取整且不小于 0）；不存在或读失败返回 None。"""
    try:
        path = _path_for(key)
        if path is None:
            return None
        return max(0, int(time.time() - os.stat(path).st_mtime))
    except Exception:
        return None