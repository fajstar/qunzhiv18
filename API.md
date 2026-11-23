# 群智 API 文档（schema 2026-11-21）

> 在线核对：`GET /api/version`。变更记录：`/CHANGELOG.md`。
> 规则：**只加字段、不改旧字段语义**；breaking change 必须提升 `schema_version`。

## 通用约定

- 鉴权：`?key=<口令>` 或请求头 `X-Access-Key` / `X-QunZhi-Key`（`/api/version`、`/api/version` 文档端点、`/api/call/{id}` 公开）。
- 所有 `/api/*` 的 JSON 返回都带 `schema_version`；非 JSON 返回带响应头 `X-API-Schema-Version`。
- 带数字/检索的接口返回 `call_id` / `computed_at` / `version` / `denominator` / `numerator` / `reproduce`。
- `call_id` 可用 `GET /api/call/{call_id}` 或 `POST /api/evidence` 带 `call_id` 原样回放。

### `call_id` 的语义：计算结果 ID，不是调用凭据

**`call_id` 是“计算结果 ID”（content-addressed result ID），不是调用凭据、不是鉴权 token、不是 session id。**

- 它由「接口名 + 归一化参数 + API 版本 + 数据快照」哈希得到（`qz_YYYYMMDD_<hash>`）；
  同一组参数 + 同一数据快照 → **同一个 `call_id`**，与调用者是谁、什么时候调用无关。
- **同一个 `call_id` 可以对应多次调用**。仅凭 `call_id` 不能证明“这是一次新的计算”，
  也不能把它当 key / 口令 / 幂等令牌使用（鉴权请用 `X-Access-Key`）。
- 每次带 `call_id` 的响应都会带下面四个字段，用来区分“首次计算”和“重复请求”：

| 字段 | 类型 | 含义 |
|---|---|---|
| `is_cached` | bool | 本次是否为同 `call_id` 的重复请求（`true` = 之前已算过/命中了已落盘结果） |
| `fetch_count` | int | 该 `call_id` 被**业务接口**返回的次数；首次为 1，重复请求递增。`GET /api/call/{id}` 只是回看，不计入 |
| `first_computed_at` | ISO8601 | 该 `call_id` 第一次被计算的时间（固定不变） |
| `last_accessed_at` | ISO8601 | 该 `call_id` 最近一次被业务接口请求的时间 |

- 因此：**同参数重复调用 → 同 `call_id`、同 `first_computed_at`，但 `fetch_count` 递增、`is_cached=true`、`last_accessed_at` 更新**。
- 数据快照变化（库更新）会改变 `call_id`，避免把旧数字当成新数据；旧 `call_id` 仍可用 `/api/call/{id}` 回放。
- 确定性统计接口（`selfcheck` / `defect_baseline`）重复请求时复用同一 `computed_at`；
  检索接口（`/api/kb/search`）重复请求会直接复用已落盘的 `items/total`，并新增 `search_id` 标识这一次请求。

## `GET /api/kb/defect_baseline`

参数：`feature`（特征名，如 `seed`）、`field`（可选）、`venue`（可选）。

**`venue` 默认值固定为 `all`（全库，向后兼容），不会随调用静默变化。**
取值：`top`（顶会，source=venue-crawl）/ `arxiv`（预印本）/ `peer`（有评审 deepreview）/ `all`。

返回关键字段：

```json
{
  "venue": "all", "venue_effective": "all", "venue_default": "all",
  "venue_requested": null, "venue_source": "default",
  "metric": "feature_occurrence_rate", "scope": "venue:all",
  "denominator": {"n": 258721, "description": "all papers in library with extracted features"},
  "numerator": {"n": 40248, "description": "papers where feature seed occurs"},
  "overall": {"papers": 40248, "n": 258721, "rate": 0.1556, "verdict": "minority"},
  "by_field": {
    "cs.CL": {"papers": 10355, "n": 54295, "rate": 0.1907, "verdict": "minority",
              "field": "cs.CL", "scope": "field:cs.CL+venue:all"}
  },
  "call_id": "qz_20261121_83180d5706681f4f",
  "computed_at": "2026-11-21T09:48:38Z",
  "version": "v2026-11-21", "schema_version": "2026-11-21",
  "cross_check": {
    "method": "sqlite_vs_fts", "feature": "seed",
    "source_a": {"name": "sqlite_scan", "n": 61250, "papers": 9867,
                 "rate": 0.1611, "seed_rate": 0.1611},
    "source_b": {"name": "fts5_sample", "n": 5000, "papers": 780,
                 "rate": 0.156, "seed_rate": 0.156, "sample_stride": 12},
    "agreement": 0.9683, "confidence": "high"
  }
}
```

