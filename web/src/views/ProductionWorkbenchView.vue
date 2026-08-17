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
  adoptCharacterAssetVersion,
  adoptSceneAssetVersion,
  getCurrentDirectorPlan,
  getCharacterReferenceImages,
  getCommerceCreativeBatches,
  getCommerceReferenceIntakes,
  getCommerceOutlines,
  getCommerceProductionAssets,
  getCommerceStoryRuns,
  getProductionState,
  getReferenceAnalyses,
  getSceneReferenceImages,
  getShotKeyframes,
  getStoryProposals,
  getV1VideoClips,
  lockCharacterReferenceImage,
  confirmCommerceProduct,
  lockReferenceAnalysis,
  lockCommerceCharacterDesign,
  lockCommerceProductionImage,
  lockCommerceSceneDesign,
  lockCommerceStoryboard,
  lockSceneReferenceImage,
  lockShotKeyframe,
  rejectReferenceAnalysis,
  rejectV1VideoClip,
  selectStoryProposal,
  selectCommerceCreativeIdea,
  confirmCommerceStage,
  reviewCommerceVideoClip,
  resumeCommerceProviderTask,
  startCommerceProduction,
  startProductionRun,
} from '@/api/production'
import { apiDownloadUrl } from '@/api/http'
import { getCharacterAssets, getSceneAssets } from '@/api/asset-library'
import { deleteSourceVideo, getProject, getWorkflowRun, uploadSourceVideo } from '@/api/projects'
import type {
  CharacterReferenceImageV1,
  CharacterAsset,
  CommerceCreativeBatch,
  CommerceOutline,
  CommerceProductionAssets,
  CommerceReferenceIntake,
  CommerceStoryRun,
  CommerceVideoClip,
  DirectorPlanV1,
  ProductionStage,
  ProductionState,
  ProjectDetail,
  ReferenceAnalysis,
  SceneReferenceImageV1,
  SceneAsset,
  ShotKeyframeV1,
  StoryProposalV1,
  VideoClipV1,
} from '@/types/domain'

const props = defineProps<{ projectId: string }>()

const project = ref<ProjectDetail | null>(null)
const state = ref<ProductionState | null>(null)
const analyses = ref<ReferenceAnalysis[]>([])
const stories = ref<StoryProposalV1[]>([])
const commerceIntakes = ref<CommerceReferenceIntake[]>([])
const commerceCreativeBatches = ref<CommerceCreativeBatch[]>([])
const commerceStoryRuns = ref<CommerceStoryRun[]>([])
const selectedCommerceStoryRunId = ref('')
const commerceOutlines = ref<CommerceOutline[]>([])
const commerceAssets = ref<CommerceProductionAssets | null>(null)
const characterImages = ref<CharacterReferenceImageV1[]>([])
const sceneImages = ref<SceneReferenceImageV1[]>([])
const keyframes = ref<ShotKeyframeV1[]>([])
const videoClips = ref<VideoClipV1[]>([])
const directorPlan = ref<DirectorPlanV1 | null>(null)
const characterLibraryAssets = ref<CharacterAsset[]>([])
const sceneLibraryAssets = ref<SceneAsset[]>([])
const selectedCharacterLibraryVersion = ref<Record<string, string>>({})
const selectedSceneLibraryVersion = ref<Record<string, string>>({})
const selectedFiles = ref<File[]>([])
const selectedSourceAssetId = ref('')
const reviewerLabel = ref('制作人')
const reviewNote = ref('')
const qualityScore = ref<number | null>(null)
const loading = ref(false)
const uploading = ref(false)
const deletingSourceAssetId = ref('')
const actionId = ref('')
const generatingKey = ref('')
const recoveringClipId = ref('')
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
const sourceVideos = computed(() => project.value?.assets.filter((item) => item.kind === 'SOURCE_VIDEO') ?? [])
const hasSourceVideo = computed(() => sourceVideos.value.length > 0)
const latestCommerceIntake = computed(() => commerceIntakes.value[0] ?? null)
const latestSuccessfulCreativeBatch = computed(() => commerceCreativeBatches.value.find((item) => item.status === 'SUCCEEDED') ?? null)
const commerceProductReady = computed(() => latestCommerceIntake.value?.product_status === 'CONFIRMED' && Boolean(latestCommerceIntake.value.product_frozen_at))
/** Slice 1 可能保留多个历史运行；默认最近一条，但制作人可切换查看，不会混用资产。 */
const activeCommerceStoryRun = computed(() => (
  commerceStoryRuns.value.find((item) => item.id === selectedCommerceStoryRunId.value)
  ?? commerceStoryRuns.value[0]
  ?? null
))
const activeCommerceStoryboard = computed(() => commerceAssets.value?.storyboards.find((item) => item.status === 'LOCKED') ?? null)
/**
 * 页面只负责把后端前置条件提前说明给制作人；真正的放行仍由 StoryRun 服务端用冻结
 * 版本校验，因此手工改 DOM、刷新页面或多个标签页都不能跳过任何审核闸门。
 */
