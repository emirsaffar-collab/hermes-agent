'use client'

import * as React from 'react'
import type { BundledLanguage, ThemedToken } from 'shiki'

import { exceedsHighlightBudget, SHIKI_THEME } from '@/components/chat/shiki-highlighter'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { Textarea } from '@/components/ui/textarea'
import type { HermesReviewScope } from '@/global'
import { useI18n } from '@/i18n'
import { isSubmitEnter } from '@/lib/ime'
import { formatCombo } from '@/lib/keybinds/combo'
import { shikiLanguageForFilename } from '@/lib/markdown-code'
import { cn } from '@/lib/utils'
import type { DiffCommentSide, DiffReviewComment, DraftAnchor, NewReviewCommentInput } from '@/store/review-comments'

import {
  buildHunkRevertPatch,
  buildLinesRevertPatch,
  type ParsedPatchHunk,
  type ParsedPatchLine,
  type ParsedUnifiedDiff,
  parseUnifiedDiff
} from '../../../electron/git-patch-builder'

export interface FileDiffReviewOptions {
  /** Allow hunk-level and partial line-range revert (uncommitted scope only). */
  canRevert: boolean
  /** Saved inline comments for this file. */
  comments: DiffReviewComment[]
  /** Active draft anchor if the user is composing a comment. */
  draftAnchor: DraftAnchor | null
  /** Repo-relative path of the file being reviewed. */
  filePath: string
  /** Persist a newly composed comment. */
  onAddComment: (comment: NewReviewCommentInput) => void
  /** Dismiss the active inline composer. */
  onCancelDraft: () => void
  /** Delete a saved comment by id. */
  onDeleteComment: (id: string) => void
  /** Apply a synthesized reverse patch for a hunk or selected line subset. */
  onRevertPatch: (patchText: string, staged: boolean) => Promise<void>
  /** Open the inline composer anchored to a line or line range. */
  onStartDraft: (anchor: DraftAnchor) => void
  /** Update the body of an existing comment. */
  onUpdateComment: (id: string, body: string) => void
  /** Canonical repo/worktree path owning this review. */
  repoCwd: string
  /** Active review scope. */
  scope: HermesReviewScope
  /** Whether the displayed diff is staged (routes `--cached` on revert). */
  staged: boolean
}

interface SelectedRange {
  anchorIdx: number
  endIdx: number
  hunkIndex: number
  startIdx: number
}

interface RowCoordinate {
  hunkIndex: number
  lineIdx: number
}

const KIND_TINT: Record<ParsedPatchLine['kind'], string> = {
  add: 'border-(--ui-diff-add-border) bg-(--ui-diff-add-background)',
  context: 'border-transparent',
  remove: 'border-(--ui-diff-remove-border) bg-(--ui-diff-remove-background)'
}

const KIND_TEXT: Record<ParsedPatchLine['kind'], string> = {
  add: 'text-(--ui-diff-add-foreground)',
  context: '',
  remove: 'text-(--ui-diff-remove-foreground)'
}

function rowLineInfo(line: ParsedPatchLine): { lineNo: number; side: DiffCommentSide } {
  if (line.kind === 'remove') {
    return { lineNo: line.oldNo ?? 0, side: 'old' }
  }

  return { lineNo: line.newNo ?? line.oldNo ?? 0, side: 'new' }
}

function snippetForLines(lines: ParsedPatchLine[]): string[] {
  return lines.map(line => {
    const prefix = line.kind === 'add' ? '+' : line.kind === 'remove' ? '-' : ' '

    return `${prefix}${line.text}`
  })
}

function formatLineLabel(startLine: number, endLine: number, side: DiffCommentSide): string {
  const prefix = side === 'old' ? 'old ' : ''

  return startLine === endLine ? `${prefix}L${startLine}` : `${prefix}L${startLine}-L${endLine}`
}

