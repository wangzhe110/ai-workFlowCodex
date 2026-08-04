<script setup lang="ts">
/**
 * 项目首页。
 * 创建成功后进入项目工作台，避免用户在列表页完成复杂生产操作。
 */
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { useProjectStore } from '@/stores/project'

const router = useRouter()
const projectStore = useProjectStore()

const title = ref('')
const description = ref('')
const creating = ref(false)

onMounted(() => void projectStore.loadProjects())

/** 提交新项目，成功后导航到该项目工作台。 */
async function submitProject() {
  if (!title.value.trim()) {
    projectStore.error = '请填写项目名称'
    return
  }
  creating.value = true
  const project = await projectStore.createProject(title.value, description.value)
  creating.value = false
  if (project) {
    await router.push({ name: 'project-workbench', params: { projectId: project.id } })
  }
}

/** 将 ISO 时间显示为本机可读时间；原始时间仍保留在 API 数据中。 */
function formatTime(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value))
}
</script>

<template>
  <section class="page-heading">
    <div>
      <h1>项目工作台</h1>
      <p>从有权使用的参考视频中提炼抽象创作机制，进入多模型协作、人工审核的原创生产流程。</p>
    </div>
  </section>

  <div class="grid">
    <form class="panel stack" @submit.prevent="submitProject">
      <div>
        <h2>新建项目</h2>
        <p class="muted">新项目将直接进入 LemonFlow V1 正式生产链路。</p>
      </div>
      <label class="field">
        项目名称
        <input v-model="title" maxlength="120" placeholder="例如：都市反转短剧实验" />
      </label>
      <label class="field">
        创作方向（可选）
        <textarea v-model="description" maxlength="2000" placeholder="例如：目标受众、题材边界、原创方向" />
      </label>
      <p v-if="projectStore.error" class="notice error">{{ projectStore.error }}</p>
      <button class="button" type="submit" :disabled="creating">
        {{ creating ? '正在创建…' : '创建并进入工作台' }}
      </button>
    </form>

    <section class="panel stack">
      <div>
        <h2>已有项目</h2>
        <p class="muted">{{ projectStore.loading ? '正在加载…' : `共 ${projectStore.projects.length} 个项目` }}</p>
      </div>
      <p v-if="projectStore.error && projectStore.projects.length" class="notice error">{{ projectStore.error }}</p>
      <div v-if="projectStore.projects.length" class="stack">
        <RouterLink
          v-for="project in projectStore.projects"
          :key="project.id"
          class="card-link"
          :to="{ name: 'project-workbench', params: { projectId: project.id } }"
        >
          <article class="panel project-card">
            <div class="meta-row">
              <strong>{{ project.title }}</strong>
              <span>{{ project.source_video_count ? '已上传素材' : '待上传素材' }}</span>
            </div>
            <p class="muted">{{ project.description || '暂未填写创作方向' }}</p>
            <small class="muted">更新于 {{ formatTime(project.updated_at) }}</small>
          </article>
        </RouterLink>
      </div>
      <p v-else-if="!projectStore.loading" class="muted">还没有项目，先从左侧创建一个。</p>
    </section>
  </div>
</template>
