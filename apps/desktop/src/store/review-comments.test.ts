import { beforeEach, describe, expect, it } from 'vitest'

import {
  $activeDraftAnchor,
  $reviewComments,
  addReviewComment,
  clearRepoComments,
  closeDraftAnchor,
  commentsForFile,
  commentsForRepo,
  deleteReviewComment,
  markStaleComments,
  MAX_SNIPPET_LINES,
  openDraftAnchor,
  pruneResolvedComments,
  reloadReviewCommentsFromStorage,
  restoreRepoComments,
  REVIEW_COMMENTS_STORAGE_KEY,
  updateReviewComment
} from './review-comments'

describe('review-comments store', () => {
  beforeEach(() => {
    window.sessionStorage.clear()
    $reviewComments.set({})
    $activeDraftAnchor.set(null)
  })

  it('isolates comments per repo/worktree cwd and persists to sessionStorage', () => {
    const first = addReviewComment({
      body: 'Keep default hidden',
      endLine: 48,
      filePath: 'src/a.ts',
      hunkHeader: '@@ -40,5 +40,9 @@',
      repoCwd: '/repos/alpha',
      scope: 'uncommitted',
      side: 'new',
      snippet: ['+const a = 1'],
      startLine: 40
    })

    const second = addReviewComment({
      body: 'Rename helper',
      endLine: 12,
      filePath: 'src/b.ts',
      hunkHeader: '@@ -10,3 +10,4 @@',
      repoCwd: '/repos/beta',
      scope: 'uncommitted',
      side: 'new',
      snippet: ['+function foo() {}'],
      startLine: 12
    })

    expect(first).not.toBeNull()
    expect(second).not.toBeNull()
    expect(commentsForRepo('/repos/alpha')).toHaveLength(1)
    expect(commentsForRepo('/repos/beta')).toHaveLength(1)
    expect(commentsForFile('/repos/alpha', 'src/a.ts')).toHaveLength(1)
    expect(commentsForFile('/repos/alpha', 'src/b.ts')).toHaveLength(0)

    const raw = window.sessionStorage.getItem(REVIEW_COMMENTS_STORAGE_KEY)

    expect(raw).toBeTruthy()

    // Simulate reload into a fresh atom.
    $reviewComments.set({})
    window.sessionStorage.setItem(REVIEW_COMMENTS_STORAGE_KEY, raw!)
    reloadReviewCommentsFromStorage()

    expect(commentsForRepo('/repos/alpha').map(c => c.body)).toEqual(['Keep default hidden'])
    expect(commentsForRepo('/repos/beta').map(c => c.body)).toEqual(['Rename helper'])
  })

  it('normalizes reversed line ranges, trims body, and caps snippet length', () => {
    const longSnippet = Array.from({ length: 20 }, (_, i) => `+line ${i + 1}`)

    const created = addReviewComment({
      body: '   Trimmed note   ',
      endLine: 10,
      filePath: 'src/x.ts',
      hunkHeader: '@@ -1,5 +1,20 @@',
      repoCwd: '  /repos/alpha  ',
      scope: 'uncommitted',
      side: 'new',
      snippet: longSnippet,
      startLine: 25
    })

    expect(created).not.toBeNull()
    expect(created!.repoCwd).toBe('/repos/alpha')
    expect(created!.startLine).toBe(10)
    expect(created!.endLine).toBe(25)
    expect(created!.body).toBe('Trimmed note')
    expect(created!.snippet).toHaveLength(MAX_SNIPPET_LINES)
  })

  it('supports editing, deleting, clearing, and restoring comments', () => {
    const comment = addReviewComment({
      body: 'Initial note',
      endLine: 5,
      filePath: 'src/a.ts',
      hunkHeader: '@@ -1,3 +1,5 @@',
      repoCwd: '/repos/alpha',
      scope: 'uncommitted',
      side: 'new',
      snippet: ['+const x = 1'],
      startLine: 5
    })!

    const edited = updateReviewComment('/repos/alpha', comment.id, 'Updated note')

    expect(edited?.body).toBe('Updated note')
    expect(commentsForRepo('/repos/alpha')[0].body).toBe('Updated note')

    // Empty body update is ignored.
    expect(updateReviewComment('/repos/alpha', comment.id, '   ')).toBeNull()
    expect(commentsForRepo('/repos/alpha')[0].body).toBe('Updated note')

    const cleared = clearRepoComments('/repos/alpha')

    expect(cleared).toHaveLength(1)
    expect(commentsForRepo('/repos/alpha')).toHaveLength(0)

    restoreRepoComments('/repos/alpha', cleared)
    expect(commentsForRepo('/repos/alpha')).toHaveLength(1)

    expect(deleteReviewComment('/repos/alpha', comment.id)).toBe(true)
    expect(deleteReviewComment('/repos/alpha', comment.id)).toBe(false)
    expect(commentsForRepo('/repos/alpha')).toHaveLength(0)
  })

  it('marks stale comments when a file disappears and clears stale when it returns', () => {
    addReviewComment({
      body: 'Check error path',
      endLine: 15,
      filePath: 'src/gone.ts',
      hunkHeader: '@@ -10,5 +10,6 @@',
      repoCwd: '/repos/alpha',
      scope: 'uncommitted',
      side: 'new',
      snippet: ['+throw err'],
      startLine: 14
    })

    addReviewComment({
      body: 'Still here',
      endLine: 3,
      filePath: 'src/live.ts',
      hunkHeader: '@@ -1,2 +1,3 @@',
      repoCwd: '/repos/alpha',
      scope: 'uncommitted',
      side: 'new',
      snippet: ['+ok'],
      startLine: 3
    })

    markStaleComments('/repos/alpha', [{ path: 'src/live.ts' }])

    const afterMark = commentsForRepo('/repos/alpha')

    expect(afterMark.find(c => c.filePath === 'src/gone.ts')?.stale).toBe(true)
    expect(afterMark.find(c => c.filePath === 'src/live.ts')?.stale).toBeFalsy()

    // Reappearing clears stale.
    markStaleComments('/repos/alpha', [{ path: 'src/live.ts' }, { path: 'src/gone.ts' }])
    expect(commentsForRepo('/repos/alpha').find(c => c.filePath === 'src/gone.ts')?.stale).toBe(false)
  })

  it('prunes resolved comments whose files disappeared from liveFiles', () => {
    addReviewComment({
      body: 'Gone',
      endLine: 2,
      filePath: 'src/gone.ts',
      hunkHeader: '@@ -1,1 +1,2 @@',
      repoCwd: '/repos/alpha',
      scope: 'uncommitted',
      side: 'new',
      snippet: ['+x'],
      startLine: 2
    })

    addReviewComment({
      body: 'Live',
      endLine: 2,
      filePath: 'src/live.ts',
      hunkHeader: '@@ -1,1 +1,2 @@',
      repoCwd: '/repos/alpha',
      scope: 'uncommitted',
      side: 'new',
      snippet: ['+y'],
      startLine: 2
    })

    const pruned = pruneResolvedComments('/repos/alpha', [{ path: 'src/live.ts' }])

    expect(pruned).toBe(1)
    expect(commentsForRepo('/repos/alpha').map(c => c.filePath)).toEqual(['src/live.ts'])
  })

  it('manages activeDraftAnchor lifecycle and clears it when saving a comment on that file', () => {
    openDraftAnchor({
      endLine: 4,
      filePath: 'src/a.ts',
      hunkHeader: '@@ -1,4 +1,5 @@',
      repoCwd: '/repos/alpha',
      scope: 'uncommitted',
      side: 'new',
      snippet: ['+a', '+b'],
      startLine: 8
    })

    expect($activeDraftAnchor.get()?.startLine).toBe(4)
    expect($activeDraftAnchor.get()?.endLine).toBe(8)

    addReviewComment({
      body: 'Saved',
      endLine: 8,
      filePath: 'src/a.ts',
      hunkHeader: '@@ -1,4 +1,5 @@',
      repoCwd: '/repos/alpha',
      scope: 'uncommitted',
      side: 'new',
      snippet: ['+a', '+b'],
      startLine: 4
    })

    expect($activeDraftAnchor.get()).toBeNull()

    openDraftAnchor({
      endLine: 2,
      filePath: 'src/b.ts',
      hunkHeader: '@@ -1,2 +1,2 @@',
      repoCwd: '/repos/alpha',
      scope: 'uncommitted',
      side: 'old',
      snippet: ['-old'],
      startLine: 2
    })
    closeDraftAnchor()
    expect($activeDraftAnchor.get()).toBeNull()
  })
})
