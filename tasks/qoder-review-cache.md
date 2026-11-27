# Qoder 任务：给 v6 评审加「内容寻址缓存」（解决同一篇两次结果不同）

## 背景

`/api/review/v6` 每次调用都会走 LLM（`review_v6.v6_review`），temperature 0.5/0.6 且无缓存，
所以**同一份报告连续评两次，问题清单不完全一致**——这与项目"可复现/可核验"的主张冲突。

## 方案（低风险：新增独立模块 + 一处接线）

### 1. 新模块 `review_cache.py`（纯函数，不依赖 FastAPI）

```python
CACHE_DIR = os.path.join(os.path.dirname(__file__), "data", "review_v6_cache")

def key_for(report: str, topic: str, baseline: str | None, version: str = "v1") -> str:
    """内容寻址：sha256(version|topic|baseline|report)[:24]"""

def get(key: str, ttl_sec: int = 7 * 86400) -> dict | None:
    """命中且未过期 → 返回 payload dict；否则 None。读失败一律返回 None（不影响主流程）"""

def put(key: str, payload: dict) -> bool:
    """原子写入（先写 .tmp 再 replace）；失败返回 False，绝不抛异常"""

def age_sec(key: str) -> int | None:
    """缓存文件的年龄（秒）；不存在返回 None"""
```

要求：
- 目录不存在自动创建；写失败不能影响主流程（捕获所有异常）；
- 文件名只允许 `[a-f0-9]{24}.json`；
- 不记录任何用户原文到日志。

### 2. 接线（只改 `/api/review/v6` 处理函数）

- 函数签名加 `refresh: bool = False`（查询参数 `?refresh=1` 可强制重算）；
- 入口先算 `key = review_cache.key_for(req.report, req.topic, req.baseline)`；
- 若 `not refresh` 且 `hit := review_cache.get(key)`：直接返回 `hit`，并附
  `{"cached": true, "cache_age_sec": review_cache.age_sec(key)}`；
- 若未命中：正常跑 `_v6.v6_review(...)`，成功后在返回体加 `{"cached": false}`，
  并 `review_cache.put(key, 返回值)`（**写缓存失败也照常返回**）；
- 不改任何现有字段语义，只**新增** `cached` / `cache_age_sec`。

### 3. 测试 `tests/test_review_cache.py`（不联网、不依赖网关）

至少 5 条：
1. `key_for` 同输入 → 同 key；report 改一个字 → key 变；
2. `put` 后 `get` 能取回同样内容；
3. `ttl_sec=0` 时 `get` 返回 None（过期）；
4. 损坏的缓存文件（写入非法 JSON）→ `get` 返回 None 且不抛异常；
5. `key_for` 对 None baseline 与 "" baseline 的处理一致（都视为无 baseline）。

## 硬约束

- 不改 `review_v6.py` 的评审逻辑；
- 不改其它接口；
- 不引入第三方依赖（只用标准库）；
- 缓存目录加入 `.gitignore`（`data/review_v6_cache/`）。

## 验收

`python -m pytest -q tests` 退出码 0，且新测试文件里的用例全部通过（不是 skip）。
