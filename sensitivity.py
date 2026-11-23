"""口径敏感性（sensitivity）——真实从 SQLite 现算，不写死任何数字。

背景（tasks/sensitivity.md）：
    同一指标在不同口径（全库 / 顶会 / arXiv / 有评审）下会得到不同数字。
    `seed`/cs.CL 在全库是 19.1%，在顶会是 16.2%，差 2.9 个百分点——
    这个量级足以凭空产生或抵消一个「提升」。所以每个指标都要回显：
      - 各口径的 n / 分子 / 率
      - max_delta_pp（最大口径差，百分点）
      - direction_consistent（方向是否一致）
      - conclusion_robust（<1pp 视为稳健，否则 false + warning）
      - recommended_scope（推荐口径 + 理由）

口径定义（与 /api/kb/defect_baseline 的 venue 完全一致）：
    all   = 全库（所有有特征的论文）
    top   = source='venue-crawl'（顶会来源）
    arxiv = source='arxiv-api'（预印本）
    peer  = source='deepreview'（有评审）

本模块只读 DB，不写库；连接用 mode=ro。
"""

import sqlite3
import threading
import time

DB_PATH = "./paperlib/db/paperlib.sqlite"

# 与 main_gateway._VENUE_SOURCE 保持一致（这里独立一份，避免循环 import）。
SCOPE_SOURCE = {"top": "venue-crawl", "arxiv": "arxiv-api", "peer": "deepreview"}
SCOPES = ("all", "top", "arxiv", "peer")
SCOPE_DOC = {
    "all": "全库（所有有特征的论文）",
    "top": "顶会来源（source=venue-crawl）",
    "arxiv": "arXiv 来源（source=arxiv-api）",
    "peer": "有评审的论文（source=deepreview）",
}
ROBUST_PP = 1.0          # <1 个百分点视为稳健
_CACHE_TTL = 600
_CACHE = {}
_LOCK = threading.Lock()


# ---------------------------------------------------------------- 基础设施
def _conn():
    conn = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True, timeout=8)
    conn.row_factory = sqlite3.Row
    return conn


def _cache_get(key):
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and (now - hit["ts"]) < _CACHE_TTL:
            return hit["data"]
    return None


def _cache_put(key, data):
    with _LOCK:
        _CACHE[key] = {"ts": time.time(), "data": data}


def _rows(sql, args=()):
    with _conn() as c:
        return c.execute(sql, args).fetchall()


# ---------------------------------------------------------------- 口径统计
def scope_stats(feature, field=None):
    """某特征在各口径下的 {n, papers, rate, pct}。

    一次 group by source 拿全四个口径，比逐口径查快且口径自洽。
    n = 该口径下有特征抽取的论文数（与 baseline 的 total_papers 同分母）。
    """
    feature = (feature or "seed").strip().lower()
    field = (field or "").strip() or None
    key = ("scope_stats", feature, field or "")
    hit = _cache_get(key)
    if hit is not None:
        return hit
    sql = ("select coalesce(p.source,'') as src, count(*) as n, "
           "sum(case when pf.pid is not null then 1 else 0 end) as k "
           "from papers p "
           "left join paper_features pf on pf.pid = p.pid and pf.feat = ? "
           "where exists(select 1 from paper_features x where x.pid = p.pid)")
    args = [feature]
    if field:
        sql += " and p.field = ?"
        args.append(field)
    sql += " group by p.source"
    by_src = {}
    for r in _rows(sql, args):
        by_src[str(r["src"] or "")] = (int(r["n"] or 0), int(r["k"] or 0))
    tot_n = sum(v[0] for v in by_src.values())
    tot_k = sum(v[1] for v in by_src.values())
    out = {}
    for sc in SCOPES:
        if sc == "all":
            n, k = tot_n, tot_k
        else:
            n, k = by_src.get(SCOPE_SOURCE[sc], (0, 0))
        out[sc] = {
            "scope": sc,
            "definition": SCOPE_DOC[sc],
            "n": n,
            "papers": k,
            "rate": round(k / n, 4) if n else None,
            "pct": round(k / n * 100, 2) if n else None,
        }
    _cache_put(key, out)
    return out


