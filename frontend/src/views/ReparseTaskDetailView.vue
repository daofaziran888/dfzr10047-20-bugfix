<template>
  <section class="page reparse-workbench" data-testid="reparse-workbench" :data-task-no="taskNo">
    <header class="page-head">
      <div>
        <p class="back-link"><RouterLink to="/reparse_task">← 返回重解析任务台账</RouterLink></p>
        <h2>重解析工作台 · 新轮替代与原件核对</h2>
        <p class="page-desc">
          结果投递台账把已查实的确认范围同步到这里；矛盾的“已确认但缺行”台账不能实施目标替换。
          新轮替代申请须关联核对原件的原回执，领取后状态持久保存，断线重进可辨别“已领待目标校验”或“需要重新领办”。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" data-testid="sync-scope" :disabled="busy" @click="syncScope">
          同步投递已查实确认范围
        </button>
      </div>
    </header>

    <p v-if="loading && !bench" data-testid="workbench-loading">工作台加载中…</p>
    <p v-else-if="loadError && !bench" class="error-text" data-testid="workbench-load-error">{{ loadError }}</p>

    <template v-else>
      <article class="info-card">
        <h3>任务与确认范围</h3>
        <div class="task-meta">
          <span><b>{{ taskNo }}</b></span>
          <span>状态：<i data-testid="task-status">{{ text(bench?.status) }}</i></span>
          <span>覆盖策略：{{ text(bench?.覆盖策略) }}</span>
        </div>
        <div data-testid="verified-scope" class="scope-list">
          <p v-if="!verifiedScope.length" class="hint">
            暂无“已查实”的确认范围：矛盾或未核对凭据的投递不会被同步为可替换依据。
          </p>
          <div v-for="scope in verifiedScope" :key="String(scope.投递记录编号)" class="scope-chip"
               :data-deliver-no="String(scope.投递记录编号)" data-testid="scope-item">
            <RouterLink :to="`/result_deliver/${scope.投递记录编号}`">
              {{ text(scope.投递记录编号) }}
            </RouterLink>
            · {{ text(scope.已确认行数) }}/{{ text(scope.投递行数) }} 行
            · {{ text(scope.目标位置) }} · 确认于 {{ text(scope.确认时间) }}
            · 原回执 {{ text(scope.关联原回执) }}
          </div>
        </div>
      </article>

      <article class="info-card section-gap">
        <h3>提交新轮替代申请</h3>
        <div class="form-grid">
          <label><span>申请编号（可空，自动生成）</span>
            <input v-model="form.appNo" data-testid="app-no-input" placeholder="如 RA-20260905-1" />
          </label>
          <label><span>原投递记录编号</span>
            <input v-model="form.deliverNo" data-testid="original-deliver-input" placeholder="如 RD-005" />
          </label>
          <label><span>关联原回执/尝试编号</span>
            <input v-model="form.receiptNo" data-testid="original-receipt-input" placeholder="如 RD-005-A1" />
          </label>
          <label class="wide"><span>申请说明</span>
            <input v-model="form.reason" data-testid="app-reason-input" placeholder="接班人申请将来可单独交付的新轮替代" />
          </label>
        </div>
        <button class="btn primary" type="button" data-testid="submit-application" :disabled="busy" @click="submitApplication">
          提交新轮替代申请
        </button>
      </article>

      <article class="section-gap">
        <h3>替代申请与可继续操作</h3>
        <div
          v-for="app in applications"
          :key="appNo(app)"
          class="info-card app-card"
          :data-testid="`app-card-${appNo(app)}`"
          :data-app-no="appNo(app)"
          :data-state="text(app.状态)"
          :data-verdict="text(app.状态判定 || app.重进判定)"
        >
          <header class="app-head-row">
            <b>{{ appNo(app) }}</b>
            <span class="state-badge" data-testid="app-state">{{ text(app.状态) }}</span>
            <span class="verdict" data-testid="app-reentry-verdict">
              重进判定：{{ text(app.状态判定 || app.重进判定) }}
            </span>
          </header>
          <div class="app-meta">
            <span>原投递：
              <RouterLink v-if="app.原投递记录" :to="`/result_deliver/${app.原投递记录}`"
                          :data-testid="`original-deliver-link-${appNo(app)}`">
                {{ text(app.原投递记录) }}
              </RouterLink>
              <template v-else>未关联</template>
            </span>
            <span data-testid="original-receipts" :data-receipt="text(app.关联原回执)">
              关联原回执：{{ text(app.关联原回执) || '待补证据' }}
            </span>
            <span v-if="app.领取人">领取人：{{ text(app.领取人) }} · {{ text(app.领取时间) }}</span>
          </div>

          <p v-if="app.阻断原因" class="banner danger small" data-testid="app-block-reason">
            {{ text(app.阻断原因) }}
          </p>
          <p v-if="app.可领为新轮" class="banner ok small" data-testid="app-eligible">
            ✓ 原件已核对，可领为独立新轮
          </p>

          <div class="receipt-evidence" :data-testid="`original-receipts-${appNo(app)}`">
            <span class="hint">原回执清单：</span>
            <span v-for="rc in originalReceipts(app)" :key="String(rc.尝试编号)" class="receipt-chip"
                  :data-check-status="text(rc.核对状态)">
              {{ text(rc.尝试编号) }}（{{ text(rc.版本) }}，{{ text(rc.核对状态) }}）
            </span>
            <span v-if="!originalReceipts(app).length" class="hint">暂无回执明细</span>
          </div>

          <div class="action-zone">
            <button
              v-if="text(app.状态) === '待领办'"
              class="btn primary"
              type="button"
              :data-testid="`claim-${appNo(app)}`"
              :disabled="busy"
              @click="claim(appNo(app))"
            >领取办理（领取后为“已领待目标校验”）</button>

            <template v-if="text(app.状态) === '已领待目标校验'">
              <input v-model="proofDrafts[appNo(app)].credential"
                     :data-testid="`app-proof-credential-${appNo(app)}`"
                     placeholder="接收方三行证明凭据" />
              <input v-model="proofDrafts[appNo(app)].rows" class="rows-input" type="number" min="0"
                     :data-testid="`app-proof-rows-${appNo(app)}`" placeholder="证明行数，如 3" />
              <button class="btn" type="button"
                      :data-testid="`verify-evidence-${appNo(app)}`" :disabled="busy"
                      @click="verifyEvidence(appNo(app))">补交证明并核对原件</button>

              <input v-model="newRoundDrafts[appNo(app)]"
                     :data-testid="`new-round-no-${appNo(app)}`"
                     placeholder="新轮编号/新投递编号（可空）" />
              <button class="btn primary" type="button"
                      :data-testid="`claim-new-round-${appNo(app)}`"
                      :disabled="busy || !app.可领为新轮"
                      :title="app.可领为新轮 ? '' : text(app.阻断原因)"
                      @click="claimNewRound(appNo(app))">领为独立新轮</button>
            </template>

            <template v-if="text(app.状态) === '已领为独立新轮'">
              <RouterLink class="btn" :to="`/result_deliver/${app.新轮投递记录}`"
                          :data-testid="`new-round-link-${appNo(app)}`">
                查看新轮投递 {{ text(app.新轮投递记录) }}
              </RouterLink>
              <input v-model="failureDrafts[appNo(app)]" class="rows-input" type="number" min="0"
                     :data-testid="`new-round-failure-rows-${appNo(app)}`" placeholder="未写入行数" />
              <button class="btn" type="button"
                      :data-testid="`register-new-round-failure-${appNo(app)}`" :disabled="busy"
                      @click="registerFailure(appNo(app))">登记新轮失败</button>
              <button class="btn" type="button"
                      :data-testid="`retry-new-round-${appNo(app)}`" :disabled="busy"
                      @click="retryNewRound(appNo(app))">重试新轮（0=全部补上）</button>
            </template>
          </div>
        </div>

        <p v-if="!applications.length" class="empty-state info-card">
          还没有新轮替代申请，可在上方提交；申请会持久保存在重解析单上，断线重进不丢。
        </p>
      </article>

      <p v-if="message" class="ok-text" data-testid="workbench-message" role="status">{{ message }}</p>
      <p v-if="actionError" class="error-text" data-testid="workbench-action-error" role="alert">{{ actionError }}</p>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import { getEntry, getReparseWorkbench, listEntries, runAction } from '@/api/backend-service'

