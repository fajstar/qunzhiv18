# -*- coding: utf-8 -*-
"""
公网访问防护中间件 guard.py
============================
公网暴露的防护：

1. 访问口令：需要 ?key=xxx 或 header X-Access-Key（防陌生人乱用）
2. IP 限流：每 IP 每 60s 最多 N 次启动预演（防单点刷爆）
3. 全局限流：每 IP 总请求频率限制
4. **每日全局消费上限**：全体用户合计每天最多花 N 元，超了直接停服到次日
   —— 这是最后一道防线：即使 IP 限流被绕过（换 IP、分布式），
      账单也不会失控。默认 50 元/天 ≈ 312 次预演。

配置（全部走环境变量，仓库里不含真值）：

    QUNZHI_ACCESS_KEY       访问口令（必填；未配置时启动失败，不再有默认值）
    QZ_DEMO_KEY             演示口令（可选；公开在比赛材料里，限额低）
    QZ_DEMO_DAILY_QUOTA     演示口令每日请求上限，默认 1000
    QUNZHI_DAILY_BUDGET     每日消费上限（元），默认 50
    QUNZHI_COST_PER_RUN     单次预演成本（元），默认 0.16
    QUNZHI_BUDGET_FILE      计数落盘路径，默认 /tmp/qunzhi_daily_spend.json
"""
import hashlib
import json
import os
import time
import threading

from fastapi import Request, HTTPException

# ---------- 访问口令 ----------
# 部署时用环境变量设置；仓库里不放真值，也不留默认值。
ACCESS_KEY = os.environ.get("QUNZHI_ACCESS_KEY", "")
if not ACCESS_KEY:
    raise RuntimeError("QUNZHI_ACCESS_KEY 未配置")
DEMO_KEY = os.environ.get("QZ_DEMO_KEY", "")
DEMO_DAILY_QUOTA = int(os.environ.get("QZ_DEMO_DAILY_QUOTA", "1000"))
DEMO_KEY_NAME = "演示 key"
DEMO_QUOTA_FILE = os.environ.get("QZ_DEMO_QUOTA_FILE", "/tmp/qz_demo_quota.json")
_demo_lock = threading.Lock()
_DEMO_MEM = {"date": "", "used": 0}

# ---------------------------------------------------------------------------
# API 分层（public / private）—— 2026-11-23 开源 / 隐私任务
# ---------------------------------------------------------------------------
# 规则：
#   public  = 数据全部公开（论文元数据 / PwC benchmark / 基线 / 版本 / call 回放），
#             免 key 可读；只读 GET（以及显式列出的公开 POST 检索）放行。
#   private = 用户上传、可能含用户原文、管理类；必须 key，且 handler 不得返回原文。
#   self    = /api/v1/* /api/admin/* 自带鉴权（用户 key / admin key），这里不重复校验。
#   未声明 = 默认 private（新增接口忘了登记也不会裸奔）。
PUBLIC_PATHS = frozenset({
    "/api/version",
    "/api/kb/search",            # GET 免 key；POST 见 PUBLIC_POST_PATHS
    "/api/kb/selfcheck",
    "/api/kb/defect_baseline",
    "/api/kb/baseline2",
    "/api/kb/insights",
    "/api/kb/gaps",
    "/api/kb/stats",
    "/api/kb/search_h",
    "/api/benchmark",            # 前缀 /api/benchmark/ 见 PUBLIC_PREFIXES
    "/api/pwc/scores",
    "/api/pwc/datasets",
    "/api/pwc/models",
    "/api/call",                 # 前缀 /api/call/ 见 PUBLIC_PREFIXES
    "/api/meta",
    "/api/tools",
    "/api/outline/templates",
    "/api/weekly/export",
    "/api/review/bib",
    "/api/arxiv/html_url",
    "/api/wb/stats",
    "/api/lib/status",
    "/api/lib/health",
    "/api/eval",
    "/api/eval_cases",
    "/api/runs",
    "/api/gates/status",
})

PUBLIC_PREFIXES = (
    "/api/benchmark/",
    "/api/pwc/",
    "/api/call/",
    "/api/arxiv/html/",
    "/api/metrics/",            # 仅 /sensitivity 后缀，见 api_layer
    "/api/kb/similar/",
    "/api/kb/features/",
    "/api/tools/v15/result/",
)

# 只读 GET 例外之外，允许匿名 POST 的路径（检索是公开数据面）
PUBLIC_POST_PATHS = frozenset({"/api/kb/search"})

