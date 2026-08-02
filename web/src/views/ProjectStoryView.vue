<script setup lang="ts">
/** 故事审核页：模型生成草稿，人确认后才允许生成分镜。 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import WorkflowTimeline from '@/components/WorkflowTimeline.vue'
import { useProjectStore } from '@/stores/project'

const props = defineProps<{ projectId: string }>()
const store = useProjectStore(); const creating = ref(false)
const selectedTopic = computed(() => store.topicCandidates.find((topic) => topic.status === 'SELECTED'))
onMounted(async () => { await store.loadProject(props.projectId); await store.loadTopics(props.projectId); await store.loadStories(props.projectId) })
onUnmounted(() => store.stopPolling())
async function generate() { creating.value = true; await store.beginStoryGeneration(props.projectId); creating.value = false }
</script>

<template>
  <section class="page-heading"><div><RouterLink class="muted" :to="{ name: 'project-topics', params: { projectId } }">← 返回原创选题</RouterLink><h1>故事创作</h1><p>生成故事大纲、角色卡与场景卡；确认后才可进入分镜。</p></div><button class="button" :disabled="creating || !selectedTopic" @click="generate">{{ creating ? '正在生成…' : '生成故事包' }}</button></section>
  <p v-if="store.error" class="notice error">{{ store.error }}</p><p v-if="!selectedTopic" class="notice info">请先确认一个原创选题。</p>
  <WorkflowTimeline v-if="store.activeRun?.workflow_key === 'story_generation'" :run="store.activeRun" @retry="store.retryAnalysis" />
  <article v-for="story in store.storyPackages" :key="story.id" class="panel stack" style="margin-top:20px"><div class="meta-row"><h2>{{ story.title }}</h2><span class="status" :class="story.status === 'CONFIRMED' ? 'SUCCEEDED' : 'PENDING'">{{ story.status === 'CONFIRMED' ? '已确认' : '待审核' }}</span></div><p>{{ story.premise }}</p>
    <div class="grid"><section><h3>故事大纲</h3><div v-for="part in story.outline" :key="part.act" class="result"><strong>{{ part.act }}</strong><br>{{ part.content }}</div></section><section><h3>角色卡</h3><div v-for="role in story.roles" :key="role.name" class="result"><strong>{{ role.name }} · {{ role.role }}</strong><br>目标：{{ role.goal }}<br>冲突：{{ role.conflict }}</div></section></div>
    <section><h3>场景卡</h3><div class="grid"><div v-for="scene in story.scenes" :key="scene.name" class="result"><strong>{{ scene.name }}</strong><br>{{ scene.purpose }}</div></div></section>
    <button class="button" :disabled="story.status === 'CONFIRMED'" @click="store.confirmStory(story.id, props.projectId)">{{ story.status === 'CONFIRMED' ? '当前确认版本' : '确认此故事包' }}</button>
  </article>
  <RouterLink v-if="store.storyPackages.some((story) => story.status === 'CONFIRMED')" class="button" style="margin-top:20px" :to="{ name: 'project-storyboard', params: { projectId } }">进入分镜细纲</RouterLink>
</template>