type AppView = Record<string, any>
type Workbench = {
  重解析编号?: string
  status?: string
  覆盖策略?: string
  已核实确认范围?: Record<string, unknown>[]
  applications?: AppView[]
  替代申请清单?: AppView[]
}

const route = useRoute()
const routeKey = String(route.params.key ?? '')

const loading = ref(true)
const loadError = ref('')
const bench = ref<Workbench | null>(null)
const numericId = ref(0)
const taskNo = ref('')
const message = ref('')
const actionError = ref('')
const busy = ref(false)

const form = reactive({ appNo: '', deliverNo: '', receiptNo: '', reason: '' })
const proofDrafts = reactive<Record<string, { credential: string; rows: string }>>({})
const newRoundDrafts = reactive<Record<string, string>>({})
const failureDrafts = reactive<Record<string, string>>({})

const applications = computed<AppView[]>(
  () => bench.value?.applications ?? bench.value?.替代申请清单 ?? [],
)
const verifiedScope = computed<Record<string, unknown>[]>(
  () => bench.value?.已核实确认范围 ?? [],
)

function text(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  return String(value)
}
function appNo(app: AppView): string {
  return String(app.申请编号 ?? '')
}
function originalReceipts(app: AppView): Record<string, unknown>[] {
  return (app.原回执清单 as Record<string, unknown>[]) ?? []
}
function ensureDrafts() {
  for (const app of applications.value) {
    const no = appNo(app)
    if (!proofDrafts[no]) proofDrafts[no] = { credential: '', rows: '3' }
    if (newRoundDrafts[no] === undefined) newRoundDrafts[no] = ''
    if (failureDrafts[no] === undefined) failureDrafts[no] = ''
  }
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    let no = routeKey
    if (/^\d+$/.test(routeKey)) {
      const entry = await getEntry('reparse_task', Number(routeKey))
      numericId.value = Number(entry.id)
      no = String(entry.重解析编号 ?? '')
    }
    taskNo.value = no
    const data = (await getReparseWorkbench(no)) as Workbench & { id?: number }
    bench.value = data
    if (!numericId.value) {
      numericId.value = data.id ? Number(data.id) : await getEntryByNo(no)
    }
    ensureDrafts()
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '工作台加载失败'
  } finally {
    loading.value = false
  }
}

