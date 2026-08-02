<script setup lang="ts">
/** 分镜审核页：镜头数由用户设定，确认后才会消耗图片/视频模型额度。 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import WorkflowTimeline from '@/components/WorkflowTimeline.vue'
import { useProjectStore } from '@/stores/project'

const props = defineProps<{ projectId: string }>()
const store = useProjectStore(); const shotCount = ref(12); const creating = ref(false)
const confirmedStory = computed(() => store.storyPackages.some((story) => story.status === 'CONFIRMED'))
onMounted(async () => { await store.loadProject(props.projectId); await store.loadStories(props.projectId); await store.loadStoryboards(props.projectId) })
onUnmounted(() => store.stopPolling())
async function generate() { creating.value = true; await store.beginStoryboardGeneration(props.projectId, shotCount.value); creating.value = false }
</script>

<template>
  <section class="page-heading"><div><RouterLink class="muted" :to="{ name: 'project-story', params: { projectId } }">← 返回故事创作</RouterLink><h1>分镜细纲</h1><p>镜头数按本项目需要配置，不沿用其他项目的固定数量。</p></div></section>
  <section class="panel stack"><div class="grid"><label class="field">计划镜头数<input v-model.number="shotCount" min="1" max="200" type="number" /></label><div><p class="muted">确认故事后再生成；图片和视频模型将在确认分镜后才允许调用。</p><button class="button" :disabled="creating || !confirmedStory || shotCount < 1 || shotCount > 200" @click="generate">{{ creating ? '正在生成…' : '生成分镜细纲' }}</button></div></div><p v-if="store.error" class="notice error">{{ store.error }}</p><p v-if="!confirmedStory" class="notice info">请先确认一个故事包。</p></section>
  <WorkflowTimeline v-if="store.activeRun?.workflow_key === 'storyboard_generation'" style="margin-top:20px" :run="store.activeRun" @retry="store.retryAnalysis" />
  <article v-for="pack in store.storyboardPackages" :key="pack.id" class="panel stack" style="margin-top:20px"><div class="meta-row"><h2>{{ pack.target_shot_count }} 镜分镜细纲</h2><span class="status" :class="pack.status === 'CONFIRMED' ? 'SUCCEEDED' : 'PENDING'">{{ pack.status === 'CONFIRMED' ? '已确认' : '待审核' }}</span></div><details v-for="shot in pack.shots" :key="shot.number" class="result"><summary>第 {{ shot.number }} 镜 · {{ shot.scene }} · {{ shot.duration_seconds }} 秒</summary><p><strong>画面：</strong>{{ shot.visual }}</p><p><strong>台词/旁白：</strong>{{ shot.dialogue_or_voiceover }}</p><p><strong>运镜：</strong>{{ shot.camera }}</p><p><strong>图片提示词：</strong>{{ shot.image_prompt }}</p><p><strong>视频提示词：</strong>{{ shot.video_prompt }}</p></details><button class="button" :disabled="pack.status === 'CONFIRMED'" @click="store.confirmStoryboard(pack.id, props.projectId)">{{ pack.status === 'CONFIRMED' ? '当前确认分镜' : '确认此分镜' }}</button></article>
  <RouterLink v-if="store.storyboardPackages.some((pack) => pack.status === 'CONFIRMED')" class="button" style="margin-top:20px" :to="{ name: 'project-images', params: { projectId } }">进入分镜图片</RouterLink>
</template>
