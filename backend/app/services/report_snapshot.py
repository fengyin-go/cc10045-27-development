"""报告快照领域服务：保存报告的完整数据快照并支持历史版本对比。

在快照对比之外，本服务承载「对账快照」的完整生命周期：

- 作废：记录失效原因、时间、操作人，已作废快照不能再为新正式报送提供数值；
- 替代确认：记录作废快照与替代快照的替代关系，替代快照未核实前不能用于正式对账；
- 核实：补充核实后的正确数值（如 100 → 94），核实是正式报送的前置条件；
- 版本校验：待发报告版本所引用的对账数据必须存在、未作废且已核实；
- 正式报送与催核通知：正式报送落一份不可变投递副本；催核通知单独标记为非正式付款凭证；
- 追溯：已发送内容始终可按原凭据核查，后续作废 / 替代 / 核实都不改写历史。

真实数据流：report_outputs（报告版本）→ snapshots（对账数据快照）→ report_deliveries（不可变
投递副本）→ reconciliation_events（作废 / 替代 / 核实 / 报送审计日志）。
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.database import db
from app.services.base import BaseModuleService


SNAPSHOT_VOIDED = "已作废"
VERIFICATION_PENDING = "待核实"
VERIFICATION_VERIFIED = "已核实"
VERIFICATION_REJECTED = "核实有误"

DELIVERY_PENDING = "待发送"
DELIVERY_PAUSED = "已暂停"
DELIVERY_SENT = "已发送"

FORMAL_DELIVERY_KIND = "正式对账报告"
NOTICE_DELIVERY_KIND = "催核通知"

# 动作别名：页面按钮、通用动作接口和领域接口使用同一套语义。
VOID_ACTIONS = {"作废快照", "作废对账快照", "作废", "标记作废"}
REPLACE_ACTIONS = {"替代确认", "确认替代", "确认替代快照", "确认替代关系", "指定替代快照"}
VERIFY_ACTIONS = {"核实快照", "补充核实", "核实", "标记已核实"}


def _load_json(value: Any) -> dict[str, Any]:
    """把快照 / 报告内容解析成字典，解析失败返回空字典。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
            return {"value": parsed}
        except (json.JSONDecodeError, TypeError):
            return {"raw": value}
    return {}


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _content_metrics(content: dict[str, Any]) -> list[dict[str, Any]]:
    """从报告 / 快照内容中提取受影响指标，兼容多种内容结构。"""
    metrics: list[dict[str, Any]] = []
    sections = content.get("affected_metrics") or content.get("sections")
    if isinstance(sections, list):
        for section in sections:
            if not isinstance(section, dict):
                continue
            metrics.append({
                "metric_name": section.get("metric_name") or section.get("指标名称") or "未命名指标",
                "value": section.get("current_value", section.get("value", section.get("快照值"))),
                "unit": section.get("unit", ""),
                "status": section.get("status", ""),
            })
    metric_name = content.get("metric_name") or content.get("指标名称")
    if metric_name and not metrics:
        metrics.append({
            "metric_name": metric_name,
            "value": content.get("current_value", content.get("value", content.get("快照值"))),
            "unit": content.get("unit", ""),
            "status": content.get("status", ""),
        })
    return metrics


def _format_value(value: Any) -> str:
    """把数值渲染成可读摘要：整数值不带小数点，例如 100 而不是 100.0。"""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _summary_values(content: dict[str, Any]) -> str:
    """给列表用的可读数值摘要，例如「付款额=100」。"""
    parts = []
    for metric in _content_metrics(content):
        value = metric.get("value")
        if value is None:
            continue
        parts.append(f"{metric['metric_name']}={_format_value(value)}")
    return "，".join(parts)


