<script setup lang="ts">
/**
 * 工作流时间线组件。
 * 它接收已加载的运行数据，所有重试等业务动作以事件交给页面处理，从而保持可复用。
 */
import type { WorkflowRun } from '@/types/domain'

const props = defineProps<{
  run: WorkflowRun
  retrying?: boolean
}>()

const emit = defineEmits<{
  retry: [runId: string]
}>()

/** 把后端英文状态映射为用户能理解的中文，不改变后端协议值。 */
function statusText(status: string): string {
  return {
    PENDING: '排队中',
    RUNNING: '执行中',
    SUCCEEDED: '已完成',
    FAILED: '失败',
    CANCELLED: '已取消',
  }[status] ?? status
}

/** 将结构化结果格式化为可审阅的中文 JSON，后续替换为专用结果卡片。 */
function resultText(payload: Record<string, unknown> | null): string {
  return payload ? JSON.stringify(payload, null, 2) : ''
}

/** 将后端稳定步骤键转换为工作台可读的名称。 */
function stepLabel(stepKey: string): string {
  return {
    transcribe_reference_audio: '提取语音开头机制',
    analyze_reference_mechanisms: '提取画面与综合原创机制',
  }[stepKey] ?? stepKey
}
</script>

<template>
  <section class="panel stack" aria-label="工作流运行详情">
    <div class="meta-row">
      <strong>视频分析工作流</strong>
      <span class="status" :class="props.run.status">{{ statusText(props.run.status) }}</span>
    </div>

    <article
      v-for="step in props.run.steps"
      :key="step.id"
      class="timeline-step"
      :class="`is-${step.status.toLowerCase()}`"
    >
      <div class="meta-row">
        <strong>{{ stepLabel(step.step_key) }}</strong>
        <span>{{ statusText(step.status) }} · 第 {{ step.attempt }} 次</span>
      </div>
      <div class="progress-track" aria-label="任务进度">
        <div class="progress-value" :style="{ width: `${step.progress}%` }" />
      </div>
      <p v-if="step.error_message" class="notice error">{{ step.error_message }}</p>
      <pre v-if="step.output_payload" class="result">{{ resultText(step.output_payload) }}</pre>
    </article>

    <button
      v-if="props.run.status === 'FAILED' || props.run.status === 'CANCELLED'"
      class="button danger"
      :disabled="props.retrying"
      @click="emit('retry', props.run.id)"
    >
      {{ props.retrying ? '正在重新投递…' : '重试该工作流' }}
    </button>
  </section>
</template>
