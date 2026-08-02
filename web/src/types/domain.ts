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
