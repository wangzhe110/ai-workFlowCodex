<script setup lang="ts">
/** 系统 Prompt 中心：项目的业务视频 Prompt 不在此页编辑。 */
import { computed, onMounted, ref, watch } from 'vue'
import {
  activatePromptTemplateVersion,
  createPromptTemplateDraft,
  getPromptTemplateCatalog,
  previewPromptTemplateRender,
  publishPromptTemplateDraft,
  updatePromptTemplateDraft,
} from '@/api/production'
import type { PromptTemplateCatalog, PromptTemplateRenderPreview, PromptTemplateVersion } from '@/types/domain'

const catalog = ref<PromptTemplateCatalog[]>([])
const selectedKey = ref('')
const selectedVersionId = ref('')
const loading = ref(true)
const saving = ref(false)
const action = ref('')
const error = ref('')
const notice = ref('')
const systemTemplate = ref('')
const userTemplate = ref('')
const changeSummary = ref('')
const previewVariablesText = ref('{}')
const preview = ref<PromptTemplateRenderPreview | null>(null)

const selected = computed(() => catalog.value.find((item) => item.prompt_key === selectedKey.value) || null)
const selectedVersion = computed(() => selected.value?.versions.find((item) => item.id === selectedVersionId.value) || null)
const activeVersion = computed(() => selected.value?.active_version || null)
const isDraft = computed(() => selectedVersion.value?.status === 'DRAFT')
const comparisonVersion = computed(() => {
  if (!selectedVersion.value) return null
  if (activeVersion.value && activeVersion.value.id !== selectedVersion.value.id) return activeVersion.value
  return selected.value?.versions.find((item) => item.id !== selectedVersion.value?.id) || null
})
const invalidTemplateVariables = computed(() => {
  const full = `${systemTemplate.value}\n${userTemplate.value}`
  const values = [...full.matchAll(/\{([^}]*)\}/g)].map((match) => match[1])
  const allowed = new Set(Object.keys(selectedVersion.value?.allowed_variables || {}))
  return values.filter((value) => !/^[A-Za-z_][A-Za-z0-9_]*$/.test(value) || !allowed.has(value))
})
const groupedCatalog = computed(() => {
  const groups = new Map<string, PromptTemplateCatalog[]>()
  for (const item of catalog.value) {
    const group = item.capability === 'video_analysis' ? '视频理解' : item.capability === 'image' ? '图片提示词' : item.capability === 'video' ? '视频提示词' : '文本创作'
    groups.set(group, [...(groups.get(group) || []), item])
  }
  return [...groups.entries()]
})

function shortHash(value: string | undefined | null): string { return value ? `${value.slice(0, 12)}…` : '未记录' }
/**
 * 不引入可执行 diff 库的最小文本差异展示。
 * 这是帮助制作人员核对版本正文的页面能力，不参与任何 Prompt 渲染或模型调用。
 */
