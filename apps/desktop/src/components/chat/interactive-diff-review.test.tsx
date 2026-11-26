import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import * as React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'
import type { DiffReviewComment, DraftAnchor } from '@/store/review-comments'

import { FileDiffPanel, type FileDiffReviewOptions } from './diff-lines'

const SAMPLE_DIFF = [
  'diff --git a/src/demo.ts b/src/demo.ts',
  'index 1111111..2222222 100644',
  '--- a/src/demo.ts',
  '+++ b/src/demo.ts',
  '@@ -1,4 +1,5 @@',
  ' const a = 1',
  '-const b = 2',
  '+const b = 20',
  '+const c = 30',
  ' const d = 4',
  ' export { a, b, c, d }'
].join('\n')

function Harness({
  canRevert = true,
  initialComments = [],
  onRevertPatch = vi.fn(async () => undefined),
  staged = false
}: {
  canRevert?: boolean
  initialComments?: DiffReviewComment[]
  onRevertPatch?: (patchText: string, staged: boolean) => Promise<void>
  staged?: boolean
}) {
  const [comments, setComments] = React.useState<DiffReviewComment[]>(initialComments)
  const [draftAnchor, setDraftAnchor] = React.useState<DraftAnchor | null>(null)

  const review: FileDiffReviewOptions = {
    canRevert,
    comments,
    draftAnchor,
    filePath: 'src/demo.ts',
    onAddComment: comment => {
      setComments(prev => [
        ...prev,
        {
          ...comment,
          createdAt: 1,
          id: `c-${prev.length + 1}`,
          snippet: [...comment.snippet]
        }
      ])
      setDraftAnchor(null)
    },
    onCancelDraft: () => setDraftAnchor(null),
    onDeleteComment: id => setComments(prev => prev.filter(item => item.id !== id)),
    onRevertPatch,
    onStartDraft: anchor => setDraftAnchor(anchor),
    onUpdateComment: (id, body) =>
      setComments(prev => prev.map(item => (item.id === id ? { ...item, body } : item))),
    repoCwd: '/repo',
    scope: 'uncommitted',
    staged
  }

  return (
    <I18nProvider configClient={null} initialLocale="en">
      <FileDiffPanel diff={SAMPLE_DIFF} path="src/demo.ts" review={review} />
    </I18nProvider>
  )
}

describe('InteractiveReviewDiff', () => {
  afterEach(() => {
    cleanup()
  })

  it('adds, edits, and deletes an inline comment via the gutter + trigger', () => {
    render(<Harness />)

    const addButtons = screen.getAllByTestId('diff-gutter-add-comment')
    // Row 2 is `+const b = 20` (new line 2)
    fireEvent.click(addButtons[2])

    const composer = screen.getByTestId('diff-inline-comment-composer')
    const textarea = composer.querySelector('textarea')!
    fireEvent.change(textarea, { target: { value: 'Avoid magic number 20' } })
    fireEvent.keyDown(textarea, { key: 'Enter', metaKey: true })

    expect(screen.queryByTestId('diff-inline-comment-composer')).toBeNull()
    expect(screen.getByText('Avoid magic number 20')).toBeTruthy()
    expect(screen.getByText('L2')).toBeTruthy()

    // Edit
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    const editBox = screen.getByTestId('diff-inline-comment-card').querySelector('textarea')!
    fireEvent.change(editBox, { target: { value: 'Extract DEFAULT_B constant' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(screen.getByText('Extract DEFAULT_B constant')).toBeTruthy()

    // Delete
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    expect(screen.queryByTestId('diff-inline-comment-card')).toBeNull()
  })

  it('cancels inline comment drafting on Escape', () => {
    render(<Harness />)

    fireEvent.click(screen.getAllByTestId('diff-gutter-add-comment')[2])
    const textarea = screen.getByTestId('diff-inline-comment-composer').querySelector('textarea')!
    fireEvent.keyDown(textarea, { key: 'Escape' })

    expect(screen.queryByTestId('diff-inline-comment-composer')).toBeNull()
  })

  it('supports Shift+Click multi-line selection for commenting and selective line revert', async () => {
    const onRevertPatch = vi.fn(async (_patchText: string, _staged: boolean) => undefined)

    render(<Harness onRevertPatch={onRevertPatch} staged />)

    const lineNumbers = screen.getAllByTestId('diff-line-number')
    // Click `+const b = 20` (idx 2), then Shift+Click `+const c = 30` (idx 3)
    fireEvent.click(lineNumbers[2])
    fireEvent.click(lineNumbers[3], { shiftKey: true })

    const rows = screen.getAllByTestId('diff-interactive-row')
    expect(rows[2].getAttribute('data-selected')).toBe('true')
    expect(rows[3].getAttribute('data-selected')).toBe('true')
    expect(rows[1].getAttribute('data-selected')).toBeNull()

    // Revert 2 selected lines
    const revertLinesBtn = screen.getByRole('button', { name: 'Revert 2 lines' })
    fireEvent.click(revertLinesBtn)

    expect(onRevertPatch).toHaveBeenCalledTimes(1)
    const [patchText, staged] = onRevertPatch.mock.calls[0]
    expect(staged).toBe(true)
    expect(patchText).toContain('+const b = 20')
    expect(patchText).toContain('+const c = 30')
    // Unselected `-const b = 2` must NOT appear in the reverse patch
    expect(patchText).not.toContain('-const b = 2')
  })

  it('triggers whole-hunk revert and hides revert buttons in read-only scope', () => {
    const onRevertPatch = vi.fn(async (_patchText: string, _staged: boolean) => undefined)

    const { rerender } = render(<Harness canRevert onRevertPatch={onRevertPatch} />)

    fireEvent.click(screen.getByRole('button', { name: 'Revert hunk' }))
    expect(onRevertPatch).toHaveBeenCalledTimes(1)
    expect(onRevertPatch.mock.calls[0][0]).toContain('@@ -1,4 +1,5 @@')

    rerender(<Harness canRevert={false} onRevertPatch={onRevertPatch} />)
    expect(screen.queryByRole('button', { name: 'Revert hunk' })).toBeNull()
  })

  it('renders a stale badge on stale comments', () => {
    render(
      <Harness
        initialComments={[
          {
            body: 'Old comment',
            createdAt: 1,
            endLine: 2,
            filePath: 'src/demo.ts',
            hunkHeader: '@@ -1,4 +1,5 @@',
            id: 'stale-1',
            repoCwd: '/repo',
            scope: 'uncommitted',
            side: 'new',
            snippet: ['+const b = 20'],
            stale: true,
            startLine: 2
          }
        ]}
      />
    )

    expect(screen.getByTestId('diff-comment-stale-badge').textContent).toBe('Stale')
  })
})
