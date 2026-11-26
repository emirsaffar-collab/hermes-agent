/**
 * Pure unified-diff parser and minimal reverse-patch synthesizer.
 *
 * Given a `git diff` string for a single file, builds a valid unified diff
 * patch suitable for `git apply --reverse [--cached] --unidiff-zero` to revert:
 *   - an entire hunk (`buildHunkRevertPatch`), or
 *   - an arbitrary subset of `+`/`-` lines within a hunk (`buildLinesRevertPatch`).
 *
 * Math for `git apply --reverse` against the current target state (`NEW`):
 *   - Context `' '` line: present in `NEW`, stays unchanged -> emit `' '`.
 *   - Selected `'+'` line: present in `NEW`, should be removed -> emit `'+'`.
 *   - Unselected `'+'` line: present in `NEW`, should STAY -> convert to context `' '`.
 *   - Selected `'-'` line: absent from `NEW`, should be restored -> emit `'-'`.
 *   - Unselected `'-'` line: absent from `NEW`, should stay absent -> omit.
 */

export type PatchLineKind = 'add' | 'context' | 'remove'

export interface ParsedPatchLine {
  kind: PatchLineKind
  /** Line content without the leading '+', '-', or ' ' marker. */
  text: string
  /** Original raw line including leading marker. */
  raw: string
  /** 1-based line number in the old file (present for context and remove). */
  oldNo?: number
  /** 1-based line number in the new file (present for context and add). */
  newNo?: number
  /** True when a `\ No newline at end of file` line immediately followed this line. */
  noNewlineAfter?: boolean
}

export interface ParsedPatchHunk {
  index: number
  header: string
  oldStart: number
  oldCount: number
  newStart: number
  newCount: number
  sectionHeading: string
  lines: ParsedPatchLine[]
}

export interface ParsedUnifiedDiff {
  headerLines: string[]
  oldPath: string
  newPath: string
  isNewFile: boolean
  isDeletedFile: boolean
  hunks: ParsedPatchHunk[]
}

const HUNK_HEADER_RE = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$/

