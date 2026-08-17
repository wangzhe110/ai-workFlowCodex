import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

/**
 * 前端构建配置。
 *
 * @ 保证组件导入路径稳定，避免页面深层目录出现冗长相对路径。
 */
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    // 仅开发服务器使用。生产环境由 Nginx、Traefik 或云网关提供同源 /api 转发。
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      // 与生产 Nginx 保持一致：本地受控图片/视频均通过 API 静态挂载读取。
      '/media': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
