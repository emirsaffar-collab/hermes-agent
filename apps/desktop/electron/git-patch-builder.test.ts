import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, describe, expect, it } from 'vitest'

import { buildHunkRevertPatch, buildLinesRevertPatch, parseUnifiedDiff } from './git-patch-builder'

const tempDirs: string[] = []

afterEach(() => {
  for (const dir of tempDirs.splice(0)) {
    fs.rmSync(dir, { force: true, recursive: true })
  }
})

function initRepo(files: Record<string, string>): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-patch-builder-'))

  tempDirs.push(dir)
  execFileSync('git', ['init', '-q'], { cwd: dir })
  execFileSync('git', ['config', 'user.email', 'test@hermes.local'], { cwd: dir })
  execFileSync('git', ['config', 'user.name', 'Hermes Test'], { cwd: dir })

  for (const [rel, content] of Object.entries(files)) {
    const full = path.join(dir, rel)

    fs.mkdirSync(path.dirname(full), { recursive: true })
    fs.writeFileSync(full, content, 'utf8')
  }

  execFileSync('git', ['add', '-A'], { cwd: dir })
  execFileSync('git', ['commit', '-qm', 'initial'], { cwd: dir })

  return dir
}

function gitDiff(dir: string, filePath: string, staged = false): string {
  const args = staged ? ['diff', '--cached', '--no-color', '--', filePath] : ['diff', '--no-color', '--', filePath]

  return execFileSync('git', args, { cwd: dir, encoding: 'utf8' })
}

function applyReversePatch(dir: string, patchText: string, staged = false): void {
  const args = ['apply', '--reverse', ...(staged ? ['--cached'] : []), '--unidiff-zero', '--whitespace=nowarn', '-']

  execFileSync('git', args, { cwd: dir, encoding: 'utf8', input: patchText })
}