function stripGitPrefix(rawPath: string): string {
  const trimmed = rawPath.trim()

  if (trimmed === '/dev/null') {
    return ''
  }

  return trimmed.replace(/^[ab]\//, '')
}

function normalizeDiffLines(diffText: string): string[] {
  const raw = diffText.replace(/\r\n/g, '\n')
  const lines = raw.split('\n')

  if (lines.length > 0 && lines[lines.length - 1] === '') {
    lines.pop()
  }

  return lines
}

export function parseUnifiedDiff(diffText: string): ParsedUnifiedDiff {
  const rawLines = normalizeDiffLines(diffText)
  const headerLines: string[] = []
  const hunks: ParsedPatchHunk[] = []
  let oldPath = ''
  let newPath = ''
  let isNewFile = false
  let isDeletedFile = false
  let active: ParsedPatchHunk | null = null
  let oldNo = 1
  let newNo = 1
  let seenOld = 0
  let seenNew = 0

  for (const line of rawLines) {
    const hunkMatch = HUNK_HEADER_RE.exec(line)

    if (hunkMatch) {
      const oldStart = Number(hunkMatch[1])
      const oldCount = hunkMatch[2] !== undefined ? Number(hunkMatch[2]) : 1
      const newStart = Number(hunkMatch[3])
      const newCount = hunkMatch[4] !== undefined ? Number(hunkMatch[4]) : 1

      active = {
        header: line,
        index: hunks.length,
        lines: [],
        newCount,
        newStart,
        oldCount,
        oldStart,
        sectionHeading: hunkMatch[5] ?? ''
      }
      hunks.push(active)
      oldNo = oldStart
      newNo = newStart
      seenOld = 0
      seenNew = 0

      continue
    }

    if (!active) {
      headerLines.push(line)

      if (line.startsWith('new file mode ')) {
        isNewFile = true
      } else if (line.startsWith('deleted file mode ')) {
        isDeletedFile = true
      } else if (line.startsWith('--- ')) {
        const rest = line.slice(4).trim()

        if (rest === '/dev/null') {
          isNewFile = true
        } else {
          oldPath = stripGitPrefix(rest)
        }
      } else if (line.startsWith('+++ ')) {
        const rest = line.slice(4).trim()

        if (rest === '/dev/null') {
          isDeletedFile = true
        } else {
          newPath = stripGitPrefix(rest)
        }
      }

      continue
    }

    if (line.startsWith('\\')) {
      const prev = active.lines[active.lines.length - 1]

      if (prev) {
        prev.noNewlineAfter = true
      }

      continue
    }

    // Ignore trailing lines once the declared hunk counts are satisfied.
    if (seenOld >= active.oldCount && seenNew >= active.newCount) {
      continue
    }

    if (line.startsWith('+')) {
      active.lines.push({
        kind: 'add',
        newNo: newNo++,
        raw: line,
        text: line.slice(1)
      })
      seenNew += 1
    } else if (line.startsWith('-')) {
      active.lines.push({
        kind: 'remove',
        oldNo: oldNo++,
        raw: line,
        text: line.slice(1)
      })
      seenOld += 1
    } else {
      const text = line.startsWith(' ') ? line.slice(1) : line

      active.lines.push({
        kind: 'context',
        newNo: newNo++,
        oldNo: oldNo++,
        raw: ` ${text}`,
        text
      })
      seenOld += 1
      seenNew += 1
    }
  }

  return {
    headerLines,
    hunks,
    isDeletedFile,
    isNewFile,
    newPath,
    oldPath
  }
}

function buildPatchHeader(
  parsed: ParsedUnifiedDiff,
  filePath: string | undefined,
  oldCount: number,
  newCount: number
): string[] {
  const resolvedPath = (filePath?.trim() || parsed.newPath || parsed.oldPath).replace(/^\/+/, '')

  if (!resolvedPath) {
    throw new Error('Cannot synthesize patch without a target file path')
  }

  // Whole-file reverse of a newly created file -> preserve `new file mode` + `/dev/null`
  // so `git apply --reverse` removes the file cleanly.
  if (parsed.isNewFile && oldCount === 0 && parsed.hunks.length === 1) {
    const modeLine = parsed.headerLines.find(line => line.startsWith('new file mode ')) ?? 'new file mode 100644'

    return [`diff --git a/${resolvedPath} b/${resolvedPath}`, modeLine, '--- /dev/null', `+++ b/${resolvedPath}`]
  }

  // Whole-file reverse of a deleted file -> preserve `deleted file mode` + `/dev/null`
  // so `git apply --reverse` restores the file cleanly.
  if (parsed.isDeletedFile && newCount === 0 && parsed.hunks.length === 1) {
    const modeLine = parsed.headerLines.find(line => line.startsWith('deleted file mode ')) ?? 'deleted file mode 100644'

    return [`diff --git a/${resolvedPath} b/${resolvedPath}`, modeLine, `--- a/${resolvedPath}`, '+++ /dev/null']
  }

  // Standard modification (or partial revert of a new/deleted file where the file stays non-empty).
  const existingDiff = parsed.headerLines.find(line => line.startsWith('diff --git '))
  const existingOld = parsed.headerLines.find(line => line.startsWith('--- ') && line.slice(4).trim() !== '/dev/null')
  const existingNew = parsed.headerLines.find(line => line.startsWith('+++ ') && line.slice(4).trim() !== '/dev/null')

  if (!filePath && existingDiff && existingOld && existingNew) {
    return [existingDiff, existingOld, existingNew]
  }

  return [`diff --git a/${resolvedPath} b/${resolvedPath}`, `--- a/${resolvedPath}`, `+++ b/${resolvedPath}`]
}

function formatHunkRange(start: number, count: number): string {
  return count === 1 ? `${start}` : `${start},${count}`
}

export interface BuildRevertPatchOptions {
  /** 0-based hunk index inside `diffText`. */
  hunkIndex: number
  /**
   * 0-based indices within `hunk.lines` to revert.
   * When omitted, every `+` and `-` line in the hunk is reverted.
   */
  selectedLineIndices?: readonly number[]
  /** Optional explicit repo-relative file path override. */
  filePath?: string
}

/**
 * Synthesize a valid minimal unified patch for `git apply --reverse [--cached] --unidiff-zero`.
 * Throws if the hunk index is invalid or no changed lines are selected.
 */
export function buildRevertPatch(diffText: string, options: BuildRevertPatchOptions): string {
  const parsed = parseUnifiedDiff(diffText)
  const hunk = parsed.hunks[options.hunkIndex]

  if (!hunk) {
    throw new Error(`Hunk index ${options.hunkIndex} is out of range (${parsed.hunks.length} hunks)`)
  }

  const selected = new Set<number>()

  if (options.selectedLineIndices === undefined) {
    for (let i = 0; i < hunk.lines.length; i += 1) {
      if (hunk.lines[i].kind !== 'context') {
        selected.add(i)
      }
    }
  } else {
    for (const index of options.selectedLineIndices) {
      const line = hunk.lines[index]

      if (line && line.kind !== 'context') {
        selected.add(index)
      }
    }
  }

  if (selected.size === 0) {
    throw new Error('No changed lines selected to revert')
  }

  const bodyLines: string[] = []
  let oldCount = 0
  let newCount = 0
  let changedLines = 0

  for (let i = 0; i < hunk.lines.length; i += 1) {
    const line = hunk.lines[i]

    if (line.kind === 'context') {
      bodyLines.push(` ${line.text}`)
      oldCount += 1
      newCount += 1

      if (line.noNewlineAfter) {
        bodyLines.push('\\ No newline at end of file')
      }
    } else if (line.kind === 'add') {
      if (selected.has(i)) {
        bodyLines.push(`+${line.text}`)
        newCount += 1
        changedLines += 1
      } else {
        // Keep unselected added line in the target by converting it to context.
        bodyLines.push(` ${line.text}`)
        oldCount += 1
        newCount += 1
      }

      if (line.noNewlineAfter) {
        bodyLines.push('\\ No newline at end of file')
      }
    } else if (selected.has(i)) {
      // Selected removal: keep as '-' so `git apply --reverse` restores it.
      bodyLines.push(`-${line.text}`)
      oldCount += 1
      changedLines += 1

      if (line.noNewlineAfter) {
        bodyLines.push('\\ No newline at end of file')
      }
    }
    // Unselected removal: absent from current target and should remain absent -> omit.
  }

  if (changedLines === 0) {
    throw new Error('No changed lines selected to revert')
  }

  // `git apply --reverse` anchors on the `+` side against the current file.
  // Because unselected lines prior to this hunk remain in their NEW state, the
  // single-hunk patch starts at `hunk.newStart` on both sides (except when one
  // side is empty, where unified diff convention uses line 0).
  const newStart = newCount === 0 ? 0 : hunk.newStart
  const oldStart = oldCount === 0 ? 0 : hunk.newStart === 0 ? hunk.oldStart : hunk.newStart
  const hunkHeader = `@@ -${formatHunkRange(oldStart, oldCount)} +${formatHunkRange(newStart, newCount)} @@`
  const headerLines = buildPatchHeader(parsed, options.filePath, oldCount, newCount)

  return `${[...headerLines, hunkHeader, ...bodyLines].join('\n')}\n`
}

export function buildHunkRevertPatch(diffText: string, hunkIndex: number, filePath?: string): string {
  return buildRevertPatch(diffText, { filePath, hunkIndex })
}

export function buildLinesRevertPatch(
  diffText: string,
  hunkIndex: number,
  selectedLineIndices: readonly number[],
  filePath?: string
): string {
  return buildRevertPatch(diffText, { filePath, hunkIndex, selectedLineIndices })
}
