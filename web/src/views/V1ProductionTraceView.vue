<script setup lang="ts">
/**
 * LemonFlow V1 项目生产追溯页。
 *
 * 展示每次生成被冻结的工作流、模型和 Prompt 版本，方便制作人定位质量或成本问题。
 * 不展示 Prompt 正文、原视频内容或模型原始输出，避免把敏感生产素材扩散到普通页面。
 */
import { computed, onMounted, ref, watch } from 'vue'

import { getProjectModelInvocations } from '@/api/production'
import { getProject } from '@/api/projects'
import type { ModelInvocationTrace, ProjectDetail } from '@/types/domain'

const props = defineProps<{ projectId: string }>()

const project = ref<ProjectDetail | null>(null)
const rows = ref<ModelInvocationTrace[]>([])
const loading = ref(true)
const error = ref('')
const taskFilter = ref('')

const taskLabels: Record<string, string> = {
  VIDEO_ANALYSIS: '参考视频分析',
  STORY_GENERATE: '原创故事生成',
  CHARACTER_DESIGN: '角色文字资产设计',
  SCENE_DESIGN: '场景文字资产设计',
  DIRECTOR_PLAN: 'AI 导演分镜',
  IMAGE_GENERATE: '图片资产生成',
  VIDEO_GENERATE: '视频片段生成',
  FINAL_COMPOSE: '最终成片合成',
}

const filteredRows = computed(() => rows.value.filter((item) => !taskFilter.value || item.task_type === taskFilter.value))

function formatTime(value: string | null): string {
  return value ? new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value)) : '未完成'
}

function duration(value: number | null): string {
  return value === null ? '暂无' : value >= 1000 ? `${(value / 1000).toFixed(1)} 秒` : `${value} 毫秒`
}

function cost(item: ModelInvocationTrace): string {
  return item.cost_amount === null ? '未填写' : `${item.cost_amount.toFixed(4)} ${item.currency}`
}

function shortHash(value: string | null | undefined): string {
  return value ? `${value.slice(0, 12)}…` : '未记录哈希'
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [loadedProject, traces] = await Promise.all([getProject(props.projectId), getProjectModelInvocations(props.projectId)])
    project.value = loadedProject
    rows.value = traces
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '生产追溯记录加载失败，请确认后端服务已启动。'
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(() => props.projectId, load)
</script>

<template>
  <section v-if="loading" class="panel">正在读取生产追溯记录…</section>
  <template v-else>
    <section class="page-heading">
      <div>
        <RouterLink class="muted" :to="`/projects/${projectId}`">← 返回生产台</RouterLink>
        <h1>{{ project?.title || '项目' }}：模型与版本记录</h1>
        <p>这里用于回答“这一版结果用了什么”。每一行是一次模型调用的冻结记录。</p>
      </div>
    </section>

    <p v-if="error" class="notice error">{{ error }}</p>
    <section v-else class="panel stack">
      <h2>如何使用</h2>
      <ol class="setup-steps">
        <li>内容效果有问题时，先看对应“用途”，再确认模型版本和 Prompt 版本。</li>
        <li>视频生成有问题时，可记录供应商任务号交给技术人员排查。</li>
        <li>成本与耗时只用于比较；“未填写”代表模型中心没有配置预估成本。</li>
      </ol>
      <p class="notice info">为了保护生产素材，本页不显示原视频、Prompt 正文、模型输入或原始输出。完整历史仍由后端审计记录保留。</p>
    </section>

    <section v-if="!error" class="panel trace-tools">
      <label class="field">只看一个用途
        <select v-model="taskFilter"><option value="">全部用途</option><option v-for="(label, key) in taskLabels" :key="key" :value="key">{{ label }}</option></select>
      </label>
      <button class="button secondary" @click="load">刷新记录</button>
    </section>

    <section v-if="!error && !filteredRows.length" class="panel stack">
      <h2>还没有模型调用记录</h2>
      <p class="muted">先在生产台发起一次生成任务。模型实际开始运行后，记录会自动写入这里。</p>
    </section>
    <section v-else-if="!error" class="panel trace-table-wrap">
      <table class="trace-table">
        <thead><tr><th>用途 / 状态</th><th>工作流版本</th><th>模型版本</th><th>Prompt 版本</th><th>成本 / 耗时</th><th>任务与时间</th></tr></thead>
        <tbody>
          <tr v-for="item in filteredRows" :key="item.id">
            <td><strong>{{ taskLabels[item.task_type] || item.task_type }}</strong><span class="status" :class="item.status">{{ item.status }}</span><small v-if="item.error_code" class="error-text">错误：{{ item.error_code }}</small></td>
            <td><strong>{{ item.workflow_version || '历史记录' }}</strong><small>{{ item.workflow_key || '未记录工作流键' }}</small></td>
            <td><strong>{{ item.model_display_name }}</strong><small>{{ item.model_key }} · {{ item.model_version }}<template v-if="item.model_profile_version"> · 配置 v{{ item.model_profile_version }}</template></small></td>
            <td><strong>{{ item.prompt_name || '未记录' }}</strong><small>{{ item.prompt_version ? `v${item.prompt_version}` : '未记录版本' }} · {{ shortHash(item.prompt_content_hash) }}</small></td>
            <td><strong>{{ cost(item) }}</strong><small>耗时：{{ duration(item.latency_ms) }}</small></td>
            <td><strong>{{ formatTime(item.created_at) }}</strong><small v-if="item.provider_task_id">供应商任务号：{{ item.provider_task_id }}</small><small v-else>平台调用 ID：{{ item.id }}</small></td>
          </tr>
        </tbody>
      </table>
    </section>
  </template>
</template>

<style scoped>
.trace-tools { display: flex; align-items: end; gap: 14px; margin-top: 20px; }
.trace-tools .field { margin: 0; min-width: 220px; }
.trace-table-wrap { margin-top: 20px; overflow-x: auto; }
.trace-table { width: 100%; min-width: 1020px; border-collapse: collapse; font-size: 14px; }
.trace-table th, .trace-table td { padding: 12px; border-bottom: 1px solid #e2e8f0; text-align: left; vertical-align: top; }
.trace-table th { color: #475569; background: #f8fafc; white-space: nowrap; }
.trace-table td > small, .trace-table td > .status { display: block; margin-top: 5px; color: #64748b; }
.trace-table .status { width: fit-content; }
.error-text { color: #b91c1c !important; }
@media (max-width: 640px) { .trace-tools { flex-direction: column; align-items: stretch; } .trace-tools .field { min-width: 0; } }
</style>
