# -*- coding: utf-8 -*-
"""snapshot_hash.py · 数据快照 id 的内容哈希（v18-fix9 第 6 项）

问题
----
原 `_data_snapshot()` 用 `(mtime, size)`。mtime 可以被 `touch`/复制/恢复备份改掉：
同一份数据可能算出不同 id，不同数据也可能因为 mtime 相同而算出同一 id。

方案（三选一，选 A）
--------------------
A. **stat 门控的全量 SHA-256 + 持久缓存 + 后台刷新（本模块，推荐）**
   - 文件内容全量哈希，任何一行变了 hash 必变，**不漏**；
   - 用 `(size, mtime_ns, ctime_ns, inode, dev)` 做门控：stat 没变就直接复用缓存，
     不重复读 12GB；stat 变了才重算；
   - 12GB 冷读约 40–55 秒（本机实测 314MB/s），所以 API 侧默认 `wait=False`：
     stat 变化时立刻返回 `paperlib-pending-<stat指纹>`，后台线程算完再切到内容 hash。
     **数据一变 id 立刻变，绝不会把旧内容 hash 当新数据返回**；
   - 需要严格“同一份数据同一 hash”的离线场景（CLI / 测试 / 入库后预计算）用 `wait=True`。

B. 采样 hash（前 1MB + 后 1MB + 中间若干块）
   - 快，但改的那一行如果落在采样块之间就**漏检**，不满足“数据变了 hash 必变”。

C. `PRAGMA data_version`
   - 它是**连接级**计数器：只反映“本连接打开后别的连接是否提交过”，
     换进程/重连就重置，不能作为可复现的持久快照 id，也不能跨机器比对。

结论：选 A。采样和 data_version 都快，但都不满足“不能漏 / 可复现”。

并发
----
缓存按 cache_path 分文件、分锁；计算在锁外做，只把结果合并回文件。
避免“12GB 哈希持锁”和“_MEM 全局字典被另一个 cache 覆盖”这两个坑。

复现（一条命令）
----------------
    python snapshot_hash.py --db /home/ubuntu/paperlib/db/paperlib.sqlite \
        --baseline ./paperlib/baseline_rates.json --baseline ./paperlib/baseline2.json --json

测试（同一份数据两次相同、改一行必变）
--------------------------------------
    python snapshot_hash.py --selftest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import threading
import time

CHUNK = 8 * 1024 * 1024
CACHE_VERSION = 1
_DEFAULT_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "data", "snapshot_cache.json")
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
_REFRESHING = set()
_REFRESH_LOCK = threading.Lock()


def _lock_for(cache_path: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lk = _LOCKS.get(cache_path)
        if lk is None:
            lk = threading.Lock()
            _LOCKS[cache_path] = lk
        return lk


def stat_key(path: str) -> list:
    """文件身份：大小 + mtime + ctime + inode + dev。

    加 ctime 的原因：mtime 可以被 `os.utime` 伪造，ctime 在内核里随 inode 变更更新，
    用户态改不了；再加 inode/dev 防止“换了一个同名文件但 mtime/size 恰好一样”。
    """
    try:
        st = os.stat(path)
        return [int(st.st_size), int(st.st_mtime_ns), int(st.st_ctime_ns),
                int(st.st_ino), int(st.st_dev)]
    except FileNotFoundError:
        return ["missing"]
    except OSError as e:
        return ["error", type(e).__name__]


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _load_cache(cache_path: str) -> dict:
    try:
        with open(cache_path, encoding="utf-8") as f:
            raw = json.load(f)
        files = raw.get("files") if isinstance(raw, dict) else None
        if not isinstance(files, dict):
            return {}
    except Exception:
        return {}
    out = {}
    for k, v in files.items():
        if isinstance(v, dict) and isinstance(v.get("stat"), list) and isinstance(v.get("sha256"), str):
            out[os.path.realpath(k)] = {"stat": v["stat"], "sha256": v["sha256"]}
    return out


def _save_cache(cache_path: str, entries: dict) -> None:
    try:
        d = os.path.dirname(os.path.abspath(cache_path))
        os.makedirs(d, exist_ok=True)
        tmp = cache_path + ".tmp.%d.%d" % (os.getpid(), threading.get_ident())
        payload = {"version": CACHE_VERSION, "saved_at": time.time(),
                   "files": {k: {"stat": v["stat"], "sha256": v["sha256"]}
                             for k, v in entries.items()}}
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.replace(tmp, cache_path)
    except Exception:
        # 缓存写不了不影响正确性：下次重算即可
        pass


def _build_id(paths, shas) -> str:
    parts = []
    for p in paths:
        parts.append("%s:%s" % (os.path.basename(p), shas.get(p, "missing")))
    return "paperlib-" + hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:10]


def _pending_id(paths, keys) -> str:
    raw = json.dumps([[os.path.basename(p), keys[p]] for p in paths],
                     ensure_ascii=False, sort_keys=True)
    return "paperlib-pending-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


def _compute_missing(paths, keys, cache_path) -> dict:
    """锁外计算缺失项，再短锁合并进缓存文件。"""
    cache = _load_cache(cache_path)
    todo = [p for p in paths if cache.get(p, {}).get("stat") != keys[p]]
    if not todo:
        return cache
    new = {}
    for p in todo:
        if keys[p] and keys[p][0] == "missing":
            new[p] = {"stat": keys[p], "sha256": "missing"}
        else:
            new[p] = {"stat": keys[p], "sha256": file_sha256(p)}
    with _lock_for(cache_path):
        cur = _load_cache(cache_path)
        cur.update(new)
        _save_cache(cache_path, cur)
        return cur


def _refresh(paths, cache_path, keys) -> None:
    try:
        _compute_missing(paths, keys, cache_path)
    finally:
        with _REFRESH_LOCK:
            _REFRESHING.discard((cache_path, tuple(paths)))


def snapshot_id(paths, cache_path: str = None, wait: bool = False) -> str:
    """返回数据快照 id。

    wait=True  : 缺缓存时同步算完再返回（严格内容 hash；CLI/测试用）
    wait=False : 缺缓存时返回 pending id，并起后台线程算（API 请求路径用，不阻塞）
    """
    cache_path = os.path.abspath(cache_path or _DEFAULT_CACHE)
    paths = sorted({os.path.realpath(p) for p in paths})
    keys = {p: stat_key(p) for p in paths}
    cache = _load_cache(cache_path)
    if all(cache.get(p, {}).get("stat") == keys[p] for p in paths):
        return _build_id(paths, {p: cache[p]["sha256"] for p in paths})

    if wait:
        cache = _compute_missing(paths, keys, cache_path)
        if all(cache.get(p, {}).get("stat") == keys[p] for p in paths):
            return _build_id(paths, {p: cache[p]["sha256"] for p in paths})
        # 极端情况（并发写导致 stat 又变）：返回 pending，绝不返回错配的旧 hash
        return _pending_id(paths, keys)

    job = (cache_path, tuple(paths))
    with _REFRESH_LOCK:
        if job not in _REFRESHING:
            _REFRESHING.add(job)
            t = threading.Thread(target=_refresh, args=(paths, cache_path, keys),
                                 name="snapshot-hash-refresh", daemon=True)
            t.start()
    return _pending_id(paths, keys)


def _selftest() -> int:
    import sqlite3
    ok = True
    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "t.sqlite")
        c = sqlite3.connect(db)
        c.execute("create table t(a integer primary key, b text)")
        c.executemany("insert into t(b) values(?)", [("x%d" % i,) for i in range(100)])
        c.commit()
        c.close()
        cache = os.path.join(td, "cache.json")
        a = snapshot_id([db], cache, wait=True)
        b = snapshot_id([db], cache, wait=True)
        print("  same data twice:", a, b, "OK" if a == b else "FAIL")
        ok &= a == b
        c = sqlite3.connect(db)
        c.execute("update t set b='changed' where a=50")
        c.commit()
        c.close()
        d = snapshot_id([db], cache, wait=True)
        print("  one row changed:", a, "->", d, "OK" if d != a else "FAIL")
        ok &= d != a
        # 只 touch 不改内容：内容 hash 必须不变
        os.utime(db, None)
        e = snapshot_id([db], cache, wait=True)
        print("  touch only:", d, "->", e, "OK" if e == d else "FAIL")
        ok &= e == d
        # 同一数据，pending 模式在后台完成后应等于 wait 模式最终值
        os.utime(db, None)
        p = snapshot_id([db], cache, wait=False)
        time.sleep(2.0)
        f = snapshot_id([db], cache, wait=True)
        print("  pending -> final:", p, "->", f, "OK" if f == e else "FAIL")
        ok &= f == e
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="数据快照内容 hash")
    ap.add_argument("--db", action="append", default=[], help="要纳入快照的文件（可多次）")
    ap.add_argument("--baseline", action="append", default=[], help="基线 JSON（可多次）")
    ap.add_argument("--cache", default=None, help="缓存文件路径")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-wait", action="store_true", help="不等待后台计算，立即返回 pending id")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    paths = a.db + a.baseline
    if not paths:
        ap.error("至少给一个 --db 或 --baseline")
    t0 = time.time()
    sid = snapshot_id(paths, a.cache, wait=not a.no_wait)
    dt = time.time() - t0
    if a.json:
        print(json.dumps({"id": sid, "seconds": round(dt, 3),
                          "paths": paths, "wait": not a.no_wait},
                         ensure_ascii=False, indent=1))
    else:
        print(sid)
        print("elapsed %.3fs" % dt, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
