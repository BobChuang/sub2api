<template>
  <div class="min-w-0 space-y-1" :aria-busy="refreshing">
    <template v-if="snapshot?.unified">
      <p class="text-[10px] text-gray-500 dark:text-gray-400">
        {{ t('admin.accounts.cursorUsage.unified') }}
      </p>
    </template>
    <template v-else>
      <div
        v-for="window in windows"
        :key="window.key"
        class="flex flex-wrap items-center gap-x-1.5 gap-y-0.5"
      >
        <UsageProgressBar
          :label="window.label"
          :color="window.color"
          :utilization="window.usage?.utilization ?? 0"
          :utilization-unknown="!window.usage"
          detailed
        />
        <time
          v-if="window.usage?.resets_at && window.resetTime"
          :datetime="window.usage.resets_at"
          class="whitespace-nowrap text-[10px] text-gray-500 dark:text-gray-400"
        >
          {{ t('admin.accounts.cursorUsage.resetsAt', { time: window.resetTime }) }}
        </time>
      </div>
    </template>

    <p v-if="snapshot && (snapshot.stale || refreshFailed)" class="text-[10px] text-amber-600 dark:text-amber-400">
      {{ t('admin.accounts.cursorUsage.stale', { time: formatDateTimeToMinute(snapshot.updated_at) }) }}
    </p>
    <p v-else-if="!snapshot && refreshFailed" class="text-[10px] text-amber-600 dark:text-amber-400">
      {{ t('admin.accounts.cursorUsage.unavailable') }}
    </p>
    <p v-else-if="!snapshot && refreshing" class="text-[10px] text-gray-500 dark:text-gray-400">
      {{ t('admin.accounts.cursorUsage.loading') }}
    </p>
    <div class="flex items-center pt-0.5">
      <button
        type="button"
        class="inline-flex items-center gap-0.5 rounded px-1.5 py-0.5 text-[10px] font-medium text-blue-600 transition-colors hover:bg-blue-50 disabled:cursor-not-allowed disabled:opacity-50 dark:text-blue-400 dark:hover:bg-blue-900/30"
        :disabled="refreshing"
        data-testid="cursor-usage-query"
        @click="refreshUsage(true)"
      >
        <svg
          class="h-2.5 w-2.5"
          :class="{ 'animate-spin': refreshing }"
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
          aria-hidden="true"
        >
          <path
            stroke-linecap="round"
            stroke-linejoin="round"
            stroke-width="2"
            d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"
          />
        </svg>
        {{ t('admin.accounts.usageWindow.activeQuery') }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowRef } from 'vue'
import { useIntervalFn } from '@vueuse/core'
import { useI18n } from 'vue-i18n'
import { adminAPI } from '@/api/admin'
import type { CursorUsageSnapshot } from '@/types'
import { formatDateTime, formatDateTimeToMinute } from '@/utils/format'
import UsageProgressBar from './UsageProgressBar.vue'

const props = defineProps<{ accountId: number }>()
const { t } = useI18n()
const refreshInterval = 2 * 60_000
const snapshot = shallowRef<CursorUsageSnapshot>()
const refreshing = ref(false)
const refreshFailed = ref(false)
let lastAttempt: number | undefined

const windows = computed(() => ([
  { key: 'cursor_models', label: t('admin.accounts.cursorUsage.cursorModels'), color: 'indigo', usage: snapshot.value?.cursor_models },
  { key: 'other_models', label: t('admin.accounts.cursorUsage.otherModels'), color: 'emerald', usage: snapshot.value?.other_models },
  { key: 'grok_bot', label: t('admin.accounts.cursorUsage.grokBot'), color: 'amber', usage: snapshot.value?.grok_bot }
] as const).map(window => ({
  ...window,
  resetTime: formatDateTime(window.usage?.resets_at, {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23'
  })
})))

const refreshUsage = async (force = false) => {
  if (document.hidden || refreshing.value) return
  lastAttempt = Date.now()
  refreshing.value = true

  try {
    const response = force
      ? await adminAPI.accounts.getUsage(props.accountId, 'active', true)
      : await adminAPI.accounts.getUsage(props.accountId)
    refreshFailed.value = !response.cursor_usage
    if (response.cursor_usage) snapshot.value = response.cursor_usage
  } catch {
    refreshFailed.value = true
  } finally {
    refreshing.value = false
  }
}

const { pause, resume } = useIntervalFn(refreshUsage, refreshInterval, { immediate: false })
const updatePageVisibility = () => {
  if (document.hidden) {
    pause()
    return
  }
  resume()
  if (lastAttempt === undefined || Date.now() - lastAttempt >= refreshInterval) void refreshUsage()
}

onMounted(() => {
  document.addEventListener('visibilitychange', updatePageVisibility)
  updatePageVisibility()
})
onUnmounted(() => document.removeEventListener('visibilitychange', updatePageVisibility))
</script>