async function getEntryByNo(no: string): Promise<number> {
  // 通过列表按编号找到数字 id（动作接口需要行 id）。
  const page = await listEntries('reparse_task', { 重解析编号: no })
  const row = page.items.find((item) => String(item.重解析编号) === no)
  return Number(row?.id ?? 0)
}

async function invoke(action: string, params: Record<string, unknown>) {
  busy.value = true
  message.value = ''
  actionError.value = ''
  try {
    const result = await runAction('reparse_task', numericId.value, action, params)
    if (!result.ok) actionError.value = result.message
    else message.value = result.message
    await load()
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : '操作失败'
  } finally {
    busy.value = false
  }
}

function submitApplication() {
  void invoke('提交新轮替代申请', {
    申请编号: form.appNo.trim() || undefined,
    原投递记录: form.deliverNo.trim() || undefined,
    关联原回执: form.receiptNo.trim() || undefined,
    reason: form.reason.trim(),
  })
}
function claim(no: string) {
  void invoke('领取办理', { 申请编号: no })
}
function verifyEvidence(no: string) {
  const draft = proofDrafts[no] ?? { credential: '', rows: '3' }
  void invoke('核对目标凭据', {
    申请编号: no,
    credential: draft.credential.trim(),
    proof_rows: draft.rows ? Number(draft.rows) : undefined,
  })
}
function claimNewRound(no: string) {
  const custom = newRoundDrafts[no]?.trim()
  void invoke('领为独立新轮', {
    申请编号: no,
    new_deliver_no: custom || undefined,
    new_round_no: custom || undefined,
  })
}
function registerFailure(no: string) {
  void invoke('登记新轮失败', {
    申请编号: no,
    missing_rows: Number(failureDrafts[no] || '0'),
  })
}
function retryNewRound(no: string) {
  void invoke('重试新轮', { 申请编号: no, still_missing: 0 })
}
function syncScope() {
  void invoke('同步确认范围', {})
}

watch(() => route.params.key, load)
onMounted(load)
</script>

<style scoped>
.back-link { font-size: 13px; margin: 0 0 6px; }
.back-link a { color: var(--brand, #1f6feb); text-decoration: none; }
.info-card { background: #fff; border: 1px solid var(--border, #e4e7ec); border-radius: 10px; padding: 14px 16px; }
.task-meta { display: flex; gap: 16px; flex-wrap: wrap; font-size: 13px; margin-bottom: 10px; }
.scope-list { display: flex; flex-direction: column; gap: 6px; }
.scope-chip { font-size: 13px; background: #f9fafb; border: 1px solid #eef0f3; border-radius: 6px; padding: 6px 10px; }
.scope-chip a, .app-meta a { color: #1f6feb; text-decoration: none; }
.section-gap { margin-top: 16px; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 10px; }
.form-grid label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: #667085; }
.form-grid .wide { grid-column: 1 / -1; }
.form-grid input, .action-zone input { padding: 7px 9px; border: 1px solid #d0d5dd; border-radius: 6px; font-size: 13px; }
.app-card { margin-bottom: 12px; }
.app-head-row { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.state-badge { background: #eff4ff; color: #175cd3; border-radius: 999px; padding: 2px 10px; font-size: 12px; }
.verdict { font-size: 12px; color: #b54708; background: #fffaeb; border-radius: 999px; padding: 2px 10px; }
.app-meta { display: flex; gap: 16px; flex-wrap: wrap; font-size: 13px; margin-bottom: 8px; }
.receipt-evidence { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; margin-bottom: 10px; }
.receipt-chip { font-size: 12px; background: #f2f4f7; border-radius: 6px; padding: 3px 8px; }
.action-zone { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.action-zone .rows-input { width: 110px; }
.banner { border-radius: 8px; padding: 8px 12px; font-size: 13px; margin: 6px 0; }
.banner.danger { background: #fef3f2; border: 1px solid #fda29b; color: #b42318; }
.banner.ok { background: #ecfdf3; border: 1px solid #6ce9a6; color: #067647; }
.banner.small { padding: 6px 10px; }
.hint { font-size: 12px; color: #667085; }
.ok-text { color: #067647; font-size: 13px; }
@media (max-width: 900px) { .form-grid { grid-template-columns: 1fr; } }
</style>
