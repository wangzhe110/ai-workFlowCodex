/** 应用启动入口：注册全局状态与页面路由。 */
import { createApp } from 'vue'
import { createPinia } from 'pinia'

import App from './App.vue'
import router from './router'
import './style.css'

const app = createApp(App)

// Pinia 统一保存项目及工作流状态，避免页面之间通过全局事件传递数据。
app.use(createPinia())
app.use(router)
app.mount('#app')