def field_feature_matrix(field):
    """某领域下、各口径 × 各特征的分子计数。

    返回 {scope: {feat: papers}}；分母用 scope_stats(..., field=field) 的 n。
    selfcheck 的 20 行 field_pct 就是靠这个矩阵换成不同口径的。
    """
    field = (field or "").strip()
    key = ("field_matrix", field)
    hit = _cache_get(key)
    if hit is not None:
        return hit
    sql = ("select coalesce(p.source,'') as src, pf.feat as feat, count(*) as k "
           "from papers p join paper_features pf on pf.pid = p.pid "
           "where p.field = ? "
           "and exists(select 1 from paper_features x where x.pid = p.pid) "
           "group by p.source, pf.feat")
    by_src = {}
    for r in _rows(sql, (field,)):
        by_src.setdefault(str(r["src"] or ""), {})[str(r["feat"])] = int(r["k"] or 0)
    # 给出每个口径真正的特征计数映射（all 需要跨 source 求和）。
    res = {}
    for sc in SCOPES:
        if sc == "all":
            agg = {}
            for _src, feats in by_src.items():
                for f, k in feats.items():
                    agg[f] = agg.get(f, 0) + k
            res[sc] = agg
        else:
            res[sc] = dict(by_src.get(SCOPE_SOURCE[sc], {}))
    _cache_put(key, res)
    return res


# ---------------------------------------------------------------- 敏感性计算
def compute_sensitivity(rates, threshold_pp=ROBUST_PP):
    """按 tasks/sensitivity.md 的公式算敏感性。

    rates: {scope: rate}（rate 是 0-1 小数，None 会被跳过）。
    max_delta_pp = (max-min)*100；
    conclusion_robust = max_delta_pp < threshold_pp（默认 <1pp）。
    """
    vals = [(k, v) for k, v in (rates or {}).items() if isinstance(v, (int, float))]
    if len(vals) < 2:
        return {
            "max_delta_pp": 0.0,
            "direction_consistent": None,
            "conclusion_robust": None,
            "threshold_pp": threshold_pp,
            "note": "可用口径少于 2 个，无法比较",
            "error": "insufficient_scopes",
        }
    nums = [v for _, v in vals]
    max_v, min_v = max(nums), min(nums)
    max_delta_pp = round((max_v - min_v) * 100, 2)
    direction_consistent = all(v > 0 for v in nums) or all(v < 0 for v in nums)
    conclusion_robust = max_delta_pp < threshold_pp
    sens = {
        "max_delta_pp": max_delta_pp,
        "direction_consistent": direction_consistent,
        "conclusion_robust": conclusion_robust,
        "threshold_pp": threshold_pp,
        "delta_scope": "%s↔%s" % (max(vals, key=lambda x: x[1])[0],
                                   min(vals, key=lambda x: x[1])[0]),
        "note": "口径间最大差 %.2f 个百分点；<%.0fpp 视为稳健" % (max_delta_pp, threshold_pp),
    }
    if not conclusion_robust:
        sens["warning"] = "口径选择会改变结论，引用时须注明 scope"
    return sens


def _recommended(kind, scopes):
    top_n = int((scopes.get("top") or {}).get("n") or 0)
    all_n = int((scopes.get("all") or {}).get("n") or 0)
    if kind == "selfcheck":
        return {
            "for": "投稿前自检",
            "scope": "top",
            "reason": "投稿目标为顶会，应与顶会论文同口径（venue=top，n=%d）" % top_n,
        }
    return {
        "for": "顶会投稿对比",
        "scope": "top",
        "reason": "顶会论文应与顶会同比较（venue=top，n=%d）" % top_n,
    }


# ---------------------------------------------------------------- 三个接口的封装
def _stats_from_venue_json(venue_data, feature):
    """从 baseline_rates.json 的 by_venue 取各口径计数（无 field 时的快路径）。

    与 scope_stats 的 SQL 结果同源同口径；baseline 接口的 overall 本来就用这份
    JSON，用同一快照反而避免「overall 是 JSON、sensitivity 是 DB」的错配。
    """
    out = {}
    for sc in SCOPES:
        v = (venue_data or {}).get(sc) or {}
        n = int(v.get("n") or 0)
        fv = (v.get("features") or {}).get(feature) or {}
        k = int(fv.get("papers") or 0)
        out[sc] = {
            "scope": sc,
            "definition": SCOPE_DOC[sc],
            "n": n,
            "papers": k,
            "rate": round(k / n, 4) if n else None,
            "pct": round(k / n * 100, 2) if n else None,
        }
    return out


def for_baseline(feature="seed", field=None, venue_data=None):
    """返回 (scopes, sensitivity, recommended_scope)。

    scopes 的 shape 按 tasks/sensitivity.md：
      {"all": {"rate":0.1556,"n":258721,"papers":40248}, ...}

    venue_data 传入 baseline_rates.json 的 by_venue 时走快路径（无 DB 查询）；
    带 field 的查询 by_venue 不含领域切片，仍走 DB 现算。
    """
    feature = (feature or "seed").strip().lower()
    field = (field or "").strip() or None
    if not field and isinstance(venue_data, dict) and venue_data:
        st = _stats_from_venue_json(venue_data, feature)
    else:
        st = scope_stats(feature, field)
    scopes = {}
    for sc in SCOPES:
        v = st[sc]
        scopes[sc] = {"scope": sc, "definition": SCOPE_DOC[sc],
                      "rate": v["rate"], "n": v["n"], "papers": v["papers"],
                      "pct": v["pct"]}
    sens = compute_sensitivity({sc: st[sc]["rate"] for sc in SCOPES})
    sens.update({
        "metric": "seed_disclosure_rate" if feature == "seed" else "feature_occurrence_rate",
        "feature": feature,
        "field": field,
        "scopes": scopes,
    })
    return scopes, sens, _recommended("baseline", scopes)


