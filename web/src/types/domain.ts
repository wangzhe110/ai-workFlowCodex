/** 平台 API 对前端公开的领域数据模型。 */

export interface Asset {
  id: string
  kind: 'SOURCE_VIDEO'
  original_filename: string
  content_type: string
  byte_size: number
  created_at: string
}

export interface WorkflowStep {
  id: string
  step_key: string
  position: number
  status: 'PENDING' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED'
  progress: number
  attempt: number
  output_payload: Record<string, unknown> | null
  error_message: string | null
  started_at: string | null
  finished_at: string | null
}

export interface WorkflowRun {
  id: string
  project_id: string
  workflow_key: string
  status: WorkflowStep['status']
  created_at: string
  started_at: string | null
  finished_at: string | null
  steps: WorkflowStep[]
}

export interface Project {
  id: string
  title: string
  description: string | null
  created_at: string
  updated_at: string
  source_video_count: number
}

export interface ProjectDetail extends Project {
  assets: Asset[]
  workflow_runs: WorkflowRun[]
}

/** 可被人工维护并供选题生成引用的抽象创作机制。 */
export interface CreativeLibraryItem {
  id: string
  kind: 'VIRAL_ELEMENT' | 'OPENING_PATTERN'
  title: string
  content: string
  group_name: string | null
  tags: string[]
  source: 'MANUAL' | 'ANALYSIS'
  is_active: boolean
  created_at: string
  updated_at: string
}

/** 选题工作流生成的原创候选，只有用户可将其确认进入下一步。 */
export interface TopicCandidate {
  id: string
  project_id: string
  generation_run_id: string
  position: number
  title: string
  opening_hook: string
  synopsis: string
  score: number | null
  scoring_notes: string | null
  status: 'DRAFT' | 'SELECTED'
  created_at: string
  updated_at: string
}

/** 可审核的一版故事、角色与场景快照。 */
export interface StoryPackage {
  id: string; project_id: string; topic_candidate_id: string; generation_run_id: string
  title: string; premise: string
  outline: Array<{ act: string; content: string }>
  roles: Array<{ name: string; role: string; goal: string; conflict: string }>
  scenes: Array<{ name: string; purpose: string }>
  status: 'DRAFT' | 'CONFIRMED'; created_at: string; updated_at: string
}

/** 一版镜头数量可配置的分镜细纲。 */
export interface StoryboardPackage {
  id: string; project_id: string; story_package_id: string; generation_run_id: string
  target_shot_count: number
  shots: Array<{ number: number; duration_seconds: number; scene: string; visual: string; dialogue_or_voiceover: string; camera: string; image_prompt: string; video_prompt: string }>
  status: 'DRAFT' | 'CONFIRMED'; created_at: string; updated_at: string
}

/** 单镜图片的一版生成结果。 */
export interface StoryboardImage {
  id: string; storyboard_package_id: string; generation_run_id: string; shot_number: number; version: number
  prompt: string; image_url: string | null; status: 'PENDING' | 'SUCCEEDED' | 'FAILED'; error_message: string | null; created_at: string
}

/** 一个连续镜头组生成的一版视频结果；图片版本与提示词都已被冻结。 */
export interface VideoClip {
  id: string; storyboard_package_id: string; generation_run_id: string
  group_number: number; start_shot_number: number; end_shot_number: number; shots_per_group: number; version: number
  image_ids: string[]; prompt: string; video_url: string | null; provider_task_id: string | null
  status: 'PENDING' | 'SUCCEEDED' | 'FAILED'; error_message: string | null; created_at: string
}

/** 一版完整成片；冻结其片段版本顺序，下载地址仅由后端在文件就绪后提供。 */
export interface FinalVideo {
  id: string; storyboard_package_id: string; generation_run_id: string; version: number
  clip_ids: string[]; video_url: string | null; download_url: string | null
  status: 'PENDING' | 'SUCCEEDED' | 'FAILED'; error_message: string | null
  created_at: string; finished_at: string | null
}

/** 某工作流步骤的一版非敏感模型配置；真实密钥仅以环境变量名称引用。 */
export interface ModelProfile {
  id: string; step_key: string; provider_key: string; model_key: string; version: number
  provider_config: Record<string, unknown>; is_active: boolean; adapter_available: boolean; created_at: string
}

