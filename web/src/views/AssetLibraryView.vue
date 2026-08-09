<script setup lang="ts">
/**
 * Phase 4 资产中心。
 *
 * 页面只负责管理跨项目角色/场景资产的“新增和追加版本”。不提供编辑历史版本按钮，
 * 以免误导制作人认为已经被项目、分镜或视频冻结的资产还能被覆盖。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'

import {
  appendCharacterAssetVersion,
  appendSceneAssetVersion,
  createCharacterAsset,
  createSceneAsset,
  getCharacterAssets,
  getSceneAssets,
} from '@/api/asset-library'
import type { AssetReferenceImage, CharacterAsset, CharacterAssetVersion, SceneAsset, SceneAssetVersion } from '@/types/domain'

const activeTab = ref<'character' | 'scene'>('character')
const loading = ref(false)
const saving = ref(false)
const characterAssets = ref<CharacterAsset[]>([])
const sceneAssets = ref<SceneAsset[]>([])
const versionDialogVisible = ref(false)
const versionTarget = ref<{ kind: 'character'; asset: CharacterAsset; latest: CharacterAssetVersion } | { kind: 'scene'; asset: SceneAsset; latest: SceneAssetVersion } | null>(null)

/** 将四个友好输入框转换为后端稳定的多视图 reference_images 数组。 */
function imagePayload(values: Record<string, string>): AssetReferenceImage[] {
  return Object.entries(values)
    .filter(([, url]) => url.trim())
    .map(([view, url]) => ({ view: view as AssetReferenceImage['view'], url: url.trim() }))
}

const characterForm = reactive({
  name: '', description: '', age: '', gender: '', personality: '', style: '', appearance: '', costume: '',
  front: '', side: '', full_body: '', expression: '',
})
const sceneForm = reactive({
  name: '', description: '', style: '', weather: '', time_of_day: '', location: '', environment: '', mood: '',
  wide: '', detail: '',
})
const characterVersionForm = reactive({
  description: '', age: '', gender: '', personality: '', style: '', appearance: '', costume: '',
  front: '', side: '', full_body: '', expression: '',
})
const sceneVersionForm = reactive({
  description: '', style: '', weather: '', time_of_day: '', location: '', environment: '', mood: '', wide: '', detail: '',
})

const title = computed(() => activeTab.value === 'character' ? '角色资产库' : '场景资产库')
const subtitle = computed(() => activeTab.value === 'character'
  ? '角色可保存正面、侧面、全身和表情多张参考图；每次调整都创建新版本。'
  : '场景可保存风格、天气、时段和多张环境参考图；不同项目可复用已验收版本。')

async function loadAssets() {
  loading.value = true
  try {
    const [characters, scenes] = await Promise.all([getCharacterAssets(), getSceneAssets()])
    characterAssets.value = characters
    sceneAssets.value = scenes
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '资产中心加载失败')
  } finally {
    loading.value = false
  }
}

function clearCharacterForm() { Object.assign(characterForm, { name: '', description: '', age: '', gender: '', personality: '', style: '', appearance: '', costume: '', front: '', side: '', full_body: '', expression: '' }) }
function clearSceneForm() { Object.assign(sceneForm, { name: '', description: '', style: '', weather: '', time_of_day: '', location: '', environment: '', mood: '', wide: '', detail: '' }) }

async function createAsset() {
  saving.value = true
  try {
    if (activeTab.value === 'character') {
      if (!characterForm.name.trim()) throw new Error('请填写角色名称')
      await createCharacterAsset({
        ...characterForm,
        reference_images: imagePayload(characterForm),
      })
      clearCharacterForm()
    } else {
      if (!sceneForm.name.trim()) throw new Error('请填写场景名称')
      await createSceneAsset({
        ...sceneForm,
        reference_images: imagePayload(sceneForm),
      })
      clearSceneForm()
    }
    await loadAssets()
    ElMessage.success('已创建资产首版；以后请通过“新增版本”保留历史记录。')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '创建资产失败')
  } finally {
    saving.value = false
  }
}

