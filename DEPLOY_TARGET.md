# 🚫 部署目标标注（所有 AI 必读，勿搞错主机）

> **本项目的部署目标只有一台主机。搞错会覆盖生产环境或写进算力节点。**

## ✅ 唯一允许部署的主机

| 项 | 值 |
|---|---|
| 主机 | **139.155.129.66** |
| 端口 | 22 |
| 用户 | `ubuntu`（免密 sudo） |
| 私钥 | `C:\共享\qunzhi-kit\keys\new_server`（RSA 2048） |
| 代码目录 | `/home/ubuntu/qunzhi/app` |
| Python | `/home/ubuntu/qunzhi/venv/bin/python` |
| 对外域名 | `qunzhi.team`（解析到 139.155.129.66） |

## ❌ 禁止部署的主机

| 主机 | 是什么 | 原因 |
|---|---|---|
| **134.175.145.241** | 算力节点（2C/3.6G） | 只跑实验，不是生产；主机密钥已变，密码/密钥均已失效 |

## ⛔ 禁止的操作

1. **不要整文件覆盖 `/home/ubuntu/qunzhi/app/main_gateway.py`**。
   该文件在服务器上有**未提交改动**（git 停在 `f858bdd init: 迁移后首次快照`，`git status` 显示 `M main_gateway.py` 等），
   覆盖会丢掉服务器上的自有代码。
2. 不要 `git reset --hard` / `git checkout --` 服务器上的工作区。
3. 不要重启与本次改动无关的服务（`qunzhi-dsh-*` 那些是实验任务）。

## ✅ 允许的操作

1. **只新增文件**（如 `review_cache.py`、`tests/test_review_cache.py`）——零冲突；
2. 修改 `main_gateway.py` 必须按 [WIRING_v6_cache.md](WIRING_v6_cache.md) **手工合并**，由项目主人决定何时合；
3. 任何改动前先备份：`cp -a <文件> <文件>.bak_$(date +%Y%m%d_%H%M%S)`。

## 服务器现状快照（2026-11-27 采集）

```
main_gateway.py  sha256 = ac91eba1c7590b70004b25a887f489e099dd6a7074b2b51ec374fc341e8f2904
                 行数   = 8769
git HEAD         = f858bdd  init: 迁移后首次快照
git status       = M .gitignore / M guard.py / M main_gateway.py / M review_v6.py
                   D guard.py.night_bak / D main_gateway.py.night_bak
运行进程          = /home/ubuntu/qunzhi/venv/bin/python /home/ubuntu/qunzhi/app/main_gateway.py
```

若上述 sha256 变了，说明服务器代码已被改动——**部署前必须重新确认**，不要沿用本文件里的判断。

## 本次已完成（2026-11-27 合并 + 验收）

| 动作 | 结果 |
|---|---|
| 上传 `review_cache.py` | ✅ 远端 sha256 `14ee1be8…` |
| 上传 `tests/test_review_cache.py` | ✅ 远端 8 passed in 0.08s |
| 合并 `main_gateway.py`（3 处锚点，各命中 1 次） | ✅ +27 行 / +915 字符 |
| 合并前 sha256 | `ac91eba1c7590b70…`（8770 行） |
| 合并后 sha256 | `7c291d6ac5cb1c3d…`（8797 行） |
| 备份 | `/home/ubuntu/qunzhi/app/main_gateway.py.bak_cache_20261127_133910` |
| py_compile | ✅ COMPILE_OK |
| 重启 `qunzhi-gateway` | ✅ active |
| **实测缓存** | 第 1 次 `cached=false` → 第 2 次 `cached=true / age=0` ✅ |
| 缓存文件 | `app/data/review_v6_cache/ff07b6a175b1a736be6b5b86.json`（11 KB） |

回滚命令（如需）：

```bash
cp -a /home/ubuntu/qunzhi/app/main_gateway.py.bak_cache_20261127_133910       /home/ubuntu/qunzhi/app/main_gateway.py
sudo systemctl restart qunzhi-gateway
```

> 注意：合并后服务器上的 `main_gateway.py` 与你原来的"未提交改动"**叠加**在一起了
> （即：你的改动 + 本次缓存接线）。git status 仍显示 `M main_gateway.py`，这是预期的。
