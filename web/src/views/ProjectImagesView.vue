<script setup lang="ts">
/** 分镜图片页：批量生成与单镜重试共用同一任务状态机。 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import WorkflowTimeline from '@/components/WorkflowTimeline.vue'
import { useProjectStore } from '@/stores/project'
const props = defineProps<{ projectId: string }>(); const store = useProjectStore(); const creating = ref(false)
const confirmedBoard = computed(() => store.storyboardPackages.some((pack) => pack.status === 'CONFIRMED'))
onMounted(async () => { await store.loadProject(props.projectId); await store.loadStoryboards(props.projectId); await store.loadImages(props.projectId) })
onUnmounted(() => store.stopPolling())
async function generate(shots?: number[]) { creating.value = true; await store.beginImageGeneration(props.projectId, shots); creating.value = false }
function versions(shot: number) { return store.storyboardImages.filter((image) => image.shot_number === shot) }
</script>

<template>
  <section class="page-heading"><div><RouterLink class="muted" :to="{ name: 'project-storyboard', params: { projectId } }">← 返回分镜细纲</RouterLink><h1>分镜图片</h1><p>先确认分镜再生成；每次单镜重试都会保留新的图片版本。</p></div><button class="button" :disabled="creating || !confirmedBoard" @click="generate()">{{ creating ? '正在创建任务…' : '批量生成图片' }}</button></section>
  <p v-if="store.error" class="notice error">{{ store.error }}</p><p v-if="!confirmedBoard" class="notice info">请先确认一个分镜包。</p>
  <WorkflowTimeline v-if="store.activeRun?.workflow_key === 'image_generation'" :run="store.activeRun" @retry="store.retryAnalysis" />
  <section class="grid" style="margin-top:20px"><article v-for="pack in store.storyboardPackages.filter((item) => item.status === 'CONFIRMED')" :key="pack.id" class="panel stack"><div v-for="shot in pack.shots" :key="shot.number" class="result"><div class="meta-row"><strong>第 {{ shot.number }} 镜</strong><button class="button secondary" :disabled="creating" @click="generate([shot.number])">重试 / 新版本</button></div><img v-if="versions(shot.number)[0]?.image_url" :alt="`第 ${shot.number} 镜图片`" :src="versions(shot.number)[0]?.image_url || undefined" style="width:100%;max-width:260px;margin:12px 0;border-radius:8px" /><p><strong>当前提示词：</strong>{{ versions(shot.number)[0]?.prompt || shot.image_prompt }}</p><small class="muted">已生成 {{ versions(shot.number).length }} 个版本</small></div></article></section>
  <RouterLink v-if="confirmedBoard" class="button" style="margin-top:20px" :to="{ name: 'project-videos', params: { projectId } }">进入视频片段</RouterLink>
</template>
