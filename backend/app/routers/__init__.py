"""业务路由汇总：资源路由 + 管道路由 + 对账追溯路由。"""
from __future__ import annotations

from app.routers import resource
from app.routers import pipeline
from app.routers import reconciliation

ROUTERS = [resource]
PIPELINE_ROUTER = pipeline.router
RECONCILIATION_ROUTER = reconciliation.router
