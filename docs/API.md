# 群智 local · 5 个零隐私云端接口

> Base URL：`https://qunzhi.team:8443/api/pub`（可在 `config.json` / 页面设置里改）
> 免 key，公开层，只返回统计值 / 公开数据 / 候选列表。
> 服务端对字段做**硬白名单**：出现 `text` / `abstract` / `author` / `email` 等字段直接 HTTP 400。

## 统一约定

```text
只接受：feat(特征名) / field(领域) / refs(文献标题) / pid / q(关键词)
绝不接受：text(正文) / abstract(摘要) / author(作者) / email
只返回：统计值 / 公开数据 / 候选列表
免 key，但限流（按 IP，滑动窗口）
```

| 接口 | 方法 | 限流 |
|---|---|---|
| `/baseline` | POST | 40 次 / 分钟 |
| `/missing_cites` | POST | 20 次 / 分钟 |
| `/benchmark` | GET | 60 次 / 分钟 |
| `/search` | GET | 60 次 / 分钟 |
| `/version` | GET | 120 次 / 分钟 |

超限返回 `429`，带 `Retry-After` 头。

### 错误结构

```json
{
  "ok": false,
  "error": "privacy_rejected",
  "message": "隐私铁律：/api/pub/* 绝不接受正文/摘要/作者/邮箱；字段 'text' 被拒绝。只接受 feat, field。",
  "rejected": ["text"],
  "accepted": ["feat", "field"]
}
```

| HTTP | error | 含义 |
|---|---|---|
| 400 | `privacy_rejected` | 传了正文/摘要/作者/邮箱等禁止字段 |
| 400 | `unknown_field` | 字段不在白名单（未知 key / 未知 field） |
| 400 | `unknown_feat` | feat 不在 20 项名单里 |
| 400 | `invalid_refs` | refs 不是字符串数组，或全部过短 |
| 413 | `body_too_large` | 请求体超过 64 KB（正常请求远小于此） |
| 429 | `rate_limited` | 触发限流 |
| 503 | `db_unavailable` / `refs_index_missing` | 服务端数据未就绪 |

---

## 1. POST `/baseline`

特征 × 领域的同行基线。**只返回统计，不返回论文列表。**

### 请求

```bash
curl -s -X POST https://qunzhi.team:8443/api/pub/baseline \
  -H 'Content-Type: application/json' \
  -d '{"feat":"seed","field":"cs.LG"}'
```

```json
{"feat": "seed", "field": "cs.LG"}
```

`feat` 可省略，默认 `seed`；`field` 可空字符串表示全领域。

允许的 `feat`（与 `paper_features` 一致，共 20 项）：

```text
seed, ci, ablation, hyperparameter, dataset_stats, code,
sota_compare, baseline_table, related_work_table, human_eval,
multiple_runs, cross_val, threat_validity, ethics, limitations,
novelty_statement, claim_hedging, compute_budget, released_data,
significance
```

### 返回（实测，2026-11-24）

```json
{
  "ok": true,
  "feat": "seed",
  "field": "cs.LG",
  "denominator": 34520,
  "missing": 31071,
  "with_feat": 3449,
  "rate": 0.9001,
  "present_rate": 0.0999,
  "rate_meaning": "rate = missing / denominator（缺失率）；present_rate = with_feat / denominator",
  "scope_note": "分母 = L1+L2 中该 field 的论文数；field 为空表示全领域",
  "snapshot": "paperlib-3a3ef2a87c",
  "privacy": "只收到 feat + field，没有收到任何论文内容"
}
```

**口径**：

- `denominator` = 该 field 中 `quality_level in ('L1','L2')` 的论文数。
- `missing` = 其中 `paper_features` 没有该 feat 的论文数。
- `rate` = `missing / denominator`（**缺失率**，与任务样例口径一致）。
- `present_rate` = `with_feat / denominator`（做到该做法的比例）。

---

## 2. POST `/missing_cites`

**最关键的接口**：入参是你自己的参考文献标题（公开信息），不是 pid，也不含正文。

方法：`共引≥2 + 同 field + ≥20% 兄弟`。兄弟不足 3 篇时降级为「共引≥1」并在 `min_co_citation` / `method` 里标注。

### 请求

```bash
curl -s -X POST https://qunzhi.team:8443/api/pub/missing_cites \
  -H 'Content-Type: application/json' \
  -d '{
    "refs": [
      "Deep Residual Learning for Image Recognition",
      "Attention Is All You Need",
      "BERT: Pre-training of Deep Bidirectional Transformers"
    ],
    "field": "cs.CV",
    "siblings": 30
  }'
```

- `refs`：1–50 条字符串，每条 3–300 字符；服务端会做小写/标点归一化并去重。
- `field`：可选；不传表示不限领域（建议传，精度更高）。
- `siblings`：5–100，默认 30。
- `top`：1–50，默认 10。

### 返回（实测，截取前 3 条候选）