function lineDiff(before: string, after: string): string {
  const oldLines = before.split('\n')
  const newLines = after.split('\n')
  const rows: string[] = []
  const total = Math.max(oldLines.length, newLines.length)
  for (let index = 0; index < total; index += 1) {
    const oldLine = oldLines[index]
    const newLine = newLines[index]
    if (oldLine === newLine) rows.push(`  ${oldLine || ''}`)
    else {
      if (oldLine !== undefined) rows.push(`- ${oldLine}`)
      if (newLine !== undefined) rows.push(`+ ${newLine}`)
    }
  }
  return rows.join('\n') || '两个版本的正文相同。'
}
const templateDiff = computed(() => {
  if (!selectedVersion.value || !comparisonVersion.value) return null
  return {
    againstVersion: comparisonVersion.value.version,
    system: lineDiff(comparisonVersion.value.system_template, selectedVersion.value.system_template),
    user: lineDiff(comparisonVersion.value.user_template, selectedVersion.value.user_template),
  }
})
function selectVersion(version: PromptTemplateVersion | null) {
  if (!version) return
  selectedVersionId.value = version.id
  systemTemplate.value = version.system_template
  userTemplate.value = version.user_template
  changeSummary.value = version.change_summary
  preview.value = null
  error.value = ''
}
function selectPrompt(key: string) {
  selectedKey.value = key
  const item = catalog.value.find((row) => row.prompt_key === key)
  selectVersion(item?.versions[0] || item?.active_version || null)
}
async function load(preferredKey?: string, preferredVersionId?: string) {
  loading.value = true
  try {
    catalog.value = await getPromptTemplateCatalog()
    const key = preferredKey && catalog.value.some((item) => item.prompt_key === preferredKey) ? preferredKey : selectedKey.value || catalog.value[0]?.prompt_key || ''
    selectedKey.value = key
    const item = catalog.value.find((row) => row.prompt_key === key)
    selectVersion(item?.versions.find((row) => row.id === preferredVersionId) || item?.versions.find((row) => row.id === selectedVersionId.value) || item?.versions[0] || item?.active_version || null)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : 'Prompt 中心加载失败，请确认后端服务已启动。'
  } finally { loading.value = false }
}
async function copyDraft(source?: PromptTemplateVersion) {
  if (!selected.value) return
  action.value = 'copy'; error.value = ''
  try {
    const draft = await createPromptTemplateDraft(selected.value.prompt_key, source?.id)
    notice.value = `已从 v${source?.version || activeVersion.value?.version} 创建草稿 v${draft.version}。`
    await load(selected.value.prompt_key, draft.id)
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '创建草稿失败，请重试。' } finally { action.value = '' }
}
async function saveDraft() {
  if (!selectedVersion.value || !isDraft.value) return
  if (invalidTemplateVariables.value.length) { error.value = `存在未声明或不合法的变量：${invalidTemplateVariables.value.join('、')}`; return }
  saving.value = true; error.value = ''
  try {
    const result = await updatePromptTemplateDraft(selectedVersion.value.id, { system_template: systemTemplate.value, user_template: userTemplate.value, change_summary: changeSummary.value })
    notice.value = `草稿 v${result.version} 已保存，尚未影响任何新任务。`
    await load(selectedKey.value, result.id)
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '保存草稿失败，请检查变量和安全内容。' } finally { saving.value = false }
}
async function publishDraft() {
  if (!selectedVersion.value || !isDraft.value) return
  action.value = 'publish'; error.value = ''
  try {
    const result = await publishPromptTemplateDraft(selectedVersion.value.id)
    notice.value = `v${result.version} 已发布。发布不会自动切换生产任务，请继续显式启用。`
    await load(selectedKey.value, result.id)
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '发布失败，请先保存并检查变量。' } finally { action.value = '' }
}
async function activate(version: PromptTemplateVersion) {
  if (!selected.value) return
  action.value = `activate:${version.id}`; error.value = ''
  try {
    await activatePromptTemplateVersion(selected.value.prompt_key, version.id)
    notice.value = `已启用 v${version.version}。已创建的运行继续使用各自冻结的历史版本。`
    await load(selected.value.prompt_key, version.id)
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '启用失败；仅已发布且属于当前 Prompt 的版本可以启用。' } finally { action.value = '' }
}
async function previewRender() {
  if (!selected.value || !selectedVersion.value) return
  let variables: Record<string, unknown>
  try {
    const parsed: unknown = JSON.parse(previewVariablesText.value)
    if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('not object')
    variables = parsed as Record<string, unknown>
  } catch { error.value = '预览变量必须是 JSON 对象。页面只渲染，不会调用任何模型。'; return }
  action.value = 'preview'; error.value = ''
  try { preview.value = await previewPromptTemplateRender(selected.value.prompt_key, selectedVersion.value.id, variables); notice.value = '渲染预览已完成：没有调用模型，也没有创建任务。' }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '预览失败，请检查必填变量和安全输入。' } finally { action.value = '' }
}
function insertVariable(name: string) { userTemplate.value += `${userTemplate.value.endsWith('\n') || !userTemplate.value ? '' : '\n'}{${name}}` }
watch(selectedKey, () => { preview.value = null })
onMounted(load)
</script>

