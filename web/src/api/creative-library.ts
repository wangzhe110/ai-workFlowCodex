/** 创作资产库 API：浏览器仅调用平台接口，不感知模型或素材私密信息。 */
import { request } from './http'
import type { CreativeLibraryItem } from '@/types/domain'

export interface CreativeLibraryItemPayload {
  kind: CreativeLibraryItem['kind']
  title: string
  content: string
  group_name?: string | null
  tags: string[]
}

/** 查询指定类型的启用资产。 */
export function getCreativeLibrary(kind: CreativeLibraryItem['kind']): Promise<CreativeLibraryItem[]> {
  return request<CreativeLibraryItem[]>(`/creative-library?kind=${kind}`)
}

/** 新增人工审核过的抽象机制。 */
export function createCreativeLibraryItem(payload: CreativeLibraryItemPayload): Promise<CreativeLibraryItem> {
  return request<CreativeLibraryItem>('/creative-library', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

/** 软停用资产，历史工作流快照不受影响。 */
export function deactivateCreativeLibraryItem(itemId: string): Promise<void> {
  return request<void>(`/creative-library/${itemId}`, { method: 'DELETE' })
}
