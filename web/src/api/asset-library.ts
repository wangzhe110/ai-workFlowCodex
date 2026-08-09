/** Phase 4 资产中心接口：只新增版本，不支持覆盖已被项目和工作流冻结的资产。 */
import { request } from './http'
import type { CharacterAsset, CharacterAssetVersion, SceneAsset, SceneAssetVersion } from '@/types/domain'

function post<T>(path: string, payload: object): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

export function getCharacterAssets(): Promise<CharacterAsset[]> {
  return request<CharacterAsset[]>('/asset-library/characters')
}

export function createCharacterAsset(payload: Record<string, unknown>): Promise<CharacterAsset> {
  return post<CharacterAsset>('/asset-library/characters', payload)
}

export function appendCharacterAssetVersion(
  assetId: string,
  payload: Record<string, unknown>,
): Promise<CharacterAssetVersion> {
  return post<CharacterAssetVersion>(`/asset-library/characters/${encodeURIComponent(assetId)}/versions`, payload)
}

export function getSceneAssets(): Promise<SceneAsset[]> {
  return request<SceneAsset[]>('/asset-library/scenes')
}

export function createSceneAsset(payload: Record<string, unknown>): Promise<SceneAsset> {
  return post<SceneAsset>('/asset-library/scenes', payload)
}

export function appendSceneAssetVersion(assetId: string, payload: Record<string, unknown>): Promise<SceneAssetVersion> {
  return post<SceneAssetVersion>(`/asset-library/scenes/${encodeURIComponent(assetId)}/versions`, payload)
}