<template>
  <section class="page-heading"><div><RouterLink class="muted" to="/model-profiles">← 返回模型中心</RouterLink><h1>Prompt 版本管理</h1><p>管理系统级执行模板。项目内视频 Prompt、分镜 Prompt 等业务结果仍在生产工作台审核，不会被这里的切换覆盖。</p></div></section>
  <p class="notice info">推荐顺序：复制活动版本 → 编辑草稿 → 仅渲染预览 → 发布 → 明确启用。运行中的任务始终使用创建时冻结的版本。</p>
  <p v-if="error" class="notice error">{{ error }}</p><p v-if="notice" class="notice success">{{ notice }}</p>
  <div class="prompt-layout">
    <aside class="panel catalog"><h2>业务操作</h2><p v-if="loading" class="muted">正在读取…</p><template v-for="[group, items] in groupedCatalog" :key="group"><h3>{{ group }}</h3><button v-for="item in items" :key="item.id" class="catalog-item" :class="{ active: item.prompt_key === selectedKey }" @click="selectPrompt(item.prompt_key)"><strong>{{ item.display_name }}</strong><small>{{ item.prompt_key }}</small><span>活动 v{{ item.active_version?.version || '—' }} · 草稿 {{ item.draft_count }}</span></button></template></aside>
    <main v-if="selected" class="stack">
      <section class="panel stack"><div class="meta-row"><div><h2>{{ selected.display_name }}</h2><p class="muted">{{ selected.description }}</p></div><button class="button" :disabled="Boolean(action)" @click="copyDraft(activeVersion || undefined)">{{ action === 'copy' ? '正在复制…' : '复制活动版为草稿' }}</button></div><dl class="definition-grid"><div><dt>Prompt Key</dt><dd><code>{{ selected.prompt_key }}</code></dd></div><div><dt>模型槽位</dt><dd>{{ selected.model_slot_key || '本地任务，不使用模型模板' }}</dd></div><div><dt>输出契约</dt><dd><code>{{ selectedVersion?.output_contract_key || '—' }}</code></dd></div><div><dt>活动哈希</dt><dd><code>{{ shortHash(activeVersion?.content_hash) }}</code></dd></div></dl></section>
      <section class="panel stack"><div class="meta-row"><h2>版本历史</h2><span>{{ selected.versions.length }} 个版本</span></div><div class="version-list"><button v-for="version in selected.versions" :key="version.id" class="version-row" :class="{ active: version.id === selectedVersionId }" @click="selectVersion(version)"><strong>v{{ version.version }}</strong><span>{{ version.status === 'DRAFT' ? '草稿' : version.id === selected.active_version_id ? '当前活动' : '已发布' }}</span><small>{{ shortHash(version.content_hash) }}</small></button></div><div v-if="selectedVersion" class="action-row"><button class="button secondary" :disabled="Boolean(action)" @click="copyDraft(selectedVersion)">从此版本复制草稿</button><button v-if="selectedVersion.status === 'PUBLISHED' && selectedVersion.id !== selected.active_version_id" class="button" :disabled="Boolean(action)" @click="activate(selectedVersion)">{{ action === `activate:${selectedVersion.id}` ? '正在启用…' : '启用/回滚到此版本' }}</button></div></section>
      <section v-if="selectedVersion" class="panel stack"><div class="meta-row"><h2>v{{ selectedVersion.version }} {{ isDraft ? '草稿编辑' : '已发布内容（只读）' }}</h2><span>{{ selectedVersion.status }}</span></div><label class="field">系统 Prompt<textarea v-model="systemTemplate" :readonly="!isDraft" rows="8" maxlength="50000" /></label><label class="field">用户 Prompt<textarea v-model="userTemplate" :readonly="!isDraft" rows="10" maxlength="50000" /></label><label class="field">变更说明<textarea v-model="changeSummary" :readonly="!isDraft" rows="3" maxlength="4000" /></label><p v-if="invalidTemplateVariables.length" class="notice error">未知或不合法变量：{{ invalidTemplateVariables.join('、') }}</p><div v-if="isDraft" class="action-row"><button class="button" :disabled="saving || Boolean(invalidTemplateVariables.length)" @click="saveDraft">{{ saving ? '正在保存…' : '保存草稿' }}</button><button class="button secondary" :disabled="Boolean(action)" @click="publishDraft">{{ action === 'publish' ? '正在发布…' : '发布草稿' }}</button></div></section>
      <section v-if="templateDiff" class="panel stack"><div class="meta-row"><h2>版本文本差异</h2><span>当前 v{{ selectedVersion?.version }} 对比 v{{ templateDiff.againstVersion }}</span></div><p class="muted">“−”为对比版本内容，“+”为当前版本内容。差异仅在浏览器中显示，不会调用模型。</p><h3>系统 Prompt</h3><pre class="diff-content">{{ templateDiff.system }}</pre><h3>用户 Prompt</h3><pre class="diff-content">{{ templateDiff.user }}</pre></section>
      <section v-if="selectedVersion" class="panel stack"><h2>允许变量与本地渲染预览</h2><p class="muted">变量由服务端严格校验；只允许 <code>{simple_name}</code>。预览不会调用模型、不会创建任务。</p><div class="variable-list"><button v-for="(info, name) in selectedVersion.allowed_variables" :key="name" class="variable-chip" :disabled="!isDraft" @click="insertVariable(name)"><code>{ {{ name }} }</code> · {{ info.description }}{{ info.required ? '（必填）' : '' }}</button></div><label class="field">安全测试变量（JSON 对象）<textarea v-model="previewVariablesText" rows="8" class="code-input" spellcheck="false" /></label><button class="button secondary" :disabled="Boolean(action)" @click="previewRender">{{ action === 'preview' ? '正在渲染…' : '仅渲染预览' }}</button><template v-if="preview"><p class="muted">渲染哈希：<code>{{ shortHash(preview.rendered_prompt_hash) }}</code>。变量快照仅显示摘要。</p><h3>系统渲染结果</h3><pre>{{ preview.rendered_system_template }}</pre><h3>用户渲染结果</h3><pre>{{ preview.rendered_user_template }}</pre></template></section>
    </main>
  </div>
