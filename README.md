# 群智 v18 · 可核验的论文体检

> 把一篇论文变成一份**每条都能点回原文**的问题清单。40–80 秒，不是 AI 审稿。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## 这是什么

一个**论文自查工具**：上传 PDF / LaTeX，输出 ≤16 条带原文锚点的问题清单。

**每条问题包含五件事**：

| 栏目 | 内容 |
|---|---|
| **问题** | 缺了什么 / 哪里对不上 |
| **原文锚点** | 这段话在原文第几节第几行（**可点回**） |
| **为什么是问题** | 审稿人会怎么追问 |
| **怎么改** | 具体动作（不是"建议补充实验"这种空话） |
| **验收标准** | 改完怎么算合格 |
| **可忽略条件** | 什么情况下这条不用改（防过度修改） |

**它不是**：AI 审稿人（不打分、不替会议决定）、论文改写器（不替你重写）、万能查错（只对**能逐字核对**的问题负责）。

---

## 核心特性

### 1. 每条都能点回原文

**锚定率 99%**（T1 连续 ≥40 字逐字 / T2 部分对得上 / T3 无依据直接拦掉）。

**这是和"AI 说你有问题"最大的区别** —— 你能当场判断该不该信。

### 2. 数据底座

| 资产 | 数量 |
|---|---|
| **论文库** | **904,313 篇**（arXiv / ACL / OpenReview / 顶会） |
| **有全文** | **363,945 篇（40.2%）** |
| **benchmark 分数** | **205,840 条**（Papers with Code 官方） |
| **benchmark 数** | 7,432 个 |
| **模型数** | 33,176 个 |
| **评审** | 149,288 条 |

### 3. 四关三审

| 闸门 | 作用 |
|---|---|
| **立项关** | 题目值不值得做 |
| **领域关** | 领域知识是否覆盖 |
| **取证关** | 每个结论是否有原文依据 |
| **钉锚关** | 锚点是否真的定位到原文 |
| **三堂会审** | 三个跨家族模型互审（决策文档） |

### 4. 口径敏感

同一指标在不同口径下会变（`venue=all` vs `venue=top` 差 2.86 pp），
所以每个数字都带 `call_id` + `denominator` + `venue` + `sensitivity`。

---

## 架构

```
┌─────────────────────────────────────────┐
│  nginx :8443（HTTPS）                    │
└──────────────┬──────────────────────────┘
               │
┌──────────────▼──────────────────────────┐
│  main_gateway.py :5020（FastAPI 网关）   │
│  ├── /api/kb/*        论文检索/自检/基线 │
│  ├── /api/evidence    证据锚定           │
│  ├── /api/benchmark   benchmark 查询     │
│  ├── /api/pwc/*       PwC 官方数据       │
│  ├── /api/gates/*     四关状态           │
│  └── /api/review/v6   三堂会审           │
└──────────────┬──────────────────────────┘
               │
┌──────────────▼──────────────────────────┐
│  v18 引擎 :18097（vericite）             │
│  ├── review_v6.py     跨家族评审         │
│  ├── evidence.py      锚点抽取           │
│  └── privacy_scrubber 上传脱敏           │
└──────────────┬──────────────────────────┘
               │
┌──────────────▼──────────────────────────┐
│  SQLite: paperlib.sqlite（12 GB）         │
│  ├── papers          904,313             │
│  ├── fulltext_z      365,855（压缩）      │
│  ├── pwc_scores      205,840             │
│  ├── reviews         149,288             │
│  └── issues          767,753             │
└─────────────────────────────────────────┘
```

---

## 快速开始

### 在线体验

```
https://qunzhi.team:8443/portal
```

访问 key：`qunzhi2026`（演示用，1000 次/天限额）

### 本地部署