/** 模型候选在启用前的无扣费基础检查；不包含真实密钥或第三方原始错误。 */
export interface ModelPreflightCheck {
  key: string
  status: 'passed' | 'failed' | 'warning'
  message: string
}

export interface ModelProfilePreflight {
  profile_id: string
  ready: boolean
  checked_at: string
  checks: ModelPreflightCheck[]
}

/** 一次人工小样本验收的输入；只保存汇总数字，不保存模型原始内容。 */
export interface ModelEvaluationPayload {
  scenario: string
  sample_count: number
  success_count: number
  total_cost_yuan: number
  average_latency_seconds: number
  quality_score: number
  notes?: string | null
}

export interface ModelEvaluation extends ModelEvaluationPayload {
  id: string
  model_profile_id: string
  notes: string | null
  success_rate: number
  average_cost_yuan: number
  cost_per_success_yuan: number | null
  created_at: string
}

/** 同一工作流步骤中，用于横向比较的评测行；只应比较相同测试场景。 */
export interface ModelEvaluationComparison extends ModelEvaluation {
  step_key: string
  provider_key: string
  model_key: string
  profile_version: number
  display_name: string | null
}

/** LemonFlow V1 唯一生产主链路当前阶段及已冻结输入指针。 */
export interface ProductionState {
  project_id: string
  active_stage: ProductionStage
  workflow_definition_id: string
  locked_reference_analysis_id: string | null
  selected_story_proposal_id: string | null
  director_plan_id: string | null
  created_at: string
  updated_at: string
}

/** Slice 1：由 V1 分析自动生成、待制作人确认的脚本与商品输入版本。 */
export interface CommerceReferenceIntake {
  id: string
  project_id: string
  reference_analysis_id: string
  script_asset_id: string
  script_analysis_version_id: string
  product_asset_id: string
  product_analysis_version_id: string
  product_asset_version_id: string
  product_status: 'DRAFT' | 'CONFIRMED' | 'ARCHIVED'
  product_frozen_at: string | null
  input_snapshot: Record<string, unknown>
  created_at: string
  updated_at: string
}

/** 固定十个候选之一；选择后由现有 Commerce StoryRun 继续生成大纲。 */
export interface CommerceCreativeIdea {
  id: string
  batch_id: string
  project_id: string
  candidate_number: number
  content: Record<string, unknown>
  status: 'CANDIDATE' | 'SELECTED' | 'REJECTED'
  topic_candidate_id: string | null
  created_at: string
}

export interface CommerceCreativeBatch {
  id: string
  project_id: string
  reference_intake_id: string
  workflow_run_id: string
  batch_number: number
  status: 'PENDING' | 'RUNNING' | 'SUCCEEDED' | 'FAILED'
  input_snapshot: Record<string, unknown>
  model_snapshot: Record<string, unknown>
  prompt_snapshot: Record<string, unknown>
  error_message: string | null
  created_at: string
  finished_at: string | null
  ideas: CommerceCreativeIdea[]
}

export interface CommerceStoryRunSelection {
  story_run_id: string
  project_id: string
  topic_candidate_id: string
  product_asset_version_id: string
  current_stage: string
  current_status: string
}

/** Slice 1 选择创意后复用的 Commerce StoryRun；Slice 2 的所有资产都只挂在它下面。 */
export interface CommerceStoryRun {
  id: string
  project_id: string
  topic_candidate_id: string
  product_asset_version_id: string
  run_number: number
  mode: 'STEPWISE' | 'AUTO'
  current_stage: string
  current_status: string
  blocked_reason: string | null
  can_start: boolean
  can_continue: boolean
  can_confirm: boolean
  latest_error: string | null
  stage_result_references: Record<string, unknown>
  created_at: string
  updated_at: string
}

/** 已审核的大纲与商品融入策略；角色、场景、分镜任务都会冻结它的具体版本。 */
export interface CommerceOutline {
  id: string
  story_run_id: string
  version: number
  title: string
  premise: string
  story_beats: Array<Record<string, unknown>>
  product_placement_strategy: Record<string, unknown>
  status: string
  created_at: string
}

/** Slice 2 的文本版本：角色设定、场景设定或导演分镜，均为只追加版本。 */
export interface CommerceProductionVersion {
  id: string
  story_run_id: string
  version: number
  status: 'READY' | 'LOCKED' | 'SUPERSEDED' | 'STALE' | 'FAILED' | string
  content: Record<string, unknown>
  input_snapshot: Record<string, unknown>
  prompt_snapshot: Record<string, unknown>
  locked_at: string | null
  stale_at: string | null
  created_at: string
}