</template>

<style scoped>
.prompt-layout { display: grid; grid-template-columns: minmax(230px, .8fr) minmax(0, 2fr); gap: 20px; align-items: start; margin-top: 18px; }.catalog { position: sticky; top: 16px; max-height: calc(100vh - 40px); overflow: auto; }.catalog h3 { color: #64748b; font-size: 13px; margin: 18px 0 8px; }.catalog-item, .version-row { width: 100%; text-align: left; border: 1px solid #dbe3ef; background: #fff; border-radius: 8px; padding: 10px; margin: 6px 0; cursor: pointer; display: grid; gap: 3px; color: inherit; }.catalog-item.active, .version-row.active { border-color: #2563eb; background: #eff6ff; }.catalog-item small, .catalog-item span, .version-row small { color: #64748b; overflow-wrap: anywhere; }.definition-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin: 0; }.definition-grid div { border: 1px solid #e2e8f0; padding: 10px; border-radius: 8px; }.definition-grid dt { color: #64748b; font-size: 12px; }.definition-grid dd { margin: 5px 0 0; overflow-wrap: anywhere; }.version-list, .action-row, .variable-list { display: flex; gap: 8px; flex-wrap: wrap; }.version-row { width: auto; min-width: 155px; }.version-row span { font-size: 12px; color: #2563eb; }.variable-chip { border: 1px solid #cbd5e1; background: #f8fafc; border-radius: 999px; padding: 6px 9px; cursor: pointer; }.variable-chip:disabled { cursor: default; }textarea, pre, .code-input { box-sizing: border-box; width: 100%; padding: 10px; border: 1px solid #cbd5e1; border-radius: 8px; background: #f8fafc; font: 12px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }.diff-content { border-left: 3px solid #64748b; }.field textarea[readonly] { background: #f1f5f9; color: #475569; }.code-input { resize: vertical; }@media (max-width: 850px) { .prompt-layout { grid-template-columns: 1fr; }.catalog { position: static; max-height: none; }.definition-grid { grid-template-columns: 1fr; } }
</style>
