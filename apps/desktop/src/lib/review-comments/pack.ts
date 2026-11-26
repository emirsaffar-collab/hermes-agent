import type { DiffReviewComment } from '@/store/review-comments'

/** Threshold mirroring preview-annotate/pack.ts: below this a simple file list needs no batching note. */
export const REVIEW_COMMENT_GROUP_THRESHOLD = 4

function repoDisplayName(repoCwd: string): string {
  const cleaned = repoCwd.trim().replace(/[\\/]+$/, '')

  if (!cleaned) {
    return 'repository'
  }

  const parts = cleaned.split(/[\\/]/)

  return parts[parts.length - 1] || cleaned
}

function compareStrings(a: string, b: string): number {
  if (a < b) {
    return -1
  }

  if (a > b) {
    return 1
  }

  return 0
}

function formatLineLabel(startLine: number, endLine: number): string {
  return startLine === endLine ? `Line ${startLine}` : `Lines ${startLine}–${endLine}`
}

/** Pick a backtick fence strictly longer than any run of backticks inside `lines`. */
function fenceForSnippet(lines: readonly string[]): string {
  let maxRun = 0

  for (const line of lines) {
    const matches = line.match(/`+/g)

    if (!matches) {
      continue
    }

    for (const run of matches) {
      if (run.length > maxRun) {
        maxRun = run.length
      }
    }
  }

  return '`'.repeat(Math.max(3, maxRun + 1))
}

function inlineCode(text: string): string {
  let maxRun = 0
  const matches = text.match(/`+/g)

  if (matches) {
    for (const run of matches) {
      if (run.length > maxRun) {
        maxRun = run.length
      }
    }
  }

  const tick = '`'.repeat(maxRun + 1)
  const needsPadding = text.startsWith('`') || text.endsWith('`')

  return needsPadding ? `${tick} ${text} ${tick}` : `${tick}${text}${tick}`
}

function batchGuidance(fileCount: number, commentCount: number): string {
  return [
    `These ${commentCount} comments span ${fileCount} files and are grouped by file below.`,
    'Work file-by-file; fold comments in the same file into one coherent edit, do not revert unrelated hunks, and keep the scope narrow.'
  ].join(' ')
}

function formatCommentEntry(comment: DiffReviewComment, index: number): string {
  const lines: string[] = []
  const range = formatLineLabel(comment.startLine, comment.endLine)
  const hunk = comment.hunkHeader.trim() ? ` ${inlineCode(comment.hunkHeader.trim())}` : ''
  const staleTag = comment.stale ? ' _(stale)_' : ''

  lines.push(`${index + 1}. **${range} (${comment.side})**${hunk}${staleTag}`)

  if (comment.snippet.length > 0) {
    const fence = fenceForSnippet(comment.snippet)

    lines.push(`   ${fence}diff`)

    for (const snippetLine of comment.snippet) {
      lines.push(`   ${snippetLine}`)
    }

    lines.push(`   ${fence}`)
  }

  const noteLines = comment.body.trim().split('\n')

  if (noteLines.length === 1) {
    lines.push(`   Note: ${noteLines[0]}`)
  } else {
    lines.push(`   Note: ${noteLines[0]}`)

    for (const rest of noteLines.slice(1)) {
      lines.push(`   ${rest}`)
    }
  }

  return lines.join('\n')
}

export interface PackedCommentGroup {
  filePath: string
  comments: DiffReviewComment[]
}

/** Deterministically group comments by filePath and order by startLine ascending. */
export function groupDiffComments(comments: readonly DiffReviewComment[]): PackedCommentGroup[] {
  const byFile = new Map<string, DiffReviewComment[]>()

  for (const comment of comments) {
    const bucket = byFile.get(comment.filePath) ?? []

    bucket.push(comment)
    byFile.set(comment.filePath, bucket)
  }

  return [...byFile.entries()]
    .sort(([a], [b]) => compareStrings(a, b))
    .map(([filePath, list]) => ({
      comments: [...list].sort((a, b) => {
        if (a.startLine !== b.startLine) {
          return a.startLine - b.startLine
        }

        if (a.endLine !== b.endLine) {
          return a.endLine - b.endLine
        }

        if (a.createdAt !== b.createdAt) {
          return a.createdAt - b.createdAt
        }

        return compareStrings(a.id, b.id)
      }),
      filePath
    }))
}

/**
 * Pack inline diff review comments into a deterministic markdown prompt for
 * the chat composer. Returns an empty string when `comments` is empty.
 */
export function packageDiffComments(comments: readonly DiffReviewComment[], repoCwd: string): string {
  if (comments.length === 0) {
    return ''
  }

  const count = comments.length
  const repoName = repoDisplayName(repoCwd)
  const groups = groupDiffComments(comments)

  const header =
    count === 1
      ? `Address this 1 inline code review comment in ${inlineCode(repoName)}:`
      : `Address these ${count} inline code review comments in ${inlineCode(repoName)}:`

  const parts: string[] = [header]

  if (count >= REVIEW_COMMENT_GROUP_THRESHOLD && groups.length >= 2) {
    parts.push('', batchGuidance(groups.length, count))
  }

  for (const group of groups) {
    parts.push('', `### ${inlineCode(group.filePath)}`)
    parts.push(...group.comments.map((comment, idx) => formatCommentEntry(comment, idx)))
  }

  return parts.join('\n')
}
