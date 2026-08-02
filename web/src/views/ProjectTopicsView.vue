<script setup lang="ts">
/** 项目选题页：用户审核候选，模型不能自动确认选题。 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import WorkflowTimeline from '@/components/WorkflowTimeline.vue'
import { useProjectStore } from '@/stores/project'

const props = defineProps<{ projectId: string }>()
const store = useProjectStore()
const creating = ref(false)
const project = computed(() => store.currentProject)
const analysisCompleted = computed(() => Boolean(project.value?.workflow_runs.some((run) => run.workflow_key === 'video_analysis' && run.status === 'SUCCEEDED')))

onMounted(async () => { await store.loadProject(props.projectId); await store.loadTopics(props.projectId) })
onUnmounted(() => store.stopPolling())

async function generate() { creating.value = true; await store.beginTopicGeneration(props.projectId); creating.value = false }
</script>

<template>
  <section class="page-heading"><div><RouterLink class="muted" :to="{ name: 'project-workbench', params: { projectId } }">← 返回项目工作台</RouterLink><h1>原创选题</h1><p>基于抽象分析和创作资产生成候选，须由你确认后才进入故事阶段。</p></div><button class="button" :disabled="creating || !analysisCompleted" @click="generate">{{ creating ? '正在创建任务…' : '生成原创选题' }}</button></section>
  <p v-if="store.error" class="notice error">{{ store.error }}</p><p v-if="!analysisCompleted" class="notice info">请先在项目工作台完成视频分析。</p>
  <WorkflowTimeline v-if="store.activeRun?.workflow_key === 'topic_generation'" :run="store.activeRun" @retry="store.retryAnalysis" />
  <section class="grid" style="margin-top:20px"><article v-for="topic in store.topicCandidates" :key="topic.id" class="panel stack"><div class="meta-row"><span class="status" :class="topic.status === 'SELECTED' ? 'SUCCEEDED' : 'PENDING'">{{ topic.status === 'SELECTED' ? '已确认' : `候选 ${topic.position}` }}</span><strong v-if="topic.score !== null">AI 辅助评分 {{ topic.score }}</strong></div><h2>{{ topic.title }}</h2><div><strong>爆点开头</strong><p>{{ topic.opening_hook }}</p></div><div><strong>故事概要</strong><p>{{ topic.synopsis }}</p></div><small class="muted">{{ topic.scoring_notes }}</small><button class="button" :disabled="topic.status === 'SELECTED'" @click="store.selectTopic(topic.id, props.projectId)">{{ topic.status === 'SELECTED' ? '当前确认选题' : '确认此选题' }}</button></article></section>
  <RouterLink v-if="store.topicCandidates.some((topic) => topic.status === 'SELECTED')" class="button" style="margin-top:20px" :to="{ name: 'project-story', params: { projectId } }">进入故事创作</RouterLink>
</template>
