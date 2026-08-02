/// <reference types="vite/client" />

/**
 * Vite 会在构建期替换 VITE_* 变量。
 * 这里只允许声明可公开的前端运行配置，例如 API 基地址；模型密钥绝不能进入该类型。
 */
interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