function buildDraftAnchor(
  repoCwd: string,
  filePath: string,
  scope: HermesReviewScope,
  hunk: ParsedPatchHunk,
  startIdx: number,
  endIdx: number
): DraftAnchor {
  const lo = Math.max(0, Math.min(startIdx, endIdx))
  const hi = Math.min(hunk.lines.length - 1, Math.max(startIdx, endIdx))
  const slice = hunk.lines.slice(lo, hi + 1)
  const allRemoved = slice.length > 0 && slice.every(line => line.kind === 'remove')
  const side: DiffCommentSide = allRemoved ? 'old' : 'new'
  const firstNo = rowLineInfo(slice[0]).lineNo
  const lastNo = rowLineInfo(slice[slice.length - 1]).lineNo
  const startLine = Math.min(firstNo, lastNo)
  const endLine = Math.max(firstNo, lastNo)

  return {
    endLine,
    filePath,
    hunkHeader: hunk.header,
    repoCwd,
    scope,
    side,
    snippet: snippetForLines(slice),
    startLine
  }
}

function findAnchorRow(
  hunks: ParsedPatchHunk[],
  target: { endLine: number; hunkHeader: string; side: DiffCommentSide }
): null | RowCoordinate {
  const exactHunkIdx = hunks.findIndex(hunk => hunk.header === target.hunkHeader)

  const searchOrder =
    exactHunkIdx >= 0
      ? [exactHunkIdx, ...hunks.map((_, idx) => idx).filter(idx => idx !== exactHunkIdx)]
      : hunks.map((_, idx) => idx)

  for (const hunkIndex of searchOrder) {
    const hunk = hunks[hunkIndex]

    for (let lineIdx = 0; lineIdx < hunk.lines.length; lineIdx += 1) {
      const info = rowLineInfo(hunk.lines[lineIdx])

      if (info.side === target.side && info.lineNo === target.endLine) {
        return { hunkIndex, lineIdx }
      }
    }
  }

  for (const hunkIndex of searchOrder) {
    const hunk = hunks[hunkIndex]

    for (let lineIdx = 0; lineIdx < hunk.lines.length; lineIdx += 1) {
      const line = hunk.lines[lineIdx]

      if (line.newNo === target.endLine || line.oldNo === target.endLine) {
        return { hunkIndex, lineIdx }
      }
    }
  }

  return null
}

function tokenStyle({ bgColor, color, fontStyle = 0 }: ThemedToken): React.CSSProperties | undefined {
  if (!color && !bgColor && !fontStyle) {
    return undefined
  }

  return {
    backgroundColor: bgColor,
    color,
    fontStyle: fontStyle & 1 ? 'italic' : undefined,
    fontWeight: fontStyle & 2 ? 700 : undefined,
    textDecorationLine: fontStyle & 4 ? 'underline' : undefined
  }
}

function useShikiThemeName() {
  const current = () => (document.documentElement.classList.contains('dark') ? SHIKI_THEME.dark : SHIKI_THEME.light)
  const [theme, setTheme] = React.useState(current)

  React.useEffect(() => {
    const observer = new MutationObserver(() => setTheme(current()))

    observer.observe(document.documentElement, { attributeFilter: ['class'], attributes: true })

    return () => observer.disconnect()
  }, [])

  return theme
}

