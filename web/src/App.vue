<script setup lang="ts">
/**
 * 顶层应用壳。
 * 页面内容由 router-view 承载；业务请求不得写在这里，确保以后加入登录、权限时
 * 只需扩展布局与路由守卫，不影响工作流页面。
 */
</script>

<template>
  <header class="app-header">
    <RouterLink class="brand" to="/">AI 短剧生产工作流</RouterLink>
    <nav class="top-nav" aria-label="主导航">
      <RouterLink to="/creative-library">创作资产库</RouterLink>
      <RouterLink to="/model-profiles">模型配置</RouterLink>
      <span class="phase">V1</span>
    </nav>
  </header>
  <main class="app-main">
    <RouterView />
  </main>
</template>
