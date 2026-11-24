# -*- coding: utf-8 -*-
"""快照 id 测试：同一内容同 hash、改一行必变、只 touch 不变

这些用例同时验证：
  · `snapshot_hash.snapshot_id(wait=True)` 的严格内容 hash；
  · `_data_snapshot()` 已切到新实现（返回 paperlib- 前缀，且不因 mtime 伪造而变）。
"""
import os
import sqlite3
import time

import snapshot_hash


def _make_db(path, n=50):
    c = sqlite3.connect(path)
    c.execute("create table t(a integer primary key, b text)")
    c.executemany("insert into t(b) values(?)", [("row-%d" % i,) for i in range(n)])
    c.commit()
    c.close()


def test_same_content_same_id(tmp_path):
    db = str(tmp_path / "a.sqlite")
    _make_db(db)
    cache = str(tmp_path / "cache.json")
    a = snapshot_hash.snapshot_id([db], cache, wait=True)
    b = snapshot_hash.snapshot_id([db], cache, wait=True)
    assert a == b
    assert a.startswith("paperlib-")


def test_one_row_change_changes_id(tmp_path):
    db = str(tmp_path / "a.sqlite")
    _make_db(db)
    cache = str(tmp_path / "cache.json")
    a = snapshot_hash.snapshot_id([db], cache, wait=True)
    c = sqlite3.connect(db)
    c.execute("update t set b='changed' where a=7")
    c.commit()
    c.close()
    b = snapshot_hash.snapshot_id([db], cache, wait=True)
    assert b != a


def test_touch_only_does_not_change_id(tmp_path):
    db = str(tmp_path / "a.sqlite")
    _make_db(db)
    cache = str(tmp_path / "cache.json")
    a = snapshot_hash.snapshot_id([db], cache, wait=True)
    # 旧实现用 mtime：touch 会变 id；新实现必须不变
    os.utime(db, None)
    b = snapshot_hash.snapshot_id([db], cache, wait=True)
    assert b == a


def test_multi_file_set_is_order_independent(tmp_path):
    d1 = tmp_path / "one.bin"
    d2 = tmp_path / "two.bin"
    d1.write_bytes(b"aaa")
    d2.write_bytes(b"bbb")
    cache = str(tmp_path / "cache.json")
    a = snapshot_hash.snapshot_id([str(d1), str(d2)], cache, wait=True)
    b = snapshot_hash.snapshot_id([str(d2), str(d1)], cache, wait=True)
    assert a == b


def test_pending_then_final(tmp_path):
    db = str(tmp_path / "a.sqlite")
    _make_db(db)
    cache = str(tmp_path / "cache.json")
    pending = snapshot_hash.snapshot_id([db], cache, wait=False)
    assert pending.startswith("paperlib-pending-")
    final = None
    for _ in range(50):
        time.sleep(0.1)
        final = snapshot_hash.snapshot_id([db], cache, wait=True)
        if not final.startswith("paperlib-pending-"):
            break
    assert final and not final.startswith("paperlib-pending-")
    assert snapshot_hash.snapshot_id([db], cache, wait=True) == final


@__import__("pytest").mark.db
def test_data_snapshot_uses_content_hash(client, monkeypatch):
    import main_gateway
    import snapshot_hash as sh
    seen = {}

    def fake(paths, **kw):
        seen["paths"] = list(paths)
        seen["wait"] = kw.get("wait")
        return "paperlib-fake-content"

    monkeypatch.setattr(sh, "snapshot_id", fake)
    main_gateway._API_SNAPSHOT_CACHE.update({"ts": 0.0, "id": ""})
    a = main_gateway._data_snapshot()
    assert a == "paperlib-fake-content"
    assert seen["wait"] is False          # 请求路径不能同步读 12GB
    assert any("paperlib.sqlite" in p for p in seen["paths"])
    # 5 分钟缓存内必须稳定，且不再调用哈希
    assert main_gateway._data_snapshot() == a
