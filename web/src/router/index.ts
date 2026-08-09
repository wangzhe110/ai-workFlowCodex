/** 页面路由定义：懒加载工作台页面，避免项目列表首次加载不必要的代码。 */
import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      name: 'projects',
      component: () => import('@/views/ProjectsView.vue'),
    },
    {
      path: '/projects/:projectId',
      name: 'project-workbench',
      component: () => import('@/views/ProductionWorkbenchView.vue'),
      props: true,
    },
    {
      path: '/projects/:projectId/trace',
      name: 'project-production-trace',
      component: () => import('@/views/V1ProductionTraceView.vue'),
      props: true,
    },
    {
      path: '/projects/:projectId/legacy',
      name: 'legacy-project-workbench',
      component: () => import('@/views/ProjectWorkbenchView.vue'),
      props: true,
    },
    {
      path: '/projects/:projectId/topics',
      name: 'project-topics',
      component: () => import('@/views/ProjectTopicsView.vue'),
      props: true,
    },
    { path: '/projects/:projectId/story', name: 'project-story', component: () => import('@/views/ProjectStoryView.vue'), props: true },
    { path: '/projects/:projectId/storyboard', name: 'project-storyboard', component: () => import('@/views/ProjectStoryboardView.vue'), props: true },
    { path: '/projects/:projectId/images', name: 'project-images', component: () => import('@/views/ProjectImagesView.vue'), props: true },
    { path: '/projects/:projectId/videos', name: 'project-videos', component: () => import('@/views/ProjectVideosView.vue'), props: true },
    // V1 主入口只展示“模型槽位”，避免旧流程步骤干扰正式生产链路。
    { path: '/model-profiles', name: 'model-profiles', component: () => import('@/views/V1ModelCenterView.vue') },
    { path: '/model-quality', name: 'model-quality', component: () => import('@/views/V1QualityReportView.vue') },
    { path: '/prompt-templates', name: 'prompt-templates', component: () => import('@/views/V1PromptTemplatesView.vue') },
    { path: '/model-profiles/legacy', name: 'legacy-model-profiles', component: () => import('@/views/ModelProfilesView.vue') },
    {
      path: '/creative-library',
      name: 'creative-library',
      component: () => import('@/views/CreativeLibraryView.vue'),
    },
    // Phase 4：跨项目角色、场景资产库。项目采用仍会回到生产台走原有锁图审核。
    { path: '/asset-library', name: 'asset-library', component: () => import('@/views/AssetLibraryView.vue') },
  ],
})

export default router
