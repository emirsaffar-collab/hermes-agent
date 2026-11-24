// One lazily-created AudioContext for the app's synthesized UI cues (wake
// chime, turn-completion sound, thinking blips). Browsers cap how many
// contexts a page may open, and these cues are short oscillator bursts that
// need no isolation from each other, so they share one instead of each holding
// its own. Nothing closes it — it lives for the window.

let ctx: AudioContext | null = null
let idleSuspendTimer: ReturnType<typeof setTimeout> | null = null
const IDLE_SUSPEND_DELAY_MS = 3_000

function scheduleIdleSuspend(ac: AudioContext) {
  if (idleSuspendTimer !== null) {
    clearTimeout(idleSuspendTimer)
  }

  idleSuspendTimer = setTimeout(() => {
    idleSuspendTimer = null

    if (ac.state === 'running' && typeof ac.suspend === 'function') {
      void ac.suspend().catch(() => undefined)
    }
  }, IDLE_SUSPEND_DELAY_MS)
}

/** The shared AudioContext, or null where WebAudio is unavailable. */
export function getAudioContext(): AudioContext | null {
  if (typeof window === 'undefined') {
    return null
  }

  try {
    if (!ctx) {
      const Ctor =
        window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext

      if (!Ctor) {
        return null
      }

      ctx = new Ctor()
    }

    // Autoplay policies can leave the context suspended until a gesture; a
    // resume() here recovers it once the user has interacted with the window.
    if (ctx.state === 'suspended') {
      void ctx.resume().catch(() => undefined)
    }

    scheduleIdleSuspend(ctx)

    return ctx
  } catch {
    return null
  }
}
