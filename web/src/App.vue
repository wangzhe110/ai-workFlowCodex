<script setup lang="ts">
/**
 * 顶层应用壳。
 * 页面内容由 router-view 承载；业务请求不得写在这里，确保以后加入登录、权限时
 * 只需扩展布局与路由守卫，不影响工作流页面。
 */
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()
/** 仅高亮一级导航；项目内的生产台仍通过项目页面自身展示当前位置。 */
const activeNavigation = computed(() => {
  if (route.path.startsWith('/asset-library')) return '/asset-library'
  if (route.path.startsWith('/creative-library')) return '/creative-library'
  if (route.path.startsWith('/model-profiles')) return '/model-profiles'
  if (route.path.startsWith('/model-quality')) return '/model-quality'
  if (route.path.startsWith('/prompt-templates')) return '/prompt-templates'
  return ''
})
</script>

<template>
  <el-container class="app-shell">
    <el-header class="app-header">
      <RouterLink class="brand" to="/">LemonFlow</RouterLink>
      <el-menu class="top-nav" mode="horizontal" :ellipsis="false" :default-active="activeNavigation" router>
        <el-menu-item index="/asset-library">资产中心</el-menu-item>
        <el-menu-item index="/creative-library">创作资产库</el-menu-item>
        <el-menu-item index="/model-profiles">模型配置</el-menu-item>
        <el-menu-item index="/model-quality">质量报表</el-menu-item>
        <el-menu-item index="/prompt-templates">Prompt 模板</el-menu-item>
      </el-menu>
      <el-tag class="phase" type="info" effect="dark">V1</el-tag>
    </el-header>
    <el-main class="app-main"><RouterView /></el-main>
  </el-container>
</template>