function openVersion(asset: CharacterAsset | SceneAsset, kind: 'character' | 'scene') {
  const latest = asset.versions[0]
  if (!latest) return
  if (kind === 'character') {
    const version = latest as CharacterAssetVersion
    Object.assign(characterVersionForm, {
      description: version.description, age: version.age || '', gender: version.gender || '', personality: version.personality || '',
      style: version.style || '', appearance: version.appearance || '', costume: version.costume || '',
      front: version.reference_images.find((item) => item.view === 'front')?.url || '',
      side: version.reference_images.find((item) => item.view === 'side')?.url || '',
      full_body: version.reference_images.find((item) => item.view === 'full_body' || item.view === 'generated')?.url || '',
      expression: version.reference_images.find((item) => item.view === 'expression')?.url || '',
    })
    versionTarget.value = { kind, asset: asset as CharacterAsset, latest: version }
  } else {
    const version = latest as SceneAssetVersion
    Object.assign(sceneVersionForm, {
      description: version.description, style: version.style || '', weather: version.weather || '', time_of_day: version.time_of_day || '',
      location: version.location || '', environment: version.environment || '', mood: version.mood || '',
      wide: version.reference_images.find((item) => item.view === 'wide' || item.view === 'generated')?.url || '',
      detail: version.reference_images.find((item) => item.view === 'detail')?.url || '',
    })
    versionTarget.value = { kind, asset: asset as SceneAsset, latest: version }
  }
  versionDialogVisible.value = true
}

async function appendVersion() {
  if (!versionTarget.value) return
  saving.value = true
  try {
    if (versionTarget.value.kind === 'character') {
      await appendCharacterAssetVersion(versionTarget.value.asset.id, {
        ...characterVersionForm,
        reference_images: imagePayload(characterVersionForm),
      })
    } else {
      await appendSceneAssetVersion(versionTarget.value.asset.id, {
        ...sceneVersionForm,
        reference_images: imagePayload(sceneVersionForm),
      })
    }
    versionDialogVisible.value = false
    await loadAssets()
    ElMessage.success('已新增资产版本，原版本没有被修改。')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '新增版本失败')
  } finally {
    saving.value = false
  }
}

function formatTime(value: string) { return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value)) }
onMounted(() => void loadAssets())
</script>