function useHunkTokens(parsed: ParsedUnifiedDiff, path: string | undefined, rawDiff: string): ThemedToken[][][] | null {
  const language = shikiLanguageForFilename(path)
  const canHighlight = Boolean(language) && !exceedsHighlightBudget(rawDiff)
  const theme = useShikiThemeName()
  const [tokensByHunk, setTokensByHunk] = React.useState<ThemedToken[][][] | null>(null)

  const flatLines = React.useMemo(
    () => parsed.hunks.flatMap(hunk => hunk.lines.map(line => line.text)),
    [parsed]
  )

  React.useEffect(() => {
    if (!canHighlight || !language || flatLines.length === 0) {
      setTokensByHunk(null)

      return
    }

    let cancelled = false
    const code = flatLines.join('\n')

    void import('shiki')
      .then(({ codeToTokens }) => codeToTokens(code, { lang: language as BundledLanguage, theme }))
      .then(result => {
        if (cancelled) {
          return
        }

        const grouped: ThemedToken[][][] = []
        let cursor = 0

        for (const hunk of parsed.hunks) {
          grouped.push(result.tokens.slice(cursor, cursor + hunk.lines.length))
          cursor += hunk.lines.length
        }

        setTokensByHunk(grouped)
      })
      .catch(() => {
        if (!cancelled) {
          setTokensByHunk(null)
        }
      })

    return () => {
      cancelled = true
    }
  }, [canHighlight, flatLines, language, parsed, theme])

  return tokensByHunk
}

function InlineCommentComposer({
  anchor,
  onCancel,
  onSave
}: {
  anchor: DraftAnchor
  onCancel: () => void
  onSave: (body: string) => void
}) {
  const { t } = useI18n()
  const c = t.statusStack.coding
  const [body, setBody] = React.useState('')

  const submit = () => {
    const trimmed = body.trim()

    if (!trimmed) {
      return
    }

    onSave(trimmed)
    setBody('')
  }

  return (
    <div
      className="mx-2 my-1.5 rounded-md border border-(--ui-stroke-primary) bg-(--ui-surface-primary) p-2 font-sans text-xs shadow-xs"
      data-testid="diff-inline-comment-composer"
    >
      <div className="mb-1 flex items-center justify-between gap-2 text-[0.65rem] text-(--ui-text-tertiary)">
        <span className="font-mono font-medium text-(--ui-text-secondary)">
          {formatLineLabel(anchor.startLine, anchor.endLine, anchor.side)}
        </span>
      </div>
      <Textarea
        autoFocus
        className="field-sizing-content min-h-[3rem] resize-none text-xs"
        onChange={event => setBody(event.target.value)}
        onKeyDown={event => {
          if (event.key === 'Escape') {
            event.preventDefault()
            onCancel()

            return
          }

          if ((event.metaKey || event.ctrlKey) && isSubmitEnter(event)) {
            event.preventDefault()
            submit()
          }
        }}
        placeholder={c.commentPlaceholder(formatCombo('mod+enter'))}
        rows={2}
        size="sm"
        value={body}
      />
      <div className="mt-1.5 flex items-center justify-end gap-1">
        <Button className="h-6 px-2 text-[0.68rem]" onClick={onCancel} size="sm" variant="ghost">
          {c.cancelComment}
        </Button>
        <Button
          className="h-6 px-2 text-[0.68rem]"
          disabled={!body.trim()}
          onClick={submit}
          size="sm"
          variant="default"
        >
          {c.saveComment}
        </Button>
      </div>
    </div>
  )
}

