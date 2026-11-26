import { randomUuid } from '@hermes/shared'
import { atom } from 'nanostores'

import type { HermesReviewFile, HermesReviewScope } from '@/global'

export type DiffCommentSide = 'new' | 'old'

export interface DiffReviewComment {
  id: string
  /** Canonical repo/worktree root (trimmed). */
  repoCwd: string
  /** Repo-relative path matching HermesReviewFile.path. */
  filePath: string
  scope: HermesReviewScope
  /** 'new' for add/context lines, 'old' for remove lines. */
  side: DiffCommentSide
  /** 1-based inclusive line number. */
  startLine: number
  /** 1-based inclusive line number (>= startLine). */
  endLine: number
  /** Unified diff hunk header (e.g. "@@ -42,7 +42,9 @@") for stale-diff anchoring. */
  hunkHeader: string
  /** Verbatim diff lines in the commented range (capped at MAX_SNIPPET_LINES). */
  snippet: string[]
  /** User review note. */
  body: string
  createdAt: number
  /** True when the file disappeared from the live review file list. */
  stale?: boolean
}

export interface DraftAnchor {
  repoCwd: string
  filePath: string
  scope: HermesReviewScope
  side: DiffCommentSide
  startLine: number
  endLine: number
  hunkHeader: string
  snippet: string[]
}

export interface NewReviewCommentInput {
  repoCwd: string
  filePath: string
  scope: HermesReviewScope
  side: DiffCommentSide
  startLine: number
  endLine: number
  hunkHeader: string
  snippet: readonly string[]
  body: string
}

export const REVIEW_COMMENTS_STORAGE_KEY = 'hermes.desktop.reviewComments.v1'
export const MAX_SNIPPET_LINES = 12

type ReviewCommentsByRepo = Record<string, DiffReviewComment[]>

export function normalizeRepoKey(repoCwd: null | string | undefined): string {
  return repoCwd?.trim() ?? ''
}

function isCommentRecord(value: unknown): value is DiffReviewComment {
  if (!value || typeof value !== 'object') {
    return false
  }

  const candidate = value as Record<string, unknown>

  return (
    typeof candidate.id === 'string' &&
    typeof candidate.repoCwd === 'string' &&
    typeof candidate.filePath === 'string' &&
    (candidate.scope === 'uncommitted' || candidate.scope === 'lastTurn' || candidate.scope === 'branch') &&
    (candidate.side === 'new' || candidate.side === 'old') &&
    typeof candidate.startLine === 'number' &&
    typeof candidate.endLine === 'number' &&
    typeof candidate.hunkHeader === 'string' &&
    Array.isArray(candidate.snippet) &&
    candidate.snippet.every(line => typeof line === 'string') &&
    typeof candidate.body === 'string' &&
    typeof candidate.createdAt === 'number'
  )
}

function readPersistedComments(): ReviewCommentsByRepo {
  if (typeof window === 'undefined') {
    return {}
  }

  try {
    const raw = window.sessionStorage.getItem(REVIEW_COMMENTS_STORAGE_KEY)

    if (!raw) {
      return {}
    }

    const parsed: unknown = JSON.parse(raw)

    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return {}
    }

    const out: ReviewCommentsByRepo = {}

    for (const [key, list] of Object.entries(parsed as Record<string, unknown>)) {
      const normalized = normalizeRepoKey(key)

      if (!normalized || !Array.isArray(list)) {
        continue
      }

      const valid = list.filter(isCommentRecord)

      if (valid.length > 0) {
        out[normalized] = valid
      }
    }

    return out
  } catch {
    return {}
  }
}

function persistComments(map: ReviewCommentsByRepo): void {
  if (typeof window === 'undefined') {
    return
  }

  try {
    if (Object.keys(map).length === 0) {
      window.sessionStorage.removeItem(REVIEW_COMMENTS_STORAGE_KEY)
    } else {
      window.sessionStorage.setItem(REVIEW_COMMENTS_STORAGE_KEY, JSON.stringify(map))
    }
  } catch {
    // sessionStorage unavailable or quota exceeded — keep in-memory state working.
  }
}

/** Inline diff review comments keyed by canonical repo/worktree cwd. */
export const $reviewComments = atom<ReviewCommentsByRepo>(readPersistedComments())

/** Currently open inline comment composer anchor inside PreviewDiffLines. */
export const $activeDraftAnchor = atom<DraftAnchor | null>(null)

$reviewComments.listen(next => {
  persistComments(next)
})

/** Re-hydrate store from sessionStorage (primarily for tests). */
export function reloadReviewCommentsFromStorage(): void {
  $reviewComments.set(readPersistedComments())
}

export function commentsForRepo(repoCwd: null | string | undefined): DiffReviewComment[] {
  const key = normalizeRepoKey(repoCwd)

  if (!key) {
    return []
  }

  return $reviewComments.get()[key] ?? []
}

export function commentsForFile(repoCwd: null | string | undefined, filePath: string): DiffReviewComment[] {
  return commentsForRepo(repoCwd).filter(comment => comment.filePath === filePath)
}

export function openDraftAnchor(anchor: DraftAnchor): void {
  const key = normalizeRepoKey(anchor.repoCwd)

  if (!key) {
    return
  }

  const startLine = Math.min(anchor.startLine, anchor.endLine)
  const endLine = Math.max(anchor.startLine, anchor.endLine)

  $activeDraftAnchor.set({
    ...anchor,
    endLine,
    repoCwd: key,
    snippet: anchor.snippet.slice(0, MAX_SNIPPET_LINES),
    startLine
  })
}

