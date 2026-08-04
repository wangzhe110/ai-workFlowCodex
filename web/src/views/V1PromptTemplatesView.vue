<script setup lang="ts">
/**
 * LemonFlow V1 Prompt 模板版本管理页。
 *
 * 所有改动先创建草稿，再由制作负责人明确激活；已用于生产的内容已经随
 * ModelInvocation 冻结，因此本页绝不提供“直接编辑旧版本”的入口。
 */
import { computed, onMounted, ref, watch } from 'vue'

import { activatePromptTemplate, archivePromptTemplate, createPromptTemplate, getPromptTemplates } from '@/api/production'
import type { PromptTemplate } from '@/types/domain'

const templates = ref<PromptTemplate[]>([])
const loading = ref(true)
const saving = ref(false)
const actionId = ref('')
const error = ref('')
const notice = ref('')

const taskType = ref('VIDEO_ANALYSIS')
const name = ref('V1 视频分析提示词')
const content = ref('只提炼结构、开头机制、爆款元素与场景作用；不得复刻人物、台词、画面或音乐。')
const variablesSchemaText = ref('{\n  "type": "object",\n  "properties": {}\n}')

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

const defaults: Record<string, { name: string; content: string }> = {
  VIDEO_ANALYSIS: { name: 'V1 视频分析提示词', content: '只提炼结构、开头机制、爆款元素与场景作用；不得复刻人物、台词、画面或音乐。' },
  STORY_GENERATE: { name: 'V1 原创故事提示词', content: '保留已锁定创作简报中的节奏和情绪机制，创作全新人设、关系和剧情；不得复制参考故事。' },
  CHARACTER_DESIGN: { name: 'V1 角色设计提示词', content: '根据已选原创故事设计稳定角色资产：年龄、外貌、服装与性格；不使用参考视频人物。' },
  SCENE_DESIGN: { name: 'V1 场景设计提示词', content: '根据已选原创故事设计稳定场景资产：地点、环境、视觉风格与氛围。' },
  DIRECTOR_PLAN: { name: 'V1 导演分镜提示词', content: '只引用已经锁定的角色图和场景图，输出动作、机位、时长和视频动作描述。' },
  IMAGE_GENERATE: { name: 'V1 图片资产提示词', content: '保持输入角色和场景资产一致，生成原创视觉画面；不得复用参考视频的具体画面。' },
  VIDEO_GENERATE: { name: 'V1 视频片段提示词', content: '根据锁定角色图、场景图、关键帧与动作描述生成连续视频片段。' },
  FINAL_COMPOSE: { name: 'V1 成片合成提示词', content: '按人工审核通过的视频片段顺序合成，不插入未经审核的片段。' },
}

const currentTemplates = computed(() => templates.value.filter((item) => item.task_type === taskType.value))

function applyTaskDefaults(nextTask: string) {
  const preset = defaults[nextTask]
  if (!preset) return
  name.value = preset.name
  content.value = preset.content
  variablesSchemaText.value = '{\n  "type": "object",\n  "properties": {}\n}'
  error.value = ''
  notice.value = ''
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    templates.value = await getPromptTemplates()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : 'Prompt 模板加载失败，请确认后端服务已启动。'
  } finally {
    loading.value = false
  }
}

function parseVariablesSchema(): Record<string, unknown> | null {
  try {
    const value: unknown = JSON.parse(variablesSchemaText.value)
    if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error('not-object')
    return value as Record<string, unknown>
  } catch {
    error.value = '变量说明必须是有效的 JSON 对象。一般保持默认内容即可。'
    return null
  }
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value))
}

async function createDraft() {
  const variablesSchema = parseVariablesSchema()
  if (!variablesSchema) return
  if (!name.value.trim() || !content.value.trim()) {
    error.value = '请填写模板名称和模板内容。'
    return
  }
  saving.value = true
  error.value = ''
  notice.value = ''
  try {
    const created = await createPromptTemplate({
      task_type: taskType.value,
      name: name.value.trim(),
      content: content.value.trim(),
      variables_schema: variablesSchema,
    })
    notice.value = `已保存为草稿 v${created.version}。确认小样本内容后，再点击“启用这一版”。`
    await load()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '保存 Prompt 草稿失败，请重试。'
  } finally {
    saving.value = false
  }
}

async function activate(item: PromptTemplate) {
  actionId.value = item.id
  error.value = ''
  notice.value = ''
  try {
    await activatePromptTemplate(item.id)
    notice.value = `已启用「${item.name}」v${item.version}。同一用途的原生效版已归档，正在运行的项目不受影响。`
    await load()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '启用 Prompt 失败，请重试。'
  } finally {
    actionId.value = ''
  }
}

