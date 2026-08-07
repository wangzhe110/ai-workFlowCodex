/** 应用启动入口：注册全局状态与页面路由。 */
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'

import App from './App.vue'
import router from './router'
import './style.css'

const app = createApp(App)

// Pinia 统一保存项目及工作流状态，避免页面之间通过全局事件传递数据。
app.use(createPinia())
app.use(router)
// Vue 3 对应的 Element UI 官方版本是 Element Plus；全站页面均可直接使用 el-* 组件。
app.use(ElementPlus, { locale: zhCn })
app.mount('#app')