/** 角色图、场景图或单镜关键帧的独立版本；锁定后才可成为后续任务输入。 */
export interface CommerceProductionImage {
  id: string
  story_run_id: string
  owner_version_id: string
  logical_id: string
  version: number
  image_url: string | null
  status: 'READY' | 'LOCKED' | 'STALE' | 'FAILED' | string
  prompt_snapshot: string
  input_snapshot: Record<string, unknown>
  error_message: string | null
  locked_at: string | null
  stale_at: string | null
  created_at: string
}

export interface CommerceVideoPrompt {
  id: string
  story_run_id: string
  storyboard_version_id: string
  shot_id: string
  shot_number: number
  keyframe_version_id: string
  version: number
  prompt: string
  trace: Record<string, unknown>
  status: string
  locked_at: string | null
  stale_at: string | null
  created_at: string
}

/** 单镜头独立视频任务。失败只重做本镜，不会回滚同一 StoryRun 的其他镜头。 */
export interface CommerceVideoClip {
  id: string
  story_run_id: string
  storyboard_version_id: string
  shot_id: string
  shot_number: number
  keyframe_version_id: string
  video_prompt_version_id: string
  version: number
  provider_task_id: string | null
  video_url: string | null
  status: 'RUNNING' | 'SUCCEEDED' | 'APPROVED' | 'REJECTED' | 'FAILED' | 'STALE' | string
  /** 后端根据已保存的本地审计提供；点击后只恢复同一个供应商任务号。 */
  can_resume_provider_task: boolean
  error_code: string | null
  error_message: string | null
  retry_count: number
  duration_ms: number | null
  file_size_bytes: number | null
  media_metadata: Record<string, unknown>
  reviewed_at: string | null
  review_note: string | null
  stale_at: string | null
  created_at: string
  finished_at: string | null
}

/** FFmpeg 导出的成片版本，冻结有序镜头版本；旧成片不会被新镜头覆盖。 */
export interface CommerceFinalVideo {
  id: string
  story_run_id: string
  storyboard_version_id: string
  version: number
  clip_ids: string[]
  output_url: string | null
  download_url: string | null
  status: 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'STALE' | string
  error_message: string | null
  media_metadata: Record<string, unknown>
  stale_at: string | null
  created_at: string
  finished_at: string | null
}

export interface CommerceProductionAssets {
  story_run_id: string
  character_designs: CommerceProductionVersion[]
  scene_designs: CommerceProductionVersion[]
  storyboards: CommerceProductionVersion[]
  character_images: CommerceProductionImage[]
  scene_images: CommerceProductionImage[]
  keyframes: CommerceProductionImage[]
  video_prompts: CommerceVideoPrompt[]
  clips: CommerceVideoClip[]
  finals: CommerceFinalVideo[]
}

export type ProductionStage =
  | 'REFERENCE_ANALYSIS'
  | 'ANALYSIS_REVIEW'
  | 'STORY_GENERATION'
  | 'STORY_REVIEW'
  | 'CHARACTER_ASSETS'
  | 'SCENE_ASSETS'
  | 'DIRECTOR_PLANNING'
  | 'SHOT_KEYFRAMES'
  | 'VIDEO_GENERATION'
  | 'VIDEO_REVIEW'
  | 'FINAL_EXPORT'
  | 'COMPLETED'
  | 'LEGACY_READONLY'

/** V1 的人工审核附带可追溯备注；登录上线后会自动替换 reviewer_label。 */
export interface ReviewActionPayload {
  reviewer_label?: string
  note?: string
  /** 可选的制作人主观评分；采用决定与评分独立保存，均用于模型横向比较。 */
  quality_score?: number
}

/** V1 模型调用与人工审核形成的最新质量报表快照，只供人工比较，不会自动切换模型。 */
export interface ModelQualityEvaluation {
  id: string
  model_profile_id: string
  display_name: string
  model_key: string
  model_version: string
  prompt_template_id: string | null
  prompt_name: string | null
  prompt_version: number | null
  task_type: string
  scenario: string
  sample_count: number
  success_count: number
  success_rate: number
  average_cost_amount: number | null
  currency: string
  average_latency_ms: number | null
  average_human_score: number | null
  adoption_rate: number | null
  created_at: string
}

