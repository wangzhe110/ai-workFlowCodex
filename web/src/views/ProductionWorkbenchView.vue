<script setup lang="ts">
/**
 * LemonFlow V1 生产台。
 *
 * 新项目只从这里进入主生产链路。页面只展示和提交人工审核决定；模型生成结果必须由
 * 后端 Worker 写入后才会出现，不能通过浏览器伪造“已完成”。旧选题/故事包/单镜图页面
 * 仍保留在历史兼容路由中，但不在此页作为前置步骤出现。
 */
import { computed, onMounted, ref, watch } from 'vue'

import {
  approveV1VideoClip,
  getCharacterReferenceImages,
  getProductionState,
  getReferenceAnalyses,
  getSceneReferenceImages,
  getShotKeyframes,
  getStoryProposals,
  getV1VideoClips,
  lockCharacterReferenceImage,
  lockReferenceAnalysis,
  lockSceneReferenceImage,
  lockShotKeyframe,
  rejectReferenceAnalysis,
  rejectV1VideoClip,
  selectStoryProposal,
  startProductionRun,
} from '@/api/production'
import { getProject, getWorkflowRun, uploadSourceVideo } from '@/api/projects'
import type {
  CharacterReferenceImageV1,
  ProductionStage,
  ProductionState,
  ProjectDetail,
  ReferenceAnalysis,
  SceneReferenceImageV1,
  ShotKeyframeV1,
  StoryProposalV1,
  VideoClipV1,
} from '@/types/domain'

const props = defineProps<{ projectId: string }>()

const project = ref<ProjectDetail | null>(null)
const state = ref<ProductionState | null>(null)
const analyses = ref<ReferenceAnalysis[]>([])
const stories = ref<StoryProposalV1[]>([])
const characterImages = ref<CharacterReferenceImageV1[]>([])
const sceneImages = ref<SceneReferenceImageV1[]>([])
const keyframes = ref<ShotKeyframeV1[]>([])
const videoClips = ref<VideoClipV1[]>([])
const selectedFile = ref<File | null>(null)
const reviewerLabel = ref('制作人')
const reviewNote = ref('')
const qualityScore = ref<number | null>(null)
const loading = ref(false)
const uploading = ref(false)
const actionId = ref('')
const generatingKey = ref('')
const error = ref('')
const backgroundNotice = ref('')

/** 正式主链路固定顺序；只用于进度展示，真正的放行规则始终由后端状态机判断。 */
const stages: Array<{ key: ProductionStage; label: string; description: string }> = [
  { key: 'REFERENCE_ANALYSIS', label: '参考视频分析', description: '上传有授权的视频，生成抽象创作分析。' },
  { key: 'ANALYSIS_REVIEW', label: '确认创作简报', description: '审核结构、开头、爆点和场景分析后锁定。' },
  { key: 'STORY_GENERATION', label: '多模型故事生成', description: '多个导演/编剧模型并行创作原创方案。' },
  { key: 'STORY_REVIEW', label: '选择故事', description: '人工选择一份原创故事方案。' },
  { key: 'CHARACTER_ASSETS', label: '角色资产', description: '生成并锁定角色参考图。' },
  { key: 'SCENE_ASSETS', label: '场景资产', description: '生成并锁定场景参考图。' },
  { key: 'DIRECTOR_PLANNING', label: 'AI 导演分镜', description: '只使用锁定的角色和场景资产规划镜头。' },
  { key: 'SHOT_KEYFRAMES', label: '分镜关键帧', description: '生成并锁定每个镜头的关键画面。' },
  { key: 'VIDEO_GENERATION', label: 'Seedance 视频生成', description: '使用锁定资产生成独立视频片段。' },
  { key: 'VIDEO_REVIEW', label: '审核视频片段', description: '确认角色、场景、动作和连续性。' },
  { key: 'FINAL_EXPORT', label: '合成成片', description: '仅合成审核通过的视频片段。' },
]

const currentStageIndex = computed(() => stages.findIndex((item) => item.key === state.value?.active_stage))
const hasSourceVideo = computed(() => Boolean(project.value?.assets.some((item) => item.kind === 'SOURCE_VIDEO')))
function reviewPayload() {
  return {
    reviewer_label: reviewerLabel.value.trim() || '制作人',
    note: reviewNote.value.trim() || undefined,
    quality_score: qualityScore.value ?? undefined,
  }
}