describe('git-patch-builder E2E against real git repos', () => {
  it('reverts a single whole hunk while leaving another hunk untouched (unstaged)', () => {
    const original = Array.from({ length: 20 }, (_, i) => `line ${i + 1}`).join('\n') + '\n'
    const dir = initRepo({ 'demo.txt': original })

    const modifiedLines = original.trimEnd().split('\n')

    modifiedLines[1] = 'line 2 EDITED'
    modifiedLines[17] = 'line 18 EDITED'
    fs.writeFileSync(path.join(dir, 'demo.txt'), `${modifiedLines.join('\n')}\n`, 'utf8')

    const diff = gitDiff(dir, 'demo.txt')
    const parsed = parseUnifiedDiff(diff)

    expect(parsed.hunks).toHaveLength(2)

    // Revert only hunk #1 (line 18); keep hunk #0 (line 2).
    const patch = buildHunkRevertPatch(diff, 1)

    applyReversePatch(dir, patch, false)

    const after = fs.readFileSync(path.join(dir, 'demo.txt'), 'utf8').trimEnd().split('\n')

    expect(after[1]).toBe('line 2 EDITED')
    expect(after[17]).toBe('line 18')

    const remainingDiff = gitDiff(dir, 'demo.txt')

    expect(remainingDiff).toContain('+line 2 EDITED')
    expect(remainingDiff).not.toContain('line 18 EDITED')
  })

  it('reverts a partial subset of added (+) lines while keeping unselected additions', () => {
    const original = ['header', 'footer'].join('\n') + '\n'
    const dir = initRepo({ 'add.txt': original })

    fs.writeFileSync(path.join(dir, 'add.txt'), ['header', 'added-1', 'added-2', 'added-3', 'footer'].join('\n') + '\n')

    const diff = gitDiff(dir, 'add.txt')
    const parsed = parseUnifiedDiff(diff)
    const hunk = parsed.hunks[0]

    // Select only 'added-2' to revert.
    const targetIdx = hunk.lines.findIndex(line => line.kind === 'add' && line.text === 'added-2')

    expect(targetIdx).toBeGreaterThanOrEqual(0)

    const patch = buildLinesRevertPatch(diff, 0, [targetIdx])

    applyReversePatch(dir, patch, false)

    const after = fs.readFileSync(path.join(dir, 'add.txt'), 'utf8')

    expect(after).toBe(['header', 'added-1', 'added-3', 'footer'].join('\n') + '\n')
  })

  it('reverts a partial subset of removed (-) lines while keeping other deletions', () => {
    const original = ['top', 'drop-1', 'drop-2', 'drop-3', 'bottom'].join('\n') + '\n'
    const dir = initRepo({ 'rem.txt': original })

    fs.writeFileSync(path.join(dir, 'rem.txt'), ['top', 'bottom'].join('\n') + '\n')

    const diff = gitDiff(dir, 'rem.txt')
    const parsed = parseUnifiedDiff(diff)
    const hunk = parsed.hunks[0]

    // Restore only 'drop-2'; keep 'drop-1' and 'drop-3' deleted.
    const targetIdx = hunk.lines.findIndex(line => line.kind === 'remove' && line.text === 'drop-2')

    expect(targetIdx).toBeGreaterThanOrEqual(0)

    const patch = buildLinesRevertPatch(diff, 0, [targetIdx])

    applyReversePatch(dir, patch, false)

    const after = fs.readFileSync(path.join(dir, 'rem.txt'), 'utf8')

    expect(after).toBe(['top', 'drop-2', 'bottom'].join('\n') + '\n')
  })

  it('reverts a partial selection of mixed (-) and (+) lines in the same hunk', () => {
    const original = ['ctx-start', 'old-1', 'old-2', 'ctx-end'].join('\n') + '\n'
    const dir = initRepo({ 'mix.txt': original })

    fs.writeFileSync(path.join(dir, 'mix.txt'), ['ctx-start', 'new-1', 'new-2', 'ctx-end'].join('\n') + '\n')

    const diff = gitDiff(dir, 'mix.txt')
    const parsed = parseUnifiedDiff(diff)
    const hunk = parsed.hunks[0]

    // Revert 'old-1' (restore it) and 'new-2' (remove it); keep 'old-2' deleted and 'new-1' added.
    const restoreOld1 = hunk.lines.findIndex(line => line.kind === 'remove' && line.text === 'old-1')
    const dropNew2 = hunk.lines.findIndex(line => line.kind === 'add' && line.text === 'new-2')

    const patch = buildLinesRevertPatch(diff, 0, [restoreOld1, dropNew2])

    applyReversePatch(dir, patch, false)

    const after = fs.readFileSync(path.join(dir, 'mix.txt'), 'utf8')

    expect(after).toBe(['ctx-start', 'old-1', 'new-1', 'ctx-end'].join('\n') + '\n')
  })

  it('reverts staged changes via --cached without touching unselected staged lines', () => {
    const original = ['a', 'b', 'c'].join('\n') + '\n'
    const dir = initRepo({ 'staged.txt': original })

    fs.writeFileSync(path.join(dir, 'staged.txt'), ['a', 'b-changed', 'extra-staged', 'c'].join('\n') + '\n')
    execFileSync('git', ['add', 'staged.txt'], { cwd: dir })

    const stagedDiff = gitDiff(dir, 'staged.txt', true)
    const hunk = parseUnifiedDiff(stagedDiff).hunks[0]
    const extraIdx = hunk.lines.findIndex(line => line.kind === 'add' && line.text === 'extra-staged')

    const patch = buildLinesRevertPatch(stagedDiff, 0, [extraIdx])

    applyReversePatch(dir, patch, true)

    // Index should no longer contain 'extra-staged', while keeping 'b-changed'.
    const stagedContent = execFileSync('git', ['show', ':staged.txt'], { cwd: dir, encoding: 'utf8' })

    expect(stagedContent).toBe(['a', 'b-changed', 'c'].join('\n') + '\n')

    // And the remaining cached diff matches only b -> b-changed.
    const nextStagedDiff = gitDiff(dir, 'staged.txt', true)

    expect(nextStagedDiff).toContain('+b-changed')
    expect(nextStagedDiff).not.toContain('extra-staged')
  })

  it('reverts a whole hunk cleanly on a staged file via --cached', () => {
    const original = Array.from({ length: 18 }, (_, i) => `row ${i + 1}`).join('\n') + '\n'
    const dir = initRepo({ 'multi.txt': original })
    const edited = original.trimEnd().split('\n')

    edited[0] = 'row 1 staged'
    edited[16] = 'row 17 staged'
    fs.writeFileSync(path.join(dir, 'multi.txt'), `${edited.join('\n')}\n`)
    execFileSync('git', ['add', 'multi.txt'], { cwd: dir })

    const stagedDiff = gitDiff(dir, 'multi.txt', true)
    const patch = buildHunkRevertPatch(stagedDiff, 0)

    applyReversePatch(dir, patch, true)

    const stagedContent = execFileSync('git', ['show', ':multi.txt'], { cwd: dir, encoding: 'utf8' }).trimEnd().split('\n')

    expect(stagedContent[0]).toBe('row 1')
    expect(stagedContent[16]).toBe('row 17 staged')
  })

  it('rejects empty selection or out-of-range hunk index', () => {
    const diff = ['diff --git a/f.txt b/f.txt', '--- a/f.txt', '+++ b/f.txt', '@@ -1,2 +1,2 @@', ' keep', '-old', '+new'].join('\n')

    expect(() => buildHunkRevertPatch(diff, 2)).toThrow(/out of range/i)
    expect(() => buildLinesRevertPatch(diff, 0, [0])).toThrow(/no changed lines/i)
    expect(() => buildLinesRevertPatch(diff, 0, [])).toThrow(/no changed lines/i)
  })
})
