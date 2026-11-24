/** 对账追溯接口：快照作废 / 替代确认 / 核实 / 待发版本 / 正式报送 / 催核通知。 */
import { fetchJson, request } from './client'

export type SnapshotMetric = {
  metric_name: string
  value: number | string | null
  unit?: string
  status?: string
}

export type SnapshotView = {
  id: number
  snapshot_name: string
  report_id: number | null
  所属报告?: string | number | null
  status: string
  verification_status: string
  verification_note?: string | null
  void_reason?: string | null
  voided_at?: string | null
  voided_by?: string | null
  replacement_reason?: string | null
  replacement_status?: string | null
  replaced_by?: number | null
  replaces_snapshot_id?: number | null
  created_at: string
  快照时间?: string
  数据摘要?: string
  value_summary: string
  metrics: SnapshotMetric[]
  usable_for_formal: boolean
  blocking_reasons: string[]
  original_credential: { sections?: SnapshotMetric[] } | null
  replacement_snapshot: {
    id: number
    snapshot_name: string
    status: string
    verification_status: string
    value_summary: string
    verified: boolean
  } | null
  replaces_snapshot: {
    id: number
    snapshot_name: string
    void_reason?: string | null
    replacement_reason?: string | null
  } | null
  linked_deliveries: { id: number; kind: string; sent_at: string; delivery_status: string }[]
}

export type ValidationView = {
  can_submit_formal: boolean
  blocking_reasons: string[]
  snapshot_id: number | null
  snapshot_status: string | null
  verification_status: string | null
  void_reason?: string | null
  replacement_snapshot_id?: number | null
  replacement_verification_status?: string | null
  provenance: Record<string, unknown>
}

export type VersionView = {
  id: number
  report_id: number
  report_name?: string
  report_period?: string
  delivery_status: string
  paused_reason?: string | null
  value_summary: string
  snapshot_id: number | null
  content: { sections?: SnapshotMetric[] }
  validation: ValidationView
}

export type LinkedSnapshotSummary = {
  id: number | null
  snapshot_name?: string | null
  status?: string | null
  verification_status?: string | null
  void_reason?: string | null
  replaced_by?: number | null
  value_summary?: string
}

export type DeliveryView = {
  id: number
  report_id: number
  kind: string
  is_formal_voucher: boolean
  delivery_status: string
  sent_at: string
  operator?: string | null
  shift?: string | null
  reference_reason?: string | null
  value_summary: string
  metrics: SnapshotMetric[]
  content: Record<string, any>
  original_credential: Record<string, any> | null
  linked_snapshot: (LinkedSnapshotSummary & Partial<SnapshotView>) | null
}

export type ReconciliationOverview = {
  snapshots: SnapshotView[]
  pending_versions: VersionView[]
  deliveries: DeliveryView[]
  reports: { id: number; report_name: string; report_period: string; status: string }[]
  events: {
    id: number
    snapshot_id: number | null
    report_id: number | null
    event_type: string
    reason?: string | null
    operator?: string | null
    created_at: string
  }[]
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  return (await response.json()) as T
}

export function loadReconciliation(): Promise<ReconciliationOverview> {
  return fetchJson<ReconciliationOverview>('/api/reconciliation/overview')
}

export function voidSnapshot(snapshotId: number, reason: string, operator?: string) {
  return postJson<{ ok: boolean; message: string; snapshot: SnapshotView }>(
    `/api/reconciliation/snapshots/${snapshotId}/void`, { reason, operator })
}

export function confirmReplacement(
  snapshotId: number, replacementSnapshotId: number, reason: string, operator?: string,
) {
  return postJson<{ ok: boolean; message: string; snapshot: SnapshotView }>(
    `/api/reconciliation/snapshots/${snapshotId}/replacement`,
    { replacement_snapshot_id: replacementSnapshotId, reason, operator })
}

export function verifySnapshot(
  snapshotId: number,
  payload: { note?: string; verified_value?: number | null; metric_name?: string; operator?: string },
) {
  return postJson<{ ok: boolean; message: string; snapshot: SnapshotView }>(
    `/api/reconciliation/snapshots/${snapshotId}/verify`, payload)
}

export function createVersion(reportId: number, snapshotId: number, operator?: string) {
  return postJson<{ ok: boolean; message: string; output_id: number; value_summary?: string }>(
    `/api/reconciliation/reports/${reportId}/versions`, { snapshot_id: snapshotId, operator })
}

export function pauseVersion(outputId: number, reason: string, operator?: string) {
  return postJson<{ ok: boolean; message: string }>(
    `/api/reconciliation/outputs/${outputId}/pause`, { reason, operator })
}

export function resumeVersion(outputId: number) {
  return postJson<{ ok: boolean; message: string }>(
    `/api/reconciliation/outputs/${outputId}/resume`, {})
}

export function submitRoutine(reportId: number, outputId: number, operator?: string) {
  return postJson<{ ok: boolean; message: string; delivery?: DeliveryView; idempotent?: boolean }>(
    `/api/reconciliation/reports/${reportId}/submit-routine`,
    { output_id: outputId, operator, shift: '本班' })
}

export function sendNotice(
  reportId: number,
  payload: { snapshot_id: number; recipients?: string; note?: string; output_id?: number; operator?: string },
) {
  return postJson<{ ok: boolean; message: string; delivery: DeliveryView }>(
    `/api/reconciliation/reports/${reportId}/notice`, payload)
}
