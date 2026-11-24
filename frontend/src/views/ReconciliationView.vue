<template>
  <section class="page" data-testid="reconciliation-console">
    <header class="page-head">
      <div>
        <h2>对账追溯</h2>
        <p class="page-desc">
          对账快照作废、替代确认、待发版本校验与本班正式报送。已发送内容始终按原凭据保留，后续作废或替代不改写历史。
        </p>
      </div>
      <div class="page-actions">
        <label class="filter-item">
          <span>报告</span>
          <select v-model.number="selectedReportId" data-testid="report-select">
            <option v-for="report in overview.reports" :key="report.id" :value="report.id">
              {{ report.report_name }}（{{ report.report_period }}）
            </option>
          </select>
        </label>
        <button class="btn" data-testid="refresh" type="button" @click="reload">刷新</button>
      </div>
    </header>

    <p v-if="message" class="notice-line" data-testid="message">{{ message }}</p>
    <p v-if="errorMessage" class="error-text" data-testid="error">{{ errorMessage }}</p>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">对账快照</span>
        <strong class="stat-value">{{ overview.snapshots.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已作废快照</span>
        <strong class="stat-value">{{ voidedCount }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待核实快照</span>
        <strong class="stat-value">{{ pendingVerifyCount }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待发版本</span>
        <strong class="stat-value">{{ reportPending.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">正式投递</span>
        <strong class="stat-value">{{ formalCount }}</strong>
      </article>
    </div>

    <!-- 快照 -->
    <h3 class="section-title">对账快照</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>快照名称</th><th>数值摘要</th><th>状态</th><th>核实状态</th>
          <th>作废原因</th><th>替代关系</th><th>关联已发送</th><th>可正式使用</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="snapshot in reportSnapshots" :key="snapshot.id" :data-testid="`snapshot-${snapshot.id}`"
            :data-status="snapshot.status">
          <td>{{ snapshot.snapshot_name }}</td>
          <td data-testid="snapshot-value">{{ snapshot.value_summary || '—' }}</td>
          <td>{{ snapshot.status }}</td>
          <td>{{ snapshot.verification_status }}</td>
          <td>{{ snapshot.void_reason || '—' }}</td>
          <td>
            <span v-if="snapshot.replacement_snapshot">
              → #{{ snapshot.replacement_snapshot.id }} {{ snapshot.replacement_snapshot.snapshot_name }}
              （{{ snapshot.replacement_snapshot.verification_status }}）
              <em v-if="snapshot.replacement_reason">：{{ snapshot.replacement_reason }}</em>
            </span>
            <span v-else-if="snapshot.replaces_snapshot">
              ← 替代 #{{ snapshot.replaces_snapshot.id }}
              <em v-if="snapshot.replaces_snapshot.replacement_reason">：{{ snapshot.replaces_snapshot.replacement_reason }}</em>
            </span>
            <span v-else>—</span>
          </td>
          <td>
            <span v-if="snapshot.linked_deliveries.length">
              {{ snapshot.linked_deliveries.length }} 份（最早 {{ snapshot.linked_deliveries[snapshot.linked_deliveries.length - 1]?.sent_at }}）
            </span>
            <span v-else>—</span>
          </td>
          <td :data-testid="`usable-${snapshot.id}`">{{ snapshot.usable_for_formal ? '是' : '否' }}</td>
          <td class="row-actions">
            <button class="link" type="button" :data-testid="`void-${snapshot.id}`"
                    @click="openVoid(snapshot)">作废</button>
            <button class="link" type="button" :data-testid="`replace-${snapshot.id}`"
                    @click="openReplace(snapshot)">替代确认</button>
            <button class="link" type="button" :data-testid="`verify-${snapshot.id}`"
                    @click="openVerify(snapshot)">核实</button>
            <button class="link" type="button" :data-testid="`new-version-${snapshot.id}`"
                    :disabled="!snapshot.usable_for_formal"
                    @click="createFromSnapshot(snapshot)">生成待发版本</button>
          </td>
        </tr>
        <tr v-if="!reportSnapshots.length">
          <td colspan="9" class="empty-state">当前报告暂无对账快照</td>
        </tr>
      </tbody>
    </table>

    <!-- 待发版本 -->
    <h3 class="section-title">待发报告版本</h3>
    <table class="data-table">
      <thead>
        <tr><th>版本</th><th>数值摘要</th><th>引用快照</th><th>发送状态</th><th>能否正式提交</th><th>操作</th></tr>
      </thead>
      <tbody>
        <tr v-for="version in reportPending" :key="version.id" :data-testid="`version-${version.id}`">
          <td>#{{ version.id }}</td>
          <td>{{ version.value_summary || '—' }}</td>
          <td>#{{ version.snapshot_id ?? '—' }}</td>
          <td>{{ version.delivery_status }}<span v-if="version.paused_reason">（{{ version.paused_reason }}）</span></td>
          <td :data-testid="`submittable-${version.id}`">
            <span v-if="version.validation.can_submit_formal" class="ok-text">可作为正式对账</span>
            <span v-else class="error-text">{{ version.validation.blocking_reasons.join('；') }}</span>
          </td>
          <td class="row-actions">
            <button class="link" type="button" :data-testid="`pause-${version.id}`"
                    @click="pause(version)">暂停待发</button>
            <button class="link" type="button" :data-testid="`submit-${version.id}`"
                    @click="submit(version)">本班正式报送</button>
            <button class="link" type="button" :data-testid="`notice-${version.id}`"
                    @click="noticeForVersion(version)">发催核通知</button>
          </td>
        </tr>
        <tr v-if="!reportPending.length">
          <td colspan="6" class="empty-state">暂无待发版本</td>
        </tr>
      </tbody>
    </table>

    <!-- 投递历史 -->
    <h3 class="section-title">投递历史（按原凭据保留）</h3>
    <table class="data-table">
      <thead>
        <tr><th>投递</th><th>类型</th><th>是否正式凭证</th><th>数值摘要</th><th>关联快照</th><th>原因</th><th>发送时间</th></tr>
      </thead>
      <tbody>
        <tr v-for="delivery in reportDeliveries" :key="delivery.id" :data-testid="`delivery-${delivery.id}`">
          <td>#{{ delivery.id }}</td>
          <td>{{ delivery.kind }}</td>
          <td>{{ delivery.is_formal_voucher ? '正式付款凭证' : '非正式（催核通知）' }}</td>
          <td data-testid="delivery-value">{{ delivery.value_summary }}</td>
          <td>#{{ delivery.linked_snapshot?.id ?? '—' }}</td>
          <td>{{ delivery.reference_reason || '—' }}</td>
          <td>{{ delivery.sent_at }}</td>
        </tr>
        <tr v-if="!reportDeliveries.length">
          <td colspan="7" class="empty-state">暂无投递记录</td>
        </tr>
      </tbody>
    </table>

    <div v-if="dialog" class="modal-backdrop" data-testid="action-modal">
      <form class="modal" @submit.prevent="confirmDialog">
        <h3>{{ dialog.title }}</h3>
        <p class="page-desc">{{ dialog.desc }}</p>
        <label v-for="field in dialog.fields" :key="field.key" class="create-field">
          <span>{{ field.label }}</span>
          <input v-model="dialog.values[field.key]" :data-testid="`dialog-${field.key}`" :required="field.required" />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="dialog = null">取消</button>
          <button class="btn primary" data-testid="dialog-confirm" type="submit">确认</button>
        </div>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  confirmReplacement,
  createVersion,
  loadReconciliation,
  pauseVersion,
  sendNotice,
  submitRoutine,
  verifySnapshot,
  voidSnapshot,
} from '@/api/reconciliation'
import type { DeliveryView, ReconciliationOverview, SnapshotView, VersionView } from '@/api/reconciliation'

type DialogField = { key: string; label: string; required?: boolean }
type Dialog = {
  title: string
  desc: string
  fields: DialogField[]
  values: Record<string, string>
  run: (values: Record<string, string>) => Promise<string>
}

const overview = ref<ReconciliationOverview>({
  snapshots: [], pending_versions: [], deliveries: [], reports: [], events: [],
})
const selectedReportId = ref<number | null>(null)
const message = ref('')
const errorMessage = ref('')
const dialog = ref<Dialog | null>(null)

const reportSnapshots = computed(() =>
  overview.value.snapshots.filter((s) => s.report_id === selectedReportId.value))
const reportPending = computed(() =>
  overview.value.pending_versions.filter((v) => v.report_id === selectedReportId.value))
const reportDeliveries = computed(() =>
  overview.value.deliveries.filter((d) => d.report_id === selectedReportId.value))
const voidedCount = computed(() => reportSnapshots.value.filter((s) => s.status === '已作废').length)
const pendingVerifyCount = computed(() =>
  reportSnapshots.value.filter((s) => s.verification_status === '待核实' && s.status !== '已作废').length)
const formalCount = computed(() => reportDeliveries.value.filter((d) => d.is_formal_voucher).length)

function flash(text: string) {
  message.value = text
  errorMessage.value = ''
}

async function reload() {
  errorMessage.value = ''
  try {
    overview.value = await loadReconciliation()
    if (!selectedReportId.value && overview.value.reports.length) {
      selectedReportId.value = overview.value.reports[0].id
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '对账数据读取失败'
  }
}

function openVoid(snapshot: SnapshotView) {
  dialog.value = {
    title: `作废快照 #${snapshot.id}`,
    desc: '作废后该快照不能再为新正式报送提供数值，已发送内容仍按原凭据保留。',
    fields: [{ key: 'reason', label: '作废原因', required: true }],
    values: { reason: '' },
    run: async (values) => (await voidSnapshot(snapshot.id, values.reason, '财务主管')).message,
  }
}

function openReplace(snapshot: SnapshotView) {
  const candidates = reportSnapshots.value.filter(
    (s) => s.id !== snapshot.id && s.status !== '已作废')
  dialog.value = {
    title: `确认替代快照 #${snapshot.id}`,
    desc: `可选的替代快照：${candidates.map((s) => `#${s.id} ${s.snapshot_name}（${s.verification_status}）`).join('；') || '无'}`,
    fields: [
      { key: 'replacement', label: '替代快照ID', required: true },
      { key: 'reason', label: '替代原因', required: true },
    ],
    values: { replacement: String(candidates[0]?.id ?? ''), reason: '' },
    run: async (values) => (await confirmReplacement(
      snapshot.id, Number(values.replacement), values.reason, '财务主管')).message,
  }
}

function openVerify(snapshot: SnapshotView) {
  dialog.value = {
    title: `核实快照 #${snapshot.id}`,
    desc: '补充核实后可写入更正数值；未核实快照不能用于正式对账。',
    fields: [
      { key: 'value', label: '核实值（留空表示只确认已核实）' },
      { key: 'note', label: '核实说明', required: true },
    ],
    values: { value: '', note: '' },
    run: async (values) => (await verifySnapshot(snapshot.id, {
      note: values.note,
      verified_value: values.value ? Number(values.value) : null,
      metric_name: snapshot.metrics[0]?.metric_name,
      operator: '财务主管',
    })).message,
  }
}

async function confirmDialog() {
  if (!dialog.value) return
  const current = dialog.value
  try {
    flash(await current.run(current.values))
    dialog.value = null
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function pause(version: VersionView) {
  try {
    flash((await pauseVersion(version.id, '等待替代快照核实', '财务主管')).message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '暂停失败'
  }
}

async function submit(version: VersionView) {
  try {
    const result = await submitRoutine(version.report_id, version.id, '财务主管')
    if (!result.ok) throw new Error(result.message)
    flash(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '报送失败'
  }
}

async function noticeForVersion(version: VersionView) {
  const snapshot = reportSnapshots.value.find((s) => s.status === '已作废')
  if (!snapshot) {
    errorMessage.value = '当前报告没有已作废快照，无需催核'
    return
  }
  try {
    const result = await sendNotice(version.report_id, {
      snapshot_id: snapshot.id,
      recipients: '收件方财务',
      note: '请按受影响指标补齐明细',
      output_id: version.id,
      operator: '财务主管',
    })
    flash(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '催核通知发送失败'
  }
}

async function createFromSnapshot(snapshot: SnapshotView) {
  if (!snapshot.report_id) {
    errorMessage.value = '快照未关联报告'
    return
  }
  try {
    flash((await createVersion(snapshot.report_id, snapshot.id, '财务主管')).message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '生成版本失败'
  }
}

onMounted(reload)
</script>
