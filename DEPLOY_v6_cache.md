# qunzhiv18 · v6 缓存部署清单

> 目标：把 `review_cache.py` + `/api/review/v6` 缓存接线部署到服务器，让"同一报告两次评审结果一致"。

## 0. 前置：登录

```bash
ssh ubuntu@134.175.145.241
# 本机已知凭据全部失效（主机密钥已变），需要：
#   A. 新密码；或
#   B. 把本机公钥加进服务器（推荐，见下）
```

本机公钥（加到 `~/.ssh/authorized_keys` 即可免密）：

```
<本机 id_ed25519.pub 内容，见下方附录>
```

## 1. 备份

```bash
cd /home/ubuntu/qunzhi            # app 目录（main_gateway.py 所在）
cp -a main_gateway.py main_gateway.py.bak_$(date +%Y%m%d_%H%M%S)
```

## 2. 上传三个文件

在**本地**执行（PowerShell）：

```powershell
cd C:\比赛\qunzhiv18
scp main_gateway.py review_cache.py ubuntu@134.175.145.241:/home/ubuntu/qunzhi/
scp tests/test_review_cache.py ubuntu@134.175.145.241:/home/ubuntu/qunzhi/tests/
```

## 3. 编译 + 跑缓存测试

```bash
cd /home/ubuntu/qunzhi
python3 -m py_compile main_gateway.py review_cache.py && echo COMPILE_OK
python3 -m pytest -q tests/test_review_cache.py      # 应 8 passed
```

> 若无 pytest：`pip install pytest`（或用 venv：`/home/ubuntu/qunzhi/venv/bin/python -m pytest`）

## 4. 重启网关

```bash
# 先看服务名
systemctl list-units --type=service --no-pager | grep -iE 'qunzhi|gateway'

# 方式 A（有 systemd 单元）
sudo systemctl restart <服务名>

# 方式 B（进程由 Restart=always 托管）
sudo kill -TERM $(pgrep -f main_gateway.py | head -1)

# 方式 C（手动起的 uvicorn）
sudo systemctl status <服务名> || ps aux | grep main_gateway
```

## 5. 验收（三条）

```bash
# ① 服务活着
curl -s http://127.0.0.1:5020/api/kb/stats | head -c 120

# ② schema 里能看到缓存说明（新代码才有）
curl -s http://127.0.0.1:5020/api/review/v6/schema | grep -o 'cached' | head -1

# ③ 缓存目录会被创建（跑一次评审后）
ls -la data/review_v6_cache/ | head
```

## 6. 回滚（如出问题）

```bash
cd /home/ubuntu/qunzhi
cp -a main_gateway.py.bak_<时间戳> main_gateway.py
sudo systemctl restart <服务名>
```

## 附录：本机公钥

```
（执行 `type $env:USERPROFILE\.ssh\id_ed25519.pub` 取）
```