- `by_field.*.n` 是该字段分母：`papers / n = rate`。
- `venue_source=default` 表示本次用了默认 `all`；`venue_source=param` 表示调用方显式传了 `venue`。
- 默认 `all` 与 `top` 是**两个不同口径**，`call_id` 也不同，不能当成同一个数比较。
- `cross_check`：SQLite 全量（source_a）vs FTS5 抽样 5000 篇（source_b），
  `agreement = 1 - |rate_a-rate_b| / max(rate_a, rate_b)`；不传 `feature` 时固定核 `seed`。
  `agreement < 0.7` 时 `confidence="low"` 且带 `warning`。

## `GET /api/kb/selfcheck`

参数：`pid`、`field`（可选）。

返回关键字段：

```json
{
  "pid": "ax_2510.19425", "n_features": 20, "n_features_present": 9,
  "metric": "selfcheck_present_rate", "scope": "field:cs.LG",
  "denominator": {"n": 45410, "description": "papers in field cs.LG with extracted features"},
  "numerator": {"n": 9, "description": "checklist items the paper satisfies"},
  "rows": [{"feat": "claim_hedging", "has": true, "field_pct": 93.9,
            "papers": 42657, "n": 45410, "rate": 0.939}],
  "call_id": "qz_20261121_3e242163f435a1c5",
  "cross_check": {
    "method": "two_source_agreement",
    "source_a": {"name": "feature_extractor", "n": 20},
    "source_b": {"name": "rule_judge_v2", "n": 20, "text_source": "fulltext_z"},
    "agreement": 0.75, "confidence": "medium",
    "disagreements": [{"feature": "limitations", "a": false, "b": true,
                       "note": "提取器说无，独立规则裁定有"}]
  }
}
```

- `n_features` = 自检清单总项数（= `len(rows)`）；`n_features_present` = 命中项数；
  `n_features_detected` = `paper_features` 原始特征数。
- `rows[].n` 是该字段论文分母；`rows[].papers` 是做到该做法的论文数。
- `cross_check`：`source_a` = `paper_features` 提取器，`source_b` = 另一套独立正则裁定器；
  `agreement` = 20 项 `has` 布尔值一致率，分歧逐项列在 `disagreements`。
  `agreement < 0.7` 时 `confidence="low"` 且带 `warning="双源不一致，结论不可靠"`。

## `POST /api/evidence`

请求体：`{"pid": "...", "max_items": 8}`；可选 `{"call_id": "qz_..."}` 重放。

返回关键字段：

```json
{
  "ok": true, "pid": "ax_2510.19425", "n": 2, "anchored": 2,
  "metric": "evidence_anchor_rate", "scope": "pid:ax_2510.19425",
  "denominator": {"n": 2, "description": "issues returned by v18 engine for this paper"},
  "numerator": {"n": 2, "description": "issues with an exact quote located in fulltext"},
  "call_id": "qz_20261121_fc411c5693148859", "computed_at": "2026-11-21T09:48:03Z",
  "items": [{"cls": "EXP", "issue": "...", "quote": "...", "found": true, "offset": 123}]
}
```

- v18 引擎有随机性：`call_id` 标识“这一次计算”；同参数重算会得到新 `call_id`，旧 `call_id` 永远可回放。
- 降级响应（`ok=false, degraded=true`）也带 `call_id`。

## `GET/POST /api/kb/search`

参数（GET）或 JSON body（POST）：`q`、`limit`、`field`、`venue`、`year`、`auto_translate`。
`auto_translate` 默认 `true`：中文查询自动用 `glm-4-flash` 改写成英文学术关键词再检索；
显式传 `false` 可关闭（中文 CS 查询恢复 `non_en_query` 契约）。

返回关键字段：