const hasLockedCommerceOutline = computed(() => commerceOutlines.value.some((item) => item.status === 'LOCKED'))
const hasLockedCommerceCharacter = computed(() => commerceAssets.value?.character_designs.some((item) => item.status === 'LOCKED') ?? false)
const hasLockedCommerceScene = computed(() => commerceAssets.value?.scene_designs.some((item) => item.status === 'LOCKED') ?? false)
const hasLockedCommerceCharacterImages = computed(() => commerceAssets.value?.character_images.some((item) => item.status === 'LOCKED') ?? false)
const hasLockedCommerceSceneImages = computed(() => commerceAssets.value?.scene_images.some((item) => item.status === 'LOCKED') ?? false)
const commerceShots = computed<Array<Record<string, unknown>>>(() => {
  const content = activeCommerceStoryboard.value?.content
  return Array.isArray(content?.shots) ? content.shots.filter((shot): shot is Record<string, unknown> => Boolean(shot && typeof shot === 'object')) : []
})
function hasLockedKeyframe(shotId: string): boolean {
  return commerceAssets.value?.keyframes.some((item) => item.logical_id === shotId && item.status === 'LOCKED') ?? false
}
function hasLockedVideoPrompt(shotId: string): boolean {
  return commerceAssets.value?.video_prompts.some((item) => item.shot_id === shotId && item.status === 'LOCKED') ?? false
}
const allCommerceShotsApproved = computed(() => commerceShots.value.length > 0 && commerceShots.value.every((shot) => {
  const shotId = String(shot.shot_id || '')
  const versions = (commerceAssets.value?.clips ?? [])
    .filter((item) => item.shot_id === shotId)
    .sort((left, right) => right.version - left.version)
  return versions[0]?.status === 'APPROVED'
}))
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
    const [nextProject, nextState, nextAnalyses, nextStories, nextCommerceIntakes, nextCommerceCreativeBatches, nextCommerceStoryRuns, nextCharacters, nextScenes, nextDirectorPlan, nextKeyframes, nextClips, nextCharacterLibraryAssets, nextSceneLibraryAssets] = await Promise.all([
      getProject(props.projectId),
      getProductionState(props.projectId),
      getReferenceAnalyses(props.projectId),
      getStoryProposals(props.projectId),
      getCommerceReferenceIntakes(props.projectId),
      getCommerceCreativeBatches(props.projectId),
      getCommerceStoryRuns(props.projectId),
      getCharacterReferenceImages(props.projectId),
      getSceneReferenceImages(props.projectId),
      getCurrentDirectorPlan(props.projectId),
      getShotKeyframes(props.projectId),
      getV1VideoClips(props.projectId),
      getCharacterAssets(),
      getSceneAssets(),
    ])
    project.value = nextProject
    if (!nextProject.assets.some((asset) => asset.id === selectedSourceAssetId.value)) {
      selectedSourceAssetId.value = ''
    }
    state.value = nextState
    analyses.value = nextAnalyses
    stories.value = nextStories
    commerceIntakes.value = nextCommerceIntakes
    commerceCreativeBatches.value = nextCommerceCreativeBatches
    commerceStoryRuns.value = nextCommerceStoryRuns
    if (!nextCommerceStoryRuns.some((item) => item.id === selectedCommerceStoryRunId.value)) {
      selectedCommerceStoryRunId.value = nextCommerceStoryRuns[0]?.id ?? ''
    }
    const currentStoryRun = nextCommerceStoryRuns.find((item) => item.id === selectedCommerceStoryRunId.value)
    if (currentStoryRun) {
      const [nextOutlines, nextCommerceAssets] = await Promise.all([
        getCommerceOutlines(currentStoryRun.id),
        getCommerceProductionAssets(currentStoryRun.id),
      ])
      commerceOutlines.value = nextOutlines
      commerceAssets.value = nextCommerceAssets
    } else {
      commerceOutlines.value = []
      commerceAssets.value = null
    }
    characterImages.value = nextCharacters
    sceneImages.value = nextScenes
    directorPlan.value = nextDirectorPlan
    keyframes.value = nextKeyframes
    videoClips.value = nextClips
    characterLibraryAssets.value = nextCharacterLibraryAssets
    sceneLibraryAssets.value = nextSceneLibraryAssets
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '加载生产台失败，请刷新后重试'
  } finally {
    loading.value = false
  }
}

/** 切换时只重新读取该 StoryRun 的冻结资产；不会改变任何项目或工作流状态。 */
async function selectCommerceStoryRun() {
  const storyRun = activeCommerceStoryRun.value
  if (!storyRun) {
    commerceOutlines.value = []
    commerceAssets.value = null
    return
  }
  loading.value = true
  error.value = ''
  try {
    const [nextOutlines, nextAssets] = await Promise.all([
      getCommerceOutlines(storyRun.id),
      getCommerceProductionAssets(storyRun.id),
    ])
    commerceOutlines.value = nextOutlines
    commerceAssets.value = nextAssets
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '切换 StoryRun 失败，请刷新后重试'
  } finally {
    loading.value = false
  }
}

function selectVideo(event: Event) {
  const input = event.target as HTMLInputElement
  selectedFiles.value = Array.from(input.files ?? [])
}

async function submitUpload() {
  if (!selectedFiles.value.length) {
    error.value = '请选择至少一个视频文件'
    return
  }
  uploading.value = true
  error.value = ''
  try {
    // 上传只创建 media_asset；绝不在这里隐式创建或扣费执行分析任务。
    for (const file of selectedFiles.value) {
      await uploadSourceVideo(props.projectId, file)
    }
    selectedFiles.value = []
    await loadWorkbench()
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '上传失败，请重试'
  } finally {
    uploading.value = false
  }
}

/** V1 一次参考分析只使用一条视频，用户可在待分析列表中随时改选。 */
function toggleSourceAsset(assetId: string, event: Event) {
  const input = event.target as HTMLInputElement
  selectedSourceAssetId.value = input.checked ? assetId : ''
}

