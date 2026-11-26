import { beforeEach, describe, expect, it, vi } from 'vitest'

const LEGACY_VISIBLE_KEY = 'hermes.desktop.statusbarVisible'

const loadStore = () => import('./statusbar-prefs')

describe('statusbar whole-bar visibility', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.resetModules()
  })

  it('shows the bar on a fresh install and for installs that hid it under the v1 key', async () => {
    window.localStorage.setItem(LEGACY_VISIBLE_KEY, 'false')

    const { $statusbarVisible } = await loadStore()

    expect($statusbarVisible.get()).toBe(true)
  })

  it('still honours a hide made after the update', async () => {
    const first = await loadStore()

    first.toggleStatusbarVisible()
    expect(first.$statusbarVisible.get()).toBe(false)

    vi.resetModules()
    const reloaded = await loadStore()

    expect(reloaded.$statusbarVisible.get()).toBe(false)
  })
})

describe('statusbar hidden items', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.resetModules()
  })

  it('surfaces the approval pill for installs that hid it under the v1 defaults, keeping their other choices', async () => {
    window.localStorage.setItem(
      'hermes.desktop.statusbarHidden',
      JSON.stringify(['approval-mode', 'context-usage', 'cache-hit-rate', 'cron', 'gateway-health'])
    )

    const { $statusbarHiddenIds } = await loadStore()

    expect($statusbarHiddenIds.get()).toEqual(['cron', 'gateway-health'])
  })

  it('surfaces context-usage and cache-hit-rate for v2 installs while keeping custom hides', async () => {
    window.localStorage.setItem(
      'hermes.desktop.statusbarHidden.v2',
      JSON.stringify(['context-usage', 'cache-hit-rate', 'cron', 'session-cost'])
    )

    const { $statusbarHiddenIds } = await loadStore()

    expect($statusbarHiddenIds.get()).toEqual(['cron', 'session-cost'])
  })

  it('keeps context-usage, cache-hit-rate, and session-cost visible on a fresh install', async () => {
    const { STATUSBAR_HIDDEN_BY_DEFAULT, $statusbarHiddenIds } = await loadStore()

    expect(STATUSBAR_HIDDEN_BY_DEFAULT).not.toContain('context-usage')
    expect(STATUSBAR_HIDDEN_BY_DEFAULT).not.toContain('cache-hit-rate')
    expect(STATUSBAR_HIDDEN_BY_DEFAULT).not.toContain('session-cost')
    expect($statusbarHiddenIds.get()).not.toContain('context-usage')
    expect($statusbarHiddenIds.get()).not.toContain('cache-hit-rate')
    expect($statusbarHiddenIds.get()).not.toContain('session-cost')
  })

  it('still honours hiding the approval pill after the update', async () => {
    const first = await loadStore()

    first.setStatusbarItemVisible('approval-mode', false)

    vi.resetModules()
    const reloaded = await loadStore()

    expect(reloaded.$statusbarHiddenIds.get()).toContain('approval-mode')
  })
})
