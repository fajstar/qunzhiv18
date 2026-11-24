import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest


@pytest.fixture
def client():
    """网关测试客户端；缺依赖/缺数据时自动跳过，而不是报错。

    原则：clone 下来没有论文库、没有 .env 的机器，跑 pytest 应该是
    "通过 + 跳过"，不能是 43 个 ERROR。
    """
    try:
        from fastapi.testclient import TestClient
    except Exception as e:  # fastapi 未安装
        pytest.skip(f"未安装 fastapi.testclient：{type(e).__name__}")

    try:
        import main_gateway

        app = main_gateway.app
    except Exception as e:  # 缺 sandbox/app.py、缺 .env、缺库
        pytest.skip(f"网关不可用：{type(e).__name__}: {str(e)[:80]}")

    return TestClient(app)
