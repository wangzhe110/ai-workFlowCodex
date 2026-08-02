/**
 * 前端 HTTP 基础设施。
 * API Base URL 可以由部署环境 VITE_API_BASE_URL 覆盖；该变量只允许放公共地址，
 * 绝不能放第三方模型 Key 或服务器密钥。
 */
// 默认同源访问：本地由 Vite 代理到 FastAPI，生产由网关/反向代理转发。
// 这避免浏览器硬编码 localhost:8000，也允许前后端使用不同部署域名。
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? '/api/v1'

/** 将后端返回的同源下载路径转换为部署环境可访问的链接。 */
export function apiDownloadUrl(path: string): string {
  if (path.startsWith('https://') || path.startsWith('http://')) return path
  if (!API_BASE_URL.startsWith('http')) return path
  return `${new URL(API_BASE_URL).origin}${path}`
}

export class ApiError extends Error {
  /** @param status HTTP 状态码，供页面选择提示或重试策略。 */
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

/**
 * 处理所有 API 响应。
 * 后端约定错误响应包含 detail；其余情况提供稳定的兜底文案，避免页面散落解析逻辑。
 */
export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, init)
  const contentType = response.headers.get('content-type') ?? ''
  const body = contentType.includes('application/json')
    ? await response.json()
    : await response.text()

  if (!response.ok) {
    const message = typeof body === 'object' && body !== null && 'detail' in body
      ? String(body.detail)
      : `请求失败（${response.status}）`
    throw new ApiError(response.status, message)
  }

  return body as T
}