function InlineCommentCard({
  comment,
  onDelete,
  onUpdate
}: {
  comment: DiffReviewComment
  onDelete: (id: string) => void
  onUpdate: (id: string, body: string) => void
}) {
  const { t } = useI18n()
  const c = t.statusStack.coding
  const [editing, setEditing] = React.useState(false)
  const [draft, setDraft] = React.useState(comment.body)

  const commitEdit = () => {
    const trimmed = draft.trim()

    if (!trimmed) {
      return
    }

    onUpdate(comment.id, trimmed)
    setEditing(false)
  }

  return (
    <div
      className="mx-2 my-1 rounded-md border border-(--ui-stroke-secondary) bg-(--ui-surface-secondary)/90 px-2.5 py-1.5 font-sans text-xs text-(--ui-text-primary)"
      data-testid="diff-inline-comment-card"
    >
      <div className="flex items-center justify-between gap-2 text-[0.65rem]">
        <div className="flex items-center gap-1.5">
          <Codicon className="text-(--ui-text-tertiary)" name="comment" size="0.72rem" />
          <span className="font-mono font-medium text-(--ui-text-secondary)">
            {formatLineLabel(comment.startLine, comment.endLine, comment.side)}
          </span>
          {comment.stale && (
            <span
              className="rounded bg-amber-500/15 px-1 py-px text-[0.6rem] font-medium text-amber-500"
              data-testid="diff-comment-stale-badge"
            >
              {c.commentStale}
            </span>
          )}
        </div>
        {!editing && (
          <div className="flex items-center gap-0.5">
            <Button
              aria-label={c.editComment}
              className="h-5 px-1.5 text-[0.64rem] text-muted-foreground hover:text-foreground"
              onClick={() => {
                setDraft(comment.body)
                setEditing(true)
              }}
              size="sm"
              variant="ghost"
            >
              {c.editComment}
            </Button>
            <Button
              aria-label={c.deleteComment}
              className="h-5 px-1.5 text-[0.64rem] text-muted-foreground hover:text-(--ui-red)"
              onClick={() => onDelete(comment.id)}
              size="sm"
              variant="ghost"
            >
              {c.deleteComment}
            </Button>
          </div>
        )}
      </div>

      {editing ? (
        <div className="mt-1.5">
          <Textarea
            autoFocus
            className="field-sizing-content min-h-[2.75rem] resize-none text-xs"
            onChange={event => setDraft(event.target.value)}
            onKeyDown={event => {
              if (event.key === 'Escape') {
                event.preventDefault()
                setEditing(false)

                return
              }

              if ((event.metaKey || event.ctrlKey) && isSubmitEnter(event)) {
                event.preventDefault()
                commitEdit()
              }
            }}
            rows={2}
            size="sm"
            value={draft}
          />
          <div className="mt-1 flex justify-end gap-1">
            <Button className="h-6 px-2 text-[0.66rem]" onClick={() => setEditing(false)} size="sm" variant="ghost">
              {c.cancelComment}
            </Button>
            <Button
              className="h-6 px-2 text-[0.66rem]"
              disabled={!draft.trim()}
              onClick={commitEdit}
              size="sm"
              variant="default"
            >
              {c.saveComment}
            </Button>
          </div>
        </div>
      ) : (
        <p className="mt-1 whitespace-pre-wrap break-words text-[0.72rem] leading-relaxed text-(--ui-text-primary)">
          {comment.body}
        </p>
      )}
    </div>
  )
}

