<template>
  <section
    class="page delivery-detail"
    data-testid="delivery-detail"
    :data-deliver-no="deliverNo"
  >
    <header class="page-head">
      <div>
        <p class="back-link"><RouterLink to="/result_deliver">← 返回结果投递台账</RouterLink></p>
        <h2>结果投递详情 · 每次尝试的独立回执核对</h2>
        <p class="page-desc">
          已目标确认的旧件只接受核对：迟到的失败回执按其自身尝试留存，不回写原行数、目标与确认时间；
          补交接收方行数证明后可恢复一致的原确认。
        </p>
      </div>
    </header>

    <p v-if="loading && !detail" data-testid="detail-loading">详情加载中…</p>
    <p v-else-if="loadError && !detail" class="error-text" data-testid="detail-load-error">{{ loadError }}</p>

    <template v-else-if="detail">
      <div
        v-if="detail.是否矛盾"
        class="banner danger"
        data-testid="contradiction-banner"
      >
        ⚠ {{ detail.矛盾说明 || '台账同时存在“已确认”与“缺行”的矛盾结论，不能据此实施目标替换，请先核对凭据或补交证明。' }}
      </div>
      <div v-else-if="scopeVerified" class="banner ok" data-testid="verified-banner">
        ✓ 确认范围已经目标凭据核对：{{ scopeText }}
      </div>

      <div class="detail-grid">
        <article class="info-card">
          <h3>台账与原确认</h3>
          <dl class="info-list">
            <div><dt>投递记录编号</dt><dd data-testid="detail-no">{{ text(detail.投递记录编号) }}</dd></div>
            <div><dt>当前状态</dt><dd data-testid="detail-status" :data-status="text(detail.status)">{{ text(detail.status) }}</dd></div>
            <div><dt>目标位置</dt><dd data-testid="detail-target">{{ text(detail.目标位置) }}</dd></div>
            <div><dt>投递行数</dt><dd data-testid="detail-total-rows">{{ text(detail.投递行数) }}</dd></div>
            <div><dt>已确认行数</dt><dd data-testid="detail-confirmed-rows">{{ text(detail.已确认行数 || detail.投递行数) }}</dd></div>
            <div><dt>缺失行数</dt><dd data-testid="detail-missing-rows" :data-missing="text(detail.缺失行数)">{{ text(detail.缺失行数 || '0') }}</dd></div>
            <div><dt>目标确认时间</dt><dd data-testid="detail-confirm-time">{{ text(detail.目标确认时间 || detail.投递时间) }}</dd></div>
            <div><dt>目标确认凭据</dt><dd data-testid="detail-credential">{{ text(detail.目标确认凭据) || '尚未登记/核对' }}</dd></div>
            <div><dt>投递结论</dt><dd data-testid="detail-conclusion">{{ text(detail.投递结论) }}</dd></div>
          </dl>
        </article>

        <article class="info-card">
          <h3>补交接收方行数证明（恢复原确认）</h3>
          <p class="hint">例如值班员补交接收方“实际收到 3 行”的三行证明与凭据；原确认时间不会被重写。</p>
          <label class="form-line">
            <span>证明行数</span>
            <input
              v-model="proofRows"
              data-testid="proof-rows-input"
              type="number"
              min="0"
              :placeholder="text(detail.投递行数)"
            />
          </label>
          <label class="form-line">
            <span>接收方证明凭据</span>
            <input
              v-model="proofCredential"
              data-testid="proof-credential-input"
              placeholder="如 RCV-20260903-3ROWS"
            />
          </label>
          <button
            class="btn primary"
            type="button"
            data-testid="submit-proof"
            :disabled="submitting"
            @click="submitProof"
          >补交证明并恢复原确认</button>

          <h3 class="section-gap">登记迟到的失败回执</h3>
          <p class="hint">另一笔尝试迟报缺 1 行时在此登记：只生成该版本的独立回执，不改已确认台账。</p>
          <label class="form-line">
            <span>迟报缺失行数</span>
            <input v-model="lateMissing" data-testid="late-missing-input" type="number" min="0" />
          </label>
          <label class="form-line">
            <span>该笔尝试凭据（可选）</span>
            <input v-model="lateCredential" data-testid="late-credential-input" placeholder="失败回执编号/目的地凭据" />
          </label>
          <button
            class="btn"
            type="button"
            data-testid="register-late-failure"
            :disabled="submitting"
            @click="registerLate"
          >登记迟到失败（独立版本回执）</button>
        </article>
      </div>

      <article class="info-card section-gap">
        <h3>每次尝试的独立回执与目标确认凭据核对</h3>
        <div class="table-scroll">
          <table class="data-table receipts-table" data-testid="attempts-table">
            <thead>
              <tr>
                <th>尝试编号</th>
                <th>版本</th>
                <th>尝试类型</th>
                <th>报告送达/总数</th>
                <th>报告缺失</th>
                <th>回执时间</th>
                <th>目标确认凭据</th>
                <th>核对状态</th>
                <th>凭据核对</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="(attempt, idx) in attempts"
                :key="attemptNo(attempt, idx)"
                :data-testid="`attempt-row-${idx + 1}`"
                :data-attempt-no="attemptNo(attempt, idx)"
                :data-version="text(attempt.版本)"
                :data-check-status="text(attempt.核对状态)"
                :data-late="attempt.是否迟到 ? 'true' : 'false'"
              >
                <td>{{ attemptNo(attempt, idx) }}</td>
                <td data-testid="attempt-version">{{ text(attempt.版本) }}</td>
                <td>{{ text(attempt.尝试类型 || attempt.回执类型) }}</td>
                <td>{{ text(attempt.报告送达行数) }} / {{ text(attempt.报告总行数) }}</td>
                <td data-testid="attempt-missing">{{ text(attempt.报告缺失行数 ?? attempt.缺失行数) }}</td>
                <td>{{ text(attempt.回执时间) }}</td>
                <td>{{ text(attempt.目标确认凭据 || attempt.凭据) || '—' }}</td>
                <td data-testid="attempt-check-status">{{ text(attempt.核对状态) }}</td>
                <td class="verify-cell">
                  <input
                    v-model="credentialDrafts[attemptNo(attempt, idx)]"
                    :data-testid="`credential-input-${attemptNo(attempt, idx)}`"
                    placeholder="目的地确认凭据"
                  />
                  <input
                    v-model="confirmedDrafts[attemptNo(attempt, idx)]"
                    class="rows-input"
                    type="number"
                    min="0"
                    :data-testid="`confirmed-rows-input-${attemptNo(attempt, idx)}`"
                    placeholder="确认行数"
                  />
                  <button
                    class="link"
                    type="button"
                    :data-testid="`verify-attempt-${attemptNo(attempt, idx)}`"
                    @click="verifyAttempt(attemptNo(attempt, idx))"
                  >按原尝试核对</button>
                </td>
              </tr>
              <tr v-if="!attempts.length">
                <td colspan="9" class="empty-state">该记录还没有分版本尝试回执，可在右侧登记首次/迟到回执。</td>
              </tr>
            </tbody>
          </table>
        </div>
      </article>

      <p v-if="message" class="ok-text" data-testid="detail-message" role="status">{{ message }}</p>
      <p v-if="actionError" class="error-text" data-testid="detail-action-error" role="alert">{{ actionError }}</p>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import { getDeliverDetail, getEntry, runAction, type DeliverAttempt, type DeliverDetail } from '@/api/backend-service'