/** 项目内一次模型调用的安全追溯视图；不包含原视频、Prompt 正文和模型原始输出。 */
export interface ModelInvocationTrace {
  id: string
  workflow_run_id: string | null
  workflow_key: string | null
  workflow_version: string | null
  task_type: string
  slot_key: string
  model_display_name: string
  model_key: string
  model_version: string
  model_profile_version: number | null
  prompt_template_id: string | null
  prompt_template_version_id?: string | null
  prompt_key?: string | null
  prompt_content_hash?: string | null
  prompt_name: string | null
  prompt_version: number | null
  status: 'PENDING' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED'
  provider_task_id: string | null
  input_tokens: number | null
  output_tokens: number | null
  media_units: Record<string, unknown>
  cost_amount: number | null
  currency: string
  latency_ms: number | null
  error_code: string | null
  created_at: string
  finished_at: string | null
}

/** Gemini 等视频分析模型产生的抽象创作简报。 */
export interface ReferenceAnalysis {
  id: string
  project_id: string
  workflow_run_id: string
  version: number
  video_script_structure: Record<string, unknown>
  opening_analysis: Record<string, unknown>
  viral_elements: Array<Record<string, unknown>>
  scene_analysis: Array<Record<string, unknown>>
  creative_brief: Record<string, unknown>
  generation_status: 'PENDING' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED'
  review_status: 'PENDING_REVIEW' | 'LOCKED' | 'REJECTED'
  locked_snapshot: Record<string, unknown> | null
  locked_at: string | null
  created_at: string
  updated_at: string
}

/** 并行导演/编剧模型生成的原创故事方案。 */
export interface StoryProposalV1 {
  id: string
  project_id: string
  batch_id: string
  model_invocation_id: string | null
  candidate_number: number
  content: Record<string, unknown>
  status: 'CANDIDATE' | 'SELECTED' | 'REJECTED'
  created_at: string
}

export interface CharacterReferenceImageV1 {
  id: string
  project_id: string
  character_id: string
  character_code: string
  character_name: string
  version: number
  asset_version_id: string | null
  image_url: string | null
  generation_status: 'PENDING' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED'
  review_status: 'PENDING_REVIEW' | 'LOCKED' | 'REJECTED'
  created_at: string
}

export interface SceneReferenceImageV1 {
  id: string
  project_id: string
  scene_id: string
  scene_code: string
  scene_name: string
  version: number
  asset_version_id: string | null
  image_url: string | null
  generation_status: 'PENDING' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED'
  review_status: 'PENDING_REVIEW' | 'LOCKED' | 'REJECTED'
  created_at: string
}

export interface ShotKeyframeV1 {
  id: string
  project_id: string
  shot_id: string
  shot_number: number
  version: number
  image_url: string | null
  generation_status: 'PENDING' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED'
  review_status: 'PENDING_REVIEW' | 'LOCKED' | 'REJECTED'
  input_asset_snapshot: Record<string, unknown>
  created_at: string
}

export interface VideoClipV1 {
  id: string
  project_id: string
  shot_plan_id: string
  shot_number: number
  version: number
  video_url: string | null
  provider_task_id: string | null
  task_status: string | null
  is_current: boolean
  generation_status: string | null
  review_status: 'PENDING_REVIEW' | 'APPROVED' | 'REJECTED' | null
  review_note: string | null
  input_asset_snapshot: Record<string, unknown> | null
  created_at: string
}

/** Phase 4 资产中心：资产主体跨项目复用，版本内容只追加、从不覆盖。 */
export interface AssetReferenceImage {
  view: 'front' | 'side' | 'full_body' | 'expression' | 'wide' | 'detail' | 'generated'
  url: string
  label?: string
}

export interface CharacterAssetVersion {
  id: string
  character_asset_id: string
  version: number
  description: string
  age: string | null
  gender: string | null
  personality: string | null
  style: string | null
  appearance: string | null
  costume: string | null
  reference_images: AssetReferenceImage[]
  created_at: string
}

export interface CharacterAsset {
  id: string
  library_id: string
  name: string
  description: string
  status: string
  versions: CharacterAssetVersion[]
  created_at: string
  updated_at: string
}

export interface SceneAssetVersion {
  id: string
  scene_asset_id: string
  version: number
  description: string
  style: string | null
  weather: string | null
  time_of_day: string | null
  location: string | null
  environment: string | null
  mood: string | null
  reference_images: AssetReferenceImage[]
  created_at: string
}

