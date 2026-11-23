# 群智网关 API CHANGELOG

> 规则：只加字段、不改旧字段语义；任何破坏性变更必须提升 `schema_version` 并在此登记。
> 当前 `schema_version`：**2026-11-21**；`api_version`：**v2026-11-21**。
> 在线核对：`GET /api/version`（公开）。

## 2026-11-21 — 检索 call_id + call_id 访问语义

### 新增（只加字段，旧字段语义不变）

- **`GET/POST /api/kb/search` 新增 `call_id` / `computed_at` / `search_id`**：
  - `call_id` 内容寻址：`接口 + 归一化参数（q/limit/field/venue/year/decision/auto_translate） + api_version + data_snapshot`；
    同参数 + 同快照重复调用得到同一 `call_id`；
  - `computed_at` = 该结果第一次计算时间；`search_id` = 这一次请求的唯一 ID（每次不同）；
  - 命中同一 `call_id` 的已落盘结果时直接复用 `items/total`，并返回 `cache_note`。
- **所有带 `call_id` 的响应新增四个字段**：`is_cached`、`fetch_count`、`first_computed_at`、`last_accessed_at`；
  - 覆盖 `selfcheck`、`defect_baseline`、`evidence`、`kb/search` 以及 `GET /api/call/{call_id}`；
  - `fetch_count` 统计业务接口返回该 `call_id` 的次数，首次为 1，重复请求递增；`/api/call` 回看不计入；
  - 每个 `call_id` 一份旁路统计 `app/data/calls/_stats/<call_id>.json`（线程锁 + flock + 原子替换），不改旧 payload 字段。
- **`GET /api/version` 的 `calls` 段新增** `call_id_kind` / `semantics` / `fields` / `fetch_count_note`。
- **文档明确**：`call_id` 是“计算结果 ID”（内容寻址），**不是调用凭据/鉴权 token**；同一 `call_id` 可对应多次调用，
  不能当 key 或幂等令牌使用。见 `API.md`「通用约定 → call_id 的语义」。
- 改完重启网关（`kill -TERM` + `Restart=always`，以 `MainPID`/`ExecMainStartTimestamp` 确认），
  curl 实测见 `tasks/fix-search-callid.md` 报告。

## 2026-11-21 — 双源交叉验证（cross_check / sources）

### 新增（只加字段，旧字段语义不变）

- **`GET /api/kb/selfcheck` 新增 `cross_check`**：
  - `method=two_source_agreement`；`source_a=feature_extractor`（`paper_features` 表），
    `source_b=rule_judge_v2`（另一套独立正则，与 `features_extract_v2.py` 的 FEATS 不同）；
  - `agreement` = 20 项 `has` 布尔值一致率；`disagreements[]` 逐项列出 `feature/a/b/note`；
  - `agreement < 0.7` → `confidence="low"` + `warning="双源不一致，结论不可靠"`。
- **`GET /api/kb/defect_baseline` 新增 `cross_check`**：
  - `method=sqlite_vs_fts`；`source_a=sqlite_scan`（SQLite 全量，与本次响应同口径），
    `source_b=fts5_sample`（FTS5 索引按 rowid 确定性抽 5000 篇）；
  - `agreement = 1 - |rate_a-rate_b| / max(rate_a, rate_b)`；
  - 不传 `feature` 时固定核 `seed`，返回里 `cross_check.feature` 写明。
- **`GET/POST /api/kb/search` 新增 `sources`**：
  - `source_a=fts5`（全字段），`source_b=fts5_title_abstract`（标题+摘要子索引）；
  - `agreement` = 两个候选 pid 集合的交集 / 并集；
  - `vector` 显式标 `available=false`（本环境未部署向量索引，不伪造数字）；
  - `agreement < 0.7` → `sources.confidence="low"` + `sources.warning`。
- 三个新字段都带 `confidence`（high/medium/low）与 `note`，阈值：`>=0.95 high`、`>=0.70 medium`、`<0.70 low`。
- 改完重启网关（`kill -TERM` + `Restart=always`；经 `systemctl show -p MainPID -p ExecMainStartTimestamp` 确认新进程，最终 MainPID 846174/18:27:32），curl 实测见 `tasks/cross-check.md` 报告。

## 2026-11-21 — 平台可证伪性修复（schema 2026-11-21）

### 新增（全部为新增字段，旧字段语义不变）

- **所有 `/api/*` 返回**：JSON 新增 `schema_version`、`api_version`（middleware 统一注入，含 401/403/429）；响应头统一带 `X-API-Schema-Version`（覆盖 HTML/SSE 等非 JSON）。
- **新增 `GET /api/version`**：列出当前版本、每个 `/api/*` 的版本、数据快照与 changelog。
- **`GET /api/kb/selfcheck`**：
  - `call_id` / `computed_at` / `version` / `data_snapshot` / `reproduce`；
  - `denominator: {n, description}`、`numerator: {n, description}`、`metric`、`scope`；
  - `rows[].n`、`rows[].papers`、`rows[].rate`（每项都带分母）。
- **`GET /api/kb/defect_baseline`**：
  - `venue` / `venue_effective` / `venue_default` / `venue_requested` / `venue_source`；
  - `metric` / `scope`；
  - `by_field.<field>.n`（分母）与 `papers` / `rate` / `verdict`；
  - `call_id` / `computed_at` / `version` / `denominator` / `numerator` / `reproduce`。
- **`POST /api/evidence`**：
  - `call_id` / `computed_at` / `version` / `denominator` / `numerator` / `reproduce`；
  - 支持请求体带 `call_id` 重放；`GET /api/call/{call_id}` 可原样回查落盘结果；
  - 引擎有随机性：`call_id` 标识“这一次计算”，同参数重算会得到新 `call_id`；旧 `call_id` 永远能原样回放（落盘 `data/calls/`）；
  - 降级响应（`ok=false, degraded=true`）同样带 `call_id`。
- **`GET/POST /api/kb/search`**：
  - `total_exact` / `total_display` / `cap: 600` / `total_is_capped` / `cap_scope` / `total_note`；
  - `matched_terms`（顶层）+ `matched_terms_is_semantic: false` + `matched_terms_note`；
  - `lang` / `lang_supported` / `lang_note`（中文查询实测记录）；
  - `items[].quality` / `items[].quality_reason`（正文 <3000 字符标记 `truncated`）。
- **新增 `GET /CHANGELOG.md` / `GET /API.md`**：本文件 + 人读 API 文档（默认值、字段口径、call_id 回放、CLI）。

### 修正（不改变字段名，只修正口径/数据）

- `selfcheck.n_features` 口径固定为「自检清单总项数 = len(rows)」；命中数见 `n_features_present`，原始特征数见 `n_features_detected`（2026-11-21 早间已上线，本次补登）。
- `defect_baseline` 的 `venue` 默认值**固定为 `all`**，并在返回中回显 `venue_source=default`；不再存在“同一接口静默换口径”。

### 未变

- 旧字段 `total`、`papers`、`rate`、`verdict`、`_filtered_by`、`n_features`、`anchored`、`items` 等名称与语义保持兼容。

## 2026-11-20

- 分层基线 v3：`by_venue` 支持 `top` / `arxiv` / `peer` / `all`。
- `defect_baseline` 增加 `by_field._filtered_by` 口径标注（修复 by_field 大于 overall 的子集矛盾）。
