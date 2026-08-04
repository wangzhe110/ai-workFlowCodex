<script setup lang="ts">
/**
 * LemonFlow V1 模型质量与成本报表。
 *
 * 报表只读取已有模型调用与人工审核数据。点击“生成最新报表”不会再次调用任何模型，
 * 更不会自动修改模型中心的启用状态；制作人仍需回到模型中心手工决定是否切换版本。
 */
import { computed, onMounted, ref } from 'vue'

import { getModelQualityEvaluations, refreshModelQualityEvaluations } from '@/api/production'
import type { ModelQualityEvaluation } from '@/types/domain'

const rows = ref<ModelQualityEvaluation[]>([])
const loading = ref(true)
const refreshing = ref(false)
const error = ref('')
const taskType = ref('')

const taskLabels: Record<string, string> = {
  VIDEO_ANALYSIS: '参考视频分析',
  STORY_GENERATE: '原创故事生成',
  CHARACTER_DESIGN: '角色文字设计',
  SCENE_DESIGN: '场景文字设计',
  DIRECTOR_PLAN: 'AI 导演分镜',
  IMAGE_GENERATE: '图片资产生成',
  VIDEO_GENERATE: '视频片段生成',
  FINAL_COMPOSE: '成片合成',
}

const selectedRows = computed(() => rows.value.filter((row) => !taskType.value || row.task_type === taskType.value))

function percentage(value: number | null): string {
  return value === null ? '暂无' : `${(value * 100).toFixed(0)}%`
}

function cost(row: ModelQualityEvaluation): string {
  return row.average_cost_amount === null ? '未填写' : `${row.average_cost_amount.toFixed(4)} ${row.currency}`
}

function latency(value: number | null): string {
  return value === null ? '暂无' : value >= 1000 ? `${(value / 1000).toFixed(1)} 秒` : `${value} 毫秒`
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value))
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    rows.value = await getModelQualityEvaluations(taskType.value || undefined)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '质量报表加载失败，请确认后端服务已启动。'
  } finally {
    loading.value = false
  }
}

async function refreshReport() {
  refreshing.value = true
  error.value = ''
  try {
    rows.value = await refreshModelQualityEvaluations(taskType.value || undefined)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '生成质量报表失败，请稍后重试。'
  } finally {
    refreshing.value = false
  }
}

onMounted(load)
</script>

<template>
  <section class="page-heading">
    <div>
      <RouterLink class="muted" to="/model-profiles">← 返回模型中心</RouterLink>
      <h1>模型质量与成本报表</h1>
      <p>用真实生产调用和人工审核来比较模型。报表只给参考，不会自动替你切换模型。</p>
    </div>
  </section>

  <section class="panel stack">
    <h2>怎么看这张表？</h2>
    <ol class="setup-steps">
      <li>先在生产台审核时填写“质量评分”；不打分也不影响正常生产。</li>
      <li>每完成一批测试，点击“生成最新报表”。这只统计已有数据，不会再次产生模型费用。</li>
      <li>优先在同一任务下比较：评分、成功率、已审核采用率和预计成本；最后回到模型中心手动切换。</li>
    </ol>
    <p class="notice info">“预计成本”来自模型中心手工填写的每次生成费用，不是供应商账单；未填写时显示“未填写”。“已审核采用率”只统计已经作出审核决定的结果。</p>
  </section>

  <section class="panel stack report-tools">
    <label class="field">只看一个用途
      <select v-model="taskType" :disabled="loading || refreshing" @change="load">
        <option value="">全部用途</option>
        <option v-for="(label, key) in taskLabels" :key="key" :value="key">{{ label }}</option>
      </select>
    </label>
    <button class="button" :disabled="refreshing" @click="refreshReport">{{ refreshing ? '正在统计…' : '生成最新报表' }}</button>
  </section>

  <p v-if="error" class="notice error">{{ error }}</p>
  <section v-else-if="loading" class="panel">正在读取质量报表…</section>
  <section v-else-if="!selectedRows.length" class="panel stack">
    <h2>还没有可比较的数据</h2>
    <p class="muted">请先完成至少一次模型生成，然后点“生成最新报表”。若希望看到质量分数，请在生产台审核时选择 1–10 分。</p>
  </section>
  <section v-else class="panel report-table-wrap">
    <table class="report-table">
      <thead>
        <tr>
          <th>用途 / 模型 / Prompt</th>
          <th>样本</th>
          <th>成功率</th>
          <th>人工评分</th>
          <th>已审核采用率</th>
          <th>预计每次成本</th>
          <th>平均耗时</th>
          <th>统计时间</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in selectedRows" :key="row.id">
          <td>
            <strong>{{ taskLabels[row.task_type] || row.task_type }}</strong>
            <span>{{ row.display_name }} · {{ row.model_version }}</span>
            <small>Prompt：{{ row.prompt_name ? `${row.prompt_name} v${row.prompt_version}` : '未记录' }}</small>
          </td>
          <td>{{ row.success_count }} / {{ row.sample_count }}</td>
          <td>{{ percentage(row.success_rate) }}</td>
          <td>{{ row.average_human_score === null ? '暂无评分' : `${row.average_human_score.toFixed(1)} / 10` }}</td>
          <td>{{ percentage(row.adoption_rate) }}</td>
          <td>{{ cost(row) }}</td>
          <td>{{ latency(row.average_latency_ms) }}</td>
          <td>{{ formatTime(row.created_at) }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<style scoped>
.report-tools { display: flex; align-items: end; gap: 14px; margin-top: 20px; }
.report-tools .field { min-width: 220px; margin: 0; }
.report-table-wrap { margin-top: 20px; overflow-x: auto; }
.report-table { width: 100%; min-width: 940px; border-collapse: collapse; font-size: 14px; }
.report-table th, .report-table td { padding: 12px; border-bottom: 1px solid #e2e8f0; text-align: left; vertical-align: top; }
.report-table th { color: #475569; background: #f8fafc; white-space: nowrap; }
.report-table td > span, .report-table td > small { display: block; margin-top: 4px; color: #64748b; }
@media (max-width: 640px) { .report-tools { align-items: stretch; flex-direction: column; } .report-tools .field { min-width: 0; } }
</style>
