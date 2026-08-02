<script setup lang="ts">
/**
 * 单个模型配置的小样本验收面板。
 * 组件只处理表单展示与输入校验；读取、保存数据仍由父页面通过 Store 注入，避免组件
 * 直接耦合 HTTP 接口，便于以后把评测展示移动到独立的模型对比页面。
 */
import { computed, ref } from 'vue'
import type { ModelEvaluation, ModelEvaluationPayload } from '@/types/domain'

const props = defineProps<{
  profileId: string
  profileLabel: string
  evaluations: ModelEvaluation[]
  loading: boolean
  saving: boolean
  loadEvaluations: (profileId: string) => Promise<void>
  saveEvaluation: (profileId: string, payload: ModelEvaluationPayload) => Promise<boolean>
}>()

const expanded = ref(false)
const scenario = ref('')
const sampleCount = ref(10)
const successCount = ref(0)
const totalCostYuan = ref(0)
const averageLatencySeconds = ref(1)
const qualityScore = ref(80)
const notes = ref('')
const formError = ref('')

const hasRecords = computed(() => props.evaluations.length > 0)

/** 首次展开时才请求历史记录，避免配置页加载时产生大量无意义请求。 */
async function toggle(): Promise<void> {
  expanded.value = !expanded.value
  if (expanded.value) await props.loadEvaluations(props.profileId)
}

/** 前端先做简单范围校验，后端仍会再次验证并作为最终可信边界。 */
async function submit(): Promise<void> {
  formError.value = ''
  if (!scenario.value.trim()) {
    formError.value = '请填写这次小样本的测试场景'
    return
  }
  if (successCount.value > sampleCount.value) {
    formError.value = '成功样本数不能大于总样本数'
    return
  }
  const saved = await props.saveEvaluation(props.profileId, {
    scenario: scenario.value.trim(),
    sample_count: sampleCount.value,
    success_count: successCount.value,
    total_cost_yuan: totalCostYuan.value,
    average_latency_seconds: averageLatencySeconds.value,
    quality_score: qualityScore.value,
    ...(notes.value.trim() ? { notes: notes.value.trim() } : {}),
  })
  if (saved) {
    scenario.value = ''
    notes.value = ''
  }
}

/** 小数统一保留四位，避免不同浏览器的默认浮点展示干扰模型成本比较。 */
function yuan(value: number | null): string {
  return value === null ? '—' : `¥${value.toFixed(4)}`
}
</script>

<template>
  <section class="stack" :aria-label="`${props.profileLabel} 实测记录`">
    <button class="button secondary" type="button" @click="toggle">
      {{ expanded ? '收起实测记录' : '记录 / 查看模型实测' }}
    </button>

    <div v-if="expanded" class="stack">
      <form class="stack" @submit.prevent="submit">
        <strong>新增小样本验收</strong>
        <label class="field">测试场景<input v-model="scenario" maxlength="120" placeholder="例如：9:16 五镜图生视频小样" /></label>
        <label class="field">总样本数<input v-model.number="sampleCount" type="number" min="1" max="100000" /></label>
        <label class="field">成功样本数<input v-model.number="successCount" type="number" min="0" :max="sampleCount" /></label>
        <label class="field">总成本（元）<input v-model.number="totalCostYuan" type="number" min="0" step="0.0001" /></label>
        <label class="field">平均耗时（秒）<input v-model.number="averageLatencySeconds" type="number" min="0.01" max="86400" step="0.01" /></label>
        <label class="field">人工质量评分（0-100）<input v-model.number="qualityScore" type="number" min="0" max="100" /></label>
        <label class="field">备注（可选）<textarea v-model="notes" maxlength="2000" rows="2" placeholder="例如：角色一致性好，但动作幅度偏小" /></label>
        <p v-if="formError" class="notice error">{{ formError }}</p>
        <button class="button" :disabled="props.saving">
          {{ props.saving ? '保存中…' : '保存本次实测' }}
        </button>
      </form>

      <p v-if="props.loading" class="muted">正在读取历史实测…</p>
      <p v-else-if="!hasRecords" class="muted">还没有实测记录。建议先用最小样本验证，再决定是否启用此模型版本。</p>
      <article v-for="record in props.evaluations" :key="record.id" class="panel stack">
        <div class="meta-row">
          <strong>{{ record.scenario }}</strong>
          <span>质量 {{ record.quality_score }}/100</span>
        </div>
        <small class="muted">
          成功 {{ record.success_count }}/{{ record.sample_count }}（{{ record.success_rate }}%） ·
          平均耗时 {{ record.average_latency_seconds }} 秒 ·
          单样本 {{ yuan(record.average_cost_yuan) }} ·
          单成功样本 {{ yuan(record.cost_per_success_yuan) }}
        </small>
        <p v-if="record.notes" class="muted">{{ record.notes }}</p>
      </article>
    </div>
  </section>
</template>