const route = useRoute()
const routeKey = String(route.params.key ?? route.params.id ?? '')

const loading = ref(true)
const loadError = ref('')
const detail = ref<DeliverDetail | null>(null)
const numericId = ref<number>(0)
const deliverNo = ref('')
const message = ref('')
const actionError = ref('')
const submitting = ref(false)

const proofRows = ref<string | number>('3')
const proofCredential = ref('')
const lateMissing = ref<string | number>('1')
const lateCredential = ref('')
const credentialDrafts = ref<Record<string, string>>({})
const confirmedDrafts = ref<Record<string, string | number>>({})

const attempts = computed<DeliverAttempt[]>(() => detail.value?.尝试回执 ?? [])
const scopeVerified = computed(() => {
  const scope = detail.value?.确认范围 as { verified?: boolean } | undefined
  return Boolean(scope?.verified)
})
const scopeText = computed(() => {
  const scope = detail.value?.确认范围 as
    | { 已确认行数?: number; 目标位置?: string; 确认时间?: string }
    | undefined
  if (!scope) return ''
  return `${scope.已确认行数 ?? ''} 行 @ ${scope.目标位置 ?? ''}（${scope.确认时间 ?? ''}）`
})

function text(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  return String(value)
}

function attemptNo(attempt: DeliverAttempt, idx: number): string {
  return String(attempt.尝试编号 ?? attempt.回执编号 ?? `ATT-${idx + 1}`)
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    // 路由 key 既可能是数字 id，也可能是投递记录编号。
    if (/^\d+$/.test(routeKey)) {
      const entry = await getEntry('result_deliver', Number(routeKey))
      numericId.value = Number(entry.id)
      deliverNo.value = String(entry.投递记录编号 ?? '')
      detail.value = entry as DeliverDetail
      if (deliverNo.value) {
        detail.value = await getDeliverDetail(deliverNo.value)
      }
    } else {
      deliverNo.value = routeKey
      detail.value = await getDeliverDetail(routeKey)
      numericId.value = Number(detail.value.id)
    }
    proofRows.value = String(detail.value.投递行数 ?? '3')
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '详情加载失败'
  } finally {
    loading.value = false
  }
}

