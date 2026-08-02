/** 项目与工作流状态。 */
import { defineStore } from 'pinia'
import {
  createProject,
  getProject,
  getProjects,
  getTopicCandidates,
  getWorkflowRun,
  retryWorkflowRun,
  startAnalysis,
  startTopicGeneration,
  selectTopicCandidate,
  startStoryGeneration,
  getStoryPackages,
  confirmStoryPackage,
  startStoryboardGeneration,
  getStoryboardPackages,
  confirmStoryboardPackage,
  startImageGeneration,
  getStoryboardImages,
  startVideoGeneration,
  getVideoClips,
  getFinalVideos,
  startFinalVideoExport,
  uploadSourceVideo,
} from '@/api/projects'
import type { FinalVideo, Project, ProjectDetail, StoryboardImage, StoryboardPackage, StoryPackage, TopicCandidate, VideoClip, WorkflowRun } from '@/types/domain'

/** 正在执行的状态集合；只有这些状态需要轮询。 */
const ACTIVE_RUN_STATUSES = new Set(['PENDING', 'RUNNING'])

export const useProjectStore = defineStore('project', {
  state: () => ({
    projects: [] as Project[],
    currentProject: null as ProjectDetail | null,
    activeRun: null as WorkflowRun | null,
    topicCandidates: [] as TopicCandidate[],
    storyPackages: [] as StoryPackage[],
    storyboardPackages: [] as StoryboardPackage[],
    storyboardImages: [] as StoryboardImage[],
    videoClips: [] as VideoClip[],
    finalVideos: [] as FinalVideo[],
    loading: false,
    error: '' as string,
    pollingTimer: null as ReturnType<typeof setInterval> | null,
  }),
  actions: {
    /** 统一捕获用户可见错误，调用方只需决定页面加载状态。 */
    setError(error: unknown) {
      this.error = error instanceof Error ? error.message : '操作失败，请稍后重试'
    },

    async loadProjects() {
      this.loading = true
      this.error = ''
      try {
        this.projects = await getProjects()
      } catch (error) {
        this.setError(error)
      } finally {
        this.loading = false
      }
    },

    async createProject(title: string, description?: string): Promise<Project | null> {
      this.error = ''
      try {
        const project = await createProject({ title, description })
        this.projects = [project, ...this.projects]
        return project
      } catch (error) {
        this.setError(error)
        return null
      }
    },

    async loadProject(projectId: string) {
      this.loading = true
      this.error = ''
      try {
        this.currentProject = await getProject(projectId)
        // 优先展示最近一次运行；运行详情在轮询过程中会持续更新。
        this.activeRun = this.currentProject.workflow_runs[0] ?? null
        if (this.activeRun && ACTIVE_RUN_STATUSES.has(this.activeRun.status)) {
          this.startPolling(this.activeRun.id)
        }
      } catch (error) {
        this.setError(error)
      } finally {
        this.loading = false
      }
    },

    async uploadVideo(projectId: string, file: File): Promise<boolean> {
      this.error = ''
      try {
        await uploadSourceVideo(projectId, file)
        await this.loadProject(projectId)
        return true
      } catch (error) {
        this.setError(error)
        return false
      }
    },

    async beginAnalysis(projectId: string): Promise<void> {
      this.error = ''
      try {
        this.activeRun = await startAnalysis(projectId)
        this.startPolling(this.activeRun.id)
      } catch (error) {
        this.setError(error)
      }
    },

    async retryAnalysis(runId: string): Promise<void> {
      this.error = ''
      try {
        this.activeRun = await retryWorkflowRun(runId)
        this.startPolling(runId)
      } catch (error) {
        this.setError(error)
      }
    },

    async loadTopics(projectId: string) {
      this.error = ''
      try {
        this.topicCandidates = await getTopicCandidates(projectId)
      } catch (error) {
        this.setError(error)
      }
    },

    /** 选题生成同样是异步任务，复用统一运行状态和轮询机制。 */
    async beginTopicGeneration(projectId: string): Promise<void> {
      this.error = ''
      try {
        this.activeRun = await startTopicGeneration(projectId)
        this.startPolling(this.activeRun.id)
      } catch (error) {
        this.setError(error)
      }
    },

    /** 选题确认后刷新全部卡片，确保唯一 SELECTED 状态同步。 */
    async selectTopic(topicId: string, projectId: string): Promise<void> {
      this.error = ''
      try {
        await selectTopicCandidate(topicId)
        await this.loadTopics(projectId)
      } catch (error) {
        this.setError(error)
      }
    },

    async loadStories(projectId: string) { try { this.storyPackages = await getStoryPackages(projectId) } catch (error) { this.setError(error) } },
    async beginStoryGeneration(projectId: string) { try { this.activeRun = await startStoryGeneration(projectId); this.startPolling(this.activeRun.id) } catch (error) { this.setError(error) } },
    async confirmStory(packageId: string, projectId: string) { try { await confirmStoryPackage(packageId); await this.loadStories(projectId) } catch (error) { this.setError(error) } },
    async loadStoryboards(projectId: string) { try { this.storyboardPackages = await getStoryboardPackages(projectId) } catch (error) { this.setError(error) } },
    async beginStoryboardGeneration(projectId: string, shotCount: number) { try { this.activeRun = await startStoryboardGeneration(projectId, shotCount); this.startPolling(this.activeRun.id) } catch (error) { this.setError(error) } },
    async confirmStoryboard(packageId: string, projectId: string) { try { await confirmStoryboardPackage(packageId); await this.loadStoryboards(projectId) } catch (error) { this.setError(error) } },
    async loadImages(projectId: string) { try { this.storyboardImages = await getStoryboardImages(projectId) } catch (error) { this.setError(error) } },
    async beginImageGeneration(projectId: string, shotNumbers?: number[]) { try { this.activeRun = await startImageGeneration(projectId, shotNumbers); this.startPolling(this.activeRun.id) } catch (error) { this.setError(error) } },
    async loadVideoClips(projectId: string) { try { this.videoClips = await getVideoClips(projectId) } catch (error) { this.setError(error) } },
    async beginVideoGeneration(projectId: string, shotsPerGroup: number, groupNumbers?: number[]) { try { this.activeRun = await startVideoGeneration(projectId, shotsPerGroup, groupNumbers); this.startPolling(this.activeRun.id) } catch (error) { this.setError(error) } },
    async loadFinalVideos(projectId: string) { try { this.finalVideos = await getFinalVideos(projectId) } catch (error) { this.setError(error) } },
    async beginFinalVideoExport(projectId: string) { try { this.activeRun = await startFinalVideoExport(projectId); this.startPolling(this.activeRun.id) } catch (error) { this.setError(error) } },

    /** 单次刷新运行状态；完成或失败后自动停止轮询。 */
    async refreshRun(runId: string): Promise<void> {
      try {
        this.activeRun = await getWorkflowRun(runId)
        if (!ACTIVE_RUN_STATUSES.has(this.activeRun.status)) {
          this.stopPolling()
          if (this.currentProject) void this.loadTopics(this.currentProject.id)
          if (this.currentProject) void this.loadStories(this.currentProject.id)
          if (this.currentProject) void this.loadStoryboards(this.currentProject.id)
          if (this.currentProject) void this.loadImages(this.currentProject.id)
          if (this.currentProject) void this.loadVideoClips(this.currentProject.id)
          if (this.currentProject) void this.loadFinalVideos(this.currentProject.id)
        }
      } catch (error) {
        this.setError(error)
        this.stopPolling()
      }
    },

    /** 轮询间隔集中定义，后续切换 SSE 时只替换此方法。 */
    startPolling(runId: string) {
      this.stopPolling()
      void this.refreshRun(runId)
      this.pollingTimer = setInterval(() => void this.refreshRun(runId), 1000)
    },

    /** 页面离开或运行完成时释放定时器。 */
    stopPolling() {
      if (this.pollingTimer) {
        clearInterval(this.pollingTimer)
        this.pollingTimer = null
      }
    },
  },
})
