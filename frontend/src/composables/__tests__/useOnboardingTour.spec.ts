import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent } from 'vue'
import type { Config } from 'driver.js'
import { useOnboardingTour } from '../useOnboardingTour'

const state = vi.hoisted(() => ({ index: 0, config: {} as Config }))
const tourDriver = vi.hoisted(() => ({
  getActiveIndex: () => state.index,
  getConfig: () => state.config,
  moveTo: vi.fn((index: number) => { state.index = index }),
  moveNext: vi.fn(() => { state.index++ }),
  movePrevious: vi.fn(() => { state.index-- }),
  isActive: () => true,
  drive: vi.fn(),
  destroy: vi.fn(),
}))

vi.mock('driver.js', () => ({ driver: (config: Config) => {
  state.config = config
  return tourDriver
} }))
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: (key: string) => key }) }))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => ({ user: { id: 1, role: 'user' }, isSimpleMode: false }) }))
vi.mock('@/stores/onboarding', () => ({ useOnboardingStore: () => ({
  getDriverInstance: () => null,
  isDriverActive: () => false,
  setDriverInstance: vi.fn(),
  setControlMethods: vi.fn(),
  clearControlMethods: vi.fn(),
}) }))
vi.mock('@/components/Guide/steps', () => ({
  getUserSteps: () => [
    { popover: { title: 'Welcome' } },
    { element: '[data-tour="key-form-name"]' },
    { element: '[data-tour="key-form-group"]' },
    { element: '[data-tour="key-form-submit"]' },
  ],
}))

let cleanup: () => void
beforeEach(() => {
  vi.clearAllMocks()
  state.index = 0
})
afterEach(() => cleanup?.())

it.each([false, true])('navigates the key group step with auto-selection=%s', async (automatic) => {
  const form = document.createElement('form')
  form.id = 'key-form'
  if (automatic) form.setAttribute('data-auto-selected-group', '')
  document.body.appendChild(form)
  const wrapper = mount(defineComponent({
    setup: () => ({ tour: useOnboardingTour({ autoStart: false }) }),
    template: '<div />',
  }))
  cleanup = () => {
    // The tour survives component unmount by design; emulate its destruction.
    state.config.onDestroyed?.(undefined, {} as never, { config: state.config, state: {} } as never)
    wrapper.unmount()
    form.remove()
  }
  await wrapper.vm.tour.startTour()

  state.index = 1
  await wrapper.vm.tour.nextStep(0)
  expect(state.index).toBe(automatic ? 3 : 2)

  state.index = 3
  state.config.onPrevClick?.(undefined, {} as never, {} as never)
  expect(state.index).toBe(automatic ? 1 : 2)

  state.index = 1
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }))
  expect(state.index).toBe(automatic ? 3 : 2)
  state.index = 3
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true }))
  expect(state.index).toBe(automatic ? 1 : 2)
})
