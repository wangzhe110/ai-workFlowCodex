/** 创作资产库状态：素材只保存抽象机制，不保存参考视频的具体内容。 */
import { defineStore } from 'pinia'
import {
  createCreativeLibraryItem,
  deactivateCreativeLibraryItem,
  getCreativeLibrary,
  type CreativeLibraryItemPayload,
} from '@/api/creative-library'
import type { CreativeLibraryItem } from '@/types/domain'

export const useCreativeLibraryStore = defineStore('creative-library', {
  state: () => ({
    items: [] as CreativeLibraryItem[],
    loading: false,
    submitting: false,
    error: '',
  }),
  actions: {
    /** 将接口错误归一化，避免页面各自解析异常。 */
    setError(error: unknown) {
      this.error = error instanceof Error ? error.message : '操作失败，请稍后重试'
    },

    async load(kind: CreativeLibraryItem['kind']) {
      this.loading = true
      this.error = ''
      try {
        this.items = await getCreativeLibrary(kind)
      } catch (error) {
        this.setError(error)
      } finally {
        this.loading = false
      }
    },

    async create(payload: CreativeLibraryItemPayload): Promise<boolean> {
      this.submitting = true
      this.error = ''
      try {
        const item = await createCreativeLibraryItem(payload)
        this.items = [item, ...this.items]
        return true
      } catch (error) {
        this.setError(error)
        return false
      } finally {
        this.submitting = false
      }
    },

    async deactivate(itemId: string) {
      this.error = ''
      try {
        await deactivateCreativeLibraryItem(itemId)
        this.items = this.items.filter((item) => item.id !== itemId)
      } catch (error) {
        this.setError(error)
      }
    },
  },
})
