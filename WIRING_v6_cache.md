# v6 缓存接线说明（**不要自动合并**）

> 🚫 **部署目标：只有 139.155.129.66（用户 ubuntu，私钥 C:\共享\qunzhi-kit\keys\new_server）**
> **禁止**部署到 134.175.145.241（算力节点）；**禁止**整文件覆盖服务器上的 `main_gateway.py`（有未提交改动）。
> 详见 [DEPLOY_TARGET.md](DEPLOY_TARGET.md)。


> 主服务器的 `main_gateway.py` 有你自己的一段代码，**本方案不覆盖它**。
> 只做两件事：①放一个新文件 `review_cache.py`；②你方便时按下面的两处改动手工合并。
> 合并前请先 `cp main_gateway.py main_gateway.py.bak_$(date +%Y%m%d_%H%M%S)`。

## 第一步：放新文件（零冲突，可随时删）

```bash
scp review_cache.py tests/test_review_cache.py ubuntu@139.155.129.66:/tmp/
# 到服务器上
sudo cp /tmp/review_cache.py      <代码目录>/
sudo cp /tmp/test_review_cache.py <代码目录>/tests/
```

`review_cache.py` 是**全新文件**，不改任何现有代码；不接线时它不会被调用，放进去也不影响运行。

自测（不依赖网关，随时可跑）：

```bash
cd <代码目录>
python3 -m pytest -q tests/test_review_cache.py     # 期望 8 passed
```

## 第二步：两处改动（你自己合并）

找到 `/api/review/v6` 的处理函数（函数名 `review_v6_evaluate`，装饰器 `@app.post("/api/review/v6")`），改两处。

### 改动 1／2：函数签名 + 入口缓存检查

改前（大约在你文件里长这样）：

```python
@app.post("/api/review/v6")
def review_v6_evaluate(req: ReviewV6Request):
    """v6 评审：..."""
```

改后（新增 `refresh` 参数；在文档字符串之后、`import review_v6` 之前插入缓存块）：

```python
@app.post("/api/review/v6")
def review_v6_evaluate(req: ReviewV6Request, refresh: bool = False):
    """v6 评审：...（原文档字符串保持不动，可在末尾追加一行说明）"""

    # ---- 内容寻址缓存（新增，失败不影响评审）----
    _cache = None
    cache_key = ""
    try:
        import review_cache as _cache
        cache_key = _cache.key_for(req.report, req.topic, req.baseline)
        if not refresh and cache_key:
            hit = _cache.get(cache_key)
            if hit is not None:
                resp = dict(hit)
                resp["cached"] = True
                resp["cache_age_sec"] = _cache.age_sec(cache_key)
                return resp
    except Exception:
        _cache = None
        cache_key = ""
    # ---- 缓存块结束 ----

    try:
        import review_v6 as _v6
    except Exception as e:
        ...
```

### 改动 2／2：正常返回时写缓存

找到这个函数里的成功返回。**如果你文件里也是 `return {"ok": True, "review_v6": out, ...}` 的组合式返回**，改成先赋值再补缓存：

```python
        resp = {"ok": True, "review_v6": out,
                ...原有的所有字段保持不变...}
        resp["cached"] = False
        # 写缓存失败也要正常返回
        try:
            if _cache is not None and cache_key:
                _cache.put(cache_key, resp)
        except Exception:
            pass
        return resp
```

> 如果你的返回结构不同（比如先构造 `payload` 再 return），只需在 `return` 之前加：
> `payload["cached"] = False`，再 `try: _cache.put(cache_key, payload) except Exception: pass`。

## 第三步：验证

```bash
cd <代码目录>
python3 -m py_compile main_gateway.py && echo COMPILE_OK
# 重启网关后
curl -s -X POST http://127.0.0.1:5020/api/review/v6 \
  -H 'Content-Type: application/json' \
  -d '{"report":"这是一段测试报告，包含一个断言。","topic":"测试"}' | head -c 200
# 第二次同样请求，应看到 "cached": true
```

## 回滚

```bash
cp main_gateway.py.bak_<时间戳> main_gateway.py
# 并删除 review_cache.py（如不再使用）
```

## 说明

- 缓存目录：`<代码目录>/data/review_v6_cache/`，7 天过期，`?refresh=1` 强制重算；
- 缓存键 = `sha256(version|topic|baseline|report)[:24]`，**同一内容同一结果**，内容改一个字就重算；
- 缓存里存的是**完整返回体**（与线上响应一致），不额外落任何日志。
