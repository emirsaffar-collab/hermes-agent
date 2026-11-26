import { describe, expect, it } from 'vitest'

import type { DiffReviewComment } from '@/store/review-comments'

import { groupDiffComments, packageDiffComments } from './pack'

function makeComment(over: Partial<DiffReviewComment> = {}): DiffReviewComment {
  return {
    body: 'Fix this line',
    createdAt: 1_700_000_000_000,
    endLine: 40,
    filePath: 'src/a.ts',
    hunkHeader: '@@ -38,5 +38,6 @@',
    id: 'c-1',
    repoCwd: '/Users/dev/hermes-agent',
    scope: 'uncommitted',
    side: 'new',
    snippet: ['+const value = 42'],
    startLine: 40,
    ...over
  }
}

describe('packageDiffComments', () => {
  it('returns empty string for an empty comment list', () => {
    expect(packageDiffComments([], '/Users/dev/hermes-agent')).toBe('')
  })

  it('formats a single inline comment with repo name, file heading, hunk header, snippet, and note', () => {
    const prompt = packageDiffComments(
      [
        makeComment({
          body: 'Keep `tokens-per-second` hidden by default.',
          endLine: 48,
          filePath: 'src/store/statusbar-prefs.ts',
          hunkHeader: '@@ -34,15 +34,11 @@',
          snippet: ['+export const STATUSBAR_HIDDEN_BY_DEFAULT: readonly string[] = ['],
          startLine: 40
        })
      ],
      '/Users/dev/hermes-agent/'
    )

    expect(prompt).toContain('Address this 1 inline code review comment in `hermes-agent`:')
    expect(prompt).toContain('### `src/store/statusbar-prefs.ts`')
    expect(prompt).toContain('1. **Lines 40–48 (new)** `@@ -34,15 +34,11 @@`')
    expect(prompt).toContain('   ```diff\n   +export const STATUSBAR_HIDDEN_BY_DEFAULT: readonly string[] = [\n   ```')
    expect(prompt).toContain('   Note: Keep `tokens-per-second` hidden by default.')
    expect(prompt).not.toContain('Work file-by-file')
  })

  it('groups comments deterministically by filePath and orders by startLine ascending', () => {
    const comments = [
      makeComment({ body: 'Later line in b', endLine: 90, filePath: 'src/b.ts', id: 'c3', startLine: 88 }),
      makeComment({ body: 'Second in a', endLine: 30, filePath: 'src/a.ts', id: 'c2', startLine: 30 }),
      makeComment({ body: 'First in a', endLine: 10, filePath: 'src/a.ts', id: 'c1', startLine: 10 })
    ]

    const groups = groupDiffComments(comments)

    expect(groups.map(g => g.filePath)).toEqual(['src/a.ts', 'src/b.ts'])
    expect(groups[0].comments.map(c => c.startLine)).toEqual([10, 30])

    const prompt = packageDiffComments(comments, '/Users/dev/hermes-agent')

    expect(prompt.indexOf('### `src/a.ts`')).toBeLessThan(prompt.indexOf('### `src/b.ts`'))
    expect(prompt.indexOf('First in a')).toBeLessThan(prompt.indexOf('Second in a'))
    expect(prompt).toContain('1. **Line 10 (new)**')
    expect(prompt).toContain('2. **Line 30 (new)**')
    expect(prompt).not.toContain('Work file-by-file')
  })

  it('includes structural batch guidance when >= 4 comments span >= 2 files', () => {
    const comments = [
      makeComment({ body: 'One', filePath: 'src/a.ts', id: '1', startLine: 5, endLine: 5 }),
      makeComment({ body: 'Two', filePath: 'src/a.ts', id: '2', startLine: 15, endLine: 15 }),
      makeComment({ body: 'Three', filePath: 'src/b.ts', id: '3', startLine: 8, endLine: 9 }),
      makeComment({ body: 'Four', filePath: 'src/c.ts', id: '4', startLine: 22, endLine: 22 })
    ]

    const prompt = packageDiffComments(comments, '/Users/dev/hermes-agent')

    expect(prompt).toContain('Address these 4 inline code review comments in `hermes-agent`:')
    expect(prompt).toContain('These 4 comments span 3 files and are grouped by file below.')
    expect(prompt).toContain('Work file-by-file; fold comments in the same file into one coherent edit')
  })

  it('omits batch guidance when >= 4 comments all live in a single file', () => {
    const comments = Array.from({ length: 4 }, (_, i) =>
      makeComment({ body: `Note ${i + 1}`, endLine: (i + 1) * 5, filePath: 'src/only.ts', id: `c-${i}`, startLine: (i + 1) * 5 })
    )

    const prompt = packageDiffComments(comments, '/Users/dev/hermes-agent')

    expect(prompt).not.toContain('Work file-by-file')
  })

  it('uses a longer markdown fence when snippets contain triple backticks', () => {
    const prompt = packageDiffComments(
      [
        makeComment({
          body: 'Ensure fenced block stays valid',
          snippet: ['+const doc = "```ts\\nconst x = 1\\n```"']
        })
      ],
      '/Users/dev/hermes-agent'
    )

    expect(prompt).toContain('   ````diff')
    expect(prompt).toContain('   ````\n   Note: Ensure fenced block stays valid')
  })
})