export function closeDraftAnchor(): void {
  $activeDraftAnchor.set(null)
}

export function addReviewComment(input: NewReviewCommentInput): DiffReviewComment | null {
  const repoCwd = normalizeRepoKey(input.repoCwd)
  const body = input.body.trim()

  if (!repoCwd || !body) {
    return null
  }

  const startLine = Math.min(input.startLine, input.endLine)
  const endLine = Math.max(input.startLine, input.endLine)

  const comment: DiffReviewComment = {
    body,
    createdAt: Date.now(),
    endLine,
    filePath: input.filePath,
    hunkHeader: input.hunkHeader,
    id: randomUuid(),
    repoCwd,
    scope: input.scope,
    side: input.side,
    snippet: input.snippet.slice(0, MAX_SNIPPET_LINES),
    startLine
  }

  const prev = $reviewComments.get()
  const existing = prev[repoCwd] ?? []

  $reviewComments.set({
    ...prev,
    [repoCwd]: [...existing, comment]
  })

  const draft = $activeDraftAnchor.get()

  if (draft && draft.repoCwd === repoCwd && draft.filePath === input.filePath) {
    $activeDraftAnchor.set(null)
  }

  return comment
}

export function updateReviewComment(repoCwd: string, id: string, body: string): DiffReviewComment | null {
  const key = normalizeRepoKey(repoCwd)
  const trimmed = body.trim()

  if (!key || !trimmed) {
    return null
  }

  const prev = $reviewComments.get()
  const list = prev[key]

  if (!list) {
    return null
  }

  let updated: DiffReviewComment | null = null

  const nextList = list.map(comment => {
    if (comment.id !== id) {
      return comment
    }

    updated = { ...comment, body: trimmed }

    return updated
  })

  if (!updated) {
    return null
  }

  $reviewComments.set({ ...prev, [key]: nextList })

  return updated
}

export function deleteReviewComment(repoCwd: string, id: string): boolean {
  const key = normalizeRepoKey(repoCwd)

  if (!key) {
    return false
  }

  const prev = $reviewComments.get()
  const list = prev[key]

  if (!list) {
    return false
  }

  const nextList = list.filter(comment => comment.id !== id)

  if (nextList.length === list.length) {
    return false
  }

  if (nextList.length === 0) {
    const rest = { ...prev }

    delete rest[key]
    $reviewComments.set(rest)
  } else {
    $reviewComments.set({ ...prev, [key]: nextList })
  }

  return true
}

export function clearRepoComments(repoCwd: string): DiffReviewComment[] {
  const key = normalizeRepoKey(repoCwd)

  if (!key) {
    return []
  }

  const prev = $reviewComments.get()
  const removed = prev[key] ?? []

  if (removed.length === 0) {
    return []
  }

  const rest = { ...prev }

  delete rest[key]
  $reviewComments.set(rest)

  const draft = $activeDraftAnchor.get()

  if (draft && draft.repoCwd === key) {
    $activeDraftAnchor.set(null)
  }

  return removed
}

export function restoreRepoComments(repoCwd: string, comments: readonly DiffReviewComment[]): void {
  const key = normalizeRepoKey(repoCwd)

  if (!key || comments.length === 0) {
    return
  }

  const prev = $reviewComments.get()
  const existing = prev[key] ?? []
  const existingIds = new Set(existing.map(c => c.id))
  const toAdd = comments.filter(c => !existingIds.has(c.id))

  if (toAdd.length === 0) {
    return
  }

  $reviewComments.set({
    ...prev,
    [key]: [...existing, ...toAdd]
  })
}

/**
 * Mark comments whose file disappeared from the live review list as `stale`,
 * without silently deleting them (so the user sees a subtle stale badge).
 * If a file reappears, clears the stale flag.
 */
export function markStaleComments(repoCwd: string, liveFiles: readonly Pick<HermesReviewFile, 'path'>[]): void {
  const key = normalizeRepoKey(repoCwd)

  if (!key) {
    return
  }

  const prev = $reviewComments.get()
  const list = prev[key]

  if (!list || list.length === 0) {
    return
  }

  const livePaths = new Set(liveFiles.map(file => file.path))
  let changed = false

  const nextList = list.map(comment => {
    const nextStale = !livePaths.has(comment.filePath)

    if (Boolean(comment.stale) === nextStale) {
      return comment
    }

    changed = true

    return nextStale ? { ...comment, stale: true } : { ...comment, stale: false }
  })

  if (changed) {
    $reviewComments.set({ ...prev, [key]: nextList })
  }
}

/**
 * Remove comments whose file is no longer in `liveFiles` (e.g. after reverting
 * or committing). Returns the number of pruned comments.
 */
export function pruneResolvedComments(repoCwd: string, liveFiles: readonly Pick<HermesReviewFile, 'path'>[]): number {
  const key = normalizeRepoKey(repoCwd)

  if (!key) {
    return 0
  }

  const prev = $reviewComments.get()
  const list = prev[key]

  if (!list || list.length === 0) {
    return 0
  }

  const livePaths = new Set(liveFiles.map(file => file.path))
  const remaining = list.filter(comment => livePaths.has(comment.filePath))
  const removedCount = list.length - remaining.length

  if (removedCount === 0) {
    return 0
  }

  if (remaining.length === 0) {
    const rest = { ...prev }

    delete rest[key]
    $reviewComments.set(rest)
  } else {
    $reviewComments.set({ ...prev, [key]: remaining })
  }

  return removedCount
}
