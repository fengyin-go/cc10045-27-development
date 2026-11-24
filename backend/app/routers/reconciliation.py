"""对账追溯路由：对账快照作废 / 替代确认、待发版本校验、例行正式报送和催核通知。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services import service_for

router = APIRouter(prefix="/api/reconciliation", tags=["对账追溯"])


class VoidPayload(BaseModel):
    reason: str
    operator: str | None = None


class ReplacementPayload(BaseModel):
    replacement_snapshot_id: int
    reason: str
    operator: str | None = None


class VerifyPayload(BaseModel):
    note: str | None = None
    verified_value: float | None = None
    metric_name: str | None = None
    operator: str | None = None


class VersionPayload(BaseModel):
    snapshot_id: int
    operator: str | None = None


class PausePayload(BaseModel):
    reason: str
    operator: str | None = None


class SubmitPayload(BaseModel):
    output_id: int | None = None
    recipients: Any = None
    shift: str | None = None
    operator: str | None = None


class NoticePayload(BaseModel):
    snapshot_id: int
    recipients: Any = None
    note: str | None = None
    output_id: int | None = None
    operator: str | None = None


def _snapshot_service():
    return service_for("report_snapshot")


@router.get("/overview")
def reconciliation_overview() -> dict[str, Any]:
    """对账控制台：快照生命周期、待发版本校验、投递历史与审计事件。"""
    return _snapshot_service().reconciliation_overview()


@router.get("/snapshots/{snapshot_id}")
def snapshot_detail(snapshot_id: int) -> dict[str, Any]:
    snapshot = _snapshot_service().describe_snapshot(snapshot_id)
    return {"ok": snapshot is not None, "snapshot": snapshot}


@router.post("/snapshots/{snapshot_id}/void")
def void_snapshot(snapshot_id: int, payload: VoidPayload) -> dict[str, Any]:
    """作废对账快照，记录失效原因；重复作废幂等，不改写已发送报告。"""
    return _snapshot_service().void_snapshot(
        snapshot_id, reason=payload.reason, operator=payload.operator)


@router.post("/snapshots/{snapshot_id}/replacement")
def confirm_replacement(snapshot_id: int, payload: ReplacementPayload) -> dict[str, Any]:
    """确认作废快照与替代快照的关系；替代快照核实前不能用于正式报送。"""
    return _snapshot_service().confirm_replacement(
        snapshot_id,
        replacement_snapshot_id=payload.replacement_snapshot_id,
        reason=payload.reason,
        operator=payload.operator,
    )


@router.post("/snapshots/{snapshot_id}/verify")
def verify_snapshot(snapshot_id: int, payload: VerifyPayload) -> dict[str, Any]:
    """补充核实快照，可写入更正后的数值（如 100 → 94）。"""
    return _snapshot_service().verify_snapshot(
        snapshot_id,
        note=payload.note or "",
        verified_value=payload.verified_value,
        metric_name=payload.metric_name,
        operator=payload.operator,
    )


@router.post("/reports/{report_id}/versions")
def create_version(report_id: int, payload: VersionPayload) -> dict[str, Any]:
    """基于核实后的对账快照生成新的待发报告版本，并记录数据出处。"""
    return _snapshot_service().create_version_from_snapshot(
        report_id, payload.snapshot_id, operator=payload.operator)


@router.get("/reports/{report_id}/pending")
def pending_versions(report_id: int) -> dict[str, Any]:
    versions = _snapshot_service().pending_versions(report_id)
    return {"ok": True, "report_id": report_id, "items": versions, "total": len(versions)}


@router.get("/reports/{report_id}/deliveries")
def report_deliveries(report_id: int) -> dict[str, Any]:
    """已发送内容：按原凭据返回，后续快照作废 / 替代 / 核实都不改写。"""
    items = _snapshot_service().report_deliveries(report_id)
    return {"ok": True, "report_id": report_id, "items": items, "total": len(items)}


@router.get("/outputs/{output_id}")
def output_detail(output_id: int) -> dict[str, Any]:
    view = _snapshot_service().output_view(output_id)
    return {"ok": view is not None, "output": view}


@router.post("/outputs/{output_id}/pause")
def pause_output(output_id: int, payload: PausePayload) -> dict[str, Any]:
    """暂停待发报告等待替代快照核实。"""
    return _snapshot_service().pause_output(
        output_id, reason=payload.reason, operator=payload.operator)


@router.post("/outputs/{output_id}/resume")
def resume_output(output_id: int) -> dict[str, Any]:
    return _snapshot_service().resume_output(output_id)


@router.post("/reports/{report_id}/submit-routine")
def submit_routine(report_id: int, payload: SubmitPayload) -> dict[str, Any]:
    """提交本班例行报送：仅当对账数据有效且已核实才作为正式对账提交。"""
    return _snapshot_service().submit_routine(
        report_id,
        output_id=payload.output_id,
        operator=payload.operator,
        shift=payload.shift,
        recipients=payload.recipients,
    )


@router.post("/reports/{report_id}/notice")
def send_notice(report_id: int, payload: NoticePayload) -> dict[str, Any]:
    """发送带作废原因的催核通知；通知本身不是正式付款凭证。"""
    return _snapshot_service().send_notice(
        report_id,
        snapshot_id=payload.snapshot_id,
        recipients=payload.recipients,
        note=payload.note,
        output_id=payload.output_id,
        operator=payload.operator,
    )


@router.get("/deliveries/{delivery_id}")
def delivery_detail(delivery_id: int) -> dict[str, Any]:
    delivery = _snapshot_service().describe_delivery(delivery_id)
    return {"ok": delivery is not None, "delivery": delivery}