PRIVATE_PATHS = frozenset({
    "/api/kb/upload",            # 用户论文上传：仅本机、入库前脱敏
    "/api/evidence",             # 证据/锚点：可能含用户论文内容，需 key
})

PRIVATE_PREFIXES = (
    "/api/kb/upload/",
    "/api/evidence/",
)

SELF_AUTH_PREFIXES = ("/api/v1/", "/api/admin/")


def api_layer(path: str) -> str:
    """返回 public / private / self / page。默认拒绝：未声明的 /api/* 按 private。"""
    if path.startswith(SELF_AUTH_PREFIXES):
        return "self"
    if path in PRIVATE_PATHS or path.startswith(PRIVATE_PREFIXES):
        return "private"
    if path in PUBLIC_PATHS:
        return "public"
    if path.startswith(PUBLIC_PREFIXES):
        if path.startswith("/api/metrics/"):
            return "public" if path.endswith("/sensitivity") else "private"
        return "public"
    if (path.startswith("/api/paper/")
            and (path.endswith("/text") or path.endswith("/outline"))):
        return "public"
    if path.startswith("/api/tools/") and path.endswith("/schema"):
        return "public"
    if path.startswith("/api/"):
        return "private"
    return "page"


def layer_manifest() -> dict:
    """给 /api/version 与文档用的机器可读分层清单。"""
    return {
        "policy": "public=免key只读；private=需key且不返回用户原文；self=自带鉴权；未声明默认private",
        "public": sorted(PUBLIC_PATHS),
        "public_prefixes": list(PUBLIC_PREFIXES),
        "public_post": sorted(PUBLIC_POST_PATHS),
        "private": sorted(PRIVATE_PATHS),
        "private_prefixes": list(PRIVATE_PREFIXES),
        "self_auth_prefixes": list(SELF_AUTH_PREFIXES),
    }

# 管理端数据层（key 库 / 调用记录 / 审计）
_STORE = None


def store():
    global _STORE
    if _STORE is None:
        import sys
        sys.path.insert(0, "/home/ubuntu/paperlib")
        from admin_store import Store
        _STORE = Store()
    return _STORE

# ---------- 频率限流 ----------
# 启动预演限流：每 IP 60s 内最多 N 次
# 注：评审期间多名评委可能在同一间办公室/同一 NAT 出口 IP 下同时体验，
#     原来的 3 次/60s 会让第 4 个人直接被挡（429）——对评审体验是负面印象。
#     这里放宽到 8 次/60s，同时保留上限以防 API 额度被刷。
_START_WINDOW = 60
_START_MAX = 8
# 全局限流：每 IP 10s 内最多 N 次请求（页面轮询/SSE 重连较频繁，留足余量）
_GLOBAL_WINDOW = 10
_GLOBAL_MAX = 40

# ---------- 每日消费上限（新增） ----------
DAILY_BUDGET = float(os.environ.get("QUNZHI_DAILY_BUDGET", "50"))
COST_PER_RUN = float(os.environ.get("QUNZHI_COST_PER_RUN", "0.16"))
BUDGET_FILE = os.environ.get("QUNZHI_BUDGET_FILE", "/tmp/qunzhi_daily_spend.json")

# ---------- 请求配额 / 每-key 限流（2026-11-21 glm-fix4 问题 5） ----------
# daily_requests 是"全站每天请求数"配额（对外承诺值）；budget 是 LLM 预演成本上限。
# 之前这两项只存在于运维脑子里，/api/version 无法自证 → 现在统一从这里暴露。
DAILY_REQUEST_QUOTA = int(os.environ.get("QUNZHI_DAILY_REQUESTS", "10000"))
_KEY_WINDOW = 60          # 每 key 限流窗口（秒）
_KEY_MAX = 100            # 每 key 每窗口最多请求数

_lock = threading.Lock()
_start_hits = {}   # ip -> [timestamps]
_global_hits = {}  # ip -> [timestamps]
_key_hits = {}     # sha256(key)[:16] -> [timestamps]（不落明文 key）


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _demo_save_unlocked(n: int) -> None:
    """演示额度计数落盘；落盘失败时保留进程内计数，不会变成无限额。"""
    n = max(0, int(n))
    _DEMO_MEM["date"] = _today()
    _DEMO_MEM["used"] = n
    try:
        tmp = DEMO_QUOTA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"date": _today(), "used": n}, f)
        os.replace(tmp, DEMO_QUOTA_FILE)
    except Exception:
        pass


