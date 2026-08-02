<script setup lang="ts">
/** 创作资产库页面：管理可复用的抽象机制，不录入具体参考素材。 */
import { onMounted, ref, watch } from 'vue'
import { useCreativeLibraryStore } from '@/stores/creative-library'
import type { CreativeLibraryItem } from '@/types/domain'

const store = useCreativeLibraryStore()
const kind = ref<CreativeLibraryItem['kind']>('VIRAL_ELEMENT')
const title = ref('')
const content = ref('')
const groupName = ref('')
const tagsText = ref('')

onMounted(() => void store.load(kind.value))
watch(kind, (nextKind) => void store.load(nextKind))

/** 将逗号分隔输入转为标签数组，实际去重仍由服务端保障。 */
function tags(): string[] {
  return tagsText.value.split(/[,，]/).map((value) => value.trim()).filter(Boolean)
}

async function submit() {
  if (!title.value.trim() || !content.value.trim()) {
    store.error = '请填写名称和内容'
    return
  }
  if (await store.create({ kind: kind.value, title: title.value, content: content.value, group_name: groupName.value, tags: tags() })) {
    title.value = ''; content.value = ''; groupName.value = ''; tagsText.value = ''
  }
}
</script>

<template>
  <section class="page-heading"><div><RouterLink class="muted" to="/">← 返回项目</RouterLink><h1>创作资产库</h1><p>保存开头、冲突和节奏等抽象机制，供后续选题生成引用。</p></div></section>
  <div class="grid">
    <form class="panel stack" @submit.prevent="submit">
      <h2>新增资产</h2>
      <label class="field">资产类型<select v-model="kind"><option value="VIRAL_ELEMENT">爆点元素</option><option value="OPENING_PATTERN">爆款开头</option></select></label>
      <label class="field">名称<input v-model="title" maxlength="160" placeholder="例如：目标与阻碍同屏" /></label>
      <label class="field">抽象机制<textarea v-model="content" placeholder="描述可复用的原创机制，不写具体台词或人物。" /></label>
      <label class="field">分组<input v-model="groupName" placeholder="例如：开场冲突" /></label>
      <label class="field">标签<input v-model="tagsText" placeholder="冲突，开场，悬念" /></label>
      <p v-if="store.error" class="notice error">{{ store.error }}</p><button class="button" :disabled="store.submitting">{{ store.submitting ? '保存中…' : '保存资产' }}</button>
    </form>
    <section class="panel stack"><div class="meta-row"><h2>{{ kind === 'VIRAL_ELEMENT' ? '爆点元素' : '爆款开头' }}</h2><span>{{ store.items.length }} 项</span></div>
      <p v-if="store.loading" class="muted">正在加载…</p><p v-else-if="!store.items.length" class="muted">暂无资产，可在左侧新增。</p>
      <article v-for="item in store.items" :key="item.id" class="panel stack"><div class="meta-row"><strong>{{ item.title }}</strong><button class="button secondary" @click="store.deactivate(item.id)">停用</button></div><p>{{ item.content }}</p><small class="muted">{{ item.group_name || '未分组' }} · {{ item.tags.join(' / ') || '无标签' }}</small></article>
    </section>
  </div>
</template>