class ReportSnapshotService(BaseModuleService):
    MODULE = "report_snapshot"
    ENTITY = "快照记录"
    OPERATION_TERM = "存储"
    LIST_FIELDS = ["快照名称", "所属报告", "快照时间", "数据摘要", "差异对比"]
    REQUIRED_FIELDS = ["快照名称", "所属报告"]
    STATUS_ORDER = ["已保存", "已对比", "有差异", "已作废"]
    ACTION_RULES = {"生成快照": "已保存", "对比快照": "已对比"}
    NEGATIVE_ACTIONS = ["作废快照"]

    # ================================================================
    # 快照生成与对比（原有能力）
    # ================================================================

    def create_for_report(self, report_id: int, snapshot_name: str) -> dict[str, Any]:
        """为指定报告的最新输出创建快照。"""
        latest_output = db.fetchone(
            "SELECT * FROM report_outputs WHERE report_id=? ORDER BY generated_at DESC LIMIT 1",
            [report_id],
        )
        if not latest_output:
            return {"ok": False, "message": f"报告 {report_id} 没有已生成的输出"}

        report = db.fetchone("SELECT * FROM reports WHERE id=?", [report_id])
        content = latest_output["output_content"]
        snapshot_id = db.insert("snapshots", {
            "snapshot_name": snapshot_name,
            "report_id": report_id,
            "snapshot_content": content,
            "original_content": content,
            "status": "已保存",
            "verification_status": VERIFICATION_VERIFIED,
            "verification_note": "由报告输出直接生成，内容与报告版本一致",
            "verified_at": db.now(),
            "reconciliation_period": (report or {}).get("report_period"),
            "source_output_id": latest_output["id"],
        })
        db.update("report_outputs", {"snapshot_id": snapshot_id}, "id=?", [latest_output["id"]])
        self._log(snapshot_id=snapshot_id, report_id=report_id, output_id=latest_output["id"],
                  event_type="生成快照", reason=f"从报告输出 {latest_output['id']} 生成快照")
        return {
            "ok": True,
            "snapshot_id": snapshot_id,
            "snapshot_name": snapshot_name,
            "report_id": report_id,
        }

    def compare_snapshots(self, snapshot_id_a: int, snapshot_id_b: int) -> dict[str, Any]:
        """对比两个快照的差异。"""
        a = db.fetchone("SELECT * FROM snapshots WHERE id=?", [snapshot_id_a])
        b = db.fetchone("SELECT * FROM snapshots WHERE id=?", [snapshot_id_b])

        if not a or not b:
            return {"ok": False, "message": "快照不存在"}
        if a.get("status") == SNAPSHOT_VOIDED or b.get("status") == SNAPSHOT_VOIDED:
            return {"ok": False, "message": "已作废快照仅用于历史追溯，不参与数值对比"}

        content_a = _load_json(a["snapshot_content"])
        content_b = _load_json(b["snapshot_content"])

        diffs = []
        sections_a = content_a.get("sections", [])
        sections_b = content_b.get("sections", [])

        for sa, sb in zip(sections_a, sections_b):
            val_a = sa.get("current_value", 0)
            val_b = sb.get("current_value", 0)
            if val_a and val_b:
                try:
                    diff_pct = round(float(val_a - val_b) / max(abs(float(val_b)), 0.0001) * 100, 2)
                    diffs.append({
                        "metric": sa.get("metric_name"),
                        "value_a": val_a,
                        "value_b": val_b,
                        "diff_pct": diff_pct,
                        "has_diff": abs(diff_pct) > 1.0,
                    })
                except (TypeError, ValueError):
                    pass

        has_diff = any(d["has_diff"] for d in diffs)
        comparison_summary = {
            "snapshot_a": snapshot_id_a,
            "snapshot_b": snapshot_id_b,
            "total_metrics": len(diffs),
            "diffs_found": sum(1 for d in diffs if d["has_diff"]),
            "details": diffs,
        }

        db.update("snapshots", {
            "status": "已对比" if not has_diff else "有差异",
            "comparison_diff": _dump_json(comparison_summary),
        }, "id=?", [snapshot_id_a])

        return {"ok": True, "has_difference": has_diff, "summary": comparison_summary}

    def report_snapshots(self, report_id: int) -> list[dict[str, Any]]:
        """获取报告的所有快照。"""
        return db.fetchall(
            "SELECT * FROM snapshots WHERE report_id=? ORDER BY created_at DESC",
            [report_id],
        )

    # ================================================================
    # 列表 / 详情：在原始列之上补齐中文展示字段和资产生命周期
    # ================================================================

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows, total = super().list_entries(
            filters=filters, keyword=keyword, status=status, page=page, size=size)
        return [self._decorate_row(row) for row in rows], total

    def describe_entry(self, entry_id: int) -> dict[str, Any] | None:
        """通用资源详情：返回带生命周期、原凭据和关联投递的快照视图。"""
        return self.describe_snapshot(entry_id)

    def _decorate_row(self, row: dict[str, Any]) -> dict[str, Any]:
        content = _load_json(row.get("snapshot_content"))
        replacement = self._fetch(int(row["replaced_by"])) if row.get("replaced_by") else None
        predecessor = self._fetch(int(row["replaces_snapshot_id"])) if row.get("replaces_snapshot_id") else None
        report = self._fetch_report(row.get("report_id"))
        usable = (
            row.get("status") != SNAPSHOT_VOIDED
            and row.get("verification_status") == VERIFICATION_VERIFIED
        )
        return {
            **row,
            "快照名称": row.get("snapshot_name"),
            "所属报告": (report or {}).get("report_name") or row.get("report_id"),
            "快照时间": row.get("created_at"),
            "数据摘要": _summary_values(content) or row.get("snapshot_content"),
            "差异对比": row.get("comparison_diff"),
            "核实状态": row.get("verification_status") or VERIFICATION_PENDING,
            "核实说明": row.get("verification_note"),
            "作废原因": row.get("void_reason"),
            "作废时间": row.get("voided_at"),
            "替代快照": replacement.get("id") if replacement else None,
            "替代快照名称": replacement.get("snapshot_name") if replacement else None,
            "替代快照核实状态": replacement.get("verification_status") if replacement else None,
            "替代原因": row.get("replacement_reason"),
            "替代状态": row.get("replacement_status"),
            "原快照": predecessor.get("id") if predecessor else None,
            "可用于正式报送": usable,
            "metrics": _content_metrics(content),
            "value_summary": _summary_values(content),
        }

    @staticmethod
    def _fetch(entry_id: int) -> dict[str, Any] | None:
        """直接读快照原始行，避免详情装饰递归。"""
        return db.fetchone("SELECT * FROM snapshots WHERE id=?", [entry_id])

    @staticmethod
    def _fetch_report(report_id: Any) -> dict[str, Any] | None:
        if report_id in (None, ""):
            return None
        try:
            return db.fetchone("SELECT id, report_name FROM reports WHERE id=?", [int(report_id)])
        except (TypeError, ValueError):
            return None

    # ================================================================
    # 登记 / 动作入口：兼容通用资源路由 POST /api/report_snapshot/...
    # ================================================================

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """登记快照记录。数据摘要可传 JSON，也可传一段可读文本。

        通过通用登记接口建出的快照默认「待核实」，须先补充核实才能用于正式对账。
        """
        missing = [
            field for field in self.REQUIRED_FIELDS
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing

        raw_summary = values.get("数据摘要")
        if raw_summary is None or (isinstance(raw_summary, str) and not raw_summary.strip()):
            content: dict[str, Any] = {}
        elif isinstance(raw_summary, dict):
            content = raw_summary
        else:
            content = _load_json(raw_summary)

        report_id = values.get("所属报告")
        try:
            report_id_int = int(report_id) if report_id not in (None, "") else None
        except (TypeError, ValueError):
            report_id_int = None

        verification_status = str(
            values.get("核实状态") or values.get("verification_status") or VERIFICATION_PENDING
        ).strip()
        if verification_status not in (VERIFICATION_PENDING, VERIFICATION_VERIFIED):
            verification_status = VERIFICATION_PENDING

        data: dict[str, Any] = {
            "snapshot_name": str(values.get("快照名称") or "").strip(),
            "report_id": report_id_int,
            "snapshot_content": _dump_json(content),
            "original_content": _dump_json(content),
            "comparison_diff": values.get("差异对比"),
            "status": "已保存",
            "verification_status": verification_status,
            "verification_note": values.get("核实说明"),
            "verified_at": db.now() if verification_status == VERIFICATION_VERIFIED else None,
            "reconciliation_period": values.get("对账周期") or values.get("reconciliation_period"),
        }
        snapshot_time = values.get("快照时间")
        if snapshot_time:
            data["created_at"] = snapshot_time

        snapshot_id = db.insert("snapshots", data)
        if report_id_int:
            report = db.fetchone("SELECT * FROM reports WHERE id=?", [report_id_int])
            if report and not data["reconciliation_period"]:
                db.update("snapshots", {"reconciliation_period": report.get("report_period")},
                          "id=?", [snapshot_id])
        self._log(snapshot_id=snapshot_id, report_id=report_id_int, event_type="登记快照",
                  reason=f"核实状态：{verification_status}")
        return self.get_entry(snapshot_id), []

    def run_action(self, entry_id: int, action: str, remark: str | None = None):
        payload = self._parse_remark(remark)
        if action in VOID_ACTIONS:
            result = self.void_snapshot(
                entry_id,
                reason=str(payload.get("reason") or payload.get("原因") or remark or ""),
                operator=payload.get("operator") or payload.get("操作人"),
            )
            return result.get("snapshot"), result.get("message", "")
        if action in REPLACE_ACTIONS:
            result = self.confirm_replacement(
                entry_id,
                replacement_snapshot_id=payload.get("replacement_snapshot_id")
                or payload.get("替代快照ID") or payload.get("替代快照"),
                reason=str(payload.get("reason") or payload.get("原因") or ""),
                operator=payload.get("operator") or payload.get("操作人"),
            )
            return result.get("snapshot"), result.get("message", "")
        if action in VERIFY_ACTIONS:
            result = self.verify_snapshot(
                entry_id,
                note=str(payload.get("note") or payload.get("核实说明") or payload.get("reason") or ""),
                operator=payload.get("operator") or payload.get("操作人"),
                verified_value=payload.get("verified_value") or payload.get("核实值"),
                metric_name=payload.get("metric_name") or payload.get("指标名称"),
            )
            return result.get("snapshot"), result.get("message", "")
        return super().run_action(entry_id, action)

    @staticmethod
    def _parse_remark(remark: str | None) -> dict[str, Any]:
        """把动作备注解析成结构化参数：支持 JSON，也支持「替代快照ID=3;原因=...」。"""
        if not remark:
            return {}
        try:
            parsed = json.loads(remark)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
        payload: dict[str, Any] = {}
        for part in re.split(r"[;；,，]", remark):
            if "=" in part:
                key, value = part.split("=", 1)
            elif "：" in part:
                key, value = part.split("：", 1)
            else:
                continue
            key = key.strip()
            if key in ("原因", "作废原因", "reason"):
                payload["reason"] = value.strip()
            elif key in ("替代快照ID", "替代快照", "replacement_snapshot_id"):
                payload["replacement_snapshot_id"] = value.strip()
            elif key in ("核实值", "verified_value"):
                payload["verified_value"] = value.strip()
            elif key in ("核实说明", "note"):
                payload["note"] = value.strip()
            elif key in ("操作人", "operator"):
                payload["operator"] = value.strip()
            else:
                payload[key] = value.strip()
        if not payload:
            payload["reason"] = remark.strip()
        return payload

    # ================================================================
    # 作废
    # ================================================================

    def void_snapshot(
        self,
        snapshot_id: int,
        *,
        reason: str,
        operator: str | None = None,
    ) -> dict[str, Any]:
        """作废对账快照并保存失效原因。

        幂等：重复作废只返回第一次的结果，不改写原因 / 时间，也不影响已发送报告。
        """
        snapshot = self.get_entry(snapshot_id)
        if snapshot is None:
            return {"ok": False, "message": f"快照 {snapshot_id} 不存在"}

        if snapshot.get("status") == SNAPSHOT_VOIDED:
            return {
                "ok": True,
                "idempotent": True,
                "message": f"快照已作废（原因：{snapshot.get('void_reason') or '未记录'}），本次未重复处理",
                "snapshot": self.describe_snapshot(snapshot_id),
            }

        reason = (reason or "").strip()
        if not reason:
            return {"ok": False, "message": "作废对账快照必须填写作废原因"}
        if snapshot.get("replaced_by"):
            return {
                "ok": False,
                "message": "该快照已被确认替代，作废关系不可重复改写",
                "snapshot": self.describe_snapshot(snapshot_id),
            }

        now = db.now()
        db.update("snapshots", {
            "status": SNAPSHOT_VOIDED,
            "void_reason": reason,
            "voided_at": now,
            "voided_by": operator or "system",
            "original_content": snapshot.get("original_content") or snapshot.get("snapshot_content"),
        }, "id=?", [snapshot_id])
        self._log(snapshot_id=snapshot_id, report_id=snapshot.get("report_id"),
                  event_type="作废", reason=reason, operator=operator)
        return {
            "ok": True,
            "idempotent": False,
            "message": f"对账快照已作废：{reason}",
            "snapshot": self.describe_snapshot(snapshot_id),
        }

    # ================================================================
    # 替代确认
    # ================================================================

    def confirm_replacement(
        self,
        snapshot_id: int,
        *,
        replacement_snapshot_id: Any,
        reason: str,
        operator: str | None = None,
    ) -> dict[str, Any]:
        """确认已作废快照与替代快照的替代关系。

        替代确认本身不要求替代快照已核实，但未核实的替代快照不能用于正式报送。
        幂等：重复确认同一替代关系不重复写入；已确认的其他替代关系不可改写。
        """
        snapshot = self.get_entry(snapshot_id)
        if snapshot is None:
            return {"ok": False, "message": f"快照 {snapshot_id} 不存在"}

        try:
            replacement_id = int(str(replacement_snapshot_id).strip())
        except (TypeError, ValueError):
            return {"ok": False, "message": "请指定有效的替代快照ID"}

        if replacement_id == snapshot_id:
            return {"ok": False, "message": "替代快照不能是快照本身"}

        replacement = self.get_entry(replacement_id)
        if replacement is None:
            return {"ok": False, "message": f"替代快照 {replacement_id} 不存在"}
        if snapshot.get("report_id") and replacement.get("report_id") not in (None, snapshot.get("report_id")):
            return {"ok": False, "message": "替代快照必须属于同一报告"}
        if replacement.get("status") == SNAPSHOT_VOIDED:
            return {"ok": False, "message": "已作废快照不能作为替代快照"}
        if snapshot.get("status") != SNAPSHOT_VOIDED:
            return {"ok": False, "message": "只有已作废的快照可以确认替代关系"}

        confirmed = snapshot.get("replaced_by")
        if confirmed:
            if int(confirmed) == replacement_id:
                return {
                    "ok": True,
                    "idempotent": True,
                    "message": "替代关系已确认，本次未重复处理",
                    "snapshot": self.describe_snapshot(snapshot_id),
                }
            return {
                "ok": False,
                "message": f"已确认的替代关系不可改写（当前替代快照 {confirmed}）",
                "snapshot": self.describe_snapshot(snapshot_id),
            }

        reason = (reason or "").strip()
        if not reason:
            return {"ok": False, "message": "确认替代关系必须填写替代原因"}

        now = db.now()
        replacement_verified = replacement.get("verification_status") == VERIFICATION_VERIFIED
        db.update("snapshots", {
            "replaced_by": replacement_id,
            "replacement_reason": reason,
            "replacement_confirmed_at": now,
            "replacement_status": "已确认替代" if replacement_verified else "待核实替代",
        }, "id=?", [snapshot_id])
        # 反向记录，便于从替代快照追溯它替代了哪一份失效数据。
        db.update("snapshots", {
            "replaces_snapshot_id": snapshot_id,
        }, "id=? AND (replaces_snapshot_id IS NULL OR replaces_snapshot_id=?)",
            [replacement_id, snapshot_id])
        self._log(snapshot_id=snapshot_id, report_id=snapshot.get("report_id"),
                  event_type="替代确认",
                  reason=reason,
                  detail={"replacement_snapshot_id": replacement_id,
                          "replacement_verified": replacement_verified},
                  operator=operator)
        return {
            "ok": True,
            "idempotent": False,
            "message": (
                f"替代关系已确认：快照 {snapshot_id} → {replacement_id}"
                + ("" if replacement_verified else "；替代快照尚未核实，核实后才能用于正式报送")
            ),
            "snapshot": self.describe_snapshot(snapshot_id),
        }

    # ================================================================
    # 核实
    # ================================================================

    def verify_snapshot(
        self,
        snapshot_id: int,
        *,
        note: str,
        operator: str | None = None,
        verified_value: Any = None,
        metric_name: str | None = None,
    ) -> dict[str, Any]:
        """补充核实对账快照，并可写入更正后的数值。

        已核实快照重复核实时保持首次结论不变；已作废快照不能核实。
        """
        snapshot = self.get_entry(snapshot_id)
        if snapshot is None:
            return {"ok": False, "message": f"快照 {snapshot_id} 不存在"}
        if snapshot.get("status") == SNAPSHOT_VOIDED:
            return {"ok": False, "message": "已作废快照不能核实，也不能用于正式报送"}

        if snapshot.get("verification_status") == VERIFICATION_VERIFIED:
            return {
                "ok": True,
                "idempotent": True,
                "message": "快照已核实，本次未重复处理",
                "snapshot": self.describe_snapshot(snapshot_id),
            }

        note = (note or "").strip()
        if not note and verified_value is None:
            return {"ok": False, "message": "核实快照必须填写核实说明或核实值"}

        content = _load_json(snapshot.get("snapshot_content"))
        correction: dict[str, Any] | None = None
        if verified_value is not None:
            try:
                new_value = float(verified_value)
            except (TypeError, ValueError):
                return {"ok": False, "message": "核实值必须是数值"}
            sections = content.get("sections")
            if not isinstance(sections, list):
                content["current_value"] = new_value
                sections = []
            touched = False
            for section in sections:
                if not isinstance(section, dict):
                    continue
                if metric_name and section.get("metric_name") != metric_name:
                    continue
                old_value = section.get("current_value")
                section["current_value"] = new_value
                section["verified_value"] = new_value
                section["verification_status"] = VERIFICATION_VERIFIED
                correction = {"metric_name": section.get("metric_name"),
                              "old_value": old_value, "new_value": new_value}
                touched = True
                if metric_name:
                    break
            if not touched:
                content["current_value"] = new_value
                content["verified_value"] = new_value
                correction = {"metric_name": metric_name or content.get("metric_name"),
                              "old_value": None, "new_value": new_value}
            content["verified_at"] = db.now()

        now = db.now()
        updates: dict[str, Any] = {
            "verification_status": VERIFICATION_VERIFIED,
            "verification_note": note or (f"补充核实为 {verified_value}" if verified_value is not None else ""),
            "verified_at": now,
            "verified_by": operator or "system",
        }
        if verified_value is not None:
            updates["snapshot_content"] = _dump_json(content)
            if not snapshot.get("original_content"):
                updates["original_content"] = snapshot.get("snapshot_content")
        db.update("snapshots", updates, "id=?", [snapshot_id])

        # 如果该快照是某份作废快照的替代，则替代关系转为「已确认替代」。
        predecessor = snapshot.get("replaces_snapshot_id")
        if predecessor:
            db.update("snapshots", {"replacement_status": "已确认替代"},
                      "id=? AND replaced_by=?", [predecessor, snapshot_id])

        self._log(snapshot_id=snapshot_id, report_id=snapshot.get("report_id"),
                  event_type="核实", reason=note, detail={"correction": correction},
                  operator=operator)
        return {
            "ok": True,
            "idempotent": False,
            "message": "快照已核实" + (f"，核实值 {verified_value}" if verified_value is not None else ""),
            "snapshot": self.describe_snapshot(snapshot_id),
        }

    # ================================================================
    # 快照描述 / 校验
    # ================================================================

    def describe_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        """返回带生命周期、替代关系、关联投递和可用性判断的快照详情。"""
        row = self.get_entry(snapshot_id)
        if row is None:
            return None
        content = _load_json(row.get("snapshot_content"))
        original = _load_json(row.get("original_content")) if row.get("original_content") else None
        replacement = self.get_entry(int(row["replaced_by"])) if row.get("replaced_by") else None
        predecessor = self.get_entry(int(row["replaces_snapshot_id"])) if row.get("replaces_snapshot_id") else None

        deliveries = db.fetchall(
            "SELECT id, kind, is_formal_voucher, delivery_status, sent_at, snapshot_id, reference_reason "
            "FROM report_deliveries WHERE snapshot_id=? ORDER BY sent_at DESC",
            [snapshot_id],
        )
        outputs = db.fetchall(
            "SELECT id, report_id, delivery_status, snapshot_id FROM report_outputs WHERE snapshot_id=? "
            "ORDER BY generated_at DESC",
            [snapshot_id],
        )

        usable = (
            row.get("status") != SNAPSHOT_VOIDED
            and row.get("verification_status") == VERIFICATION_VERIFIED
        )
        blocking: list[str] = []
        if row.get("status") == SNAPSHOT_VOIDED:
            blocking.append(f"快照已作废：{row.get('void_reason') or '未记录原因'}")
        if row.get("verification_status") != VERIFICATION_VERIFIED:
            blocking.append("快照尚未核实")

        return {
            **row,
            # 中文展示字段：列表、详情和动作返回统一用同一套键，页面无需再做映射。
            "快照名称": row.get("snapshot_name"),
            "所属报告": row.get("report_id"),
            "快照时间": row.get("created_at"),
            "数据摘要": _summary_values(content) or row.get("snapshot_content"),
            "差异对比": row.get("comparison_diff"),
            "核实状态": row.get("verification_status") or VERIFICATION_PENDING,
            "核实说明": row.get("verification_note"),
            "作废原因": row.get("void_reason"),
            "作废时间": row.get("voided_at"),
            "替代原因": row.get("replacement_reason"),
            "替代状态": row.get("replacement_status"),
            "content": content,
            "original_credential": original,
            "metrics": _content_metrics(content),
            "value_summary": _summary_values(content),
            "usable_for_formal": usable,
            "blocking_reasons": blocking,
            "replacement_snapshot": {
                "id": replacement.get("id"),
                "snapshot_name": replacement.get("snapshot_name"),
                "status": replacement.get("status"),
                "verification_status": replacement.get("verification_status"),
                "value_summary": _summary_values(_load_json(replacement.get("snapshot_content"))),
                "verified": replacement.get("verification_status") == VERIFICATION_VERIFIED,
            } if replacement else None,
            "replaces_snapshot": {
                "id": predecessor.get("id"),
                "snapshot_name": predecessor.get("snapshot_name"),
                "void_reason": predecessor.get("void_reason"),
                "replacement_reason": predecessor.get("replacement_reason"),
            } if predecessor else None,
            "linked_deliveries": deliveries,
            "linked_outputs": outputs,
        }

    def validate_output(self, output_id: int) -> dict[str, Any]:
        """校验一个待发报告版本所引用的对账数据能否用于正式对账提交。"""
        output = db.fetchone("SELECT * FROM report_outputs WHERE id=?", [output_id])
        if output is None:
            return {"ok": False, "message": f"报告版本 {output_id} 不存在", "can_submit_formal": False}

        output_provenance = _load_json(output.get("provenance"))
        snapshot_id = output.get("snapshot_id")
        snapshot = self.get_entry(int(snapshot_id)) if snapshot_id else None
        reasons: list[str] = []

        if snapshot is None:
            reasons.append("报告版本未引用对账快照，无法确认对账数据出处")
        else:
            if snapshot.get("status") == SNAPSHOT_VOIDED:
                reasons.append(f"引用的对账快照已作废：{snapshot.get('void_reason') or '未记录原因'}")
            if snapshot.get("verification_status") != VERIFICATION_VERIFIED:
                reasons.append("引用的对账快照尚未核实")
            if snapshot.get("status") == SNAPSHOT_VOIDED and snapshot.get("replaced_by"):
                replacement = self.get_entry(int(snapshot["replaced_by"]))
                if replacement:
                    state = "已核实" if replacement.get("verification_status") == VERIFICATION_VERIFIED else "尚未核实"
                    reasons.append(
                        f"存在替代快照 {replacement['id']}（{state}），应基于替代快照重新生成正式报告版本"
                    )
            if output_provenance.get("derived_from_snapshot") and snapshot.get("snapshot_content"):
                if _load_json(output.get("output_content")) != _load_json(snapshot.get("snapshot_content")):
                    reasons.append("报告版本内容与引用的对账快照不一致")

        if output.get("delivery_status") == DELIVERY_SENT:
            reasons.append("该报告版本已发送，不能重复提交")

        return {
            "ok": True,
            "output_id": output_id,
            "report_id": output.get("report_id"),
            "delivery_status": output.get("delivery_status"),
            "snapshot_id": snapshot_id,
            "snapshot_status": (snapshot or {}).get("status"),
            "verification_status": (snapshot or {}).get("verification_status"),
            "void_reason": (snapshot or {}).get("void_reason"),
            "replacement_snapshot_id": (snapshot or {}).get("replaced_by"),
            "replacement_verification_status": (
                self.get_entry(int(snapshot["replaced_by"])).get("verification_status")
                if snapshot and snapshot.get("replaced_by") and self.get_entry(int(snapshot["replaced_by"]))
                else None
            ),
            "can_submit_formal": not reasons,
            "blocking_reasons": reasons,
            "provenance": {
                "snapshot_id": snapshot_id,
                "snapshot_name": (snapshot or {}).get("snapshot_name"),
                "verification_status": (snapshot or {}).get("verification_status"),
                "replaces_snapshot_id": (snapshot or {}).get("replaces_snapshot_id"),
                "value_summary": _summary_values(_load_json((snapshot or {}).get("snapshot_content"))),
                **output_provenance,
            },
        }

    def pending_versions(self, report_id: int) -> list[dict[str, Any]]:
        """列出报告下尚未发送的版本及其实时校验结果。"""
        outputs = db.fetchall(
            "SELECT * FROM report_outputs WHERE report_id=? AND COALESCE(delivery_status,'待发送') "
            "IN (?,?) ORDER BY generated_at DESC",
            [report_id, DELIVERY_PENDING, DELIVERY_PAUSED],
        )
        return [
            {**output, "content": _load_json(output.get("output_content")),
             "value_summary": _summary_values(_load_json(output.get("output_content"))),
             "validation": self.validate_output(output["id"])}
            for output in outputs
        ]

    # ================================================================
    # 报告版本 / 暂停
    # ================================================================

    def create_version_from_snapshot(
        self,
        report_id: int,
        snapshot_id: int,
        *,
        operator: str | None = None,
    ) -> dict[str, Any]:
        """基于已核实且未作废的对账快照生成新的报告版本。

        若报告存在已作废快照，则所选快照必须是被确认的替代快照，保证替代原因可追溯。
        """
        report = db.fetchone("SELECT * FROM reports WHERE id=?", [report_id])
        if report is None:
            return {"ok": False, "message": f"报告 {report_id} 不存在"}
        snapshot = self.get_entry(snapshot_id)
        if snapshot is None:
            return {"ok": False, "message": f"快照 {snapshot_id} 不存在"}
        if snapshot.get("report_id") not in (None, report_id):
            return {"ok": False, "message": "对账快照不属于该报告"}
        if snapshot.get("status") == SNAPSHOT_VOIDED:
            return {"ok": False, "message": "已作废快照不能为新正式报送提供数值"}
        if snapshot.get("verification_status") != VERIFICATION_VERIFIED:
            return {"ok": False, "message": "替代快照尚未核实，不能用于正式对账提交"}

        voided = db.fetchall(
            "SELECT * FROM snapshots WHERE report_id=? AND status=? ORDER BY id DESC",
            [report_id, SNAPSHOT_VOIDED],
        )
        if voided and not snapshot.get("replaces_snapshot_id"):
            return {
                "ok": False,
                "message": "该报告存在已作废快照，所选快照尚未被确认为替代快照，替代关系未确认",
            }
        if voided and snapshot.get("replaces_snapshot_id"):
            predecessor = self.get_entry(int(snapshot["replaces_snapshot_id"]))
            if predecessor and predecessor.get("replaced_by") != snapshot_id:
                return {"ok": False, "message": "替代关系尚未确认，不能生成正式报告版本"}

        content = _load_json(snapshot.get("snapshot_content"))
        provenance = {
            "source": "reconciliation_snapshot",
            "derived_from_snapshot": True,
            "snapshot_id": snapshot_id,
            "snapshot_name": snapshot.get("snapshot_name"),
            "verification_status": snapshot.get("verification_status"),
            "verified_at": snapshot.get("verified_at"),
            "replaces_snapshot_id": snapshot.get("replaces_snapshot_id"),
            "replacement_reason": None,
            "value_summary": _summary_values(content),
        }
        if snapshot.get("replaces_snapshot_id"):
            predecessor = self.get_entry(int(snapshot["replaces_snapshot_id"]))
            if predecessor:
                provenance["replacement_reason"] = predecessor.get("replacement_reason")
                provenance["void_reason"] = predecessor.get("void_reason")

        output_id = db.insert("report_outputs", {
            "report_id": report_id,
            "output_content": _dump_json(content),
            "snapshot_id": snapshot_id,
            "delivery_status": DELIVERY_PENDING,
            "provenance": _dump_json(provenance),
        })
        self._log(snapshot_id=snapshot_id, report_id=report_id, output_id=output_id,
                  event_type="生成报告版本", reason="基于核实后的对账快照生成",
                  detail=provenance, operator=operator)
        return {
            "ok": True,
            "message": f"已按对账快照 {snapshot_id} 生成待发报告版本",
            "output_id": output_id,
            "report_id": report_id,
            "snapshot_id": snapshot_id,
            "content": content,
            "provenance": provenance,
        }

    def pause_output(self, output_id: int, *, reason: str, operator: str | None = None) -> dict[str, Any]:
        """暂停待发报告，等待替代快照核实。已发送报告不可暂停。"""
        output = db.fetchone("SELECT * FROM report_outputs WHERE id=?", [output_id])
        if output is None:
            return {"ok": False, "message": f"报告版本 {output_id} 不存在"}
        status = output.get("delivery_status") or DELIVERY_PENDING
        if status == DELIVERY_SENT:
            return {"ok": False, "message": "已发送报告不可暂停，历史记录保持不变"}
        if status == DELIVERY_PAUSED:
            return {
                "ok": True,
                "idempotent": True,
                "message": f"报告版本已处于暂停状态（{output.get('paused_reason') or '未记录原因'}）",
                "output": self.output_view(output_id),
            }
        if not (reason or "").strip():
            return {"ok": False, "message": "暂停待发报告必须填写暂停原因"}
        db.update("report_outputs", {
            "delivery_status": DELIVERY_PAUSED,
            "paused_reason": reason.strip(),
            "paused_at": db.now(),
        }, "id=?", [output_id])
        self._log(snapshot_id=output.get("snapshot_id"), report_id=output.get("report_id"),
                  output_id=output_id, event_type="暂停待发", reason=reason, operator=operator)
        return {"ok": True, "message": "待发报告已暂停，等待替代快照核实",
                "output": self.output_view(output_id)}

    def resume_output(self, output_id: int, *, operator: str | None = None) -> dict[str, Any]:
        """恢复暂停的待发报告（不会自动发送）。"""
        output = db.fetchone("SELECT * FROM report_outputs WHERE id=?", [output_id])
        if output is None:
            return {"ok": False, "message": f"报告版本 {output_id} 不存在"}
        if output.get("delivery_status") == DELIVERY_SENT:
            return {"ok": False, "message": "已发送报告不可恢复为待发"}
        if output.get("delivery_status") != DELIVERY_PAUSED:
            return {"ok": True, "idempotent": True, "message": "报告版本未暂停",
                    "output": self.output_view(output_id)}
        db.update("report_outputs", {
            "delivery_status": DELIVERY_PENDING,
            "paused_reason": None,
            "paused_at": None,
        }, "id=?", [output_id])
        self._log(snapshot_id=output.get("snapshot_id"), report_id=output.get("report_id"),
                  output_id=output_id, event_type="恢复待发", reason="恢复待发", operator=operator)
        return {"ok": True, "message": "待发报告已恢复", "output": self.output_view(output_id)}

    # ================================================================
    # 正式报送 / 催核通知
    # ================================================================

    def submit_routine(
        self,
        report_id: int,
        *,
        output_id: int | None = None,
        operator: str | None = None,
        shift: str | None = None,
        recipients: Any = None,
    ) -> dict[str, Any]:
        """提交本班例行正式对账报送。

        正式报送必须基于未作废、已核实且替代关系明确的对账数据；已发送版本重复提交
        直接返回原投递记录，不新写、不改写。
        """
        report = db.fetchone("SELECT * FROM reports WHERE id=?", [report_id])
        if report is None:
            return {"ok": False, "message": f"报告 {report_id} 不存在"}

        if output_id is not None:
            output = db.fetchone("SELECT * FROM report_outputs WHERE id=?", [output_id])
        else:
            output = db.fetchone(
                "SELECT * FROM report_outputs WHERE report_id=? AND COALESCE(delivery_status,'待发送')=? "
                "ORDER BY generated_at DESC LIMIT 1",
                [report_id, DELIVERY_PENDING],
            )
        if output is None:
            return {"ok": False, "message": "没有可提交的待发报告版本，请先生成报告"}

        if output.get("delivery_status") == DELIVERY_PAUSED:
            return {
                "ok": False,
                "message": "该报告版本处于暂停状态，等待替代快照核实；请先恢复再提交",
                "validation": self.validate_output(output["id"]),
            }

        if output.get("delivery_status") == DELIVERY_SENT:
            existing = db.fetchone(
                "SELECT * FROM report_deliveries WHERE output_id=? ORDER BY id DESC LIMIT 1",
                [output["id"]],
            )
            return {
                "ok": True,
                "idempotent": True,
                "message": "该报告版本已发送，本次未重复提交，历史内容保持不变",
                "delivery": self.describe_delivery(existing["id"]) if existing else None,
                "validation": self.validate_output(output["id"]),
            }

        validation = self.validate_output(output["id"])
        if not validation.get("can_submit_formal"):
            self._log(snapshot_id=output.get("snapshot_id"), report_id=report_id,
                      output_id=output["id"], event_type="正式报送拦截",
                      reason="；".join(validation.get("blocking_reasons") or []), operator=operator)
            return {
                "ok": False,
                "message": "无法作为正式对账提交：" + "；".join(validation.get("blocking_reasons") or []),
                "validation": validation,
            }

        snapshot_id = output.get("snapshot_id")
        snapshot = self.get_entry(int(snapshot_id)) if snapshot_id else None
        content = _load_json(output.get("output_content"))
        original_credential = _load_json(snapshot.get("original_content") or snapshot.get("snapshot_content"))
        provenance = _load_json(output.get("provenance"))
        provenance = {
            **provenance,
            "snapshot_id": snapshot_id,
            "snapshot_name": (snapshot or {}).get("snapshot_name"),
            "verification_status": (snapshot or {}).get("verification_status"),
            "replaces_snapshot_id": (snapshot or {}).get("replaces_snapshot_id"),
            "delivered_value_summary": _summary_values(content),
        }

        now = db.now()
        delivery_id = db.insert("report_deliveries", {
            "report_id": report_id,
            "output_id": output["id"],
            "snapshot_id": snapshot_id,
            "kind": FORMAL_DELIVERY_KIND,
            "is_formal_voucher": 1,
            "content": _dump_json(content),
            "content_original": _dump_json(original_credential),
            "reference_reason": provenance.get("replacement_reason"),
            "recipients": _dump_json(recipients) if not isinstance(recipients, str) else recipients,
            "delivery_status": DELIVERY_SENT,
            "shift": shift or "本班",
            "operator": operator or "财务主管",
            "sent_at": now,
        })
        # 报告版本只在这里从未发送变为已发送，且只写一次；之后再提交直接返回原投递。
        db.update("report_outputs", {
            "delivery_status": DELIVERY_SENT,
            "sent_delivery_id": delivery_id,
            "sent_at": now,
            "provenance": _dump_json(provenance),
        }, "id=? AND COALESCE(delivery_status,'待发送')<>?", [output["id"], DELIVERY_SENT])
        self._log(snapshot_id=snapshot_id, report_id=report_id, output_id=output["id"],
                  delivery_id=delivery_id, event_type="正式报送",
                  reason=provenance.get("replacement_reason") or "例行正式对账报送",
                  detail=provenance, operator=operator)
        return {
            "ok": True,
            "idempotent": False,
            "message": "本班例行报送已完成，正式对账报告已发送",
            "delivery": self.describe_delivery(delivery_id),
            "validation": validation,
        }

    def send_notice(
        self,
        report_id: int,
        *,
        snapshot_id: int,
        recipients: Any = None,
        note: str | None = None,
        operator: str | None = None,
        output_id: int | None = None,
    ) -> dict[str, Any]:
        """发送带作废原因的催核通知。

        通知列出受影响指标供收件方补齐明细，但明确标记为非正式付款凭证，
        不能替代正式对账报告，也不改变待发报告和历史记录。
        """
        report = db.fetchone("SELECT * FROM reports WHERE id=?", [report_id])
        if report is None:
            return {"ok": False, "message": f"报告 {report_id} 不存在"}
        snapshot = self.get_entry(snapshot_id)
        if snapshot is None:
            return {"ok": False, "message": f"快照 {snapshot_id} 不存在"}
        if snapshot.get("report_id") not in (None, report_id):
            return {"ok": False, "message": "快照不属于该报告"}

        content = _load_json(snapshot.get("snapshot_content"))
        affected = [
            {**metric, "value_usable_for_formal": False}
            for metric in _content_metrics(content)
        ]
        reason = snapshot.get("void_reason") or note or "对账数据需要补充核实"
        notice_content = {
            "notice_type": NOTICE_DELIVERY_KIND,
            "is_formal_voucher": False,
            "report_name": report.get("report_name"),
            "title": f"{report.get('report_name')} 对账数据催核通知",
            "linked_snapshot_id": snapshot_id,
            "linked_snapshot_name": snapshot.get("snapshot_name"),
            "void_reason": reason,
            "affected_metrics": affected,
            "instruction": "请收件方按受影响指标补充明细并完成核实；本通知不能作为正式付款凭证。",
            "note": note,
            "generated_at": db.now(),
        }
        delivery_id = db.insert("report_deliveries", {
            "report_id": report_id,
            "output_id": output_id,
            "snapshot_id": snapshot_id,
            "kind": NOTICE_DELIVERY_KIND,
            "is_formal_voucher": 0,
            "content": _dump_json(notice_content),
            "content_original": snapshot.get("original_content") or snapshot.get("snapshot_content"),
            "void_snapshot_id": snapshot_id if snapshot.get("status") == SNAPSHOT_VOIDED else None,
            "reference_reason": reason,
            "recipients": _dump_json(recipients) if not isinstance(recipients, str) else recipients,
            "delivery_status": DELIVERY_SENT,
            "shift": "本班",
            "operator": operator or "财务主管",
            "sent_at": db.now(),
        })
        self._log(snapshot_id=snapshot_id, report_id=report_id, output_id=output_id,
                  delivery_id=delivery_id, event_type="催核通知", reason=reason, operator=operator)
        return {
            "ok": True,
            "message": "催核通知已发送；通知本身不是正式付款凭证",
            "delivery": self.describe_delivery(delivery_id),
        }

    # ================================================================
    # 投递追溯
    # ================================================================

    def describe_delivery(self, delivery_id: int) -> dict[str, Any] | None:
        """返回不可变投递副本、关联快照和原凭据。"""
        row = db.fetchone("SELECT * FROM report_deliveries WHERE id=?", [delivery_id])
        if row is None:
            return None
        snapshot = self.get_entry(int(row["snapshot_id"])) if row.get("snapshot_id") else None
        content = _load_json(row.get("content"))
        original = _load_json(row.get("content_original")) if row.get("content_original") else None
        return {
            **row,
            "content": content,
            "original_credential": original,
            "metrics": _content_metrics(content),
            "value_summary": _summary_values(content),
            "is_formal_voucher": bool(row.get("is_formal_voucher")),
            "linked_snapshot": self.describe_snapshot(int(row["snapshot_id"])) if snapshot else None,
        }

    def report_deliveries(self, report_id: int) -> list[dict[str, Any]]:
        rows = db.fetchall(
            "SELECT * FROM report_deliveries WHERE report_id=? ORDER BY sent_at DESC, id DESC",
            [report_id],
        )
        return [
            {**row, "content": _load_json(row.get("content")),
             "value_summary": _summary_values(_load_json(row.get("content"))),
             "is_formal_voucher": bool(row.get("is_formal_voucher")),
             "linked_snapshot": self._linked_snapshot_summary(row.get("snapshot_id"))}
            for row in rows
        ]

    def _linked_snapshot_summary(self, snapshot_id: Any) -> dict[str, Any] | None:
        """投递关联快照的轻量摘要，避免与快照详情的投递列表互相递归。"""
        if snapshot_id in (None, ""):
            return None
        row = self._fetch(int(snapshot_id))
        if row is None:
            return None
        return {
            "id": row.get("id"),
            "snapshot_name": row.get("snapshot_name"),
            "status": row.get("status"),
            "verification_status": row.get("verification_status"),
            "void_reason": row.get("void_reason"),
            "replaced_by": row.get("replaced_by"),
            "value_summary": _summary_values(_load_json(row.get("snapshot_content"))),
        }

    def reconciliation_overview(self) -> dict[str, Any]:
        """对账控制台数据：快照生命周期、待发版本校验和投递历史。"""
        snapshots = db.fetchall("SELECT id FROM snapshots ORDER BY id DESC")
        described = [self.describe_snapshot(row["id"]) for row in snapshots]
        reports = db.fetchall(
            "SELECT id, report_name, report_period, status FROM reports ORDER BY id DESC"
        )
        pending = []
        for report in reports:
            for version in self.pending_versions(report["id"]):
                pending.append({**version, "report_name": report.get("report_name"),
                                "report_period": report.get("report_period")})
        deliveries = []
        for row in db.fetchall("SELECT id FROM report_deliveries ORDER BY sent_at DESC, id DESC"):
            deliveries.append(self.describe_delivery(row["id"]))
        return {
            "snapshots": [item for item in described if item],
            "pending_versions": pending,
            "deliveries": deliveries,
            "reports": reports,
            "events": db.fetchall(
                "SELECT * FROM reconciliation_events ORDER BY id DESC LIMIT 100"
            ),
        }

    # ================================================================
    # 内部工具
    # ================================================================

    def output_view(self, output_id: int) -> dict[str, Any] | None:
        output = db.fetchone("SELECT * FROM report_outputs WHERE id=?", [output_id])
        if output is None:
            return None
        return {
            **output,
            "content": _load_json(output.get("output_content")),
            "value_summary": _summary_values(_load_json(output.get("output_content"))),
            "validation": self.validate_output(output_id),
        }

    @staticmethod
    def _log(
        *,
        snapshot_id: int | None = None,
        report_id: int | None = None,
        output_id: int | None = None,
        delivery_id: int | None = None,
        event_type: str,
        reason: str | None = None,
        detail: Any = None,
        operator: str | None = None,
    ) -> None:
        db.insert("reconciliation_events", {
            "snapshot_id": snapshot_id,
            "report_id": report_id,
            "output_id": output_id,
            "delivery_id": delivery_id,
            "event_type": event_type,
            "reason": reason,
            "detail": _dump_json(detail) if detail is not None and not isinstance(detail, str) else detail,
            "operator": operator or "system",
        })

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["total_snapshots"] = db.count("snapshots")
        snapshot["voided_snapshots"] = db.count("snapshots", "status=?", [SNAPSHOT_VOIDED])
        snapshot["pending_verification"] = db.count(
            "snapshots", "verification_status=? AND status<>?",
            [VERIFICATION_PENDING, SNAPSHOT_VOIDED],
        )
        snapshot["formal_deliveries"] = db.count("report_deliveries", "is_formal_voucher=1")
        snapshot["notice_deliveries"] = db.count("report_deliveries", "is_formal_voucher=0")
        return snapshot