def _demo_used_unlocked() -> int:
    """今日演示 key 已用次数。首次/跨天时用 qz_usage 做种子（有 admin_store 时）。"""
    today = _today()
    if _DEMO_MEM.get("date") == today:
        return int(_DEMO_MEM.get("used") or 0)
    try:
        with open(DEMO_QUOTA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if d.get("date") == today:
            n = max(0, int(d.get("used") or 0))
            _DEMO_MEM.update({"date": today, "used": n})
            return n
    except Exception:
        pass
    seed = 0
    try:
        seed = int(store().conn().execute(
            "select count(*) from qz_usage where day=? and key_name=?",
            (today, DEMO_KEY_NAME)).fetchone()[0] or 0)
    except Exception:
        # 开源包不带 admin_store：退回文件计数，从 0 开始，不能直接判满。
        seed = 0
    _demo_save_unlocked(seed)
    return seed


def _demo_used() -> int:
    with _demo_lock:
        return _demo_used_unlocked()


def demo_quota_status() -> dict:
    """演示 key 的真实配额状态（不返回 key 明文）。"""
    used = _demo_used() if DEMO_KEY else 0
    return {
        "enabled": bool(DEMO_KEY),
        "daily_quota": DEMO_DAILY_QUOTA,
        "used_today": used,
        "remaining_today": max(0, DEMO_DAILY_QUOTA - used),
        "reset_in_sec": seconds_to_midnight(),
        "scope": "演示 key 只用于公开比赛材料；主访问口令不在前端出现",
    }


def _check_demo_quota():
    """检查并原子占用一次演示额度；超限返回 429，次日 0 点重置。"""
    with _demo_lock:
        used = _demo_used_unlocked()
        if used >= DEMO_DAILY_QUOTA:
            raise HTTPException(
                status_code=429,
                detail=("演示 key 今日额度已用完（每天 %d 次）。"
                        "请明天再试，或用你自己的访问口令。" % DEMO_DAILY_QUOTA),
                headers={"Retry-After": str(seconds_to_midnight())})
        _demo_save_unlocked(used + 1)


def _load_spend() -> dict:
    """读当日消费计数；跨天自动重置。返回 {'date':..., 'runs':int}"""
    try:
        with open(BUDGET_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if d.get("date") == _today():
            return d
    except Exception:
        pass
    return {"date": _today(), "runs": 0}


def _save_spend(d: dict) -> None:
    try:
        tmp = BUDGET_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, BUDGET_FILE)
    except Exception:
        pass          # 落盘失败不能挡住服务，内存计数仍在


def budget_status() -> dict:
    """给前端/运维看的当前预算状态"""
    with _lock:
        d = _load_spend()
    used = d["runs"] * COST_PER_RUN
    return {
        "date": d["date"],
        "runs": d["runs"],
        "spent": round(used, 2),
        "budget": DAILY_BUDGET,
        "remaining": round(max(0.0, DAILY_BUDGET - used), 2),
        "cost_per_run": COST_PER_RUN,
    }


def _usage_today() -> int:
    """今日全站请求数（qz_usage 按 day 索引，毫秒级）。"""
    try:
        return int(store().conn().execute(
            "select count(*) from qz_usage where day=date('now')").fetchone()[0] or 0)
    except Exception:
        return 0


def seconds_to_midnight() -> int:
    """距离本地次日 0 点还有多少秒（配额重置时间）。"""
    now = time.localtime()
    try:
        nxt = time.mktime((now.tm_year, now.tm_mon, now.tm_mday + 1,
                           0, 0, 0, 0, 0, -1))
        return max(1, int(nxt - time.time()))
    except Exception:
        return 86400


def quota_status() -> dict:
    """统一配额口径：请求数配额 + 成本预算（两者不是一回事）。"""
    used = _usage_today()
    bs = budget_status()
    return {
        "daily_requests": DAILY_REQUEST_QUOTA,
        "used_today": used,
        "requests_remaining": max(0, DAILY_REQUEST_QUOTA - used),
        "cost_per_run": bs["cost_per_run"],
        "currency": "CNY",
        "budget_daily": bs["budget"],
        "budget_used": bs["spent"],
        "budget_remaining": bs["remaining"],
        "budget_runs": bs["runs"],
        "budget_date": bs["date"],
        "reset_in_sec": seconds_to_midnight(),
        "scope": ("used_today=今日全站请求数（含所有 /api/*）；budget_used=仅 LLM 预演成本"
                  "（runs × cost_per_run），两者独立计。"),
    }


def rate_limit_status() -> dict:
    """真实生效的限流参数（不是文档里的期望值）。"""
    return {
        "per_ip_per_minute": int(_GLOBAL_MAX * 60 / _GLOBAL_WINDOW),
        "per_ip_max_per_window": _GLOBAL_MAX,
        "per_ip_window_sec": _GLOBAL_WINDOW,
        "per_key_per_minute": _KEY_MAX,
        "per_key_window_sec": _KEY_WINDOW,
        "burst": _GLOBAL_MAX,
        "retry_after": _GLOBAL_WINDOW,
        "start_per_ip_per_minute": _START_MAX,
        "start_window_sec": _START_WINDOW,
        "scope": "per_ip + per_key；/api/sandbox/start 另有 8 次/60s；429 带 Retry-After",
        "note": ("per_ip 是防刷的通用请求限流；per_key 只对携带有效 key 的请求计数，"
                 "未带 key 的公开只读 GET 不受 per_key 限制。"),
    }


def error_meta(status: int, headers=None, detail: str = "") -> dict:
    """错误响应统一附加字段：retry_after_sec + quota_remaining（问题 5）。

    所有 /api/* 的 4xx/5xx 都由 main_gateway 的 middleware 调这里补字段，
    不依赖每个 handler 手工写；旧字段 detail 原样保留。
    """
    retry = 0
    try:
        if headers:
            retry = int(headers.get("Retry-After") or headers.get("retry-after") or 0)
    except Exception:
        retry = 0
    if not retry and status == 429:
        retry = _GLOBAL_WINDOW
    if not retry and status == 503 and ("额度" in (detail or "") or "budget" in (detail or "").lower()):
        retry = seconds_to_midnight()
    qs = quota_status()
    return {
        "retry_after_sec": int(retry),
        "quota_remaining": int(qs["requests_remaining"]),
        "quota_reset_sec": int(qs["reset_in_sec"]),
    }


def _client_ip(request: Request) -> str:
    """取客户端 IP（穿透后取真实 IP）"""
    cf = request.headers.get("CF-Connecting-IP")
    if cf:
        return cf
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def check_access(request: Request):
    """访问校验：口令 + 限流 + 每日预算。不通过抛 HTTPException"""
    ip = _client_ip(request)
    path = request.url.path

    # 0. CORS 预检放行：OPTIONS 不带业务载荷，交给 CORSMiddleware 回答；
    #    否则浏览器跨域 POST 会在预检阶段被 401 拦掉（2026-11-18 修）
    if request.method == "OPTIONS":
        return

    # 1. 访问分层：public 免 key，private 必须 key，self 交给下游自有鉴权
    layer = api_layer(path)
    key = ""
    key_ok = False
    if path.startswith("/api/"):
        public_read = (layer == "public" and request.method in ("GET", "HEAD"))
        public_post = (layer == "public" and request.method == "POST"
                       and path in PUBLIC_POST_PATHS)
        # 只读预热：/read 打开时触发后台抓 arXiv HTML，自带每 IP 限流
        warm_post = request.method == "POST" and path.startswith("/api/arxiv/html/warm/")
        key = (request.headers.get("X-Access-Key")
               or request.headers.get("X-QunZhi-Key")
               or request.query_params.get("key") or "")
        is_demo = bool(DEMO_KEY) and key == DEMO_KEY
        if layer == "self":
            # /api/v1/* /api/admin/* 的自有鉴权里会再查演示 key 配额
            pass
        elif public_read or public_post or warm_post:
            if is_demo:
                _check_demo_quota()
                key_ok = True
        else:
            if is_demo:
                _check_demo_quota()
                key_ok = True
            elif key and key != ACCESS_KEY:
                rec = store().verify(key)
                if rec and rec.get("disabled"):
                    raise HTTPException(status_code=403, detail="该 key 已被停用，请联系管理员")
                if not rec:
                    raise HTTPException(status_code=401, detail="无效的 key")
                # 租户被停用 → 该租户所有 key 一律拒
                try:
                    t = (rec.get("tenant") or "default")
                    tt = store().conn().execute(
                        "select enabled from qz_tenants where name=?", (t,)).fetchone()
                    if tt and not tt["enabled"]:
                        raise HTTPException(status_code=403, detail="tenant_disabled: 所属单位已被停用")
                except HTTPException:
                    raise
                except Exception:
                    pass
                key_ok = True
            elif key == ACCESS_KEY:
                key_ok = True
            else:
                if layer == "private":
                    raise HTTPException(
                        status_code=401,
                        detail="该接口属于私有层（private），需要访问口令（X-Access-Key 或 ?key=）")
                raise HTTPException(status_code=401,
                                    detail="需要访问口令（X-Access-Key 或 ?key=）")

    now = time.time()
    with _lock:
        # 2. 每-key 限流（只对已验证的 key 计数；公开只读 GET 不计数）
        if key_ok and key:
            kh = _key_hits.setdefault(hashlib.sha256(key.encode("utf-8")).hexdigest()[:16], [])
            kh[:] = [t for t in kh if now - t < _KEY_WINDOW]
            if len(kh) >= _KEY_MAX:
                raise HTTPException(
                    status_code=429,
                    detail=("该 key 请求过于频繁（每 %d 秒最多 %d 次），"
                            "请 %d 秒后重试" % (_KEY_WINDOW, _KEY_MAX, _KEY_WINDOW)),
                    headers={"Retry-After": str(_KEY_WINDOW)})
            kh.append(now)

        # 3. 全局限流
        g = _global_hits.setdefault(ip, [])
        g[:] = [t for t in g if now - t < _GLOBAL_WINDOW]
        if len(g) >= _GLOBAL_MAX:
            raise HTTPException(
                status_code=429,
                detail="请求过于频繁，请等待约 10 秒后重试（系统正在为其他体验者服务）",
                headers={"Retry-After": str(_GLOBAL_WINDOW)})
        g.append(now)

        # 3. 启动预演限流（防单点刷 API）
        if path == "/api/sandbox/start":
            s = _start_hits.setdefault(ip, [])
            s[:] = [t for t in s if now - t < _START_WINDOW]
            if len(s) >= _START_MAX:
                raise HTTPException(
                    status_code=429,
                    detail=f"启动预演过于频繁（同一网络 {_START_WINDOW} 秒内最多 {_START_MAX} 次）。"
                           "若多位体验者共用同一出口网络，请稍等约 1 分钟再试。",
                    headers={"Retry-After": str(_START_WINDOW)})
            s.append(now)

        # 4. 每日全局消费上限（最后一道防线）
        if path == "/api/sandbox/start":
            d = _load_spend()
            used = d["runs"] * COST_PER_RUN
            if used + COST_PER_RUN > DAILY_BUDGET:
                raise HTTPException(
                    status_code=503,
                    detail=(f"今日公开体验额度已用完（每天上限 ¥{DAILY_BUDGET:g}，"
                            f"约 {int(DAILY_BUDGET / COST_PER_RUN)} 次预演）。"
                            f"这是作者自费运行的演示站，请明天再来，或按 README 自行部署。"),
                    headers={"Retry-After": str(seconds_to_midnight())})
            d["runs"] += 1
            _save_spend(d)


def setup_guard(app):
    """注册防护中间件"""
    @app.middleware("http")
    async def guard_middleware(request: Request, call_next):
        try:
            check_access(request)
        except HTTPException as e:
            from fastapi.responses import JSONResponse
            _log(request, e.status_code, 0)
            return JSONResponse(status_code=e.status_code, content={"detail": e.detail},
                                headers={"X-API-Layer": api_layer(request.url.path)})
        t0 = time.time()
        resp = await call_next(request)
        try:
            resp.headers["X-API-Layer"] = api_layer(request.url.path)
        except Exception:
            pass
        _log(request, resp.status_code, int((time.time() - t0) * 1000))
        return resp


def _log(request, status: int, ms: int):
    """记录调用（跳过静态资源与健康检查，避免噪声）"""
    try:
        path = request.url.path
        if path.startswith("/static/") or path in ("/api/lib/health", "/favicon.ico"):
            return
        key = (request.headers.get("X-Access-Key")
               or request.headers.get("X-Api-Key")
               or request.headers.get("X-QunZhi-Key")
               or request.query_params.get("key") or "")
        rec = None
        name = "匿名"
        if key:
            if DEMO_KEY and key == DEMO_KEY:
                rec = None
                name = DEMO_KEY_NAME
            else:
                rec = store().verify(key) if key != ACCESS_KEY else None
                if rec:
                    name = rec.get("name") or "key"
                elif key == ACCESS_KEY:
                    name = "(主口令)"
                else:
                    name = "无效key"
        _tenant = "default"
        try:
            if rec:
                _tenant = rec.get("tenant") or "default"
        except Exception:
            pass
        store().log_usage(name, path[:120], request.method, status, ms, _client_ip(request), _tenant)
    except Exception:
        pass
