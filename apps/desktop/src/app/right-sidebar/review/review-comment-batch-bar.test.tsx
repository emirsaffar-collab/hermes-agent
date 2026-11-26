import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'
import { $reviewScopeCwd, $reviewScopeTarget } from '@/store/review'
import { $activeDraftAnchor, $reviewComments, addReviewComment } from '@/store/review-comments'
import { $currentCwd } from '@/store/session'

import { ReviewCommentBatchBar } from './review-comment-batch-bar'

const requestComposerSubmit = vi.hoisted(() => vi.fn<(text: string, options?: unknown) => boolean>(() => true))
const notifyError = vi.hoisted(() => vi.fn())

vi.mock('@/app/chat/composer/focus', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  requestComposerSubmit: (text: string, options?: unknown) => requestComposerSubmit(text, options)
}))

vi.mock('@/store/notifications', () => ({
  notifyError: (...args: unknown[]) => notifyError(...args)
}))

function renderBar() {
  return render(
    <I18nProvider configClient={null} initialLocale="en">
      <ReviewCommentBatchBar />
    </I18nProvider>
  )
}

describe('ReviewCommentBatchBar', () => {
  beforeEach(() => {
    $reviewComments.set({})
    $activeDraftAnchor.set(null)
    requestComposerSubmit.mockReset()
    requestComposerSubmit.mockReturnValue(true)
    notifyError.mockReset()
    $currentCwd.set('/repo-a')
    $reviewScopeCwd.set('/repo-a')
    $reviewScopeTarget.set('main')
  })

  afterEach(() => {
    cleanup()
    $reviewComments.set({})
    $activeDraftAnchor.set(null)
  })

  it('renders nothing when there are no comments for the active repo', () => {
    addReviewComment({
      body: 'other repo comment',
      endLine: 1,
      filePath: 'b.ts',
      hunkHeader: '@@ -1 +1 @@',
      repoCwd: '/repo-b',
      scope: 'uncommitted',
      side: 'new',
      snippet: ['+x'],
      startLine: 1
    })

    const { container } = renderBar()

    expect(container.firstChild).toBeNull()
  })

  it('shows comment and file counts, clears on Clear, and packs + sends on Send to chat', () => {
    addReviewComment({
      body: 'Check nullability',
      endLine: 10,
      filePath: 'src/a.ts',
      hunkHeader: '@@ -9,2 +9,3 @@',
      repoCwd: '/repo-a',
      scope: 'uncommitted',
      side: 'new',
      snippet: ['+const x = y.z'],
      startLine: 10
    })
    addReviewComment({
      body: 'Add error handling',
      endLine: 25,
      filePath: 'src/b.ts',
      hunkHeader: '@@ -20,2 +20,6 @@',
      repoCwd: '/repo-a',
      scope: 'uncommitted',
      side: 'new',
      snippet: ['+await run()'],
      startLine: 24
    })

    renderBar()

    expect(screen.getByText('2 comments across 2 files')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Send to chat' }))

    expect(requestComposerSubmit).toHaveBeenCalledTimes(1)
    const [prompt, opts] = requestComposerSubmit.mock.calls[0]
    expect(prompt).toContain('### `src/a.ts`')
    expect(prompt).toContain('Check nullability')
    expect(prompt).toContain('### `src/b.ts`')
    expect(prompt).toContain('Add error handling')
    expect(opts).toEqual({ target: 'main' })
    expect($reviewComments.get()['/repo-a']).toBeUndefined()
  })

  it('preserves comments and notifies if no composer claims the submit', () => {
    requestComposerSubmit.mockReturnValue(false)
    addReviewComment({
      body: 'Keep me',
      endLine: 5,
      filePath: 'src/a.ts',
      hunkHeader: '@@ -1 +1 @@',
      repoCwd: '/repo-a',
      scope: 'uncommitted',
      side: 'new',
      snippet: ['+x'],
      startLine: 5
    })

    renderBar()
    fireEvent.click(screen.getByRole('button', { name: 'Send to chat' }))

    expect(notifyError).toHaveBeenCalledTimes(1)
    expect($reviewComments.get()['/repo-a']).toHaveLength(1)

    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
    expect($reviewComments.get()['/repo-a']).toBeUndefined()
  })
})
