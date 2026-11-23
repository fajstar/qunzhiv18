#!/bin/bash
cd /home/ubuntu/qunzhi/app || exit 1
echo "=== 搜索鉴权实现 ==="
grep -rn "需要访问口令" . --include=*.py | head -5
echo "=== PUBLIC 白名单 ==="
grep -rn "PUBLIC\|public_paths\|allowlist\|skip_auth" . --include=*.py | head -10
