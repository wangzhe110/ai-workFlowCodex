<script setup lang="ts">
/** 视频片段页：按可调镜头组提交任务，并允许对单组产生新版本。 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { apiDownloadUrl } from '@/api/http'
import WorkflowTimeline from '@/components/WorkflowTimeline.vue'
import { useProjectStore } from '@/stores/project'

const props = defineProps<{ projectId: string }>()
const store = useProjectStore()
const shotsPerGroup = ref(4)
const creating = ref(false)
const exporting = ref(false)

const confirmedBoard = computed(() => store.storyboardPackages.find((item) => item.status === 'CONFIRMED'))
const imagesReady = computed(() => {
  if (!confirmedBoard.value) return false
  return confirmedBoard.value.shots.every((shot) => store.storyboardImages.some(
    (image) => image.shot_number === shot.number && image.status === 'SUCCEEDED' && image.image_url,
  ))
})
const latestClips = computed(() => store.videoClips.filter((clip, index, clips) =>
  clips.findIndex((candidate) => candidate.group_number === clip.group_number) === index,
))
const canExport = computed(() => latestClips.value.length > 0 && latestClips.value.every((clip) => clip.status === 'SUCCEEDED'))

onMounted(async () => {
  await store.loadProject(props.projectId)
  await store.loadStoryboards(props.projectId)
  await store.loadImages(props.projectId)
  await store.loadVideoClips(props.projectId)
  await store.loadFinalVideos(props.projectId)
})
onUnmounted(() => store.stopPolling())

async function generateAll() {
  creating.value = true
  await store.beginVideoGeneration(props.projectId, shotsPerGroup.value)
  creating.value = false
}

async function regenerateGroup(groupNumber: number, groupShotsPerGroup: number) {
  creating.value = true
  await store.beginVideoGeneration(props.projectId, groupShotsPerGroup, [groupNumber])
  creating.value = false
}

/** 服务器会再次校验完整片段方案，页面只负责发起可追踪的后台导出任务。 */
async function exportFinalVideo() {
  exporting.value = true
  await store.beginFinalVideoExport(props.projectId)
  exporting.value = false
}

function versionCount(groupNumber: number) {
  return store.videoClips.filter((clip) => clip.group_number === groupNumber).length
}
</script>

<template>
  <section class="page-heading">
    <div>
      <RouterLink class="muted" :to="{ name: 'project-images', params: { projectId } }">← 返回分镜图片</RouterLink>
      <h1>视频片段</h1>
      <p>按连续镜头组生成短视频；默认每组 4 镜，可根据模型能力调整。</p>
    </div>
    <button class="button" :disabled="creating || !imagesReady" @click="generateAll">
      {{ creating ? '正在创建任务…' : '批量生成视频片段' }}
    </button>
  </section>

  <p v-if="store.error" class="notice error">{{ store.error }}</p>
  <p v-if="!confirmedBoard" class="notice info">请先确认一个分镜包。</p>
  <p v-else-if="!imagesReady" class="notice info">请先为确认分镜的每个镜头生成成功图片。</p>

  <section class="panel stack" style="margin-bottom:20px">
    <label class="field">
      每组镜头数
      <input v-model.number="shotsPerGroup" type="number" min="1" max="20" :disabled="creating" />
    </label>
    <small class="muted">这个参数会随本次任务写入记录；不同模型可使用不同分组，不影响历史结果。</small>
  </section>

  <WorkflowTimeline v-if="store.activeRun?.workflow_key === 'video_generation' || store.activeRun?.workflow_key === 'final_video_export'" :run="store.activeRun" @retry="store.retryAnalysis" />

  <section class="grid" style="margin-top:20px">
    <article v-for="clip in latestClips" :key="clip.id" class="panel stack">
      <div class="meta-row">
        <strong>第 {{ clip.group_number }} 组 · 第 {{ clip.start_shot_number }}–{{ clip.end_shot_number }} 镜</strong>
        <span class="status" :class="clip.status">{{ clip.status === 'SUCCEEDED' ? '已生成' : clip.status }}</span>
      </div>
      <p>{{ clip.prompt }}</p>
      <small class="muted">图片 {{ clip.image_ids.length }} 张 · 已保留 {{ versionCount(clip.group_number) }} 个版本</small>
      <a v-if="clip.video_url?.startsWith('http')" :href="clip.video_url" target="_blank" rel="noreferrer">打开视频结果</a>
      <p v-else class="notice info">当前为本地模拟适配器结果；接入中转站后此处展示真实视频地址。</p>
      <button class="button secondary" :disabled="creating || !imagesReady" @click="regenerateGroup(clip.group_number, clip.shots_per_group)">重做此组 / 新版本</button>
    </article>
  </section>

  <section class="panel stack" style="margin-top:20px">
    <div class="meta-row">
      <div>
        <h2>完整成片</h2>
        <p class="muted">按组号顺序合并当前最新成功片段；重做某一组后再次导出会保留新版本，不覆盖旧成片。</p>
      </div>
      <button class="button" :disabled="exporting || creating || !canExport" @click="exportFinalVideo">
        {{ exporting ? '正在创建导出任务…' : '合成完整成片' }}
      </button>
    </div>
    <p v-if="!canExport" class="notice info">请先成功生成完整的一套视频片段，才能导出完整成片。</p>
    <article v-for="video in store.finalVideos" :key="video.id" class="panel stack">
      <div class="meta-row">
        <strong>完整成片 · v{{ video.version }}</strong>
        <span class="status" :class="video.status">{{ video.status === 'SUCCEEDED' ? '已生成' : video.status }}</span>
      </div>
      <small class="muted">已冻结 {{ video.clip_ids.length }} 个片段版本，后续重做片段不会改写本成片。</small>
      <a v-if="video.download_url" :href="apiDownloadUrl(video.download_url)">下载 MP4</a>
      <p v-else-if="video.video_url?.startsWith('mock://')" class="notice info">当前为本地模拟成片；切换 FFmpeg 合成配置并使用真实视频片段后可下载 MP4。</p>
      <p v-if="video.error_message" class="notice error">{{ video.error_message }}</p>
    </article>
  </section>
</template>
