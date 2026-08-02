<script setup lang="ts">
/**
 * 项目生产工作台。
 * 当前只实现“素材 → 视频分析”闭环；后续选题、故事和分镜仍沿用同一个 projectId。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'

import WorkflowTimeline from '@/components/WorkflowTimeline.vue'
import { useProjectStore } from '@/stores/project'

const props = defineProps<{ projectId: string }>()
const projectStore = useProjectStore()

const selectedFile = ref<File | null>(null)
const uploading = ref(false)
const starting = ref(false)

const project = computed(() => projectStore.currentProject)
const hasSourceVideo = computed(() => Boolean(project.value?.assets.some((asset) => asset.kind === 'SOURCE_VIDEO')))

onMounted(() => void projectStore.loadProject(props.projectId))
onUnmounted(() => projectStore.stopPolling())

// 同一个组件实例切换路由参数时，重新加载数据，避免展示上一个项目的状态。
watch(() => props.projectId, (projectId) => void projectStore.loadProject(projectId))

/** 只保存 File 引用，真正上传由用户点击确认触发。 */
function selectVideo(event: Event) {
  const input = event.target as HTMLInputElement
  selectedFile.value = input.files?.[0] ?? null
}

/** 上传成功后刷新 Store，供“启动分析”按钮读取最新素材状态。 */
async function submitUpload() {
  if (!selectedFile.value) {
    projectStore.error = '请选择一个视频文件'
    return
  }
  uploading.value = true
  const success = await projectStore.uploadVideo(props.projectId, selectedFile.value)
  uploading.value = false
  if (success) selectedFile.value = null
}

/** 创建后台分析任务；页面由 Store 轮询其状态而不等待模型调用。 */
async function submitAnalysis() {
  starting.value = true
  await projectStore.beginAnalysis(props.projectId)
  starting.value = false
}

/** 文件大小仅用于页面展示，服务端仍是最终校验者。 */
function formatBytes(size: number): string {
  return `${(size / 1024 / 1024).toFixed(2)} MB`
}
</script>

<template>
  <section v-if="projectStore.loading && !project" class="panel">正在加载项目…</section>
  <section v-else-if="!project" class="panel stack">
    <p class="notice error">{{ projectStore.error || '未找到项目' }}</p>
    <RouterLink class="button secondary" to="/">返回项目列表</RouterLink>
  </section>

  <template v-else>
    <section class="page-heading">
      <div>
        <RouterLink class="muted" to="/">← 返回项目列表</RouterLink>
        <h1>{{ project.title }}</h1>
        <p>{{ project.description || '暂未填写创作方向' }}</p>
      </div>
    </section>

    <p v-if="projectStore.error" class="notice error">{{ projectStore.error }}</p>

    <div class="grid">
      <section class="panel stack">
        <div>
          <h2>1. 上传参考视频</h2>
          <p class="muted">请确认你对上传素材拥有合法使用与分析授权。</p>
        </div>
        <label class="field">
          视频文件
          <input accept="video/mp4,video/quicktime,video/x-matroska,video/webm" type="file" @change="selectVideo" />
        </label>
        <button class="button" :disabled="uploading || !selectedFile" @click="submitUpload">
          {{ uploading ? '正在上传…' : '上传参考视频' }}
        </button>
        <div v-if="project.assets.length" class="stack">
          <strong>已上传素材</strong>
          <div v-for="asset in project.assets" :key="asset.id" class="meta-row">
            <span>{{ asset.original_filename }}</span>
            <span>{{ formatBytes(asset.byte_size) }}</span>
          </div>
        </div>
      </section>

      <section class="panel stack">
        <div>
          <h2>2. 分析原创机制</h2>
          <p class="muted">提取开头、冲突、节奏等抽象结构，不复用具体内容。</p>
        </div>
        <button class="button" :disabled="!hasSourceVideo || starting" @click="submitAnalysis">
          {{ starting ? '正在创建任务…' : '启动视频分析' }}
        </button>
        <p v-if="!hasSourceVideo" class="notice info">请先上传一个参考视频。</p>
        <RouterLink v-if="project.workflow_runs.some((run) => run.workflow_key === 'video_analysis' && run.status === 'SUCCEEDED')" class="button secondary" :to="{ name: 'project-topics', params: { projectId } }">
          进入原创选题
        </RouterLink>
      </section>
    </div>

    <section v-if="projectStore.activeRun" style="margin-top: 20px">
      <WorkflowTimeline
        :run="projectStore.activeRun"
        :retrying="starting"
        @retry="projectStore.retryAnalysis"
      />
    </section>
  </template>
</template>