/** 聚合读取所有 V1 面板数据；空列表代表模型还未产出结果，不被视为页面错误。 */
async function loadWorkbench() {
  loading.value = true
  error.value = ''
  try {
    const [nextProject, nextState, nextAnalyses, nextStories, nextCharacters, nextScenes, nextKeyframes, nextClips] = await Promise.all([
      getProject(props.projectId),
      getProductionState(props.projectId),
      getReferenceAnalyses(props.projectId),
      getStoryProposals(props.projectId),
      getCharacterReferenceImages(props.projectId),
      getSceneReferenceImages(props.projectId),
      getShotKeyframes(props.projectId),
      getV1VideoClips(props.projectId),
    ])
    project.value = nextProject
    state.value = nextState
    analyses.value = nextAnalyses
    stories.value = nextStories
    characterImages.value = nextCharacters
    sceneImages.value = nextScenes
    keyframes.value = nextKeyframes
    videoClips.value = nextClips
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '加载生产台失败，请刷新后重试'
  } finally {
    loading.value = false
  }
}

function selectVideo(event: Event) {
  const input = event.target as HTMLInputElement
  selectedFile.value = input.files?.[0] ?? null
}

async function submitUpload() {
  if (!selectedFile.value) {
    error.value = '请选择一个视频文件'
    return
  }
  uploading.value = true
  error.value = ''
  try {
    await uploadSourceVideo(props.projectId, selectedFile.value)
    selectedFile.value = null
    await loadWorkbench()
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '上传失败，请重试'
  } finally {
    uploading.value = false
  }
}

/** 审核写操作统一刷新全部面板，避免前端自行猜测下一个阶段。 */
async function review(id: string, operation: () => Promise<unknown>) {
  actionId.value = id
  error.value = ''
  try {
    await operation()
    reviewNote.value = ''
    qualityScore.value = null
    await loadWorkbench()
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '审核操作失败，请重试'
  } finally {
    actionId.value = ''
  }
}

/**
 * 创建后台任务后只做短时状态刷新。真实视频可能耗时数分钟，页面停止等待不代表
 * 后台失败；刷新页面会继续读取同一个 WorkflowRun 的子任务状态。
 */
async function startGeneration(runKey: string, label: string, shotPlanIds: string[] = []) {
  const actionKey = shotPlanIds.length ? `${runKey}:${shotPlanIds.join(',')}` : runKey
  generatingKey.value = actionKey
  error.value = ''
  backgroundNotice.value = ''
  try {
    const created = await startProductionRun(props.projectId, runKey, shotPlanIds)
    let latest = created
    for (let attempt = 0; attempt < 15 && ['PENDING', 'RUNNING'].includes(latest.status); attempt += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 2000))
      latest = await getWorkflowRun(created.id)
    }
    if (latest.status === 'FAILED') throw new Error(latest.steps.find((item) => item.error_message)?.error_message || `${label}没有完成`)
    if (['PENDING', 'RUNNING'].includes(latest.status)) {
      backgroundNotice.value = `${label}仍在后台执行。关闭或刷新本页不会取消任务，稍后刷新即可查看每个镜头的状态。`
    }
    await loadWorkbench()
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : `${label}创建失败，请检查模型配置后重试`
  } finally {
    generatingKey.value = ''
  }
}

/** 角色/场景资产分别由“文字设计”和“参考图生成”两个模型槽位完成，页面为小白合并成一个按钮。 */
async function startAssetGeneration(kind: 'character' | 'scene') {
  const label = kind === 'character' ? '角色资产' : '场景资产'
  await startGeneration(`${kind}_design`, `${label}文字设计`)
  if (!error.value) await startGeneration(`${kind}_images`, `${label}参考图生成`)
}

function formatJson(value: Record<string, unknown> | Array<Record<string, unknown>>): string {
  return JSON.stringify(value, null, 2)
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value))
}

function safeExternalUrl(value: string | null): string | null {
  return value && /^(https?:|data:image\/)/.test(value) ? value : null
}

onMounted(() => void loadWorkbench())
watch(() => props.projectId, () => void loadWorkbench())
</script>

