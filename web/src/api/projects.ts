/** 项目与工作流的具体 API 调用。 */
import { request } from './http'
import type { Asset, FinalVideo, Project, ProjectDetail, StoryboardImage, StoryboardPackage, StoryPackage, TopicCandidate, VideoClip, WorkflowRun } from '@/types/domain'

export interface CreateProjectPayload {
  title: string
  description?: string
}

/** 创建项目容器；调用成功后页面再上传素材。 */
export function createProject(payload: CreateProjectPayload): Promise<Project> {
  return request<Project>('/projects', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

/** 获取项目列表，供首页选择或新建。 */
export function getProjects(): Promise<Project[]> {
  return request<Project[]>('/projects')
}

/** 获取项目详情、素材与历史运行。 */
export function getProject(projectId: string): Promise<ProjectDetail> {
  return request<ProjectDetail>(`/projects/${projectId}`)
}

/**
 * 上传用户有权使用的参考视频。
 * FormData 不手动设置 Content-Type，浏览器会附带正确 multipart boundary。
 */
export function uploadSourceVideo(projectId: string, file: File): Promise<Asset> {
  const formData = new FormData()
  formData.append('file', file)
  return request<Asset>(`/projects/${projectId}/source-video`, {
    method: 'POST',
    body: formData,
  })
}

/** 创建并后台投递视频分析任务；接口返回的是初始 PENDING 状态。 */
export function startAnalysis(projectId: string): Promise<WorkflowRun> {
  return request<WorkflowRun>(`/projects/${projectId}/analysis-runs`, { method: 'POST' })
}

/** 轮询单个工作流运行状态。后续可由 SSE 替代，不改变页面数据结构。 */
export function getWorkflowRun(runId: string): Promise<WorkflowRun> {
  return request<WorkflowRun>(`/workflow-runs/${runId}`)
}

/** 仅对 FAILED/CANCELLED 运行开放的重试入口。 */
export function retryWorkflowRun(runId: string): Promise<WorkflowRun> {
  return request<WorkflowRun>(`/workflow-runs/${runId}/retry`, { method: 'POST' })
}

/** 创建原创选题任务；服务端会校验项目是否已完成视频分析。 */
export function startTopicGeneration(projectId: string): Promise<WorkflowRun> {
  return request<WorkflowRun>(`/projects/${projectId}/topic-generation-runs`, { method: 'POST' })
}

/** 读取项目全部选题候选，包含已确认项和历史草稿。 */
export function getTopicCandidates(projectId: string): Promise<TopicCandidate[]> {
  return request<TopicCandidate[]>(`/projects/${projectId}/topic-candidates`)
}

/** 人工确认选题；后续故事工作流只消费 SELECTED 候选。 */
export function selectTopicCandidate(topicId: string): Promise<TopicCandidate> {
  return request<TopicCandidate>(`/topic-candidates/${topicId}/select`, { method: 'POST' })
}

/** 从已确认选题生成一版待审核故事包。 */
export function startStoryGeneration(projectId: string): Promise<WorkflowRun> { return request<WorkflowRun>(`/projects/${projectId}/story-generation-runs`, { method: 'POST' }) }
/** 查询项目历史故事版本。 */
export function getStoryPackages(projectId: string): Promise<StoryPackage[]> { return request<StoryPackage[]>(`/projects/${projectId}/story-packages`) }
/** 人工确认故事版本，后续分镜只使用该版本。 */
export function confirmStoryPackage(packageId: string): Promise<StoryPackage> { return request<StoryPackage>(`/story-packages/${packageId}/confirm`, { method: 'POST' }) }
/** 按用户指定镜头数生成分镜细纲。 */
export function startStoryboardGeneration(projectId: string, shotCount: number): Promise<WorkflowRun> { return request<WorkflowRun>(`/projects/${projectId}/storyboard-runs`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ shot_count: shotCount }) }) }
export function getStoryboardPackages(projectId: string): Promise<StoryboardPackage[]> { return request<StoryboardPackage[]>(`/projects/${projectId}/storyboard-packages`) }
export function confirmStoryboardPackage(packageId: string): Promise<StoryboardPackage> { return request<StoryboardPackage>(`/storyboard-packages/${packageId}/confirm`, { method: 'POST' }) }
/** 不传镜头号即批量生成；传入数组则仅为这些镜头生成新版本。 */
export function startImageGeneration(projectId: string, shotNumbers?: number[]): Promise<WorkflowRun> { const query = shotNumbers?.length ? `?${shotNumbers.map((number) => `shot_numbers=${number}`).join('&')}` : ''; return request<WorkflowRun>(`/projects/${projectId}/image-runs${query}`, { method: 'POST' }) }
export function getStoryboardImages(projectId: string): Promise<StoryboardImage[]> { return request<StoryboardImage[]>(`/projects/${projectId}/storyboard-images`) }
/** 创建按连续镜头组运行的视频生成任务；groupNumbers 用于只重做部分组。 */
export function startVideoGeneration(projectId: string, shotsPerGroup: number, groupNumbers?: number[]): Promise<WorkflowRun> {
  return request<WorkflowRun>(`/projects/${projectId}/video-runs`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ shots_per_group: shotsPerGroup, group_numbers: groupNumbers }) })
}
/** 获取全部视频片段版本，响应按组号、版本倒序排列。 */
export function getVideoClips(projectId: string): Promise<VideoClip[]> { return request<VideoClip[]>(`/projects/${projectId}/video-clips`) }
/** 冻结当前完整片段方案，并在后台导出一版完整成片。 */
export function startFinalVideoExport(projectId: string): Promise<WorkflowRun> { return request<WorkflowRun>(`/projects/${projectId}/final-video-runs`, { method: 'POST' }) }
/** 查询完整成片历史；结果按最新导出在前。 */
export function getFinalVideos(projectId: string): Promise<FinalVideo[]> { return request<FinalVideo[]>(`/projects/${projectId}/final-videos`) }