async function archive(item: PromptTemplate) {
  actionId.value = item.id
  error.value = ''
  notice.value = ''
  try {
    await archivePromptTemplate(item.id)
    notice.value = `已归档「${item.name}」v${item.version}，历史模型调用仍可完整回溯。`
    await load()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '归档 Prompt 失败，请重试。'
  } finally {
    actionId.value = ''
  }
}

watch(taskType, applyTaskDefaults)
onMounted(load)
</script>

<template>
  <section class="page-heading">
    <div>
      <RouterLink class="muted" to="/model-profiles">← 返回模型中心</RouterLink>
      <h1>Prompt 模板版本</h1>
      <p>修改提示词会影响生成结果，所以每次改动都创建新版本；系统不会覆盖历史，也不会自动启用草稿。</p>
    </div>
  </section>

  <section class="panel stack">
    <h2>安全使用顺序</h2>
    <ol class="setup-steps">
      <li>选择要优化的生产用途，修改默认提示词后先保存为草稿。</li>
      <li>用小项目验证结果；旧项目仍使用自己已冻结的 Prompt 版本。</li>
      <li>确认效果更好后，再点击“启用这一版”。每个用途同时只会有一个生效 Prompt。</li>
    </ol>
    <p class="notice info">不要把 API Key、客户隐私或没有授权的原视频台词填进 Prompt。一般情况下，“变量说明”保持默认内容即可。</p>
  </section>

  <div class="grid prompt-grid">
    <form class="panel stack" @submit.prevent="createDraft">
      <h2>新建 Prompt 草稿</h2>
      <label class="field">用于哪一步？
        <select v-model="taskType"><option v-for="(label, key) in taskLabels" :key="key" :value="key">{{ label }}</option></select>
      </label>
      <label class="field">模板名称<input v-model="name" maxlength="160" placeholder="例如：V1 原创故事提示词" /></label>
      <label class="field">模板内容<textarea v-model="content" rows="12" maxlength="50000" /></label>
      <details>
        <summary>高级：变量说明（通常不用改）</summary>
        <p class="muted">这是给后续技术扩展读取的 JSON Schema。若不确定，请保留默认值。</p>
        <textarea v-model="variablesSchemaText" rows="7" class="code-input" spellcheck="false" />
      </details>
      <p v-if="error" class="notice error">{{ error }}</p>
      <p v-if="notice" class="notice success">{{ notice }}</p>
      <button class="button" :disabled="saving">{{ saving ? '正在保存…' : '保存为新草稿版本' }}</button>
    </form>

    <section class="panel stack">
      <div class="meta-row"><h2>{{ taskLabels[taskType] }}的历史版本</h2><span>{{ currentTemplates.length }} 个版本</span></div>
      <p v-if="loading" class="muted">正在读取…</p>
      <p v-else-if="!currentTemplates.length" class="muted">当前用途还没有自定义 Prompt；系统会使用 V1 默认生效模板。</p>
      <article v-for="item in currentTemplates" :key="item.id" class="prompt-card stack">
        <div class="meta-row">
          <strong>{{ item.name }} · v{{ item.version }}</strong>
          <span class="status" :class="item.status === 'ACTIVE' ? 'SUCCEEDED' : item.status === 'DRAFT' ? 'PENDING' : 'muted'">{{ item.status === 'ACTIVE' ? '当前生效' : item.status === 'DRAFT' ? '草稿' : '已归档' }}</span>
        </div>
        <pre>{{ item.content }}</pre>
        <small class="muted">更新：{{ formatTime(item.updated_at) }}</small>
        <div class="action-row">
          <button v-if="item.status !== 'ACTIVE'" class="button" :disabled="Boolean(actionId)" @click="activate(item)">{{ actionId === item.id ? '正在启用…' : '启用这一版' }}</button>
          <button v-if="item.status === 'DRAFT'" class="button danger" :disabled="Boolean(actionId)" @click="archive(item)">{{ actionId === item.id ? '正在归档…' : '归档草稿' }}</button>
        </div>
      </article>
    </section>
  </div>
</template>

<style scoped>
.prompt-grid { margin-top: 20px; }
.prompt-card { border: 1px solid #dbe3ef; border-radius: 10px; padding: 14px; }
.action-row { display: flex; gap: 10px; flex-wrap: wrap; }
pre, .code-input { width: 100%; box-sizing: border-box; white-space: pre-wrap; overflow-wrap: anywhere; margin: 0; padding: 10px; border: 1px solid #dbe3ef; border-radius: 7px; background: #f8fafc; font: 12px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace; }
.code-input { resize: vertical; }
details { border: 1px solid #e2e8f0; border-radius: 8px; padding: 10px; }
summary { cursor: pointer; font-weight: 600; }
</style>