<template>
  <section v-if="loading && !project" class="panel">正在加载 V1 生产台…</section>
  <section v-else-if="!project" class="panel stack">
    <p class="notice error">{{ error || '未找到项目' }}</p>
    <RouterLink class="button secondary" to="/">返回项目列表</RouterLink>
  </section>

  <template v-else>
    <section class="page-heading">
      <div>
        <RouterLink class="muted" to="/">← 返回项目列表</RouterLink>
        <h1>{{ project.title }}</h1>
        <p>{{ project.description || '暂未填写创作方向' }}</p>
      </div>
      <span v-if="state" class="status RUNNING">当前：{{ stages[currentStageIndex]?.label || state.active_stage }}</span>
    </section>

    <p v-if="error" class="notice error">{{ error }}</p>
    <p v-if="backgroundNotice" class="notice info">{{ backgroundNotice }}</p>

    <section class="panel stack">
      <div class="meta-row"><h2>V1 生产链路</h2><span class="muted">每一步都需要完成或人工确认后才会放行</span></div>
      <ol class="production-steps">
        <li v-for="(item, index) in stages" :key="item.key" :class="{ active: index === currentStageIndex, done: index < currentStageIndex }">
          <strong>{{ index + 1 }}. {{ item.label }}</strong>
          <span>{{ item.description }}</span>
        </li>
      </ol>
    </section>

    <section class="grid workbench-grid">
      <section class="panel stack">
        <div>
          <h2>第一步：上传授权参考视频</h2>
          <p class="muted">系统只提炼结构、节奏与情绪机制，不复制原视频人物、台词或画面。</p>
        </div>
        <label class="field">视频文件
          <input accept="video/mp4,video/quicktime,video/x-matroska,video/webm" type="file" @change="selectVideo" />
        </label>
        <button class="button" :disabled="uploading || !selectedFile" @click="submitUpload">
          {{ uploading ? '正在上传…' : '上传参考视频' }}
        </button>
        <div v-if="project.assets.length" class="stack">
          <strong>已上传素材</strong>
          <div v-for="asset in project.assets" :key="asset.id" class="meta-row">
            <span>{{ asset.original_filename }}</span><span>{{ Math.ceil(asset.byte_size / 1024 / 1024) }} MB</span>
          </div>
        </div>
      </section>

      <section class="panel stack">
        <h2>当前等待什么？</h2>
        <template v-if="state?.active_stage === 'REFERENCE_ANALYSIS'">
          <p v-if="!hasSourceVideo" class="notice info">先上传参考视频；上传后，Gemini 视频分析任务会在此处创建。</p>
          <template v-else><p class="notice info">参考视频已就绪。分析模型会生成“脚本结构、爆款开头、爆款元素、场景分析和创作简报”，完成后自动进入人工确认。</p><button class="button" :disabled="Boolean(generatingKey)" @click="startGeneration('reference_analysis', '参考视频分析')">{{ generatingKey === 'reference_analysis' ? '正在分析…' : '开始视频分析' }}</button></template>
        </template>
        <template v-else-if="state?.active_stage === 'STORY_GENERATION'">
          <p class="notice info">创作简报已经锁定。多个编剧模型会并行生成原创故事候选，不能复刻原故事或人物。</p>
          <button class="button" :disabled="Boolean(generatingKey)" @click="startGeneration('story_generation', '原创故事生成')">{{ generatingKey === 'story_generation' ? '正在生成…' : '并行生成原创故事方案' }}</button>
        </template>
        <template v-else-if="state?.active_stage === 'CHARACTER_ASSETS'">
          <p class="notice info">先由角色设计模型生成角色卡，再由图片模型生成可人工锁定的角色参考图。</p>
          <button class="button" :disabled="Boolean(generatingKey)" @click="startAssetGeneration('character')">{{ generatingKey ? '正在生成…' : '生成角色资产与参考图' }}</button>
        </template>
        <template v-else-if="state?.active_stage === 'SCENE_ASSETS'">
          <p class="notice info">先由场景设计模型生成场景卡，再由图片模型生成可人工锁定的场景参考图。</p>
          <button class="button" :disabled="Boolean(generatingKey)" @click="startAssetGeneration('scene')">{{ generatingKey ? '正在生成…' : '生成场景资产与参考图' }}</button>
        </template>
        <template v-else-if="state?.active_stage === 'DIRECTOR_PLANNING'">
          <p class="notice info">角色图和场景图均已锁定。AI 导演只会引用这些固定资产生成分镜规划。</p>
          <button class="button" :disabled="Boolean(generatingKey)" @click="startGeneration('director_plan', 'AI 导演分镜')">{{ generatingKey === 'director_plan' ? '正在规划…' : '生成 AI 导演分镜' }}</button>
        </template>
        <template v-else-if="state?.active_stage === 'SHOT_KEYFRAMES'">
          <p class="notice info">导演分镜已就绪。图片模型会按每个分镜生成关键画面，待人工选择后再进入视频。</p>
          <button class="button" :disabled="Boolean(generatingKey)" @click="startGeneration('shot_keyframes', '分镜关键帧')">{{ generatingKey === 'shot_keyframes' ? '正在生成…' : '生成分镜关键帧' }}</button>
        </template>
        <template v-else-if="state?.active_stage === 'VIDEO_GENERATION'">
          <p class="notice info">全部关键帧已锁定。Seedance 将使用锁定的角色图、场景图、关键帧和动作描述生成视频片段。</p>
          <button class="button" :disabled="Boolean(generatingKey)" @click="startGeneration('video_generation', '视频片段')">{{ generatingKey === 'video_generation' ? '正在创建任务…' : '生成未通过的视频片段' }}</button>
        </template>
        <template v-else-if="state?.active_stage === 'FINAL_EXPORT'">
          <p class="notice info">目标视频片段均已审核通过，可以创建一版新的完整成片。</p>
          <button class="button" :disabled="Boolean(generatingKey)" @click="startGeneration('final_compose', '完整成片')">{{ generatingKey === 'final_compose' ? '正在合成…' : '合成完整成片' }}</button>
        </template>
        <p v-else class="muted">请在下方完成当前阶段的人工审核或版本锁定。</p>
        <RouterLink class="button secondary" to="/model-profiles">查看模型配置与测试记录</RouterLink>
        <RouterLink class="button secondary" :to="`/projects/${project.id}/trace`">查看本项目的模型与版本记录</RouterLink>
      </section>
    </section>

    <section class="panel stack review-panel">
      <div>
        <h2>人工审核备注</h2>
        <p class="muted">确认、锁定、选择、通过或驳回都会保存审核记录。没有登录前，先使用一个易识别的审核人名称。</p>
      </div>
      <div class="review-fields">
        <label class="field">审核人<input v-model="reviewerLabel" maxlength="120" placeholder="例如：制作人小王" /></label>
        <label class="field">质量评分（可选）
          <select v-model.number="qualityScore"><option :value="null">暂不评分</option><option v-for="score in 10" :key="score" :value="score">{{ score }} 分</option></select>
        </label>
        <label class="field">备注（可选）<input v-model="reviewNote" maxlength="2000" placeholder="例如：角色外观符合要求" /></label>
      </div>
    </section>

    <section v-if="analyses.length" class="panel stack review-panel">
      <div class="meta-row"><h2>分析结果与创作简报</h2><span>{{ analyses.length }} 个版本</span></div>
      <article v-for="analysis in analyses" :key="analysis.id" class="asset-card stack">
        <div class="meta-row"><strong>分析版本 {{ analysis.version }}</strong><span class="status" :class="analysis.review_status === 'LOCKED' ? 'SUCCEEDED' : 'PENDING'">{{ analysis.review_status }}</span></div>
        <div class="analysis-grid">
          <details open><summary>视频脚本结构</summary><pre>{{ formatJson(analysis.video_script_structure) }}</pre></details>
          <details><summary>爆款开头</summary><pre>{{ formatJson(analysis.opening_analysis) }}</pre></details>
          <details><summary>爆款元素</summary><pre>{{ formatJson(analysis.viral_elements) }}</pre></details>
          <details><summary>场景分析</summary><pre>{{ formatJson(analysis.scene_analysis) }}</pre></details>
          <details open><summary>创作简报</summary><pre>{{ formatJson(analysis.creative_brief) }}</pre></details>
        </div>
        <div v-if="analysis.review_status === 'PENDING_REVIEW' && state?.active_stage === 'ANALYSIS_REVIEW'" class="action-row">
          <button class="button" :disabled="Boolean(actionId)" @click="review(`analysis-lock-${analysis.id}`, () => lockReferenceAnalysis(analysis.id, reviewPayload()))">
            {{ actionId === `analysis-lock-${analysis.id}` ? '正在锁定…' : '确认并锁定创作简报' }}
          </button>
          <button class="button danger" :disabled="Boolean(actionId)" @click="review(`analysis-reject-${analysis.id}`, () => rejectReferenceAnalysis(analysis.id, reviewPayload()))">驳回并重新分析</button>
        </div>
      </article>
    </section>

    <section v-if="stories.length" class="panel stack review-panel">
      <div class="meta-row"><h2>多模型原创故事方案</h2><span>{{ stories.length }} 个候选</span></div>
      <article v-for="story in stories" :key="story.id" class="asset-card stack">
        <div class="meta-row"><strong>方案 {{ story.candidate_number }}</strong><span class="status" :class="story.status === 'SELECTED' ? 'SUCCEEDED' : 'PENDING'">{{ story.status }}</span></div>
        <pre>{{ formatJson(story.content) }}</pre>
        <button v-if="story.status === 'CANDIDATE' && state?.active_stage === 'STORY_REVIEW'" class="button" :disabled="Boolean(actionId)" @click="review(`story-${story.id}`, () => selectStoryProposal(story.id, reviewPayload()))">
          {{ actionId === `story-${story.id}` ? '正在选择…' : '选择这份原创故事' }}
        </button>
      </article>
    </section>

    <section v-if="characterImages.length" class="panel stack review-panel">
      <div class="meta-row"><h2>角色参考图锁定</h2><span>{{ characterImages.length }} 个版本</span></div>
      <article v-for="image in characterImages" :key="image.id" class="asset-card asset-preview">
        <a v-if="safeExternalUrl(image.image_url)" :href="safeExternalUrl(image.image_url) || undefined" target="_blank" rel="noreferrer"><img :src="safeExternalUrl(image.image_url) || undefined" :alt="`${image.character_name} 角色参考图`" /></a>
        <div class="stack"><div class="meta-row"><strong>{{ image.character_name }} · v{{ image.version }}</strong><span class="status" :class="image.review_status === 'LOCKED' ? 'SUCCEEDED' : 'PENDING'">{{ image.review_status }}</span></div><small class="muted">{{ image.character_code }} · {{ formatTime(image.created_at) }}</small>
          <button v-if="image.review_status === 'PENDING_REVIEW' && state?.active_stage === 'CHARACTER_ASSETS'" class="button" :disabled="Boolean(actionId)" @click="review(`character-${image.id}`, () => lockCharacterReferenceImage(image.id, reviewPayload()))">{{ actionId === `character-${image.id}` ? '正在锁定…' : '锁定此角色图版本' }}</button>
        </div>
      </article>
    </section>

    <section v-if="sceneImages.length" class="panel stack review-panel">
      <div class="meta-row"><h2>场景参考图锁定</h2><span>{{ sceneImages.length }} 个版本</span></div>
      <article v-for="image in sceneImages" :key="image.id" class="asset-card asset-preview">
        <a v-if="safeExternalUrl(image.image_url)" :href="safeExternalUrl(image.image_url) || undefined" target="_blank" rel="noreferrer"><img :src="safeExternalUrl(image.image_url) || undefined" :alt="`${image.scene_name} 场景参考图`" /></a>
        <div class="stack"><div class="meta-row"><strong>{{ image.scene_name }} · v{{ image.version }}</strong><span class="status" :class="image.review_status === 'LOCKED' ? 'SUCCEEDED' : 'PENDING'">{{ image.review_status }}</span></div><small class="muted">{{ image.scene_code }} · {{ formatTime(image.created_at) }}</small>
          <button v-if="image.review_status === 'PENDING_REVIEW' && state?.active_stage === 'SCENE_ASSETS'" class="button" :disabled="Boolean(actionId)" @click="review(`scene-${image.id}`, () => lockSceneReferenceImage(image.id, reviewPayload()))">{{ actionId === `scene-${image.id}` ? '正在锁定…' : '锁定此场景图版本' }}</button>
        </div>
      </article>
    </section>

    <section v-if="keyframes.length" class="panel stack review-panel">
      <div class="meta-row"><h2>分镜关键帧锁定</h2><span>{{ keyframes.length }} 个版本</span></div>
      <article v-for="frame in keyframes" :key="frame.id" class="asset-card asset-preview">
        <a v-if="safeExternalUrl(frame.image_url)" :href="safeExternalUrl(frame.image_url) || undefined" target="_blank" rel="noreferrer"><img :src="safeExternalUrl(frame.image_url) || undefined" :alt="`镜头 ${frame.shot_number} 关键帧`" /></a>
        <div class="stack"><div class="meta-row"><strong>镜头 {{ frame.shot_number }} · v{{ frame.version }}</strong><span class="status" :class="frame.review_status === 'LOCKED' ? 'SUCCEEDED' : 'PENDING'">{{ frame.review_status }}</span></div>
          <button v-if="frame.review_status === 'PENDING_REVIEW' && state?.active_stage === 'SHOT_KEYFRAMES'" class="button" :disabled="Boolean(actionId)" @click="review(`keyframe-${frame.id}`, () => lockShotKeyframe(frame.id, reviewPayload()))">{{ actionId === `keyframe-${frame.id}` ? '正在锁定…' : '锁定此关键帧版本' }}</button>
        </div>
      </article>
    </section>

    <section v-if="videoClips.length" class="panel stack review-panel">
      <div class="meta-row"><h2>视频片段审核</h2><span>{{ videoClips.length }} 个版本</span></div>
      <article v-for="clip in videoClips" :key="clip.id" class="asset-card stack">
        <div class="meta-row"><strong>镜头 {{ clip.shot_number }} · 视频 v{{ clip.version }}<template v-if="clip.is_current">（当前采用）</template></strong><span class="status" :class="clip.review_status === 'APPROVED' ? 'SUCCEEDED' : 'PENDING'">{{ clip.review_status || '等待生成' }}</span></div>
        <a v-if="safeExternalUrl(clip.video_url)" class="button secondary" :href="safeExternalUrl(clip.video_url) || undefined" target="_blank" rel="noreferrer">打开视频预览</a>
        <small class="muted">生成任务：{{ clip.task_status || 'PENDING' }}<template v-if="clip.provider_task_id"> · 供应商任务号：{{ clip.provider_task_id }}</template></small>
        <small v-if="clip.review_note" class="muted">上次审核备注：{{ clip.review_note }}</small>
        <div v-if="clip.review_status === 'PENDING_REVIEW' && state?.active_stage === 'VIDEO_REVIEW'" class="action-row"><button class="button" :disabled="Boolean(actionId)" @click="review(`video-approve-${clip.id}`, () => approveV1VideoClip(clip.id, reviewPayload()))">{{ actionId === `video-approve-${clip.id}` ? '正在确认…' : '通过此视频片段' }}</button><button class="button danger" :disabled="Boolean(actionId)" @click="review(`video-reject-${clip.id}`, () => rejectV1VideoClip(clip.id, reviewPayload()))">驳回并生成新版本</button></div>
        <button v-if="clip.review_status === 'REJECTED' && state?.active_stage === 'VIDEO_GENERATION'" class="button" :disabled="Boolean(generatingKey)" @click="startGeneration('video_generation', `镜头 ${clip.shot_number} 重做`, [clip.shot_plan_id])">{{ generatingKey ? '正在创建任务…' : '仅重做此镜头' }}</button>
      </article>
    </section>
  </template>