/** 仅允许删除从未被冻结为分析输入的待分析素材。 */
async function removeSourceVideo(assetId: string) {
  if (!window.confirm('确定删除这条待分析视频吗？删除后无法恢复。')) return
  deletingSourceAssetId.value = assetId
  error.value = ''
  try {
    await deleteSourceVideo(props.projectId, assetId)
    if (selectedSourceAssetId.value === assetId) selectedSourceAssetId.value = ''
    await loadWorkbench()
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '删除参考视频失败，请重试'
  } finally {
    deletingSourceAssetId.value = ''
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
async function startGeneration(
  runKey: string,
  label: string,
  shotPlanIds: string[] = [],
  sourceAssetId?: string,
) {
  const actionKey = shotPlanIds.length ? `${runKey}:${shotPlanIds.join(',')}` : runKey
  generatingKey.value = actionKey
  error.value = ''
  backgroundNotice.value = ''
  try {
    const created = await startProductionRun(props.projectId, runKey, shotPlanIds, sourceAssetId)
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

async function startReferenceAnalysis() {
  if (!selectedSourceAssetId.value) {
    error.value = '请先在待分析视频列表中勾选一条参考视频'
    return
  }
  await startGeneration('reference_analysis', '参考视频分析', [], selectedSourceAssetId.value)
}

/** Slice 1 的十创意使用与 V1 相同的 Worker、模型槽位和 Prompt 冻结边界。 */
async function startCommerceCreativeGeneration() {
  await startGeneration('commerce_creative_generation', '10 个带货故事创意')
}

async function confirmProductDraft(intake: CommerceReferenceIntake) {
  await review(`commerce-product-${intake.id}`, () => confirmCommerceProduct(intake.id, reviewPayload()))
}

async function chooseCommerceIdea(ideaId: string) {
  await review(`commerce-idea-${ideaId}`, async () => {
    const selection = await selectCommerceCreativeIdea(ideaId, { ...reviewPayload(), mode: 'STEPWISE' })
    backgroundNotice.value = `已创建带货 StoryRun。下一步请在 Commerce 工作流中启动故事大纲：${selection.story_run_id}`
  })
}

/** Slice 2 操作永远经由 StoryRun，不读取项目级“最新商品/大纲”。 */
async function startCommerceOperation(operation: string, label: string, targetId?: string, retry = false) {
  const storyRun = activeCommerceStoryRun.value
  if (!storyRun) {
    error.value = '请先从固定 10 个创意中选择一个，创建带货 StoryRun'
    return
  }
  const key = `commerce:${operation}:${targetId || 'all'}:${retry ? 'retry' : 'new'}`
  generatingKey.value = key
  error.value = ''
  backgroundNotice.value = ''
  try {
    const run = await startCommerceProduction(storyRun.id, operation, { target_id: targetId, retry })
    let latest = run
    for (let attempt = 0; attempt < 15 && ['PENDING', 'RUNNING'].includes(latest.status); attempt += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 2000))
      latest = await getWorkflowRun(run.id)
    }
    if (latest.status === 'FAILED') throw new Error(latest.steps.find((item) => item.error_message)?.error_message || `${label}没有完成`)
    if (['PENDING', 'RUNNING'].includes(latest.status)) backgroundNotice.value = `${label}正在后台执行；刷新页面不会取消任务。`
    await loadWorkbench()
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : `${label}创建失败，请检查模型配置后重试`
  } finally {
    generatingKey.value = ''
  }
}

async function confirmCommerceOutline() {
  const storyRun = activeCommerceStoryRun.value
  if (!storyRun) return
  await review('commerce-outline-confirm', () => confirmCommerceStage(storyRun.id, 'OUTLINE', reviewPayload()))
}

async function lockCommerceVersion(kind: 'character' | 'scene' | 'storyboard', versionId: string) {
  const storyRun = activeCommerceStoryRun.value
  if (!storyRun) return
  const operations = {
    character: () => lockCommerceCharacterDesign(storyRun.id, versionId, reviewPayload()),
    scene: () => lockCommerceSceneDesign(storyRun.id, versionId, reviewPayload()),
    storyboard: () => lockCommerceStoryboard(storyRun.id, versionId, reviewPayload()),
  }
  await review(`commerce-${kind}-lock-${versionId}`, operations[kind])
}

async function lockCommerceImage(kind: 'CHARACTER' | 'SCENE' | 'KEYFRAME', imageId: string) {
  const storyRun = activeCommerceStoryRun.value
  if (!storyRun) return
  await review(`commerce-${kind}-image-lock-${imageId}`, () => lockCommerceProductionImage(storyRun.id, kind, imageId, reviewPayload()))
}

async function reviewCommerceClip(clip: CommerceVideoClip, decision: 'APPROVED' | 'REJECTED') {
  const storyRun = activeCommerceStoryRun.value
  if (!storyRun) return
  await review(`commerce-clip-${decision}-${clip.id}`, () => reviewCommerceVideoClip(storyRun.id, clip.id, decision, reviewPayload()))
}

/**
 * 只请求后端的“恢复已有任务”入口。浏览器不持有、也不传入供应商任务号；后端会冻结
 * 原片段并只查询同一 task ID，因此不会重新提交 Seedance 创建请求。
 */
async function resumeCommerceClip(clip: CommerceVideoClip) {
  const storyRun = activeCommerceStoryRun.value
  if (!storyRun || !clip.can_resume_provider_task) return
  recoveringClipId.value = clip.id
  error.value = ''
  backgroundNotice.value = '将继续查询原供应商任务，不会重新生成视频，也不会重复提交付费任务。'
  try {
    const run = await resumeCommerceProviderTask(storyRun.id, clip.id)
    let latest = run
    // 复用生产台已有的短时轮询策略；前端停止等待不改变后台任务状态。
    for (let attempt = 0; attempt < 15 && ['PENDING', 'RUNNING'].includes(latest.status); attempt += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 2000))
      latest = await getWorkflowRun(run.id)
    }
    if (latest.status === 'FAILED') {
      throw new Error(latest.steps.find((item) => item.error_message)?.error_message || '恢复原供应商任务未完成')
    }
    if (['PENDING', 'RUNNING'].includes(latest.status)) {
      backgroundNotice.value = '恢复任务仍在后台查询原供应商结果。刷新页面不会取消任务，也不会再次提交视频生成。'
    }
    await loadWorkbench()
  } catch (caught) {
    error.value = caught instanceof Error
      ? caught.message
      : '供应商任务已经创建，但查询或下载时发生临时网络错误。可以恢复原任务，不会重复扣费。'
    // 即使恢复任务再次失败，也读取源片段的最新状态，让后端决定恢复按钮是否继续可用。
    await loadWorkbench()
  } finally {
    recoveringClipId.value = ''
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

/**
 * 只允许 API 返回的 HTTPS 媒体或本机受控生成目录。拒绝 data:、任意本机路径和
 * 供应商临时 URL 以外的协议，避免页面把工作流快照误当作可加载资源。
 */
function safeMediaUrl(value: string | null): string | null {
  if (!value) return null
  if (value.startsWith('/media/generated/')) return apiDownloadUrl(value)
  try {
    const parsed = new URL(value)
    const sensitiveQuery = [...parsed.searchParams.keys()].some((key) => {
      const normalized = key.toLowerCase()
      return ['signature', 'sig', 'token', 'access_token', 'credential', 'expires', 'policy'].includes(normalized)
        || normalized.startsWith('x-amz-')
    })
    return parsed.protocol === 'https:' && !sensitiveQuery ? value : null
  } catch {
    return null
  }
}

/** 本地交付的成片只有受控下载/预览 API；S3 则直接给 HTTPS 地址。两种交付方式都
 * 可以在浏览器内播放，且不会把 mock:// 当作真实媒体 URL。 */
function safeFinalVideoUrl(outputUrl: string | null, downloadUrl: string | null): string | null {
  const candidate = outputUrl || downloadUrl
  if (!candidate) return null
  if (candidate.startsWith('/api/')) return apiDownloadUrl(candidate)
  return safeMediaUrl(candidate)
}

function clipStatusLabel(status: string): string {
  return ({ PENDING: '等待处理', RUNNING: '生成中', SUCCEEDED: '生成成功', APPROVED: '审核通过', REJECTED: '已驳回', FAILED: '生成失败', STALE: '已失效' } as Record<string, string>)[status] || status
}

function shortenedProviderTaskId(value: string | null): string {
  if (!value) return '未保存'
  return value.length > 18 ? `${value.slice(0, 12)}…${value.slice(-5)}` : value
}

function formatDuration(durationMs: number | null): string {
  return durationMs === null ? '时长待获取' : `${(durationMs / 1000).toFixed(2)} 秒`
}

function formatBytes(value: number | null): string {
  if (value === null) return '文件大小待获取'
  return value >= 1024 * 1024 ? `${(value / 1024 / 1024).toFixed(2)} MB` : `${Math.ceil(value / 1024)} KB`
}

function mediaText(metadata: Record<string, unknown>, key: string, fallback = '待获取'): string {
  const value = metadata[key]
  return typeof value === 'string' || typeof value === 'number' ? String(value) : fallback
}

const characterLibraryVersions = computed(() => characterLibraryAssets.value.flatMap((asset) => asset.versions.map((version) => ({
  id: version.id, label: `${asset.name} · v${version.version}`,
}))))
const sceneLibraryVersions = computed(() => sceneLibraryAssets.value.flatMap((asset) => asset.versions.map((version) => ({
  id: version.id, label: `${asset.name} · v${version.version}`,
}))))

/** 资产库采用不会直接锁图，仍回到当前审核卡片由制作人确认，维持 V1 审核闸门。 */
async function adoptLibraryCharacter(image: CharacterReferenceImageV1) {
  const versionId = selectedCharacterLibraryVersion.value[image.character_id]
  if (!versionId) return
  await review(`character-adopt-${image.character_id}`, async () => {
    await adoptCharacterAssetVersion(props.projectId, image.character_id, versionId)
  })
}

async function adoptLibraryScene(image: SceneReferenceImageV1) {
  const versionId = selectedSceneLibraryVersion.value[image.scene_id]
  if (!versionId) return
  await review(`scene-adopt-${image.scene_id}`, async () => {
    await adoptSceneAssetVersion(props.projectId, image.scene_id, versionId)
  })
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
          <input accept="video/mp4,video/quicktime,video/x-matroska,video/webm" type="file" multiple @change="selectVideo" />
        </label>
        <small v-if="selectedFiles.length" class="muted">已选择 {{ selectedFiles.length }} 个文件，上传仅保存到待分析列表，不会调用模型。</small>
        <button class="button" :disabled="uploading || !selectedFiles.length" @click="submitUpload">
          {{ uploading ? '正在上传…' : `上传 ${selectedFiles.length || ''} 个参考视频` }}
        </button>
        <div v-if="sourceVideos.length" class="stack">
          <strong>待分析视频（一次只能勾选一条）</strong>
          <label v-for="asset in sourceVideos" :key="asset.id" class="source-video-row">
            <span><input type="checkbox" :checked="selectedSourceAssetId === asset.id" @change="toggleSourceAsset(asset.id, $event)" /> {{ asset.original_filename }}</span>
            <span class="muted">{{ Math.ceil(asset.byte_size / 1024 / 1024) }} MB</span>
            <button type="button" class="button danger compact" :disabled="Boolean(deletingSourceAssetId)" @click.prevent="removeSourceVideo(asset.id)">{{ deletingSourceAssetId === asset.id ? '正在删除…' : '删除' }}</button>
          </label>
        </div>
      </section>

      <section class="panel stack">
        <h2>当前等待什么？</h2>
        <template v-if="state?.active_stage === 'REFERENCE_ANALYSIS'">
          <p v-if="!hasSourceVideo" class="notice info">先上传参考视频。上传只保存素材，不会自动调用 Gemini 或其他模型。</p>
          <template v-else><p class="notice info">请在左侧待分析视频列表中勾选一条后，再开始分析。分析模型会生成“脚本结构、爆款开头、爆款元素、场景分析和创作简报”。</p><button class="button" :disabled="Boolean(generatingKey) || !selectedSourceAssetId" @click="startReferenceAnalysis">{{ generatingKey === 'reference_analysis' ? '正在分析…' : '开始分析已勾选视频' }}</button></template>
        </template>
        <template v-else-if="state?.active_stage === 'STORY_GENERATION'">
          <template v-if="latestCommerceIntake">
            <p class="notice info">本项目已进入带货短剧主链：先确认商品，再用锁定脚本、商品、模型与 Prompt 生成固定 10 个创意。</p>
            <button class="button" :disabled="Boolean(generatingKey) || !commerceProductReady" @click="startCommerceCreativeGeneration">{{ generatingKey === 'commerce_creative_generation' ? '正在生成…' : '生成固定 10 个带货创意' }}</button>
            <p v-if="!commerceProductReady" class="muted">请先在下方“商品确认”区块确认并冻结商品版本。</p>
          </template>
          <template v-else>
            <p class="notice info">创作简报已经锁定。多个编剧模型会并行生成原创故事候选，不能复刻原故事或人物。</p>
            <button class="button" :disabled="Boolean(generatingKey)" @click="startGeneration('story_generation', '原创故事生成')">{{ generatingKey === 'story_generation' ? '正在生成…' : '并行生成原创故事方案' }}</button>
          </template>
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

    <section v-if="commerceIntakes.length" class="panel stack review-panel">
      <div class="meta-row"><h2>带货商品确认</h2><span>自动从视频分析生成 · {{ commerceIntakes.length }} 个版本</span></div>
      <p class="muted">商品草稿不会自动补造功效、包装或用法。确认后将冻结具体产品版本，所有后续创意和 StoryRun 都只能引用该版本。</p>
      <article v-for="intake in commerceIntakes" :key="intake.id" class="asset-card stack">
        <div class="meta-row"><strong>脚本分析版本 {{ intake.script_analysis_version_id }}</strong><span class="status" :class="intake.product_status === 'CONFIRMED' ? 'SUCCEEDED' : 'PENDING'">{{ intake.product_status === 'CONFIRMED' ? '商品已确认并冻结' : '商品草稿待确认' }}</span></div>
        <details><summary>商品草稿与分析来源</summary><pre>{{ formatJson(intake.input_snapshot) }}</pre></details>
        <button v-if="intake.product_status === 'DRAFT' && state?.active_stage === 'STORY_GENERATION'" class="button" :disabled="Boolean(actionId)" @click="confirmProductDraft(intake)">{{ actionId === `commerce-product-${intake.id}` ? '正在确认…' : '确认并冻结这个商品版本' }}</button>
      </article>
    </section>

    <section v-if="latestSuccessfulCreativeBatch" class="panel stack review-panel">
      <div class="meta-row"><h2>固定 10 个带货故事创意</h2><span>批次 {{ latestSuccessfulCreativeBatch.batch_number }} · 使用已冻结脚本、商品、模型和 Prompt</span></div>
      <p class="muted">页面只突出最新成功批次；旧批次仍保留在数据库中用于追溯。选择后会创建现有 Commerce StoryRun，继续生成故事大纲和商品融入方案。</p>
      <article v-for="idea in latestSuccessfulCreativeBatch.ideas" :key="idea.id" class="asset-card stack">
        <div class="meta-row"><strong>创意 {{ idea.candidate_number }}</strong><span class="status" :class="idea.status === 'SELECTED' ? 'SUCCEEDED' : 'PENDING'">{{ idea.status }}</span></div>
        <pre>{{ formatJson(idea.content) }}</pre>
        <button v-if="idea.status === 'CANDIDATE' && state?.active_stage === 'STORY_REVIEW'" class="button" :disabled="Boolean(actionId)" @click="chooseCommerceIdea(idea.id)">{{ actionId === `commerce-idea-${idea.id}` ? '正在创建 StoryRun…' : '选择这个创意并进入故事大纲' }}</button>
      </article>
      <button v-if="commerceProductReady && state?.active_stage === 'STORY_REVIEW'" class="button secondary" :disabled="Boolean(generatingKey)" @click="startCommerceCreativeGeneration">重新生成一批 10 个创意（保留历史）</button>
    </section>

    <!--
      Slice 2 的唯一入口：它只在 Slice 1 选定创意创建的 StoryRun 上继续。这里不复用
      项目级“当前角色/当前场景”指针，避免旧 V1 资产或另一条带货运行混入本次样片。
    -->
    <section v-if="activeCommerceStoryRun" class="panel stack review-panel commerce-production">
      <div class="meta-row">
        <div><h2>带货短剧生产线</h2><p class="muted">StoryRun {{ activeCommerceStoryRun.id }} · 商品冻结版本 {{ activeCommerceStoryRun.product_asset_version_id }}</p></div>
        <span class="status" :class="activeCommerceStoryRun.current_status === 'COMPLETED' ? 'SUCCEEDED' : 'RUNNING'">{{ activeCommerceStoryRun.current_stage }} · {{ activeCommerceStoryRun.current_status }}</span>
      </div>
      <label v-if="commerceStoryRuns.length > 1" class="field story-run-picker">查看哪一条 StoryRun
        <select v-model="selectedCommerceStoryRunId" @change="selectCommerceStoryRun">
          <option v-for="run in commerceStoryRuns" :key="run.id" :value="run.id">
            第 {{ run.run_number }} 次运行 · {{ run.current_stage }} · {{ run.current_status }}
          </option>
        </select>
      </label>
      <p v-if="activeCommerceStoryRun.blocked_reason" class="notice info">当前闸门：{{ activeCommerceStoryRun.blocked_reason }}</p>
      <p v-if="activeCommerceStoryRun.latest_error" class="notice error">最近工作流错误：{{ activeCommerceStoryRun.latest_error }}</p>

      <article class="asset-card stack">
        <div class="meta-row"><strong>0. 大纲与商品融入方案</strong><span>{{ commerceOutlines.length }} 个版本</span></div>
        <template v-if="commerceOutlines.length">
          <div v-for="outline in commerceOutlines" :key="outline.id" class="sub-card stack">
            <div class="meta-row"><strong>v{{ outline.version }} · {{ outline.title }}</strong><span class="status" :class="outline.status === 'LOCKED' ? 'SUCCEEDED' : 'PENDING'">{{ outline.status }}</span></div>
            <p>{{ outline.premise }}</p>
            <details><summary>故事节拍与商品融入方案</summary><pre>{{ formatJson({ story_beats: outline.story_beats, product_placement_strategy: outline.product_placement_strategy }) }}</pre></details>
            <button v-if="outline.status !== 'LOCKED'" class="button" :disabled="Boolean(actionId)" @click="confirmCommerceOutline">确认并锁定大纲与商品融入方案</button>
          </div>
        </template>
        <p v-else class="muted">选择创意后，Commerce 主工作流会生成大纲；完成后请先在这里确认。</p>
      </article>

      <article class="asset-card stack">
        <div class="meta-row"><strong>1. 角色设定</strong><span>{{ commerceAssets?.character_designs.length || 0 }} 个版本</span></div>
        <p class="muted">角色设定冻结大纲、商品、创意、脚本分析以及本次模型和 Prompt；锁定后只能重生新版。</p>
        <p v-if="!hasLockedCommerceOutline" class="notice info">请先确认并锁定故事大纲与商品融入方案。</p>
        <button class="button" :disabled="Boolean(generatingKey) || !hasLockedCommerceOutline" @click="startCommerceOperation('CHARACTER_DESIGN', '角色设定')">生成角色设定新版本</button>
        <div v-for="version in commerceAssets?.character_designs || []" :key="version.id" class="sub-card stack">
          <div class="meta-row"><strong>角色设定 v{{ version.version }}</strong><span class="status" :class="version.status === 'LOCKED' ? 'SUCCEEDED' : 'PENDING'">{{ version.status }}</span></div>
          <details><summary>查看结构化角色资料</summary><pre>{{ formatJson(version.content) }}</pre></details>
          <button v-if="version.status === 'READY'" class="button" :disabled="Boolean(actionId)" @click="lockCommerceVersion('character', version.id)">确认并锁定角色设定</button>
        </div>
      </article>

      <article class="asset-card stack">
        <div class="meta-row"><strong>2. 场景设定</strong><span>{{ commerceAssets?.scene_designs.length || 0 }} 个版本</span></div>
        <p class="muted">场景只引用锁定角色、已确认大纲、商品融入方案和冻结商品版本。</p>
        <p v-if="!hasLockedCommerceCharacter" class="notice info">请先确认并锁定角色设定。</p>
        <button class="button" :disabled="Boolean(generatingKey) || !hasLockedCommerceCharacter" @click="startCommerceOperation('SCENE_DESIGN', '场景设定')">生成场景设定新版本</button>
        <div v-for="version in commerceAssets?.scene_designs || []" :key="version.id" class="sub-card stack">
          <div class="meta-row"><strong>场景设定 v{{ version.version }}</strong><span class="status" :class="version.status === 'LOCKED' ? 'SUCCEEDED' : 'PENDING'">{{ version.status }}</span></div>
          <details><summary>查看结构化场景资料</summary><pre>{{ formatJson(version.content) }}</pre></details>
          <button v-if="version.status === 'READY'" class="button" :disabled="Boolean(actionId)" @click="lockCommerceVersion('scene', version.id)">确认并锁定场景设定</button>
        </div>
      </article>

      <article class="asset-card stack">
        <div class="meta-row"><strong>3. AI 导演分镜</strong><span>{{ commerceAssets?.storyboards.length || 0 }} 个版本</span></div>
        <p class="muted">只有锁定的角色和场景可以进入分镜。每镜保留片段摘要、连续性、商品融入节点与图片/视频提示词。</p>
        <p v-if="!hasLockedCommerceCharacter || !hasLockedCommerceScene" class="notice info">请先锁定角色设定和场景设定。</p>
        <button class="button" :disabled="Boolean(generatingKey) || !hasLockedCommerceCharacter || !hasLockedCommerceScene" @click="startCommerceOperation('STORYBOARD', 'AI 导演分镜')">生成导演分镜新版本</button>
        <div v-for="version in commerceAssets?.storyboards || []" :key="version.id" class="sub-card stack">
          <div class="meta-row"><strong>导演分镜 v{{ version.version }}</strong><span class="status" :class="version.status === 'LOCKED' ? 'SUCCEEDED' : 'PENDING'">{{ version.status }}</span></div>
          <details open><summary>镜头与商品证据链</summary><pre>{{ formatJson(version.content) }}</pre></details>
          <button v-if="version.status === 'READY'" class="button" :disabled="Boolean(actionId)" @click="lockCommerceVersion('storyboard', version.id)">确认并锁定导演分镜</button>
        </div>
      </article>

      <article v-if="activeCommerceStoryboard" class="asset-card stack">
        <div class="meta-row"><strong>4. 角色图、场景图与关键帧</strong><span>仅锁图可进入视频</span></div>
        <div class="action-row">
          <button class="button secondary" :disabled="Boolean(generatingKey) || !hasLockedCommerceCharacter" @click="startCommerceOperation('CHARACTER_IMAGES', '角色参考图')">生成角色参考图</button>
          <button class="button secondary" :disabled="Boolean(generatingKey) || !hasLockedCommerceScene" @click="startCommerceOperation('SCENE_IMAGES', '场景基础图')">生成场景基础图</button>
        </div>
        <div v-for="image in commerceAssets?.character_images || []" :key="image.id" class="media-row">
          <a v-if="safeMediaUrl(image.image_url)" :href="safeMediaUrl(image.image_url) || undefined" target="_blank" rel="noreferrer"><img :src="safeMediaUrl(image.image_url) || undefined" alt="角色参考图" /></a>
          <div><strong>角色 {{ image.logical_id }} · v{{ image.version }}</strong><p class="muted">{{ image.status }}</p><button v-if="image.status === 'READY'" class="button" :disabled="Boolean(actionId)" @click="lockCommerceImage('CHARACTER', image.id)">锁定角色图</button></div>
        </div>
        <div v-for="image in commerceAssets?.scene_images || []" :key="image.id" class="media-row">
          <a v-if="safeMediaUrl(image.image_url)" :href="safeMediaUrl(image.image_url) || undefined" target="_blank" rel="noreferrer"><img :src="safeMediaUrl(image.image_url) || undefined" alt="场景基础图" /></a>
          <div><strong>场景 {{ image.logical_id }} · v{{ image.version }}</strong><p class="muted">{{ image.status }}</p><button v-if="image.status === 'READY'" class="button" :disabled="Boolean(actionId)" @click="lockCommerceImage('SCENE', image.id)">锁定场景图</button></div>
        </div>
        <div v-for="shot in commerceShots" :key="String(shot.shot_id)" class="shot-production-card stack">
          <div class="meta-row"><strong>镜头 {{ shot.shot_number }} · {{ Number(shot.duration_ms || 0) / 1000 }} 秒</strong><span>{{ String(shot.segment_summary || '') }}</span></div>
          <p class="muted">商品节点：{{ String(shot.product_integration_node_id || '') }} · 动作：{{ String(shot.action || '') }}</p>
          <button class="button secondary" :disabled="Boolean(generatingKey) || !hasLockedCommerceCharacterImages || !hasLockedCommerceSceneImages" @click="startCommerceOperation('SHOT_KEYFRAME', `镜头 ${String(shot.shot_number)} 关键帧`, String(shot.shot_id))">生成/重生此镜关键帧</button>
          <div v-for="frame in (commerceAssets?.keyframes || []).filter((item) => item.logical_id === String(shot.shot_id))" :key="frame.id" class="media-row">
            <a v-if="safeMediaUrl(frame.image_url)" :href="safeMediaUrl(frame.image_url) || undefined" target="_blank" rel="noreferrer"><img :src="safeMediaUrl(frame.image_url) || undefined" :alt="`镜头 ${String(shot.shot_number)} 关键帧`" /></a>
            <div><strong>关键帧 v{{ frame.version }}</strong><p class="muted">{{ frame.status }}</p><button v-if="frame.status === 'READY'" class="button" :disabled="Boolean(actionId)" @click="lockCommerceImage('KEYFRAME', frame.id)">确认并锁定关键帧</button></div>
          </div>
        </div>
      </article>

      <article v-if="activeCommerceStoryboard" class="asset-card stack">
        <div class="meta-row"><strong>5. 视频 Prompt 与逐镜 MP4</strong><span>每镜独立重试</span></div>
        <div v-for="shot in commerceShots" :key="`video-${String(shot.shot_id)}`" class="shot-production-card stack">
          <div class="meta-row"><strong>镜头 {{ shot.shot_number }}</strong><span>{{ String(shot.segment_summary || '') }}</span></div>
          <button class="button secondary" :disabled="Boolean(generatingKey) || !hasLockedKeyframe(String(shot.shot_id))" @click="startCommerceOperation('VIDEO_PROMPT', `镜头 ${String(shot.shot_number)} 视频 Prompt`, String(shot.shot_id))">生成视频 Prompt</button>
          <div v-for="prompt in (commerceAssets?.video_prompts || []).filter((item) => item.shot_id === String(shot.shot_id))" :key="prompt.id" class="sub-card"><strong>Prompt v{{ prompt.version }} · {{ prompt.status }}</strong><p>{{ prompt.prompt }}</p></div>
          <button class="button" :disabled="Boolean(generatingKey) || !hasLockedVideoPrompt(String(shot.shot_id))" @click="startCommerceOperation('VIDEO_RENDER', `镜头 ${String(shot.shot_number)} MP4`, String(shot.shot_id))">生成此镜 MP4</button>
          <div v-for="clip in (commerceAssets?.clips || []).filter((item) => item.shot_id === String(shot.shot_id))" :key="clip.id" class="media-row">
            <video v-if="safeMediaUrl(clip.video_url)" controls preload="metadata" :src="safeMediaUrl(clip.video_url) || undefined" />
            <div class="stack"><strong>视频 v{{ clip.version }} · {{ clipStatusLabel(clip.status) }}</strong><small class="muted">版本 v{{ clip.version }} · 已记录重试 {{ clip.retry_count }} 次</small><small class="muted">供应商任务：{{ shortenedProviderTaskId(clip.provider_task_id) }} · {{ clip.can_resume_provider_task ? '已保存，可恢复查询' : (clip.provider_task_id ? '已保存' : '尚未创建') }}</small><small class="muted">创建：{{ formatTime(clip.created_at) }}<template v-if="clip.finished_at"> · 完成：{{ formatTime(clip.finished_at) }}</template></small><small v-if="clip.video_url" class="muted">{{ formatDuration(clip.duration_ms) }} · {{ mediaText(clip.media_metadata, 'width') }}×{{ mediaText(clip.media_metadata, 'height') }} · {{ mediaText(clip.media_metadata, 'video_codec') }} · {{ formatBytes(clip.file_size_bytes) }}</small><small v-if="clip.error_code" class="muted">错误代码：{{ clip.error_code }}</small><small v-if="clip.error_message" class="notice error">{{ clip.error_message }}</small>
              <div v-if="safeMediaUrl(clip.video_url)" class="action-row"><a class="button secondary" :href="safeMediaUrl(clip.video_url) || undefined" download>下载本地 MP4</a></div>
              <div v-if="clip.status === 'SUCCEEDED'" class="action-row"><button class="button" :disabled="Boolean(actionId)" @click="reviewCommerceClip(clip, 'APPROVED')">审核通过</button><button class="button danger" :disabled="Boolean(actionId)" @click="reviewCommerceClip(clip, 'REJECTED')">驳回</button></div>
              <p v-if="clip.can_resume_provider_task" class="notice info">供应商任务已经创建。可以继续查询其当前状态，不会重新生成视频或重复提交付费任务。</p>
              <button v-if="clip.can_resume_provider_task" class="button secondary" :disabled="Boolean(recoveringClipId) || Boolean(generatingKey)" @click="resumeCommerceClip(clip)">{{ recoveringClipId === clip.id ? '正在恢复…' : '恢复已有视频任务' }}</button>
              <button v-else-if="clip.status === 'FAILED' || clip.status === 'REJECTED'" class="button secondary" :disabled="Boolean(generatingKey) || Boolean(recoveringClipId)" @click="startCommerceOperation('VIDEO_RENDER', `镜头 ${String(shot.shot_number)} 重试`, String(shot.shot_id), true)">仅重试此镜</button>
            </div>
          </div>
        </div>
      </article>

      <article v-if="activeCommerceStoryboard" class="asset-card stack">
        <div class="meta-row"><strong>6. 成片合成</strong><span>只使用当前审核通过的镜头版本</span></div>
        <p class="muted">FFmpeg 会统一编码并按镜头顺序拼接。镜头重生后会生成新的成片版本，不覆盖历史。</p>
        <p v-if="!allCommerceShotsApproved" class="notice info">请先审核通过当前导演分镜的全部镜头，才可合成成片。</p>
        <button class="button" :disabled="Boolean(generatingKey) || !allCommerceShotsApproved" @click="startCommerceOperation('FINAL_COMPOSE', 'FFmpeg 成片合成')">合成新的完整 MP4</button>
        <div v-for="finalVideo in commerceAssets?.finals || []" :key="finalVideo.id" class="media-row">
          <video v-if="safeFinalVideoUrl(finalVideo.output_url, finalVideo.download_url)" controls preload="metadata" :src="safeFinalVideoUrl(finalVideo.output_url, finalVideo.download_url) || undefined" />
          <div class="stack"><strong>成片 v{{ finalVideo.version }} · {{ finalVideo.status }}</strong><small v-if="finalVideo.error_message" class="notice error">{{ finalVideo.error_message }}</small><a v-if="finalVideo.download_url" class="button secondary" :href="finalVideo.download_url">下载 MP4</a><small v-else-if="finalVideo.status === 'SUCCEEDED'" class="muted">成片已生成；当前存储方式尚未提供浏览器下载地址。</small></div>
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
        <a v-if="safeMediaUrl(image.image_url)" :href="safeMediaUrl(image.image_url) || undefined" target="_blank" rel="noreferrer"><img :src="safeMediaUrl(image.image_url) || undefined" :alt="`${image.character_name} 角色参考图`" /></a>
        <div class="stack"><div class="meta-row"><strong>{{ image.character_name }} · v{{ image.version }}</strong><span class="status" :class="image.review_status === 'LOCKED' ? 'SUCCEEDED' : 'PENDING'">{{ image.review_status }}</span></div><small class="muted">{{ image.character_code }} · {{ formatTime(image.created_at) }}<template v-if="image.asset_version_id"> · 资产中心版本已绑定</template></small>
          <div v-if="state?.active_stage === 'CHARACTER_ASSETS' && characterLibraryVersions.length" class="library-adopt-row"><el-select v-model="selectedCharacterLibraryVersion[image.character_id]" placeholder="或采用资产中心角色版本" size="small" filterable><el-option v-for="option in characterLibraryVersions" :key="option.id" :label="option.label" :value="option.id" /></el-select><el-button size="small" plain type="primary" :disabled="Boolean(actionId) || !selectedCharacterLibraryVersion[image.character_id]" @click="adoptLibraryCharacter(image)">采用为候选</el-button></div>
          <button v-if="image.review_status === 'PENDING_REVIEW' && state?.active_stage === 'CHARACTER_ASSETS'" class="button" :disabled="Boolean(actionId)" @click="review(`character-${image.id}`, () => lockCharacterReferenceImage(image.id, reviewPayload()))">{{ actionId === `character-${image.id}` ? '正在锁定…' : '锁定此角色图版本' }}</button>
        </div>
      </article>
    </section>

    <section v-if="sceneImages.length" class="panel stack review-panel">
      <div class="meta-row"><h2>场景参考图锁定</h2><span>{{ sceneImages.length }} 个版本</span></div>
      <article v-for="image in sceneImages" :key="image.id" class="asset-card asset-preview">
        <a v-if="safeMediaUrl(image.image_url)" :href="safeMediaUrl(image.image_url) || undefined" target="_blank" rel="noreferrer"><img :src="safeMediaUrl(image.image_url) || undefined" :alt="`${image.scene_name} 场景参考图`" /></a>
        <div class="stack"><div class="meta-row"><strong>{{ image.scene_name }} · v{{ image.version }}</strong><span class="status" :class="image.review_status === 'LOCKED' ? 'SUCCEEDED' : 'PENDING'">{{ image.review_status }}</span></div><small class="muted">{{ image.scene_code }} · {{ formatTime(image.created_at) }}<template v-if="image.asset_version_id"> · 资产中心版本已绑定</template></small>
          <div v-if="state?.active_stage === 'SCENE_ASSETS' && sceneLibraryVersions.length" class="library-adopt-row"><el-select v-model="selectedSceneLibraryVersion[image.scene_id]" placeholder="或采用资产中心场景版本" size="small" filterable><el-option v-for="option in sceneLibraryVersions" :key="option.id" :label="option.label" :value="option.id" /></el-select><el-button size="small" plain type="primary" :disabled="Boolean(actionId) || !selectedSceneLibraryVersion[image.scene_id]" @click="adoptLibraryScene(image)">采用为候选</el-button></div>
          <button v-if="image.review_status === 'PENDING_REVIEW' && state?.active_stage === 'SCENE_ASSETS'" class="button" :disabled="Boolean(actionId)" @click="review(`scene-${image.id}`, () => lockSceneReferenceImage(image.id, reviewPayload()))">{{ actionId === `scene-${image.id}` ? '正在锁定…' : '锁定此场景图版本' }}</button>
        </div>
      </article>
    </section>

    <section v-if="directorPlan" class="panel stack review-panel">
      <div class="meta-row"><h2>AI 导演结构化分镜</h2><span>{{ directorPlan.shots.length }} 个镜头 · {{ directorPlan.status }}</span></div>
      <p class="muted">每个镜头已冻结角色/场景资产版本，图片、视频和声音提示词均来自同一份导演方案；资产中心后续新增版本不会改写这里。</p>
      <details><summary>视觉圣经</summary><pre>{{ formatJson(directorPlan.visual_bible) }}</pre></details>
      <article v-for="shot in directorPlan.shots" :key="shot.id" class="director-shot">
        <div class="meta-row"><strong>镜头 {{ shot.shot_number }} · {{ shot.duration }} 秒</strong><span class="muted">角色资产 v{{ shot.character_asset_version_ids.length }} · 场景资产 {{ shot.scene_asset_version_id ? '已冻结' : '历史兼容' }}</span></div>
        <dl class="director-fields">
          <div><dt>动作</dt><dd>{{ shot.action }}</dd></div><div><dt>情绪</dt><dd>{{ shot.emotion }}</dd></div><div><dt>镜头</dt><dd>{{ shot.camera_type }} · {{ shot.camera_move }}</dd></div><div><dt>光线</dt><dd>{{ shot.lighting }}</dd></div>
          <div><dt>图片提示</dt><dd>{{ shot.image_prompt }}</dd></div><div><dt>视频提示</dt><dd>{{ shot.video_prompt }}</dd></div><div><dt>声音提示</dt><dd>{{ shot.sound_prompt }}</dd></div>
        </dl>
      </article>
    </section>

    <section v-if="keyframes.length" class="panel stack review-panel">
      <div class="meta-row"><h2>分镜关键帧锁定</h2><span>{{ keyframes.length }} 个版本</span></div>
      <article v-for="frame in keyframes" :key="frame.id" class="asset-card asset-preview">
        <a v-if="safeMediaUrl(frame.image_url)" :href="safeMediaUrl(frame.image_url) || undefined" target="_blank" rel="noreferrer"><img :src="safeMediaUrl(frame.image_url) || undefined" :alt="`镜头 ${frame.shot_number} 关键帧`" /></a>
        <div class="stack"><div class="meta-row"><strong>镜头 {{ frame.shot_number }} · v{{ frame.version }}</strong><span class="status" :class="frame.review_status === 'LOCKED' ? 'SUCCEEDED' : 'PENDING'">{{ frame.review_status }}</span></div>
          <button v-if="frame.review_status === 'PENDING_REVIEW' && state?.active_stage === 'SHOT_KEYFRAMES'" class="button" :disabled="Boolean(actionId)" @click="review(`keyframe-${frame.id}`, () => lockShotKeyframe(frame.id, reviewPayload()))">{{ actionId === `keyframe-${frame.id}` ? '正在锁定…' : '锁定此关键帧版本' }}</button>
        </div>
      </article>
    </section>

    <section v-if="videoClips.length" class="panel stack review-panel">
      <div class="meta-row"><h2>视频片段审核</h2><span>{{ videoClips.length }} 个版本</span></div>
      <article v-for="clip in videoClips" :key="clip.id" class="asset-card stack">
        <div class="meta-row"><strong>镜头 {{ clip.shot_number }} · 视频 v{{ clip.version }}<template v-if="clip.is_current">（当前采用）</template></strong><span class="status" :class="clip.review_status === 'APPROVED' ? 'SUCCEEDED' : 'PENDING'">{{ clip.review_status || '等待生成' }}</span></div>
        <a v-if="safeMediaUrl(clip.video_url)" class="button secondary" :href="safeMediaUrl(clip.video_url) || undefined" target="_blank" rel="noreferrer">打开视频预览</a>
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
.commerce-production { border-color: #bfdbfe; }
.sub-card { padding: 12px; border: 1px solid #e2e8f0; border-radius: 8px; background: #fcfdff; }
.shot-production-card { padding: 13px; border-left: 3px solid #60a5fa; background: #f8fbff; }
.media-row { display: grid; grid-template-columns: 150px minmax(0, 1fr); gap: 14px; align-items: start; padding: 12px; border: 1px solid #e2e8f0; border-radius: 8px; }
.media-row img, .media-row video { display: block; width: 150px; max-height: 180px; object-fit: cover; border-radius: 7px; background: #0f172a; }
.asset-preview { display: grid; grid-template-columns: 140px 1fr; gap: 16px; align-items: start; }
.asset-preview img { display: block; width: 140px; height: 180px; object-fit: cover; border-radius: 8px; background: #f1f5f9; }
.library-adopt-row { display: flex; gap: 8px; align-items: center; max-width: 520px; }
.library-adopt-row .el-select { min-width: 220px; flex: 1; }
.action-row { display: flex; flex-wrap: wrap; gap: 10px; }
.analysis-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 10px; }
.director-shot { border: 1px solid #dbe3ef; border-radius: 10px; padding: 14px; display: grid; gap: 12px; }
.director-fields { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 10px; margin: 0; }
.director-fields div { padding: 10px; border-radius: 8px; background: #f8fafc; }
.director-fields dt { font-size: 12px; color: #64748b; }
.director-fields dd { margin: 5px 0 0; white-space: pre-wrap; font-size: 13px; line-height: 1.55; }
.source-video-row { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: 10px; align-items: center; padding: 10px; border: 1px solid #dbe3ef; border-radius: 8px; cursor: pointer; }
.compact { padding: 6px 10px; font-size: 13px; }
details { border: 1px solid #e2e8f0; border-radius: 8px; padding: 10px; }
summary { cursor: pointer; font-weight: 600; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; margin: 10px 0 0; padding: 10px; border-radius: 7px; background: #f8fafc; font: 12px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace; }
@media (max-width: 640px) { .review-fields, .asset-preview, .media-row { grid-template-columns: 1fr; } .asset-preview img, .media-row img, .media-row video { width: 100%; height: auto; max-height: 320px; } .library-adopt-row { align-items: stretch; flex-direction: column; } }
</style>