```bash
# 1. 克隆
git clone git@github.com:fajstar/qunzhiv18.git
cd qunzhiv18

# 2. 装依赖
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. 配置（复制模板，填你自己的 key）
cp .env.example .env
# 编辑 .env，填 DEEPSEEK_API_KEY / GLM_API_KEY / QZ_ADMIN_KEY

# 4. 启动
python main_gateway.py
# 打开 http://127.0.0.1:5020/portal
```

**注意**：完整论文库（12 GB）不在仓库里，需要自己导入或下载公开快照。

---

## API 示例

### 论文检索（免 key）

```bash
curl -X POST https://qunzhi.team:8443/api/kb/search \
  -H "Content-Type: application/json" \
  -d '{"q": "contrastive learning", "limit": 10}'
```

返回带 `call_id`，可回查：

```bash
curl https://qunzhi.team:8443/api/call/qz_20261121_xxx
```

### 单篇体检

```bash
curl -X POST https://qunzhi.team:8443/api/v1/review \
  -H "Content-Type: application/json" \
  -d '{"title": "...", "text": "...(≥200字)...", "field": "cs.CL"}'
```

### benchmark 查询

```bash
curl "https://qunzhi.team:8443/api/pwc/scores?dataset=MMLU&model=GPT-4o"
```

### API 分层

| 类型 | 例子 | key |
|---|---|---|
| **公开**（免 key） | `/api/kb/search`、`/api/benchmark`、`/api/pwc/*` | ❌ 不需要 |
| **私有**（需 key） | `/api/kb/upload`、`/api/evidence` | ✅ 需要 |

---

## 隐私

**用户上传的论文 = 最敏感数据，绝不出本机。**

| 层 | 数据 | 开源 | API |
|---|---|---|---|
| **公开层** | 论文库（arXiv/OpenReview 元数据） | ✅ | ✅ 免 key |
| **公开层** | benchmark（PwC 官方） | ✅ | ✅ 免 key |
| **私有层** | **用户上传的论文** | ❌ **绝不开源** | ❌ **仅本机** |
| **私有层** | 评审原文（人名） | ❌ | ⚠️ 脱敏后 |
| **私有层** | API key / 凭证 | ❌ | ❌ 绝不返回 |

**PrivacyScrubber**：上传时自动脱敏（作者/单位/邮箱/基金号/评审人 ID），
**数字和算法名保留**（对账需要）。

---

## 技术栈

| 层 | 技术 |
|---|---|
| **网关** | FastAPI + uvicorn |
| **数据库** | SQLite（WAL）+ FTS5 |
| **检索** | FTS5 BM25 + 向量（可选）+ RRF |
| **LLM** | DeepSeek / GLM / Qwen（可替换） |
| **前端** | 原生 JS + CSS（无框架） |

---

## 数据来源（CC-BY-SA 4.0）

| 源 | 数量 | 许可 |
|---|---|---|
| **arXiv** | 309,713 | 开放 |
| **Papers with Code** | 205,840（分数）+ 309,058（元数据） | CC-BY-SA 4.0 |
| **ACL Anthology** | 81,453 | 开放 |
| **OpenReview** | 54,698 | 开放 |
| **顶会采集** | 74,583 | 开放 |

---

## 相关仓库

| 仓库 | 说明 |
|---|---|
| [**fajstar/qunzhi**](https://github.com/fajstar/qunzhi) | **v15**：多智能体决策预演（316 次实验） |
| [**fajstar/qunzhiv18**](https://github.com/fajstar/qunzhiv18) | **v18**：可核验的论文体检（本仓库） |

---

## 许可

MIT License —— 见 [LICENSE](LICENSE)

数据部分遵循各自原始许可（CC-BY-SA 4.0 / 开放获取）。

---

## 联系

- **在线 Demo**：https://qunzhi.team:8443/portal
- **Issue**：https://github.com/fajstar/qunzhiv18/issues

---

*群智 v18 · 2026-11-23（数据快照 `paperlib-b21b77e32c`）*