def for_selfcheck(field, rows, focal=None):
    """selfcheck 的 sensitivity：field_pct 在各口径下的值。

    返回 (scopes, sensitivity, recommended_scope)。
    scopes 既带 <field>_<feat>_pct 扁平键，也带 features 明细；
    敏感性围绕 focal 特征（默认 seed，缺失时取 rows 里 field_pct 最高的项）。
    """
    field = (field or "").strip()
    row_feats = [r.get("feat") for r in (rows or []) if r.get("feat")]
    if not focal or focal not in row_feats:
        if "seed" in row_feats:
            focal = "seed"
        elif rows:
            focal = max(rows, key=lambda r: r.get("field_pct") or 0).get("feat")
        else:
            focal = "seed"
    if field:
        matrix = field_feature_matrix(field)
        st = scope_stats(focal, field)
    else:
        # 没有领域切片时退化为全库口径（不按 field 过滤）；
        # 此时 rows 的 papers 是 recent 窗口口径，不能拿来当分子，只用 focal 的全库计数。
        st = scope_stats(focal, None)
        matrix = {sc: {focal: int(st[sc]["papers"] or 0)} for sc in SCOPES}
    scopes = {}
    for sc in SCOPES:
        n = int(st[sc]["n"] or 0)
        flat = {"scope": sc, "definition": SCOPE_DOC[sc], "n": n}
        feats = {}
        for feat, k in sorted((matrix.get(sc) or {}).items()):
            rate = round(k / n, 4) if n else None
            feats[feat] = {"papers": k, "n": n, "rate": rate,
                           "pct": round(rate * 100, 2) if rate is not None else None}
            flat["%s_%s_pct" % (field or "all", feat)] = rate
        flat["features"] = feats
        scopes[sc] = flat
    focal_rates = {sc: (scopes[sc].get("features", {}).get(focal) or {}).get("rate")
                   for sc in SCOPES}
    sens = compute_sensitivity(focal_rates)
    sens.update({
        "metric": "field_pct",
        "focal_feature": focal,
        "field": field or None,
        "scope_key": "%s_%s_pct" % (field or "all", focal),
        "scopes": scopes,
    })
    # 附：20 项里最敏感的那一项，方便判断整张表是否都受口径影响。
    any_delta = 0.0
    for feat in row_feats:
        rr = {sc: (scopes[sc].get("features", {}).get(feat) or {}).get("rate")
              for sc in SCOPES}
        nums = [v for v in rr.values() if isinstance(v, (int, float))]
        if len(nums) >= 2:
            any_delta = max(any_delta, (max(nums) - min(nums)) * 100)
    sens["max_delta_pp_any_feature"] = round(any_delta, 2)
    return scopes, sens, _recommended("selfcheck", scopes)


def _feature_exists(name):
    key = ("feature_exists", name)
    hit = _cache_get(key)
    if hit is not None:
        return hit
    try:
        r = _rows("select 1 from paper_features where feat=? limit 1", (name,))
        ok = bool(r)
    except Exception:
        ok = False
    _cache_put(key, ok)
    return ok


def known_features(limit=40):
    key = ("known_features", limit)
    hit = _cache_get(key)
    if hit is not None:
        return hit
    try:
        feats = [str(r[0]) for r in _rows(
            "select feat, count(*) c from paper_features group by feat order by c desc limit ?",
            (int(limit),))]
    except Exception:
        feats = []
    _cache_put(key, feats)
    return feats