async function invoke(action: string, params: Record<string, unknown>) {
  submitting.value = true
  message.value = ''
  actionError.value = ''
  try {
    const result = await runAction('result_deliver', numericId.value, action, params)
    if (!result.ok) {
      actionError.value = result.message
    } else {
      message.value = result.message
    }
    await load()
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : '操作失败'
  } finally {
    submitting.value = false
  }
}

function submitProof() {
  const rows = String(proofRows.value ?? '').trim()
  void invoke('补交证明', {
    confirmed_rows: rows === '' ? undefined : Number(rows),
    credential: proofCredential.value.trim(),
  })
}

function registerLate() {
  void invoke('登记迟到失败', {
    missing_rows: Number(lateMissing.value || '1'),
    credential: lateCredential.value.trim(),
  })
}

function verifyAttempt(no: string) {
  // type="number" 的 v-model 会被 Vue 自动转成 number，这里统一字符串化。
  const rows = String(confirmedDrafts.value[no] ?? '').trim()
  void invoke('核对凭据', {
    attempt_no: no,
    credential: credentialDrafts.value[no]?.trim() ?? '',
    confirmed_rows: rows ? Number(rows) : undefined,
  })
}

watch(() => route.params.key, load)
onMounted(load)
</script>

<style scoped>
.back-link { font-size: 13px; margin: 0 0 6px; }
.back-link a { color: var(--brand, #1f6feb); text-decoration: none; }
.banner { border-radius: 8px; padding: 10px 14px; margin: 10px 0; font-size: 14px; }
.banner.danger { background: #fef3f2; border: 1px solid #fda29b; color: #b42318; }
.banner.ok { background: #ecfdf3; border: 1px solid #6ce9a6; color: #067647; }
.detail-grid { display: grid; grid-template-columns: 1.2fr 1fr; gap: 14px; }
.info-card { background: #fff; border: 1px solid var(--border, #e4e7ec); border-radius: 10px; padding: 14px 16px; }
.info-card h3 { margin: 0 0 10px; font-size: 15px; }
.info-list { display: grid; grid-template-columns: 1fr; gap: 6px; margin: 0; }
.info-list div { display: grid; grid-template-columns: 110px 1fr; gap: 8px; font-size: 13px; }
.info-list dt { color: #667085; }
.info-list dd { margin: 0; word-break: break-all; }
.form-line { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: #667085; margin-bottom: 10px; }
.form-line input { padding: 7px 9px; border: 1px solid #d0d5dd; border-radius: 6px; font-size: 13px; color: #1f2937; }
.hint { font-size: 12px; color: #667085; margin: 0 0 10px; }
.section-gap { margin-top: 18px; }
.table-scroll { overflow-x: auto; }
.receipts-table .verify-cell { display: flex; gap: 6px; align-items: center; min-width: 280px; }
.verify-cell input { padding: 5px 7px; border: 1px solid #d0d5dd; border-radius: 6px; font-size: 12px; min-width: 120px; }
.verify-cell .rows-input { min-width: 76px; }
.ok-text { color: #067647; font-size: 13px; margin-top: 10px; }
@media (max-width: 1100px) { .detail-grid { grid-template-columns: 1fr; } }
</style>