export interface SceneAsset {
  id: string
  library_id: string
  name: string
  description: string
  status: string
  versions: SceneAssetVersion[]
  created_at: string
  updated_at: string
}

/** AI 导演镜头是图片、视频和声音步骤的统一生产输入，而非自由文本。 */
export interface DirectorShot {
  id: string
  shot_number: number
  duration: number
  character_ids: string[]
  character_asset_version_ids: string[]
  scene_id: string
  scene_asset_version_id: string | null
  action: string
  emotion: string
  camera_type: string
  camera_move: string
  lighting: string
  image_prompt: string
  video_prompt: string
  sound_prompt: string
  locked_keyframe_id: string | null
  created_at: string
}

export interface DirectorPlanV1 {
  id: string
  project_id: string
  story_proposal_id: string
  workflow_run_id: string
  visual_bible: Record<string, unknown>
  status: string
  shots: DirectorShot[]
  created_at: string
  updated_at: string
}

/** 模型槽位只描述要完成的能力，业务页面不依赖具体供应商名称。 */
export interface ModelSlot {
  id: string
  slot_key: string
  capability: string
  selection_mode: 'SINGLE' | 'MULTI_PARALLEL' | 'AB_TEST'
  description: string
  is_enabled: boolean
  created_at: string
  updated_at: string
}

/** V1 模型中心中的一个候选版本；Key 只以服务器环境变量名引用，不会出现在这里。 */
export interface V1ModelProfile {
  id: string
  slot_key: string
  adapter_key: string
  model_key: string
  display_name: string
  model_version: string | null
  version: number
  provider_config: Record<string, unknown>
  parameter_config: ModelParameterConfig
  parameter_config_complete: boolean
  is_bound: boolean
  is_enabled_in_slot: boolean
  priority: number | null
  profile_status: 'DRAFT' | 'ACTIVE' | 'HISTORICAL'
  has_model_invocations: boolean
  can_edit: boolean
  active_run_count: number
  can_delete: boolean
  delete_block_reason: string | null
  created_at: string
}

export interface ModelParameterConfig {
  schema_version: number
  capability: 'text' | 'image' | 'video' | 'local_compose'
  supported_parameters: Record<string, Record<string, unknown>>
  defaults: Record<string, unknown>
  presets: Partial<Record<'preview' | 'standard' | 'high', Record<string, unknown>>>
}

export interface V1ModelProfileCreatePayload {
  slot_key: string
  adapter_key: string
  model_key: string
  display_name: string
  model_version?: string
  provider_config: Record<string, unknown>
  parameter_config?: ModelParameterConfig
  enable_in_slot: boolean
  replace_existing: boolean
  priority?: number
}

export interface V1ModelProfileUpdatePayload {
  adapter_key: string
  model_key: string
  display_name: string
  model_version?: string
  provider_config: Record<string, unknown>
  parameter_config?: ModelParameterConfig
}

/** 可版本化的生产 Prompt；真实调用会另外冻结一份快照。 */
export interface PromptTemplate {
  id: string
  task_type: string
  name: string
  version: number
  content: string
  variables_schema: Record<string, unknown>
  status: 'DRAFT' | 'ACTIVE' | 'ARCHIVED'
  created_at: string
  updated_at: string
}

/** 系统级 Prompt 配置。业务镜头产生的 video_prompt_versions 不属于该对象。 */
export interface PromptTemplateVersion {
  id: string
  prompt_template_id: string
  version: number
  status: 'DRAFT' | 'PUBLISHED'
  system_template: string
  user_template: string
  allowed_variables: Record<string, { description: string; required: boolean }>
  output_contract_key: string
  content_hash: string
  change_summary: string
  created_at: string
  updated_at: string
}

export interface PromptTemplateCatalog {
  id: string
  prompt_key: string
  display_name: string
  description: string
  operation_key: string
  model_slot_key: string | null
  capability: string
  active_version_id: string | null
  active_version: PromptTemplateVersion | null
  versions: PromptTemplateVersion[]
  draft_count: number
  created_at: string
  updated_at: string
}

export interface PromptTemplateRenderPreview {
  prompt_key: string
  prompt_template_id: string
  prompt_version_id: string
  prompt_version: number
  content_hash: string
  rendered_system_template: string
  rendered_user_template: string
  rendered_prompt_hash: string
  sanitized_variable_snapshot: Record<string, unknown>
}