def for_metric(name, feature=None, field=None):
    """通用指标敏感性。返回 dict；未知指标抛 KeyError（带 available_metrics）。"""
    raw = (name or "").strip()
    low = raw.lower()
    field = (field or "").strip() or None
    feat = (feature or "").strip().lower() or None

    if low in ("seed_disclosure_rate", "seed_rate", "seed_pct", "seed"):
        feat = feat or "seed"
        resolved = "seed_disclosure_rate"
        kind = "baseline"
    elif low in ("feature_occurrence_rate", "feature_rate", "occurrence_rate"):
        feat = feat or "seed"
        resolved = "feature_occurrence_rate"
        kind = "baseline"
    elif low in ("field_pct", "field_feature_rate", "selfcheck_present_rate"):
        if not field:
            raise KeyError("field_required")
        feat = feat or "seed"
        resolved = "field_pct"
        kind = "selfcheck"
    elif _feature_exists(low):
        feat = low
        resolved = "feature_occurrence_rate"
        kind = "baseline"
    else:
        raise KeyError(low)

    if kind == "selfcheck":
        matrix = field_feature_matrix(field)
        st = scope_stats(feat, field)
        scopes = {}
        for sc in SCOPES:
            n = int(st[sc]["n"] or 0)
            k = int((matrix.get(sc) or {}).get(feat) or 0)
            rate = round(k / n, 4) if n else None
            scopes[sc] = {"scope": sc, "definition": SCOPE_DOC[sc],
                          "n": n, "papers": k, "rate": rate,
                          "pct": round(rate * 100, 2) if rate is not None else None}
        sens = compute_sensitivity({sc: scopes[sc]["rate"] for sc in SCOPES})
        sens.update({"metric": resolved, "feature": feat, "field": field})
    else:
        st = scope_stats(feat, field)
        scopes = {}
        for sc in SCOPES:
            v = st[sc]
            scopes[sc] = {"scope": sc, "definition": SCOPE_DOC[sc],
                          "n": v["n"], "papers": v["papers"], "rate": v["rate"],
                          "pct": v["pct"]}
        sens = compute_sensitivity({sc: scopes[sc]["rate"] for sc in SCOPES})
        sens.update({"metric": resolved, "feature": feat, "field": field})

    _scopes_list = [scopes[sc] for sc in SCOPES]
    return {
        "metric": resolved,
        "metric_input": raw,
        "feature": feat,
        "field": field,
        # 规范要求 scopes 是数组；同时给 scopes_by_name 便于按口径取值。
        "scopes": _scopes_list,
        "scopes_by_name": scopes,
        "scopes_list": _scopes_list,
        "scope_order": list(SCOPES),
        "sensitivity": sens,
        "recommended_scope": _recommended("selfcheck" if kind == "selfcheck" else "baseline",
                                          scopes),
        "available_metrics": (["seed_disclosure_rate", "feature_occurrence_rate",
                               "field_pct"] + known_features(20)),
    }


# ---------------------------------------------------------------- search 口径说明
def library_counts():
    """检索口径的论文数（全库 / 各来源），用于 scope_note。"""
    key = ("library_counts",)
    hit = _cache_get(key)
    if hit is not None:
        return hit
    rows = _rows("select coalesce(source,'') src, count(*) n from papers group by source")
    by_src = {str(r["src"] or ""): int(r["n"] or 0) for r in rows}
    out = {
        "all": sum(by_src.values()),
        "top": by_src.get(SCOPE_SOURCE["top"], 0),
        "arxiv": by_src.get(SCOPE_SOURCE["arxiv"], 0),
        "peer": by_src.get(SCOPE_SOURCE["peer"], 0),
        "by_source": by_src,
    }
    _cache_put(key, out)
    return out


def search_scope_note(venue_input=None):
    """返回 (scope_note 字符串, scope_note_meta dict)。

    说明必须真实：/api/kb/search 的 venue 参数按「发表 venue 名」过滤
    （如 ICLR/ACL），不是 top/arxiv/peer 的 source 口径；所以不能骗用户
    「传 venue=top 就是顶会来源口径」。
    """
    cnt = library_counts()
    n_all = int(cnt["all"])
    n_top = int(cnt["top"])
    v = (venue_input or "").strip()
    if v:
        note = ("本次检索附加 venue=%s 过滤（按发表 venue 名匹配，不是 top/arxiv/peer 来源口径）；"
                "全库可检索 n=%d。来源口径的敏感性请用 "
                "/api/metrics/seed_disclosure_rate/sensitivity 或 "
                "/api/kb/defect_baseline?venue=top" % (v, n_all))
    else:
        note = ("本检索为全库（venue=all，n=%d）；venue 参数按发表 venue 名过滤（如 ICLR/ACL），"
                "传 venue=top 不会按来源过滤；顶会来源口径（n=%d）请用 "
                "/api/kb/defect_baseline?venue=top 或 "
                "/api/metrics/seed_disclosure_rate/sensitivity" % (n_all, n_top))
    meta = {
        "venue_requested": v or None,
        "n_all": n_all,
        "n_top_source": n_top,
        "n_arxiv_source": int(cnt["arxiv"]),
        "n_peer_source": int(cnt["peer"]),
        "venue_filter_semantics": "pub_venue_name",
        "source_scope_hint": "/api/kb/defect_baseline?venue=top",
        "sensitivity_api": "/api/metrics/seed_disclosure_rate/sensitivity",
    }
    return note, meta