<template>
  <section class="page-heading">
    <div><h1>资产中心</h1><p>把已验证的角色与场景沉淀为可复用版本，供新项目在人工确认后采用。</p></div>
    <el-button :loading="loading" @click="loadAssets">刷新资产</el-button>
  </section>

  <el-alert type="info" :closable="false" show-icon>
    资产版本一旦创建不会被覆盖。项目采用后仍会经过原有“锁图”审核；工作流、分镜和视频只冻结已选择的版本。
  </el-alert>

  <el-tabs v-model="activeTab" class="asset-tabs">
    <el-tab-pane label="角色资产" name="character" />
    <el-tab-pane label="场景资产" name="scene" />
  </el-tabs>

  <section class="asset-layout">
    <el-card shadow="never" class="create-card">
      <template #header><strong>新建{{ title }}</strong></template>
      <p class="muted">{{ subtitle }}</p>
      <el-form v-if="activeTab === 'character'" label-position="top" @submit.prevent="createAsset">
        <el-form-item label="角色名称" required><el-input v-model="characterForm.name" placeholder="例如：冷静的女律师" /></el-form-item>
        <div class="two-fields"><el-form-item label="年龄"><el-input v-model="characterForm.age" placeholder="例如：30 岁左右" /></el-form-item><el-form-item label="性别"><el-input v-model="characterForm.gender" placeholder="例如：女" /></el-form-item></div>
        <el-form-item label="外貌"><el-input v-model="characterForm.appearance" type="textarea" :rows="2" /></el-form-item>
        <el-form-item label="服装"><el-input v-model="characterForm.costume" type="textarea" :rows="2" /></el-form-item>
        <el-form-item label="性格 / 气质"><el-input v-model="characterForm.personality" type="textarea" :rows="2" /></el-form-item>
        <el-form-item label="视觉风格"><el-input v-model="characterForm.style" placeholder="例如：写实都市短剧" /></el-form-item>
        <el-form-item label="说明"><el-input v-model="characterForm.description" type="textarea" :rows="2" /></el-form-item>
        <el-divider content-position="left">参考图（可选，多张）</el-divider>
        <el-form-item label="正面图 URL"><el-input v-model="characterForm.front" /></el-form-item><el-form-item label="侧面图 URL"><el-input v-model="characterForm.side" /></el-form-item><el-form-item label="全身图 URL"><el-input v-model="characterForm.full_body" /></el-form-item><el-form-item label="表情参考 URL"><el-input v-model="characterForm.expression" /></el-form-item>
        <el-button native-type="submit" type="primary" :loading="saving">创建角色资产 v1</el-button>
      </el-form>
      <el-form v-else label-position="top" @submit.prevent="createAsset">
        <el-form-item label="场景名称" required><el-input v-model="sceneForm.name" placeholder="例如：雨夜城市医院走廊" /></el-form-item>
        <el-form-item label="地点"><el-input v-model="sceneForm.location" /></el-form-item><el-form-item label="环境"><el-input v-model="sceneForm.environment" type="textarea" :rows="2" /></el-form-item><el-form-item label="视觉风格"><el-input v-model="sceneForm.style" /></el-form-item>
        <div class="two-fields"><el-form-item label="天气"><el-input v-model="sceneForm.weather" placeholder="例如：小雨" /></el-form-item><el-form-item label="时段"><el-input v-model="sceneForm.time_of_day" placeholder="例如：夜晚" /></el-form-item></div>
        <el-form-item label="氛围"><el-input v-model="sceneForm.mood" /></el-form-item><el-form-item label="说明"><el-input v-model="sceneForm.description" type="textarea" :rows="2" /></el-form-item>
        <el-divider content-position="left">参考图（可选，多张）</el-divider><el-form-item label="全景图 URL"><el-input v-model="sceneForm.wide" /></el-form-item><el-form-item label="细节图 URL"><el-input v-model="sceneForm.detail" /></el-form-item>
        <el-button native-type="submit" type="primary" :loading="saving">创建场景资产 v1</el-button>
      </el-form>
    </el-card>

    <section class="asset-list" v-loading="loading">
      <el-empty v-if="activeTab === 'character' && !characterAssets.length" description="还没有角色资产" />
      <el-empty v-else-if="activeTab === 'scene' && !sceneAssets.length" description="还没有场景资产" />
      <el-card v-for="asset in activeTab === 'character' ? characterAssets : sceneAssets" :key="asset.id" shadow="hover" class="asset-card">
        <template #header><div class="card-header"><div><strong>{{ asset.name }}</strong><p>{{ asset.description || '暂未填写说明' }}</p></div><el-button size="small" type="primary" plain @click="openVersion(asset, activeTab)">新增版本</el-button></div></template>
        <el-collapse>
          <el-collapse-item v-for="version in asset.versions" :key="version.id" :name="version.id">
            <template #title><span>版本 v{{ version.version }}</span><el-tag size="small" class="version-time">{{ formatTime(version.created_at) }}</el-tag></template>
            <template v-if="activeTab === 'character'">
              <p><b>外貌：</b>{{ (version as CharacterAssetVersion).appearance || '未填写' }}</p><p><b>服装：</b>{{ (version as CharacterAssetVersion).costume || '未填写' }}</p><p><b>性格：</b>{{ (version as CharacterAssetVersion).personality || '未填写' }}</p>
            </template>
            <template v-else>
              <p><b>地点：</b>{{ (version as SceneAssetVersion).location || '未填写' }}</p><p><b>环境：</b>{{ (version as SceneAssetVersion).environment || '未填写' }}</p><p><b>天气 / 时段：</b>{{ (version as SceneAssetVersion).weather || '未填写' }} / {{ (version as SceneAssetVersion).time_of_day || '未填写' }}</p>
            </template>
            <div v-if="version.reference_images.length" class="reference-links"><el-link v-for="image in version.reference_images" :key="`${image.view}-${image.url}`" :href="image.url" target="_blank" type="primary">{{ image.view }}参考图</el-link></div>
          </el-collapse-item>
        </el-collapse>
      </el-card>
    </section>
  </section>

  <el-dialog v-model="versionDialogVisible" :title="`为 ${versionTarget?.asset.name || ''} 新增版本`" width="min(680px, 94vw)">
    <p class="muted">将以当前最新版本为初始值。保存后会形成新的版本，旧版本继续保留给历史项目。</p>
    <el-form v-if="versionTarget?.kind === 'character'" label-position="top"><el-form-item label="说明"><el-input v-model="characterVersionForm.description" type="textarea" /></el-form-item><div class="two-fields"><el-form-item label="年龄"><el-input v-model="characterVersionForm.age" /></el-form-item><el-form-item label="性别"><el-input v-model="characterVersionForm.gender" /></el-form-item></div><el-form-item label="外貌"><el-input v-model="characterVersionForm.appearance" type="textarea" /></el-form-item><el-form-item label="服装"><el-input v-model="characterVersionForm.costume" type="textarea" /></el-form-item><el-form-item label="性格"><el-input v-model="characterVersionForm.personality" type="textarea" /></el-form-item><el-form-item label="风格"><el-input v-model="characterVersionForm.style" /></el-form-item><el-form-item label="正面 / 侧面 / 全身 / 表情参考图"><el-input v-model="characterVersionForm.front" placeholder="正面 URL" /><el-input v-model="characterVersionForm.side" placeholder="侧面 URL" class="top-gap" /><el-input v-model="characterVersionForm.full_body" placeholder="全身 URL" class="top-gap" /><el-input v-model="characterVersionForm.expression" placeholder="表情 URL" class="top-gap" /></el-form-item></el-form>
    <el-form v-else-if="versionTarget" label-position="top"><el-form-item label="说明"><el-input v-model="sceneVersionForm.description" type="textarea" /></el-form-item><el-form-item label="地点"><el-input v-model="sceneVersionForm.location" /></el-form-item><el-form-item label="环境"><el-input v-model="sceneVersionForm.environment" type="textarea" /></el-form-item><el-form-item label="风格"><el-input v-model="sceneVersionForm.style" /></el-form-item><div class="two-fields"><el-form-item label="天气"><el-input v-model="sceneVersionForm.weather" /></el-form-item><el-form-item label="时段"><el-input v-model="sceneVersionForm.time_of_day" /></el-form-item></div><el-form-item label="氛围"><el-input v-model="sceneVersionForm.mood" /></el-form-item><el-form-item label="全景 / 细节参考图"><el-input v-model="sceneVersionForm.wide" placeholder="全景 URL" /><el-input v-model="sceneVersionForm.detail" placeholder="细节 URL" class="top-gap" /></el-form-item></el-form>
    <template #footer><el-button @click="versionDialogVisible = false">取消</el-button><el-button type="primary" :loading="saving" @click="appendVersion">创建新版本</el-button></template>
  </el-dialog>
</template>

<style scoped>
.asset-tabs { margin-top: 18px; }
.asset-layout { display: grid; grid-template-columns: minmax(300px, 410px) minmax(0, 1fr); gap: 18px; align-items: start; }
.asset-list { display: grid; gap: 14px; }
.create-card { position: sticky; top: 18px; }
.muted { color: var(--el-text-color-secondary); font-size: 13px; line-height: 1.65; }
.two-fields { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.card-header { display: flex; justify-content: space-between; gap: 12px; align-items: start; }
.card-header p { margin: 5px 0 0; color: var(--el-text-color-secondary); font-size: 13px; }
.version-time { margin-left: 10px; font-weight: 400; }
.reference-links { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 12px; }
.top-gap { margin-top: 8px; }
@media (max-width: 900px) { .asset-layout { grid-template-columns: 1fr; } .create-card { position: static; } }
@media (max-width: 520px) { .two-fields { grid-template-columns: 1fr; gap: 0; } }
</style>
