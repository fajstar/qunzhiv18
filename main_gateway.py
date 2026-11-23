# -*- coding: utf-8 -*-
"""
群智 V15 · FastAPI 统一网关
===========================
外层统一入口：集成 v14 讨论引擎（SandboxSession/Coach/ExperimentMemory）
+ V15 新增的主教练联网搜索 + 预设教研课题库 + 场景化决策报告。

设计：
  - 复用 sandbox/app.py 的类（importlib 加载，避免重复实现引擎）
  - 新增 /api/coach/search（主教练联网）、/api/sandbox/start-v15（带信息注入）
  - 所有路由统一 /api/* 前缀
  - v15 数据独立（data/ 目录，与 v14 隔离）

启动：python app/main_gateway.py（默认 5020 端口）
"""
import sys, os, json, re, threading, time, importlib.util, uuid, hashlib, hmac
from datetime import datetime
from typing import Optional
from contextlib import asynccontextmanager
import urllib.request

# v15 根目录加入 path（便于 import 共享模块）
V15_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, V15_ROOT)
sys.path.insert(0, os.path.join(V15_ROOT, "sandbox"))

# UTF-8 输出
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# 口径敏感性（sensitivity）：独立模块，只读 DB 现算，避免和网关主文件的其他改动冲突。
try:
    import sensitivity as _sensitivity
except Exception as _sens_e:  # pragma: no cover - 可选模块，缺失时接口降级并报错
    _sensitivity = None
    print("[sensitivity] import 失败：%r" % (_sens_e,), flush=True)

# 静态资源（logo 等）
_static_dir = os.path.join(V15_ROOT, "app", "static")
os.makedirs(_static_dir, exist_ok=True)

# ---------- 加载 v14/v15 讨论引擎（复用类，不启动 Flask） ----------
def _load_engine_module():
    """用 importlib 加载 sandbox/app.py（v14 引擎），返回模块"""
    engine_path = os.path.join(V15_ROOT, "sandbox", "app.py")
    spec = importlib.util.spec_from_file_location("v15_engine", engine_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

engine = _load_engine_module()
SandboxSession = engine.SandboxSession
Coach = engine.Coach
ExperimentMemory = engine.ExperimentMemory
call_with_retry = engine.call_with_retry
# V15.4：多模型分流（外围环节用 Qwen/GLM，核心讨论仍 DeepSeek）
call_llm = getattr(engine, "call_llm", None)
PLAYER_NAMES = engine.PLAYER_NAMES

# ---------- 加载 V15 新增模块 ----------
sys.path.insert(0, os.path.join(V15_ROOT, "app"))
from coach_search import CoachSearcher, coach_search_and_inject
from presets import PRESET_TOPICS, get_scenario_report
from coach_team import build_optimal_team, generate_interventions
from review_v3 import v3_review
from text_reflow import reflow_pdf_text

# ---------- FastAPI 应用 ----------
app = FastAPI(title="群智 · 科研决策预演智能体 V15")
app.mount("/static", StaticFiles(directory=_static_dir), name="static")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# 公网防护（口令 + 限流）
import guard as _guard            # 配额/限流/错误字段统一口径（glm-fix4 问题 5）
from guard import setup_guard
import privacy_scrubber as _privacy
setup_guard(app)

# 页面禁用缓存（防止同学浏览器缓存旧版 JS，导致 API 地址还是 127.0.0.1）
@app.middleware("http")
async def no_cache_middleware(request, call_next):
    import time as _t
    response = await call_next(request)
    if request.url.path in ("/", "/classroom") or request.url.path.endswith(".html"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response

# API 版本回显（2026-11-21 平台修复 · 问题 4）：
# 所有 /api/* 的 JSON 返回都带 schema_version / api_version，
# 不依赖每个 handler 记得手写。放在 guard 之后注册 = 最外层，
# 连 401/403/429 这类 guard 直接返回的 JSON 也会带上版本号。
@app.middleware("http")
async def api_schema_version_middleware(request, call_next):
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        return response
    # 响应头兜底：非 JSON（HTML/SSE/下载）也能看到版本号。
    response.headers["X-API-Schema-Version"] = API_SCHEMA_VERSION
    if "application/json" not in (response.headers.get("content-type") or ""):
        return response
    body = b""
    try:
        async for chunk in response.body_iterator:
            body += chunk
        data = json.loads(body.decode("utf-8"))
        if isinstance(data, dict):
            data.setdefault("schema_version", API_SCHEMA_VERSION)
            data.setdefault("api_version", API_VERSION)
            # 问题 5（glm-fix4）：所有错误响应统一补 retry_after_sec / quota_remaining，
            # 包括 guard 直接返回的 401/403/429，以及 handler 抛的 4xx。
            if response.status_code >= 400:
                try:
                    em = _guard.error_meta(response.status_code, response.headers,
                                           str(data.get("detail") or ""))
                    for _k, _v in em.items():
                        data.setdefault(_k, _v)
                except Exception as _e:
                    print("[api-error-meta] 注入失败：%r" % (_e,), flush=True)
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        from starlette.responses import Response as _Resp
        headers = {k: v for k, v in response.headers.items()
                   if k.lower() not in ("content-length", "content-type")}
        if isinstance(data, dict) and response.status_code >= 400:
            _retry = int(data.get("retry_after_sec") or 0)
            if _retry > 0:
                headers["Retry-After"] = str(_retry)
            if data.get("quota_remaining") is not None:
                headers["X-Quota-Remaining"] = str(data.get("quota_remaining"))
        return _Resp(content=body, status_code=response.status_code,
                     headers=headers, media_type="application/json")
    except Exception as e:
        print("[api-version] 注入 schema_version 失败：%r" % (e,), flush=True)
        from starlette.responses import Response as _Resp
        headers = {k: v for k, v in response.headers.items()
                   if k.lower() != "content-length"}
        return _Resp(content=body, status_code=response.status_code, headers=headers,
                     media_type=response.headers.get("content-type") or "application/json")

# 演示 key 注入：模板/静态 JS 只保留占位符 __QZ_DEMO_KEY__，运行时从环境变量注入。
# 演示 key 允许公开（限额 1000/天）；主访问口令 QUNZHI_ACCESS_KEY 绝不下发到前端。
@app.middleware("http")
async def demo_key_injection_middleware(request, call_next):
    response = await call_next(request)
    ct = (response.headers.get("content-type") or "").lower()
    if not (ct.startswith("text/html") or "javascript" in ct):
        return response
    body = b""
    try:
        async for chunk in response.body_iterator:
            body += chunk
    except Exception as e:
        print("[demo-key] 读取响应失败：%r" % (e,), flush=True)
        return response
    token = b"__QZ_DEMO_KEY__"
    if token in body:
        inject = (getattr(_guard, "DEMO_KEY", "")
                  or getattr(_guard, "ACCESS_KEY", "") or "")
        body = body.replace(token, inject.encode("utf-8"))
    from starlette.responses import Response as _Resp
    headers = {k: v for k, v in response.headers.items()
               if k.lower() != "content-length"}
    return _Resp(content=body, status_code=response.status_code,
                 headers=headers,
                 media_type=response.headers.get("content-type"))

# 活跃会话（复用引擎的机制）
active_sessions = {}
session_queues = {}

# ==================== 请求模型 ====================

class SandboxStartRequest(BaseModel):
    topic: str
    user_thought: str = ""
    max_rounds: int = 3
    preset_name: Optional[str] = None          # 预设课题（教研场景）
    info_mode: str = "coach_search"            # coach_search=主教练联网注入
    personality_enabled: bool = True
    scenario: Optional[str] = None             # 场景化报告：教研/科研/通用
    allow_web: Optional[bool] = None           # B9：本局是否启用网络资料白名单网（None=跟随服务端默认）
    # V16.2 插件化：用户自选评审器（'v3'=快约40秒 / 'v6'=准约160秒 / None=用默认插件）
    review_plugin: Optional[str] = None


class IntentRequest(BaseModel):
    """输入意图识别请求。"""
    topic: str = ""
    user_thought: str = ""

class CoachSearchRequest(BaseModel):
    topic: str
    count: int = 6

class CoachChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None

class ReviewRequest(BaseModel):
    report: str
    topic: str = ""

# ==================== 预设课题 ====================

@app.get("/api/presets")
def get_presets():
    """预设教研课题库（A 场景落地）"""
    return {"ok": True, "presets": PRESET_TOPICS}

# ==================== 主教练联网搜索 ====================

@app.post("/api/coach/search")
def coach_search(req: CoachSearchRequest):
    """主教练独占联网搜索：动态构建信息库"""
    searcher = CoachSearcher(call_with_retry)
    data = searcher.search_topic(req.topic, n_queries=2)
    return {
        "ok": True,
        "topic": req.topic,
        "queries": data.get("queries", []),
        "results_count": data.get("results_count", 0),
        "info_pool": data.get("info_pool", []),
    }

# ==================== 讨论会话（V15 版：带主教练信息注入） ====================

@app.post("/api/intent")
def check_input_intent(req: IntentRequest):
    """输入意图识别：建议性接口，不是闸门。失败时返回 ok=False，前端照常开始。"""
    try:
        for _p in (PRESET_TOPICS or []):
            _title = _p.get("title") if isinstance(_p, dict) else str(_p)
            if _title and _title.strip() == (req.topic or "").strip():
                return {"ok": True, "intent": "PRESET", "clarity": 100,
                        "needs_clarify": False, "missing": [],
                        "rewritten": req.topic,
                        "reason": "产品预设课题，直接开始", "source": "preset"}
    except Exception:
        pass
    # [intent-fast] 快速通道：课题够具体就直接放行，不调 LLM（避免启动前等 10+ 秒）
    _topic = (req.topic or "").strip()
    if len(_topic) >= 8:
        return {"ok": True, "intent": "FAST", "clarity": 85,
                "needs_clarify": False, "missing": [],
                "rewritten": _topic,
                "reason": "输入已足够具体（快速判定，未调用模型）",
                "source": "fast"}

    try:
        from input_intent import assess_intent
        d = assess_intent(req.topic, req.user_thought, call_with_retry, timeout=18)
        d["ok"] = True
        return d
    except Exception as e:
        return {"ok": False, "reason": "intent unavailable: %s" % str(e)[:80]}


@app.post("/api/sandbox/start")
def start_session(req: SandboxStartRequest):
    """启动 V15 讨论：主教练先联网构建信息库，注入给玩家，再启动讨论"""
    topic = req.topic.strip()
    if not topic:
        raise HTTPException(status_code=400, detail="请输入课题")

    # 0. 复杂度分级（V15.2：区分简单/中等/复杂，防"简单问题复杂化"）
    try:
        from complexity import assess_complexity, get_level_config
        level = assess_complexity(topic, call_with_retry)
        lvl_cfg = get_level_config(level)
        max_rounds = lvl_cfg["max_rounds"]
        output_mode = lvl_cfg["output"]
        print(f"[V15.2] 课题复杂度: {level}（{max_rounds}轮，输出{output_mode}）", flush=True)
    except Exception as e:
        level, max_rounds, output_mode = "中等", req.max_rounds, "standard"
        print(f"[V15.2] 复杂度评估失败，用默认: {e}", flush=True)

    # 1. 主教练组建最优人格团队（V15：动态选择，而非固定组合）
    #    健壮性：组队/干预失败时回退默认配置，绝不让 start 接口 500
    try:
        team = build_optimal_team(topic, req.user_thought, call_with_retry) or {}
    except Exception as e:
        print(f"[V15] 组队失败，使用默认配置: {str(e)[:120]}", flush=True)
        team = {}
    config = team.get("config", {}) or {}
    team_reasoning = team.get("reasoning", "") or "（组队降级：使用默认人格配置）"
    print(f"[V15] 主教练组队: {team_reasoning}")

    # 2. 主教练预审：根据课题生成干预指令（V15：具备干预能力）
    try:
        coach_interventions = generate_interventions(topic, call_with_retry)
    except Exception as e:
        print(f"[V15] 干预生成失败，跳过: {str(e)[:120]}", flush=True)
        coach_interventions = None
    print(f"[V15] 主教练预审干预: {coach_interventions}")

    # 3. 主教练联网注入信息（V15 核心新能力）
    coach_infos = None
    search_meta = None
    coach_pool = None                     # V15 优化：结构化信息池（含编号 P1..Pn）
    if req.info_mode == "coach_search":
        try:
            # B9：本局是否启用网络资料白名单网（req.allow_web 为 None 时跟随服务端默认）
            searcher = CoachSearcher(call_with_retry, allow_web=req.allow_web)
            fragments, search_data = searcher.build_info_fragments(topic, list(range(6)))
            coach_infos = fragments
            coach_pool = search_data.get("info_pool") or []
            search_meta = {
                "queries": search_data.get("queries", []),
                "results_count": search_data.get("results_count", 0),
                "raw_count": search_data.get("raw_count", search_data.get("results_count", 0)),
                "injected_count": len(search_data.get("info_pool") or []),
                # V15（2026-10-09）：逐源台账 → 区分"没有数据"与"被限速/超时/被挡"
                "source_status": search_data.get("source_status") or {},
                # V15（2026-10-10，B5）：检索漏斗——检索→去重→类型过滤→撤稿→注入 逐级计数，
                # 含快照日期与级别覆盖率，供事后审计与"改动前后对比"。
                "funnel": search_data.get("funnel") or {},
                "allow_web": bool(getattr(searcher, "allow_web", False)),
            }
        except Exception as e:
            print(f"[V15] 主教练搜索失败，使用预设信息: {e}")
            coach_infos = None

    # 4. 构建会话（experiment_mode 标记 V15 + 最优人格 + 干预）
    experiment_mode = {
        "personality_enabled": req.personality_enabled,
        "info_mode": req.info_mode,
        "v15": True,
        "discussion_mode": "structured",
        "personality_mode": "optimal",   # V15：最优人格
        "team_reasoning": team_reasoning,
        # V15 优化（2026-09-25）：干预时机标记。
        # 旧行为 = predefined_once（开局预设、第2轮一次性注入）；
        # 新行为 = per_round（本局内逐轮决策）。两者不可合并统计。
        "intervention_timing": "per_round",
    }

    # 会话 ID：课题前 10 字（可读）+ 时间戳 + 4 位随机后缀（避免同秒同课题撞 ID 覆盖）
    sid = f"v15_{topic[:10]}_{datetime.now().strftime('%H%M%S')}_{uuid.uuid4().hex[:4]}"
    session = SandboxSession(
        topic, config, max_rounds, req.user_thought,
        custom_infos=coach_infos,  # 主教练注入的信息
        preset_name=req.preset_name,
        experiment_mode=experiment_mode,
        force_dynamic_info=(req.info_mode == "coach_search"),
        coach_interventions=coach_interventions,  # V15：开局候选（不再强制注入）
        info_pool=coach_pool,                     # V15 优化：结构化信息池 → 参考文献与核验
    )
    # 标记 V15 附加数据
    session.v15_meta = {
        "search": search_meta,
        "scenario": req.scenario or "通用",
        "complexity_level": level,        # V15.2：课题复杂度
        "output_mode": output_mode,       # V15.2：输出形态
        "started_at": datetime.now().isoformat(),
        "team_reasoning": team_reasoning,
        "team_config": config,
        "coach_interventions": coach_interventions,
        "intervention_timing": "per_round",
        "references": getattr(session, "references", []),   # V15 优化：参考文献结构化清单
    }
    # V16.2：把用户选定的评审插件记在会话上（插件层随后据此执行；未选则取默认插件）
    try:
        import review_plugins as _RP
        _plugs = {x['id']: x for x in _RP.list_plugins()}
        _want = req.review_plugin    # 函数参数是 req（原先误写成裸变量 review_plugin → NameError，被 review_error 如实记下）
        if _want and _want in _plugs and _plugs[_want]['available']:
            session.review_plugin_id = _want
        elif _want and _want in _plugs:
            # 用户点的插件不可用 —— **不悄悄换**，如实记下来，前端会显示原因
            session.review_plugin_id = None
            session.review_error = ('所选评审插件 %s 不可用：%s'
                                    % (_want, _plugs[_want].get('unavailable_reason') or '原因未给出'))
        else:
            session.review_plugin_id = next(
                (x['id'] for x in _RP.list_plugins() if x.get('default')), None)
    except Exception as _e:
        session.review_plugin_id = None
        session.review_error = '插件清单不可用：%s: %s' % (type(_e).__name__, str(_e)[:150])
    active_sessions[sid] = session
    session_queues[sid] = []

    def run_and_notify():
        orig = session.add_log
        def hooked(lt, msg, player=None, plain="", collision=False):
            orig(lt, msg, player, plain, collision)
            if sid in session_queues:
                try:
                    payload = {"type": lt, "message": msg, "plain_text": plain, "collision": collision}
                    if player:
                        payload.update({
                            "player": player.name,
                            "prototype_name": player.prototype['name'],
                            "profession_name": player.profession['name'],
                            "profession_icon": player.profession['icon'],
                            "value_name": player.value['name'],
                            "value_icon": player.value['icon'],
                            "color": player.prototype['color'],
                        })
                    session_queues[sid].append(payload)
                except Exception:
                    pass
        session.add_log = hooked
        # 健壮性：讨论线程任何异常都必须把 done 事件发出去，
        # 否则前端 /api/sandbox/stream 会永久等待、result 永远 running（实测过的翻车形态）
        try:
            session.run()
        except Exception as e:
            import traceback
            print(f"[讨论异常] {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
            if sid in session_queues:
                session_queues[sid].append({
                    "type": "system",
                    "message": f"⚠️ 讨论过程出现异常，已中断：{type(e).__name__}。可返回重新发起预演。",
                })
        finally:
            if sid in session_queues:
                session_queues[sid].append({"type": "done", "message": "完成"})

        # 讨论结束后立即预生成「人话报告 + v3 评审 + 深批者」（后台并行预热，
        # 避免用户点开报告时干等 2-3 分钟）
        def _precompute():
            session._precomputing = True
            try:
                # ================================================================
                # V16.6b：**预生成自身也并行**。
                # 原先是串行「人话化 → v3 → 深批者」，实测约 95 秒；用户点开报告时
                # 若预生成没完，等待时间就由这条链的长度决定。
                # 其中只有深批者需要人话报告，另两个互不依赖 → 可并行。
                # ================================================================
                _tp = time.time()
                from concurrent.futures import ThreadPoolExecutor

                def _p_hr():
                    if not (session.report and not getattr(session, "human_report", None)):
                        return
                    try:
                        session.human_report = _humanize_report(session.topic, session.report, "full")
                        print("[预生成] 人话报告完成（%.1fs）" % (time.time() - _tp), flush=True)
                    except Exception as e:
                        print(f"[预生成] 人话报告失败: {e}", flush=True)

                def _p_v3():
                    # V16.4：只在用户真的选了 v3 时才跑（否则白花 ~31 秒，且它是已下线的旧尺子）
                    if getattr(session, "review_plugin_id", None) != "v3":
                        print("[预生成] v3 跳过（本局选的是 %s）"
                              % (getattr(session, "review_plugin_id", None) or "默认"), flush=True)
                        return
                    if not (session.report and not getattr(session, "v3_review_result", None)):
                        return
                    try:
                        session.v3_review_result = v3_review(session.report, session.topic)
                        print("[预生成] v3 评审完成（%.1fs）" % (time.time() - _tp), flush=True)
                    except Exception as e:
                        print(f"[预生成] v3 评审失败: {e}", flush=True)

                with ThreadPoolExecutor(max_workers=2) as _ex:
                    for _fu in (_ex.submit(_p_hr), _ex.submit(_p_v3)):
                        _fu.result()
                print("[预生成] 人话化/v3 并行段完成（%.1fs）" % (time.time() - _tp), flush=True)

                # 深批者排在其后（素材是人话报告；拿不到则退原文）
                if session.report and not getattr(session, "deep_critic", None):
                    try:
                        hr = getattr(session, "human_report", None) or ""
                        session.deep_critic = _deep_critic_review(session.topic, session.report, hr)
                        print("[预生成] 深批者完成（%.1fs）" % (time.time() - _tp), flush=True)
                    except Exception as e:
                        print(f"[预生成] 深批者失败: {e}", flush=True)
            finally:
                session._precomputing = False

        threading.Thread(target=_precompute, daemon=True).start()

        # ④ 评审（V16.2 **插件化**）——跑用户选的插件，单独开线程，不占上面那条串行链。
        # 用户要求：「老版本就是快，新版本需要更多的时间，让用户自己选」——
        # 所以这里不再写死 v6，而是按 `session.review_plugin_id`（来自启动参数）挑。
        # 为什么不复用 _precompute：v6 要跑锚点自检 + 三模型分维，实测约 160 秒；
        # 塞进串行链会把「人话报告」一起拖慢（用户点开报告要干等）。
        # 独立线程 → 人话报告/v3/深批者照常先出，评审后到。
        # **不回落**：所选插件不可用就如实记不可用，绝不悄悄换一个顶上。
        def _precompute_review():
            _pid = getattr(session, "review_plugin_id", None)
            print("[预生成] 评审插件线程入口：plugin=%s report=%s 已有结果=%s" % (
                _pid, bool(session.report), bool(getattr(session, "review_result", None))), flush=True)
            try:
                if not session.report:
                    print("[预生成] 评审跳过：session.report 为空", flush=True)
                    return
                if not _pid:
                    print("[预生成] 评审跳过：本局未选择评审插件", flush=True)
                    return
                if getattr(session, "review_result", None):
                    print("[预生成] 评审跳过：已有结果", flush=True)
                    return
                import review_plugins as _RP
                t0 = time.time()
                _out = _RP.run_plugin(_pid, session.report, topic=session.topic)
                _res = _out.get("result") if _out.get("ok") else None
                session.review_result = _res
                session.review_plugin_id = _pid
                print("[预生成] 评审插件 %s 完成（%.0f 秒｜ok=%s）" % (
                    _pid, time.time() - t0, _out.get("ok")), flush=True)
                if not _out.get("ok"):
                    session.review_error = _out.get("error")
                    print("[预生成] 评审插件失败：%s" % str(_out.get("error"))[:200], flush=True)
                # **算完立刻补存一次**：评审约 40–160 秒，而主流程的 save_session 在结果返回时就
                # 执行了，那时评审往往还没跑完 → 落盘里没有这个字段（深批者就是这么丢了 60% 的局）。
                try:
                    from local_store import save_session
                    save_session(session)
                    print("[预生成] 评审结果已补存进会话文件", flush=True)
                except Exception as _e:
                    print("[预生成] 评审补存失败（结果仍在内存里，前端可看）: %s" % _e, flush=True)
            except Exception as e:
                session.review_error = "%s: %s" % (type(e).__name__, str(e)[:200])
                print("[预生成] 评审线程异常: %s" % str(e)[:200], flush=True)

        threading.Thread(target=_precompute_review, daemon=True).start()

    threading.Thread(target=run_and_notify, daemon=True).start()

    return {
        "status": "started",
        "session_id": sid,
        "topic": topic,
        "search_meta": search_meta,
        "v15": True,
    }

@app.get("/api/sandbox/stream")
def stream_session(session_id: str = ""):
    """SSE 实时讨论流"""
    RETRY_MS = 3600000       # 1 小时；SSE 规范字段，单位毫秒

    def _closed(reason):
        def g():
            yield "retry: %d\n\n" % RETRY_MS
            yield "data: %s\n\n" % json.dumps(
                {"type": "done", "message": reason, "closed": True}, ensure_ascii=False)
        return StreamingResponse(g(), media_type="text/event-stream")

    if session_id not in session_queues:
        if active_sessions.get(session_id) is None:
            return _closed("该会话已结束（或服务已重启），不再实时推送；报告请看「决策报告」。")
        return _closed("该会话没有实时流，请看「决策报告」。")
    q = session_queues[session_id]

    def gen():
        yield "retry: %d\n\n" % RETRY_MS     # 先声明重连间隔，防浏览器 3 秒狂连
        idx = 0
        while True:
            while idx < len(q):
                item = q[idx]
                idx += 1
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            if any(e.get("type") == "done" for e in q):
                break
            time.sleep(0.3)
    return StreamingResponse(gen(), media_type="text/event-stream")

@app.get("/api/sandbox/result/{session_id}")
def get_result(session_id: str):
    """获取会话完整结果（讨论结束后的报告/涌现/评估）

    V16.1（2026-10-11）：**内存里找不到时，回落到磁盘上的会话文件**。
    为什么必须这样做（用户原话：「我在页面刷新过后，我原有的报告也消失了，
    跟我们正常用的网页不一样」）：
      会话一直在落盘（`local_store.save_session`），但**这个接口只从内存读**，
      而 `local_store` 原先**只有写、没有读回的函数**。后果是——
      **服务一重启，磁盘上所有会话就再也没人读得到**（实测：重启后旧会话 404）。
      数据没丢，只是没人读。刷新能不能看到，取决于服务进程活没活着，这不合理。
    """
    session = active_sessions.get(session_id)
    if not session:
        # ---- 回落：从磁盘读回（内存里没有 ≠ 数据不存在）----
        try:
            from local_store import load_session
            rec = load_session(session_id)
        except Exception as e:
            rec = None
            print(f"[V16.1] 会话回落读取失败: {e}", flush=True)
        if not rec:
            raise HTTPException(status_code=404, detail="会话不存在（内存与磁盘均无此 session_id）")
        print(f"[V16.1] 会话 {session_id} 不在内存，已从磁盘读回：{rec.get('_loaded_from')}", flush=True)
        # 磁盘记录的字段名与接口载荷不同（v3_review / v6_review vs *_result），逐项映射。
        # 另外磁盘里没有"重启前才在内存里的东西"（如 interventions 的实时快照），
        # 所以拿不到就如实给空，**不要编**。
        return {
            "status": "done",
            "session_id": session_id,
            "topic": rec.get("topic", ""),
            "report": rec.get("report"),
            "human_report": rec.get("human_report"),
            "complexity_level": rec.get("complexity_level", ""),
            "emergence": rec.get("emergence"),
            "evaluation": rec.get("evaluation"),
            "v3_review": rec.get("v3_review"),
            "v6_review": rec.get("v6_review"),          # ◀ 新尺子的读数，重启后依然可看
            # V16.2 插件化：历史记录也要能回答"这局读数哪把尺子给的、跑成没跑成"
            "review_plugin": rec.get("review_plugin"),
            "review": rec.get("review"),
            "review_error": rec.get("review_error"),
            "deep_critic": rec.get("deep_critic", ""),
            "coach_audit": _aggregate_audit(rec.get("coach_audit") or []),
            "references": rec.get("references") or [],
            "citation_check": rec.get("citation_check"),
            "info_pool": rec.get("info_pool") or [],
            "intervention_records": rec.get("intervention_records") or [],
            "intervention_timing": rec.get("intervention_timing", ""),
            "round_signals": rec.get("round_signals") or [],
            "next_session_suggestions": rec.get("next_session_suggestions") or [],
            "logs": rec.get("logs") or [],
            "restored_from_disk": True,                 # 前端可据此提示"这是历史记录"
            "saved_at": rec.get("saved_at", ""),
        }
    if session.running:
        return {"status": "running", "session_id": session_id}
    # 审计意见聚合（v14 引擎按行分割导致 30+ 条，V15 展示聚合为要点）
    audit_raw = session.coach_audit_opinions or []
    audit = _aggregate_audit(audit_raw)

    # v3 评审（优先复用讨论结束时的预生成结果）
    # V16.4 提速：**用户没选 v3 就完全不跑**（含这里的"现场计算"）。
    # 原先无论用户选了什么，只要预生成没命中，就会**在用户正等待的这条路径上**补算 v3
    # （约 31 秒）—— 用户明明选了 v6，却要为一个已下线的旧尺子干等。
    _want_v3_now = (getattr(session, "review_plugin_id", None) == "v3")
    if _want_v3_now and not getattr(session, "v3_review_result", None):
        try:
            report_text = session.report or ""
            session.v3_review_result = v3_review(report_text, session.topic) if report_text else None
            print("[V15] v3 五角色评审完成（现场计算）")
        except Exception as e:
            print(f"[V15] v3 评审失败: {e}")
            session.v3_review_result = None
    elif getattr(session, "v3_review_result", None):
        print("[V15] v3 评审命中预生成缓存", flush=True)
    else:
        print("[V15] v3 跳过（本局未选 v3）", flush=True)

    # 人话化输出（V15.3：所有模式都生成大白话版，公网同学可读）
    v15_meta = getattr(session, "v15_meta", {})
    output_mode = v15_meta.get("output_mode", "standard")
    # ================================================================
    # V16.6 提速（2026-10-11）：用户反馈「决策报告生成一辈子，太慢了」
    # 实测：讨论后处理 91 秒，且**结果路径与预生成线程在算同一件事**（各算一遍）。
    #   · 先**等预生成**（它做的就是人话化/v3/深批者），不重复算；
    #   · 再**并行**跑还缺的环节（v3 与人话化互不依赖，只有深批者需要人话报告）。
    # ================================================================
    # V16.6b：**只等我们真正需要的字段**，不等"整个预生成"。
    # 实测教训：等整个预生成要 67–80 秒，而我们要的 `human_report` 早就好了；
    # 深批者是后置模块，没必要为它挡住整页。
    def _wait_field(attr, timeout):
        if getattr(session, attr, None):
            return True
        if not getattr(session, "_precomputing", False):
            return False
        _tw = time.time()
        while (time.time() - _tw) < timeout:
            if getattr(session, attr, None):
                return True
            if not getattr(session, "_precomputing", False):
                return bool(getattr(session, attr, None))
            time.sleep(0.3)
        return bool(getattr(session, attr, None))

    if getattr(session, "_precomputing", False):
        _tw0 = time.time()
        print("[V16.6b] 预生成在跑：只等需要的字段（人话报告≤90s）...", flush=True)
        _got_hr = _wait_field("human_report", 90)
        print("[V16.6b] 人话报告 %s（等了 %.0f 秒；预生成整体仍在后台跑）"
              % ("已就绪" if _got_hr else "未就绪，将自行计算", time.time() - _tw0), flush=True)

    human_report = getattr(session, "human_report", None)   # 优先用预生成
    _need_hr = (not human_report) and bool(session.report)
    _need_v3 = (not getattr(session, "v3_review_result", None)) and bool(session.report)
    if not _need_hr and human_report:
        print("[V15.2] 人话报告命中预生成缓存", flush=True)
    if not _need_v3 and getattr(session, "v3_review_result", None):
        print("[V15] v3 评审命中预生成缓存", flush=True)

    if _need_hr or _need_v3:
        from concurrent.futures import ThreadPoolExecutor
        _t0p = time.time()

        def _job_hr():
            try:
                hr = _humanize_report(session.topic, session.report,
                                      "full" if output_mode == "standard" else output_mode)
                print("[V15.2] 人话化完成（现场计算 %.1fs）" % (time.time() - _t0p), flush=True)
                return "hr", hr, None
            except Exception as e:
                print(f"[V15.2] 人话化转译失败: {e}")
                return "hr", None, str(e)[:150]

        def _job_v3():
            # 复用与预生成相同的实现（v3_review）——**只在一处定义**，避免两套逻辑漂移
            try:
                r = v3_review(session.report, session.topic)
                print("[V15] v3 五角色评审完成（现场计算 %.1fs）" % (time.time() - _t0p), flush=True)
                return "v3", r, None
            except Exception as e:
                print(f"[V15] v3 评审失败: {e}")
                return "v3", None, str(e)[:150]

        # V16.6 熔断③：**后处理总截止**。原先是无限等 —— 一次模型挂住就会把
        # 「决策报告」拖到十分钟（用户实测原话："生成一辈子"）。
        # 现在到点就用已有结果返回；缺哪个模块由前端如实标注，绝不空着等。
        _POST_DEADLINE = int(os.getenv("QZ_POST_DEADLINE", "240"))
        _deadline = _t0p + _POST_DEADLINE
        _res = {}
        with ThreadPoolExecutor(max_workers=2) as ex:
            _futs = {}
            if _need_hr:
                _futs[ex.submit(_job_hr)] = "hr"
            if _need_v3:
                _futs[ex.submit(_job_v3)] = "v3"
            for fu in _futs:
                _left = max(1.0, _deadline - time.time())
                try:
                    k, val, err = fu.result(timeout=_left)
                    _res[k] = (val, err)
                except Exception as _e:
                    print("[V16.6] 熔断：%s 超过后处理截止（%.0fs），放弃等待（%s）"
                          % (_futs[fu], _POST_DEADLINE, type(_e).__name__), flush=True)
                    _res[_futs[fu]] = (None, "超过后处理截止 %ds" % _POST_DEADLINE)
        print("[V16.6] v3 与人话化并行完成（共 %.1fs）" % (time.time() - _t0p), flush=True)
        if _need_hr and not human_report:
            human_report = _res.get("hr", (None, None))[0]
            if not human_report:
                human_report = session.report        # 兜底：至少拿原文，不让报告空着
        if _need_v3 and not getattr(session, "v3_review_result", None):
            session.v3_review_result = _res.get("v3", (None, None))[0]
    session.human_report = human_report

    # 深批者：先算，**后存盘**。
    # 事故（2026-10-11）：原顺序是「先 save_session → 后算深批者」，而预生成跑在 daemon 线程里
    # 串行做「人话报告 → v3 评审 → 深批者」，存盘时深批者常常还没跑完 →
    # **最近 10 局里 6 局落盘的 deep_critic 是空字符串**，实验数据里这个字段基本不可用。
    # 现在把计算提到存盘之前：预生成命中就零等待，未命中就现场算完再存。
    deep_critic = getattr(session, "deep_critic", "")
    if not deep_critic:
        # V16.6 熔断③续：深批者也放进线程并设截止，不让它把整页拖住
        try:
            print("[深批者] 开始质疑示范（现场计算）...", flush=True)
            import concurrent.futures as _cf
            with _cf.ThreadPoolExecutor(max_workers=1) as _ex1:
                _fu1 = _ex1.submit(_deep_critic_review, session.topic,
                                   session.report or "", human_report or "")
                try:
                    deep_critic = _fu1.result(timeout=int(os.getenv("QZ_DC_DEADLINE", "90"))) or ""
                except Exception as _e1:
                    print("[V16.6] 熔断：深批者超时，本局不展示（%s）" % type(_e1).__name__, flush=True)
                    deep_critic = ""
            session.deep_critic = deep_critic
        except Exception as e:
            print(f"[深批者] 失败: {e}", flush=True)
            session.deep_critic = ""
            deep_critic = ""
    else:
        print("[深批者] 命中预生成缓存", flush=True)

    # 本地数据留存（V15.3：每次讨论结果落盘）——**放在深批者之后**，保证落盘字段完整
    saved_path = None
    try:
        from local_store import save_session
        saved_path = save_session(session)
        print(f"[V15.3] 已留存本地数据: {saved_path}", flush=True)
    except Exception as e:
        print(f"[V15.3] 本地留存失败: {e}", flush=True)

    # 诚实声明（底部小字）：AI 输出偏理想化，需人类判断
    honesty_note = ("⚠️ AI 团队讨论天然偏理想化——它假设资源到位、人配合、制度可执行。"
                    "真实世界的预算约束、利益博弈、执行阻力，需要你在决策时自行补上。"
                    "本报告的讨论机制来自 798 轮实验（口径与数据见提交材料：实验证据总表/实验数据表/实验背书报告），"
                    "评审为 AI 独立评审、暂无人类盲评，仅供参考，不替代专业判断。")

    return {
        "status": "done",
        "session_id": session_id,
        "topic": session.topic,
        "deep_critic": deep_critic,          # 深批者视角（对抗性评审）
        "honesty_note": honesty_note,        # 底部诚实声明
        "report": session.report,
        "human_report": human_report,              # V15.2：人话化报告
        "saved_path": saved_path,                  # V15.3：本地数据留存路径
        "complexity_level": v15_meta.get("complexity_level", "中等"),  # V15.2
        "output_mode": output_mode,
        "emergence": session.emergence_analysis,
        "evaluation": session.report_evaluation,       # 引擎旧评分（兼容保留）
        "v3_review": session.v3_review_result,          # v3 五角色评审（1–10 分已下线，仅留原文证据）
        # V16.2 插件化：`review` 是**通用**字段（哪个插件跑的/结果/错误），
        # `v6_review` 保留为兼容别名（老前端与老脚本仍读它）。
        "review_plugin": getattr(session, "review_plugin_id", None),
        "review": getattr(session, "review_result", None),
        "review_error": getattr(session, "review_error", None),
        "v6_review": ((getattr(session, "review_result", None) or {}).get("v6")
                      if getattr(session, "review_plugin_id", None) == "v6"
                      else getattr(session, "v6_review_result", None)),
        "cards": session.cards,
        "golden_insight": session.golden_insight,
        "coach_audit": audit,
        "v15_meta": v15_meta,
        # ---- V15 优化（2026-09-25）：引用与干预 ----
        "references": getattr(session, "references", []),          # 参考文献结构化清单
        "citation_check": getattr(session, "citation_check", None),  # 三类标记
        "interventions": getattr(session, "intervention_records", []),   # 本局逐轮干预记录
        "intervention_timing": getattr(session, "intervention_timing", "per_round"),
        "round_signals": getattr(session, "round_signals", []),          # 每轮客观信号（复现用）
        # V16.4：**讨论记录必须进载荷**（原先只有"磁盘回落"那条路径带 logs）。
        # 用户反馈「我的讨论记录怎么又不见了」：日志只在 SSE 实时流里渲染过，
        # 一刷新前端就没有可重放的数据 —— 而 `session.logs` 一直都是有的（落盘 50 条）。
        # 数据没丢，是接口没把它给出去、前端也没画。两边都补上。
        "logs": getattr(session, "logs", []),                            # 完整讨论过程（可重放）
        "next_session_suggestions": getattr(session, "next_session_suggestions", []),  # 留给下一局
        # 场景化决策报告（A/B 融合）——单独兜底：场景报告失败不应连累整个结果接口
        "scenario_report": _safe_scenario_report(
            v15_meta.get("scenario", "通用"),
            session.topic, session.report, session.report_evaluation,
            audit,
        ),
    }


def _safe_scenario_report(*args):
    """调用 get_scenario_report 并兜底（失败返回 None，不影响主报告展示）"""
    try:
        return get_scenario_report(*args)
    except Exception as e:
        print(f"[场景报告] 生成失败（已降级为不展示该模块）: {str(e)[:150]}", flush=True)
        return None

# ==================== 平台同步接口（学校低代码平台用） ====================

class PlatformRunRequest(BaseModel):
    topic: str
    user_thought: str = ""

@app.post("/api/platform/run")
def platform_run(req: PlatformRunRequest):
    """同步执行一次完整预演，返回决策建议书（供学校低代码平台 HTTP 插件直接调用）。
    实现：内部通过已验证的 start→轮询 result 路径（复用同一网关的异步流程），
    避免直接操作 session 内部状态导致的不稳定。
    """
    topic = req.topic.strip()
    if not topic:
        raise HTTPException(status_code=400, detail="请输入课题")

    # 1. 走 start_session 逻辑（异步启动讨论线程）
    start_req = SandboxStartRequest(
        topic=topic, user_thought=req.user_thought,
        max_rounds=2, info_mode="real", scenario="通用", preset_name="通用科研",
    )
    started = start_session(start_req)
    sid = started.get("session_id")
    if not sid:
        return {"status": "error", "message": "预演启动失败", "topic": topic}

    # 2. 轮询结果（最长等 7 分钟）
    deadline = time.time() + 420
    while time.time() < deadline:
        sess = active_sessions.get(sid)
        if sess and not sess.running:
            # 已完成，组装精简结果
            v15_meta = getattr(sess, "v15_meta", {})
            human_report = getattr(sess, "human_report", None)
            if human_report is None and sess.report:
                try:
                    human_report = _humanize_report(sess.topic, sess.report, "full")
                    sess.human_report = human_report
                except Exception as e:
                    print(f"[platform] 人话化失败: {e}")
                    human_report = sess.report
            scenario_report = None
            try:
                scenario_report = get_scenario_report(
                    "通用", sess.topic, sess.report, sess.report_evaluation,
                    sess.coach_audit_opinions or [],
                )
            except Exception as e:
                print(f"[platform] 场景化失败: {e}")
            return {
                "status": "done",
                "topic": topic,
                "report": sess.report,
                "human_report": human_report or sess.report or "",
                "scenario_report": scenario_report,
                "emergence": sess.emergence_analysis,
                "complexity_level": v15_meta.get("complexity_level", "中等"),
            }
        time.sleep(5)
    return {"status": "timeout", "message": "预演超时，请稍后重试", "topic": topic}

def _humanize_report(topic: str, report: str, output_mode: str) -> str:
    """人话化转译：把讨论报告翻译成决策者直接能懂的话"""
    if output_mode == "quick":
        prompt = f"""你是教研导师。课题：{topic}
请把下面这份讨论报告，压缩成【快速建议】，说人话，不展开理论。
格式：
一句话结论：【30字内】
三步行动：
①【本周能做的具体动作】
②【下一步的改进动作】
③【一个最常踩的坑】
报告：
{report[:2500]}"""
        result = call_llm(prompt, provider="qwen", max_tokens=1500, temperature=0.5, timeout=60)
        return result or report
    else:
        prompt = f"""你是决策顾问。课题：{topic}
把下面这份讨论报告，改写成【人话化决策建议书】。
要求：每部分先给结论再给支撑，专业术语翻译成大白话。
格式：
一句话结论：【30字内，直接回答该不该/怎么做】
给决策者的三步行动：
①【本周可做的具体动作】
②【一个月内的推进动作】
③【长期的结构性动作】
你的认知盲区：【讨论中发现、提问者没想到的角度】
支撑细节：【1-2句说明依据】
报告：
{report[:6000]}"""
    result = call_llm(prompt, provider="qwen", max_tokens=4000, temperature=0.5, timeout=120)
    return result or report

def _deep_critic_review(topic: str, report: str, human_report: str = "") -> str:
    """深批者：**给用户看的「质疑示范」**。

    ⚠ 设计意图（2026-10-11 据作者澄清后重写，这是本次改动的核心）：
      深批者**不是**又一个对抗性评审，而是**把「质疑 AI」这件事做给用户看**——
      告诉大家：AI 时代要有自己的思考，要敢于质疑 AI。

    因此评价标准不是"批得对不对"，而是"**用户看完，会不会质疑了**"：
      · 原先的 prompt 通篇是"你是专门负责拆台的资深从业者"——**它不知道自己是示范**，
        于是产出的是"拆台"，不是"示范质疑的方法"；
      · 实测暴露的问题：全是"**可能**难以找到资源"式的空想、⑤给出"确保教师培训到位"这种
        它自己 prompt 明令禁止的空话、以及无任何依据的"理念 4/10 落地 2/10"。
        **用户会模仿它示范的语言**——示范空话，就是在教用户说空话。

    现在的三条硬改造：
      ① 每条质疑必须挂一行「💬 你可以这样问 AI」——**把锤子递给用户**，让他拿走可复用的提问；
      ② **禁空话清单 + 代码级检查**（见 _deep_critic_has_empty_talk）：命中就重生成一次；
      ③ 结尾**邀请用户反驳深批者自己** —— 三层质疑：AI 生成 → AI 质疑 → 用户质疑 AI 的质疑。
    """
    source = (human_report or report) or ""
    prompt = f"""你是群智的「深批者」。

【你存在的意义 —— 先读这一段】
群智的信念是：**AI 时代，人要有自己的思考，要敢于质疑 AI。**
但"敢于质疑"不是一句口号——多数人不知道该从哪儿下手。所以由你**先示范一遍**：
让用户看到"原来可以这样质疑"，然后他才有底气去质疑别的 AI 输出，**包括质疑你**。

所以你的输出会被当成**范例**来看。这不只是"批得对"的问题，而是"**批得让人能学会**"的问题：
用户会模仿你的句式、你的推理方式、你的分寸。**你示范空话，就是在教用户说空话。**

课题：{topic}

【硬性要求（违反即不合格）】
1. 每条质疑必须**点名报告里的具体建议**（引用其原话或内容片段），禁止"存在风险""需注意执行"这类空话；
2. 每条质疑必须给出**可运转的机制**：谁、在什么情况下、会怎么做、导致什么后果。
   **禁止用"可能"代替机制**（"可能难以找到资源"不是质疑，是猜测）；
3. 区分两类并标注：❌ **方向错**＝这条建议的前提本身不成立；⚠️ **执行难**＝方向对但推不动，卡在哪一环；
4. **【本次新增，最重要】** 每条质疑后面必须另起一行给一个**追问**，让用户下次能自己用：
   `💬 你可以这样追问 AI：`
   它只有两种合格形态，**必须是这两种之一**：
   · **甲｜逼 AI 交底**：问得让 AI 要么拿出**可核查的依据**、要么**承认拿不出来**。
     ❌ 不合格：`匿名提问的实施比例是多少？`——AI 会随口给个数字或套话，你什么都没得到；
     ✅ 合格：`你说"学生怕被评价"——这句话在你引的那几篇里有原文吗？把标题和原句贴出来；贴不出来就直说没有。`
   · **乙｜去现实里验证**：给一个用户**今天就能做**的动作。
     ✅ 合格：`随机找 10 个学生问一句"这学期最让你睡不着的是哪件事"，看答案里是期末考还是保研。`
   **判断标准：照这句话做完，用户能拿到一个原本拿不到的信息**（一个承认、一份原文、一条真实反馈）。
5. 结尾必须**邀请用户反驳你**：点名你认为自己**最站不住的那一条**，说清它可能错在哪。
   这是本环节的立意——**连"教你质疑的 AI"本身也应该被质疑**。

【禁用语（出现即判不合格，会被程序打回重写）】
- "可能难以""可能会""或许会"这类**替代机制**的模糊猜测；
- "确保…到位""建立有效的…体系""加强…建设""提高…意识"这类**没有动作的空话**；
- "总体而言""综合来看""建议还是有一定价值的"这类和稀泥；
- **任何没有列出扣分项依据的分数**。

【检查维度】
① **最脆弱的假设**：报告默认了哪些"人会配合／资源到位／制度会执行"？哪一条不成立整套就崩？
② **现实阻力的落点**：点名哪条建议在真实环境（预算／人力／部门利益／流程惯性）下推不动，卡在谁那里；
③ **会被怎么钻空子**：利益足够大时谁会"反向适应"这套建议？具体怎么钻；
④ **最坏的代价**：按这套执行，最坏情况伤害谁？有什么被忽略的副作用；
⑤ **落地前置条件**：坚持要做，必须先具备什么（1-2 条**可检查**的条件，不要空话）。

【范例 —— 这是**另一个课题**，只看形状，内容要换成当前课题】
课题：大学该不该取消期末考试
**① 最脆弱的假设**
报告假设"取消期末考就能降低学生焦虑"，这依赖"焦虑主要来自考试"。
但补考、绩点排名、保研竞争都没变——焦虑只是换了个出口。
💬 你可以这样追问 AI：你写"考试是焦虑主因"，这话在你引的那几篇里有原文吗？
把标题和原句贴出来；贴不出来就直说没有。

**② 现实里会卡在哪**
卡在教务处。课程成绩要进学籍系统、要参与保研排序，
教务员一个人管 3000 人，他不会为某一门课的"无成绩"单独开一条流程。
💬 你可以这样追问 AI：这门课不出成绩，学籍系统那一栏具体填什么？请给出字段名和操作路径。

【输出格式】600-900 字。**每节都要先写 2-4 句质疑正文，再写 💬 行**——只写 💬 行是不合格的。
标题与 💬 行照抄，其余写内容：

### 🧐 深批者：AI 也会错，给你打个样

**① 最脆弱的假设**

💬 你可以这样追问 AI：

**② 现实里会卡在哪**

💬 你可以这样追问 AI：

**③ 会被怎么钻空子**

💬 你可以这样追问 AI：

**④ 最坏的代价**

💬 你可以这样追问 AI：

**⑤ 如果一定要做，先解决**

**📊 打分**

**🙋 现在轮到你了**

（两处格式说明，只此一次：**📊 打分** 写两行，形如
`理念 8/10：扣分项 —— <你实际列出的依据>`
`落地 7/10：扣分项 —— <你实际列出的依据>`，分数由你自己给（0-10 整数）。
**🙋** 写明你认为自己哪一条最站不住、可能错在哪，并请用户反驳你。）

【写完自查三件事】
1. 四条 💬 是否**逼 AI 交底**或**给现实动作**（不是泛泛征询，也不是把质疑句加问号）？
2. 把每个"可能"都改写成"**谁、在什么情况下、会怎么做**"——数一数还剩几个"可能"；
3. 有没有出现"确保…到位""建立有效的…"这类没有动作的话？

团队产出：
{source[:5000]}"""
    ALIAS = "深批者"
    for _attempt in (1, 2):
        try:
            # V15.4：深批者分流到 GLM——该模型在批判/挑刺类任务上表现突出
            r = call_llm(prompt, provider="glm", max_tokens=2400, temperature=0.6, timeout=120)
        except Exception as e:
            print(f"[{ALIAS}] 调用失败: {e}", flush=True)
            return ""
        txt = (r or "").strip()
        if not txt:
            return ""
        hits = _deep_critic_empty_talk(txt)
        if not hits:
            return _deep_critic_soft_notes(_deep_critic_sanitize(txt))
        print(f"[{ALIAS}] 第 {_attempt} 次产出命中空话清单 {hits}，重写", flush=True)
        if _attempt == 2:
            # 第二次仍命中 → **如实标注**，不假装通过（与项目"不利结果如实报告"一致）
            return (_deep_critic_sanitize(txt) +
                    "\n\n> ⚠️ 本节经程序检查仍有疑似空话：%s。"
                    "请以此为例注意——**AI 的质疑本身也可能不合格**。" % u"、".join(hits))
    return ""


# 脚手架清洗：模型会把"给它看的格式说明"照抄进正文（实测两轮都发生：
# 把【点名 + 具体机制】、（逐条）、（说清可能错在哪）原样输出）。
# 换成只留标题后应大幅减少，但**不能只靠 prompt**——prompt 已经失败过两次，
# 所以在代码层再兜一道，并在触发时打日志（日志出现即说明 prompt 还需收紧）。
_SCAFFOLD_RE = re.compile(
    u"[（(][^）)]{0,140}?(?:点名|说清|具体钻法|逐条|可检查|谁会受损|卡在哪|图什么|"
    u"一个能立刻执行|答案会暴露|报告里那句话|写给你看)[^）)]{0,140}?[）)]")


def _deep_critic_sanitize(text: str) -> str:
    """剥掉漏进正文的格式说明（脚手架），并记录。"""
    t = str(text or "")
    hits = _SCAFFOLD_RE.findall(t)
    if hits:
        t = _SCAFFOLD_RE.sub(u"", t)
        print(f"[深批者] 清洗掉 {len(hits)} 处脚手架泄漏：{str(hits[:3])[:120]}", flush=True)
    # 空标题行（清理后可能留下只有标题、内容被清掉的节）——把连续空行压成一个
    t = re.sub(u"\n{3,}", u"\n\n", t)
    # **残句处理**：剥掉括号后可能留下断句（"扣分项 ——"、"因为它。"）。
    # 直接留着比脚手架更难看，所以补一句**如实**的说明，而不是编内容。
    t = re.sub(u"扣分项\\s*——\\s*$", u"扣分项 ——（模型未填写，本节不合格）", t, flags=re.M)
    t = re.sub(u"因为它\\s*[。，,；;]?\\s*$", u"因为它**没有给出具体理由**——这本身就是一处反面示范。",
               t, flags=re.M)
    # "第 N 条"字面残留（模型没替换编号）
    t = t.replace(u"第 N 条", u"其中一条").replace(u"第N条", u"其中一条")
    return t


# 空话清单：这些句式是"**看起来在质疑、实际没给出任何机制**"的典型。
# 之所以要做程序级检查：深批者的输出是**给用户看的质疑范例**，
# 而**示范的语言就是用户会模仿的语言**——示范空话等于教用户说空话。
_EMPTY_TALK = (
    # V16 第六轮实测补漏：旧清单只有"建立有效的"，而模型写的是"建立**科学合理的**评估体系"、
    # "确保教师具备有效引导课堂互动的**能力**"——**换了形容词就绕过了检查**。
    # 所以这里按**句式骨架**匹配（建立…体系 / 确保…能力 / 合理分配…），不绑定具体形容词。
    # 注意长度上界：实测句是"确保教师具备有效引导课堂互动的**能力**"——「能力」前有 **13** 个字，
    # 最初写成 {0,12} 恰好漏掉，自检当场抓到。留足余量写 {0,20}。
    u"确保[^。；\n]{0,20}(?:到位|能力|实施|支持|落实)",
    u"建立[^。；\n]{0,20}(?:体系|机制|制度)",
    u"加强[^。；\n]{0,8}建设", u"提高[^。；\n]{0,8}(?:意识|水平|能力)",
    u"合理分配", u"科学合理", u"进一步完善", u"积极探索",
    u"总体而言", u"综合来看", u"有一定价值", u"可能存在一定", u"或许会",
)


def _deep_critic_empty_talk(text: str) -> list:
    """返回命中的空话句式（空列表 = 通过）。"""
    import re as _re
    t = str(text or "")
    return [p for p in _EMPTY_TALK if _re.search(p, t)]


def _deep_critic_soft_notes(text: str) -> str:
    """软提示（不重写）：把"可能的密度"如实标注出来。

    "可能"在**确实给了机制**时是合法的限定词，所以不做硬检查、不触发重写；
    但实测旧版产出通篇是"**可能**难以找到资源""教师**可能**不愿意"——**用可能替代机制**。
    密度过高时给读者一句提示，让他知道该往哪儿追问。
    """
    t = str(text or "")
    n = t.count(u"可能")
    if n < 4:
        return t
    return (t + "\n\n> ℹ️ 本节出现 %d 次「可能」。**限定词不等于机制**——"
                 "读到「可能会…」时，请追问一句：**谁、在什么情况下、会怎么做？**"
            % n)

def _aggregate_audit(raw: list, max_items: int = 4) -> list:
    """把引擎产生的审计意见行聚合为 3-5 条要点（对非字符串输入做兜底，避免 AttributeError）"""
    if not raw:
        return []
    # 去掉 markdown 装饰行（先统一转成字符串：raw 里可能混入 None / dict）
    cleaned = [str(l).strip() for l in raw
               if l is not None and str(l).strip()
               and not str(l).strip().startswith("#") and str(l).strip() != "---"]
    # 按 "意见" / 序号 / 关键行 提取
    items = []
    for line in cleaned:
        if any(k in line for k in ("意见", "观察到", "建议", "挑战", "验证", "下一步", "风险")):
            # 聚合连续行
            if items and len(items[-1]) < 60 and line.startswith(("**", "-")):
                items[-1] += " " + line.strip("*- ")
            else:
                items.append(line.strip("*- "))
    # 去重 + 截断
    seen = set()
    result = []
    for item in items:
        key = item[:20]
        if key not in seen and len(item) > 10:
            seen.add(key)
            result.append(item[:150])
        if len(result) >= max_items:
            break
    return result if result else cleaned[:max_items]

# ==================== 主教练对话 ====================

@app.post("/api/coach/chat")
def coach_chat(req: CoachChatRequest):
    """主教练对话（提交版：无实验记忆库）"""
    try:
        coach = Coach(call_with_retry)
        response = coach.chat(req.message, None)
        return {"ok": True, "reply": response}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==================== 评审 ====================

@app.post("/api/review/evaluate")
def review_evaluate(req: ReviewRequest):
    """v3 五角色独立评审（理论家/实践者/批评家/审计员/方法论审计员，双模型）

    ⚠ 2026-10-11 起：**1–10 分这把尺子已下线**（v6 取代），但本端点**逐字保留**——
    原因有二：① 它有 T7 回归测试守着（旧端点结构未变）；② v6 是"并存"，不是"覆盖"。
    前端不再展示它的 score，只保留其"原文证据"部分。
    """
    try:
        result = v3_review(req.report, req.topic)
        return {"ok": True, "review": result, "v3": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# 群智评审机制 v6 接入层（**打分尺子下线，改用成对强制选择**）
# ------------------------------------------------------------
# 依据（全部来自你自己的实验报告，引自 04_评审系统实验/评审系统报告/）：
#   · 01_v6实验结果_人话版.md §七：**v3 在同一批 18 对上只挑对 8/18 = 44%，低于瞎猜**。
#     归因已查清 —— **不是它笨，是它的尺子没有刻度**：所有报告分数全挤在 6.4–7.2
#     （跨度 0.8 分），而"判定有差别"的门槛是 0.5 分 → 18 对里 10 对落进"看起来一样"。
#   · 01 §三：v6（成对强制选择）三家 18/18、17/18、17/18 全部达标，且**两轮一致**。
#   · 03_今晚新增_探路与闸门_人话版.md：当初那个"体检没过"的悬案已定因 ——
#     **"是记分记错了，不是模型认不出"**，且已修（核对口径松紧再也左右不了主结论）。
# 设计纪律（v6 预注册，逐条遵守）：
#   ① 不许给自己的东西打分 —— 评审模型必须与生成模型不同族（deepseek）；**不回落**
#   ② 不许拿总分当门面 —— 不用 1–10 分；**锚点未过则一个分数都不给**（scores_hidden）
#   ③ 每条判断必须带原文证据
# 只**新增**端点，不修改任何现有端点行为（上面 /api/review/evaluate 逐字未动）。
# ============================================================

@app.get("/api/review/v6/health")
def review_v6_health():
    """v6 生效证据。

    预注册写明：**启动日志不能作为生效证据（可能被吞）**，
    必须以本端点的可自动断言为准（自带测试 T1 就是断言这个）。
    """
    try:
        import review_v6 as _v6
        reviewers = list(getattr(_v6, "REVIEWERS", []))
    except Exception as e:
        return {"v6": False, "error": f"review_v6 不可导入：{type(e).__name__}: {str(e)[:120]}"}
    # 锚点材料是否就位 —— 缺了会导致 anchors_passed=None → **永远不出分**（静默失效）。
    # 这里主动报出来，让"没分数"这件事在 health 上就能看见，而不是让用户猜。
    _anchor_dir, _anchor_ok, _anchor_n = "", None, 0
    try:
        import review_anchors as _ra
        _anchor_dir = getattr(_ra, "MATERIAL_DIR", "")
        _anchor_n = len(_ra.load_materials())
        _anchor_ok = True
    except Exception as e:
        _anchor_ok, _anchor_dir = False, f"{type(e).__name__}: {str(e)[:120]}"
    return {"v6": True, "reviewers": reviewers, "n_reviewers": len(reviewers),
            "anchors_required": True, "no_fallback": True,
            "anchor_materials_ok": _anchor_ok, "anchor_dir": _anchor_dir,
            "anchor_materials": _anchor_n}


@app.get("/api/review/v6/schema")
def review_v6_schema():
    """v6 的输入/输出契约（免猜字段名；/api/tools 清单里也有摘要）。"""
    return {
        "ok": True, "id": "review_v6", "method": "POST", "path": "/api/review/v6",
        "input": {
            "report": "string（必填，待评审的报告/建议书正文）",
            "topic": "string（可选，课题）",
            "baseline": "string（可选；不传则 pairwise 层按设计跳过）",
        },
        "output": {
            "review_v6.pairwise": "对象（传了 baseline 且有有效投票）或 null（没传 baseline）",
            "review_v6.pairwise_skipped": "没传 baseline 时存在，code=no_baseline，说明本层跳过",
            "review_v6.anchors_passed": "锚点自检结果；不为 true 时 scores_hidden=true",
            "pairwise": "顶层镜像，同 review_v6.pairwise",
            "pairwise_skipped": "顶层镜像，同 review_v6.pairwise_skipped",
        },
        "notes": [
            "不传 baseline 时 review_v6.pairwise=null，这是设计行为，不是 502/引擎失败。",
            "需要成对强制选择（pairwise 主读数）时，请传 baseline。",
            "锚点自检检验评审模型当前能力，与本次输入报告内容无关。",
        ],
    }


class ReviewV6Request(BaseModel):
    report: str
    topic: str = ""
    baseline: str = ""


@app.post("/api/review/v6")
def review_v6_evaluate(req: ReviewV6Request):
    """v6 评审：成对强制选择 + 分维**三档**（强/中/弱/无依据，不求和）+ 风险栏 + 独有贡献。

    **不回落**：任一评审模型不可用 → 返回 unavailable，绝不悄悄用少数模型顶替。
    **锚点未过** → `scores_hidden=True`，不产出任何分数。

    **pairwise（2026-11-21 补充文档）**：只有传 `baseline` 才会跑第 2 层
    「成对强制选择」。不传 baseline 时：
      · `review_v6.pairwise` = `null`（本层按设计跳过，不是引擎故障）；
      · `review_v6.pairwise_skipped` = `{"skipped": true, "code": "no_baseline", ...}`；
      · 顶层同时镜像 `pairwise` / `pairwise_skipped`，方便调用方一眼判断。
    需要 pairwise 主读数时，请在请求体里传 `baseline`。
    """
    try:
        import review_v6 as _v6
    except Exception as e:
        return {"ok": False, "v6": False,
                "error": f"review_v6 不可导入：{type(e).__name__}: {str(e)[:200]}"}
    try:
        out = _v6.v6_review(req.report, topic=req.topic,
                            baseline=(req.baseline or None))
        # 四关一审（/gates）：把本次真实运行记进状态快照，供页面展示"最近一次实测"。
        # 只记录，不影响评审返回；记录失败也不能影响主流程。
        try:
            _gates_record_v6(out)
        except Exception:
            pass
        return {"ok": True, "review_v6": out,
                # 2026-11-21（Bug 4）：pairwise 语义镜像到顶层（只新增字段）。
                # 不传 baseline 时 pairwise=null、pairwise_skipped 带 no_baseline。
                "pairwise": out.get("pairwise"),
                "pairwise_skipped": out.get("pairwise_skipped"),
                # 2026-11-21：把锚点语义镜像到顶层（只新增字段）。
                "anchors_passed": out.get("anchors_passed"),
                "anchors_scope": out.get("anchors_scope"),
                "anchors_cached": out.get("anchors_cached"),
                "anchors_cached_at": out.get("anchors_cached_at")}
    except Exception as e:
        # 硬校验失败（如"锚点未过却产出了分数"）会走到这里 —— 必须如实报错，不得静默
        return {"ok": False, "v6": True,
                "error": f"{type(e).__name__}: {str(e)[:300]}"}


# ============================================================
# 评审插件化（V16.2，2026-10-11）
# ------------------------------------------------------------
# 用户要求：「把这个系统模块化，里面的工具要做成插头即插即用，也方便我们后面的维护」
#          「v3 的保留，就是给用户一个选择的机会，老版本就是快，新版本需要更多时间，让用户自己选」
#
# 做法：所有评审器都是 `app/review_plugins/` 下的**独立插件**，实现同一个契约
#       （PLUGIN 元数据 + available() + run()）。加一个新评审器 = 丢一个文件，
#       **不改本文件任何一行**。本层只负责把插件清单与调用结果转成 HTTP，
#       评审逻辑一行都不在这里 —— 这正是"即插即用"要的边界。
# 旧端点 `/api/review/evaluate`（v3）与 `/api/review/v6` **逐字保留**：
#       ① 有回归测试守着；② 老用户/老脚本仍在调；③ 插件层是新增，不是替换。
# ============================================================

@app.get("/api/review/plugins")
def review_plugins_list():
    """评审插件清单（含**耗时/成本/产出/可用性**，供用户自己权衡"快"与"准"）。"""
    try:
        import review_plugins as RP
        return {"ok": True, "plugins": RP.list_plugins(),
                "load_errors": RP.load_errors()}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


class ReviewRunRequest(BaseModel):
    report: str
    topic: str = ""
    baseline: str = ""


@app.post("/api/review/run/{plugin_id}")
def review_run_plugin(plugin_id: str, req: ReviewRunRequest):
    """按插件 id 跑评审（统一入口）。失败一律返回结构化错误，不抛 500。"""
    try:
        import review_plugins as RP
    except Exception as e:
        return {"ok": False, "error": f"review_plugins 不可导入：{type(e).__name__}: {str(e)[:200]}"}
    return RP.run_plugin(plugin_id, req.report, topic=req.topic, baseline=req.baseline or "")


# ============================================================
# 统一插件接口（V16.3，2026-10-11）
# ------------------------------------------------------------
# 用户要求：「我希望不只是评审系统模块化，而是能模块化的都可以这样」
# 所以插件不再只有"评审器"一类，而是按 kind 分组（review / source / …），
# 统一由 `app/plugin_registry.py` 发现与调度。
# **加一类插件**（例如以后把"报告生成器""深批者"也插件化）= 在 KINDS 里加一行 + 建目录，
# 本文件只需再加一个可选入口；**加同类插件**则连这里都不用改。
# ============================================================

@app.get("/api/plugins")
def plugins_list(kind: Optional[str] = None):
    """全部插件（可按 kind 过滤）：评审器 / 检索源 / …，各带耗时成本与可用性。"""
    try:
        import plugin_registry as PR
        return {"ok": True, "kinds": PR.KINDS, "plugins": PR.list_plugins(kind=kind),
                "load_errors": PR.load_errors(kind=kind)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/api/plugins/sources")
def plugins_sources(kind_free: bool = True):
    """本局实际会跑的检索源（按插件自报的 default_on / applies_when / order 计算）。

    这是"路由也是插件自报"的可核查证据：把源清单与顺序直接暴露出来，
    而不是让路由规则埋在函数里没人看得见。
    """
    try:
        import plugin_registry as PR
        demo = PR.active_sources("示例课题", {"cn_terms": True, "med": False, "effective": False})
        return {"ok": True,
                "default_route_cn": [{"id": a["id"], "name": a["name"], "order": a["order"]}
                                     for a in demo],
                "all_sources": [{k: v for k, v in p.items()
                                 if k in ("id", "name", "desc", "lang", "free", "web",
                                          "eta_sec", "default_on", "order", "applies_when",
                                          "available", "unavailable_reason")}
                                for p in PR.list_plugins(kind="source")]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}

# ==================== 课堂模拟器（V15 改造） ====================

# 课堂模拟器实例（复用 classroom_engine）
try:
    from classroom_engine import ClassroomSimulator, TeachingPhase, InstructionRequest
    _classroom = ClassroomSimulator()
    CLASSROOM_OK = True
    print("[课堂模拟器] 引擎加载成功", flush=True)
except Exception as e:
    CLASSROOM_OK = False
    print(f"[课堂模拟器] 引擎加载失败: {e}", flush=True)


@app.get("/classroom")
def classroom_page():
    """课堂模拟器前端"""
    page = os.path.join(V15_ROOT, "app", "templates", "classroom.html")
    if os.path.exists(page):
        return FileResponse(page)
    return {"ok": CLASSROOM_OK, "hint": "classroom.html 未找到"}


@app.post("/api/classroom/teach")
def classroom_teach(req: dict):
    """老师输入教学指令 → 学生响应 + 课堂反馈"""
    if not CLASSROOM_OK:
        raise HTTPException(status_code=500, detail="课堂引擎未加载")
    try:
        instruction = req.get("instruction", "")
        phase_str = req.get("phase", "导入")
        phase = TeachingPhase(phase_str) if phase_str in [p.value for p in TeachingPhase] else TeachingPhase.INTRODUCTION
        result = _classroom.process_instruction(instruction, phase)
        return {"ok": True, "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/classroom/students")
def classroom_students():
    if not CLASSROOM_OK:
        return {"ok": False, "students": []}
    return {"ok": True, "students": _classroom.get_student_states()}


@app.post("/api/classroom/reset")
def classroom_reset():
    if CLASSROOM_OK:
        _classroom.reset()
        return {"ok": True}
    return {"ok": False}


# ==================== 用户反馈（比赛实证材料） ====================

class FeedbackRequest(BaseModel):
    topic: str = ""
    rating: int = 5
    comment: str = ""
    session_id: str = ""

@app.post("/api/feedback")
def submit_feedback(req: FeedbackRequest):
    """同学体验后提交反馈（打分+留言）；对取值范围与长度做校验，防脏数据污染答辩统计"""
    try:
        rating = int(req.rating)
    except Exception:
        raise HTTPException(status_code=400, detail="评分必须是 1-5 的整数")
    if not (1 <= rating <= 5):
        raise HTTPException(status_code=400, detail="评分必须在 1-5 之间")
    comment = (req.comment or "").strip()
    if len(comment) > 500:
        comment = comment[:500]
    topic = (req.topic or "").strip()[:200]
    try:
        from feedback import save_feedback
        result = save_feedback(topic, rating, comment, (req.session_id or "")[:64])
        return {"ok": True, "stats": result.get("stats", {})}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/feedback/stats")
def feedback_stats():
    """反馈统计（答辩用）"""
    try:
        from feedback import get_feedback
        d = get_feedback()
        return {"ok": True, "stats": d.get("stats", {}), "total": d.get("stats", {}).get("total", 0),
                "list": d.get("feedback", [])[-20:]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==================== 健康检查 ====================

@app.get("/api/budget")
def api_budget():
    # 公开体验额度：前端与访客都能查今天还剩多少
    try:
        from guard import budget_status
        return budget_status()
    except Exception as e:
        return {"error": str(e)[:200]}

@app.get("/health")
def health():
    """健康检查：反映各模块真实加载状态与配置情况（便于排查"为什么跑不起来"）"""
    engine_ok = engine is not None
    try:
        from cnki_search import COOKIE_PATH
        cnki_cookie = os.path.exists(COOKIE_PATH)
    except Exception:
        cnki_cookie = False
    cfg = {}
    try:
        cfg["deepseek_key"] = _valid_key_label(engine.DEEPSEEK_API_KEY_1) if engine_ok else False
        cfg["glm_key"] = bool(getattr(engine, "GLM_API_KEY", ""))
        cfg["qwen_key"] = bool(getattr(engine, "QWEN_API_KEY", ""))
        cfg["bocha_key"] = bool(os.environ.get("BOCHA_API_KEY", ""))
    except Exception:
        pass
    return {
        "status": "ok",
        "service": "群智 V15",
        "modules": {
            "engine(讨论引擎)": engine_ok,
            "coach(主教练联网)": True,
            "review(v3五角色评审)": True,
            "classroom(课堂模拟器)": bool(CLASSROOM_OK),
            "cnki_cookie(知网凭据)": cnki_cookie,
        },
        "keys_configured": cfg,
        "warning": getattr(engine, "_KEY_WARNING", "") if engine_ok else "引擎未加载",
    }


def _valid_key_label(k: str) -> bool:
    """判断 key 是否为有效形态（非占位符）——与 sandbox/app.py 的 _valid_key 同口径"""
    try:
        k.encode("ascii")
    except Exception:
        return False
    low = (k or "").lower()
    return len(k or "") >= 12 and not any(b in low for b in ("your", "replace", "placeholder", "xxx"))



# ==================== 构造判据集盲评（2026-10-10）====================
# 用途：给「干预信号尺子替换」实验的构造判据集做**独立双人盲评**。
# 页面本身**不含构造脚本、也不含任何尺子读数**——只有「组别 + 标签 + 定义 + 文本」，
# 顺序由固定种子打乱，两位核验人看到同一顺序，便于配对算 Cohen's κ。
_BLIND_DIR = os.path.join(V15_ROOT, "data", "blind")
os.makedirs(_BLIND_DIR, exist_ok=True)


@app.get("/blind")
async def blind_page():
    """盲评页（挂在 /static 下，裸路径也给一个入口）"""
    p = os.path.join(_static_dir, "blind_review.html")
    if not os.path.exists(p):
        return HTMLResponse(u"<h1>盲评页未部署</h1><p>缺少 app/static/blind_review.html</p>",
                            status_code=404)
    return FileResponse(p)


@app.post("/api/blind/submit")
async def blind_submit(request: Request):
    """接收一份标注结果并落盘（标注人不用回传文件）"""
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "invalid json"}
    raw = str(body.get("verifier") or "anon")
    ver = re.sub(r"[^\w\u4e00-\u9fa5-]", "", raw)[:40] or "anon"
    body["_saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    body["_ip"] = (request.client.host if request.client else "")
    fname = "%s_%s.json" % (ver, time.strftime("%Y%m%d_%H%M%S"))
    path = os.path.join(_BLIND_DIR, fname)
    if os.path.exists(path):                       # 同一秒重复提交不覆盖
        fname = "%s_%s_%s.json" % (ver, time.strftime("%Y%m%d_%H%M%S"), uuid.uuid4().hex[:4])
        path = os.path.join(_BLIND_DIR, fname)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False, indent=1)
    return {"ok": True, "saved": fname, "n": len(body.get("answers") or {}),
            "expected": body.get("items")}


@app.get("/api/blind/list")
async def blind_list():
    """看当前收到了几份、每份标了多少条"""
    out = []
    for fn in sorted(os.listdir(_BLIND_DIR)):
        if not fn.endswith(".json"):
            continue
        try:
            d = json.load(open(os.path.join(_BLIND_DIR, fn), encoding="utf-8"))
        except Exception:
            continue
        out.append({"file": fn, "verifier": d.get("verifier"),
                    "n": len(d.get("answers") or {}), "items": d.get("items"),
                    "at": d.get("_saved_at")})
    return {"count": len(out), "dir": _BLIND_DIR, "submissions": out}


import html as _html  # 2026-11-19 XSS 修复

# ---- V18 PROXY (auto-inserted) ----
@app.api_route("/v18", methods=["GET", "POST", "OPTIONS"])
@app.api_route("/v18/{path:path}", methods=["GET", "POST", "OPTIONS"])
async def v18_proxy(request: Request, path: str = ""):
    """v18（VeriCite 工程版）反代：公网只开 5020，也能访问 /v18"""
    import httpx
    from fastapi.responses import Response
    url = "http://127.0.0.1:18097/" + (path or "")
    if request.url.query:
        url += "?" + request.url.query
    body = await request.body()
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "content-length", "connection")}
    try:
        async with httpx.AsyncClient(timeout=900) as client:
            r = await client.request(request.method, url, content=body, headers=headers)
        out_headers = {k: v for k, v in r.headers.items()
                       if k.lower() not in ("content-length", "content-encoding", "transfer-encoding", "connection")}
        return Response(content=r.content, status_code=r.status_code, headers=out_headers)
    except Exception as e:
        return Response(status_code=502, content=("v18 不可达：%s" % type(e).__name__).encode("utf-8"))
# ---- /V18 PROXY END ----


# ---- DEMO & SHARE ROUTES (auto-inserted) ----
_DEMO_PRESETS = [
    {"id": "d1", "t": "大学生用 AI 写作业，学校该禁止还是引导？",
     "d": "教研决策 · 6 人团队预演", "scenario": "教研"},
    {"id": "d2", "t": "师范生课堂参与度低，该改教学法还是改评价？",
     "d": "教学改革 · 跨专业视角", "scenario": "教研"},
    {"id": "d3", "t": "课题组要不要买 GPU：自建还是租云？",
     "d": "科研投入 · 成本与产出权衡", "scenario": "科研"},
]


@app.get("/api/demo/presets")
def demo_presets():
    """免口令：只返回预设题（不含任何敏感信息）"""
    return {"ok": True, "presets": _DEMO_PRESETS}


@app.get("/demo", response_class=HTMLResponse)
def demo_page():
    p = os.path.join(V15_ROOT, "app", "templates", "demo.html")
    if os.path.exists(p):
        return FileResponse(p)
    return HTMLResponse("<h1>demo</h1><p>demo.html 未部署</p>", status_code=500)


def _public_result(session_id: str) -> dict:
    """取会话结果并脱敏（分享用）。兼容内存对象(SandboxSession)与磁盘 dict 两种形态"""
    keep = ("topic", "report", "human_report", "v3_review", "v6_review", "cards",
            "golden_insight", "emergence", "evaluation", "references", "citation_check",
            "interventions", "scenario_report", "complexity_level", "output_mode",
            "honesty_note", "next_session_suggestions")
    rec = active_sessions.get(session_id)
    if rec is None:
        try:
            from local_store import load_session
            rec = load_session(session_id)
        except Exception:
            rec = None
    if rec is None:
        return {}

    def pick(k):
        if isinstance(rec, dict):
            v = rec.get(k)
        else:
            v = getattr(rec, k, None)
            if v is None:
                # 有的实现在 .result / .data 里
                for attr in ("result", "data", "state"):
                    sub = getattr(rec, attr, None)
                    if isinstance(sub, dict) and k in sub:
                        v = sub.get(k)
                        break
        return v

    out = {}
    for k in keep:
        v = pick(k)
        if v is not None:
            out[k] = v
    # 空则尝试整体转 dict
    if not out:
        try:
            d = rec if isinstance(rec, dict) else getattr(rec, "__dict__", {}) or {}
            out = {k: d.get(k) for k in keep if d.get(k) is not None}
        except Exception:
            pass
    return out


# ---- SHARE STORAGE ----
_SHARE_DIR = os.path.join(V15_ROOT, "app", "data", "shares")
os.makedirs(_SHARE_DIR, exist_ok=True)


@app.post("/api/share")
def api_share(payload: dict):
    """把会话结果脱敏落盘，返回只读分享 id"""
    sid = (payload or {}).get("session_id") or ""
    if not sid:
        raise HTTPException(status_code=400, detail="缺少 session_id")
    d = _public_result(sid)
    if not d:
        raise HTTPException(status_code=404, detail="会话不存在（内存与磁盘均无）")
    safe_id = re.sub(r"[^A-Za-z0-9_.\-\u4e00-\u9fff]", "_", sid)[:120]
    path = os.path.join(_SHARE_DIR, safe_id + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"id": safe_id, "saved_at": datetime.now().isoformat(timespec="seconds"), **d},
                  f, ensure_ascii=False, indent=1)
    return {"ok": True, "id": safe_id, "url": "/r/" + safe_id}


def _load_share(sid: str):
    safe_id = re.sub(r"[^A-Za-z0-9_.\-\u4e00-\u9fff]", "_", sid)[:120]
    path = os.path.join(_SHARE_DIR, safe_id + ".json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f), safe_id
        except Exception:
            return None, safe_id
    return None, safe_id


@app.get("/r/{session_id}", response_class=HTMLResponse)
def share_page(session_id: str):
    """只读分享页：任何人可看（不含口令、不含 prompt/密钥）"""
    d, _ = _load_share(session_id)
    if not d:
        d = _public_result(session_id)
    if not d:
        return HTMLResponse("<h1>报告不存在或已过期</h1>", status_code=404)
    title = str(d.get("topic") or "决策预演报告")
    report = d.get("report") or d.get("human_report") or ""
    if not isinstance(report, str):
        try:
            report = json.dumps(report, ensure_ascii=False, indent=1)
        except Exception:
            report = str(report)
    cards = d.get("cards") or []
    if isinstance(cards, dict):
        cards = [v for _, v in cards.items()]
    elif isinstance(cards, str):
        cards = [cards]
    else:
        try:
            cards = list(cards)
        except Exception:
            cards = []
    html = ["<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>",
            "<meta name='viewport' content='width=device-width, initial-scale=1'>",
            f"<title>{_html.escape(str(title or ''))} · 群智</title>",
            "<style>:root{--p:#2563EB;--bg:#FAF6F0;--card:#fff;--bd:#EAE3D8;--tx:#1F2937;--dim:#6B7280}",
            "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font-family:'Microsoft YaHei',system-ui,sans-serif}",
            ".top{padding:14px 22px;background:var(--card);border-bottom:1px solid var(--bd);display:flex;justify-content:space-between;align-items:center}",
            ".top b{color:var(--p)}.wrap{max-width:900px;margin:0 auto;padding:22px}",
            ".card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:18px;margin-bottom:16px}",
            ".card h3{margin:0 0 10px;font-size:15px;color:var(--p)}",
            ".rep{white-space:pre-wrap;line-height:1.85;font-size:13.5px;overflow-wrap:anywhere}",
            ".card2{background:#F5F0E8;border:1px solid var(--bd);border-radius:8px;padding:10px;margin-bottom:8px;font-size:13px}",
            "@media(max-width:768px){.wrap{padding:14px}.rep{font-size:14px}}</style></head><body>",
            "<div class='top'><div><b>群智</b> · 科研决策预演</div><div style='color:var(--dim);font-size:12px'>只读分享</div></div>",
            "<div class='wrap'>",
            f"<div class='card'><h3>课题</h3><div style='font-size:16px;font-weight:700'>{_html.escape(str(title or ''))}</div></div>"]
    if cards:
        html.append("<div class='card'><h3>关键结论</h3>")
        for c in cards[:8]:
            txt = c if isinstance(c, str) else (c.get("text") or c.get("title") or str(c))
            html.append(f"<div class='card2'>{_html.escape(str(txt or ''))}</div>")
        html.append("</div>")
    html.append(f"<div class='card'><h3>完整报告</h3><div class='rep'>{_html.escape(str(report or ''))}</div></div>")
    html.append("<div class='card' style='color:#6B7280;font-size:12px'>本页为脱敏只读分享，不含密钥与内部提示词。Generated by 群智 v15.</div>")
    html.append("</div></body></html>")
    return HTMLResponse("".join(html))
# ---- /DEMO & SHARE ROUTES END ----


# ---- TOOLS HUB (auto-inserted) ----
_TOOLS = [
    {"id": "v15", "ic": "🧠", "name": "群智 · 决策预演", "ver": "v15",
     "desc": "6 个不同专业/性格的 AI 成员，信息不对称下多轮讨论 + 主教练联网补料 + 三评审打分，输出决策预演报告。",
     "tags": ["多智能体", "联网检索", "决策报告"], "status": "on", "url": "/", "demo": "/demo"},
    {"id": "v18", "ic": "📄", "name": "群智 · 文档问题发现", "ver": "v18",
     "desc": "上传论文 PDF/LaTeX → 带逐字原文锚点的问题清单 + 改法 + 验收标准 + 合规提醒 + 图表核对。",
     "tags": ["可核验", "原文锚点", "作者自查"], "status": "on", "url": "/v18/", "demo": None},
    {"id": "pkg", "ic": "📦", "name": "群智 · 论文包检查", "ver": "v18",
     "desc": "上传整包材料（论文+代码+数据+文档 zip）→ 文件角色分类、引用/依赖图、核心文件、疑点清单。",
     "tags": ["整包分析", "角色分诊", "疑点定位"], "status": "on", "url": "/pkg", "demo": None},
    {"id": "selfcheck", "ic": "✅", "name": "群智 · 投稿前自检", "ver": "v18",
     "desc": "输入论文 → 对照 4 万篇全文，逐项告诉你「这项做法同领域多少人做了」，并给出该补什么。",
     "tags": ["同行基线", "自检清单", "领域分布"], "status": "on", "url": "/kb/selfcheck", "demo": None},
    {"id": "search", "ic": "🔎", "name": "群智 · 检索装置", "ver": "v15",
     "desc": "多源学术检索（中文核心 / arXiv / NCPSSD 等），把检索结果整理成可引用的材料。",
     "tags": ["多源检索", "材料整理"], "status": "soon", "url": None, "demo": None},
    {"id": "review", "ic": "🧾", "name": "群智 · 盲评与回收", "ver": "v15",
     "desc": "三人盲评问卷、回收统计与一致性（α）计算，用于小样本评测。",
     "tags": ["盲评", "一致性"], "status": "soon", "url": None, "demo": None},
    {"id": "review_v6", "ic": "⚖️", "name": "群智 · 三堂会审 v6", "ver": "v6",
     "desc": ("三家模型成对强制选择 + 锚点自检。注意：只有传 baseline 才产出 pairwise "
              "成对主读数；不传 baseline 时 review_v6.pairwise=null，并由 "
              "pairwise_skipped（code=no_baseline）说明本层按设计跳过。"),
     "tags": ["成对评审", "锚点自检", "无基线跳过"], "status": "on",
     "url": "/gates", "demo": None},
]


@app.get("/api/tools")
def api_tools():
    """能力清单（免口令，供入口页/外部接入发现）"""
    return {"ok": True, "tools": _TOOLS}


# ---- TOOLS DISPATCH (auto-inserted) ----
_SPECS = {
    "v15": {
        "name": "群智 · 决策预演",
        "input": {"topic": "string（≥6 字）", "scenario": "教研|科研|产业|其他（默认教研）",
                  "rounds": "int 1–5（默认 3）", "user_thought": "string（可选）"},
        "output": {"session_id": "string", "poll": "/api/tools/v15/result/{session_id}"},
        "cost": "约 ¥0.3–1.5/次，2–4 分钟",
    },
    "v18": {
        "name": "群智 · 文档问题发现",
        "input": {"text": "string（≥200 字）或 file_base64+filename（pdf/docx/tex）",
                  "venue": "all|iclr|neurips|icml|aaai|acl", "max_items": "8–24"},
        "output": {"markdown": "报告", "passed": "条数", "table_stats": "图表核对统计"},
        "cost": "约 ¥0.1–0.3/次，40–80 秒",
    },
}

_V18 = "http://127.0.0.1:18097"


@app.get("/api/tools/{tid}/schema")
def tool_schema(tid: str):
    if tid not in _SPECS:
        raise HTTPException(status_code=404, detail="未知工具：%s" % tid)
    return {"ok": True, "id": tid, **_SPECS[tid]}


@app.post("/api/tools/{tid}/run")
async def tool_run(tid: str, payload: dict):
    """统一调度：一个接口跑所有能力"""
    import base64
    import httpx

    if tid not in _SPECS:
        raise HTTPException(status_code=404, detail="未知工具：%s" % tid)

    if tid == "v15":
        topic = (payload or {}).get("topic") or ""
        if len(topic) < 6:
            raise HTTPException(status_code=400, detail="topic 至少 6 个字")
        body = {
            "topic": topic,
            "user_thought": (payload or {}).get("user_thought") or "",
            "max_rounds": int((payload or {}).get("rounds") or 3),
            "info_mode": "coach_search",
            "scenario": (payload or {}).get("scenario") or "教研",
            "allow_web": bool((payload or {}).get("allow_web")),
        }
        try:
            async with httpx.AsyncClient(timeout=300) as c:
                r = await c.post("http://127.0.0.1:5020/api/sandbox/start",
                                 params={"key": _guard.ACCESS_KEY}, json=body)
            j = r.json()
        except Exception as e:
            raise HTTPException(status_code=502, detail="v15 调用失败：%s" % type(e).__name__)
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=j.get("detail") or "v15 启动失败")
        sid = j.get("session_id")
        return {"ok": True, "tool": "v15", "session_id": sid,
                "poll": "/api/tools/v15/result/%s" % sid,
                "search_meta": j.get("search_meta")}

    # v18
    venue = (payload or {}).get("venue") or "all"
    max_items = int((payload or {}).get("max_items") or 16)
    try:
        async with httpx.AsyncClient(timeout=900) as c:
            if (payload or {}).get("file_base64"):
                raw = base64.b64decode(payload["file_base64"])
                files = {"file": (payload.get("filename") or "upload.pdf", raw)}
                r = await c.post(_V18 + "/upload",
                                 params={"mode": "fast", "venue": venue, "max_items": max_items},
                                 files=files)
            else:
                text = (payload or {}).get("text") or ""
                if len(text) < 200:
                    raise HTTPException(status_code=400, detail="text 至少 200 字")
                r = await c.post(_V18 + "/review",
                                 json={"title": "", "text": text, "mode": "fast",
                                       "venue": venue, "max_items": max_items,
                                       "field": (payload or {}).get("field") or None})
        j = r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail="v18 调用失败：%s" % type(e).__name__)
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=(j.get("error") or {}).get("message") or "v18 失败")
    return {"ok": True, "tool": "v18", "markdown": j.get("markdown"),
            "passed": j.get("passed"), "flagged": j.get("flagged"),
            "seconds": j.get("seconds"), "table_stats": j.get("table_stats") or {},
            "input_info": j.get("input_info") or {}}


@app.get("/api/tools/v15/result/{session_id}")
async def tool_v15_result(session_id: str):
    """统一的 v15 结果轮询（免口令，供外部接入）"""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.get("http://127.0.0.1:5020/api/sandbox/result/%s" % session_id,
                            params={"key": _guard.ACCESS_KEY})
        j = r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail="v15 结果读取失败：%s" % type(e).__name__)
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=j.get("detail") or "v15 结果读取失败")
    return {"ok": True, "tool": "v15", "status": j.get("status"),
            "report": j.get("report") or j.get("human_report"),
            "share": "/r/%s" % session_id if j.get("status") == "done" else None,
            "raw": j}
# ---- /TOOLS DISPATCH END ----


# ---- KB API (auto-inserted) ----
import sqlite3 as _sqlite3

_KB_PATH = os.environ.get("QZ_KB_PATH", "./paperlib/db/paperlib.sqlite")
_KB_FTS = os.environ.get("QZ_KB_FTS", "./paperlib/db/paperlib_fts.sqlite")

# ==================== API 元数据 / call_id / 版本（2026-11-21 平台修复） ====================
# 问题 1：平台数字无 call_id，不可证伪。
# 这里给出统一契约：每个带数字的接口都回传
#   call_id（确定性哈希） + computed_at + version + denominator + numerator + reproduce
# call_id 由「接口名 + 归一化参数 + API 版本 + 数据快照」哈希得到：
# 同一 call_id + 同一参数 + 同一数据快照 → 同一结果，可复算、可回查。
# 问题 4：每个 /api/* 返回都带 schema_version（由 middleware 注入），/api/version 列版本。
API_SCHEMA_VERSION = "2026-11-21"
API_VERSION = "v2026-11-21"
API_RELEASED_AT = "2026-11-21"
SEARCH_TOTAL_CAP = 600          # 单次结果集展示/翻页上限（不是匹配总数）
_CALL_DIR = os.path.join(V15_ROOT, "app", "data", "calls")
_API_SNAPSHOT_CACHE = {"ts": 0.0, "id": ""}


def _utc_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _data_snapshot() -> str:
    """数据快照 id：库/基线文件的 (mtime,size) 指纹，5 分钟内复用。

    同一 call_id 只在同一数据快照下承诺同一结果；库更新后快照变、call_id 变，
    不会出现「旧数字被当成新数据」的静默错配。
    """
    now = time.time()
    if _API_SNAPSHOT_CACHE["id"] and (now - _API_SNAPSHOT_CACHE["ts"]) < 300:
        return _API_SNAPSHOT_CACHE["id"]
    parts = []
    for p in (_KB_PATH,
              "./paperlib/baseline_rates.json",
              "./paperlib/baseline2.json"):
        try:
            st = os.stat(p)
            parts.append("%s:%d:%d" % (os.path.basename(p), int(st.st_mtime), int(st.st_size)))
        except Exception:
            parts.append(os.path.basename(p) + ":missing")
    sid = "paperlib-" + hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:10]
    _API_SNAPSHOT_CACHE.update({"ts": now, "id": sid})
    return sid


# ==================== 覆盖时效性 / venue 别名（2026-11-21 glm-fix4 问题 2 & 4） ====================
_COVERAGE_CACHE = {"ts": 0.0, "data": {}}
_COVERAGE_TTL = 600          # 10 分钟内复用；库 mtime 变化会立即失效
_COVERAGE_NOTE = "论文库每 24 小时更新一次；新论文可能有 1-2 天延迟"
_VENUE_ALIAS_PATH = "./paperlib/venue_aliases.json"
_VENUE_ALIAS_CACHE = {"mtime": 0.0, "data": {}}
_VENUE_ALIAS_FALLBACK = {
    "_note": "venue 匹配用别名表；不在表内的 venue 按原样精确匹配（可能漏）",
    "NEURIPS": ["NIPS", "NeurIPS", "neural information processing systems"],
    "CVPR": ["IEEE/CVF conf. computer vision",
             "IEEE/CVF Conference on Computer Vision and Pattern Recognition"],
}


def _coverage_info(force: bool = False) -> dict:
    """库覆盖口径：总篇数、数据截止日、最后入库时间。

    cutoff 取 papers.fetched_at 的最大日期（本库没有 publication_date 字段，
    fetched_at 是“这条记录何时被我们抓到”的可靠时间）；last_ingest 取库文件 mtime。
    """
    now = time.time()
    cached = _COVERAGE_CACHE.get("data") or {}
    try:
        kb_mtime = os.stat(_KB_PATH).st_mtime
    except Exception:
        kb_mtime = 0.0
    if (not force and cached and (now - _COVERAGE_CACHE["ts"]) < _COVERAGE_TTL
            and cached.get("_kb_mtime") == kb_mtime):
        return {k: v for k, v in cached.items() if not k.startswith("_")}
    total, cutoff = 0, ""
    try:
        c = _sqlite3.connect("file:%s?mode=ro" % _KB_PATH, uri=True, timeout=8)
        row = c.execute("select count(*), max(substr(fetched_at,1,10)) from papers").fetchone()
        c.close()
        total = int(row[0] or 0)
        cutoff = (row[1] or "")
    except Exception as e:
        print("[coverage] 查询失败：%r" % (e,), flush=True)
    try:
        last_ingest = datetime.utcfromtimestamp(kb_mtime).replace(
            microsecond=0).isoformat() + "Z"
    except Exception:
        last_ingest = ""
    data = {
        "cutoff": cutoff,
        "total_papers": total,
        "newest_paper": cutoff,
        "last_ingest": last_ingest,
        "update_freq": "daily",
        "coverage_note": _COVERAGE_NOTE,
        "_kb_mtime": kb_mtime,
        "_ts": now,
    }
    _COVERAGE_CACHE.update({"ts": now, "data": data})
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _load_venue_aliases() -> dict:
    """读 ./paperlib/venue_aliases.json（可维护，热加载）。"""
    try:
        mt = os.stat(_VENUE_ALIAS_PATH).st_mtime
    except Exception:
        return dict(_VENUE_ALIAS_FALLBACK)
    if _VENUE_ALIAS_CACHE["data"] and _VENUE_ALIAS_CACHE["mtime"] == mt:
        return _VENUE_ALIAS_CACHE["data"]
    try:
        with open(_VENUE_ALIAS_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and any(not str(k).startswith("_") for k in d):
            _VENUE_ALIAS_CACHE.update({"mtime": mt, "data": d})
            return d
    except Exception as e:
        print("[venue-alias] 读取失败，用内置表：%r" % (e,), flush=True)
    return dict(_VENUE_ALIAS_FALLBACK)


def _norm_venue(v: str) -> str:
    """归一化：小写、去掉标点、压缩空格（用于别名查表，不改原值）。"""
    v = (v or "").lower().replace("&", " and ")
    v = re.sub(r"[^a-z0-9]+", " ", v)
    return re.sub(r"\s+", " ", v).strip()


def _venue_alias_table() -> dict:
    d = _load_venue_aliases()
    return {k: v for k, v in d.items() if not str(k).startswith("_")}


def _resolve_venue(venue_input: str) -> dict:
    """输入 venue → (resolved, method)。

    method:
      canonical = 输入就是表内规范名
      alias     = 命中别名，归一到规范名
      exact     = 不在表内，按原样精确匹配（may miss 变体，这是已知边界）
      none      = 调用方没传 venue
    """
    raw = (venue_input or "").strip()
    if not raw:
        return {"input": None, "resolved": None, "method": "none",
                "matched_values": [], "alias_table": _VENUE_ALIAS_PATH}
    table = _venue_alias_table()
    rev = {}
    for canon, aliases in table.items():
        rev[_norm_venue(canon)] = canon
        for a in (aliases or []):
            if isinstance(a, str) and a.strip():
                rev[_norm_venue(a)] = canon
    n = _norm_venue(raw)
    canon = rev.get(n)
    if canon:
        vals = [canon] + [a for a in (table.get(canon) or []) if isinstance(a, str)]
        # 去重（大小写不敏感），保留原顺序
        seen, uniq = set(), []
        for x in vals:
            k = x.lower()
            if k not in seen:
                seen.add(k)
                uniq.append(x)
        return {"input": raw, "resolved": canon,
                "method": "canonical" if n == _norm_venue(canon) else "alias",
                "matched_values": uniq, "alias_table": _VENUE_ALIAS_PATH}
    return {"input": raw, "resolved": raw, "method": "exact",
            "matched_values": [raw], "alias_table": _VENUE_ALIAS_PATH}


def _venue_alias_payload() -> dict:
    d = dict(_load_venue_aliases())
    d.setdefault("_note", "venue 匹配用别名表；不在表内的 venue 按原样精确匹配（可能漏）")
    d["_path"] = _VENUE_ALIAS_PATH
    d["_canonical_count"] = len(_venue_alias_table())
    return d


def _call_id(endpoint: str, params: dict, snapshot: str = "", nonce: str = "") -> str:
    snap = snapshot or _data_snapshot()
    canon = json.dumps({"endpoint": endpoint, "params": params,
                        "version": API_VERSION, "snapshot": snap, "nonce": nonce},
                       ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    h = hashlib.sha1(canon.encode("utf-8")).hexdigest()[:16]
    return "qz_%s_%s" % (API_VERSION[1:].replace("-", ""), h)


def _call_meta(endpoint: str, params: dict, denominator: dict = None,
               numerator: dict = None, metric: str = None, scope: str = None,
               snapshot: str = "", nonce: str = "") -> dict:
    snap = snapshot or _data_snapshot()
    cid = _call_id(endpoint, params, snap, nonce)
    # 确定性接口（selfcheck / baseline）同一 call_id 只认第一次计算的 computed_at：
    # 重复调用返回逐字段相同的响应，而不是“同 id 不同时间戳”。
    prev = None if nonce else _load_call(cid)
    prev_computed = (prev or {}).get("computed_at")
    # 2026-11-21：call_id 是计算结果 ID，同一 ID 可能对应多次请求。
    # 这里登记一次访问：首次 fetch_count=1，重复请求递增，is_cached=true。
    stat = _call_stats_touch(cid, computed_at=prev_computed, prior_exists=bool(prev))
    out = {
        "call_id": cid,
        "computed_at": prev_computed or stat.get("first_computed_at") or _utc_now(),
        "version": API_VERSION,
        "api_version": API_VERSION,
        "schema_version": API_SCHEMA_VERSION,
        "data_snapshot": snap,
        "call_id_kind": "content_addressed_result_id",
        "call_id_semantics": _CALL_SEMANTICS_NOTE,
        "reproduce": {
            "endpoint": endpoint,
            "params": params,
            "rule": "同一 call_id + 同一 params + 同一 data_snapshot → 同一结果",
            "replay": "/api/call/{call_id}",
            "call_id_is_content_addressed": True,
        },
    }
    out.update(_call_stats_fields(stat))
    if metric:
        out["metric"] = metric
    if scope:
        out["scope"] = scope
    if denominator is not None:
        out["denominator"] = denominator
    if numerator is not None:
        out["numerator"] = numerator
    return out


def _save_call(call_id: str, payload: dict) -> None:
    """把一次计算落盘，供 /api/call/{call_id} 原样回查（失败不影响主流程）。"""
    if not call_id or not isinstance(payload, dict):
        return
    try:
        os.makedirs(_CALL_DIR, exist_ok=True)
        fp = os.path.join(_CALL_DIR, re.sub(r"[^A-Za-z0-9_.-]", "_", call_id) + ".json")
        tmp = fp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, fp)
    except Exception as e:
        print("[call] 保存失败 %s：%r" % (call_id, e), flush=True)


def _load_call(call_id: str):
    if not call_id:
        return None
    try:
        fp = os.path.join(_CALL_DIR, re.sub(r"[^A-Za-z0-9_.-]", "_", call_id) + ".json")
        if not os.path.exists(fp):
            return None
        with open(fp, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


# ==================== call_id 访问统计（2026-11-21 平台修复 2） ====================
# 问题：同参数重复调用得到同一 call_id 与同一 computed_at，但用户看不出
#       这个 call_id 已经对应过几次调用（缓存命中）。
# 修法：每个 call_id 一份旁路统计 data/calls/_stats/<call_id>.json，
#       只增不改 payload；四个字段随所有带 call_id 的响应返回。
_CALL_STATS_DIR = os.path.join(_CALL_DIR, "_stats")
_CALL_STATS_LOCK = threading.Lock()
_CALL_SEMANTICS_NOTE = (
    "call_id 是“计算结果 ID”（内容寻址：接口 + 归一化参数 + API 版本 + 数据快照的哈希），"
    "不是调用凭据 / 鉴权 token；同参数重复调用会得到同一个 call_id，"
    "fetch_count 递增，is_cached=true 表示这是一次重复请求。")
_CALL_FETCH_NOTE = ("fetch_count 统计“带该 call_id 的业务响应被返回了几次”；"
                    "/api/call/{call_id} 只是回看落盘结果，不增加 fetch_count。")


def _call_safe(call_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(call_id or ""))


def _call_stats_path(call_id: str) -> str:
    return os.path.join(_CALL_STATS_DIR, _call_safe(call_id) + ".json")


def _call_stats_read(call_id: str):
    try:
        with open(_call_stats_path(call_id), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _call_stats_fields(stat: dict) -> dict:
    """从统计记录取对外四个字段（统一出口，避免各接口字段缺漏）。"""
    stat = stat or {}
    return {
        "is_cached": bool(stat.get("is_cached")),
        "fetch_count": int(stat.get("fetch_count") or 0),
        "first_computed_at": stat.get("first_computed_at"),
        "last_accessed_at": stat.get("last_accessed_at"),
    }


def _call_stats_touch(call_id: str, computed_at: str = None,
                      prior_exists: bool = False) -> dict:
    """登记一次 call_id 访问，返回最新统计。

    prior_exists：payload 已落盘但没有统计文件（升级前的旧 call_id）。
    旧记录至少被算过 1 次，所以从 2 起算并标记 is_cached=true。
    """
    if not call_id:
        return {}
    now = _utc_now()
    try:
        os.makedirs(_CALL_STATS_DIR, exist_ok=True)
    except Exception as e:
        print("[call] 统计目录创建失败 %s：%r" % (call_id, e), flush=True)
        return {"is_cached": bool(prior_exists), "fetch_count": 2 if prior_exists else 1,
                "first_computed_at": computed_at or now, "last_accessed_at": now}
    with _CALL_STATS_LOCK:
        lock_fp = os.path.join(_CALL_STATS_DIR, ".lock")
        lf = None
        try:
            import fcntl
            lf = open(lock_fp, "a+")
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        except Exception:
            lf = None
        try:
            d = _call_stats_read(call_id)
            if d and int(d.get("fetch_count") or 0) >= 1:
                d["fetch_count"] = int(d.get("fetch_count") or 0) + 1
                d["is_cached"] = True
                d["last_accessed_at"] = now
                d.setdefault("first_computed_at", computed_at or now)
            else:
                d = {
                    "call_id": call_id,
                    "fetch_count": 2 if prior_exists else 1,
                    "is_cached": bool(prior_exists),
                    "first_computed_at": computed_at or now,
                    "last_accessed_at": now,
                }
            fp = _call_stats_path(call_id)
            tmp = "%s.%d.%d.tmp" % (fp, os.getpid(), threading.get_ident())
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
            os.replace(tmp, fp)
            return d
        except Exception as e:
            print("[call] 统计写入失败 %s：%r" % (call_id, e), flush=True)
            return {"is_cached": bool(prior_exists), "fetch_count": 2 if prior_exists else 1,
                    "first_computed_at": computed_at or now, "last_accessed_at": now}
        finally:
            if lf is not None:
                try:
                    import fcntl
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                try:
                    lf.close()
                except Exception:
                    pass


def _call_stats_peek(call_id: str):
    """/api/call 回看用：只读统计，不增加 fetch_count。"""
    return _call_stats_read(call_id)


def _call_touch_cached(payload: dict) -> dict:
    """缓存命中路径：该 payload 的 call_id 又被返回了一次。"""
    if not isinstance(payload, dict):
        return payload
    cid = payload.get("call_id")
    stat = _call_stats_touch(cid, computed_at=payload.get("computed_at"),
                             prior_exists=True)
    payload.update(_call_stats_fields(stat))
    payload["call_id_kind"] = "content_addressed_result_id"
    payload["call_id_semantics"] = _CALL_SEMANTICS_NOTE
    return payload


def _new_search_id() -> str:
    """同一次检索请求的唯一 ID（每次请求都新生成，不复用）。"""
    return "srch_%s_%s" % (datetime.utcnow().strftime("%Y%m%d"), uuid.uuid4().hex[:16])



def _search_lang(q: str):
    """返回 (lang, supported)。库内论文以英文为主，中文查询暂不保证命中。"""
    q = q or ""
    cjk = bool(re.search(r"[\u4e00-\u9fff]", q))
    latin = bool(re.search(r"[A-Za-z]", q))
    if cjk and latin:
        return "mixed", False
    if cjk:
        return "zh", False
    return "en", True


def _paper_quality(it: dict):
    """残条目/异常条目标记（问题 5）：1749 字符这类只到摘要的条目不混列成正常结果。"""
    try:
        n = int(it.get("n_chars") or 0)
    except Exception:
        n = 0
    if n == 0:
        return "missing_text", "无正文字符（n_chars=0），只有元数据"
    if n < 3000:
        return "truncated", "正文仅 %d 字符（<3000），疑似摘要/残条目，相关性不可靠" % n
    # quality_flags 里的 no_gold 只表示“没有人工评审 gold”，不是正文质量问题，
    # 不能拿它当 truncated。只有 meta_partial=true 才是元数据缺字段。
    flags = it.get("quality_flags")
    try:
        fd = json.loads(flags) if isinstance(flags, str) and flags.strip() else (flags or {})
        if isinstance(fd, dict) and (fd.get("meta_partial") is True
                                     or fd.get("no_text") is True):
            return "partial_metadata", "quality_flags=%s" % json.dumps(fd, ensure_ascii=False)[:200]
    except Exception:
        pass
    return "ok", ""


def _search_data_sig() -> str:
    """检索额外依赖的文件指纹：FTS 索引 + venue 别名表。

    _data_snapshot() 只覆盖 paperlib.sqlite / baseline 文件；FTS 重建或别名表
    修改时主库 mtime 可能不变，若不纳入 call_id 会命中过期缓存。
    """
    parts = []
    for p in (_KB_FTS, _VENUE_ALIAS_PATH):
        try:
            st = os.stat(p)
            parts.append("%s:%d:%d" % (os.path.basename(p), int(st.st_mtime), int(st.st_size)))
        except Exception:
            parts.append(os.path.basename(p) + ":missing")
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


def _search_call_params(q_search: str, limit: int, payload: dict,
                        year=None, auto_translate: bool = True) -> dict:
    """检索的归一化参数（call_id 的输入）：同参数 → 同 call_id。

    q 用“实际检索词”（auto_translate 后），这样翻译结果不同不会被误认成同一结果。
    """
    def _s(v):
        if v is None:
            return None
        v = str(v).strip()
        return v or None
    return {
        "q": (q_search or "").strip(),
        "auto_translate": bool(auto_translate),
        "limit": int(limit),
        "field": _s(payload.get("field")),
        "venue": _s(payload.get("venue")),
        "year": int(year) if year is not None else None,
        "decision": _s(payload.get("decision")),
        "index_sig": _search_data_sig(),
    }


def _search_attach_call(result: dict, params: dict) -> dict:
    """给检索结果挂 call_id + computed_at + search_id + 访问统计，并落盘。"""
    try:
        meta = _call_meta(
            "/api/kb/search", params,
            {"n": int(result.get("total") or 0), "description": "exact matches"},
            {"n": int(result.get("returned") or 0), "description": "items returned"},
            metric="search_returned",
            scope=("q:" + (params or {}).get("q", ""))[:120])
    except Exception as e:
        print("[kb] call_id 生成失败：%r" % (e,), flush=True)
        meta = {}
    result["search_id"] = _new_search_id()
    result.update(meta)
    try:
        _save_call(result.get("call_id"), result)
    except Exception as e:
        print("[kb] call 落盘失败：%r" % (e,), flush=True)
    return result


def _search_cached(call_id: str):
    """同参数同快照命中已落盘结果：原样返回并递增 fetch_count。"""
    d = _load_call(call_id)
    if not isinstance(d, dict):
        return None
    out = dict(d)
    _call_touch_cached(out)
    out["search_id"] = _new_search_id()
    out["cache_note"] = ("同参数 + 同数据快照命中已落盘结果：items/total 与首次计算一致；"
                         "search_id 每次请求都不同，call_id 不变，fetch_count 递增")
    return out


def _search_enrich(result: dict, q_original: str = None, q_search: str = None,
                   venue_input: str = None) -> dict:
    """检索返回的度量口径补充（问题 5）：不删旧字段，只加解释字段。"""
    raw_total = int(result.get("total") or 0)
    result["total"] = raw_total                 # 保持旧字段=精确匹配数，不改语义
    result["total_exact"] = raw_total
    result["total_display"] = min(raw_total, SEARCH_TOTAL_CAP)
    result["cap"] = SEARCH_TOTAL_CAP
    result["total_is_capped"] = raw_total > SEARCH_TOTAL_CAP
    result["cap_scope"] = "pagination"
    result["total_note"] = ("total 为精确匹配数；单次最多展示/翻页 cap 条，"
                            "total_is_capped=true 表示命中数超过该展示上限（旧版会把 total 截到 600）")
    g = result.get("gate") or {}
    mt = g.get("matched_terms") or []
    result["matched_terms"] = mt
    result["matched_terms_is_semantic"] = False
    result["matched_terms_note"] = ("matched_terms 只是查询词在闸门词表里的词面/关键词命中，"
                                    "不代表检索结果与查询语义相关；语义检索请用 /api/kb/search_h")
    lang, supported = _search_lang(q_original if q_original is not None else q_search)
    translated = result.get("translated")
    if isinstance(translated, dict) and translated.get("query"):
        supported = True        # 中文查询已由 LLM 改写，语言不再是障碍
    result["lang"] = lang
    result["lang_supported"] = supported
    if isinstance(translated, dict) and translated.get("query"):
        result["lang_note"] = ("中文查询已自动改写为英文学术关键词后检索；"
                               "translated 字段可追溯改写结果与调用 ID")
    elif (result.get("gate") or {}).get("reason") == "non_cs_topic":
        result["lang_note"] = ("查询包含非 CS/ML 领域词，未做跨语言改写；"
                               "本库只收计算机/机器学习论文，请换 CS 关键词")
    elif not supported:
        result["lang_note"] = ("库内论文以英文为主，中文查询不会直接命中；"
                               "默认 auto_translate=true 可自动改写（本次被显式关闭）")
    for it in result.get("items") or []:
        qv, why = _paper_quality(it)
        it["quality"] = qv
        it["quality_reason"] = why
    # 问题 2（glm-fix4）：每次检索都回显覆盖范围，调用方不用再猜库停在哪天。
    try:
        cov = _coverage_info()
        result["coverage"] = {
            "cutoff": cov.get("cutoff"),
            "total_papers": cov.get("total_papers"),
            "newest_paper": cov.get("newest_paper"),
            "last_ingest": cov.get("last_ingest"),
            "update_freq": cov.get("update_freq"),
        }
    except Exception as _e:
        result["coverage"] = {"error": repr(_e)}
    # 问题 4（glm-fix4）：venue 过滤到底用了规范名还是别名，必须回显。
    try:
        result["venue_resolution"] = _resolve_venue(venue_input)
    except Exception as _e:
        result["venue_resolution"] = {"input": venue_input, "error": repr(_e)}
    return result




def _kb_conn():
    if not os.path.exists(_KB_PATH):
        raise HTTPException(status_code=503, detail="论文库未就绪：%s" % _KB_PATH)
    conn = _sqlite3.connect("file:%s?mode=ro" % _KB_PATH, uri=True, timeout=8)
    conn.row_factory = _sqlite3.Row
    return conn


# 全库统计的附加口径缓存：paper_features 的 count(distinct pid) 约 2-3 秒，
# 5 分钟内复用，避免 /kb、/portal、/judge、/read 每次打开都扫一遍。
_KB_STATS_EXTRA = {"ts": 0.0, "data": {}}


def _kb_stats_extra(conn) -> dict:
    now = time.time()
    if _KB_STATS_EXTRA["data"] and (now - _KB_STATS_EXTRA["ts"]) < 300:
        return dict(_KB_STATS_EXTRA["data"])

    def one(sql):
        try:
            return conn.execute(sql).fetchone()[0]
        except Exception:
            return None

    data = {
        # 有全文的论文数（fulltext_z 里按 pid 去重）
        "fulltext": one("select count(distinct pid) from fulltext_z"),
        # 有特征、可做同行基线的论文数（不等于全文数；2026-11-22 修 BUG-003）
        "features_papers": one("select count(distinct pid) from paper_features"),
        "top_source_papers": one("select count(*) from papers where source='venue-crawl'"),
        "refs": one("select count(*) from paper_refs"),
        "update_freq": "daily",
    }
    _KB_STATS_EXTRA.update({"ts": now, "data": data})
    return dict(data)


# ---- PRIVATE LAYER: 用户论文上传（仅本机 + 入库前脱敏） ----
# 核心原则：用户上传的论文绝不出本机。这里只落"脱敏后的正文 + 元数据"，
# 原始文件不持久化、不返回、不进入任何公开 API。
_UPLOAD_ROOT = os.environ.get("QZ_UPLOAD_DIR", os.path.join(V15_ROOT, "data", "uploads_private"))
_UPLOAD_DB = os.path.join(_UPLOAD_ROOT, "uploads.sqlite")
_UPLOAD_MAX_MB = int(os.environ.get("QZ_UPLOAD_MAX_MB", "30"))


def _upload_conn():
    os.makedirs(_UPLOAD_ROOT, exist_ok=True)
    import sqlite3 as _sq
    c = _sq.connect(_UPLOAD_DB, timeout=10)
    c.execute("""create table if not exists uploads(
        pid text primary key, sha256 text, filename text, ext text,
        chars integer, scrub_total integer, scrub_json text,
        created text, text_z blob)""")
    c.commit()
    return c


def _extract_upload_text(raw: bytes, ext: str) -> str:
    if ext == ".pdf":
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(stream=raw, filetype="pdf")
            parts = [pg.get_text("text") or "" for pg in doc]
            doc.close()
            return "\n".join(parts)
        except Exception as e:
            raise HTTPException(status_code=400, detail="PDF 解析失败：%s" % type(e).__name__)
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


@app.post("/api/kb/upload")
async def kb_upload(request: Request):
    """上传论文（PDF/LaTeX/txt/md）→ 入库前脱敏（private 层，仅本机）。

    - 仅接受回环/内网来源；公网部署需显式 QZ_ALLOW_REMOTE_UPLOAD=1。
    - 返回只含 pid / 字符数 / 脱敏计数 / sha256，**不返回原文**。
    - 原始文件不落盘；落盘的是脱敏后的正文（gzip）与元数据。
    """
    import gzip as _gz
    import hashlib as _hl

    import privacy_scrubber as _ps

    # 1) 仅本机
    xff = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    remote = xff or (request.client.host if request.client else "")
    if os.environ.get("QZ_ALLOW_REMOTE_UPLOAD", "0") != "1" and not _ps.looks_local_ip(remote):
        raise HTTPException(status_code=403,
                            detail="用户论文上传属于私有层，仅限本机/内网（QZ_ALLOW_REMOTE_UPLOAD=1 可放开）")

    # 2) 读取（multipart file 或 form text）
    form = await request.form()
    file = form.get("file")
    if file is not None:
        raw = await file.read()
        filename = file.filename or "upload.txt"
    else:
        text = form.get("text") or ""
        raw = text.encode("utf-8")
        filename = (form.get("filename") or "upload.txt")
    if not raw:
        raise HTTPException(status_code=400, detail="缺少 file（PDF/tex/txt）或 text 字段")
    if len(raw) > _UPLOAD_MAX_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail="文件超过 %d MB" % _UPLOAD_MAX_MB)

    ext = os.path.splitext(filename)[1].lower() or ".txt"
    if ext not in (".pdf", ".tex", ".txt", ".md", ".latex"):
        raise HTTPException(status_code=400, detail="仅支持 PDF / LaTeX / txt / md")

    text = _extract_upload_text(raw, ext)
    if len(text.strip()) < 200:
        raise HTTPException(status_code=400,
                            detail="正文过短（%d 字符），无法入库；扫描版 PDF 请先 OCR" % len(text.strip()))

    # 3) 入库前脱敏
    res = _ps.scrub_with_stats(text, mode="upload")
    scrubbed = res["text"]
    sha = _hl.sha256(raw).hexdigest()
    pid = "up_" + sha[:16]
    stats = {"redactions": res["redactions"], "total": res["total"],
             "original_chars": res["original_chars"], "output_chars": res["output_chars"]}

    conn = _upload_conn()
    try:
        row = conn.execute("select chars, scrub_total from uploads where pid=?", (pid,)).fetchone()
        if row:
            return {"ok": True, "pid": pid, "dedup": True, "chars": row[0],
                    "redactions_total": row[1], "layer": "private",
                    "stored": "private-local", "raw_text_returned": False}
        conn.execute(
            "insert into uploads(pid,sha256,filename,ext,chars,scrub_total,scrub_json,created,text_z)"
            " values(?,?,?,?,?,?,?,?,?)",
            (pid, sha, os.path.basename(filename)[:200], ext, len(scrubbed),
             int(res["total"]), json.dumps(stats, ensure_ascii=False),
             datetime.now().isoformat(timespec="seconds"),
             _gz.compress(scrubbed.encode("utf-8"))))
        conn.commit()
    finally:
        conn.close()

    return {"ok": True, "pid": pid, "chars": len(scrubbed), "dedup": False,
            "sha256": sha, "scrub": stats, "layer": "private",
            "stored": "private-local", "raw_text_returned": False,
            "policy": "原始文件不落盘；脱敏后仅存本机私有库，绝不出本机"}


@app.get("/api/kb/stats")
def kb_stats():
    """论文库总口径（所有前端页面「库规模」的唯一来源）。

    2026-11-22 前端一致性修复：
      papers           论文入库总量
      fulltext         有全文的论文数（pid 去重）
      features_papers  有特征、可对照同行基线的论文数（旧文案误标为“全文可锚定”）
      reviews/issues/refs
    响应带 call_id，可 /api/call/{call_id} 回放。
    """
    with _kb_conn() as c:
        def one(sql, *a):
            try:
                return c.execute(sql, a).fetchone()[0]
            except Exception:
                return None
        out = {
            "ok": True,
            "papers": one("select count(*) from papers"),
            "reviews": one("select count(*) from reviews"),
            "issues": one("select count(*) from issues"),
            "years": [r[0] for r in c.execute("select distinct year from papers where year is not null order by year desc limit 10")],
            "venues": [r[0] for r in c.execute("select venue, count(*) c from papers group by venue order by c desc limit 12")],
            "fields": [r[0] for r in c.execute("select field, count(*) c from papers group by field order by c desc limit 12")],
            "db_mb": round(os.path.getsize(_KB_PATH) / 1048576, 1),
        }
        out.update(_kb_stats_extra(c))
    out.update(_call_meta("/api/kb/stats", {},
                          {"description": "count(*) from papers table"},
                          {"papers": out.get("papers")},
                          metric="library_size", scope="all"))
    _save_call(out.get("call_id"), out)
    return out

def _qz_translate(q: str) -> tuple[str, bool]:
    """中文问题 → 英文检索词；失败返回原文与 False"""
    import urllib.request as _u
    try:
        key = ""
        for ln in open("./experiments/.env", encoding="utf-8", errors="ignore"):
            if ln.startswith("DEEPSEEK_API_KEY="):
                key = ln.split("=", 1)[1].strip()
                break
        if not key:
            return q, False
        prompt = ("Translate into a short English search query (keywords, no sentence): " + q[:200])
        body = json.dumps({"model": "deepseek-chat",
                           "messages": [{"role": "user", "content": prompt}],
                           "temperature": 0.1, "max_tokens": 60}).encode()
        req = _u.Request("https://api.deepseek.com/chat/completions", data=body,
                         headers={"Content-Type": "application/json",
                                  "Authorization": "Bearer " + key})
        with _u.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode())
        out = (d["choices"][0]["message"]["content"] or "").strip().strip('"')
        if out and not any("\u4e00" <= ch <= "\u9fff" for ch in out):
            return out, True
        return q, False
    except Exception:
        return q, False


@app.post("/api/kb/search_h")
def kb_search_hybrid(payload: dict):
    """混合检索：中文问题先翻英文 → FTS ∪ 向量 → 合并去重（带来源标记）"""
    import subprocess
    p = payload or {}
    q = (p.get("q") or "").strip()
    limit = max(1, min(int(p.get("limit") or 8), 30))
    if not q:
        raise HTTPException(status_code=400, detail="缺 q")
    has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in q)

    def fts(query, k):
        try:
            r = subprocess.run(["/home/ubuntu/qunzhi/venv/bin/python", "-c",
                                "import sys;sys.path.insert(0,'/home/ubuntu/paperlib');"
                                "from search_vec import fts_search;"
                                f"print('\\n'.join(fts_search({query!r}, {k})))"],
                               capture_output=True, text=True, timeout=60)
            return [x for x in r.stdout.split("\n") if x.strip()]
        except Exception:
            return []

    def vec(query, k):
        try:
            r = subprocess.run(["./paperlib/vec_query.sh", query, str(k)],
                               capture_output=True, text=True, timeout=300)
            return [x for x in r.stdout.split("\n") if x.strip()]
        except Exception:
            return []

    en = q
    if has_cjk:
        en, ok_tr = _qz_translate(q)
        if not ok_tr:
            en = q

    f_ids = fts(en, limit)
    v_ids = vec(en, limit)
    # RRF 融合：两路各按名次给分，避免"FTS 结果全部排在前面"
    score = {}
    for lst in (f_ids, v_ids):
        for rank, pid in enumerate(lst):
            score[pid] = score.get(pid, 0.0) + 1.0 / (60 + rank)
    merged = sorted(score.items(), key=lambda kv: -kv[1])
    seen, out = set(), []
    for pid, _ in merged:
        if pid in seen:
            continue
        seen.add(pid)
        row = None
        try:
            with _kb_conn() as c:
                row = c.execute("select pid,title,year,field from papers where pid=?", (pid,)).fetchone()
        except Exception:
            pass
        if not row:
            continue
        out.append({"pid": row["pid"], "title": row["title"], "year": row["year"],
                    "field": row["field"],
                    "from": ("fts" if pid in f_ids else "") + ("+vec" if pid in v_ids else "")})
    # 重排：候选前 20 条过 cross-encoder
    if len(out) > 1:
        cand = [x["pid"] for x in out[:20]]
        try:
            order = []
            try:
                import urllib.request as _u
                body = json.dumps({"query": en, "pids": cand}).encode()
                req = _u.Request("http://127.0.0.1:18098/rerank", data=body,
                                 headers={"Content-Type": "application/json"})
                with _u.urlopen(req, timeout=60) as rr:
                    order = json.loads(rr.read().decode()).get("order") or []
            except Exception:
                r = subprocess.run(["/home/ubuntu/qunzhi/venv/bin/python",
                                    "./paperlib/rerank_cli.py", en] + cand,
                                   capture_output=True, text=True, timeout=120)
                order = [x for x in (r.stdout or "").split("\n") if x.strip()]
            if len(order) >= 2:
                rank = {pid: i for i, pid in enumerate(order)}
                out.sort(key=lambda x: rank.get(x["pid"], 999))
        except Exception:
            pass
    items = out[:limit]
    return {"ok": True, "q": q, "query_en": en, "translated": has_cjk,
            "n": len(items), "fts_n": len(f_ids), "vec_n": len(v_ids),
            "reranked": True, "items": items}


# ---- 问题闸门：显式判定 query 是否属于 CS/ML（P0） ----
# 设计要点：
#   1) 英文查 CS 术语表；中文查中文 CS 术语表（术语 → 英文关键词）
#   2) 不用「模型」这类过宽词，避免放过「经济模型」「物理模型」
#   3) 三种拒绝态可区分：non_cs_topic / no_match / non_en_query
_CS_EN_TERMS = (
    "machine learning", "deep learning", "neural network", "neural networks",
    "contrastive learning", "reinforcement learning", "transfer learning",
    "federated learning", "self-supervised", "semi-supervised",
    "supervised learning", "representation learning", "meta-learning",
    "large language model", "large language models", "language model", "llm",
    "natural language processing", "computer vision", "speech recognition",
    "information retrieval", "knowledge graph", "recommender system",
    "recommendation system", "data mining", "dataset", "benchmark",
    "transformer", "attention mechanism", "embedding", "embeddings",
    "classification", "clustering", "fine-tuning", "finetuning",
    "pre-training", "pretraining", "inference", "prompt engineering",
    "multi-agent", "ai agent", "intelligent agent", "retrieval-augmented",
    "knowledge distillation", "pruning", "quantization", "multimodal",
    "zero-shot", "few-shot", "in-context learning", "chain-of-thought",
    "diffusion model", "generative model", "generative adversarial",
    "object detection", "semantic segmentation", "image classification",
    "machine translation", "question answering", "text classification",
    "sentiment analysis", "named entity recognition", "graph neural network",
    "graph neural networks", "gnn", "cnn", "rnn", "lstm", "bert", "gpt",
    "llama", "sota", "ablation", "reproducibility", "reproduce", "replication",
    "peer review", "arxiv", "quantum computing", "edge computing",
    "cloud computing", "distributed system", "operating system", "database",
    "compiler", "programming language", "software engineering",
    "cybersecurity", "cryptography", "blockchain", "computer science",
    "algorithm", "algorithms", "evaluation", "retrieval", "rag",
    "nlp", "ml", "ai", "cv", "cs",
    # 2026-11-21 补充：评审/评测里常用的标准 CS/ML 术语（此前被误杀）
    "meta learning", "meta-learning", "metalearning",
    "position bias", "sycophancy", "hallucination", "hallucination detection",
    "verbosity", "verbosity bias", "length bias", "contrastive",
    "attention", "self-attention", "cross-attention", "diffusion",
    "rlhf", "human feedback", "reward model", "preference optimization",
    "alignment", "ai safety", "jailbreak", "red teaming", "guardrail",
    "prompt", "prompts", "prompting", "token", "tokens", "tokenizer",
    "logit", "logits", "softmax", "gradient", "gradients", "loss function",
    "optimization", "optimizer", "hyperparameter", "hyperparameters",
    "regularization", "overfitting", "generalization", "distribution shift",
    "calibration", "uncertainty", "interpretability", "explainability",
    "fairness", "robustness", "adversarial", "privacy", "differential privacy",
    "corpus", "annotation", "labeling", "leaderboard", "evaluation metric",
    "instruction tuning", "knowledge editing", "data augmentation",
    "active learning", "continual learning", "online learning",
    "probing", "latent", "generative", "synthetic data",
    "zero shot", "few shot", "chain of thought", "retrieval augmented",
    "multi agent", "llm-as-a-judge", "judge model",
)

# 软术语：不足以单独断定「就是 CS」，但可作为「不是非 CS」的兜底信号。
# 仅用于 total=0 时的判定：命中软术语 → no_match（CS 但没命中），而不是 non_cs_topic。
_CS_SOFT_TERMS = (
    "model", "models", "training", "learning", "neural", "network", "networks",
    "dataset", "datasets", "data", "evaluation", "evaluate", "benchmark",
    "bias", "prompt", "reasoning", "agent", "agents", "performance",
    "annotation", "label", "labels", "sample", "samples", "distribution",
    "system", "systems", "method", "methods", "experiment", "experiments",
)

# 中文 CS 术语 → 英文关键词（同时用于跨语言改写建议）
_CS_ZH = {
    "对比学习": "contrastive learning",
    "机器学习": "machine learning",
    "深度学习": "deep learning",
    "神经网络": "neural network",
    "大语言模型": "large language model",
    "大模型": "large language model",
    "语言模型": "language model",
    "自然语言处理": "natural language processing",
    "计算机视觉": "computer vision",
    "强化学习": "reinforcement learning",
    "迁移学习": "transfer learning",
    "联邦学习": "federated learning",
    "多模态": "multimodal",
    "知识图谱": "knowledge graph",
    "推荐系统": "recommender system",
    "信息检索": "information retrieval",
    "图神经网络": "graph neural network",
    "生成模型": "generative model",
    "扩散模型": "diffusion model",
    "目标检测": "object detection",
    "语义分割": "semantic segmentation",
    "文本分类": "text classification",
    "情感分析": "sentiment analysis",
    "机器翻译": "machine translation",
    "问答系统": "question answering",
    "智能体": "agent",
    "提示工程": "prompt engineering",
    "提示词": "prompt",
    "微调": "fine-tuning",
    "预训练": "pretraining",
    "注意力机制": "attention mechanism",
    "卷积": "convolution",
    "消融实验": "ablation study",
    "消融": "ablation",
    "数据集": "dataset",
    "评测": "benchmark evaluation",
    "基准": "benchmark",
    "算法": "algorithm",
    "论文": "paper",
    "审稿": "peer review",
    "复现": "reproducibility",
    "量子计算": "quantum computing",
    "计算机科学": "computer science",
    "计算机": "computer science",
    # 2026-11-21：评审/评测常用术语（此前中文侧也会被误判成非 CS）
    "元学习": "meta learning",
    "位置偏见": "position bias",
    "谄媚": "sycophancy",
    "幻觉": "hallucination",
    "冗长": "verbosity",
    "长度偏见": "length bias",
    "注意力": "attention",
    "扩散": "diffusion",
    "对齐": "alignment",
    "人类反馈": "human feedback",
    "奖励模型": "reward model",
    "越狱": "jailbreak",
    "公平性": "fairness",
    "鲁棒性": "robustness",
    "可解释性": "interpretability",
    "隐私": "privacy",
    "对抗": "adversarial",
    "语料": "corpus",
    "标注": "annotation",
    "指令微调": "instruction tuning",
    "小样本": "few-shot",
    "零样本": "zero-shot",
    "思维链": "chain-of-thought",
    "检索增强": "retrieval-augmented",
    "多智能体": "multi-agent",
    "编程": "programming",
    "软件工程": "software engineering",
    "数据库": "database",
    "编译器": "compiler",
    "信息安全": "cybersecurity",
    "区块链": "blockchain",
    "边缘计算": "edge computing",
    "云计算": "cloud computing",
}

# 明确属于其它领域的词：用于识别「CS + 化学/医学/金融…」的混合查询。
# 混合查询不自动翻译（避免把化学问题误译成纯 CS 查询），按原文检索后报 no_match。
_NON_CS_ZH = (
    "化学", "生物", "医学", "医疗", "临床", "药学", "金融", "经济", "会计",
    "法律", "法学", "教育", "教学", "心理", "材料", "物理", "地质", "农业",
    "烹饪", "菜谱", "红烧肉", "美食", "旅游", "体育", "音乐", "艺术",
    "历史", "哲学", "文学", "社会学", "政治", "军事", "天气", "股票",
    "房价", "健康", "营养", "健身",
)

# 中文查询里的功能词/疑问词：跨语言改写时剔除，不算“未翻译内容”
_ZH_STOPWORDS = (
    "请问", "帮我", "我想", "我要", "找一下", "找找", "关于", "方面", "相关",
    "如何", "怎么", "怎样", "什么", "是什么", "有哪些", "为什么", "是否",
    "在", "的", "中", "里", "对", "和", "与", "及", "或", "及其", "以及",
    "应用", "研究", "方法", "技术", "问题", "论文", "文献", "工作", "进展",
    "综述", "介绍", "分析", "实现", "设计", "优化", "改进", "提升",
    "吗", "呢", "吧", "啊", "请", "给", "我", "想", "要", "做", "用",
)


def _has_cjk(s: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in (s or ""))


def _en_term_hit(low: str, term: str) -> bool:
    if " " in term or "-" in term:
        return term in low
    return re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", low) is not None


def _gate_translate_zh(q: str) -> str:
    """用内置术语表把纯 CS 中文查询改写成英文关键词；无法完整覆盖时返回空串。"""
    s = q or ""
    for zh in sorted(_CS_ZH, key=len, reverse=True):
        if zh in s:
            s = s.replace(zh, " %s " % _CS_ZH[zh])
    for w in sorted(_ZH_STOPWORDS, key=len, reverse=True):
        s = s.replace(w, " ")
    # 还有中文残留 → 说明不是我们能可靠改写的纯 CS 查询
    if _has_cjk(s):
        return ""
    s = re.sub(r"[^A-Za-z0-9\-\+ ]+", " ", s)
    words, seen = [], set()
    for w in s.split():
        wl = w.lower()
        if wl not in seen:
            seen.add(wl)
            words.append(w)
    return " ".join(words)[:200]


# ==================== 中文查询跨语言改写（2026-11-21 zh-search） ====================
# 目标：中文查询不再硬拒绝。先用 LLM（glm-4-flash）把中文查询改写成
# 2-5 个英文学术关键词，再检索；翻译结果缓存 24 小时（同 query 不重复调 LLM）。
# 注意：这不是“直译”——prompt 要求使用领域公认术语（对比学习=contrastive learning）。
_ZH_REWRITE_MODEL = os.environ.get("QZ_ZH_REWRITE_MODEL", "glm-4-flash")
_ZH_REWRITE_PROMPT_VERSION = "zh-rewrite-v1"
_ZH_REWRITE_TTL = 24 * 3600
_ZH_REWRITE_DIR = os.path.join(V15_ROOT, "app", "data", "cache", "zh_search")
_ZH_REWRITE_PROMPT = """你是计算机/机器学习领域的学术检索词改写器。把中文查询改写成英文论文库检索词。
规则：
1) 使用该领域公认的英文术语；不要直译，也不要用近义但不常用的说法。
   例：对比学习=contrastive learning（不是 comparative learning）；大语言模型=large language model / LLM。
2) "query" 是最适合直接检索的一条紧凑查询，保留查询里的核心概念，2-6 个英文词，空格分隔，不要引号、不要 AND/OR。
3) "keywords" 给 2-5 个英文关键词短语（同义/相关方向），每个 1-5 个词。
4) 只输出 JSON，不要解释：{{"query":"...","keywords":["...","..."]}}

示例：
输入：大语言模型幻觉检测
输出：{{"query":"large language model hallucination detection","keywords":["LLM hallucination","factual hallucination","hallucination evaluation"]}}
输入：对比学习
输出：{{"query":"contrastive learning","keywords":["contrastive learning","self-supervised representation learning","instance discrimination"]}}
输入：多智能体强化学习
输出：{{"query":"multi-agent reinforcement learning","keywords":["multi-agent RL","cooperative multi-agent learning","agent collaboration"]}}

中文查询：{q}
"""


def _zh_norm_query(q: str) -> str:
    return re.sub(r"\s+", " ", (q or "").strip())


def _zh_cache_key(q: str) -> str:
    raw = "|".join((_zh_norm_query(q), _ZH_REWRITE_MODEL, _ZH_REWRITE_PROMPT_VERSION))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _zh_cache_path(q: str) -> str:
    return os.path.join(_ZH_REWRITE_DIR, _zh_cache_key(q) + ".json")


def _zh_call_id(q: str) -> str:
    """翻译记录 ID（可 /api/call/{call_id} 回查）。同一 query 稳定不变。"""
    return "qz_tr_" + _zh_cache_key(q)[:16]


def _zh_cache_get(q: str):
    fp = _zh_cache_path(q)
    try:
        with open(fp, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict) or d.get("version") != 1:
            return None
        age = time.time() - float(d.get("ts") or 0)
        if age < 0 or age >= _ZH_REWRITE_TTL:
            return None
        if not d.get("query"):
            return None
        return d, age
    except Exception:
        return None


def _zh_cache_put(q: str, data: dict) -> None:
    try:
        os.makedirs(_ZH_REWRITE_DIR, exist_ok=True)
        entry = {"version": 1, "q": _zh_norm_query(q), "ts": time.time(),
                 "model": data.get("model"), "method": data.get("method"),
                 "query": data.get("query"),
                 "keywords": list(data.get("keywords") or [])[:5],
                 "raw": (data.get("raw") or "")[:2000]}
        fp = _zh_cache_path(q)
        tmp = "%s.%d.%d.tmp" % (fp, os.getpid(), threading.get_ident())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entry, f, ensure_ascii=False)
        os.replace(tmp, fp)
    except Exception as e:
        print("[zh-rewrite] 缓存写入失败：%r" % (e,), flush=True)


def _glm_api_key() -> str:
    key = (os.environ.get("GLM_API_KEY") or "").strip()
    if key:
        return key
    for fp in ("./experiments/.env", "/home/ubuntu/qunzhi/.env"):
        try:
            with open(fp, encoding="utf-8", errors="ignore") as f:
                for ln in f:
                    if ln.startswith("GLM_API_KEY="):
                        return ln.split("=", 1)[1].strip()
        except Exception:
            pass
    return ""


def _valid_en_query(s: str) -> bool:
    s = (s or "").strip()
    if not s or _has_cjk(s) or len(s) > 120:
        return False
    if not re.search(r"[A-Za-z]", s):
        return False
    return 1 <= len(s.split()) <= 10


def _parse_zh_rewrite_json(text: str):
    s = (text or "").strip()
    s = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", s)
    s = re.sub(r"\s*```$", "", s).strip()
    obj = None
    try:
        obj = json.loads(s)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", s)
        if m:
            try:
                obj = json.loads(m.group(0))
            except Exception:
                obj = None
    if not isinstance(obj, dict):
        return None
    q = str(obj.get("query") or "").strip().strip('"').strip()
    kws = obj.get("keywords")
    if isinstance(kws, str):
        kws = [kws]
    if not isinstance(kws, list):
        kws = []
    clean = []
    for k in kws:
        k = str(k).strip().strip('"').strip()
        if k and _valid_en_query(k) and k not in clean:
            clean.append(k)
        if len(clean) >= 5:
            break
    if not _valid_en_query(q):
        q = clean[0] if clean else ""
    if not q:
        return None
    if not clean:
        clean = [q]
    return {"query": q, "keywords": clean}


def _zh_llm_rewrite(q: str):
    """调 glm-4-flash 改写；返回 (data, error)。"""
    key = _glm_api_key()
    if not key:
        return None, "no_key"
    body = json.dumps({
        "model": _ZH_REWRITE_MODEL,
        "messages": [{"role": "user",
                      "content": _ZH_REWRITE_PROMPT.format(q=(q or "")[:200])}],
        "temperature": 0.1, "max_tokens": 300,
    }).encode("utf-8")
    try:
        import requests as _requests
        r = _requests.post("https://open.bigmodel.cn/api/paas/v4/chat/completions",
                           headers={"Authorization": "Bearer " + key,
                                    "Content-Type": "application/json"},
                           data=body, timeout=(5, 15))
        if r.status_code != 200:
            return None, "http_%d" % r.status_code
        text = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        data = _parse_zh_rewrite_json(text)
        if not data:
            return None, "bad_json"
        data.update({"method": "llm_rewrite", "model": _ZH_REWRITE_MODEL,
                     "raw": text[:2000]})
        return data, ""
    except Exception as e:
        return None, "err:" + repr(e)[:120]


def _zh_rewrite_retrieval(q: str):
    """中文 → 英文检索词：LLM 优先，失败时退回术语表/DeepSeek。

    成功结果缓存 24h；只有 LLM 成功结果落缓存，避免一次网络故障被缓存一天。
    """
    hit = _zh_cache_get(q)
    if hit:
        d, age = hit
        d = dict(d)
        d["_cached"] = True
        d["_cache_age_sec"] = round(age, 3)
        print("[zh-rewrite] cache hit q=%r -> %r age=%.1fs" %
              (q[:40], d.get("query"), age), flush=True)
        return d
    t0 = time.time()
    data, err = _zh_llm_rewrite(q)
    if data:
        _zh_cache_put(q, data)
        print("[zh-rewrite] llm q=%r -> %r keywords=%r %.2fs" %
              (q[:40], data.get("query"), data.get("keywords"),
               time.time() - t0), flush=True)
        return data
    # LLM 失败：术语表兜底，再退 DeepSeek 直译（不落 24h 缓存）
    g = _gate_translate_zh(q)
    if g:
        data = {"query": g, "keywords": [g], "method": "glossary_fallback",
                "model": "builtin_glossary", "raw": ""}
    else:
        en, ok = _qz_translate(q)
        if ok and _valid_en_query(en):
            data = {"query": en, "keywords": [en], "method": "deepseek_translate",
                    "model": "deepseek-chat", "raw": ""}
    if data:
        print("[zh-rewrite] fallback(%s) q=%r -> %r (llm_error=%s)" %
              (data.get("method"), q[:40], data.get("query"), err), flush=True)
        return data
    print("[zh-rewrite] failed q=%r err=%s" % (q[:40], err), flush=True)
    return None


def _fts_hits_for_query(q: str):
    """候选英文检索词的 FTS 命中数；索引不可用/语法错误返回 None。"""
    if not q or not os.path.exists(_KB_FTS):
        return None
    terms = [t.replace('"', "") for t in q.split() if t.replace('"', "")]
    if not terms:
        return 0
    match = " AND ".join('"%s"' % t for t in terms)
    try:
        c = _sqlite3.connect("file:%s?mode=ro" % _KB_FTS, uri=True, timeout=8)
        try:
            return int(c.execute("select count(*) from fts where fts match ?",
                                 (match,)).fetchone()[0])
        finally:
            c.close()
    except Exception:
        return None


_ZH_FALLBACK_STOP = {"a", "an", "the", "of", "in", "on", "for", "with", "and",
                     "or", "to", "via", "using", "based", "from", "by"}


def _zh_tokens(s: str):
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower())
            if t not in _ZH_FALLBACK_STOP}


def _zh_pick_query(data: dict):
    """从 LLM 的 query + keywords 里选实际检索词。

    优先用 LLM 的紧凑 query（保留原查询核心概念）；若它在库里 0 命中，
    再依次试 keywords（同义/相关方向），选第一个有命中的。
    兜底 keyword 必须与主查询至少共享一个实词，避免“火星殖民”落到
    “AI in space exploration”这类无关宽词上。
    """
    primary = (data.get("query") or "").strip()
    cands = []
    for c in [primary] + list(data.get("keywords") or []):
        c = (c or "").strip()
        if c and c not in cands and _valid_en_query(c):
            cands.append(c)
    if not cands:
        return primary, "none", {}
    ptok = _zh_tokens(primary)
    counts = {}
    for c in cands:
        n = _fts_hits_for_query(c)
        counts[c] = n
        if n is not None and n > 0:
            if c == primary or not ptok or (ptok & _zh_tokens(c)):
                return c, ("llm_query" if c == primary else "keyword_fallback"), counts
    return primary or cands[0], "llm_query", counts


def _zh_rewrite_gate(q: str, matched: list):
    """构造中文改写的 gate 与检索词；失败返回 (None, "")。"""
    data = _zh_rewrite_retrieval(q)
    if not data:
        return None, ""
    chosen, selection, counts = _zh_pick_query(data)
    if not chosen:
        return None, ""
    keywords = list(data.get("keywords") or [])[:5]
    translated = {
        "query": chosen,
        "method": data.get("method") or "llm_rewrite",
        "model": data.get("model") or _ZH_REWRITE_MODEL,
        "call_id": _zh_call_id(q),
        "keywords": keywords,
        "cached": bool(data.get("_cached")),
        "cache_ttl_sec": _ZH_REWRITE_TTL,
        "selection": selection,
        "candidate_hits": counts,
    }
    if data.get("_cached"):
        translated["cache_age_sec"] = data.get("_cache_age_sec")
    if chosen != (data.get("query") or ""):
        translated["llm_query"] = data.get("query")
    if data.get("method") == "llm_rewrite" and selection == "llm_query":
        translated["note"] = "LLM 学术改写（非直译）；按英文关键词检索"
    try:
        _save_call(translated["call_id"], {
            "ok": True, "call_id": translated["call_id"],
            "computed_at": _utc_now(), "q": q, "translated": translated,
            "source": "zh_rewrite", "cache_ttl_sec": _ZH_REWRITE_TTL})
    except Exception:
        pass
    gate = {"passed": True, "reason": "auto_translated",
            "hint": "已将中文查询改写为英文学术关键词（LLM 改写，非直译）",
            "matched_terms": matched, "translated": translated,
            "query_en": chosen}
    return gate, chosen


def _kb_soft_hits(q: str):
    """软 CS 信号：仅用于 total=0 时区分 no_match / non_cs_topic。"""
    low = (q or "").lower()
    return [t for t in _CS_SOFT_TERMS if _en_term_hit(low, t)]


def _kb_gate(q: str, auto_translate: bool = False):
    """返回 (gate, 实际检索词)。

    2026-11-21 改版（Bug 1「gate 误杀真 CS 术语」）——**先检索，再判定**：

    · 明确非 CS（中文 + 命中 _NON_CS_ZH + 无任何 CS 术语）→ 直接 non_cs_topic；
    · 其余无 CS 硬术语的查询 → gate.defer=True，先检索，再由 _kb_finish 按 total 判定：
        total>0                  → ok（有命中就不可能是"非 CS"）
        total=0 且有软 CS 信号   → no_match（是 CS，只是库里没命中）
        total=0 且毫无 CS 信号   → non_cs_topic
    · 中文 + CS 术语：默认 auto_translate=True → LLM 改写成英文学术关键词
      （非混合领域；显式 auto_translate=false 才维持 non_en_query）；
    · 混合领域（CS + 化学/医学…）→ 放行并按原文检索 → no_match。
    """
    gate = {"passed": True, "reason": "ok", "hint": ""}
    q = (q or "").strip()
    if not q:
        return gate, q

    low = q.lower()
    en_hits = [t for t in _CS_EN_TERMS if _en_term_hit(low, t)]
    zh_hits = [z for z in _CS_ZH if z in q]
    # 硬术语命中：英文按词边界，中文按子串
    if not en_hits and not zh_hits:
        non_cs = [w for w in _NON_CS_ZH if w in q]
        if _has_cjk(q) and non_cs:
            return {"passed": False, "reason": "non_cs_topic",
                    "hint": "这个查询不像 CS/ML 主题；本库只收计算机/机器学习论文，请换 CS 关键词",
                    "matched_terms": [], "non_cs_terms": non_cs[:5]}, q
        # 中文且未命中非 CS 词：默认也尝试 LLM 改写（覆盖术语表未收录的 CS 说法）
        if _has_cjk(q) and auto_translate:
            g, en = _zh_rewrite_gate(q, [])
            if g:
                return g, en
        soft = _kb_soft_hits(q)
        return {"passed": True, "reason": "ok", "hint": "",
                "defer": True, "soft_hit": bool(soft),
                "matched_terms": soft[:6]}, q

    matched = (zh_hits + en_hits)[:6]
    if _has_cjk(q):
        non_cs = [w for w in _NON_CS_ZH if w in q]
        suggestion = _gate_translate_zh(q)
        if non_cs:
            # CS 术语 + 其它领域：领域闸门放行，但不自动翻译，按原文检索 → 通常 no_match
            return {"passed": True, "reason": "no_match",
                    "hint": "已识别 CS 主题，但同时包含其它领域词（%s）；请用英文关键词拆分检索"
                            % "、".join(non_cs[:3]),
                    "matched_terms": matched, "mixed_domain": non_cs[:5]}, q
        if not auto_translate:
            return {"passed": False, "reason": "non_en_query",
                    "hint": ("请用英文关键词；可改用：%s" % suggestion) if suggestion
                            else "请用英文关键词（本库论文以英文为主，中文查询无法直接命中）",
                    "matched_terms": matched,
                    "suggested_query": suggestion or None}, q
        g, en = _zh_rewrite_gate(q, matched)
        if g:
            return g, en
        return {"passed": False, "reason": "non_en_query",
                "hint": "请用英文关键词（自动跨语言改写失败：LLM/术语表/DeepSeek 均未返回可用英文）",
                "matched_terms": matched, "suggested_query": suggestion or None}, q

    return {"passed": True, "reason": "ok", "hint": "",
            "matched_terms": matched}, q


def _kb_finish(result: dict, gate: dict, total: int,
               q_original: str = None, q_search: str = None,
               venue_input: str = None, call_params: dict = None) -> dict:
    """检索后补 gate：有命中=ok，无命中=no_match（域闸门仍算通过）。

    2026-11-21：支持 gate["defer"]（先检索再判定），并新增 gate["soft_hit"]。
    2026-11-21（call_id 修复）：检索结果挂 call_id / computed_at / search_id，
    同参数 + 同数据快照重复调用复用落盘结果，fetch_count 递增。
    """
    g = dict(gate)
    deferred = bool(g.pop("defer", False))
    soft_hit = bool(g.pop("soft_hit", False))
    translated = g.pop("translated", None)
    translated_used = bool(isinstance(translated, dict) and translated.get("query"))
    if deferred:
        if total:
            # 实测反证优先：库里能搜到，就不可能是"非 CS"
            g["passed"] = True
            g["reason"] = "ok"
            g["hint"] = ""
        elif soft_hit:
            g["passed"] = True
            g["reason"] = "no_match"
            g["hint"] = "查询含 CS/ML 信号，但库内没有命中；可换更通用的英文关键词"
        else:
            g["passed"] = False
            g["reason"] = "non_cs_topic"
            g["hint"] = "这个查询不像 CS/ML 主题；本库只收计算机/机器学习论文，请换 CS 关键词"
    elif not total:
        if translated_used:
            broad = ""
            try:
                singles = [k for k in (translated.get("keywords") or [])
                           if len(k.split()) == 1]
                if singles:
                    broad = singles[-1]
                elif translated.get("query"):
                    _ws = [w for w in re.findall(r"[A-Za-z0-9-]+", translated["query"])
                           if len(w) > 3 and w.lower() not in _ZH_FALLBACK_STOP]
                    broad = max(_ws, key=len) if _ws else translated["query"].split()[-1]
            except Exception:
                broad = ""
            g["passed"] = True
            g["reason"] = "translated_no_match"
            g["hint"] = ("已改写为英文，但库内无命中；可试更通用的词%s"
                         % (("（如 '%s'）" % broad) if broad else "（如只用核心概念词）"))
        else:
            g["passed"] = True
            g["reason"] = "no_match"
            g["hint"] = g.get("hint") or "查询属于 CS/ML 主题，但库内没有命中；可换更通用的英文关键词"
    elif g.get("reason") == "no_match":
        g["reason"] = "ok"
        g["hint"] = ""
    result["gate"] = g
    if translated_used:
        result["translated"] = translated
    # 保持既有 q 字段=用户原查询；翻译后的检索词放新字段 query_en
    if q_original is not None:
        result["q"] = q_original
    if q_search and q_search != q_original:
        result["query_en"] = q_search
    # 问题 5：补 total 上限、matched_terms 说明、lang 实测口径、残条目标记。
    enriched = _search_enrich(result, q_original, q_search, venue_input)
    if call_params is not None:
        enriched = _search_attach_call(enriched, call_params)
    return enriched


@app.post("/api/kb/search")
def kb_search(payload: dict):
    """检索口径 wrapper：给所有返回路径（含缓存命中）统一挂 scope_note。

    scope_note 说明本次检索是全库还是加了 venue 过滤、各来源口径各多少篇，
    并把「发表 venue 名过滤」和「top/arxiv/peer 来源口径」的区别讲清楚。
    """
    out = _kb_search_impl(payload)
    try:
        if isinstance(out, dict) and _sensitivity is not None:
            _note, _meta = _sensitivity.search_scope_note(
                (payload or {}).get("venue") if isinstance(payload, dict) else None)
            out.setdefault("scope_note", _note)
            out.setdefault("scope_note_meta", _meta)
    except Exception as _e:
        if isinstance(out, dict):
            out.setdefault("scope_note", "口径说明暂不可用：%r" % (_e,))
    return out


def _kb_search_impl(payload: dict):
    """关键词检索：FTS5 优先（毫秒级），无索引时回落 LIKE；支持过滤

    先过 CS/ML 问题闸门：非 CS → non_cs_topic；中文 CS 查询默认走
    auto_translate=true（LLM 改写成英文学术关键词，缓存 24h）；CS 但无命中 →
    no_match（改写后无命中 → translated_no_match）。显式 auto_translate=false
    可关闭改写，恢复 non_en_query 契约。
    """
    p = payload or {}
    if not isinstance(p, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    _raw_q = p.get("q")
    if _raw_q is not None and not isinstance(_raw_q, str):
        raise HTTPException(status_code=400, detail="q 必须是字符串")
    q = (_raw_q or "").strip()
    orig_q = q
    try:
        limit = max(1, min(int(p.get("limit") or 10), 50))
    except Exception:
        raise HTTPException(status_code=400, detail="limit 必须是 1-50 的整数")
    # 2026-11-21 zh-search：默认开启中文→英文改写。显式 false 才关闭。
    _at = p.get("auto_translate")
    if _at is None:
        _at = p.get("translate")
    if _at is None:
        auto_translate = True
    elif isinstance(_at, bool):
        auto_translate = _at
    else:
        auto_translate = str(_at).strip().lower() not in ("0", "false", "no", "off", "")
    try:
        _year = int(p["year"]) if p.get("year") else None
    except Exception:
        raise HTTPException(status_code=400, detail="year 必须是整数")
    # 问题 4（glm-fix4）：先解析 venue 别名，再拼 SQL；解析结果随响应回显。
    venue_res = _resolve_venue(str(p.get("venue") or ""))
    gate, search_q = _kb_gate(q, auto_translate=auto_translate)
    # 2026-11-21（call_id 修复）：参数归一化后先查内容寻址缓存。
    # 同 q / 过滤条件 / 数据快照 → 同 call_id；命中则原样返回并递增 fetch_count。
    _sparams = _search_call_params(search_q, limit, p, year=_year,
                                   auto_translate=auto_translate)
    _cached = _search_cached(_call_id("/api/kb/search", _sparams))
    if _cached is not None:
        # 检索结果命中落盘缓存时，用本次实时翻译元数据刷新（让 translated.cached
        # 真实反映 24h 翻译缓存是否命中，而不是第一次检索时的 false）。
        if gate.get("translated"):
            _cached["translated"] = gate["translated"]
            _cached["query_en"] = search_q
            # 保留落盘 gate 的检索后判定；只刷新翻译元数据，避免把
            # translated_no_match 覆盖回检索前的 auto_translated。
            _g = dict(_cached.get("gate") or gate)
            _g["query_en"] = search_q
            if int(_cached.get("total") or 0) > 0:
                _g["passed"] = True
                _g["reason"] = "auto_translated"
            else:
                _g["passed"] = True
                _g["reason"] = "translated_no_match"
            _cached["gate"] = _g
        return _cached
    if gate.get("reason") in ("non_cs_topic", "non_en_query"):
        _gate_out = {"ok": True, "engine": "gate", "q": orig_q, "total": 0,
                     "returned": 0, "items": [], "gate": gate}
        _gate_out["sources"] = _search_sources_single(
            "gate", 0, 0, "查询被领域闸门拦截，未执行检索，双源不可比")
        return _search_attach_call(
            _search_enrich(_gate_out, orig_q, search_q, venue_res.get("input")),
            _sparams)
    q = search_q
    filters, fargs = [], []
    if p.get("field"):
        filters.append("p.field like ?")
        fargs.append("%" + str(p["field"]) + "%")
    if p.get("venue"):
        # 规范名 + 别名一起匹配（upper 做大小写不敏感）；不在表内的按原样精确匹配。
        vals = [v for v in (venue_res.get("matched_values") or [venue_res.get("resolved")])
                if v]
        filters.append("upper(p.venue) in (%s)" % ",".join("?" * len(vals)))
        fargs.extend([str(v).upper() for v in vals])
    if _year is not None:
        filters.append("p.year = ?")
        fargs.append(_year)
    if p.get("decision"):
        filters.append("p.decision = ?")
        fargs.append(str(p["decision"]))
    fwhere = (" and " + " and ".join(filters)) if filters else ""

    base_cols = ("p.pid, p.title, p.abstract, p.year, p.venue, p.field, p.decision, p.n_chars, "
                 "p.quality_flags, "
                 "(select count(*) from issues i where i.pid = p.pid) as n_issues ")

    # ---- FTS 优先 ----
    if q and os.path.exists(_KB_FTS):
        terms = [t for t in q.replace('"', " ").split() if t]
        if terms:
            match = " AND ".join('"%s"' % t for t in terms)
            try:
                f = _sqlite3.connect("file:%s?mode=ro" % _KB_FTS, uri=True, timeout=8)
                f.row_factory = _sqlite3.Row
                ids = [r["pid"] for r in f.execute(
                    "select pid from fts where fts match ? order by rank limit ?", (match, 200))]
                total = f.execute("select count(*) from fts where fts match ?", (match,)).fetchone()[0]
                # 第二个源：同一查询限定在 title/abstract 列（不含 issues 评审文本）。
                # 与全字段源共用 pid 交并集算 agreement；不是同一个结果集换名字。
                _K = max(20, min(int(limit) * 2, 50))
                core_match = "{title abstract} : (" + match + ")"
                core_ids, core_total = [], 0
                try:
                    core_ids = [r["pid"] for r in f.execute(
                        "select pid from fts where fts match ? order by rank limit ?",
                        (core_match, _K))]
                    core_total = f.execute("select count(*) from fts where fts match ?",
                                           (core_match,)).fetchone()[0]
                except Exception:
                    core_ids, core_total = [], 0
                f.close()
                if ids:
                    ph = ",".join("?" * len(ids))
                    sql = ("select " + base_cols + "from papers p where p.pid in (" + ph + ")"
                           + fwhere + " limit ?")
                    with _kb_conn() as c:
                        rows = [dict(r) for r in c.execute(sql, ids + fargs + [limit])]
                    order = {pid: i for i, pid in enumerate(ids)}
                    rows.sort(key=lambda r: order.get(r["pid"], 999))
                    # 第二个源也跑完整过滤，returned 是真实可展示条数。
                    core_rows = []
                    if core_ids:
                        try:
                            ph2 = ",".join("?" * len(core_ids))
                            sql2 = ("select " + base_cols + "from papers p where p.pid in (" + ph2 + ")"
                                    + fwhere + " limit ?")
                            with _kb_conn() as c2:
                                core_rows = [dict(r) for r in c2.execute(sql2, core_ids + fargs + [limit])]
                        except Exception:
                            core_rows = []
                    return _kb_finish({"ok": True, "engine": "fts5", "q": q, "total": total,
                                       "returned": len(rows), "items": rows,
                                       "sources": _search_sources(
                                           total, len(rows), core_total, len(core_rows),
                                           ids[:_K], core_ids[:_K])},
                                      gate, total, orig_q, q, venue_res.get("input"),
                                      call_params=_sparams)
                _empty_out = {"ok": True, "engine": "fts5", "q": q, "total": total,
                              "returned": 0, "items": [],
                              "sources": _search_sources(total, 0, core_total, 0,
                                                         ids[:_K], core_ids[:_K])}
                return _kb_finish(_empty_out, gate, total, orig_q, q,
                                  venue_res.get("input"), call_params=_sparams)
            except Exception as e:
                print("[kb] FTS 失败，回落 LIKE:", e, flush=True)

    # ---- 回落 LIKE ----
    where, args = [], []
    if q:
        like = "%" + q.replace("%", "").replace("_", "") + "%"
        where.append("(p.title like ? or p.abstract like ?)")
        args += [like, like]
    # 问题 4（glm-fix4）：LIKE 回落路径原来漏了 venue/field/year/decision 过滤，
    # 导致 q 为空时 venue 参数被静默忽略（同一接口两种行为）。这里统一拼上。
    where.extend(filters)
    args.extend(fargs)
    sql_where = (" where " + " and ".join(where)) if where else ""
    score = "(case when p.title like ? then 3 else 1 end)" if q else "1"
    args2 = ([("%" + q + "%")] if q else [])
    sql = ("select " + base_cols + ", " + score + " as score from papers p" + sql_where
           + " order by score desc, n_chars desc limit ?")
    with _kb_conn() as c:
        rows = [dict(r) for r in c.execute(sql, args2 + args + [limit])]
        total = c.execute("select count(*) from papers p" + sql_where, args).fetchone()[0]
    _like_out = {"ok": True, "engine": "like", "q": q, "total": total,
                 "returned": len(rows), "items": rows,
                 "sources": _search_sources_single(
                     "like", total, len(rows),
                     "FTS5 索引不可用，回落 LIKE；仅单源，未完成双源交叉验证")}
    return _kb_finish(_like_out, gate, total, orig_q, q,
                      venue_res.get("input"), call_params=_sparams)


@app.get("/api/kb/paper/{pid}")
def kb_paper(pid: str, issues: int = 20):
    with _kb_conn() as c:
        row = c.execute("select pid,title,year,venue,field,decision,ratings,n_chars,source "
                        "from papers where pid = ?", (pid,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="论文不存在")
        its = c.execute("select text, cluster_id from issues where pid = ? limit ?",
                        (pid, max(1, min(int(issues), 100)))).fetchall()
    d = dict(row)
    d["issues"] = [{"text": r["text"], "cluster": r["cluster_id"]} for r in its]
    out = {"ok": True, "paper": d}
    # 2026-11-22 前端 BUG-005：单篇接口补 call_id（翻页/回放可复算）。
    out.update(_call_meta("/api/kb/paper",
                          {"pid": pid, "issues": max(1, min(int(issues), 100))},
                          {"description": "paper row + extracted issues"},
                          {"n_issues": len(d["issues"])},
                          metric="paper_issues", scope="paper:" + pid))
    _save_call(out.get("call_id"), out)
    return out


# ---- KB INSIGHTS (auto-inserted) ----
_INSIGHTS_JSON = os.environ.get("QZ_KB_INSIGHTS", "./paperlib/kb_insights.json")
_MINE_JSON = "./paperlib/mined_issues.json"


@app.get("/api/kb/insights")
def kb_insights():
    """库洞察：哪类问题与「被拒」最相关（按篇统计，非按条）"""
    if not os.path.exists(_INSIGHTS_JSON):
        raise HTTPException(status_code=503, detail="洞察数据未生成（跑 mine_insights.py）")
    d = json.load(open(_INSIGHTS_JSON, encoding="utf-8"))
    extra = {}
    if os.path.exists(_MINE_JSON):
        try:
            m = json.load(open(_MINE_JSON, encoding="utf-8"))
            extra = {"weights_mined": m.get("weights_mined"), "issues_classified": m.get("issues_classified")}
        except Exception:
            pass
    d.update(extra)
    # 按区分力排序
    cls = d.get("classes") or {}
    d["ranked"] = sorted(cls.items(), key=lambda x: -abs((x[1] or {}).get("diff_pt", 0)))
    return d


# ---- KB INSIGHTS2 (auto-inserted) ----
_INSIGHTS2 = os.environ.get("QZ_KB_INSIGHTS2", "./paperlib/kb_insights2.json")


@app.get("/api/kb/insights2")
def kb_insights2():
    """第二辑洞察：领域切片 / 拒稿话术 / 写作基线"""
    if not os.path.exists(_INSIGHTS2):
        raise HTTPException(status_code=503, detail="洞察数据未生成（跑 mine_v2.py）")
    return json.load(open(_INSIGHTS2, encoding="utf-8"))


@app.get("/api/kb/similar/{pid}")
def kb_similar(pid: str, k: int = 8):
    """相似论文：用标题关键词在 FTS 上找（支撑"该比未比"线索）"""
    import sqlite3 as _sq
    if not os.path.exists(_KB_FTS):
        raise HTTPException(status_code=503, detail="FTS 索引未就绪")
    with _kb_conn() as c:
        row = c.execute("select title from papers where pid=?", (pid,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="论文不存在")
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", row["title"])][:6]
    if not words:
        return {"ok": True, "items": []}
    match = " OR ".join('"%s"' % w.lower() for w in words)
    f = _sq.connect("file:%s?mode=ro" % _KB_FTS, uri=True, timeout=8)
    f.row_factory = _sq.Row
    ids = [r["pid"] for r in f.execute(
        "select pid from fts where fts match ? order by rank limit ?", (match, max(1, min(k, 20))))]
    f.close()
    ids = [i for i in ids if i != pid]
    if not ids:
        return {"ok": True, "items": []}
    ph = ",".join("?" * len(ids))
    with _kb_conn() as c:
        rows = [dict(r) for r in c.execute(
            "select pid,title,year,venue,field,decision from papers where pid in (" + ph + ")", ids)]
    order = {p: i for i, p in enumerate(ids)}
    rows.sort(key=lambda r: order.get(r["pid"], 999))
    return {"ok": True, "query_pid": pid, "items": rows}
# ---- /KB INSIGHTS2 END ----


# ---- KB FEATURES API ----
_BASELINE2 = "./paperlib/baseline2.json"


@app.get("/api/kb/baseline2")
def kb_baseline2(field: str = "", year: int = 0):
    """分领域/分年份的同行基线（15 项做法的比例）"""
    if not os.path.exists(_BASELINE2):
        raise HTTPException(status_code=503, detail="基线未生成（跑 baseline_v2.py）")
    d = json.load(open(_BASELINE2, encoding="utf-8"))
    if field and field in (d.get("by_field") or {}):
        return {"ok": True, "slice": f"field:{field}", **(d["by_field"][field])}
    if year and str(year) in (d.get("by_year") or {}):
        return {"ok": True, "slice": f"year:{year}", **(d["by_year"][str(year)])}
    return {"ok": True, "slice": "overall",
            "n_papers": d.get("n_papers"),
            "rates": d.get("overall"),
            "recent_2025_2026": d.get("recent_2025_2026"),
            "fields": list((d.get("by_field") or {}).keys()),
            "years": list((d.get("by_year") or {}).keys())}


@app.get("/api/kb/features/{pid}")
def kb_features(pid: str):
    """单篇论文做到了哪些做法（seed/ci/ablation/...）"""
    with _kb_conn() as c:
        row = c.execute("select title, field, year from papers where pid=?", (pid,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="论文不存在")
    feats = []
    try:
        with _kb_conn() as c:
            feats = [r[0] for r in c.execute(
                "select feat from paper_features where pid=?", (pid,))]
    except Exception:
        pass
    return {"ok": True, "pid": pid, "title": row["title"], "field": row["field"],
            "year": row["year"], "features": sorted(feats), "n_features": len(feats)}


@app.get("/api/kb/gaps")
def kb_gaps(feat: str = "seed", field: str = "", limit: int = 10, has: bool = False):
    """按做法检索：has=false 找'没做该做法'的论文，has=true 找'做了'的"""
    if not feat:
        raise HTTPException(status_code=400, detail="需要 feat 参数")
    ph = "select pid from paper_features where feat=?"
    where = []
    args = []
    where.append(("p.pid in (" if has else "p.pid not in (") + ph + ")")
    args.append(feat)
    if field:
        where.append("p.field = ?")
        args.append(field)
    sql = ("select p.pid, p.title, p.field, p.year from papers p where " + " and ".join(where)
           + " and p.quality_level in ('L1','L2') limit ?")
    args.append(max(1, min(int(limit), 50)))
    with _kb_conn() as c:
        rows = [dict(r) for r in c.execute(sql, args)]
    return {"ok": True, "feat": feat, "has": has, "field": field or "all",
            "returned": len(rows), "items": rows}
# ---- /KB FEATURES API END ----


# ---- SELFCHECK & PORTRAIT ----
_BASELINE2_PATH = "./paperlib/baseline2.json"


# ---- 双源交叉验证（2026-11-21，glm-5.3-flash 建议 2）----
# 设计原则：
#   1) 只新增交叉验证字段，不改任何旧字段语义；
#   2) 两个源必须能独立重算，结果里带 n / 一致率 / 分歧明细；
#   3) agreement < 0.7 明确降级为 low + warning，避免“单源结论被当成事实”。
_XC_HIGH = 0.95
_XC_MIN = 0.70
_XC_SAMPLE_N = 5000
_XC_BASELINE_TTL = 600
_XC_BASELINE_CACHE = {}
_XC_BASELINE_LOCK = threading.Lock()


def _xc_confidence(agreement):
    """一致率 → 置信度：>=0.95 high，>=0.70 medium，<0.70 low。"""
    try:
        a = float(agreement)
    except Exception:
        return "low"
    if a >= _XC_HIGH:
        return "high"
    if a >= _XC_MIN:
        return "medium"
    return "low"


def _xc_warning(agreement):
    """硬要求：agreement < 0.7 时给出明确 warning。"""
    try:
        if float(agreement) < _XC_MIN:
            return "双源不一致，结论不可靠"
    except Exception:
        pass
    return None


# source_b：与 features_extract_v2.py 的 FEATS **不同**的一套独立规则。
# 命名不用 llm_judge：本环境按 token 计费的 LLM 不适合放进每次自检请求；
# 任务允许“另一个提取器”，这里用另一套正则 + 章节标题作为独立裁定器。
_ALT_FEATS = {
    "seed": r"\b(?:random|fixed|initial|specified?)\s+seeds?\b|\bseeds?\s*(?:=|:)\s*\d|\bseed\s+(?:value|number|setting)",
    "ci": r"confidence intervals?|error bars?|standard deviations?|\bstd\.?\b|±\s*\d",
    "ablation": r"\bablat(?:e|ed|ing|ions?)\b",
    "code": r"github\.com|gitlab\.com|code (?:will be|is|are) (?:made )?(?:publicly )?available|publicly available (?:code|implementation)",
    "hyperparameter": r"hyper-?parameters?|learning rates?|batch sizes?|grid search|sensitivity analysis",
    "significance": r"statistical(?:ly)? significan|p-?values?|significance (?:test|level)|wilcoxon|t-test",
    "multiple_runs": r"(?:three|five|ten|\d+)\s+(?:independent\s+)?runs|repeated (?:the )?experiments?|averaged? (?:over|across)|mean\s*(?:and|±)\s*(?:standard deviation|std)",
    "limitations": r"\blimitations?\b|future (?:work|research|directions?)",
    "dataset_stats": r"(?:training|test|validation) set (?:contains|consists of|has)|(?:samples|instances|examples|images|documents)\b[^.]{0,50}\b\d[\d,]{2,}|\b\d[\d,]{2,}\s+(?:samples|instances|examples|images|documents)",
    "baseline_table": r"\bbaselines?\b|comparison (?:table|with)",
    "compute_budget": r"\b(?:GPU|TPU|CPU)[- ]hours?\b|compute (?:budget|resources?|cost)|trained? (?:for|on) \d+ (?:hours|days)|A100|V100|H100",
    "human_eval": r"human (?:evaluation|study|subjects|annotators?)|crowd(?:workers|sourcing)|IRB",
    "cross_val": r"cross-?validat|\d+-fold",
    "released_data": r"(?:publicly )?(?:release|released|available) (?:the )?(?:dataset|data|code)|dataset (?:is|are) (?:publicly )?available",
    "ethics": r"\bethics?\b|informed consent|privacy|IRB",
    "related_work_table": r"related work|prior work|comparison table",
    "sota_compare": r"state[- ]of[- ]the[- ]art|\bSOTA\b|best[- ]performing baseline|outperform(?:s|ed)? (?:all )?(?:baselines?|existing)",
    "novelty_statement": r"our (?:main )?contributions?|novel(?:ty)?|first (?:to|work)|we propose",
    "claim_hedging": r"\bmay\b|\bmight\b|\bcould\b|\blikely\b|\bpossibly\b|\bpotentially\b|\bsuggest",
    "threat_validity": r"threats? to validity|validity (?:threats?|concerns?)|generaliz(?:ability|ation) (?:of|to)",
}
_ALT_RE = {k: re.compile(v, re.I) for k, v in _ALT_FEATS.items()}


def _selfcheck_cross_check(rows, text, text_source):
    """selfcheck 双源：source_a=paper_features 提取器，source_b=独立规则裁定器。

    agreement = 两个源的 20 项 has 布尔值一致率；分歧逐项列出，便于人工复核。
    """
    b = {k: bool(rx.search(text or "")) for k, rx in _ALT_RE.items()}
    disagreements = []
    for r in rows or []:
        k = r.get("feat")
        a_v = bool(r.get("has"))
        b_v = bool(b.get(k, False))
        if a_v != b_v:
            disagreements.append({
                "feature": k, "a": a_v, "b": b_v,
                "note": "提取器说%s，独立规则裁定%s" % ("有" if a_v else "无",
                                                      "有" if b_v else "无"),
            })
    n = len(rows or [])
    agreement = round(1 - len(disagreements) / n, 4) if n else 1.0
    out = {
        "method": "two_source_agreement",
        "source_a": {"name": "feature_extractor", "n": n},
        "source_b": {"name": "rule_judge_v2", "n": n, "text_source": text_source,
                     "note": "另一套独立正则（与 features_extract_v2 的 FEATS 不同）"},
        "agreement": agreement,
        "disagreements": disagreements,
        "confidence": _xc_confidence(agreement),
        "note": "两个独立源的判断一致率；<0.7 表示该结论不可靠",
    }
    w = _xc_warning(agreement)
    if w:
        out["warning"] = w
    return out


def _baseline_cross_check(feature, venue_eff, field, n_a, k_a):
    """baseline 双源：source_a=SQLite 全量，source_b=FTS5 索引抽样。

    agreement = 1 - |rate_a - rate_b| / max(rate_a, rate_b)。
    抽样是确定性的（按 FTS rowid 取模），同样参数可复算；结果缓存 10 分钟。
    """
    feat = (feature or "seed").lower()
    try:
        n_a = int(n_a or 0)
        k_a = int(k_a or 0)
    except Exception:
        n_a = k_a = 0
    venue_eff = venue_eff or "all"
    field = field or ""
    key = (feat, venue_eff, field, n_a)
    now = time.time()
    with _XC_BASELINE_LOCK:
        hit = _XC_BASELINE_CACHE.get(key)
        if hit and (now - hit["ts"]) < _XC_BASELINE_TTL:
            return dict(hit["data"])

    src = _VENUE_SOURCE.get(venue_eff) if venue_eff else None
    conds = ["p.pid = f.pid",
             "exists(select 1 from paper_features x where x.pid = p.pid)"]
    cargs = []
    if src:
        conds.append("p.source = ?")
        cargs.append(src)
    if field:
        conds.append("p.field = ?")
        cargs.append(field)

    sample_n = min(_XC_SAMPLE_N, n_a) if n_a else _XC_SAMPLE_N
    sample_n = max(1, int(sample_n))
    stride = max(1, int(n_a // sample_n)) if n_a else 1
    n_b = k_b = 0
    err = ""
    try:
        with _kb_conn() as c:
            c.execute("ATTACH DATABASE 'file:%s?mode=ro' AS ftsdb" % _KB_FTS)
            sql = ("select count(*) as n, coalesce(sum(case when exists("
                   "select 1 from paper_features pf where pf.pid = p.pid and pf.feat = ?) "
                   "then 1 else 0 end), 0) as k from ("
                   "select p.pid from ftsdb.fts f join papers p on " + " and ".join(conds) +
                   " and (f.rowid % ?) = 0 limit ?) p")
            row = c.execute(sql, [feat] + cargs + [stride, sample_n]).fetchone()
            n_b, k_b = int(row[0] or 0), int(row[1] or 0)
    except Exception as e:
        err = "%s: %s" % (type(e).__name__, str(e)[:120])

    rate_a = round(k_a / n_a, 4) if n_a else 0.0
    rate_b = round(k_b / n_b, 4) if n_b else 0.0
    mx = max(rate_a, rate_b)
    agreement = round(1 - abs(rate_a - rate_b) / mx, 4) if mx > 0 else 1.0
    if n_b == 0:
        agreement = 0.0
    out = {
        "method": "sqlite_vs_fts",
        "feature": feat,
        "source_a": {"name": "sqlite_scan", "n": n_a, "papers": k_a,
                     "rate": rate_a, "seed_rate": rate_a},
        "source_b": {"name": "fts5_sample", "n": n_b, "papers": k_b,
                     "rate": rate_b, "seed_rate": rate_b,
                     "sample_stride": stride, "sample_target": sample_n},
        "agreement": agreement,
        "confidence": _xc_confidence(agreement),
        "note": ("SQLite 全量 vs FTS5 抽样；agreement = 1 - |rate_a-rate_b| / max(...)；"
                 ">0.95 表示口径一致" + ("；抽样失败：" + err if err else "")),
    }
    w = _xc_warning(agreement)
    if w:
        out["warning"] = w
    with _XC_BASELINE_LOCK:
        _XC_BASELINE_CACHE[key] = {"ts": now, "data": dict(out)}
    return out


def _search_sources(fts_n, fts_returned, core_n, core_returned,
                    a_ids, b_ids, note_extra=""):
    """search 双源：FTS5 全字段 vs FTS5 标题+摘要子索引。

    agreement = 两个候选 pid 集合的交集 / 并集（top-K 候选，K 在调用处定）。
    向量源当前环境未部署（模型/分片缺失），显式标 available=false，不伪造数字。
    """
    a, b = set(a_ids or []), set(b_ids or [])
    union = a | b
    agreement = round(len(a & b) / len(union), 4) if union else 1.0
    core_ok = bool(core_n) or bool(core_returned)
    out = {
        "method": "fts5_all_vs_title_abstract",
        "source_a": {"name": "fts5", "n": int(fts_n or 0),
                     "returned": int(fts_returned or 0)},
        "source_b": {"name": "fts5_title_abstract", "n": int(core_n or 0),
                     "returned": int(core_returned or 0)},
        "fts5": {"n": int(fts_n or 0), "returned": int(fts_returned or 0)},
        "vector": {"name": "vector", "available": False, "n": 0, "returned": 0,
                   "reason": "本环境未部署向量索引（models/bge-small-en 与 db/emb_*.npy 缺失）"},
        "agreement": agreement,
        "confidence": _xc_confidence(agreement),
        "candidate_k": len(union),
        "note": ("FTS5 全字段 vs 标题+摘要子索引；agreement = pid 交集 / 并集；"
                 "<0.7 表示结果可能不稳" + ("；" + note_extra if note_extra else "")),
    }
    if not fts_n and not core_n:
        # 两源都真的没有命中：空集一致，不是“单源不可用”，不能误报 warning。
        out["note"] = "两个源均无命中；agreement 记为 1.0（空集一致）" + (
            "；" + note_extra if note_extra else "")
        out["confidence"] = "high"
        return out
    if not core_ok:
        out["note"] = ("仅 FTS5 单源可用（标题+摘要子索引无命中/不可用），"
                       "未完成双源交叉验证" + ("；" + note_extra if note_extra else ""))
        out["warning"] = "仅单源可用，未完成双源交叉验证"
        out["confidence"] = "low"
        return out
    w = _xc_warning(agreement)
    if w:
        out["warning"] = w
    return out


def _search_sources_single(name, n, returned, note):
    """单源回退（FTS5 不可用 / 查询被闸门拦截）时也保持字段结构稳定。"""
    return {
        "method": "single_source",
        "source_a": {"name": name, "n": int(n or 0), "returned": int(returned or 0)},
        "source_b": None,
        "fts5": {"available": False, "n": 0, "returned": 0},
        "vector": {"name": "vector", "available": False, "n": 0, "returned": 0,
                   "reason": "本环境未部署向量索引"},
        "agreement": 1.0,
        "confidence": "low",
        "note": note,
        "warning": "仅单源可用，未完成双源交叉验证",
    }


@app.get("/api/kb/selfcheck")
def kb_selfcheck(pid: str = "", field: str = ""):
    """投稿前自检：这篇论文 vs 同领域分布

    `n_features` == `len(rows)`（自检清单总项数，2026-11-21 修正口径）；
    `n_features_present` 是清单中该论文已做到的项数；
    `n_features_detected` 是 paper_features 里检出的原始特征数（旧 n_features 语义）。
    """
    if not os.path.exists(_BASELINE2_PATH):
        raise HTTPException(status_code=503, detail="基线未生成")
    d = json.load(open(_BASELINE2_PATH, encoding="utf-8"))
    if not pid:
        raise HTTPException(status_code=400, detail="需要 pid")
    with _kb_conn() as c:
        row = c.execute("select pid,title,abstract,field,year,n_chars from papers where pid=?",
                        (pid,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="论文不存在")
        feats = {r[0] for r in c.execute("select feat from paper_features where pid=?", (pid,))}
        # 2026-11-21（Bug 3）：selfcheck 必须像 /api/evidence 一样先确认"有没有全文"。
        # 只读摘要时 20 项全 has:false 是假阳性，不是"这篇论文的问题"。
        ft_chars = int((c.execute("select coalesce(max(length(z)), 0) from fulltext_z "
                                  "where pid=?", (pid,)).fetchone()[0]) or 0)
        # 交叉验证用原文：优先 fulltext_z，缺失时用 title+abstract（并在 source_b 里标注）。
        zrow = c.execute("select z from fulltext_z where pid=? limit 1", (pid,)).fetchone()
    xc_text, xc_src = "", "title+abstract"
    if zrow and zrow[0]:
        try:
            import zlib as _zlib
            xc_text = _zlib.decompress(zrow[0]).decode("utf-8", "ignore")
            xc_src = "fulltext_z"
        except Exception:
            xc_text = ""
    if not xc_text:
        xc_text = ((row["title"] or "") + "\n" + (row["abstract"] or ""))
        xc_src = "title+abstract"
    n_chars = int(row["n_chars"] or 0)
    fulltext_chars = max(n_chars, ft_chars)
    has_fulltext = fulltext_chars >= 5000
    f = field or (row["field"] or "")
    slice_ = (d.get("by_field") or {}).get(f)
    rates = (slice_ or {}).get("rates") or d.get("recent_2025_2026") or {}
    scope = f"领域 {f}（{slice_['n']:,} 篇）" if slice_ else "近两年全库"
    # 问题 3：每一行都必须带分母 n，不能只给百分比。
    if slice_:
        denom_n = int(slice_.get("n") or 0)
        denom_desc = "papers in field %s with extracted features" % f
    else:
        denom_n = int(d.get("n_recent") or d.get("n_papers") or 0)
        denom_desc = "papers in recent window (2025-2026) with extracted features"
    rows = []
    for k, v in (rates or {}).items():
        rows.append({"feat": k, "has": k in feats, "field_pct": v["pct"],
                     "papers": int(v.get("n") or 0), "n": denom_n,
                     "rate": round(v["pct"] / 100.0, 4)})
    rows.sort(key=lambda x: (-x["field_pct"], x["feat"]))
    missing = [x["feat"] for x in rows if not x["has"] and x["field_pct"] >= 40]
    # 2026-11-21（Bug 1）：`n_features` 必须等于 `rows` 的条数。
    # 原语义是「这篇论文命中的特征数」（len(feats)），但 rows 是 20 项自检清单，
    # 两者口径不同 → 用户看到 n_features=10、rows=20 会以为数据不一致。
    # 新语义：n_features = 清单总项数 = len(rows)；命中数另给 n_features_present，
    # 原始特征数保留在 n_features_detected（只新增字段，不改旧字段名）。
    n_total = len(rows)
    n_present = sum(1 for x in rows if x["has"])
    out = {"ok": True, "pid": pid, "title": row["title"], "field": f, "year": row["year"],
           "scope": scope, "n_features": n_total, "rows": rows,
           "n_features_total": n_total, "n_features_present": n_present,
           "n_features_detected": len(feats),
           "suggestions": missing[:6],
           # 新增（不改旧字段）：全文状态。无全文时自检结果不可靠，必须显式告知。
           "has_fulltext": has_fulltext, "n_chars": n_chars,
           "fulltext_chars": fulltext_chars, "reliable": has_fulltext}
    # 问题 2（glm-fix4）：这篇论文的年份相对库截止日是否还算“新”。
    # is_recent 口径：paper_year >= cutoff 年份 - 1（默认看近两年），不是“今天减一年”。
    try:
        _cov = _coverage_info()
        _py = int(row["year"] or 0)
        _cy = int((_cov.get("cutoff") or "0")[:4] or 0)
        out["data_freshness"] = {
            "paper_year": _py or None,
            "cutoff": _cov.get("cutoff"),
            "newest_paper": _cov.get("newest_paper"),
            "update_freq": _cov.get("update_freq"),
            "is_recent": bool(_py and _cy and _py >= _cy - 1),
            "rule": "paper_year >= cutoff 年份 - 1",
        }
    except Exception as _e:
        out["data_freshness"] = {"error": repr(_e)}
    if not has_fulltext:
        out["warning"] = ("这篇只有摘要（%d 字符），自检结果不可靠："
                          "缺全文时 20 项特征无法判定，不代表论文真的缺这些项" % n_chars)
        out["hint"] = "请先用 /api/evidence 或 /api/paper/{pid}/text 确认全文是否已入库"
    # 口径敏感性（sensitivity，2026-11-21）：selfcheck 的 field_pct 在不同口径下
    # 会变（cs.CL seed：全库 19.1% vs 顶会 16.2%），必须随结果回显；
    # 只新增字段，不改 rows/suggestions/n_features 旧语义。
    if _sensitivity is not None:
        try:
            _sens_scopes, _sens, _sens_rec = _sensitivity.for_selfcheck(f, rows)
            out["sensitivity"] = _sens
            out["recommended_scope"] = _sens_rec
            if _sens.get("conclusion_robust") is False:
                _sens_warn = _sens.get("warning")
                if "warning" in out:
                    # 已有 warning（如无全文）时不能覆盖，另给 sensitivity_warning。
                    out["sensitivity_warning"] = _sens_warn
                else:
                    out["warning"] = _sens_warn
        except Exception as _e:
            out["sensitivity"] = {"error": repr(_e)}
            print("[selfcheck] sensitivity 计算失败：%r" % (_e,), flush=True)
    # 双源交叉验证：只新增字段，不改 rows/suggestions/n_features 旧语义。
    out["cross_check"] = _selfcheck_cross_check(rows, xc_text, xc_src)
    # 问题 1：call_id / computed_at / version / denominator / numerator，可复算。
    _params = {"pid": pid, "field": field or None}
    out.update(_call_meta("/api/kb/selfcheck", _params,
                          {"n": denom_n, "description": denom_desc},
                          {"n": n_present, "description": "checklist items the paper satisfies"},
                          metric="selfcheck_present_rate",
                          scope=("field:" + f) if slice_ else "recent_all"))
    _save_call(out.get("call_id"), out)
    return out


@app.get("/kb/portrait", response_class=HTMLResponse)
def kb_portrait():
    p = os.path.join(V15_ROOT, "app", "templates", "kb_portrait.html")
    if os.path.exists(p):
        return FileResponse(p)
    return HTMLResponse("<h1>领域画像未部署</h1>", status_code=404)


@app.get("/kb/selfcheck", response_class=HTMLResponse)
def kb_selfcheck_page():
    p = os.path.join(V15_ROOT, "app", "templates", "kb_selfcheck.html")
    if os.path.exists(p):
        return FileResponse(p)
    return HTMLResponse("<h1>自检页未部署</h1>", status_code=404)
# ---- /SELFCHECK & PORTRAIT END ----


# ---- MISSING CITES ----
_KB_FTS_PATH = os.environ.get("QZ_KB_FTS", "./paperlib/db/paperlib_fts.sqlite")
_STOP = set("a an the of for and to in on with via using towards toward from by is are we our this that these "
            "those study analysis approach method framework model models learning deep neural network networks".split())


def _norm_ref(t: str) -> str:
    t = re.sub(r"[^a-z0-9 ]+", " ", (t or "").lower())
    return re.sub(r"\s+", " ", t).strip()


_ref_index_cache = {"at": 0, "idx": None}


def _ref_index():
    """ref_title_norm -> [pid]；10 分钟缓存"""
    import time as _t
    now = _t.time()
    c = _ref_index_cache
    if c["idx"] is not None and now - c["at"] < 600:
        return c["idx"]
    import sqlite3 as _sq
    import re as _re
    idx = {}
    try:
        conn = _sq.connect("file:" + _KB_PATH + "?mode=ro", uri=True, timeout=20)
        for pid, title in conn.execute("select pid, ref_title from paper_refs"):
            t = _re.sub(r"[^a-z0-9 ]+", " ", (title or "").lower())
            t = _re.sub(r"\s+", " ", t).strip()
            if len(t) < 10:
                continue
            idx.setdefault(t, []).append(pid)
        conn.close()
    except Exception:
        pass
    c["idx"], c["at"] = idx, now
    return idx


@app.get("/api/kb/missing_cites")
def kb_missing_cites(pid: str = "", top: int = 10, siblings: int = 30):
    """漏引检测：目标没引、但同领域多篇兄弟论文都引了的文献"""
    import sqlite3 as _sq
    if not pid:
        raise HTTPException(status_code=400, detail="需要 pid")
    with _kb_conn() as c:
        row = c.execute("select pid,title,field,year from papers where pid=?", (pid,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="论文不存在")
        mine = {_norm_ref(r[0]) for r in c.execute("select ref_title from paper_refs where pid=?", (pid,)) if r[0]}
        words = [w for w in re.findall(r"[a-z]{4,}", (row["title"] or "").lower()) if w not in _STOP][:5]
        if not words:
            return {"ok": True, "pid": pid, "siblings": 0, "candidates": [], "reason": "标题关键词不足"}
        if not os.path.exists(_KB_FTS_PATH):
            return {"ok": True, "pid": pid, "siblings": 0, "candidates": [], "reason": "FTS 未就绪"}
        # 兄弟论文：优先「共引」——与目标共享参考文献最多的论文
        from collections import Counter as _C
        idx = _ref_index()
        co = _C()
        for t in mine:
            for pid2 in idx.get(t, []):
                if pid2 != pid:
                    co[pid2] += 1
        ids = [p for p, n in co.most_common(max(30, siblings)) if n >= 2]
        if len(ids) < 10:
            ids += [r[0] for r in c.execute(
                "select p.pid from papers p where p.field=? and p.dup_of is null "
                "and p.pid in (select pid from paper_refs group by pid) "
                "order by coalesce(p.year,0) desc limit ?", (row["field"], max(30, siblings * 3)))]
        if len(ids) < 10:
            try:
                f = _sq.connect("file:%s?mode=ro" % _KB_FTS_PATH, uri=True, timeout=8)
                match = " OR ".join('"%s"' % w for w in words)
                ids += [r[0] for r in f.execute(
                    "select pid from fts where fts match ? order by rank limit ?",
                    (match, max(30, siblings * 3)))]
                f.close()
            except Exception:
                pass
        sib, cnt, title_of = [], {}, {}
        for sid in ids:
            if sid == pid:
                continue
            r = c.execute("select pid,field from papers where pid=?", (sid,)).fetchone()
            if not r:
                continue
            if row["field"] and r["field"] and r["field"] != row["field"]:
                continue
            refs = [x[0] for x in c.execute("select ref_title from paper_refs where pid=?", (sid,)) if x[0]]
            if not refs:
                continue
            sib.append(sid)
            seen = set()
            for t in refs:
                nt = _norm_ref(t)
                if not nt or nt in mine or nt in seen:
                    continue
                seen.add(nt)
                cnt[nt] = cnt.get(nt, 0) + 1
                title_of.setdefault(nt, t)
            if len(sib) >= siblings:
                break
    thr = max(3, int(len(sib) * 0.2)) if sib else 3
    cands = [{"ref_title": title_of[t], "cited_by": n, "ratio": round(n / max(len(sib), 1) * 100, 1)}
             for t, n in sorted(cnt.items(), key=lambda x: -x[1]) if n >= thr][:max(1, min(top, 50))]
    return {"ok": True, "pid": pid, "title": row["title"], "field": row["field"],
            "n_refs": len(mine), "siblings": len(sib), "threshold": thr,
            "candidates": cands,
            "note": "兄弟论文 = 同领域 + 标题关键词相近 + 有参考文献；候选 = 目标未引但多篇兄弟引用"}


@app.get("/kb/missing", response_class=HTMLResponse)
def kb_missing_page():
    p = os.path.join(V15_ROOT, "app", "templates", "kb_missing.html")
    if os.path.exists(p):
        return FileResponse(p)
    return HTMLResponse("<h1>漏引检测页未部署</h1>", status_code=404)
# ---- /MISSING CITES END ----


# ---- PACKAGE CHECK (论文包) ----
import sys as _sys
if "/home/ubuntu/paperlib" not in _sys.path:
    _sys.path.insert(0, "/home/ubuntu/paperlib")

_PKG_ROOT = os.environ.get("QZ_PKG_ROOT", "./paperlib/pkg_out")


@app.post("/api/pkg/analyze")
async def pkg_analyze(request: Request):
    from fastapi import UploadFile as _UF
    form = await request.form()
    file = form.get("file")
    if file is None:
        raise HTTPException(status_code=400, detail="缺少 file 字段")
    """上传论文包（zip：论文+代码+数据+文档）→ 包级检查报告"""
    import tempfile
    name = (file.filename or "package.zip").lower()
    if not name.endswith(".zip"):
        raise HTTPException(status_code=400, detail="请上传 .zip 包")
    os.makedirs(_PKG_ROOT, exist_ok=True)
    tmp = os.path.join(_PKG_ROOT, "upload_" + time.strftime("%Y%m%d_%H%M%S") + ".zip")
    with open(tmp, "wb") as f:
        f.write(await file.read())
    try:
        from base.pkg_service import analyze_zip  # noqa
    except Exception as e:
        raise HTTPException(status_code=503, detail="包分析模块导入失败：%s: %s" % (type(e).__name__, e))
    info = analyze_zip(tmp, _PKG_ROOT)
    info.pop("report_html", None)
    return {"ok": True, **info}


@app.get("/api/pkg/{job}")
def pkg_info(job: str):
    job = _safe_job(job)
    p = os.path.join(_PKG_ROOT, job, "pkg_info.json")
    if not os.path.exists(p):
        raise HTTPException(status_code=404, detail="任务不存在")
    return json.load(open(p, encoding="utf-8"))


@app.get("/pkg/{job}", response_class=HTMLResponse)
def pkg_view(job: str):
    job = _safe_job(job)
    p = os.path.join(_PKG_ROOT, job, "short_report.md")
    if not os.path.exists(p):
        raise HTTPException(status_code=404, detail="报告不存在")
    md = open(p, encoding="utf-8").read()
    try:
        from base.pkg_service import _md_to_html
        body = _md_to_html(md)
    except Exception:
        body = "<pre>" + md.replace("<", "&lt;") + "</pre>"
    html = ("<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>论文包检查 · 群智</title><link rel='stylesheet' href='/static/qz.css'>"
            "<style>body{background:#FAF6F0}table{border-collapse:collapse;width:100%;font-size:13px}"
            "td,th{border-bottom:1px solid #EAE3D8;padding:6px 8px;text-align:left}"
            "code{background:#F5F0E8;padding:1px 5px;border-radius:4px}</style></head><body>"
            "<div class='qz-top'><div class='qz-title'><span class='qz-logo'>群智</span> · 论文包检查</div>"
            "<div><a class='qz-link' href='/pkg'>换一个包</a></div></div>"
            "<div class='qz-wrap'>" + body + "</div><script src='/static/qz-nav.js'></script></body></html>")
    return HTMLResponse(html)


# ---- PKG DEEP REVIEW ----
@app.post("/api/pkg/{job}/review")
def pkg_deep_review(job: str, max_items: int = 9):
    """对已上传的论文包做深度审查（LLM 判定每个疑点是否成立）"""
    job = _safe_job(job)
    work = os.path.join(_PKG_ROOT, job)
    if not os.path.isdir(work):
        raise HTTPException(status_code=404, detail="任务不存在")
    src = None
    for d in os.listdir(work):
        if d.startswith("_src_") and os.path.isdir(os.path.join(work, d)):
            src = os.path.join(work, d)
            break
    if src is None:
        # 兼容：包内若只有一个子目录，就取它
        subs = [d for d in os.listdir(work) if os.path.isdir(os.path.join(work, d))
                and not d.startswith("focus_state")]
        src = os.path.join(work, subs[0]) if subs else work
    import subprocess
    r = subprocess.run(["/home/ubuntu/qunzhi/venv/bin/python",
                        "./paperlib/base/pkg_deep.py",
                        "--job", work, "--root", src, "--max", str(max(1, min(max_items, 20)))],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=600)
    if r.returncode != 0:
        raise HTTPException(status_code=500, detail=(r.stderr or r.stdout or "")[-300:])
    jp = os.path.join(work, "pkg_review.json")
    data = json.load(open(jp, encoding="utf-8")) if os.path.exists(jp) else {}
    return {"ok": True, "job": job, "counts": data.get("counts", {}),
            "url": "/pkg/%s/review" % job}


@app.get("/pkg/{job}/review", response_class=HTMLResponse)
def pkg_deep_page(job: str):
    job = _safe_job(job)
    p = os.path.join(_PKG_ROOT, job, "pkg_review.md")
    if not os.path.exists(p):
        raise HTTPException(status_code=404, detail="深度审查报告不存在，先 POST /api/pkg/{job}/review")
    md = open(p, encoding="utf-8").read()
    try:
        from base.pkg_service import _md_to_html
        body = _md_to_html(md)
    except Exception:
        body = "<pre>" + md.replace("<", "&lt;") + "</pre>"
    html = ("<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>论文包深度审查 · 群智</title><link rel='stylesheet' href='/static/qz.css'>"
            "<style>body{background:#FAF6F0}table{border-collapse:collapse;width:100%;font-size:13px}"
            "td,th{border-bottom:1px solid #EAE3D8;padding:6px 8px;text-align:left}</style></head><body>"
            "<div class='qz-top'><div class='qz-title'><span class='qz-logo'>群智</span> · 论文包深度审查</div>"
            "<div><a class='qz-link' href='/pkg'>换一个包</a></div></div>"
            "<div class='qz-wrap'>" + body + "</div><script src='/static/qz-nav.js'></script></body></html>")
    return HTMLResponse(html)
# ---- /PKG DEEP REVIEW END ----


# ---- PKG NUMBERS (数字对账) ----
def _pkg_src_dir(work: str) -> str:
    for d in os.listdir(work):
        if d.startswith("_src_") and os.path.isdir(os.path.join(work, d)):
            return os.path.join(work, d)
    subs = [d for d in os.listdir(work) if os.path.isdir(os.path.join(work, d))]
    return os.path.join(work, subs[0]) if subs else work


@app.post("/api/pkg/{job}/numbers")
def pkg_numbers(job: str):
    """跨文件数字对账：找同一指标的不同数值（LLM 归并 + 证据）"""
    job = _safe_job(job)
    work = os.path.join(_PKG_ROOT, job)
    if not os.path.isdir(work):
        raise HTTPException(status_code=404, detail="任务不存在")
    src = _pkg_src_dir(work)
    import subprocess
    r = subprocess.run(["/home/ubuntu/qunzhi/venv/bin/python",
                        "./paperlib/base/pkg_numbers_llm.py",
                        "--root", src, "--outdir", work],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=600)
    if r.returncode != 0:
        raise HTTPException(status_code=500, detail=(r.stderr or r.stdout or "")[-300:])
    jp = os.path.join(work, "number_conflicts.json")
    data = json.load(open(jp, encoding="utf-8")) if os.path.exists(jp) else {}
    cs = data.get("conflicts") or []
    real = [c for c in cs if str(c.get("status", "")).startswith("conflict")]
    return {"ok": True, "job": job, "total": len(cs), "conflict": len(real),
            "url": "/pkg/%s/numbers" % job,
            "items": [{"metric": c.get("metric"), "status": c.get("status"),
                       "values": [v.get("value") for v in c.get("values", [])]} for c in cs[:10]]}


@app.get("/pkg/{job}/numbers", response_class=HTMLResponse)
def pkg_numbers_page(job: str):
    job = _safe_job(job)
    p = os.path.join(_PKG_ROOT, job, "number_conflicts.md")
    if not os.path.exists(p):
        raise HTTPException(status_code=404, detail="报告不存在，先 POST /api/pkg/{job}/numbers")
    md = open(p, encoding="utf-8").read()
    try:
        from base.pkg_service import _md_to_html
        body = _md_to_html(md)
    except Exception:
        body = "<pre>" + md.replace("<", "&lt;") + "</pre>"
    html = ("<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>论文包数字对账 · 群智</title><link rel='stylesheet' href='/static/qz.css'>"
            "<style>body{background:#FAF6F0}table{border-collapse:collapse;width:100%;font-size:12.5px}"
            "td,th{border-bottom:1px solid #EAE3D8;padding:6px 8px;text-align:left;vertical-align:top}</style></head><body>"
            "<div class='qz-top'><div class='qz-title'><span class='qz-logo'>群智</span> · 论文包数字对账</div>"
            "<div><a class='qz-link' href='/pkg'>换一个包</a></div></div>"
            "<div class='qz-wrap'>" + body + "</div><script src='/static/qz-nav.js'></script></body></html>")
    return HTMLResponse(html)
# ---- /PKG NUMBERS END ----


@app.get("/pkg", response_class=HTMLResponse)
def pkg_page():
    p = os.path.join(V15_ROOT, "app", "templates", "pkg.html")
    if os.path.exists(p):
        return FileResponse(p)
    return HTMLResponse("<h1>论文包检查</h1><p>上传 zip：POST /api/pkg/analyze</p>")
# ---- /PACKAGE CHECK END ----


@app.get("/kb/insights", response_class=HTMLResponse)
def kb_insights_page():
    p = os.path.join(V15_ROOT, "app", "templates", "kb_insights.html")
    if os.path.exists(p):
        return FileResponse(p)
    return HTMLResponse("<h1>库洞察</h1>", status_code=200)
# ---- /KB INSIGHTS END ----


# ---- WEEKLY REPORT (auto-inserted) ----
_WEEKLY_DIR = "./paperlib/weekly"


# ---- PERSONAL PAGE (auto-inserted) ----
@app.get("/me", response_class=HTMLResponse)
def me_page():
    """公开学术身份页（姓名+邮箱+单位），用于平台 profile 校验"""
    p = os.path.join(V15_ROOT, "app", "templates", "me.html")
    if os.path.exists(p):
        return FileResponse(p)
    return HTMLResponse("<h1>个人主页未部署</h1>", status_code=404)
# ---- /PERSONAL PAGE END ----


@app.get("/weekly", response_class=HTMLResponse)
def weekly_page():
    """AI 周报（每周一自动更新）"""
    p = os.path.join(_WEEKLY_DIR, "weekly_latest.html")
    if os.path.exists(p):
        return FileResponse(p)
    return HTMLResponse("<h1>周报尚未生成</h1><p>跑：python weekly.py</p>", status_code=200)


# ---- AI SHOCK PAGE (auto-inserted) ----
@app.get("/weekly/ai-shock", response_class=HTMLResponse)
def ai_shock_page():
    p = os.path.join(_WEEKLY_DIR, "ai_shock.html")
    if os.path.exists(p):
        return FileResponse(p)
    return HTMLResponse("<h1>专题未生成</h1><p>跑 ai_shock.py</p>", status_code=200)


@app.get("/api/ai-shock")
def ai_shock_json():
    p = os.path.join(_WEEKLY_DIR, "ai_shock.json")
    if not os.path.exists(p):
        raise HTTPException(status_code=503, detail="专题未生成")
    return json.load(open(p, encoding="utf-8"))
# ---- /AI SHOCK END ----


@app.get("/api/weekly")
def weekly_json():
    p = os.path.join(_WEEKLY_DIR, "weekly_latest.json")
    if not os.path.exists(p):
        raise HTTPException(status_code=503, detail="周报未生成")
    return json.load(open(p, encoding="utf-8"))
# ---- /WEEKLY END ----


@app.get("/kb", response_class=HTMLResponse)
def kb_page():
    p = os.path.join(V15_ROOT, "app", "templates", "kb.html")
    if os.path.exists(p):
        return FileResponse(p)
    return HTMLResponse("<h1>论文库</h1>", status_code=200)
# ---- /KB API END ----


@app.get("/workbench", response_class=HTMLResponse)
def workbench_page():
    """群智工作台（统一前端壳：产品线导航 + 论文包检查 + 嵌旧页）"""
    p = os.path.join(V15_ROOT, "app", "templates", "workbench.html")
    if os.path.exists(p):
        return FileResponse(p)
    return HTMLResponse("<h1>工作台未部署</h1>", status_code=404)


@app.get("/api/wb/stats")
def wb_stats():
    """工作台实时数据（缓存 60 秒；响应带 call_id，可回放）"""
    import sqlite3 as _sq
    now = time.time()
    cached = getattr(wb_stats, "_cache", None)
    if cached and now - cached[0] < 60:
        base = dict(cached[1])
    else:
        out = {"ok": True, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        try:
            c = _sq.connect("file:" + _KB_PATH + "?mode=ro", uri=True, timeout=8)
            q = lambda sql: c.execute(sql).fetchone()[0]
            out["papers"] = q("select count(*) from papers")
            out["fulltext"] = q("select count(*) from fulltext_z")
            out["features"] = q("select count(*) from paper_features")
            out["refs"] = q("select count(*) from paper_refs")
            out["year_2025"] = q("select count(*) from papers where year>=2025")
            try:
                out["or_reviews"] = q("select count(*) from or_reviews")
            except Exception:
                out["or_reviews"] = 0
            c.close()
        except Exception as e:
            out["error"] = "%s: %s" % (type(e).__name__, e)
        wb_stats._cache = (now, out)
        base = dict(out)
    # 2026-11-22 前端 BUG-005：工作台数字也带 call_id（缓存内容不变，元数据每次刷新）。
    base.update(_call_meta("/api/wb/stats", {},
                           {"description": "papers/fulltext/features/refs counts"},
                           {"papers": base.get("papers"),
                            "fulltext": base.get("fulltext")},
                           metric="library_size", scope="all"))
    _save_call(base.get("call_id"), base)
    return base


@app.get("/api/lib/status")
def lib_status():
    """云端长任务状态（watchdog 每 5 分钟写 jobs_status.json）"""
    p = "./paperlib/jobs_status.json"
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception as e:
            return {"ok": False, "error": str(e)[:120]}
    return {"ok": False, "error": "状态文件不存在（watchdog 未运行）"}



# ---- NIGHT ADD-ONS (additive only) ----
@app.get("/api/lib/health")
def lib_health_fast():
    """库健康（读 watchdog 写的状态文件，毫秒级）"""
    p = "./paperlib/jobs_status.json"
    if os.path.exists(p):
        try:
            d = json.load(open(p, encoding="utf-8"))
            d["ok"] = True
            d["cached"] = True
            return d
        except Exception:
            pass
    return {"ok": False, "error": "状态文件不可用"}


@app.get("/api/lib/health_live")
def lib_health(probe: int = 0):
    """库健康：分年覆盖率 / 重复 / 全文缺口（只读）。

    probe=1 只做一次 select 1 探活，供全站顶部「服务在线」每 30 秒轮询，
    避免每 30 秒跑一遍 4 年覆盖率聚合查询。
    """
    import sqlite3 as _sq
    out = {"ok": True}
    if probe:
        try:
            c = _sq.connect("file:" + _KB_PATH + "?mode=ro", uri=True, timeout=5)
            c.execute("select 1").fetchone()
            c.close()
            out["live"] = True
            out["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        except Exception as e:
            out["ok"] = False
            out["error"] = "%s: %s" % (type(e).__name__, str(e)[:120])
        return out
    try:
        c = _sq.connect("file:" + _KB_PATH + "?mode=ro", uri=True, timeout=20)
        q = lambda s: c.execute(s).fetchone()[0]
        out["papers"] = q("select count(*) from papers")
        out["unique_papers"] = q("select count(*) from papers where dup_of is null")
        out["fulltext"] = q("select count(*) from fulltext_z")
        out["features"] = q("select count(*) from paper_features")
        out["issues"] = q("select count(*) from issues")
        cov = []
        for y in (2026, 2025, 2024, 2023):
            n = q("select count(*) from papers where year=%d" % y)
            ft = q("select count(*) from papers p join (select distinct pid from fulltext_z) f on f.pid=p.pid where p.year=%d" % y)
            cov.append({"year": y, "papers": n, "fulltext": ft,
                        "rate": round(ft / n * 100, 1) if n else None})
        out["coverage_by_year"] = cov
        out["dup_marked"] = q("select count(*) from papers where dup_of is not null")
        c.close()
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, str(e)[:120])
    return out


@app.post("/api/pkg/{job}/review2")
def pkg_deep2_review(job: str, max_items: int = 9):
    """论文包审查（方法学加固版：每条结论带成立条件与边界）"""
    job = _safe_job(job)
    work = os.path.join(_PKG_ROOT, job)
    if not os.path.isdir(work):
        raise HTTPException(status_code=404, detail="任务不存在")
    src = _pkg_src_dir(work)
    import subprocess
    r = subprocess.run(["/home/ubuntu/qunzhi/venv/bin/python",
                        "./paperlib/base/pkg_deep2.py",
                        "--job", work, "--root", src, "--max", str(max(1, min(max_items, 20)))],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=600)
    if r.returncode != 0:
        raise HTTPException(status_code=500, detail=(r.stderr or r.stdout or "")[-300:])
    jp = os.path.join(work, "pkg_review2.json")
    data = json.load(open(jp, encoding="utf-8")) if os.path.exists(jp) else {}
    return {"ok": True, "job": job, "counts": data.get("counts", {}),
            "url": "/pkg/%s/review2" % job}


@app.get("/pkg/{job}/review2", response_class=HTMLResponse)
def pkg_deep2_page(job: str):
    job = _safe_job(job)
    p = os.path.join(_PKG_ROOT, job, "pkg_review2.md")
    if not os.path.exists(p):
        raise HTTPException(status_code=404, detail="报告不存在，先 POST /api/pkg/{job}/review2")
    md = open(p, encoding="utf-8").read()
    try:
        from base.pkg_service import _md_to_html
        body = _md_to_html(md)
    except Exception:
        body = "<pre>" + md.replace("<", "&lt;") + "</pre>"
    html = ("<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>论文包审查（加固版）· 群智</title><link rel='stylesheet' href='/static/wb.css'></head><body>"
            "<div class='page'>" + body + "</div><script src='/static/qz-nav.js'></script></body></html>")
    return HTMLResponse(html)
# ---- /NIGHT ADD-ONS ----


# ---- 缺陷基线：venue 过滤（2026-11-21 修 Bug 2）----
# 根因：原实现只有 overall 受 venue 过滤，by_field 永远是全库数；
#       cs.CL 的 10,355（全库）> overall 的 9,867（顶会）→ 子集大于全集。
# 修法：by_field 用与 overall 同一批论文现算（分母=该领域内该层论文数），
#       并给 by_field 加 `_filtered_by` 说明口径。
_VENUE_SOURCE = {"top": "venue-crawl", "peer": "deepreview", "arxiv": "arxiv-api"}
_VENUE_CACHE = {}
_VENUE_CACHE_LOCK = threading.Lock()
_VENUE_CACHE_TTL = 600          # 秒；数据静态，10 分钟足够，且 DB 增量后不会长期错


def _baseline_verdict(rate: float) -> str:
    if rate >= 0.80:
        return "universal"
    if rate >= 0.50:
        return "majority"
    return "minority"


def _venue_stats(venue: str, feature: str):
    """返回 (by_field, overall)，二者口径完全一致。

    venue: top / peer / arxiv / all（all 或未知 venue 不加 source 过滤）
    feature: paper_features.feat
    """
    vkey = venue or "all"
    key = ("by_field", vkey, feature)
    now = time.time()
    with _VENUE_CACHE_LOCK:
        hit = _VENUE_CACHE.get(key)
        if hit and (now - hit["ts"]) < _VENUE_CACHE_TTL:
            return dict(hit["data"]), dict(hit["overall"])

    src = _VENUE_SOURCE.get(venue)
    cond, args = "", [feature]
    if src:
        cond = " and p.source = ?"
        args.append(src)
    sql = ("select p.field as field, count(*) as n, "
           "sum(case when pf.pid is not null then 1 else 0 end) as k "
           "from papers p "
           "left join paper_features pf on pf.pid = p.pid and pf.feat = ? "
           "where p.field is not null and p.field <> '' "
           "and exists(select 1 from paper_features x where x.pid = p.pid)"
           + cond + " group by p.field")
    by_field = {}
    with _kb_conn() as c:
        for r in c.execute(sql, args):
            n, k = int(r["n"] or 0), int(r["k"] or 0)
            if not n:
                continue
            rate = round(k / n, 4)
            by_field[r["field"]] = {"papers": k, "n": n, "rate": rate,
                                     "verdict": _baseline_verdict(rate)}
        # overall 必须包含"没有 field 标签"的论文（如 peer 层的评审论文），
        # 否则会像修 Bug 2 前那样与分层口径打架。by_field 只按有 field 的论文分组，
        # 因此 by_field 的 n 之和 ≤ overall.n 是正常的（差额=无 field 论文）。
        osql = ("select count(*) as n, "
                "sum(case when pf.pid is not null then 1 else 0 end) as k "
                "from papers p "
                "left join paper_features pf on pf.pid = p.pid and pf.feat = ? "
                "where exists(select 1 from paper_features x where x.pid = p.pid)"
                + cond)
        r = c.execute(osql, args).fetchone()
        total_n, total_k = int(r["n"] or 0), int(r["k"] or 0)
    overall = {"papers": total_k, "n": total_n,
               "rate": round(total_k / total_n, 4) if total_n else None,
               "verdict": _baseline_verdict(total_k / total_n) if total_n else None}
    with _VENUE_CACHE_LOCK:
        _VENUE_CACHE[key] = {"ts": now, "data": by_field, "overall": overall}
    return dict(by_field), dict(overall)


def _venue_field_features(venue: str, field: str):
    """某个领域 + venue 分层下的 20 项出现率（用于 ?field=&venue= 交叉查询）。"""
    vkey = venue or "all"
    key = ("field_features", vkey, field)
    now = time.time()
    with _VENUE_CACHE_LOCK:
        hit = _VENUE_CACHE.get(key)
        if hit and (now - hit["ts"]) < _VENUE_CACHE_TTL:
            return dict(hit)
    src = _VENUE_SOURCE.get(venue)
    cond, args = "", [field]
    if src:
        cond = " and p.source = ?"
        args.append(src)
    base = ("from papers p where p.field = ? "
            "and exists(select 1 from paper_features x where x.pid = p.pid)" + cond)
    out = {"n": 0, "features": {}}
    with _kb_conn() as c:
        out["n"] = int((c.execute("select count(*) " + base, args).fetchone()[0]) or 0)
        for r in c.execute("select pf.feat as feat, count(*) as k "
                           "from papers p join paper_features pf on pf.pid = p.pid "
                           "where p.field = ? "
                           "and exists(select 1 from paper_features x where x.pid = p.pid)"
                           + cond + " group by pf.feat", args):
            k = int(r["k"] or 0)
            rate = round(k / out["n"], 4) if out["n"] else 0.0
            out["features"][r["feat"]] = {"papers": k, "rate": rate,
                                           "verdict": _baseline_verdict(rate)}
    with _VENUE_CACHE_LOCK:
        _VENUE_CACHE[key] = {"ts": now, "n": out["n"],
                             "features": dict(out["features"])}
    return out


@app.get("/api/metrics/{name}/sensitivity")
def metric_sensitivity(name: str, feature: str = "", field: str = ""):
    """任意指标的口径敏感性（all / top / arxiv / peer）。

    - metric 名：seed_disclosure_rate、feature_occurrence_rate、field_pct，
      或任意 paper_features.feat（如 code / ablation）。
    - ?feature= 指定特征（默认 seed）；?field= 指定领域（field_pct 必填）。
    - 数字全部按 SQLite 现算：每个口径的 n / 分子 / rate 都回显，可复算。
    """
    if _sensitivity is None:
        raise HTTPException(status_code=503,
                            detail="sensitivity 模块未加载，无法计算口径敏感性")
    fn = (name or "").strip()
    try:
        d = _sensitivity.for_metric(fn, feature=feature or None, field=field or None)
    except KeyError as e:
        if "field_required" in str(e):
            raise HTTPException(status_code=400,
                                detail="metric=%s 需要 ?field= 参数（如 field=cs.CL）" % fn)
        raise HTTPException(status_code=404, detail={
            "error": "unknown_metric",
            "metric": fn,
            "available_metrics": (["seed_disclosure_rate", "feature_occurrence_rate",
                                   "field_pct"] + (_sensitivity.known_features(20))),
            "hint": "也可直接传 paper_features.feat（如 code / ablation / seed）",
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail="敏感性计算失败：%r" % (e,))

    scopes = d.get("scopes_by_name") or {}
    _all = scopes.get("all") or {}
    d["ok"] = True
    d.update(_call_meta(
        "/api/metrics/%s/sensitivity" % fn,
        {"name": fn, "feature": feature or None, "field": field or None},
        {"n": int(_all.get("n") or 0),
         "description": "papers under scope=all for metric %s" % d.get("metric")},
        {"n": int(_all.get("papers") or 0),
         "description": "papers where metric %s numerator holds (scope=all)" % d.get("metric")},
        metric=d.get("metric"), scope="all"))
    if (d.get("sensitivity") or {}).get("conclusion_robust") is False:
        d["warning"] = (d.get("sensitivity") or {}).get("warning")
    try:
        _save_call(d.get("call_id"), d)
    except Exception as _e:
        print("[metrics] call 落盘失败：%r" % (_e,), flush=True)
    return d


@app.get("/api/kb/defect_baseline")
def defect_baseline(field: str = "", feature: str = "", venue: str = ""):
    """缺陷-同行基线：某缺陷在库内的出现率（按 venue 分层）

    venue 可选：top（顶会）/ arxiv（预印本）/ peer（有评审）/ all（全部）。
    **默认值固定为 all（全库），并在返回里用 venue_source=default 明确回显**，
    不再出现「同一接口只改默认值就换了一个数」的静默口径漂移。

    2026-11-21 平台修复：
    - 问题 2：回显 venue / venue_default / venue_source，同名指标带 metric + scope 枚举；
    - 问题 3：by_field 每个字段都带 {papers, n, rate, verdict}，n 是分母；
    - 问题 1：返回 call_id / computed_at / version / denominator / numerator，可复算。
    """
    p = "./paperlib/baseline_rates.json"
    if not os.path.exists(p):
        raise HTTPException(status_code=503, detail="基线表未生成")
    data = json.load(open(p, encoding="utf-8"))
    venue_req = (venue or "").strip()
    venue_eff = venue_req or "all"
    venue_source = "param" if venue_req else "default"
    snapshot = _data_snapshot()

    out = {
        "ok": True,
        "total_papers": data.get("total_papers"),
        # 问题 2：口径必须回显，不能靠调用方猜默认值。
        "venue": venue_eff,
        "venue_effective": venue_eff,
        "venue_default": "all",
        "venue_requested": venue_req or None,
        "venue_source": venue_source,
        "venue_options": ["top", "arxiv", "peer", "all"],
        "venue_doc": ("venue 不传时固定默认 all（全库，向后兼容）；"
                      "top=顶会来源 venue-crawl，arxiv=预印本，peer=有评审 deepreview。"
                      "返回里的 venue_source=default/param 标明本次取的是默认值还是显式参数。"),
        "metric": "feature_occurrence_rate",
        # 问题 4（glm-fix4）：venue 变体归一规则公开，不再是黑盒。
        "venue_alias": _venue_alias_payload(),
    }

    # 口径敏感性（sensitivity，2026-11-21）：真实按 SQLite 现算四个口径，
    # 不写死数字；默认用 seed（与 sensitivity 规范里的示例指标一致），传 feature 则算该特征。
    # 只新增字段，不改 overall/by_field/venue 等旧字段。
    if _sensitivity is not None:
        try:
            _sens_scopes, _sens, _sens_rec = _sensitivity.for_baseline(
                feature or "seed", field or None, venue_data=data.get("by_venue"))
            out["sensitivity"] = _sens
            out["recommended_scope"] = _sens_rec
            if _sens.get("conclusion_robust") is False:
                out.setdefault("warning", _sens.get("warning"))
        except Exception as _e:
            out["sensitivity"] = {"error": repr(_e)}
            print("[baseline] sensitivity 计算失败：%r" % (_e,), flush=True)

    # 分层基线（v3，2026-11-20）
    BV = data.get("by_venue") or {}
    venue_ok = False
    if venue_req:
        v = BV.get(venue_req)
        if not v:
            out["venue_missing"] = venue_req
            out["venue_available"] = list(BV.keys())
        else:
            venue_ok = True
            out["venue_n"] = v.get("n")
            out["venue_basis"] = ("source=" + _VENUE_SOURCE[venue_req]
                                  if venue_req in _VENUE_SOURCE else "all papers with features")
            if feature:
                fv = (v.get("features") or {}).get(feature.lower())
                out["overall"] = fv
            else:
                out["overall"] = v.get("features")
    else:
        out["venue_available"] = list(BV.keys())

    if feature:
        f = feature.lower()
        out["feature"] = f
        meta_n, meta_k = 0, 0
        meta_scope = "venue:" + venue_eff
        meta_desc = "papers under venue=%s with extracted features" % venue_eff

        if not venue_req:
            ov = data.get("overall", {}).get(f)
            out["overall"] = ov
            meta_n = int(data.get("total_papers") or 0)
            meta_desc = "all papers in library with extracted features"
            meta_scope = "venue:all"
            bf = {}
            for k, v in (data.get("by_field") or {}).items():
                fv = (v.get("features") or {}).get(f)
                if not fv:
                    continue
                # 问题 3：补上分母 n（旧 payload 只有 papers/rate/verdict）。
                bf[k] = {"papers": fv.get("papers"), "n": int(v.get("n") or 0),
                         "rate": fv.get("rate"), "verdict": fv.get("verdict"),
                         "field": k, "scope": "field:%s+venue:all" % k}
            bf["_filtered_by"] = "all"
            out["by_field"] = bf
            out["_filtered_by"] = "all"
            meta_k = int((ov or {}).get("papers") or 0)
            if isinstance(out.get("overall"), dict):
                out["overall"] = dict(out["overall"])
                out["overall"]["n"] = meta_n
        elif venue_ok:
            bf, overall = _venue_stats(venue_req, f)
            # 同一批论文现算 overall，确保与 by_field 数学自洽
            out["overall"] = overall
            out["venue_n"] = overall["n"]
            for k, v in list(bf.items()):
                if isinstance(v, dict):
                    v["field"] = k
                    v["scope"] = "field:%s+venue:%s" % (k, venue_req)
            bf["_filtered_by"] = venue_req
            out["by_field"] = bf
            out["_filtered_by"] = venue_req
            meta_n = int(overall.get("n") or 0)
            meta_k = int(overall.get("papers") or 0)
            meta_desc = ("papers with source=%s (venue=%s) with extracted features"
                         % (_VENUE_SOURCE.get(venue_req, "all"), venue_req))
        else:
            # 未知 venue：保持旧行为（不过滤），但显式标注，避免再被当成已过滤
            bf = {}
            for k, v in (data.get("by_field") or {}).items():
                fv = (v.get("features") or {}).get(f)
                if not fv:
                    continue
                bf[k] = {"papers": fv.get("papers"), "n": int(v.get("n") or 0),
                         "rate": fv.get("rate"), "verdict": fv.get("verdict"),
                         "field": k, "scope": "field:%s+venue:unfiltered" % k}
            bf["_filtered_by"] = "unfiltered_unknown_venue"
            out["by_field"] = bf
            out["_filtered_by"] = "unfiltered_unknown_venue"
            meta_n = int(data.get("total_papers") or 0)
            if isinstance(out.get("overall"), dict):
                out["overall"] = dict(out["overall"])
                out["overall"]["n"] = meta_n

        if field:
            fd = out.get("by_field", {}).get(field)
            if isinstance(fd, dict):
                out["field_value"] = {"n": fd.get("n"), "papers": fd.get("papers"),
                                      "feature": f, "value": fd,
                                      "_filtered_by": out["by_field"].get("_filtered_by")}
                # 指定 field 时，分母口径收窄到该字段
                meta_n = int(fd.get("n") or 0)
                meta_k = int(fd.get("papers") or 0)
                meta_scope = "field:%s+venue:%s" % (field, venue_eff)
                meta_desc = ("papers in field %s under venue=%s with extracted features"
                             % (field, venue_eff))
            else:
                _fd = data.get("by_field", {}).get(field, {})
                out["field_value"] = {"n": _fd.get("n", _fd.get("papers", 0)),
                                      "feature": f,
                                      "value": (_fd.get("features") or {}).get(f),
                                      "_filtered_by": "all"}
                meta_n = int(_fd.get("n") or 0)
                meta_k = int((_fd.get("features") or {}).get(f, {}).get("papers") or 0)

        # 双源交叉验证：SQLite 全量（本次响应同口径）vs FTS5 抽样。
        out["cross_check"] = _baseline_cross_check(f, venue_eff, field, meta_n, meta_k)
        out.update(_call_meta("/api/kb/defect_baseline",
                              {"field": field or None, "feature": f, "venue": venue_eff},
                              {"n": meta_n, "description": meta_desc},
                              {"n": meta_k, "description": "papers where feature %s occurs" % f},
                              metric="feature_occurrence_rate", scope=meta_scope,
                              snapshot=snapshot))
        _save_call(out.get("call_id"), out)
        return out

    if field:
        out["field"] = field
        if venue_ok:
            vf = _venue_field_features(venue_req, field)
            out["papers"] = vf["n"]
            for k, v in list((vf.get("features") or {}).items()):
                if isinstance(v, dict):
                    v["n"] = vf["n"]
                    v["field"] = field
            out["features"] = vf["features"]
            out["_filtered_by"] = venue_req
            # 未指定 feature 时，交叉验证固定用 seed（与示例口径一致，返回里写明）。
            _seed = (vf.get("features") or {}).get("seed") or {}
            out["cross_check"] = _baseline_cross_check(
                "seed", venue_eff, field, int(vf["n"] or 0), int(_seed.get("papers") or 0))
            out.update(_call_meta("/api/kb/defect_baseline",
                                  {"field": field, "feature": None, "venue": venue_eff},
                                  {"n": int(vf["n"] or 0),
                                   "description": "papers in field %s under venue=%s" % (field, venue_eff)},
                                  metric="feature_occurrence_rate",
                                  scope="field:%s+venue:%s" % (field, venue_eff),
                                  snapshot=snapshot))
            _save_call(out.get("call_id"), out)
            return out
        d = data.get("by_field", {}).get(field)
        if not d:
            raise HTTPException(status_code=404, detail="该领域数据不足")
        out["papers"] = d.get("papers", d.get("n"))
        for k, v in list((d.get("features") or {}).items()):
            if isinstance(v, dict):
                v["n"] = int(d.get("n") or 0)
                v["field"] = field
        out["features"] = d["features"]
        _seed = (d.get("features") or {}).get("seed") or {}
        out["cross_check"] = _baseline_cross_check(
            "seed", venue_eff, field, int(d.get("n") or 0), int(_seed.get("papers") or 0))
        out.update(_call_meta("/api/kb/defect_baseline",
                              {"field": field, "feature": None, "venue": "all"},
                              {"n": int(d.get("n") or 0),
                               "description": "all papers in field %s with extracted features" % field},
                              metric="feature_occurrence_rate", scope="field:" + field,
                              snapshot=snapshot))
        _save_call(out.get("call_id"), out)
        return out

    out.setdefault("overall", data["overall"])
    out["fields"] = {k: v.get("n", v.get("papers"))
                     for k, v in (data.get("by_field") or {}).items()}
    if venue_ok:
        _dn = int(out.get("venue_n") or 0)
        _ddesc = ("papers with source=%s (venue=%s) with extracted features"
                  % (_VENUE_SOURCE.get(venue_req, "all"), venue_req))
    else:
        _dn = int(data.get("total_papers") or 0)
        _ddesc = "all papers in library with extracted features"
    _ov = out.get("overall") or {}
    if isinstance(_ov, dict) and "papers" in _ov:
        _k_seed = int(_ov.get("papers") or 0)
    else:
        _k_seed = int(((_ov.get("seed") or {}) if isinstance(_ov, dict) else {}).get("papers") or 0)
    # 未指定 feature/field 时，交叉验证固定用 seed（返回里 cross_check.feature 会写明）。
    out["cross_check"] = _baseline_cross_check("seed", venue_eff, field, _dn, _k_seed)
    out.update(_call_meta("/api/kb/defect_baseline",
                          {"field": None, "feature": None, "venue": venue_eff},
                          {"n": _dn, "description": _ddesc},
                          metric="feature_occurrence_rate", scope="venue:" + venue_eff,
                          snapshot=snapshot))
    _save_call(out.get("call_id"), out)
    return out


@app.get("/kb/baseline", response_class=HTMLResponse)
def kb_baseline_page():
    """缺陷-同行基线页"""
    p = os.path.join(V15_ROOT, "app", "templates", "kb_baseline.html")
    if os.path.exists(p):
        return FileResponse(p)
    return HTMLResponse("<h1>缺陷基线页未部署</h1>", status_code=404)


@app.get("/api/version")
def api_version():
    """版本端点（问题 4）：列出当前 schema 版本、每个 /api/* 的版本与 changelog 位置。"""
    apis = {}
    for r in app.routes:
        path = getattr(r, "path", "") or ""
        if path.startswith("/api/"):
            apis[path] = API_SCHEMA_VERSION
    # 问题 2：覆盖时效性（库停在哪天、多久更新一次、最后入库时间）。
    cov = _coverage_info()
    # 问题 5：配额 + 限流（真实生效值，不是文档里的期望值）。
    try:
        qs = _guard.quota_status()
    except Exception as _e:
        qs = {"error": repr(_e)}
    try:
        rls = _guard.rate_limit_status()
    except Exception as _e:
        rls = {"error": repr(_e)}
    try:
        demo = _guard.demo_quota_status()
    except Exception as _e:
        demo = {"error": repr(_e)}
    return {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "current": API_VERSION,
        "released_at": API_RELEASED_AT,
        "data_snapshot": _data_snapshot(),
        # ---- 问题 2：覆盖时效性 ----
        "data_cutoff": cov.get("cutoff"),
        "update_freq": cov.get("update_freq") or "daily",
        "last_ingest": cov.get("last_ingest"),
        "newest_paper": cov.get("newest_paper"),
        "total_papers": cov.get("total_papers"),
        "coverage_note": cov.get("coverage_note") or _COVERAGE_NOTE,
        "coverage": cov,
        # ---- 问题 5：成本与速率透明 ----
        "quota": {
            "daily_requests": qs.get("daily_requests"),
            "used_today": qs.get("used_today"),
            "remaining_today": qs.get("requests_remaining"),
            "cost_per_run": qs.get("cost_per_run"),
            "currency": qs.get("currency", "CNY"),
            "budget_daily": qs.get("budget_daily"),
            "budget_used": qs.get("budget_used"),
            "budget_remaining": qs.get("budget_remaining"),
            "budget_runs": qs.get("budget_runs"),
            "reset_in_sec": qs.get("reset_in_sec"),
            "scope": qs.get("scope"),
        },
        "rate_limit": rls,
        "rate_limit_scope": rls.get("scope"),
        "demo_key": demo,
        "api_layers": _guard.layer_manifest(),
        "api_layer_by_path": {p: _guard.api_layer(p) for p in sorted(apis)},
        "versioning": ("每个 /api/* 的 JSON 返回都带 schema_version + api_version；"
                       "新增字段只加不改，breaking change 必须升级 schema_version。"),
        "changelog": "/CHANGELOG.md",
        "docs": "/API.md",
        "apis": dict(sorted(apis.items())),
        "calls": {"replay": "/api/call/{call_id}",
                  "stored_dir": _CALL_DIR,
                  "note": "带数字/检索的接口返回 call_id；用同一 call_id 可原样回查落盘结果",
                  "call_id_kind": "content_addressed_result_id",
                  "semantics": _CALL_SEMANTICS_NOTE,
                  "fields": {
                      "is_cached": "本次响应是否为同 call_id 的重复请求（true=之前已算过/命中落盘结果）",
                      "fetch_count": "该 call_id 被业务接口返回的次数（不含 /api/call 回看）",
                      "first_computed_at": "该 call_id 第一次被计算的时间",
                      "last_accessed_at": "该 call_id 最近一次被业务接口请求的时间",
                  },
                  "fetch_count_note": _CALL_FETCH_NOTE},
    }


@app.get("/api/call/{call_id}")
def api_call_replay(call_id: str):
    """按 call_id 回查一次计算（问题 1 的可复算入口）。

    2026-11-21：回看也返回 is_cached / fetch_count / first_computed_at /
    last_accessed_at。回看只读，不增加 fetch_count（避免“看一次算一次调用”）。
    """
    d = _load_call(call_id)
    if not d:
        raise HTTPException(status_code=404, detail="call_id 不存在或尚未落盘")
    out = dict(d)
    stat = _call_stats_peek(call_id)
    if stat:
        out.update(_call_stats_fields(stat))
        out["fetch_count_exact"] = True
    else:
        # 升级前落盘的旧 call_id 没有统计文件：至少算过 1 次，按 computed_at 回填。
        base_t = d.get("computed_at") or d.get("first_computed_at") or ""
        out.update({"is_cached": bool(d.get("is_cached")),
                    "fetch_count": int(d.get("fetch_count") or 1),
                    "first_computed_at": d.get("first_computed_at") or base_t,
                    "last_accessed_at": d.get("last_accessed_at") or base_t})
        out["fetch_count_exact"] = False
    out["replayed"] = True
    out["replay_of"] = call_id
    out["call_id_kind"] = "content_addressed_result_id"
    out["call_id_semantics"] = _CALL_SEMANTICS_NOTE
    out["fetch_count_note"] = _CALL_FETCH_NOTE
    return out


@app.get("/CHANGELOG.md")
def changelog_md():
    p = os.path.join(V15_ROOT, "app", "CHANGELOG.md")
    if os.path.exists(p):
        return FileResponse(p, media_type="text/markdown; charset=utf-8")
    return HTMLResponse("<h1>CHANGELOG.md 未部署</h1>", status_code=404)


@app.get("/API.md")
def api_doc_md():
    """人读 API 文档：默认值、字段口径、call_id 回放、CLI。"""
    p = os.path.join(V15_ROOT, "app", "API.md")
    if os.path.exists(p):
        return FileResponse(p, media_type="text/markdown; charset=utf-8")
    return HTMLResponse("<h1>API.md 未部署</h1>", status_code=404)


@app.get("/api/meta")
def api_meta():
    """自述：能力清单、版本、限额（公开，供外部接入发现）"""
    return {
        "name": "qunzhi",
        "version": "1.0.0",
        "domain": "QA over CS papers: single-paper review, package review, defect baseline, missing citations",
        "base": "https://qunzhi.team:8443",
        "auth": {"param": "key", "header": "X-Access-Key", "note": "contact owner for a key"},
        "capabilities": [
            {"id": "v18_review", "method": "POST", "port": 18097, "path": "/review",
             "desc": "单篇文档自检：16 条带原文锚点的问题", "cost": "~0.11 CNY/paper"},
            {"id": "kb_search", "method": "POST", "path": "/api/kb/search",
             "desc": "37 万篇 CS 论文检索（FTS5）"},
            {"id": "paper_detail", "method": "GET", "path": "/api/kb/paper/{pid}"},
            {"id": "defect_baseline", "method": "GET", "path": "/api/kb/defect_baseline",
             "desc": "缺陷-同行基线（出现率 + universal/majority/minority）"},
            {"id": "missing_cites", "method": "GET", "path": "/api/kb/missing_cites",
             "desc": "漏引候选（共引兄弟对比）"},
            {"id": "pkg_analyze", "method": "POST", "path": "/api/pkg/analyze",
             "desc": "论文包结构分析（zip 上传）"},
            {"id": "pkg_review2", "method": "POST", "path": "/api/pkg/{job}/review2",
             "desc": "疑点判定（每条带成立条件与不能推出）"},
            {"id": "pkg_numbers", "method": "POST", "path": "/api/pkg/{job}/numbers",
             "desc": "跨文件数字对账"},
            {"id": "lib_health", "method": "GET", "path": "/api/lib/health"},
        ],
        "mcp": {"server": "base/mcp_server.py", "tools": 5,
                "note": "MCP stdio server wrapping the above capabilities"},
        "limits": {"domain": "cs.* / stat.ML / eess.*", "rate": "per-key throttle", "language": "EN papers"},
    }


@app.post("/api/v15/rehearse")
async def v15_rehearse(payload: dict):
    """v15 决策预演（重构版：立场×证据矩阵 + 可证伪规则 + 引用强制）"""
    q = (payload or {}).get("question", "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="缺少 question")
    import subprocess
    cmd = ["/home/ubuntu/qunzhi/venv/bin/python",
           "./paperlib/base/v15_service.py",
           "--question", q[:400],
           "--bg", str((payload or {}).get("bg", ""))[:600],
           "--field", str((payload or {}).get("field", ""))[:40],
           "--json"]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    if r.returncode != 0:
        raise HTTPException(status_code=500, detail=(r.stderr or r.stdout or "")[-300:])
    try:
        return json.loads((r.stdout or "").strip().splitlines()[-1])
    except Exception:
        raise HTTPException(status_code=500, detail="解析失败：" + (r.stdout or "")[:200])


@app.get("/v15", response_class=HTMLResponse)
def v15_page():
    p = os.path.join(V15_ROOT, "app", "templates", "v15_rehearsal.html")
    if os.path.exists(p):
        return FileResponse(p)
    return HTMLResponse("<h1>v15 预演页未部署</h1>", status_code=404)


def _locate_quote(text: str, quote: str, span: int = 220):
    """在原文中定位引用（规范化匹配 40 字窗口），返回高亮 HTML 与偏移"""
    import html as _h
    import re as _re
    if not text or not quote:
        return None
    t = _re.sub(r"\s+", " ", text)
    q = _re.sub(r"\s+", " ", quote).strip()
    if len(q) < 20:
        return None
    # 取引用的 40 字片段做定位（容忍轻微换行差异）
    probe = q[:60]
    idx = t.find(probe)
    if idx < 0:
        probe = q[:40]
        idx = t.find(probe)
    if idx < 0:
        # 逐词退化：取最长匹配子串
        words = [w for w in _re.findall(r"\S{6,}", q)][:3]
        for w in words:
            idx = t.find(w)
            if idx >= 0:
                probe = w
                break
    if idx < 0:
        return None
    lo = max(0, idx - span // 2)
    hi = min(len(t), idx + len(probe) + span // 2)
    before = _h.escape(t[lo:idx])
    hit = _h.escape(t[idx:idx + len(probe)])
    after = _h.escape(t[idx + len(probe):hi])
    return {"offset": idx, "html": before + "<mark>" + hit + "</mark>" + after,
            "matched": probe}


# /api/evidence 结果缓存：同一 (pid, max_items) 10 分钟内返回同一份问题集。
# 背景：v18 引擎每次调用带随机性，同一 pid 三跑三套结果；缓存是最小侵入的修法。
_EVIDENCE_CACHE = {}
_EVIDENCE_CACHE_TTL = 600
_EVIDENCE_CACHE_MAX = 128
_EVIDENCE_LOCK = threading.Lock()
# 2026-11-21（Bug 3）：超时保护。引擎单次最多 95 秒，整请求最多 115 秒；
# 超时返回 HTTP 200 的结构化错误（ok=false, degraded=true），不再让 nginx 记成 502。
_EVIDENCE_ENGINE_TIMEOUT = float(os.getenv("QZ_EVIDENCE_ENGINE_TIMEOUT", "95"))
_EVIDENCE_HARD_LIMIT = float(os.getenv("QZ_EVIDENCE_HARD_LIMIT", "115"))
_EVIDENCE_KEY_LOCKS = {}
_EVIDENCE_KEY_LOCKS_GUARD = threading.Lock()


def _evidence_key_lock(key):
    """同一 (pid, max_items) 的单飞锁：冷缓存下并发请求只跑一次引擎。"""
    with _EVIDENCE_KEY_LOCKS_GUARD:
        lk = _EVIDENCE_KEY_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            if len(_EVIDENCE_KEY_LOCKS) >= 256:
                # 只清理未被占用的锁，避免无限增长；正在用的锁不动
                for k in list(_EVIDENCE_KEY_LOCKS):
                    if not _EVIDENCE_KEY_LOCKS[k].locked():
                        _EVIDENCE_KEY_LOCKS.pop(k, None)
                        if len(_EVIDENCE_KEY_LOCKS) < 200:
                            break
            _EVIDENCE_KEY_LOCKS[key] = lk
        return lk


def _evidence_degraded(pid, max_items, reason, detail, elapsed_ms):
    """引擎超时/失败时的结构化 200 响应。

    为什么不再抛 502：502 是 nginx 对「上游不可用」的判定，客户端拿不到可读原因，
    也分不清「引擎慢」和「网关挂了」。这里如实返回 ok=false + degraded=true，
    既不假装成功，也不把一次慢请求放大成 502。
    """
    resp = {"ok": False, "pid": pid, "n": 0, "max_items": max_items,
            "anchored": 0, "items": [], "cached": False,
            "cached_at": datetime.now().isoformat(timespec="seconds"),
            "degraded": True, "engine_timeout": reason == "engine_timeout",
            "error": reason, "detail": detail, "retryable": True,
            "elapsed_ms": elapsed_ms}
    # 降级返回也带 call_id：失败同样要可回查，不能只在成功时才可证伪。
    resp.update(_call_meta("/api/evidence", {"pid": pid, "max_items": max_items},
                           {"n": 0, "description": "no issues returned (degraded response)"},
                           {"n": 0, "description": "no anchored issues"},
                           metric="evidence_anchor_rate", scope="pid:" + pid,
                           nonce=str(time.time_ns())))
    _save_call(resp.get("call_id"), resp)
    return resp


@app.post("/api/evidence")
def api_evidence(payload: dict):
    """给一篇论文：跑 v18 → 每条问题定位到原文并高亮

    可复现性：同 (pid, max_items) 10 分钟内返回同一份结果，缓存命中时
    `cached=true` + `cached_at`；冷缓存下同一 key 的并发请求走单飞锁，只跑一次引擎。

    超时保护：网关对 v18 引擎的硬上限 95 秒、整请求硬上限 115 秒（可用
    QZ_EVIDENCE_ENGINE_TIMEOUT / QZ_EVIDENCE_HARD_LIMIT 覆盖）。超时返回 HTTP 200 +
    `ok=false, degraded=true, engine_timeout=true`，不再让 nginx 判成 502。

    max_items 支持 1..24（引擎最低 8，网关按需截断）。
    """
    import sqlite3 as _sq
    import zlib
    import urllib.request as _u
    p = payload or {}
    pid = (p.get("pid") or "").strip()
    try:
        req_n = int(p.get("max_items") or 8)
    except Exception:
        req_n = 8
    max_items = max(1, min(req_n, 24))
    if not pid:
        raise HTTPException(status_code=400, detail="缺 pid")

    # 问题 1 的可复算入口：带上次返回的 call_id 重放，原样返回落盘结果。
    replay_id = (p.get("call_id") or "").strip()
    if replay_id:
        saved = _load_call(replay_id)
        if not saved:
            raise HTTPException(status_code=404,
                                detail="call_id 不存在或未落盘：%s" % replay_id)
        if saved.get("pid") and str(saved.get("pid")) != pid:
            raise HTTPException(status_code=400,
                                detail="call_id 属于 pid=%s，与请求 pid=%s 不一致"
                                       % (saved.get("pid"), pid))
        out = dict(saved)
        out["replayed"] = True
        out["replay_of"] = replay_id
        return out

    req_start = time.time()
    deadline = req_start + max(30.0, _EVIDENCE_HARD_LIMIT)
    cache_key = (pid, max_items)
    now = time.time()

    def _elapsed_ms():
        return int(round((time.time() - req_start) * 1000))

    # 快路径：已缓存直接返回（无锁）
    with _EVIDENCE_LOCK:
        hit = _EVIDENCE_CACHE.get(cache_key)
        if hit and (now - hit["ts"]) < _EVIDENCE_CACHE_TTL:
            resp = dict(hit["resp"])
            resp["cached"] = True
            resp["cached_at"] = hit["cached_at"]
            resp["cache_age_sec"] = round(now - hit["ts"], 1)
            resp["elapsed_ms"] = _elapsed_ms()
            # call_id 是计算结果 ID：同 ID 再次被返回，fetch_count 递增。
            _call_touch_cached(resp)
            return resp

    # 单飞：同一个 (pid,max_items) 冷缓存时只允许一个请求打引擎
    key_lock = _evidence_key_lock(cache_key)
    wait_s = max(0.0, deadline - time.time() - 1.0)
    if not key_lock.acquire(timeout=wait_s):
        return _evidence_degraded(
            pid, max_items, "wait_timeout",
            "同一 pid 的首次请求仍在跑，等待超过 %.0f 秒" % _EVIDENCE_HARD_LIMIT,
            _elapsed_ms())
    try:
        # 拿到锁后重查：等锁期间别的请求可能已经写好缓存
        now = time.time()
        with _EVIDENCE_LOCK:
            hit = _EVIDENCE_CACHE.get(cache_key)
            if hit and (now - hit["ts"]) < _EVIDENCE_CACHE_TTL:
                resp = dict(hit["resp"])
                resp["cached"] = True
                resp["cached_at"] = hit["cached_at"]
                resp["cache_age_sec"] = round(now - hit["ts"], 1)
                resp["elapsed_ms"] = _elapsed_ms()
                _call_touch_cached(resp)
                return resp

        with _kb_conn() as c:
            row = c.execute("select pid,title,field from papers where pid=?", (pid,)).fetchone()
            z = c.execute("select z from fulltext_z where pid=?", (pid,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="论文不存在")
            if not z:
                raise HTTPException(status_code=409, detail="该论文没有全文，无法高亮")
            text = zlib.decompress(z[0]).decode("utf-8", "ignore")
        # 引擎 Pydantic 约束 max_items>=8；小值在网关侧截断，保证 n 与请求一致
        engine_n = max(8, max_items)
        body = json.dumps({"title": row["title"], "text": text[:60000], "mode": "fast",
                           "venue": "all", "max_items": engine_n, "field": row["field"]}).encode()
        md, last_err, engine_ms = None, None, None
        for attempt in range(2):
            remaining = deadline - time.time() - 1.0
            if remaining < 5:
                last_err = "整请求超过 %.0f 秒预算" % _EVIDENCE_HARD_LIMIT
                break
            tmo = min(_EVIDENCE_ENGINE_TIMEOUT, remaining)
            t_eng = time.time()
            try:
                req = _u.Request("http://127.0.0.1:18097/review", data=body,
                                 headers={"Content-Type": "application/json"})
                with _u.urlopen(req, timeout=tmo) as r:
                    md = json.loads(r.read().decode()).get("markdown") or ""
                engine_ms = int(round((time.time() - t_eng) * 1000))
                break
            except Exception as e:
                last_err = e
                engine_ms = int(round((time.time() - t_eng) * 1000))
                # 只有“秒失败”才重试；已经耗掉大半个预算就立刻如实报超时
                if (time.time() - t_eng) > 5 or (deadline - time.time()) < 20:
                    break
                time.sleep(1.0)
        if md is None:
            _le = str(last_err).lower()
            reason = "engine_timeout" if ("timed out" in _le or "timeout" in _le) else "engine_error"
            return _evidence_degraded(
                pid, max_items, reason,
                "v18 引擎未在预算内返回：%s（引擎耗时 %sms）" % (last_err, engine_ms),
                _elapsed_ms())

        items = []
        cur = None
        for ln in md.splitlines():
            m = re.match(r"^- \[([A-Z]{3})/[^\]]+\]\s*(.+)$", ln.strip())
            if m:
                cur = {"cls": m.group(1), "issue": m.group(2)[:200], "quote": ""}
                items.append(cur)
            elif cur is not None and "出处：" in ln:
                q = ln.split("出处：", 1)[1].split("｜")[0].strip()
                cur["quote"] = q
        out = []
        for it in items[:max_items]:
            loc = _locate_quote(text, it.get("quote") or "")
            it2 = dict(it)
            it2["found"] = bool(loc)
            it2["offset"] = loc["offset"] if loc else None
            it2["html"] = loc["html"] if loc else ""
            it2["matched"] = (loc.get("matched") if loc else "") or ""
            out.append(it2)
        n_anchored = sum(1 for x in out if x["found"])
        resp = {"ok": True, "pid": pid, "title": row["title"], "field": row["field"],
                "n": len(out), "max_items": max_items,
                "anchored": n_anchored, "items": out,
                "cached": False,
                "cached_at": datetime.now().isoformat(timespec="seconds"),
                "elapsed_ms": _elapsed_ms(), "engine_elapsed_ms": engine_ms,
                "fulltext_chars": len(text)}
        # 私有层输出：证据正文可能含用户上传/评审内容，返回前再过一遍脱敏
        try:
            resp["items"] = _privacy.scrub_payload(
                resp.get("items") or [],
                keys={"quote", "html", "matched", "issue"}, mode="review")
            resp["privacy"] = {"scrubbed": True, "layer": "private",
                               "policy": "quote/html/matched 返回前脱敏；不返回用户原文"}
        except Exception as _pe:
            resp["privacy"] = {"scrubbed": False, "error": type(_pe).__name__}
        resp.update(_call_meta("/api/evidence", {"pid": pid, "max_items": max_items},
                               {"n": len(out),
                                "description": "issues returned by v18 engine for this paper"},
                               {"n": n_anchored,
                                "description": "issues with an exact quote located in fulltext"},
                               metric="evidence_anchor_rate", scope="pid:" + pid,
                               # 引擎有随机性：call_id 标识“这一次计算”，
                               # 同参数重算会得到新 call_id；旧 call_id 用 /api/call 原样回放。
                               nonce=str(time.time_ns())))
        _save_call(resp.get("call_id"), resp)
        with _EVIDENCE_LOCK:
            if len(_EVIDENCE_CACHE) >= _EVIDENCE_CACHE_MAX and cache_key not in _EVIDENCE_CACHE:
                oldest = min(_EVIDENCE_CACHE, key=lambda k: _EVIDENCE_CACHE[k]["ts"])
                _EVIDENCE_CACHE.pop(oldest, None)
            _EVIDENCE_CACHE[cache_key] = {"ts": time.time(),
                                          "cached_at": resp["cached_at"],
                                          "resp": resp}
        return resp
    finally:
        key_lock.release()


@app.get("/evidence", response_class=HTMLResponse)
def evidence_page():
    p = os.path.join(V15_ROOT, "app", "templates", "evidence.html")
    if os.path.exists(p):
        return FileResponse(p)
    return HTMLResponse("<h1>证据高亮页未部署</h1>", status_code=404)


# ==================== 错误样本集 / 评测基线（2026-11-21 glm-fix4 问题 6） ====================
# 每个 case 都是“真实请求 + 真实断言”，不是写死的 pass=true。
# runner 用 ASGI in-process 调用（不过网络、不依赖端口），因此 timer 跑也不会打到线上流量。
_EVAL_MISSING = object()

_EVAL_CASES = [
    {"id": "E1", "name": "中文查询自动改写",
     "input": {"q": "大语言模型"},
     "request": {"method": "POST", "path": "/api/kb/search",
                 "json": {"q": "大语言模型", "limit": 3}},
     "expect": {"gate.reason": "auto_translated", "translated.query": "exists",
                "total": ">0"},
     "asserts": [{"path": "gate.reason", "op": "eq", "value": "auto_translated"},
                 {"path": "translated.query", "op": "exists"},
                 {"path": "total", "op": "gt", "value": 0}]},
    {"id": "E2", "name": "total 超 cap",
     "input": {"q": "post-training"},
     "request": {"method": "POST", "path": "/api/kb/search",
                 "json": {"q": "post-training", "limit": 3}},
     "expect": {"total_is_capped": True},
     "asserts": [{"path": "total_is_capped", "op": "eq", "value": True}]},
    {"id": "E3", "name": "无全文 selfcheck",
     "input": {"pid": "ax_2010.05113"},
     "request": {"method": "GET", "path": "/api/kb/selfcheck",
                 "params": {"pid": "ax_2010.05113"}},
     "expect": {"has_fulltext": False},
     "asserts": [{"path": "has_fulltext", "op": "eq", "value": False}]},
    {"id": "E4", "name": "search 回显覆盖范围",
     "input": {"q": "transformer"},
     "request": {"method": "POST", "path": "/api/kb/search",
                 "json": {"q": "transformer", "limit": 2}},
     "expect": {"coverage.total_papers": ">0", "coverage.cutoff": "exists"},
     "asserts": [{"path": "coverage.total_papers", "op": "gt", "value": 0},
                 {"path": "coverage.cutoff", "op": "exists"}]},
    {"id": "E5", "name": "venue 别名 NIPS→NEURIPS（且能召回）",
     "input": {"q": "", "venue": "NIPS", "limit": 2},
     "request": {"method": "POST", "path": "/api/kb/search",
                 "json": {"q": "", "venue": "NIPS", "limit": 2}},
     "expect": {"venue_resolution.resolved": "NEURIPS",
                "venue_resolution.method": "alias", "returned": ">0"},
     "asserts": [{"path": "venue_resolution.resolved", "op": "eq", "value": "NEURIPS"},
                 {"path": "venue_resolution.method", "op": "eq", "value": "alias"},
                 {"path": "returned", "op": "gt", "value": 0}]},
    {"id": "E6", "name": "venue 长别名→CVPR",
     "input": {"q": "vision", "venue": "IEEE/CVF conf. computer vision"},
     "request": {"method": "POST", "path": "/api/kb/search",
                 "json": {"q": "vision", "venue": "IEEE/CVF conf. computer vision",
                          "limit": 2}},
     "expect": {"venue_resolution.resolved": "CVPR",
                "venue_resolution.method": "alias", "returned": ">0"},
     "asserts": [{"path": "venue_resolution.resolved", "op": "eq", "value": "CVPR"},
                 {"path": "venue_resolution.method", "op": "eq", "value": "alias"},
                 {"path": "returned", "op": "gt", "value": 0}]},
    {"id": "E7", "name": "version 暴露覆盖时效",
     "input": {},
     "request": {"method": "GET", "path": "/api/version"},
     "expect": {"data_cutoff": "exists", "update_freq": "daily", "last_ingest": "exists"},
     "asserts": [{"path": "data_cutoff", "op": "exists"},
                 {"path": "update_freq", "op": "eq", "value": "daily"},
                 {"path": "last_ingest", "op": "exists"}]},
    {"id": "E8", "name": "version 暴露配额与限流",
     "input": {},
     "request": {"method": "GET", "path": "/api/version"},
     "expect": {"quota.daily_requests": ">0", "rate_limit.per_key_per_minute": ">0"},
     "asserts": [{"path": "quota.daily_requests", "op": "gt", "value": 0},
                 {"path": "rate_limit.per_key_per_minute", "op": "gt", "value": 0},
                 {"path": "quota.used_today", "op": "ge", "value": 0}]},
    {"id": "E9", "name": "baseline 公开 venue 别名表",
     "input": {"venue": "all"},
     "request": {"method": "GET", "path": "/api/kb/defect_baseline",
                 "params": {"venue": "all"}},
     "expect": {"venue_alias.NEURIPS": "contains NIPS", "venue_alias._note": "exists"},
     "asserts": [{"path": "venue_alias.NEURIPS", "op": "contains", "value": "NIPS"},
                 {"path": "venue_alias._note", "op": "exists"}]},
    {"id": "E10", "name": "非 CS 查询被拒",
     "input": {"q": "红烧肉怎么做"},
     "request": {"method": "POST", "path": "/api/kb/search",
                 "json": {"q": "红烧肉怎么做", "limit": 2}},
     "expect": {"gate.reason": "non_cs_topic"},
     "asserts": [{"path": "gate.reason", "op": "eq", "value": "non_cs_topic"}]},
    {"id": "E11", "name": "CS 主题但库内无命中",
     "input": {"q": "transformer zzzznotexist"},
     "request": {"method": "POST", "path": "/api/kb/search",
                 "json": {"q": "transformer zzzznotexist", "limit": 2}},
     "expect": {"gate.reason": "no_match"},
     "asserts": [{"path": "gate.reason", "op": "eq", "value": "no_match"}]},
    {"id": "E12", "name": "401 错误带 retry_after/quota",
     "input": {"path": "/api/intent", "key": "invalid_eval_probe_key"},
     "request": {"method": "POST", "path": "/api/intent", "json": {"topic": "x"},
                 "headers": {"X-Access-Key": "invalid_eval_probe_key"}},
     "expect": {"status": 401, "retry_after_sec": "exists", "quota_remaining": "exists"},
     "asserts": [{"path": "status", "op": "eq", "value": 401},
                 {"path": "retry_after_sec", "op": "exists"},
                 {"path": "quota_remaining", "op": "exists"}]},
    {"id": "E13", "name": "404 错误带 retry_after/quota",
     "input": {"pid": "ax_9999.99999"},
     "request": {"method": "GET", "path": "/api/kb/selfcheck",
                 "params": {"pid": "ax_9999.99999"}},
     "expect": {"status": 404, "retry_after_sec": "exists"},
     "asserts": [{"path": "status", "op": "eq", "value": 404},
                 {"path": "retry_after_sec", "op": "exists"},
                 {"path": "quota_remaining", "op": "exists"}]},
    {"id": "E14", "name": "空查询不报错",
     "input": {"q": ""},
     "request": {"method": "POST", "path": "/api/kb/search",
                 "json": {"q": "", "limit": 1}},
     "expect": {"ok": True, "total": ">=0"},
     "asserts": [{"path": "ok", "op": "eq", "value": True},
                 {"path": "total", "op": "ge", "value": 0}]},
    {"id": "E15", "name": "GET 检索与 POST 口径一致",
     "input": {"q": "post-training", "limit": 1},
     "request": {"method": "GET", "path": "/api/kb/search",
                 "params": {"q": "post-training", "limit": 1}},
     "expect": {"total_is_capped": True, "coverage": "exists"},
     "asserts": [{"path": "total_is_capped", "op": "eq", "value": True},
                 {"path": "coverage.cutoff", "op": "exists"}]},
    {"id": "E16", "name": "400 错误带 retry_after/quota",
     "input": {"q": 123},
     "request": {"method": "POST", "path": "/api/kb/search",
                 "json": {"q": 123, "limit": 2}},
     "expect": {"status": 400, "retry_after_sec": "exists", "quota_remaining": "exists"},
     "asserts": [{"path": "status", "op": "eq", "value": 400},
                 {"path": "retry_after_sec", "op": "exists"},
                 {"path": "quota_remaining", "op": "exists"}]},
]


def _eval_get_path(env: dict, path: str):
    cur = env
    for part in (path or "").split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return _EVAL_MISSING
    return cur


def _eval_cmp(got, op: str, want) -> bool:
    if op == "exists":
        return got is not _EVAL_MISSING and got is not None
    if got is _EVAL_MISSING:
        return False
    if op == "eq":
        return got == want
    if op == "ne":
        return got != want
    if op == "gt":
        return isinstance(got, (int, float)) and got > want
    if op == "ge":
        return isinstance(got, (int, float)) and got >= want
    if op == "lt":
        return isinstance(got, (int, float)) and got < want
    if op == "contains":
        if isinstance(got, str):
            return str(want) in got
        if isinstance(got, (list, tuple)):
            return want in got
        if isinstance(got, dict):
            return want in got
        return False
    if op == "truthy":
        return bool(got)
    if op == "falsey":
        return not bool(got)
    return False


def _eval_headers() -> dict:
    k = getattr(_guard, "ACCESS_KEY", "") or ""
    return {"X-Access-Key": k} if k else {}


async def _eval_cases_async() -> list:
    import httpx
    transport = httpx.ASGITransport(app=app)
    out = []
    base_headers = _eval_headers()
    async with httpx.AsyncClient(transport=transport, base_url="http://eval.local",
                                 timeout=30) as client:
        for case in _EVAL_CASES:
            req = case.get("request") or {}
            headers = dict(base_headers)
            headers.update(req.get("headers") or {})
            t0 = time.time()
            item = {"id": case.get("id"), "name": case.get("name"),
                    "input": case.get("input"),
                    "expect": case.get("expect"), "pass": False}
            try:
                r = await client.request(req.get("method", "GET"), req.get("path", "/"),
                                         params=req.get("params"), json=req.get("json"),
                                         headers=headers)
                try:
                    body = r.json()
                except Exception:
                    body = {"_raw": (r.text or "")[:500]}
                env = dict(body) if isinstance(body, dict) else {"json": body}
                env["status"] = r.status_code
                checks, ok = [], True
                for a in case.get("asserts") or []:
                    got = _eval_get_path(env, a.get("path", ""))
                    good = _eval_cmp(got, a.get("op"), a.get("value"))
                    ok = ok and good
                    checks.append({"path": a.get("path"), "op": a.get("op"),
                                   "want": a.get("value"),
                                   "got": ("<missing>" if got is _EVAL_MISSING else got),
                                   "pass": good})
                item.update({"pass": bool(ok), "status": r.status_code,
                             "checks": checks, "duration_ms": int((time.time() - t0) * 1000)})
                if not ok:
                    item["detail"] = {k: body.get(k) for k in
                                      ("ok", "detail", "error", "gate", "total_is_capped",
                                       "venue_resolution", "coverage", "data_cutoff",
                                       "retry_after_sec", "quota_remaining") if k in body}
            except Exception as e:
                item.update({"pass": False, "error": repr(e),
                             "duration_ms": int((time.time() - t0) * 1000)})
            out.append(item)
    return out


# 60 秒缓存：eval_cases 会真的跑 16 个内部请求（带 key），
# 公网高频调用时不能把 per-key 限流额度打满，也不能给网关制造额外负载。
_EVAL_RESULT_CACHE = {"ts": 0.0, "data": None}
_EVAL_CACHE_TTL = 60


@app.get("/api/eval_cases")
def api_eval_cases(refresh: bool = False):
    """边界用例集（问题 6）：真实请求 + 真实断言，返回每条 pass/fail。

    覆盖：语言闸门、非 CS 闸门、total cap、无全文 selfcheck、覆盖时效、
    venue 别名、配额/限流字段、401/404/400 错误字段、空查询、GET/POST 一致性。
    """
    import asyncio
    now = time.time()
    if (not refresh and _EVAL_RESULT_CACHE["data"]
            and (now - _EVAL_RESULT_CACHE["ts"]) < _EVAL_CACHE_TTL):
        d = dict(_EVAL_RESULT_CACHE["data"])
        d["cached"] = True
        d["cache_age_sec"] = int(now - _EVAL_RESULT_CACHE["ts"])
        d["cache_ttl_sec"] = _EVAL_CACHE_TTL
        return d
    t0 = time.time()
    try:
        cases = asyncio.run(_eval_cases_async())
    except Exception as e:
        return {"ok": False, "error": repr(e), "cases": [],
                "summary": {"total": len(_EVAL_CASES), "passed": 0,
                            "failed": len(_EVAL_CASES), "duration_ms": 0},
                "generated_at": _utc_now()}
    passed = sum(1 for c in cases if c.get("pass"))
    failed = len(cases) - passed
    result = {
        "ok": failed == 0,
        "generated_at": _utc_now(),
        "cases": cases,
        "summary": {
            "total": len(cases), "passed": passed, "failed": failed,
            "duration_ms": int((time.time() - t0) * 1000),
            "runner": "in-process ASGI（httpx.ASGITransport），走真实 handler + guard，不过网络",
        },
        "how_to_run": ("GET /api/eval_cases；或 systemd timer qunzhi-eval-cases.timer "
                       "每 30 分钟跑一次，失败写 tasks/eval_cases.log"),
        "note": ("新修一个 /api/* 边界就在这里加一条 case：input 原样回显、expect 是人读期望、"
                 "checks 是逐条断言与实测值。pass=false 时整页 ok=false。"),
        "cached": False,
        "cache_ttl_sec": _EVAL_CACHE_TTL,
    }
    _EVAL_RESULT_CACHE.update({"ts": time.time(), "data": result})
    return result


@app.get("/api/eval")
def api_eval():
    """评测数据汇总（120 篇正式评测 + 基线对照 + NLI 标定）"""
    import json as _j

    def load(p):
        try:
            return _j.load(open(p, encoding="utf-8"))
        except Exception:
            return {}

    out = {"ok": True}
    e = load("./paperlib/eval120.json")
    if e:
        acc = e.get("accept") or {}
        rej = e.get("reject") or {}
        n_all = (acc.get("n") or 0) + (rej.get("n") or 0)

        def wavg(k):
            if not n_all:
                return None
            vals = [(acc.get(k), acc.get("n") or 0), (rej.get(k), rej.get("n") or 0)]
            tot = sum(w for _, w in vals)
            return round(sum((v or 0) * w for v, w in vals) / tot, 1) if tot else None

        out["eval120"] = {"n": n_all,
                          "avg_items": wavg("avg_items"),
                          "coverage": wavg("cov_class"),
                          "hit_rate": wavg("hit_rate"),
                          "anchor": wavg("anchor"),
                          "baseline": wavg("baseline"),
                          "accept": acc, "reject": rej}
        out["eval120"]["generated_at"] = e.get("generated_at")
    nb = load("./paperlib/v18_baseline_eval.json")
    if nb.get("summary"):
        out["baseline_rules"] = nb["summary"]
    nli = load("./paperlib/nli_bench.json")
    if nli.get("summary"):
        out["nli_bench"] = nli["summary"]
    an = load("./paperlib/anchor_eval.json")
    if an:
        out["anchor_eval"] = {
            "n_papers": an.get("papers_ok"),
            "n_items": an.get("total_items"),
            "n_anchored": an.get("total_anchored"),
            "anchor_rate": an.get("anchor_rate"),
            "avg_items": an.get("avg_items_per_paper"),
            "avg_seconds": an.get("avg_seconds"),
            "by_field": an.get("by_field"),
            "generated_at": an.get("generated_at"),
        }
    demo = load("./paperlib/baseline_demo.json")
    if demo:
        out["defect_demo"] = {"total_items": demo.get("total_items"), "totals": demo.get("totals")}
    # 2026-11-22 前端 BUG-005：评测页也要能回放，补 call_id / 快照 / 口径。
    _anchor = out.get("anchor_eval") or {}
    out.update(_call_meta("/api/eval", {},
                          {"description": "eval120 + baseline_rules + nli_bench + anchor_eval",
                           "anchor_n_papers": _anchor.get("n_papers"),
                           "anchor_n_items": _anchor.get("n_items")},
                          {"anchor_rate": _anchor.get("anchor_rate"),
                           "eval120_n": (out.get("eval120") or {}).get("n")},
                          metric="anchor_rate", scope="all"))
    _save_call(out.get("call_id"), out)
    return out


@app.get("/api/runs")
def api_runs(limit: int = 20):
    """任务历史：论文包任务 + agent 运行"""
    import json as _j
    runs = []
    root = "./paperlib/pkg_out"
    if os.path.isdir(root):
        dirs = sorted(os.listdir(root), key=lambda d: os.path.getmtime(os.path.join(root, d)), reverse=True)
        for d in dirs[:limit]:
            p = os.path.join(root, d, "pkg_info.json")
            if not os.path.exists(p):
                continue
            try:
                info = _j.load(open(p, encoding="utf-8"))
            except Exception:
                continue
            runs.append({"kind": "pkg", "job": d, "zip": info.get("zip"),
                         "files": info.get("files"), "created_at": info.get("created_at"),
                         "url": "/pkg/" + d})
    agent_root = "./paperlib/runs"
    if os.path.isdir(agent_root):
        for d in sorted(os.listdir(agent_root), key=lambda x: os.path.getmtime(os.path.join(agent_root, x)), reverse=True)[:limit]:
            hp = os.path.join(agent_root, d, "handoff.md")
            if os.path.exists(hp):
                runs.append({"kind": "agent", "run": d,
                             "at": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(hp))),
                             "url": None})
    return {"ok": True, "n": len(runs), "runs": runs[:limit]}


@app.get("/eval", response_class=HTMLResponse)
def eval_page():
    p = os.path.join(V15_ROOT, "app", "templates", "eval.html")
    if os.path.exists(p):
        return FileResponse(p)
    return HTMLResponse("<h1>评测页未部署</h1>", status_code=404)


@app.get("/runs", response_class=HTMLResponse)
def runs_page():
    p = os.path.join(V15_ROOT, "app", "templates", "runs.html")
    if os.path.exists(p):
        return FileResponse(p)
    return HTMLResponse("<h1>任务历史页未部署</h1>", status_code=404)


QZ_USER_KEYS = set(k.strip() for k in (os.environ.get("QZ_USER_KEYS") or "").split(",") if k.strip())
QZ_ADMIN_KEY = os.environ.get("QZ_ADMIN_KEY") or ""
if not QZ_ADMIN_KEY:
    raise RuntimeError("QZ_ADMIN_KEY 未配置")


def _admin_key_ok(k: str) -> bool:
    """管理 key 常量时间比较；空值一律不通过。"""
    return bool(k) and bool(QZ_ADMIN_KEY) and hmac.compare_digest(str(k), str(QZ_ADMIN_KEY))


def _auth_user(request: Request):
    """用户 key 校验：环境变量白名单 + 动态 key 库（enabled 才放行）"""
    k = (request.headers.get("X-Api-Key") or request.headers.get("X-QunZhi-Key")
         or request.query_params.get("key") or "")
    # 演示 key：可公开、低限额；与主访问口令分开，且不写进用户 key 库。
    if _guard.DEMO_KEY and k == _guard.DEMO_KEY:
        _guard._check_demo_quota()
        return k
    if k in QZ_USER_KEYS:
        return k
    if not k:
        raise HTTPException(status_code=401, detail="缺少 key（X-Api-Key 或 X-QunZhi-Key）")
    try:
        sys.path.insert(0, "/home/ubuntu/paperlib")
        from admin_store import Store
        rec = Store().verify(k)
    except Exception:
        rec = None
    if rec and rec.get("disabled"):
        raise HTTPException(status_code=403, detail="该 key 已被停用")
    if not rec:
        raise HTTPException(status_code=401, detail="无效的用户 key")
    try:
        from admin_store import Store as _S
        t = rec.get("tenant") or "default"
        tt = _S().conn().execute("select enabled from qz_tenants where name=?", (t,)).fetchone()
        if tt and not tt["enabled"]:
            raise HTTPException(status_code=403, detail="tenant_blocked: 所属单位已停用")
    except HTTPException:
        raise
    except Exception:
        pass
    # 配额（0 = 不限）
    if rec.get("quota_daily"):
        try:
            today = time.strftime("%Y-%m-%d")
            n = Store().conn().execute(
                "select count(*) from qz_usage where day=? and key_name=?",
                (today, rec["name"])).fetchone()[0]
            if n > int(rec["quota_daily"]):
                raise HTTPException(status_code=429, detail="该 key 今日配额已用完")
        except HTTPException:
            raise
        except Exception:
            pass
    return k


def _auth_admin(request: Request):
    k = request.headers.get("X-QunZhi-Admin") or request.query_params.get("admin_key") or ""
    if not _admin_key_ok(k):
        raise HTTPException(status_code=401, detail="无效的管理 key")
    return k


@app.get("/portal", response_class=HTMLResponse)
def portal_page():
    """用户端入口（给学校平台/外部用户）"""
    p = os.path.join(V15_ROOT, "app", "templates", "portal.html")
    if os.path.exists(p):
        return FileResponse(p)
    return HTMLResponse("<h1>用户端未部署</h1>", status_code=404)


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    """管理端入口"""
    p = os.path.join(V15_ROOT, "app", "templates", "admin.html")
    if os.path.exists(p):
        return FileResponse(p)
    return HTMLResponse("<h1>管理端未部署</h1>", status_code=404)


@app.post("/api/v1/search")
def api_v1_search(payload: dict, request: Request):
    _auth_user(request)
    out = kb_search_hybrid(payload)
    # 2026-11-22 前端 BUG-005：对外检索接口补 call_id，可 /api/call/{id} 回放。
    p = payload or {}
    try:
        _limit = max(1, min(int(p.get("limit") or 8), 30))
    except Exception:
        _limit = 8
    out.update(_call_meta("/api/v1/search",
                          {"q": (p.get("q") or "").strip(), "limit": _limit},
                          {"description": "hybrid retrieval items"},
                          {"n": out.get("n")},
                          metric="search_results", scope="all"))
    _save_call(out.get("call_id"), out)
    return out


@app.post("/api/v1/review")
def api_v1_review(payload: dict, request: Request):
    _auth_user(request)
    import subprocess as _sp
    t = (payload or {}).get("text") or ""
    if len(t) < 200:
        raise HTTPException(status_code=400, detail="text 至少 200 字符")
    body = json.dumps({"title": payload.get("title") or "untitled", "text": t[:80000],
                       "mode": (payload.get("mode") or "fast"),
                       "venue": payload.get("venue") or "all",
                       "max_items": max(8, int(payload.get("max_items") or 16)),
                       "field": payload.get("field")}).encode()
    req = urllib.request.Request("http://127.0.0.1:18097/review", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        raise HTTPException(status_code=502, detail=f"自检引擎返回 {e.code}：{detail}")
    except Exception as e:
        raise HTTPException(status_code=504, detail=f"自检引擎不可用：{type(e).__name__}")


@app.get("/api/v1/paper/{pid}")
def api_v1_paper(pid: str, request: Request):
    _auth_user(request)
    with _kb_conn() as c:
        r = c.execute("select pid,title,year,field,venue,abstract,decision from papers where pid=?", (pid,)).fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="论文不存在")
    return dict(r)


_JOB_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def _safe_job(job: str) -> str:
    """校验 pkg 任务名，防目录穿越（2026-11-19 审计后统一入口）"""
    j = (job or "").strip()
    if (not j) or j in (".", "..") or ("/" in j) or ("\\" in j) or (".." in j) or not _JOB_RE.match(j):
        raise HTTPException(status_code=404, detail="任务不存在")
    return j


def _auth_scope(request: Request) -> dict:
    """返回身份作用域：
       {"role":"super"} 或 {"role":"tenant_admin","tenant":"学校","key_id":N}
       也接受 user 角色（但 user 无管理权限）"""
    ak = (request.headers.get("X-QunZhi-Admin")
          or request.query_params.get("admin_key") or "")
    if ak and _admin_key_ok(ak):
        return {"role": "super"}
    k = (request.headers.get("X-Api-Key") or request.headers.get("X-QunZhi-Key")
         or request.query_params.get("key") or "")
    if not k:
        raise HTTPException(status_code=401, detail="需要管理 key")
    try:
        sys.path.insert(0, "/home/ubuntu/paperlib")
        from admin_store import Store
        rec = Store().verify(k)
    except Exception:
        rec = None
    if not rec or rec.get("disabled"):
        raise HTTPException(status_code=401, detail="无效或已停用的 key")
    if rec.get("role") not in ("admin", "tenant_admin"):
        raise HTTPException(status_code=403, detail="该 key 没有管理权限")
    try:
        from admin_store import Store as _S2
        t2 = rec.get("tenant") or "default"
        tt2 = _S2().conn().execute("select enabled from qz_tenants where name=?", (t2,)).fetchone()
        if tt2 and not tt2["enabled"]:
            raise HTTPException(status_code=403, detail="tenant_blocked: 所属单位已停用")
    except HTTPException:
        raise
    except Exception:
        pass
    return {"role": "tenant_admin", "tenant": rec.get("tenant") or "default", "key_id": rec["id"]}


def _need_super(scope: dict):
    if scope.get("role") != "super":
        raise HTTPException(status_code=403, detail="仅平台管理员可操作")


@app.get("/api/admin/overview")
def api_admin_overview(request: Request):
    _z = _auth_scope(request)
    import socket as _sock
    sys.path.insert(0, "/home/ubuntu/paperlib")
    from admin_store import Store
    st = Store()
    superuser = _z.get("role") == "super"
    tenant = None if superuser else _z.get("tenant")
    try:
        lib = json.load(open("./paperlib/jobs_status.json", encoding="utf-8"))
    except Exception:
        lib = {}
    svc = {"gateway": 200}
    for name, port in (("v18", 18097), ("rerank", 18098), ("dsh", 3080), ("nginx", 443)):
        try:
            with _sock.create_connection(("127.0.0.1", port), timeout=1.5):
                svc[name] = 200
        except Exception:
            svc[name] = 0
    keys = st.list_keys(tenant)
    usage = st.usage_summary(7, tenant)
    out = {"ok": True, "scope": _z, "lib": lib, "services": svc,
           "keys": {"n": len(keys), "active_today": sum(1 for k in keys if k["today_calls"] > 0),
                    "disabled": sum(1 for k in keys if not k["enabled"])},
           "usage": usage, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    if superuser:
        out["tenants"] = st.list_tenants()
    else:
        mine = [t for t in st.list_tenants() if t["name"] == tenant]
        out["tenant"] = mine[0] if mine else None
    return out


@app.get("/api/admin/keys")
def api_admin_keys(request: Request):
    _z = _auth_scope(request)
    sys.path.insert(0, "/home/ubuntu/paperlib")
    from admin_store import Store
    tenant = None if _z.get("role") == "super" else _z.get("tenant")
    return {"ok": True, "scope": _z, "keys": Store().list_keys(tenant)}


@app.post("/api/admin/keys")
def api_admin_keys_create(payload: dict, request: Request):
    _z = _auth_scope(request)
    sys.path.insert(0, "/home/ubuntu/paperlib")
    from admin_store import Store
    p = payload or {}
    name = (p.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="缺少 name（用途，如：计算机学院）")
    st = Store()
    if _z.get("role") == "super":
        role = (p.get("role") or "user").strip()
        tenant = (p.get("tenant") or "default").strip()
        quota = int(p.get("quota_daily") or 0)
    else:
        # 租户管理员：只能建本租户的普通 key，不能建管理员，配额受租户上限约束
        role = "user"
        tenant = _z.get("tenant")
        quota = int(p.get("quota_daily") or 0)
        tl = [t for t in st.list_tenants() if t["name"] == tenant]
        cap = (tl[0]["quota_daily"] if tl else 0) or 0
        used = st.tenant_today(tenant)
        if cap and quota > cap:
            raise HTTPException(status_code=400, detail=f"配额不能超过租户上限 {cap}")
        if cap and used >= cap:
            raise HTTPException(status_code=429, detail="本租户今日额度已用完")
    rec = st.create_key(name, role, quota, tenant)
    st.audit(_z.get("role") + ":" + str(_z.get("tenant") or "-"), "create_key",
             f"{name} role={role} tenant={tenant} quota={quota}")
    return {"ok": True, **rec, "warning": "明文 key 只显示这一次，请立即保存"}


@app.post("/api/admin/keys/{kid}/toggle")
def api_admin_key_toggle(kid: int, payload: dict, request: Request):
    _z = _auth_scope(request)
    sys.path.insert(0, "/home/ubuntu/paperlib")
    from admin_store import Store
    st = Store()
    row = st.conn().execute("select tenant, role from qz_keys where id=?", (kid,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="key 不存在")
    if _z.get("role") != "super":
        if row["tenant"] != _z.get("tenant"):
            raise HTTPException(status_code=403, detail="只能操作本单位的 key")
        if row["role"] in ("admin", "tenant_admin"):
            raise HTTPException(status_code=403, detail="租户管理员不能停用管理员 key")
    enabled = bool((payload or {}).get("enabled", True))
    st.set_enabled(kid, enabled)
    st.audit(_z.get("role") + ":" + str(_z.get("tenant") or "-"), "toggle_key",
             f"id={kid} enabled={enabled}")
    return {"ok": True, "id": kid, "enabled": enabled}


@app.delete("/api/admin/keys/{kid}")
def api_admin_key_delete(kid: int, request: Request):
    _z = _auth_scope(request)
    sys.path.insert(0, "/home/ubuntu/paperlib")
    from admin_store import Store
    st = Store()
    row = st.conn().execute("select tenant, role from qz_keys where id=?", (kid,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="key 不存在")
    if _z.get("role") != "super":
        if row["tenant"] != _z.get("tenant"):
            raise HTTPException(status_code=403, detail="只能删除本单位的 key")
        if row["role"] in ("admin", "tenant_admin"):
            raise HTTPException(status_code=403, detail="租户管理员不能删除管理员 key")
    st.delete_key(kid)
    st.audit(_z.get("role") + ":" + str(_z.get("tenant") or "-"), "delete_key", f"id={kid}")
    return {"ok": True, "id": kid}


@app.get("/api/admin/usage")
def api_admin_usage(request: Request, days: int = 7):
    _z = _auth_scope(request)
    sys.path.insert(0, "/home/ubuntu/paperlib")
    from admin_store import Store
    st = Store()
    tenant = None if _z.get("role") == "super" else _z.get("tenant")
    return {"ok": True, "scope": _z, "summary": st.usage_summary(days, tenant),
            "recent": st.recent_usage(50, tenant)}


@app.get("/api/admin/audit")
def api_admin_audit(request: Request):
    _z = _auth_scope(request)
    _need_super(_z)
    sys.path.insert(0, "/home/ubuntu/paperlib")
    from admin_store import Store
    return {"ok": True, "items": Store().recent_audit(50)}


@app.get("/api/admin/tasks")
def api_admin_tasks(request: Request, limit: int = 30):
    _z = _auth_scope(request)
    _need_super(_z)  # 2026-11-19 修：原来任何 tenant_admin 都能枚举全部任务
    import glob as _g
    out = []
    files = sorted(_g.glob("./paperlib/pkg_out/*/pkg_info.json"),
                   key=os.path.getmtime, reverse=True)[:limit]
    for p in files:
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        out.append({"job": d.get("job"), "zip": d.get("zip"), "files": d.get("files"),
                    "at": d.get("created_at"), "url": "/pkg/" + str(d.get("job"))})
    return {"ok": True, "scope": _z, "tasks": out}


@app.delete("/api/admin/tasks/{job}")
def api_admin_task_delete(job: str, request: Request):
    _z = _auth_scope(request)
    _need_super(_z)
    import shutil as _sh
    d = os.path.join("./paperlib/pkg_out", job)
    # 2026-11-19 修：原来的 ".." 判断漏掉单个 "."，"." 会通过校验并把 d 归一化成 pkg_out 本身
    if (not job) or job in (".", "..") or ("/" in job) or ("\\" in job) or (".." in job) or not re.match(r"^[A-Za-z0-9_.\-]+$", job):
        raise HTTPException(status_code=404, detail="任务不存在")
    if not os.path.isdir(d) or os.path.realpath(d) == os.path.realpath("./paperlib/pkg_out"):
        raise HTTPException(status_code=404, detail="任务不存在")
    _sh.rmtree(d, ignore_errors=True)
    sys.path.insert(0, "/home/ubuntu/paperlib")
    from admin_store import Store
    Store().audit("super", "delete_task", job)
    return {"ok": True, "job": job}


@app.get("/api/admin/tenants")
def api_admin_tenants(request: Request):
    _z = _auth_scope(request)
    sys.path.insert(0, "/home/ubuntu/paperlib")
    from admin_store import Store
    st = Store()
    if _z.get("role") == "super":
        return {"ok": True, "tenants": st.list_tenants()}
    return {"ok": True, "tenants": [t for t in st.list_tenants() if t["name"] == _z.get("tenant")]}


@app.post("/api/admin/tenants")
def api_admin_tenant_upsert(payload: dict, request: Request):
    _z = _auth_scope(request)
    _need_super(_z)
    sys.path.insert(0, "/home/ubuntu/paperlib")
    from admin_store import Store
    p = payload or {}
    name = (p.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="缺少 name（单位名）")
    st = Store()
    st.set_tenant(name, quota_daily=int(p.get("quota_daily") or 0),
                  enabled=(True if p.get("enabled", True) else False), note=p.get("note"))
    st.audit("super", "upsert_tenant", f"{name} quota={p.get('quota_daily')}")
    return {"ok": True, "name": name}

@app.get("/api/weekly/export")
def api_weekly_export(format: str = "bib", n: int = 15):
    """把本周论文导出为 BibTeX / JSON，供综述台（/reviewbench/）一键导入"""
    import glob as _g
    wd = "./paperlib/weekly"
    n = max(1, min(int(n or 15), 50))
    week = ""
    items = []

    # ① 优先 v18 周报的 per_paper（带 pid，质量最高）
    try:
        d18 = json.load(open(os.path.join(wd, "weekly_v18.json"), encoding="utf-8"))
        week = d18.get("generated_at", "")[:10]
        for it in (d18.get("per_paper") or [])[:n]:
            if it.get("pid"):
                items.append({"pid": it["pid"], "title": it.get("title") or "", "field": it.get("field") or ""})
    except Exception:
        pass

    # ② 用 latest 的 top_titles 补到 n 篇（按标题反查 pid）
    if len(items) < n:
        try:
            dl = json.load(open(os.path.join(wd, "weekly_latest.json"), encoding="utf-8"))
            week = dl.get("week") or week
            with _kb_conn() as c:
                for t in (dl.get("top_titles") or []):
                    if not t:
                        continue
                    row = c.execute("select pid, title, field from papers where title=? limit 1", (t,)).fetchone()
                    if row:
                        items.append({"pid": row["pid"], "title": row["title"], "field": row["field"]})
                    if len(items) >= n:
                        break
        except Exception:
            pass

    # ③ 补元数据
    out = []
    with _kb_conn() as c:
        for it in items[:n]:
            row = c.execute("""select pid, title, abstract, year, field, venue, source_url, tldr, citation_count
                               from papers where pid=?""", (it["pid"],)).fetchone()
            if not row:
                continue
            out.append(dict(row))

    if format == "json":
        return {"ok": True, "week": week, "n": len(out), "items": out}

    def esc(x):
        return str(x or "").replace("{", "(").replace("}", ")").strip()

    lines = [f"% 群智 · 每周必读（{week}）", f"% 共 {len(out)} 篇｜来源：群智论文库（37 万篇 CS 论文）", ""]
    for r in out:
        key = str(r["pid"]).replace("_", "")
        lines.append("@article{%s," % key)
        lines.append("  title = {%s}," % esc(r["title"]))
        lines.append("  author = {群智论文库},")
        if r.get("year"):
            lines.append("  year = {%s}," % r["year"])
        if r.get("venue"):
            lines.append("  journal = {%s}," % esc(r["venue"]))
        elif r.get("field"):
            lines.append("  journal = {%s}," % esc(r["field"]))
        if r.get("abstract"):
            lines.append("  abstract = {%s}," % esc((r["abstract"] or "")[:1200]))
        if r.get("field"):
            lines.append("  keywords = {%s}," % esc(r["field"]))
        if r.get("source_url"):
            lines.append("  url = {%s}," % esc(r["source_url"]))
        lines.append("  note = {群智周报 %s}" % esc(week))
        lines.append("}")
        lines.append("")

    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("\n".join(lines), media_type="application/x-bibtex; charset=utf-8",
                             headers={"Content-Disposition": 'attachment; filename="qunzhi-weekly.bib"'})

@app.get("/api/review/bib")
def api_review_bib(pids: str = "", n: int = 30):
    """把选中的论文导出为 BibTeX（note 里带群智问题点数，体现库的来源）"""
    import re as _re
    plist = [p.strip() for p in _re.split(r"[,\s]+", pids or "") if p.strip()][:max(1, min(n, 50))]
    rows = []
    with _kb_conn() as c:
        for pid in plist:
            row = c.execute("""select pid, title, abstract, year, field, venue, source_url,
                                      (select count(*) from issues i where i.pid=papers.pid) as n_issues
                               from papers where pid=?""", (pid,)).fetchone()
            if row:
                rows.append(dict(row))

    def esc(x):
        return str(x or "").replace("{", "(").replace("}", ")").strip()

    lines = [f"% 群智 · 选题导出（{len(rows)} 篇）", "% 来源：群智论文库 37 万篇 CS 论文", ""]
    for r in rows:
        key = str(r["pid"]).replace("_", "")
        lines.append("@article{%s," % key)
        lines.append("  title = {%s}," % esc(r["title"]))
        lines.append("  author = {群智论文库},")
        if r.get("year"):
            lines.append("  year = {%s}," % r["year"])
        lines.append("  journal = {%s}," % esc(r.get("venue") or r.get("field") or "arXiv"))
        if r.get("abstract"):
            lines.append("  abstract = {%s}," % esc((r["abstract"] or "")[:1200]))
        if r.get("field"):
            lines.append("  keywords = {%s}," % esc(r["field"]))
        if r.get("source_url"):
            lines.append("  url = {%s}," % esc(r["source_url"]))
        lines.append("  note = {群智库 · 已知问题点 %s 条}," % (r.get("n_issues") or 0))
        lines.append("}")
        lines.append("")

    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("\n".join(lines), media_type="application/x-bibtex; charset=utf-8",
                             headers={"Content-Disposition": 'attachment; filename="qunzhi-review.bib"'})


@app.get("/review", response_class=HTMLResponse)
def review_lab_page():
    """综述工作区：从群智库选题 → 导出 BibTeX → 进综述台"""
    p = os.path.join(V15_ROOT, "app", "templates", "review_lab.html")
    if os.path.exists(p):
        return FileResponse(p)
    return HTMLResponse("<h1>综述工作区未部署</h1>", status_code=404)

_ARXIV_ID_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/([^?#]+)", re.I)
_ARXIV_ID_OK = re.compile(r"(?:\d{4}\.\d{4,5}|[A-Za-z][A-Za-z\-]*(?:\.[A-Za-z]{2})?/\d{7})\Z")


def _arxiv_id_from_url(url, pid=""):
    """从 source_url 提取 arXiv ID；source_url 为空时从 pid 兜底。

    https://arxiv.org/abs/2609.10996v1 -> 2609.10996
    https://arxiv.org/pdf/hep-th/9901001v2.pdf -> hep-th/9901001
    pid ax_2609.34130（source_url 为空）-> 2609.34130   # PID_ARXIV_FALLBACK
    """
    m = _ARXIV_ID_RE.search(url or "")
    if m:
        aid = m.group(1).strip()
        aid = re.sub(r"\.pdf\Z", "", aid, flags=re.I)
        aid = re.sub(r"v\d+\Z", "", aid)
        if _ARXIV_ID_OK.match(aid):
            return aid
    # PID_ARXIV_FALLBACK
    if pid:
        m2 = __import__("re").match(r"^ax_(\d{4}\.\d{4,5})$", (pid or "").strip())
        if m2:
            return m2.group(1)
    return None


_ARXIV_HTML_CACHE = {}
_ARXIV_HTML_TTL = 600


@app.get("/api/arxiv/html_url")
def api_arxiv_html_url(arxiv_id: str = ""):
    """探测 arXiv HTML 版是否可用（供 /read iframe 用）。

    先试不带版本号，再试 v1 / v2；只发 HEAD（4 秒超时），结果缓存 10 分钟。
    返回 ok=True 时给出可直接嵌入的 URL。
    """
    aid = re.sub(r"v\d+\Z", "", (arxiv_id or "").strip())
    if not _ARXIV_ID_OK.match(aid):
        return {"ok": False, "arxiv_id": aid or None, "reason": "bad_id"}
    hit = _ARXIV_HTML_CACHE.get(aid)
    if hit and time.time() - hit[0] < _ARXIV_HTML_TTL:
        return hit[1]
    tried = []
    for suffix in ("", "v1", "v2"):
        u = "https://arxiv.org/html/%s%s" % (aid, suffix)
        try:
            req = urllib.request.Request(u, method="HEAD", headers={
                "User-Agent": "Mozilla/5.0 (compatible; QunZhi/1.0)",
                "Accept": "text/html,application/xhtml+xml",
            })
            with urllib.request.urlopen(req, timeout=4) as r:
                status = getattr(r, "status", 200)
        except Exception as e:
            status = getattr(e, "code", 0) or 0
        tried.append({"url": u, "status": status})
        if status == 200:
            out = {"ok": True, "arxiv_id": aid, "url": u, "status": 200, "tried": tried}
            _ARXIV_HTML_CACHE[aid] = (time.time(), out)
            return out
    out = {"ok": False, "arxiv_id": aid, "reason": "not_found", "tried": tried}
    _ARXIV_HTML_CACHE[aid] = (time.time(), out)
    return out


# ==================== /api/arxiv/html/<id>：HTML 后端代理 ====================
# 背景：arxiv.org/html 没有 X-Frame-Options / CSP frame-ancestors，但浏览器
# iframe 里会被反爬识别成空白。改由后端带完整浏览器 UA 抓取，把资源路径重写成
# 绝对 URL 后同源返回，iframe 只请求本网关。
_ARXIV_PROXY_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36")
_ARXIV_PROXY_TTL = 600          # 10 分钟缓存
_ARXIV_PROXY_TIMEOUT = 45       # 上游超时 45 秒（实测 arxiv 1.1MB 需 30 秒）
_ARXIV_PROXY_RATE_WINDOW = 60   # 限流窗口
_ARXIV_PROXY_RATE_MAX = 10      # 每 IP 每 60 秒最多 10 次
_ARXIV_PROXY_CACHE = {}         # aid -> (ts, final_url, rewritten_html)
_ARXIV_PROXY_HITS = {}          # ip -> [ts]
_ARXIV_PROXY_LOCK = threading.Lock()

_ARXIV_MATH_RE = re.compile(r"<math\b.*?</math>", re.I | re.S)
_ARXIV_ATTR_RE = re.compile(r'(\b(?:src|href|poster|data)\s*=\s*)(["\'])(.*?)\2', re.I | re.S)
_ARXIV_SRCSET_RE = re.compile(r'(\bsrcset\s*=\s*)(["\'])(.*?)\2', re.I | re.S)
_ARXIV_ANCHOR_RE = re.compile(r"<a\b[^>]*>", re.I | re.S)
_ARXIV_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*:", re.I)
# QBUG-006：外来 HTML 净化。注入 arXiv 活页时剥离所有脚本 / 内联事件 / javascript: URL，
# 只保留排版与静态资源；加载完成信号由我们自己的可信脚本发。
_ARXIV_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.I | re.S)
_ARXIV_SCRIPT_SELF_RE = re.compile(r"<script\b[^>]*?/\s*>", re.I)
_ARXIV_EVENT_ATTR_RE = re.compile(r"\s+on[a-z0-9_\-]+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)
_ARXIV_JS_URL_RE = re.compile(r"(\b(?:href|src)\s*=\s*[\"'])\s*javascript:[^\"']*([\"'])", re.I)
# 外来页面里的嵌套 iframe 一并剥掉：只读排版不需要，且会触发 frame-src CSP 报错
_ARXIV_IFRAME_RE = re.compile(r"<iframe\b[^>]*>.*?</iframe\s*>", re.I | re.S)
_ARXIV_IFRAME_SELF_RE = re.compile(r"<iframe\b[^>]*?/\s*>", re.I)
_ARXIV_PROXY_CSP = ("default-src 'none'; img-src https: data:; "
                    "object-src https: data:; media-src https:; "
                    "style-src 'unsafe-inline' https:; font-src https: data:; "
                    "script-src 'sha256-UuBzAzuSQolhZogtuPB+K4/mJOMWYtXrX9ffPgIbHkc='; connect-src 'none'; "
                    "frame-src https://arxiv.org; base-uri 'none'; form-action 'none'")


def _arx_proxy_client_ip(request):
    """取真实客户端 IP。

    nginx 用 `$proxy_add_x_forwarded_for`（追加模式），XFF 的**最后一段**才是
    nginx 看到的真实来源；第一段可被客户端伪造，不能用（否则限流一绕就过）。
    """
    xff = request.headers.get("X-Forwarded-For") or ""
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    if parts:
        return parts[-1]
    return request.client.host if request.client else "unknown"


def _arx_proxy_rate_ok(ip):
    """滑动窗口限流：每 IP 60 秒 ≤ 10 次（含缓存命中，防刷）"""
    now = time.time()
    with _ARXIV_PROXY_LOCK:
        hits = [t for t in _ARXIV_PROXY_HITS.get(ip, [])
                if now - t < _ARXIV_PROXY_RATE_WINDOW]
        if len(hits) >= _ARXIV_PROXY_RATE_MAX:
            _ARXIV_PROXY_HITS[ip] = hits
            return False
        hits.append(now)
        _ARXIV_PROXY_HITS[ip] = hits
        if len(_ARXIV_PROXY_HITS) > 2000:      # 清理 10 分钟没动静的 IP，防内存膨胀
            for k in list(_ARXIV_PROXY_HITS):
                v = _ARXIV_PROXY_HITS.get(k) or []
                if not v or now - v[-1] > 600:
                    _ARXIV_PROXY_HITS.pop(k, None)
    return True


def _arx_proxy_cache_get(aid):
    now = time.time()
    with _ARXIV_PROXY_LOCK:
        hit = _ARXIV_PROXY_CACHE.get(aid)
        if not hit:
            return None
        if now - hit[0] >= _ARXIV_PROXY_TTL:
            _ARXIV_PROXY_CACHE.pop(aid, None)
            return None
        return hit


def _arx_proxy_cache_put(aid, final_url, html):
    now = time.time()
    with _ARXIV_PROXY_LOCK:
        _ARXIV_PROXY_CACHE[aid] = (now, final_url, html)
        if len(_ARXIV_PROXY_CACHE) > 120:     # 单页约 300KB，控制内存上限
            for k in list(_ARXIV_PROXY_CACHE):
                if now - _ARXIV_PROXY_CACHE[k][0] >= _ARXIV_PROXY_TTL:
                    _ARXIV_PROXY_CACHE.pop(k, None)
            while len(_ARXIV_PROXY_CACHE) > 120:
                _ARXIV_PROXY_CACHE.pop(next(iter(_ARXIV_PROXY_CACHE)), None)


def _arx_proxy_fetch(aid):
    """抓 arXiv HTML（CURL_FETCH：curl 子进程，urllib 读 1MB 要 120s+，curl 40s）。

    返回 (final_url, html_bytes, None) 或 (None, None, reason)。
    """
    import subprocess as _sp
    deadline = time.time() + _ARXIV_PROXY_TIMEOUT
    reason = "not_found"
    for suffix in ("", "v1", "v2"):
        url = "https://arxiv.org/html/%s%s" % (aid, suffix)
        remain = deadline - time.time()
        if remain <= 0:
            return None, None, "timeout"
        try:
            p = _sp.run(
                ["curl", "-sS", "-L", "--max-time", str(max(5, int(remain))),
                 "-A", _ARXIV_PROXY_UA, "-o", "-", "-w",
                 "\n__META__%{http_code}__%{content_type}__%{url_effective}", url],
                capture_output=True, timeout=max(5, int(remain) + 5))
            raw = p.stdout
            idx = raw.rfind(b"\n__META__")
            if idx < 0:
                reason = "curl_no_meta"
                continue
            data = raw[:idx]
            meta = raw[idx + 1:].decode("utf-8", "ignore").split("__")
            status = meta[2] if len(meta) > 2 else "0"
            _ct = (meta[3] if len(meta) > 3 else "").lower()
            final_url = meta[4] if len(meta) > 4 else url
            if status != "200":
                reason = "HTTP %s" % status
                continue
            if "html" not in _ct:
                reason = "bad_content_type: %s" % _ct
                continue
            if not data:
                reason = "empty_body"
                continue
            return final_url, data, None
        except Exception as _e:
            reason = "%s: %s" % (type(_e).__name__, str(_e)[:80])
            continue
    return None, None, reason


def _arx_proxy_fetch_urllib(aid):
    """抓 arXiv HTML：先试无版本号，再试 v1/v2。

    返回 (final_url, html_bytes, None) 或 (None, None, reason)。
    只有 403/404/410 才换版本重试；超时/网络错误立刻放弃，避免 3×20 秒。
    """
    deadline = time.time() + _ARXIV_PROXY_TIMEOUT
    reason = "not_found"
    for suffix in ("", "v1", "v2"):
        url = "https://arxiv.org/html/%s%s" % (aid, suffix)
        remain = deadline - time.time()
        if remain <= 0:
            return None, None, "timeout"
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": _ARXIV_PROXY_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
            })
            with urllib.request.urlopen(req, timeout=max(1.0, min(_ARXIV_PROXY_TIMEOUT, remain))) as r:
                _ct = (r.headers.get("Content-Type") or "").lower()
                data = r.read()
                final_url = r.geturl() or url
            if "html" not in _ct:
                reason = "bad_content_type: %s" % _ct
                continue
            if not data:
                reason = "empty_body"
                continue
            return final_url, data, None
        except urllib.error.HTTPError as e:
            reason = "HTTP %s" % e.code
            if e.code not in (403, 404, 410):
                break
        except urllib.error.URLError as e:
            rsn = str(getattr(e, "reason", e))
            return None, None, ("timeout" if "timed out" in rsn.lower() else "URLError: " + rsn)
        except Exception as e:
            msg = str(e)
            return None, None, ("timeout" if "timed out" in msg.lower() else
                                "%s: %s" % (type(e).__name__, msg))
    return None, None, reason


def _arx_proxy_abs_url(val, final_url):
    """把资源路径变成绝对 URL；#fragment / data: / mailto: 等保持原样。"""
    from urllib.parse import urljoin
    v = (val or "").strip()
    if not v or v.startswith("#"):
        return val
    low = v.lower()
    if low.startswith("http://") or low.startswith("https://"):
        return val
    if _ARXIV_SCHEME_RE.match(v):        # data: / mailto: / javascript: / tel: ...
        return val
    if v.startswith("//"):               # 协议相对
        return "https:" + v
    if v.startswith("/"):                # 站内绝对路径
        return "https://arxiv.org" + v
    return urljoin(final_url, v)         # 相对路径（图片/附件）按原页面 URL 解析


def _arx_proxy_fix_anchor(tag):
    """<a> 加 target=_blank；fragment 锚点（#bib）保持页内跳转。"""
    m = re.search(r'\bhref\s*=\s*(["\'])(.*?)\1', tag, re.I | re.S)
    if not m:
        return tag
    href = (m.group(2) or "").strip()
    if href.startswith("#"):
        return tag
    add = []
    if not re.search(r"\btarget\s*=", tag, re.I):
        add.append('target="_blank"')
    if not re.search(r"\brel\s*=", tag, re.I):
        add.append('rel="noopener noreferrer"')
    if not add:
        return tag
    stripped = tag.rstrip()
    if stripped.endswith("/>"):
        return stripped[:-2] + " " + " ".join(add) + "/>"
    if stripped.endswith(">"):
        return stripped[:-1] + " " + " ".join(add) + ">"
    return tag


def _arx_proxy_rewrite(html, final_url):
    """重写 arXiv HTML：资源路径绝对化，<math> 原样保留。"""
    maths = []

    def _hold(m):
        maths.append(m.group(0))
        return "\x00ARXMATH%d\x00" % (len(maths) - 1)

    out = _ARXIV_MATH_RE.sub(_hold, html)

    def _repl(m):
        pre, q, val = m.group(1), m.group(2), m.group(3)
        return pre + q + _arx_proxy_abs_url(val, final_url) + q

    out = _ARXIV_ATTR_RE.sub(_repl, out)

    def _srcset(m):
        pre, q, val = m.group(1), m.group(2), m.group(3)
        items = []
        for item in val.split(","):
            seg = item.strip()
            if not seg:
                continue
            bits = seg.split()
            bits[0] = _arx_proxy_abs_url(bits[0], final_url)
            items.append(" ".join(bits))
        return pre + q + ", ".join(items) + q

    out = _ARXIV_SRCSET_RE.sub(_srcset, out)
    out = _ARXIV_ANCHOR_RE.sub(lambda m: _arx_proxy_fix_anchor(m.group(0)), out)
    # QBUG-006：剥离外来脚本与内联事件，避免 arXiv 页面代码在本站 origin 下执行。
    out = _ARXIV_SCRIPT_RE.sub("", out)
    out = _ARXIV_SCRIPT_SELF_RE.sub("", out)
    out = _ARXIV_IFRAME_RE.sub("", out)
    out = _ARXIV_IFRAME_SELF_RE.sub("", out)
    out = _ARXIV_EVENT_ATTR_RE.sub("", out)
    out = _ARXIV_JS_URL_RE.sub(lambda m: m.group(1) + "#" + m.group(2), out)
    for i, block in enumerate(maths):
        out = out.replace("\x00ARXMATH%d\x00" % i, block)
    # 加载完成信号：iframe 的 onload 会被初始 about:blank 的 load 事件污染，
    # 由代理页自己 postMessage 通知父窗口，前端据此停掉超时计时器。
    # 必须插在 <body> 之后，不能只插在 </body> 前：arXiv 大页面在 45 秒上限处
    # 会被 curl 截断（实测 14.4MB 的 2609.34130 只拿到约 0.5MB），截断点常落在
    # 标签属性中间，写在结尾的 <script> 会被吞进属性里、永远不执行。
    marker = ('<script>try{parent.postMessage({__arxProxyLoaded:true},'
              '"*")}catch(e){}</script>')
    body = re.search(r"<body\b[^>]*>", out, re.I)
    if body:
        out = out[:body.end()] + marker + out[body.end():]
    else:
        out += marker
    return out


def _arx_proxy_error_page(message, source_url="https://arxiv.org", aid=""):
    """代理失败时返回的极简页：通知父窗口切回纯文本，并给出原文链接。"""
    import json as _json
    msg = str(message or "unknown")
    safe_msg = (msg.replace("&", "&amp;").replace("<", "&lt;")
                  .replace(">", "&gt;").replace('"', "&quot;"))
    safe_src = str(source_url or "https://arxiv.org").replace('"', "%22")
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>arXiv HTML 版暂时不可用</title><style>'
        'body{margin:0;padding:32px 20px;font:15px/1.7 -apple-system,"Segoe UI",'
        'Roboto,"Helvetica Neue",Arial,sans-serif;color:#3b4048;background:#fafbfc}'
        '.box{max-width:560px;margin:8vh auto;padding:22px 24px;background:#fff;'
        'border:1px solid #e5e7eb;border-radius:10px}'
        'h1{font-size:17px;margin:0 0 8px}p{margin:6px 0;color:#6b7280}'
        'a{color:#2563eb}</style></head><body><div class="box">'
        '<h1>arXiv HTML 版暂时加载不了</h1><p>%s</p>'
        '<p>正在自动切回纯文本阅读…（也可以<a href="%s" target="_blank" '
        'rel="noopener noreferrer">在 arXiv 打开 ↗</a>）</p></div>'
        '<script>try{parent.postMessage({__arxProxyError:true,message:%s},"*")}'
        'catch(e){}</script></body></html>'
    ) % (safe_msg, safe_src, _json.dumps(msg, ensure_ascii=False))


# ==================== warm + status：后台预抓，解决首次 44 秒等待 ====================
# 背景：/api/arxiv/html/<id> 首次要 44 秒（拉 arxiv 1MB + 重写资源路径），之后缓存
# 0.25 秒。/read 打开时先显示纯文本并轮询，后台线程抓完再切 iframe。
# 注意：GET /api/arxiv/html/status/<id> 必须注册在 /api/arxiv/html/{arxiv_id:path}
# 之前，否则会被 :path 通配路由吃掉（Starlette/ FastAPI 按注册顺序匹配）。
_WARM_INFLIGHT = {}             # aid -> 开始时间戳（正在抓）
_WARM_FAILED = {}               # aid -> (ts, reason)，短时保留失败原因
_WARM_HITS = {}                 # ip -> [ts]，warm 独立限流桶（不占代理 GET 额度）
_WARM_RATE_WINDOW = 60
_WARM_RATE_MAX = 10
_WARM_FAILED_TTL = 120          # 失败原因保留 2 分钟，避免前端永远等满 60 秒


def _arx_warm_rate_ok(ip):
    """warm 独立限流：每 IP 60 秒 ≤ 10 次"""
    now = time.time()
    with _ARXIV_PROXY_LOCK:
        hits = [t for t in _WARM_HITS.get(ip, [])
                if now - t < _WARM_RATE_WINDOW]
        if len(hits) >= _WARM_RATE_MAX:
            _WARM_HITS[ip] = hits
            return False
        hits.append(now)
        _WARM_HITS[ip] = hits
        if len(_WARM_HITS) > 1000:      # 清理 10 分钟没动静的 IP，防内存膨胀
            for k in list(_WARM_HITS):
                v = _WARM_HITS.get(k) or []
                if not v or now - v[-1] > 600:
                    _WARM_HITS.pop(k, None)
    return True


@app.post("/api/arxiv/html/warm/{arxiv_id}")
def api_arxiv_html_warm(arxiv_id: str, request: Request):
    """后台预抓 arXiv HTML（立即返回，不阻塞 30-45 秒）。

    幂等：已在抓 → warming；已缓存 → ready；否则起 daemon 线程。
    """
    aid = re.sub(r"v\d+\Z", "", (arxiv_id or "").strip())
    if not _ARXIV_ID_OK.match(aid):
        return {"ok": False, "reason": "bad_id", "status": "not_started"}
    ip = _arx_proxy_client_ip(request)
    if not _arx_warm_rate_ok(ip):
        return {"ok": False, "reason": "rate_limited", "status": "not_started",
                "retry_after": _WARM_RATE_WINDOW}

    hit = _arx_proxy_cache_get(aid)
    if hit:
        return {"ok": True, "status": "ready", "cached": True,
                "bytes": len(hit[2])}

    with _ARXIV_PROXY_LOCK:
        if aid in _WARM_INFLIGHT:
            return {"ok": True, "status": "warming",
                    "elapsed": round(time.time() - _WARM_INFLIGHT[aid], 1)}
        _WARM_INFLIGHT[aid] = time.time()
        _WARM_FAILED.pop(aid, None)

    def _do_warm():
        try:
            final_url, data, reason = _arx_proxy_fetch(aid)
            if final_url and data:
                rewritten = _arx_proxy_rewrite(
                    data.decode("utf-8", "replace"), final_url)
                _arx_proxy_cache_put(aid, final_url, rewritten)
            else:
                with _ARXIV_PROXY_LOCK:
                    _WARM_FAILED[aid] = (time.time(),
                                         str(reason or "unknown"))
                    if len(_WARM_FAILED) > 500:
                        now2 = time.time()
                        for k, v in list(_WARM_FAILED.items()):
                            if now2 - v[0] > _WARM_FAILED_TTL:
                                _WARM_FAILED.pop(k, None)
        except Exception as _e:
            with _ARXIV_PROXY_LOCK:
                _WARM_FAILED[aid] = (time.time(), "%s: %s" % (
                    type(_e).__name__, str(_e)[:80]))
        finally:
            with _ARXIV_PROXY_LOCK:
                _WARM_INFLIGHT.pop(aid, None)

    threading.Thread(target=_do_warm, name="arx-warm", daemon=True).start()
    return {"ok": True, "status": "warming"}


@app.get("/api/arxiv/html/status/{arxiv_id}")
def api_arxiv_html_status(arxiv_id: str):
    """查 arXiv HTML 预抓状态：ready / warming / failed / not_started。"""
    aid = re.sub(r"v\d+\Z", "", (arxiv_id or "").strip())
    if not _ARXIV_ID_OK.match(aid):
        return {"ok": False, "reason": "bad_id", "status": "not_started"}
    hit = _arx_proxy_cache_get(aid)
    if hit:
        return {"ok": True, "status": "ready", "cached": True,
                "bytes": len(hit[2])}
    with _ARXIV_PROXY_LOCK:
        started = _WARM_INFLIGHT.get(aid)
        if started is not None:
            return {"ok": True, "status": "warming",
                    "elapsed": round(time.time() - started, 1)}
        failed = _WARM_FAILED.get(aid)
        if failed and time.time() - failed[0] < _WARM_FAILED_TTL:
            return {"ok": True, "status": "failed", "reason": failed[1]}
    return {"ok": True, "status": "not_started"}


@app.get("/api/arxiv/html/{arxiv_id:path}")
def api_arxiv_html_proxy(arxiv_id: str, request: Request):
    """arXiv HTML 后端代理：抓取 → 资源绝对化 → 10 分钟缓存 → 每 IP 10 次/分钟。"""
    aid = re.sub(r"v\d+\Z", "", (arxiv_id or "").strip())
    if not _ARXIV_ID_OK.match(aid):
        return HTMLResponse(
            _arx_proxy_error_page("论文编号不合法：%s" % (arxiv_id or "(空)"),
                                  "https://arxiv.org", aid),
            status_code=400,
            headers={"X-Arxiv-Proxy": "error", "Cache-Control": "no-store"})

    ip = _arx_proxy_client_ip(request)
    if not _arx_proxy_rate_ok(ip):
        return HTMLResponse(
            _arx_proxy_error_page(
                "请求太频繁（每个 IP 每分钟最多 10 次），请稍后再试。",
                "https://arxiv.org/html/%s" % aid, aid),
            status_code=429,
            headers={"X-Arxiv-Proxy": "rate_limited", "Retry-After": "60",
                     "Cache-Control": "no-store"})

    hit = _arx_proxy_cache_get(aid)
    if hit:
        _, final_url, cached_html = hit
        return HTMLResponse(cached_html, headers={
            "X-Arxiv-Proxy": "hit",
            "X-Arxiv-Source": final_url,
            "Cache-Control": "private, max-age=600",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": _ARXIV_PROXY_CSP,
        })

    final_url, data, reason = _arx_proxy_fetch(aid)
    if final_url is None:
        rs = str(reason)
        if rs.startswith("timeout") or "timed out" in rs.lower():
            status = 504
        elif rs.startswith("HTTP 4"):
            status = 404
        else:
            status = 502
        return HTMLResponse(
            _arx_proxy_error_page("上游 arXiv 抓取失败（%s）。" % reason,
                                  "https://arxiv.org/abs/%s" % aid, aid),
            status_code=status,
            headers={"X-Arxiv-Proxy": "error", "Cache-Control": "no-store"})

    html = data.decode("utf-8", "replace")
    rewritten = _arx_proxy_rewrite(html, final_url)
    _arx_proxy_cache_put(aid, final_url, rewritten)
    return HTMLResponse(rewritten, headers={
        "X-Arxiv-Proxy": "miss",
        "X-Arxiv-Source": final_url,
        "Cache-Control": "private, max-age=600",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": _ARXIV_PROXY_CSP,
    })


@app.get("/api/paper/{pid}/text")
def api_paper_text(pid: str, offset: int = 0, limit: int = 100000):
    """读论文全文（库内已抓的 19.6 万篇）。

    默认一次返回全文（上限 200k 字符）；确实还有剩余时返回
    has_more=true + next_offset，避免调用方把后半篇误判为“不存在”。
    """
    import zlib as _zl
    offset = max(0, int(offset or 0))
    limit = max(1000, min(int(limit or 100000), 200000))
    with _kb_conn() as c:
        row = c.execute("""select p.pid, p.title, p.year, p.field, p.venue, p.source_url,
                                  p.abstract, f.z
                           from papers p left join fulltext_z f on f.pid = p.pid
                           where p.pid = ?""", (pid,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="论文不存在")
    d = dict(row)
    text = ""
    zb = d.pop("z", None)
    if zb:
        try:
            text = _zl.decompress(zb).decode("utf-8", "ignore")
        except Exception:
            try:
                text = zb.decode("utf-8", "ignore")
            except Exception:
                text = ""
    raw_total = len(text)
    # PDF 抽出的“视觉行”合并回段落/标题，避免 /read 一句一行
    text = reflow_pdf_text(text) if text else ""
    total = len(text)
    # 分页不要切在词/标题/段落中间：优先退到 \n\n，其次 \n/空格
    end = min(total, offset + limit)
    if offset < total and end < total:
        floor = max(offset + 1, end - 800)
        cut = text.rfind("\n\n", floor, end)
        if cut < 0:
            cut = text.rfind("\n", floor, end)
        if cut < 0:
            cut = max(text.rfind(" ", floor, end), text.rfind("\t", floor, end))
        if cut > offset:
            end = cut
    has_more = end < total
    return {
        "ok": True,
        "pid": d["pid"], "title": d["title"], "year": d["year"], "field": d["field"],
        "venue": d.get("venue"), "url": d.get("source_url"), "abstract": d.get("abstract"),
        "arxiv_id": _arxiv_id_from_url(d.get("source_url"), d.get("pid")),
        "has_fulltext": raw_total > 0,
        "total": total,
        "raw_total": raw_total,
        "offset": offset,
        "returned": end - offset,
        "has_more": has_more,
        "next_offset": end if has_more else None,
        "text": text[offset:end],
        "text_reflowed": True,
    }

# ==================== benchmark 分数（2026-11-21） ====================
# 背景：平台原有 4 个 API 都不返回 Terminal-Bench / BrowseComp / Doc2Repo
# 的 task accuracy，原文的 1.42-8.99 pp 只能维持 T 级。这里新增只读接口，
# 用 benchmark_scores 表把「原文数字（T）」和「平台数字（P）」分栏返回。
_BENCH_PROVENANCE = {
    "T": {
        "source": "paper",
        "reproducible": False,
        "score_replayable": False,
        "note": "原文数字，平台未实测；只能做内部一致性核对，不能在平台回放",
        "verifiable": "internal_consistency_only",
    },
    "P": {
        "source": "platform",
        "reproducible": True,
        "score_replayable": True,
        "note": "平台回查（source=platform/leaderboard/manual），带 call_id 可复算",
        "verifiable": "platform_replay",
    },
}


def _bench_tier(source: str) -> str:
    """source=platform/leaderboard/manual 记为 P 级，其余（paper/NULL）记为 T 级。"""
    return "P" if (source or "").strip().lower() in (
        "platform", "leaderboard", "manual") else "T"


@app.get("/api/benchmark/{paper_id}")
def api_benchmark(paper_id: str, benchmark: str = "", model: str = ""):
    """返回论文的 benchmark 分数（Terminal-Bench 2.1 / BrowseComp / Doc2Repo）。

    - paper_id：论文 ID（如 ax_2611.26510）
    - benchmark：可选过滤（如 Terminal-Bench 2.1）
    - model：可选过滤（如 Qwen3.5-4B）

    T/P 分栏：source=paper 是 T 级原文数字（平台未实测、不可回放）；
    source=platform/leaderboard/manual 是 P 级平台数字（可回查）。
    顶层 call_id 是本接口结果的回查 ID（/api/call/{call_id}），
    与单条分数的平台来源 call_id（source_call_id）不是一回事。

    口径说明：原文只报了 AutoMaster−Vanilla SFT 的 Δ 95% CI（Table A6），
    没有报分数本身的 CI；因此 ci_low/ci_high 是 **Δ 的 CI**，n 是评测任务数，
    响应里用 ci_scope / n_scope 显式标注，不把 Δ CI 冒充成分数 CI。
    """
    bid = (paper_id or "").strip()
    if not bid:
        raise HTTPException(status_code=400, detail="paper_id 不能为空")
    bench_q = (benchmark or "").strip()
    model_q = (model or "").strip()
    where = ["pid = ?"]
    args = [bid]
    if bench_q:
        where.append("benchmark = ?")
        args.append(bench_q)
    if model_q:
        where.append("model = ?")
        args.append(model_q)

    rows, meta, total, table_ready = [], None, 0, True
    with _kb_conn() as c:
        meta = c.execute(
            "select pid, title, venue, source_url from papers where pid = ?",
            (bid,)).fetchone()
        try:
            rows = c.execute(
                "select pid, benchmark, model, method, score, score_std, "
                "ci_low, ci_high, n, source, source_url, call_id, computed_at "
                "from benchmark_scores where " + " and ".join(where) +
                " order by case benchmark when 'Terminal-Bench 2.1' then 1 "
                "when 'BrowseComp' then 2 when 'Doc2Repo' then 3 else 9 end, "
                "model, method", args).fetchall()
            total = c.execute(
                "select count(*) from benchmark_scores where pid = ?",
                (bid,)).fetchone()[0]
        except _sqlite3.OperationalError as e:
            if "no such table" not in str(e).lower():
                raise HTTPException(
                    status_code=500, detail="benchmark_scores 查询失败：%s" % e)
            table_ready = False

    scores, n_t, n_p = [], 0, 0
    for r in rows:
        d = dict(r)
        tier = _bench_tier(d.get("source"))
        prov = dict(_BENCH_PROVENANCE[tier])
        prov["tier"] = tier
        has_ci = d.get("ci_low") is not None and d.get("ci_high") is not None
        if tier == "P":
            n_p += 1
        else:
            n_t += 1
        scores.append({
            "benchmark": d.get("benchmark"),
            "model": d.get("model"),
            "method": d.get("method"),
            "score": d.get("score"),
            "score_std": d.get("score_std"),
            "ci_low": d.get("ci_low"),
            "ci_high": d.get("ci_high"),
            "n": d.get("n"),
            "source": d.get("source"),
            "source_url": d.get("source_url"),
            "source_call_id": d.get("call_id"),
            "tier": tier,
            "provenance": prov,
            "ci_scope": "delta_vs_vanilla_sft_95_bootstrap" if has_ci else None,
            "n_scope": "eval_tasks" if d.get("n") else None,
            "recorded_at": d.get("computed_at"),
        })

    paper = ({"in_library": True, "title": meta["title"], "venue": meta["venue"],
              "source_url": meta["source_url"]} if meta else
             {"in_library": False, "title": None, "venue": None,
              "source_url": None})
    notes = [
        "score / score_std 来自原文 Table 1（mean ± SD，每个 checkpoint 评测 3 次）。",
        "ci_low / ci_high 来自原文 Table A6 的 AutoMaster−Vanilla SFT "
        "95% paired bootstrap CI，是 Δ 的 CI，不是分数本身的 CI；"
        "n 为 Table A6 的评测任务数。原文未报分数 CI，这里不估算、不冒充。",
        "T 级（source=paper）是原文数字，平台未实测、不可平台回放；"
        "P 级（source=platform/leaderboard/manual）才可在平台回查。",
    ]
    if not meta:
        notes.append("pid 不在本地论文库 papers 表中；本接口只读 "
                     "benchmark_scores，不做论文存在性校验。")
    if not table_ready:
        notes.append("benchmark_scores 表尚未创建；请先运行 "
                     "tasks/seed_benchmarks.py。")

    out = {
        "ok": True,
        "paper_id": bid,
        "paper": paper,
        "filters": {"benchmark": bench_q or None, "model": model_q or None},
        "scores": scores,
        "provenance": {
            "T": dict(_BENCH_PROVENANCE["T"], n_scores=n_t),
            "P": dict(_BENCH_PROVENANCE["P"], n_scores=n_p),
        },
        "coverage": {
            "has_benchmark": total > 0,
            "n_scores": len(scores),
            "n_scores_total": total,
            "n_t": n_t,
            "n_p": n_p,
            "benchmarks": sorted({s["benchmark"] for s in scores
                                  if s["benchmark"]}),
            "models": sorted({s["model"] for s in scores if s["model"]}),
            "table": "benchmark_scores",
            "table_ready": table_ready,
        },
        "notes": notes,
    }
    out.update(_call_meta(
        "/api/benchmark/{paper_id}",
        {"paper_id": bid, "benchmark": bench_q or None,
         "model": model_q or None},
        {"n": total, "description":
         "benchmark_scores rows for this paper (before filters)"},
        {"n": len(scores), "description":
         "benchmark_scores rows returned (after filters)"},
        metric="benchmark_score", scope="paper"))
    _save_call(out.get("call_id"), out)
    return out


# ==================== PwC benchmark API（2026-11-23） ====================
# 数据：pwc_scores（Papers with Code 官方 evaluation-tables，205,840 行）。
# 只读、免 key（guard.py 公开白名单）、带 call_id 可回查。
_PWC_NOTE = ("数据来自 Papers with Code 官方 evaluation-tables（HF pwc-archive），"
             "score 是原文报告值，平台未实测；null/inf 这类非有限分数按 null 返回。")


def _pwc_num(v):
    """把 SQLite REAL 转成 JSON 安全数字；inf/nan → None（JSON 不能有 Infinity）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _pwc_filters(dataset="", model="", metric="", task=""):
    """四个可选过滤 → 回显 dict（去掉首尾空白）。"""
    out = {}
    for col, val in (("dataset", dataset), ("model", model),
                     ("metric", metric), ("task", task)):
        v = (val or "").strip()
        out[col] = v or None
    return out


def _pwc_where(filters, dataset_mode="exact"):
    """过滤条件 → (where 子句, 参数)。

    dataset_mode=prefix 时 dataset 用前缀匹配：PwC 把 MMLU 拆成
    "MMLU (College Biology)" 等子集，`dataset=MMLU` 精确查会 0 行；
    接口先精确查，0 行再自动前缀回退，并在 filters.dataset_match 标注。
    model/metric/task 始终精确匹配。
    """
    where, args = [], []
    if filters.get("dataset"):
        if dataset_mode == "prefix":
            where.append("dataset LIKE ?")
            args.append(filters["dataset"] + "%")
        else:
            where.append("dataset = ?")
            args.append(filters["dataset"])
    for col in ("model", "metric", "task"):
        if filters.get(col):
            where.append(col + " = ?")
            args.append(filters[col])
    return (" where " + " and ".join(where) if where else ""), args


def _pwc_limit(v, default, maximum):
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, maximum))


@app.get("/api/pwc/scores")
def api_pwc_scores(dataset: str = "", model: str = "", metric: str = "",
                   task: str = "", limit: int = 50, offset: int = 0):
    """PwC benchmark 分数查询（dataset/model/metric/task 精确过滤 + 分页）。

    limit 默认 50、上限 500；offset 用于翻页。total 是过滤后的总行数。
    返回里的 call_id 是本结果的回查 ID（/api/call/{call_id}），
    同一 call_id + 同一数据快照 → 同一结果。
    """
    filters = _pwc_filters(dataset, model, metric, task)
    wsql, args = _pwc_where(filters)
    match_mode = "exact"
    limit = _pwc_limit(limit, 50, 500)
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0

    with _kb_conn() as c:
        total = c.execute(
            "select count(*) from pwc_scores" + wsql, args).fetchone()[0]
        # dataset 精确 0 行时前缀回退（例：MMLU → MMLU (College Biology) ...）
        if total == 0 and filters.get("dataset"):
            pwsql, pargs = _pwc_where(filters, "prefix")
            ptotal = c.execute(
                "select count(*) from pwc_scores" + pwsql, pargs).fetchone()[0]
            if ptotal:
                total, wsql, args = ptotal, pwsql, pargs
                match_mode = "dataset_prefix_fallback"
        rows = c.execute(
            "select id, task, dataset, metric, model, score, paper_url, "
            "paper_title, year from pwc_scores" + wsql +
            " order by id limit ? offset ?", args + [limit, offset]).fetchall()

    filters["dataset_match"] = match_mode

    items = []
    for r in rows:
        d = dict(r)
        items.append({
            "id": d.get("id"),
            "task": d.get("task"),
            "dataset": d.get("dataset"),
            "metric": d.get("metric"),
            "model": d.get("model"),
            "score": _pwc_num(d.get("score")),
            "paper_url": d.get("paper_url") or None,
            "paper_title": d.get("paper_title") or None,
            "year": d.get("year"),
        })

    out = {
        "ok": True,
        "total": total,
        "returned": len(items),
        "limit": limit,
        "offset": offset,
        "has_more": (offset + len(items)) < total,
        "items": items,
        "filters": filters,
        "source": {"table": "pwc_scores", "dataset": "Papers with Code "
                   "evaluation-tables（HF pwc-archive）", "note": _PWC_NOTE},
    }
    out.update(_call_meta(
        "/api/pwc/scores",
        {"dataset": filters["dataset"], "model": filters["model"],
         "metric": filters["metric"], "task": filters["task"],
         "dataset_match": match_mode,
         "limit": limit, "offset": offset},
        {"n": total, "description": "pwc_scores rows matching filters"},
        {"n": len(items), "description": "rows returned on this page"},
        metric="benchmark_score", scope="pwc_scores"))
    _save_call(out.get("call_id"), out)
    return out


@app.get("/api/pwc/datasets")
def api_pwc_datasets(task: str = "", q: str = "", limit: int = 10000,
                     offset: int = 0):
    """列出所有 PwC benchmark（dataset）+ 分数条数。

    默认返回全部 7,432 个（limit 上限 20,000）；task 精确过滤，q 为名称模糊搜。
    """
    task = (task or "").strip()
    q = (q or "").strip()
    limit = _pwc_limit(limit, 10000, 20000)
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0
    where, args = [], []
    if task:
        where.append("task = ?")
        args.append(task)
    if q:
        where.append("dataset like ?")
        args.append("%" + q + "%")
    wsql = (" where " + " and ".join(where)) if where else ""

    with _kb_conn() as c:
        total = c.execute(
            "select count(distinct dataset) from pwc_scores" + wsql,
            args).fetchone()[0]
        rows = c.execute(
            "select dataset, count(*) as n, count(distinct task) as tasks, "
            "count(distinct model) as models from pwc_scores" + wsql +
            " group by dataset order by n desc, dataset limit ? offset ?",
            args + [limit, offset]).fetchall()

    items = [{"dataset": r["dataset"], "count": r["n"],
              "tasks": r["tasks"], "models": r["models"]} for r in rows]
    out = {
        "ok": True,
        "total": total,
        "returned": len(items),
        "limit": limit,
        "offset": offset,
        "has_more": (offset + len(items)) < total,
        "items": items,
        "filters": {"task": task or None, "q": q or None},
        "source": {"table": "pwc_scores", "note": _PWC_NOTE},
    }
    out.update(_call_meta(
        "/api/pwc/datasets",
        {"task": task or None, "q": q or None,
         "limit": limit, "offset": offset},
        {"n": total, "description": "distinct pwc_scores.dataset matching filters"},
        {"n": len(items), "description": "datasets returned on this page"},
        metric="benchmark_dataset_count", scope="pwc_scores"))
    _save_call(out.get("call_id"), out)
    return out


@app.get("/api/pwc/models")
def api_pwc_models(q: str = "", limit: int = 50000, offset: int = 0):
    """列出所有 PwC 模型 + 分数条数（默认全部，约 3.3 万个）。

    pwc_scores 里有 58 行 model 为空串；它们不进入模型榜，单独在
    empty_model_rows 里报数，避免把空名当成一个模型。
    """
    q = (q or "").strip()
    limit = _pwc_limit(limit, 50000, 100000)
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0
    where = ["model <> ''"]
    args = []
    if q:
        where.append("model like ?")
        args.append("%" + q + "%")
    wsql = " where " + " and ".join(where)

    with _kb_conn() as c:
        total = c.execute(
            "select count(distinct model) from pwc_scores" + wsql,
            args).fetchone()[0]
        rows = c.execute(
            "select model, count(*) as n, count(distinct dataset) as datasets, "
            "count(distinct task) as tasks from pwc_scores" + wsql +
            " group by model order by n desc, model limit ? offset ?",
            args + [limit, offset]).fetchall()
        empty_model_rows = c.execute(
            "select count(*) from pwc_scores where trim(coalesce(model,'')) = ''"
        ).fetchone()[0]

    items = [{"model": r["model"], "count": r["n"],
              "datasets": r["datasets"], "tasks": r["tasks"]} for r in rows]
    out = {
        "ok": True,
        "total": total,
        "returned": len(items),
        "limit": limit,
        "offset": offset,
        "has_more": (offset + len(items)) < total,
        "empty_model_rows": empty_model_rows,
        "items": items,
        "filters": {"q": q or None},
        "source": {"table": "pwc_scores", "note": _PWC_NOTE},
    }
    out.update(_call_meta(
        "/api/pwc/models",
        {"q": q or None, "limit": limit, "offset": offset},
        {"n": total, "description": "distinct non-empty pwc_scores.model"},
        {"n": len(items), "description": "models returned on this page"},
        metric="benchmark_model_count", scope="pwc_scores"))
    _save_call(out.get("call_id"), out)
    return out


@app.get("/outline", response_class=HTMLResponse)
def outline_page():
    """大纲树（树状节点，自己搭骨架、挂证据）"""
    p = os.path.join(V15_ROOT, "app", "templates", "outline.html")
    if os.path.exists(p):
        return FileResponse(p)
    return HTMLResponse("<h1>大纲树未部署</h1>", status_code=404)

_OUT_WORD = [
    ("abstract", r"^abstract$"),
    ("intro", r"^(introduction|overview)$"),
    ("related", r"^(related work|background|preliminaries|related works)$"),
    ("method", r"^(method|methods|methodology|approach|proposed method|model|framework|our approach|proposed approach|problem formulation)$"),
    ("experiment", r"^(experiments?|experimental setup|experimental results|evaluation|evaluation metrics|experiments and results|setup|benchmarks?|datasets?)$"),
    ("discussion", r"^(discussion|analysis|ablation study|ablation|results analysis|limitations?)$"),
    ("conclusion", r"^(conclusion|conclusions|concluding remarks|summary)$"),
    ("refs", r"^(references|bibliography)$"),
]
_OUT_WORD_RE = [(k, re.compile(p, re.I)) for k, p in _OUT_WORD]
_OUT_HINT = [
    ("intro", r"introduct"), ("related", r"related|background|preliminar"),
    ("method", r"method|approach|formulation|framework|model|proposed"),
    ("experiment", r"experiment|evaluation|results|benchmark|dataset|setup"),
    ("discussion", r"discussion|analysis|ablation|limitation"),
    ("conclusion", r"conclusion|summary|remarks"),
]


def _out_classify(title):
    t = (title or "").lower()
    for k, rx in _OUT_HINT:
        if re.search(rx, t):
            return k
    return None


def _reverse_outline(text: str):
    """从全文反推章节结构（支持三种标题格式）"""
    lines = text.split("\n")
    marks = []
    num_line = re.compile(r"^(\d{1,2})(?:\.(\d{1,2}))?$")
    num_title = re.compile(r"^(\d{1,2})(?:\.(\d{1,2}))?\.?\s+([A-Za-z][A-Za-z0-9\s\-:,&/]{2,64})$")

    for i, ln in enumerate(lines):
        s2 = ln.strip()
        if not s2 or len(s2) > 80:
            continue
        if re.search(r"\.{3,}|\|", s2):
            continue
        low = s2.lower()
        hit = None
        for k, rx in _OUT_WORD_RE:
            if rx.match(low):
                hit = k
                break
        if hit:
            marks.append((i, hit, s2[:70]))
            continue
        m = num_title.match(s2)
        if m and 1 <= int(m.group(1)) <= 12:
            if m.group(2):
                continue
            k = _out_classify(m.group(3))
            if k:
                marks.append((i, k, (m.group(1) + " " + m.group(3).strip())[:70]))
                continue
        m2 = num_line.match(s2)
        if m2 and 1 <= int(m2.group(1)) <= 12 and not m2.group(2):
            nxt = ""
            for j in range(i + 1, min(i + 4, len(lines))):
                t = lines[j].strip()
                if t:
                    nxt = t
                    break
            if nxt and len(nxt) <= 80 and re.match(r"^[A-Za-z]", nxt):
                k = _out_classify(nxt)
                if k:
                    marks.append((i, k, (m2.group(1) + " " + nxt[:60])[:70]))

    seen, uniq = set(), []
    for i, kind, title in marks:
        if kind in seen:
            continue
        seen.add(kind)
        uniq.append((i, kind, title))
    uniq.sort()

    sections = []
    total = len(lines)
    for idx, (i, kind, title) in enumerate(uniq):
        end = uniq[idx + 1][0] if idx + 1 < len(uniq) else min(total, i + 250)
        body = "\n".join(lines[i + 1:end]).strip()
        first = ""
        for para in re.split(r"\n\s*\n", body):
            p = para.strip().replace("\n", " ")
            if len(p) >= 40:
                first = p[:200]
                break
        sections.append({"kind": kind, "title": title, "chars": len(body), "first": first})

    kinds = [x["kind"] for x in sections]
    quality = "ok"
    if len(sections) < 3:
        quality = "too_few"
    elif "intro" not in kinds and "abstract" not in kinds:
        quality = "no_intro"
    elif "method" not in kinds and "experiment" not in kinds:
        quality = "no_core"
    return sections, quality


@app.get("/api/paper/{pid}/outline")
def api_paper_outline(pid: str):
    """从全文反向拆解章节结构，作为大纲模板（纯规则，可复算）"""
    import zlib as _zl
    with _kb_conn() as c:
        row = c.execute("""select p.pid, p.title, p.year, p.field, f.z
                           from papers p left join fulltext_z f on f.pid = p.pid
                           where p.pid = ?""", (pid,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="论文不存在")
    d = dict(row)
    zb = d.pop("z", None)
    if not zb:
        raise HTTPException(status_code=400, detail="这篇论文库里没有全文，无法反推")
    try:
        text = _zl.decompress(zb).decode("utf-8", "ignore")
    except Exception:
        raise HTTPException(status_code=500, detail="全文解压失败")
    sections, quality = _reverse_outline(text[:200000])
    return {"ok": True, "pid": d["pid"], "title": d.get("title"), "year": d.get("year"),
            "field": d.get("field"), "chars": len(text), "quality": quality,
            "sections": sections}

_OUTLINE_TPL = "./paperlib/templates/domain_outlines.json"


@app.get("/api/outline/templates")
def api_outline_templates(field: str = "", year: str = ""):
    """领域标准骨架（从真实论文批量反推，纯规则可复算）"""
    if not os.path.exists(_OUTLINE_TPL):
        raise HTTPException(status_code=404, detail="模板库还没生成")
    d = json.load(open(_OUTLINE_TPL, encoding="utf-8"))
    fields = d.get("fields") or {}
    if not field:
        return {"ok": True, "generated_at": d.get("generated_at"),
                "fields": {k: {"n": v.get("n"), "sections": v.get("sections")}
                           for k, v in fields.items()},
                "available": list(fields.keys())}
    hit = None
    for k, v in fields.items():
        if k.lower() == field.lower():
            hit = (k, v)
            break
    if not hit:
        raise HTTPException(status_code=404, detail="没有这个领域的模板")
    k, v = hit
    out = {"ok": True, "generated_at": d.get("generated_at"), "field": k, "n": v.get("n"),
           "sections": v.get("sections"), "samples": v.get("samples"),
           "by_year": v.get("by_year")}
    if year:
        yv = (v.get("by_year") or {}).get(str(year))
        if yv:
            out["n"], out["sections"], out["year"] = yv.get("n"), yv.get("sections"), str(year)
        else:
            out["year_note"] = "没有 %s 年的样本，已回退到全部年份" % year
    return out

@app.get("/judge", response_class=HTMLResponse)
def judge_page():
    """给评委的导览页（六步看完）"""
    p = os.path.join(V15_ROOT, "app", "templates", "judge.html")
    if os.path.exists(p):
        return FileResponse(p)
    return HTMLResponse("<h1>导览页未部署</h1>", status_code=404)


@app.get("/read", response_class=HTMLResponse)
def read_page():
    """全文阅读器"""
    p = os.path.join(V15_ROOT, "app", "templates", "read.html")
    if os.path.exists(p):
        return FileResponse(p)
    return HTMLResponse("<h1>阅读器未部署</h1>", status_code=404)


@app.get("/tools", response_class=HTMLResponse)
def tools_page():
    """能力平台首页：一个入口，按需选工具"""
    p = os.path.join(V15_ROOT, "app", "templates", "tools.html")
    if os.path.exists(p):
        return FileResponse(p)
    return HTMLResponse("<h1>群智能力平台</h1>", status_code=200)
# ---- /TOOLS HUB END ----


@app.get("/favicon.ico")
def favicon():
    """QBUG-011：全站每个页面都因缺 favicon 恒定打一条匿名 404。"""
    p = os.path.join(V15_ROOT, "app", "static", "logo.png")
    if os.path.exists(p):
        return FileResponse(p, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})
    return HTMLResponse("", status_code=204)


@app.get("/")
def index():
    """根路径 → 产品门户（工作台在 /workbench，旧控制台在 /console）"""
    from fastapi.responses import RedirectResponse as _RR
    return _RR("/portal")


@app.get("/console", response_class=HTMLResponse)
def index_console():
    console_path = os.path.join(V15_ROOT, "app", "templates", "console_new.html")
    if os.path.exists(console_path):
        return FileResponse(console_path)
    return {"service": "群智 · 科研决策预演智能体 V15", "docs": "/docs", "health": "/health"}

# ============================================================
# 四关一审 /gates（2026-11-21）
# ------------------------------------------------------------
# 命名已定：立项关（立）/ 领域关（界）/ 取证关（证）/ 钉锚关（锚）/ 三堂会审（审）。
# 设计纪律：
#   ① 页面状态**实时探测**，不写死。每张卡都带 probed_at / latency_ms。
#   ② 状态接口自身限流：每 IP 每分钟 30 次（与 guard 的全局限流叠加）。
#   ③ 取证探测走真实 /api/evidence 逻辑（同一个 api_evidence 函数），
#      首次约 30 秒；结果落盘快照，之后 10 分钟内复用并在后台刷新。
#   ④ 三堂会审不因 page view 自动烧钱：状态 = v6 健康检查（实时）+ 最近一次
#      真实评审运行（由 /api/review/v6 成功返回时自动写入快照）。
# ============================================================
_GATES_PID = "ax_2510.19425"
_GATES_QUERY = "红烧肉怎么做"
_GATES_DATA_DIR = os.path.join(V15_ROOT, "app", "data")
_GATES_SNAP_PATH = os.path.join(_GATES_DATA_DIR, "gates_probe_snapshot.json")
_GATES_EV_TTL = 600
_GATES_RL_WINDOW = 60
_GATES_RL_MAX = 30
_GATES_RL = {}
_GATES_RL_LOCK = threading.Lock()
_GATES_SNAP_LOCK = threading.RLock()
_GATES_SNAP = None
_GATES_EV_LOCK = threading.Lock()
_GATES_V6_LOCK = threading.Lock()


def _gates_now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _gates_load_snap():
    global _GATES_SNAP
    with _GATES_SNAP_LOCK:
        if _GATES_SNAP is None:
            try:
                with open(_GATES_SNAP_PATH, encoding="utf-8") as f:
                    _GATES_SNAP = json.load(f)
            except Exception:
                _GATES_SNAP = {}
        return _GATES_SNAP


def _gates_save(key, val):
    global _GATES_SNAP
    with _GATES_SNAP_LOCK:
        snap = _gates_load_snap()
        snap[key] = val
        snap["updated_at"] = _gates_now_iso()
        try:
            os.makedirs(_GATES_DATA_DIR, exist_ok=True)
            tmp = _GATES_SNAP_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False, indent=1)
            os.replace(tmp, _GATES_SNAP_PATH)
        except Exception as e:
            print("[gates] snapshot save failed: %s" % e, flush=True)


def _gates_trim_evidence(resp):
    """只保留证据页要展示的字段，避免状态接口过大。"""
    r = dict(resp or {})
    items = []
    for it in (r.get("items") or [])[:6]:
        x = dict(it)
        if isinstance(x.get("html"), str):
            x["html"] = x["html"][:800]
        items.append(x)
    r["items"] = items
    return r


def _gates_probe_evidence_raw():
    t0 = time.time()
    try:
        r = api_evidence({"pid": _GATES_PID, "max_items": 3})
        return {"ok": True, "ts": time.time(), "at": _gates_now_iso(),
                "latency_ms": int((time.time() - t0) * 1000),
                "response": _gates_trim_evidence(r)}
    except HTTPException as e:
        return {"ok": False, "ts": time.time(), "at": _gates_now_iso(),
                "latency_ms": int((time.time() - t0) * 1000),
                "error": str(e.detail)}
    except Exception as e:
        return {"ok": False, "ts": time.time(), "at": _gates_now_iso(),
                "latency_ms": int((time.time() - t0) * 1000),
                "error": "%s: %s" % (type(e).__name__, str(e)[:200])}


def _gates_evidence_snapshot(wait=0):
    """返回最近一次取证探测。

    wait>0：没有快照时同步等（首次约 30 秒，最多等 wait 秒）。
    wait=0：有旧快照就立刻返回，同时在后台刷新。
    """
    snap = _gates_load_snap()
    ev = snap.get("evidence")
    now = time.time()
    if ev and (now - float(ev.get("ts") or 0)) < _GATES_EV_TTL:
        return ev

    got = _GATES_EV_LOCK.acquire(timeout=max(0.01, float(wait or 0.01)))
    if not got:
        return ev

    # 等锁期间别人可能已经刷新好了，再查一次
    snap = _gates_load_snap()
    ev = snap.get("evidence")
    if ev and (time.time() - float(ev.get("ts") or 0)) < _GATES_EV_TTL:
        try:
            _GATES_EV_LOCK.release()
        except Exception:
            pass
        return ev

    box = {}

    def _run():
        try:
            res = _gates_probe_evidence_raw()
            _gates_save("evidence", res)
            box["res"] = res
        except Exception as e:
            box["err"] = e
        finally:
            try:
                _GATES_EV_LOCK.release()
            except Exception:
                pass

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    if wait:
        th.join(timeout=float(wait))
        if box.get("res") is not None:
            return box["res"]
        if box.get("err") is not None:
            return {"ok": False, "ts": time.time(), "at": _gates_now_iso(),
                    "error": "%s: %s" % (type(box["err"]).__name__, str(box["err"])[:200])}
    return ev


def _gates_record_v6(out):
    """把一次真实的 v6 评审结果写进快照（供 /gates 展示最近一次实测）。"""
    if not isinstance(out, dict):
        return
    with _GATES_V6_LOCK:
        dims = {}
        for k, v in (out.get("dimensions") or {}).items():
            if isinstance(v, dict):
                dims[k] = v.get("level")
        summary = {
            "at": _gates_now_iso(),
            "ts": time.time(),
            "topic": out.get("topic") or "",
            "scores_hidden": out.get("scores_hidden"),
            "anchors_passed": out.get("anchors_passed"),
            "anchors_scope": out.get("anchors_scope"),
            "anchors_cached_at": out.get("anchors_cached_at"),
            "models_used": list(out.get("models_used") or []),
            "elapsed_s": out.get("elapsed_s"),
            "n_risk": (out.get("risk") or {}).get("n"),
            "dimensions": dims,
            "errors": (out.get("errors") or [])[:3],
        }
        _gates_save("v6_last_run", {"ts": time.time(), "at": summary["at"],
                                    "summary": summary})


def _gates_rate_take(ip):
    now = time.time()
    with _GATES_RL_LOCK:
        # 防伪造 X-Forwarded-For 造成字典无限增长：超过 4096 个 IP 时清掉过期项
        if len(_GATES_RL) > 4096:
            for _k in list(_GATES_RL.keys()):
                _v = _GATES_RL.get(_k) or []
                if not _v or now - _v[-1] > _GATES_RL_WINDOW:
                    _GATES_RL.pop(_k, None)
        arr = _GATES_RL.setdefault(ip, [])
        arr[:] = [t for t in arr if now - t < _GATES_RL_WINDOW]
        if len(arr) >= _GATES_RL_MAX:
            retry = int(_GATES_RL_WINDOW - (now - arr[0])) + 1
            return False, 0, max(1, retry)
        arr.append(now)
        return True, _GATES_RL_MAX - len(arr), 0


@app.get("/gates", response_class=HTMLResponse)
def gates_page():
    """四关一审：四道闸门的实时状态 + 三堂会审。"""
    p = os.path.join(V15_ROOT, "app", "templates", "gates.html")
    if os.path.exists(p):
        return FileResponse(p)
    return HTMLResponse("<h1>四关一审未部署</h1>", status_code=404)


@app.get("/api/kb/search")
def kb_search_get(q: str = "", limit: int = 10, auto_translate: bool = True):
    """只读 GET 版检索（页面「试一下」用）；中文默认自动改写（与 POST 同口径）。"""
    return kb_search({"q": q, "limit": limit, "auto_translate": auto_translate})


@app.get("/api/gates/status")
def api_gates_status(request: Request):
    """四关一审当前状态（实时探测）。

    - 立项关：本地 experiment_gate.py（无 HTTP API）→ pending / not_connected
    - 领域关：真实 _kb_gate("红烧肉怎么做") → gate.passed=false 即 fail（拦截生效）
    - 取证关：真实 api_evidence(pid=ax_2510.19425, max_items=3) → n / anchored
    - 钉锚关：同一份取证结果 → anchored / n
    - 三堂会审：v6 健康检查（实时）+ 最近一次真实运行（快照）
    """
    ip = (request.headers.get("CF-Connecting-IP")
          or (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
          or (request.client.host if request.client else "unknown"))
    ok, remaining, retry = _gates_rate_take(ip)
    if not ok:
        raise HTTPException(status_code=429,
                            detail="四关状态探测过于频繁：每 IP 每分钟最多 30 次，请 %d 秒后再试" % retry)

    gates = []

    # ---- 1. 立项关（本地，无 HTTP API）----
    gp = os.path.join(V15_ROOT, "experiments", "gate", "experiment_gate.py")
    gexists = os.path.exists(gp)
    rec_dir = os.path.join(V15_ROOT, "experiments", "gate", "gate_records")
    try:
        n_rec = len([x for x in os.listdir(rec_dir) if not x.startswith(".")])
    except Exception:
        n_rec = 0
    gates.append({
        "id": "project", "glyph": "立", "short": "立项关", "tagline": "设计通过才立项",
        "desc": "设计通过才立项（放行 / 待签字 / 打回）。本地审批脚本 + design_lint 硬校验。",
        "backend": "experiment_gate.py（本地）",
        "probe_kind": "live",
        "status": "pending" if gexists else "error",
        "status_label": "待接入" if gexists else "未部署",
        "tone": "idle" if gexists else "bad",
        "detail": ("本地审批脚本已就位（无 HTTP API，故状态为待接入）"
                   if gexists else "未找到 experiment_gate.py"),
        "latency_ms": 0,
        "probed_at": _gates_now_iso(),
        "facts": [["脚本", gp if gexists else "缺失"],
                  ["审批记录", "%d 份" % n_rec],
                  ["接口", "无（本地 CLI）"],
                  ["退出码", "0 放行 / 1 打回 / 2 暂停"]],
        "evidence": {"path": gp, "exists": gexists, "gate_records": n_rec,
                     "api": None, "note": "本地模块，尚未挂网关 API"},
    })

    # ---- 2. 领域关（真实调用同一个 _kb_gate）----
    t0 = time.time()
    try:
        g, sq = _kb_gate(_GATES_QUERY, auto_translate=False)
        passed = (g.get("passed") is True)
        gates.append({
            "id": "domain", "glyph": "界", "short": "领域关", "tagline": "判断是不是 CS/ML",
            "desc": "判断是不是 CS/ML：非 CS → non_cs_topic；中文 CS → 默认 LLM 改写（auto_translated）；CS 无命中 → no_match。",
            "backend": "/api/kb/search 的 gate 字段",
            "probe_kind": "live",
            "status": "pass" if passed else "fail",
            "status_label": "放行" if passed else "已拦截",
            "tone": "ok" if passed else "warn",
            "detail": "%s → %s（%s）" % (_GATES_QUERY, g.get("reason") or "?",
                                          "闸门拦截生效" if not passed else "闸门放行"),
            "latency_ms": int((time.time() - t0) * 1000),
            "probed_at": _gates_now_iso(),
            "facts": [["探针查询", _GATES_QUERY],
                      ["gate.passed", str(g.get("passed"))],
                      ["reason", str(g.get("reason") or "")],
                      ["matched_terms", "、".join(g.get("matched_terms") or []) or "（无）"],
                      ["hint", str(g.get("hint") or "")]],
            "evidence": {"query": _GATES_QUERY, "search_query": sq, "gate": g},
        })
    except Exception as e:
        gates.append({
            "id": "domain", "glyph": "界", "short": "领域关", "tagline": "判断是不是 CS/ML",
            "desc": "判断是不是 CS/ML。", "backend": "/api/kb/search 的 gate 字段",
            "status": "error", "status_label": "接口异常", "tone": "bad",
            "detail": "%s: %s" % (type(e).__name__, str(e)[:160]),
            "latency_ms": int((time.time() - t0) * 1000),
            "probed_at": _gates_now_iso(), "facts": [], "evidence": {"error": str(e)},
        })

    # ---- 3/4. 取证关 + 钉锚关（同一份真实 evidence 返回）----
    snap = _gates_load_snap()
    ev = _gates_evidence_snapshot(wait=45 if not snap.get("evidence") else 0)
    resp = (ev or {}).get("response") or {}
    n = int(resp.get("n") or 0)
    anchored = int(resp.get("anchored") or 0)
    ev_age = (time.time() - float(ev.get("ts") or 0)) if ev else None

    if not ev or ev.get("ok") is None:
        ev_status, ev_tone, ev_label = "pending", "idle", "首次取证中"
        ev_detail = "首次取证探测已启动（约 30 秒），页面会自动刷新"
    elif ev.get("ok") is False:
        ev_status, ev_tone, ev_label = "error", "bad", "接口异常"
        ev_detail = "取证接口失败：%s" % str(ev.get("error") or "")[:180]
    elif n <= 0:
        ev_status, ev_tone, ev_label = "fail", "bad", "未取到问题"
        ev_detail = "%s · 引擎返回 n=0" % _GATES_PID
    else:
        ev_status, ev_tone, ev_label = "pass", "ok", "可用 · %d/%d 锚定" % (anchored, n)
        ev_detail = "%s · n=%d · anchored=%d%s" % (
            _GATES_PID, n, anchored, " · cached" if resp.get("cached") else "")

    ev_facts = [["pid", _GATES_PID],
                ["n", str(n)], ["anchored", str(anchored)],
                ["cached", str(bool(resp.get("cached")))],
                ["cached_at", str(resp.get("cached_at") or "—")],
                ["快照年龄", ("%.0f 秒" % ev_age) if ev_age is not None else "—"]]
    gates.append({
        "id": "evidence", "glyph": "证", "short": "取证关", "tagline": "每条问题有原文证据",
        "desc": "每条问题都要在原文里找到出处；拿不到 quote 的问题不算证据。",
        "backend": "/api/evidence",
        "probe_kind": "snapshot",
        "status": ev_status, "status_label": ev_label, "tone": ev_tone,
        "detail": ev_detail,
        "latency_ms": (ev or {}).get("latency_ms"),
        "probed_at": (ev or {}).get("at") or _gates_now_iso(),
        "facts": ev_facts,
        "evidence": {"response": resp, "ok": (ev or {}).get("ok"),
                     "error": (ev or {}).get("error"),
                     "snapshot_age_sec": (round(ev_age, 1) if ev_age is not None else None)},
    })

    if not ev or ev.get("ok") is None:
        an_status, an_tone, an_label = "pending", "idle", "等待取证"
        an_detail = "取证关尚未返回，钉锚关无法判定"
    elif ev.get("ok") is False:
        an_status, an_tone, an_label = "error", "bad", "接口异常"
        an_detail = "取证接口失败，无法核对锚点"
    elif n <= 0:
        an_status, an_tone, an_label = "fail", "bad", "无锚可钉"
        an_detail = "n=0，没有可核对的锚点"
    elif anchored >= n:
        an_status, an_tone, an_label = "pass", "ok", "逐字锚回 %d/%d" % (anchored, n)
        an_detail = "逐字锚回 %d/%d（T1 级逐字命中）" % (anchored, n)
    else:
        an_status, an_tone, an_label = "fail", "bad", "未钉满 %d/%d" % (anchored, n)
        an_detail = "有 %d 条问题未在原文命中" % (n - anchored)
    gates.append({
        "id": "anchor", "glyph": "锚", "short": "钉锚关", "tagline": "逐字锚回原文（T1/T2/T3）",
        "desc": "逐字锚回原文：quote 必须能在正文里定位到，找不到就不算锚上。",
        "backend": "/api/evidence 的 anchored 字段",
        "probe_kind": "snapshot",
        "status": an_status, "status_label": an_label, "tone": an_tone,
        "detail": an_detail,
        "latency_ms": (ev or {}).get("latency_ms"),
        "probed_at": (ev or {}).get("at") or _gates_now_iso(),
        "facts": [["pid", _GATES_PID], ["anchored", str(anchored)], ["n", str(n)],
                  ["锚定率", ("%.0f%%" % (100.0 * anchored / n)) if n else "—"]],
        "evidence": {"response": resp, "ok": (ev or {}).get("ok"),
                     "error": (ev or {}).get("error")},
    })

    # ---- 5. 三堂会审（实时健康检查 + 最近一次真实运行）----
    try:
        h = review_v6_health()
    except Exception as e:
        h = {"v6": False, "error": "%s: %s" % (type(e).__name__, str(e)[:160])}
    last_wrap = snap.get("v6_last_run") or {}
    last = last_wrap.get("summary") or {}
    reviewers = list(h.get("reviewers") or [])
    models = list(last.get("models_used") or [])
    health_ok = (bool(h.get("v6")) and int(h.get("n_reviewers") or 0) >= 3
                 and bool(h.get("anchor_materials_ok")))
    run_ok = (last.get("scores_hidden") is False)
    if health_ok and run_ok and len(models) >= 3:
        rv_status, rv_tone, rv_label = "pass", "ok", "已修复 · 3 模型"
        rv_detail = "scores_hidden=false · models_used=%d · 锚点通过 · 最近实测 %s" % (
            len(models), last.get("at") or "—")
    elif not health_ok:
        rv_status, rv_tone, rv_label = "error", "bad", "健康检查未过"
        rv_detail = "v6 不可用或锚点材料缺失：%s" % str(h.get("error") or "")[:160]
    elif not last:
        rv_status, rv_tone, rv_label = "pending", "idle", "待实测"
        rv_detail = "v6 健康检查通过；尚无最近一次真实评审运行（点「试一下」跑一局）"
    elif run_ok and len(models) < 3:
        rv_status, rv_tone, rv_label = "fail", "bad", "模型不全"
        rv_detail = "scores_hidden=false，但只有 %d 家模型返回" % len(models)
    else:
        rv_status, rv_tone, rv_label = "fail", "bad", "未达标"
        rv_detail = "scores_hidden=%s · 模型数=%d · 锚点=%s" % (
            last.get("scores_hidden"), len(models), last.get("anchors_passed"))
    gates.append({
        "id": "review", "glyph": "审", "short": "三堂会审", "tagline": "三家模型成对强制选择",
        "desc": "glm / qwen / minimax 三家成对强制选择；5 维度（核心 / 可执行 / 风险 / 事实 / 视角）+ 独立风险栏。锚点未过一个分数都不出。",
        "backend": "/api/review/v6",
        "probe_kind": "snapshot",
        "status": rv_status, "status_label": rv_label, "tone": rv_tone,
        "detail": rv_detail,
        "latency_ms": None,
        "probed_at": last.get("at") or _gates_now_iso(),
        "facts": [["v6", str(bool(h.get("v6")))],
                  ["reviewers", "、".join(reviewers) or "（无）"],
                  ["锚点材料", "%s 份" % h.get("anchor_materials")],
                  ["scores_hidden", str(last.get("scores_hidden"))],
                  ["models_used", "、".join(models) or "（尚未实测）"],
                  ["anchors_passed", str(last.get("anchors_passed"))],
                  ["最近实测", str(last.get("at") or "—")]],
        "evidence": {"health": h, "last_run": {
            "at": last.get("at"), "topic": last.get("topic"),
            "scores_hidden": last.get("scores_hidden"),
            "anchors_passed": last.get("anchors_passed"),
            "models_used": models, "elapsed_s": last.get("elapsed_s"),
            "dimensions": last.get("dimensions") or {},
            "n_risk": last.get("n_risk"),
            "errors": last.get("errors") or [],
        }, "reviewers": reviewers},
    })

    # 2026-11-22 前端 BUG-004：明确告诉页面哪些闸门是实时、哪些是缓存快照，
    # 不再让「generated_at 是现在」被误读成「所有闸门都刚探测过」。
    _oldest = ev_age
    return {"ok": True, "generated_at": _gates_now_iso(),
            "probe": {"pid": _GATES_PID, "query": _GATES_QUERY},
            "rate": {"limit": _GATES_RL_MAX, "window_sec": _GATES_RL_WINDOW,
                     "remaining": remaining},
            "snapshot_file": _GATES_SNAP_PATH,
            "probe_kinds": {"project": "live", "domain": "live",
                            "evidence": "snapshot", "anchor": "snapshot",
                            "review": "last_run"},
            "realtime_all": False,
            "oldest_snapshot_age_sec": (round(_oldest, 1) if _oldest is not None else None),
            "cache_note": ("立项关/领域关实时探测；取证关/钉锚关复用最近一次取证快照，"
                           "会审展示最近一次真实运行。点卡片里的「试一下」可真实重跑。"),
            "gates": gates}


if __name__ == "__main__":
    import uvicorn
    print("🚀 群智 V15 网关启动：http://127.0.0.1:%s" % os.getenv("QZ_PORT", "5020"))
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("QZ_PORT", "5020")))