</template>

<style scoped>
.workbench-grid { margin-top: 20px; }
.production-steps { display: grid; grid-template-columns: repeat(auto-fit, minmax(174px, 1fr)); gap: 10px; padding: 0; margin: 0; list-style: none; }
.production-steps li { display: grid; gap: 6px; padding: 12px; border: 1px solid #dbe3ef; border-radius: 10px; color: #64748b; font-size: 13px; }
.production-steps li strong { color: #475569; }
.production-steps li.active { border-color: #2563eb; background: #eff6ff; }
.production-steps li.active strong { color: #1d4ed8; }
.production-steps li.done { border-color: #86efac; background: #f0fdf4; }
.production-steps li.done strong { color: #166534; }
.review-panel { margin-top: 20px; }
.review-fields { display: grid; grid-template-columns: minmax(160px, .5fr) minmax(240px, 1.5fr); gap: 12px; }
.asset-card { border: 1px solid #dbe3ef; border-radius: 10px; padding: 16px; }
.asset-preview { display: grid; grid-template-columns: 140px 1fr; gap: 16px; align-items: start; }
.asset-preview img { display: block; width: 140px; height: 180px; object-fit: cover; border-radius: 8px; background: #f1f5f9; }
.action-row { display: flex; flex-wrap: wrap; gap: 10px; }
.analysis-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 10px; }
details { border: 1px solid #e2e8f0; border-radius: 8px; padding: 10px; }
summary { cursor: pointer; font-weight: 600; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; margin: 10px 0 0; padding: 10px; border-radius: 7px; background: #f8fafc; font: 12px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace; }
@media (max-width: 640px) { .review-fields, .asset-preview { grid-template-columns: 1fr; } .asset-preview img { width: 100%; height: auto; max-height: 320px; } }
</style>
