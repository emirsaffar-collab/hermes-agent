import { afterEach, describe, expect, it, vi } from 'vitest'

import { installRendererAnimationPauseState, RENDERER_ANIMATIONS_PAUSED_ATTRIBUTE } from './renderer-loop-pause'

describe('installRendererAnimationPauseState', () => {
  afterEach(() => {
    document.documentElement.removeAttribute(RENDERER_ANIMATIONS_PAUSED_ATTRIBUTE)
    vi.restoreAllMocks()
  })

  it('keeps visible animations running across blur and focus, and cleans up its root state', () => {
    let focused = true
    vi.spyOn(document, 'hasFocus').mockImplementation(() => focused)

    const dispose = installRendererAnimationPauseState()
    expect(document.documentElement.hasAttribute(RENDERER_ANIMATIONS_PAUSED_ATTRIBUTE)).toBe(false)

    focused = false
    window.dispatchEvent(new Event('blur'))
    expect(document.documentElement.hasAttribute(RENDERER_ANIMATIONS_PAUSED_ATTRIBUTE)).toBe(false)

    focused = true
    window.dispatchEvent(new Event('focus'))
    expect(document.documentElement.hasAttribute(RENDERER_ANIMATIONS_PAUSED_ATTRIBUTE)).toBe(false)

    focused = false
    window.dispatchEvent(new Event('blur'))
    dispose()
    expect(document.documentElement.hasAttribute(RENDERER_ANIMATIONS_PAUSED_ATTRIBUTE)).toBe(false)
  })

  it('pauses continuous decorative animations while on battery power and resumes on AC', async () => {
    const listeners = new Set<() => void>()
    const battery = {
      charging: false,
      addEventListener: vi.fn((_type: string, cb: () => void) => listeners.add(cb)),
      removeEventListener: vi.fn((_type: string, cb: () => void) => listeners.delete(cb))
    }
    const nav = navigator as Navigator & { getBattery?: () => Promise<typeof battery> }
    const origGetBattery = nav.getBattery
    nav.getBattery = vi.fn(async () => battery)

    try {
      const dispose = installRendererAnimationPauseState()
      await Promise.resolve()
      expect(document.documentElement.hasAttribute(RENDERER_ANIMATIONS_PAUSED_ATTRIBUTE)).toBe(true)

      battery.charging = true
      for (const cb of listeners) cb()
      expect(document.documentElement.hasAttribute(RENDERER_ANIMATIONS_PAUSED_ATTRIBUTE)).toBe(false)

      battery.charging = false
      for (const cb of listeners) cb()
      expect(document.documentElement.hasAttribute(RENDERER_ANIMATIONS_PAUSED_ATTRIBUTE)).toBe(true)

      dispose()
      expect(document.documentElement.hasAttribute(RENDERER_ANIMATIONS_PAUSED_ATTRIBUTE)).toBe(false)
    } finally {
      nav.getBattery = origGetBattery
    }
  })
})