```json
{
  "ok": true,
  "refs_given": 3,
  "refs_used": 3,
  "field": "cs.CV",
  "siblings": 30,
  "threshold": 6,
  "min_co_citation": 2,
  "candidates": [
    {"ref_title": "ImageNet: A large-scale hierarchical image database", "cited_by": 17, "ratio": 56.7},
    {"ref_title": "An Image is Worth 16x16 Words: Transformers for Image Recognition at Scale", "cited_by": 15, "ratio": 50.0},
    {"ref_title": "Swin Transformer: Hierarchical Vision Transformer using Shifted Windows", "cited_by": 15, "ratio": 50.0}
  ],
  "method": "共引≥2 + 同 field + ≥20% 兄弟",
  "snapshot": "paperlib-3a3ef2a87c",
  "privacy": "只收到文献标题 + field；标题是公开信息，没有收到正文/摘要/作者",
  "note": "兄弟论文 = 同 field 且引用了你给的参考文献；候选 = 兄弟里至少 threshold 篇引用、你的列表里没有的文献"
}
```

`ratio` = `cited_by / siblings * 100`。候选为空是正常结果，不是错误。

---

## 3. GET `/benchmark?pid=...`

公开论文的 benchmark 分数。

```bash
curl -s 'https://qunzhi.team:8443/api/pub/benchmark?pid=ax_2611.26510'
```

```json
{
  "ok": true,
  "pid": "ax_2611.26510",
  "paper": null,
  "has_benchmark": true,
  "n_scores": 6,
  "scores": [
    {
      "benchmark": "BrowseComp",
      "model": "Qwen3.5-4B",
      "method": "AutoMaster",
      "score": 46.13,
      "score_std": 2.98,
      "ci_low": -1.08,
      "ci_high": 6.85,
      "n": 185,
      "source": "paper",
      "source_url": "https://arxiv.org/abs/2611.26510v1",
      "computed_at": "2026-11-21T14:44:04Z"
    }
  ],
  "benchmarks": ["BrowseComp", "Doc2Repo", "Terminal-Bench 2.1"],
  "snapshot": "paperlib-3a3ef2a87c",
  "scope_note": "只返回公开 benchmark 分数；无分数时 scores 为空数组"
}
```

`paper` 为 `null` 表示 benchmark 表里有这个 id、但论文元数据表没有（公开快照口径差异）；`scores` 仍照常返回。无分数时返回 `has_benchmark: false` + 空数组，不报错。

---

## 4. GET `/search?q=...&limit=...`

关键词检索公开库；只返回标题、年份、领域、venue、pid。

```bash
curl -s 'https://qunzhi.team:8443/api/pub/search?q=attention&limit=3'
```

```json
{
  "ok": true,
  "q": "attention",
  "field": "all",
  "returned": 3,
  "items": [
    {"pid": "ax_2511.20102", "title": "SSA: Sparse Sparse Attention by Aligning Full and Sparse Attention Outputs in Feature Space", "year": 2025, "field": "cs.CL", "venue": "arXiv"},
    {"pid": "ax_2412.17019", "title": "Reversed Attention: On The Gradient Descent Of Attention Layers In GPT", "year": 2024, "field": "cs.CL", "venue": "arXiv"},
    {"pid": "ax_2312.08874", "title": "Agent Attention: On the Integration of Softmax and Linear Attention", "year": 2023, "field": "cs.CV", "venue": "arXiv"}
  ],
  "snapshot": "paperlib-3a3ef2a87c",
  "scope_note": "只返回标题/年份/领域/venue/pid，不返回摘要或作者"
}
```

- `q` 最长 200 字符；`limit` 1–30，默认 10；`field` 可选。
- 只命中公开标题/摘要索引，不返回摘要正文。

---

## 5. GET `/version`

版本、快照、库规模、数据截止日。

```bash
curl -s https://qunzhi.team:8443/api/pub/version
```

```json
{
  "ok": true,
  "version": "pub-v1",
  "snapshot": "paperlib-3a3ef2a87c",
  "papers": 904313,
  "papers_l1l2": 93676,
  "features_papers": 258721,
  "fulltext": 365855,
  "benchmark_scores": 886,
  "refs_indexed": 952784,
  "data_cutoff": "2026-11-23",
  "refs_index_built_at": "2026-11-24T01:07:05+0800",
  "endpoints": [
    "POST /api/pub/baseline",
    "POST /api/pub/missing_cites",
    "GET  /api/pub/benchmark?pid=",
    "GET  /api/pub/search?q=&limit=",
    "GET  /api/pub/version"
  ],
  "allowed_inputs": ["feat", "field", "refs", "pid", "q"],
  "never_accepts": ["text", "abstract", "author", "email"],
  "privacy": "免 key；只返回统计/公开数据；服务器不接收论文正文、摘要、作者、邮箱",
  "rate_limit_per_min": {"baseline": 40, "missing_cites": 20, "search": 60, "benchmark": 60, "version": 120}
}
```

---

## 自测：隐私拒绝

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  https://qunzhi.team:8443/api/pub/baseline \
  -H 'Content-Type: application/json' \
  -d '{"feat":"seed","text":"正文不应上传"}'
# → 400

curl -s -o /dev/null -w '%{http_code}\n' \
  'https://qunzhi.team:8443/api/pub/search?q=attention&text=hello'
# → 400
```

## 备注

- 服务端实现：`app/pub_api.py`；路由挂在既有 FastAPI 网关上，未改动任何老接口。
- `missing_cites` 使用派生索引 `data/pub_refs.sqlite`（由 `build_pub_refs.py` 从公开文献标题生成），避免每次扫 96 万行主表。
- 所有接口只读 SQLite（`mode=ro`），不写库。
- 返回里可能额外带 `schema_version` / `api_version`（网关统一中间件注入），属于正常字段。