export function InteractiveReviewDiff({
  className,
  diff,
  path,
  review
}: {
  className?: string
  diff: string
  path?: string
  review: FileDiffReviewOptions
}) {
  const { t } = useI18n()
  const c = t.statusStack.coding

  const parsed = React.useMemo(() => parseUnifiedDiff(diff), [diff])
  const tokensByHunk = useHunkTokens(parsed, path, diff)

  const diffKey = `${review.filePath}\0${diff}`

  const [selectionState, setSelectionState] = React.useState<{
    diffKey: string
    range: null | SelectedRange
  }>({ diffKey: '', range: null })

  const [busyHunk, setBusyHunk] = React.useState<null | number>(null)

  const selectedRange = selectionState.diffKey === diffKey ? selectionState.range : null

  const setSelectedRange = (range: null | SelectedRange) => {
    setSelectionState({ diffKey, range })
  }

  const canRevertHunks = review.canRevert && !parsed.isNewFile && !parsed.isDeletedFile

  // Map every comment for this file onto its target (hunkIndex, lineIdx),
  // collecting any unmatched comments into `orphanedComments` so none vanish.
  const { commentsByRow, orphanedComments } = React.useMemo(() => {
    const byRow = new Map<string, DiffReviewComment[]>()
    const orphaned: DiffReviewComment[] = []

    for (const comment of review.comments) {
      const coord = findAnchorRow(parsed.hunks, comment)

      if (!coord) {
        orphaned.push(comment)

        continue
      }

      const key = `${coord.hunkIndex}:${coord.lineIdx}`
      const bucket = byRow.get(key) ?? []

      bucket.push(comment)
      byRow.set(key, bucket)
    }

    return { commentsByRow: byRow, orphanedComments: orphaned }
  }, [parsed.hunks, review.comments])

  // Locate where the active inline draft composer should mount (if for this file).
  const activeDraftCoord = React.useMemo(() => {
    const anchor = review.draftAnchor

    if (!anchor || anchor.filePath !== review.filePath) {
      return null
    }

    return findAnchorRow(parsed.hunks, anchor)
  }, [parsed.hunks, review.draftAnchor, review.filePath])

  const handleSelectRow = (hunkIndex: number, lineIdx: number, shiftKey: boolean) => {
    if (shiftKey && selectedRange && selectedRange.hunkIndex === hunkIndex) {
      const startIdx = Math.min(selectedRange.anchorIdx, lineIdx)
      const endIdx = Math.max(selectedRange.anchorIdx, lineIdx)

      setSelectedRange({ anchorIdx: selectedRange.anchorIdx, endIdx, hunkIndex, startIdx })

      return
    }

    setSelectedRange({ anchorIdx: lineIdx, endIdx: lineIdx, hunkIndex, startIdx: lineIdx })
  }

  const handleGutterAddClick = (hunkIndex: number, lineIdx: number, shiftKey: boolean) => {
    const hunk = parsed.hunks[hunkIndex]

    if (!hunk) {
      return
    }

    if (shiftKey && selectedRange && selectedRange.hunkIndex === hunkIndex) {
      const startIdx = Math.min(selectedRange.anchorIdx, lineIdx)
      const endIdx = Math.max(selectedRange.anchorIdx, lineIdx)

      setSelectedRange({ anchorIdx: selectedRange.anchorIdx, endIdx, hunkIndex, startIdx })
      review.onStartDraft(buildDraftAnchor(review.repoCwd, review.filePath, review.scope, hunk, startIdx, endIdx))

      return
    }

    setSelectedRange({ anchorIdx: lineIdx, endIdx: lineIdx, hunkIndex, startIdx: lineIdx })
    review.onStartDraft(buildDraftAnchor(review.repoCwd, review.filePath, review.scope, hunk, lineIdx, lineIdx))
  }

  const handleRevertHunk = async (hunkIndex: number) => {
    if (busyHunk !== null) {
      return
    }

    const patch = buildHunkRevertPatch(diff, hunkIndex, review.filePath)

    if (!patch) {
      return
    }

    setBusyHunk(hunkIndex)

    try {
      await review.onRevertPatch(patch, review.staged)
      setSelectedRange(null)
    } finally {
      setBusyHunk(null)
    }
  }

  const handleRevertSelection = async (hunkIndex: number) => {
    if (busyHunk !== null || !selectedRange || selectedRange.hunkIndex !== hunkIndex) {
      return
    }

    const indices: number[] = []

    for (let i = selectedRange.startIdx; i <= selectedRange.endIdx; i += 1) {
      indices.push(i)
    }

    const patch = buildLinesRevertPatch(diff, hunkIndex, indices, review.filePath)

    if (!patch) {
      return
    }

    setBusyHunk(hunkIndex)

    try {
      await review.onRevertPatch(patch, review.staged)
      setSelectedRange(null)
    } finally {
      setBusyHunk(null)
    }
  }

  if (parsed.hunks.length === 0) {
    return (
      <div
        className={cn(
          'max-w-none min-w-0 overflow-auto font-mono text-[0.7rem] leading-relaxed text-(--ui-text-secondary)',
          className
        )}
        data-slot="file-diff-panel"
      >
        {orphanedComments.map(comment => (
          <InlineCommentCard
            comment={comment}
            key={comment.id}
            onDelete={review.onDeleteComment}
            onUpdate={review.onUpdateComment}
          />
        ))}
        <div className="px-2.5 py-2 whitespace-pre">{diff}</div>
      </div>
    )
  }

  return (
    <div
      className={cn(
        'relative max-w-none min-w-0 overflow-auto overscroll-x-contain overscroll-y-auto font-mono text-[0.7rem] leading-5 text-(--ui-text-secondary)',
        className
      )}
      data-slot="file-diff-panel"
      data-testid="interactive-review-diff"
    >
      {orphanedComments.length > 0 && (
        <div className="border-b border-(--ui-stroke-secondary) py-1">
          {orphanedComments.map(comment => (
            <InlineCommentCard
              comment={comment}
              key={comment.id}
              onDelete={review.onDeleteComment}
              onUpdate={review.onUpdateComment}
            />
          ))}
        </div>
      )}

      {parsed.hunks.map((hunk, hunkIndex) => {
        const rangeInHunk = selectedRange && selectedRange.hunkIndex === hunkIndex ? selectedRange : null

        const changedSelectedCount = rangeInHunk
          ? hunk.lines
              .slice(rangeInHunk.startIdx, rangeInHunk.endIdx + 1)
              .filter(line => line.kind === 'add' || line.kind === 'remove').length
          : 0

        const isBusy = busyHunk === hunkIndex

        return (
          <section className="border-b border-(--ui-stroke-secondary)/60 last:border-b-0" key={`${hunkIndex}-${hunk.header}`}>
            <div
              className="sticky top-0 z-1 flex flex-wrap items-center justify-between gap-1 border-b border-(--ui-stroke-secondary)/60 bg-(--ui-surface-secondary)/95 px-2 py-0.5 text-[0.65rem] text-(--ui-text-tertiary)"
              data-testid="diff-hunk-header"
            >
              <span className="min-w-0 truncate font-mono">{hunk.header}</span>
              <div className="flex shrink-0 items-center gap-1 font-sans">
                {rangeInHunk && (
                  <>
                    <Button
                      className="h-5 gap-1 px-1.5 text-[0.64rem]"
                      onClick={() =>
                        review.onStartDraft(
                          buildDraftAnchor(
                            review.repoCwd,
                            review.filePath,
                            review.scope,
                            hunk,
                            rangeInHunk.startIdx,
                            rangeInHunk.endIdx
                          )
                        )
                      }
                      size="sm"
                      variant="ghost"
                    >
                      <Codicon name="comment" size="0.68rem" />
                      <span>{c.commentSelectedLines}</span>
                    </Button>
                    {canRevertHunks && changedSelectedCount > 0 && (
                      <Button
                        className="h-5 gap-1 px-1.5 text-[0.64rem] text-(--ui-red) hover:text-(--ui-red)"
                        disabled={isBusy}
                        onClick={() => void handleRevertSelection(hunkIndex)}
                        size="sm"
                        variant="ghost"
                      >
                        <Codicon name="discard" size="0.68rem" />
                        <span>{c.revertSelectedLines(changedSelectedCount)}</span>
                      </Button>
                    )}
                    <Button
                      aria-label={c.clearSelection}
                      className="size-5 p-0 text-muted-foreground hover:text-foreground"
                      onClick={() => setSelectedRange(null)}
                      size="icon-xs"
                      variant="ghost"
                    >
                      <Codicon name="close" size="0.68rem" />
                    </Button>
                  </>
                )}
                {canRevertHunks && (
                  <Button
                    className="h-5 gap-1 px-1.5 text-[0.64rem] text-muted-foreground hover:text-(--ui-red)"
                    disabled={isBusy}
                    onClick={() => void handleRevertHunk(hunkIndex)}
                    size="sm"
                    variant="ghost"
                  >
                    <Codicon name="discard" size="0.68rem" />
                    <span>{c.revertHunk}</span>
                  </Button>
                )}
              </div>
            </div>

            <div className="min-w-max">
              {hunk.lines.map((line, lineIdx) => {
                const isSelected = Boolean(
                  rangeInHunk && lineIdx >= rangeInHunk.startIdx && lineIdx <= rangeInHunk.endIdx
                )

                const rowTokens = tokensByHunk?.[hunkIndex]?.[lineIdx] ?? []
                const rowKey = `${hunkIndex}:${lineIdx}`
                const rowComments = commentsByRow.get(rowKey) ?? []

                const showComposerHere =
                  activeDraftCoord !== null &&
                  activeDraftCoord.hunkIndex === hunkIndex &&
                  activeDraftCoord.lineIdx === lineIdx &&
                  review.draftAnchor !== null

                return (
                  <React.Fragment key={`${lineIdx}-${line.oldNo ?? ''}-${line.newNo ?? ''}`}>
                    <div
                      className={cn(
                        'group/diff-row flex min-h-5 items-stretch border-l-2',
                        KIND_TINT[line.kind],
                        isSelected && 'bg-(--ui-accent-background)/20 ring-1 ring-inset ring-(--ui-accent-border)/45'
                      )}
                      data-selected={isSelected ? 'true' : undefined}
                      data-testid="diff-interactive-row"
                    >
                      <div className="flex shrink-0 select-none items-center gap-0.5 pl-1 pr-1.5 text-[0.64rem] text-muted-foreground/60 tabular-nums">
                        <button
                          aria-label={c.addComment}
                          className={cn(
                            'flex size-4 items-center justify-center rounded text-muted-foreground transition-opacity hover:bg-(--ui-surface-hover) hover:text-foreground focus-visible:opacity-100',
                            isSelected ? 'opacity-100' : 'opacity-0 group-hover/diff-row:opacity-100'
                          )}
                          data-testid="diff-gutter-add-comment"
                          onClick={event => handleGutterAddClick(hunkIndex, lineIdx, event.shiftKey)}
                          title={c.addComment}
                          type="button"
                        >
                          <Codicon name="add" size="0.68rem" />
                        </button>
                        <button
                          className="w-7 cursor-pointer text-right hover:text-foreground"
                          data-testid="diff-line-number"
                          onClick={event => handleSelectRow(hunkIndex, lineIdx, event.shiftKey)}
                          type="button"
                        >
                          {line.kind === 'remove' ? (line.oldNo ?? '') : (line.newNo ?? '')}
                        </button>
                      </div>

                      <div
                        className={cn(
                          'min-w-0 flex-1 whitespace-pre px-2 leading-5',
                          rowTokens.length === 0 && KIND_TEXT[line.kind]
                        )}
                      >
                        {rowTokens.length > 0
                          ? rowTokens.map((token, tokenIndex) => (
                              <span key={`${tokenIndex}-${token.offset}`} style={tokenStyle(token)}>
                                {token.content}
                              </span>
                            ))
                          : line.text || ' '}
                      </div>
                    </div>

                    {rowComments.map(comment => (
                      <InlineCommentCard
                        comment={comment}
                        key={comment.id}
                        onDelete={review.onDeleteComment}
                        onUpdate={review.onUpdateComment}
                      />
                    ))}

                    {showComposerHere && review.draftAnchor && (
                      <InlineCommentComposer
                        anchor={review.draftAnchor}
                        onCancel={review.onCancelDraft}
                        onSave={body => {
                          const anchor = review.draftAnchor

                          if (!anchor) {
                            return
                          }

                          review.onAddComment({
                            body,
                            endLine: anchor.endLine,
                            filePath: anchor.filePath,
                            hunkHeader: anchor.hunkHeader,
                            repoCwd: anchor.repoCwd,
                            scope: anchor.scope,
                            side: anchor.side,
                            snippet: anchor.snippet,
                            startLine: anchor.startLine
                          })
                          setSelectedRange(null)
                        }}
                      />
                    )}
                  </React.Fragment>
                )
              })}
            </div>
          </section>
        )
      })}
    </div>
  )
}
