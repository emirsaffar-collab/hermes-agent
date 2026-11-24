interface WindowStatePayload {
  isMinimized?: boolean
  isVisible?: boolean
}

interface BatteryManagerLike {
  addEventListener?: (type: string, listener: () => void) => void
  charging?: boolean
  removeEventListener?: (type: string, listener: () => void) => void
}

type BatteryNavigator = Navigator & {
  getBattery?: () => Promise<BatteryManagerLike>
}

export const RENDERER_ANIMATIONS_PAUSED_ATTRIBUTE = 'data-renderer-animations-paused'

export function createRendererLoopPauseController(
  onChange: () => void,
  { pauseOnBattery = false, pauseWhenUnfocused = false } = {}
) {
  let windowPaused = false
  let windowFocused = document.hasFocus()
  let onBattery = false
  let disposed = false
  let batteryRef: BatteryManagerLike | null = null
  let offDesktopBattery: (() => void) | undefined

  const setBatteryState = (next: boolean) => {
    if (disposed || onBattery === next) {
      return
    }

    onBattery = next
    onChange()
  }

  const onVisibilityChange = () => onChange()

  const onBlur = () => {
    if (windowFocused) {
      windowFocused = false
      onChange()
    }
  }

  const onFocus = () => {
    if (!windowFocused) {
      windowFocused = true
      onChange()
    }
  }

  const onBatteryChange = () => {
    if (!batteryRef) {
      return
    }

    setBatteryState(batteryRef.charging === false)
  }

  const offWindowState = window.hermesDesktop?.onWindowStateChanged?.((payload: WindowStatePayload) => {
    const next = payload?.isMinimized === true || payload?.isVisible === false

    if (windowPaused === next) {
      return
    }

    windowPaused = next
    onChange()
  })

  document.addEventListener('visibilitychange', onVisibilityChange)

  if (pauseWhenUnfocused) {
    window.addEventListener('blur', onBlur)
    window.addEventListener('focus', onFocus)
  }

  if (pauseOnBattery) {
    offDesktopBattery = window.hermesDesktop?.onBatteryChanged?.((next: boolean) => {
      setBatteryState(Boolean(next))
    })

    if (typeof window.hermesDesktop?.getOnBattery === 'function') {
      void Promise.resolve(window.hermesDesktop.getOnBattery())
        .then(next => {
          setBatteryState(Boolean(next))
        })
        .catch(() => undefined)
    }

    if (typeof navigator !== 'undefined') {
      const nav = navigator as BatteryNavigator

      if (typeof nav.getBattery === 'function') {
        void nav
          .getBattery()
          .then(battery => {
            if (disposed || !battery) {
              return
            }

            batteryRef = battery
            onBatteryChange()
            battery.addEventListener?.('chargingchange', onBatteryChange)
          })
          .catch(() => undefined)
      }
    }
  }

  return {
    dispose: () => {
      disposed = true
      document.removeEventListener('visibilitychange', onVisibilityChange)
      window.removeEventListener('blur', onBlur)
      window.removeEventListener('focus', onFocus)
      batteryRef?.removeEventListener?.('chargingchange', onBatteryChange)
      batteryRef = null
      offDesktopBattery?.()
      offWindowState?.()
    },
    isPaused: () =>
      document.visibilityState === 'hidden' ||
      (pauseWhenUnfocused && !windowFocused) ||
      (pauseOnBattery && onBattery) ||
      windowPaused
  }
}

/**
 * Mirrors the main window's observability onto :root so continuous decorative
 * CSS animations can sleep with the JS renderer loops. The caller owns the
 * returned cleanup; overlay windows intentionally do not install this state.
 */
export function installRendererAnimationPauseState(): () => void {
  const root = document.documentElement
  let controller: ReturnType<typeof createRendererLoopPauseController>

  const sync = () => root.toggleAttribute(RENDERER_ANIMATIONS_PAUSED_ATTRIBUTE, controller.isPaused())

  controller = createRendererLoopPauseController(sync, { pauseOnBattery: true })
  sync()

  return () => {
    controller.dispose()
    root.removeAttribute(RENDERER_ANIMATIONS_PAUSED_ATTRIBUTE)
  }
}
