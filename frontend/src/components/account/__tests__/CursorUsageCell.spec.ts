import { flushPromises, mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import en from '@/i18n/locales/en'
import type { CursorUsageSnapshot } from '@/types'
import CursorUsageCell from '../CursorUsageCell.vue'

const { getUsage } = vi.hoisted(() => ({ getUsage: vi.fn() }))
vi.mock('@/api/admin', () => ({ adminAPI: { accounts: { getUsage } } }))

const refreshInterval = 2 * 60_000
const snapshot: CursorUsageSnapshot = {
  cursor_models: { utilization: 0.01111, resets_at: '2026-11-22T06:03:52Z' },
  other_models: { utilization: 0.35555, resets_at: '2026-11-22T06:03:52Z' },
  grok_bot: { utilization: 0.314264, resets_at: '2026-11-26T14:50:30Z' },
  updated_at: '2026-11-20T13:10:00Z'
}

const mountCell = () => mount(CursorUsageCell, {
  props: { accountId: 410 },
  global: { plugins: [createI18n({ legacy: false, locale: 'en', messages: { en } })] }
})

describe('CursorUsageCell', () => {
  beforeEach(() => {
    getUsage.mockReset()
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('reads real decimal percentages and shows a reset time for each quota', async () => {
    vi.spyOn(document, 'hidden', 'get').mockReturnValue(false)
    getUsage.mockResolvedValueOnce({ cursor_usage: snapshot })
    const wrapper = mountCell()
    try {
      await flushPromises()
      expect(getUsage).toHaveBeenCalledWith(410)
      expect(wrapper.text()).toContain('Cursor Models')
      expect(wrapper.text()).toContain('Other Models')
      expect(wrapper.text()).toContain('Grok Bot')
      expect(wrapper.text()).toContain('0.01%')
      expect(wrapper.text()).toContain('0.36%')
      expect(wrapper.text()).toContain('0.31%')
      expect(wrapper.findAll('time').map(time => time.attributes('datetime'))).toEqual([
        snapshot.cursor_models?.resets_at,
        snapshot.other_models?.resets_at,
        snapshot.grok_bot?.resets_at
      ])
    } finally {
      wrapper.unmount()
    }
  })

  it('forces a fresh query on click and keeps the last measurement after a failed retry', async () => {
    vi.spyOn(document, 'hidden', 'get').mockReturnValue(false)
    const refreshed: CursorUsageSnapshot = {
      ...snapshot,
      cursor_models: { utilization: 14.2 },
      updated_at: '2026-11-20T13:11:00Z'
    }
    getUsage
      .mockResolvedValueOnce({ cursor_usage: snapshot })
      .mockImplementationOnce(() => new Promise(resolve => {
        setTimeout(() => resolve({ cursor_usage: refreshed }), 100)
      }))
      .mockRejectedValueOnce(new Error('Unavailable'))
    const wrapper = mountCell()
    try {
      await flushPromises()
      const queryButton = wrapper.get<HTMLButtonElement>('[data-testid="cursor-usage-query"]')
      expect(queryButton.text()).toBe('Query')

      await queryButton.trigger('click')
      expect(getUsage).toHaveBeenNthCalledWith(2, 410, 'active', true)
      expect(queryButton.element.disabled).toBe(true)
      expect(queryButton.get('svg').classes()).toContain('animate-spin')

      await vi.advanceTimersByTimeAsync(100)
      expect(queryButton.element.disabled).toBe(false)
      expect(wrapper.text()).toContain('14.2%')

      await queryButton.trigger('click')
      await flushPromises()
      expect(getUsage).toHaveBeenNthCalledWith(3, 410, 'active', true)
      expect(queryButton.element.disabled).toBe(false)
      expect(wrapper.text()).toContain('14.2%')
      expect(wrapper.text()).toContain('Refresh pending.')
    } finally {
      wrapper.unmount()
    }
  })

  it('pauses while hidden and preserves the last measurement on failure', async () => {
    const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(false)
    getUsage.mockResolvedValueOnce({ cursor_usage: snapshot }).mockRejectedValueOnce(new Error('Unavailable'))
    const wrapper = mountCell()
    try {
      await flushPromises()
      hidden.mockReturnValue(true)
      document.dispatchEvent(new Event('visibilitychange'))
      await vi.advanceTimersByTimeAsync(refreshInterval)
      expect(getUsage).toHaveBeenCalledTimes(1)

      hidden.mockReturnValue(false)
      document.dispatchEvent(new Event('visibilitychange'))
      await flushPromises()
      expect(getUsage).toHaveBeenCalledTimes(2)
      expect(getUsage).toHaveBeenLastCalledWith(410)
      expect(wrapper.text()).toContain('0.36%')
      expect(wrapper.text()).toContain('Refresh pending.')

      getUsage.mockResolvedValueOnce({
        cursor_usage: { cursor_models: { utilization: 14.2 }, updated_at: snapshot.updated_at, stale: true }
      })
      await vi.advanceTimersByTimeAsync(refreshInterval)
      await flushPromises()
      expect(getUsage).toHaveBeenCalledTimes(3)
      expect(getUsage).toHaveBeenLastCalledWith(410)
      expect(wrapper.text()).toContain('14.2%')
      expect(wrapper.text()).toContain('—')
      expect(wrapper.text()).toContain('Refresh pending.')
    } finally {
      wrapper.unmount()
    }
  })
})