```json
{
  "total": 29778, "total_exact": 29778, "total_display": 600,
  "cap": 600, "total_is_capped": true, "cap_scope": "pagination",
  "total_note": "total 为精确匹配数；单次最多展示/翻页 cap 条……",
  "matched_terms": ["llm"], "matched_terms_is_semantic": false,
  "matched_terms_note": "matched_terms 只是词面/关键词命中，不代表语义相关……",
  "lang": "en", "lang_supported": true,
  "items": [{"pid": "...", "n_chars": 1151, "quality": "truncated",
             "quality_reason": "正文仅 1151 字符（<3000），疑似摘要/残条目……"}],
  "sources": {
    "method": "fts5_all_vs_title_abstract",
    "source_a": {"name": "fts5", "n": 169, "returned": 3},
    "source_b": {"name": "fts5_title_abstract", "n": 124, "returned": 3},
    "fts5": {"n": 169, "returned": 3},
    "vector": {"name": "vector", "available": false, "n": 0, "returned": 0,
               "reason": "本环境未部署向量索引"},
    "agreement": 0.7391, "confidence": "medium"
  },
  "call_id": "qz_20261121_ab12cd34ef567890",
  "computed_at": "2026-11-21T12:00:00Z",
  "search_id": "srch_20261121_0123456789abcdef",
  "is_cached": false, "fetch_count": 1,
  "first_computed_at": "2026-11-21T12:00:00Z",
  "last_accessed_at": "2026-11-21T12:00:00Z"
}
```

- `call_id`：这次检索的**计算结果 ID**（内容寻址），不是调用凭据；同参数 + 同数据快照重复调用会得到同一个 `call_id`。
- `computed_at`：该结果第一次被计算的时间；`search_id`：**这一次**检索请求的唯一 ID，每次请求都不同。
- `is_cached` / `fetch_count` / `first_computed_at` / `last_accessed_at`：见「通用约定 → call_id 的语义」。
  重复调用时 `is_cached=true`、`fetch_count` 递增，`items/total` 与首次计算一致（命中落盘结果）。

- `total` 是精确匹配数；`total_display = min(total, 600)`；`total_is_capped` 表示命中数超过展示上限（不是 `total` 被截断）。
- `quality`：`ok` / `truncated`（正文 <3000 字符）/ `missing_text`（0 字符）/ `partial_metadata`。
- 中文查询：默认 `auto_translate=true`，先改写成英文学术关键词再检索，返回
  `lang="zh", lang_supported=true` 与 `translated` 字段：

  ```json
  {
    "q": "大语言模型幻觉检测", "lang": "zh", "lang_supported": true,
    "total": 221, "query_en": "large language model hallucination detection",
    "translated": {
      "query": "large language model hallucination detection",
      "method": "llm_rewrite", "model": "glm-4-flash",
      "call_id": "qz_tr_bd4fb324be88df0d",
      "keywords": ["LLM hallucination detection", "factuality assessment in LLMs"],
      "cached": false, "cache_ttl_sec": 86400
    },
    "gate": {"passed": true, "reason": "auto_translated"}
  }
  ```

  改写结果缓存 24 小时；同 query 第二次调用 `translated.cached=true`，不再调 LLM。
  改写后仍无命中时 `gate.reason="translated_no_match"`（不是 `non_en_query`）。
  非 CS 查询仍被拦截：`gate.reason="non_cs_topic"`。
- `sources`：`source_a` = FTS5 全字段，`source_b` = FTS5 标题+摘要子索引；
  `agreement` = 两个候选 pid 集合的交集 / 并集。向量索引在本环境未部署，
  因此 `vector.available=false`，不伪造数字。
  `agreement < 0.7` 时 `confidence="low"` 且带 `warning="双源不一致，结论不可靠"`。

## `GET /api/version`

返回 `schema_version` / `current` / `data_snapshot` / `apis` / `changelog` / `docs`。
所有 `/api/*` 当前版本均为 `2026-11-21`。

## CLI（`qunzhi`）

```bash
qunzhi search "retrieval augmented generation" --limit 5
qunzhi selfcheck ax_2510.19425
qunzhi baseline seed --venue top --field cs.CL
qunzhi evidence ax_2510.19425 --max-items 8
qunzhi version
qunzhi call qz_20261121_fc411c5693148859
```

环境变量：`QUNZHI_BASE`（默认 `http://127.0.0.1:5020`）、`QUNZHI_KEY`。
