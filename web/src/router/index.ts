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
    { path: '/model-profiles', name: 'model-profiles', component: () => import('@/views/ModelProfilesView.vue') },
    {
      path: '/creative-library',
      name: 'creative-library',
      component: () => import('@/views/CreativeLibraryView.vue'),
    },
  ],
})

export default router
