import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import net from 'node:net'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import {
  addWorktree,
  allocateWorktreePort,
  canonicalWorktreePath,
  ensureGitRepo,
  fnv1a32,
  inspectWorktreeBeforeClose,
  listBaseBranches,
  listBranches,
  parseWorktrees,
  preferredWorktreePort,
  readWorktreeMeta,
  resolveWorktreeGitDir,
  sanitizeBranch,
  seedWorktreeIncludes,
  switchBranch
} from './git-worktree-ops'

test('sanitizeBranch: spaces → hyphens, forbidden chars dropped, edges trimmed', () => {
  assert.equal(sanitizeBranch('beach vibes'), 'beach-vibes')
  assert.equal(sanitizeBranch('feat/cool thing'), 'feat/cool-thing')
  assert.equal(sanitizeBranch('  wip~^:? '), 'wip')
  assert.equal(sanitizeBranch('///'), '')
})

test('parseWorktrees: main checkout + linked worktree', () => {
  const out = [
    'worktree /repo',
    'HEAD abc123',
    'branch refs/heads/main',
    '',
    'worktree /repo/.worktrees/feat',
    'HEAD def456',
    'branch refs/heads/hermes/feat',
    ''
  ].join('\n')

  const trees = parseWorktrees(out)

  assert.equal(trees.length, 2)
  assert.equal(trees[0].path, '/repo')
  assert.equal(trees[0].branch, 'main')
  assert.equal(trees[1].path, '/repo/.worktrees/feat')
  assert.equal(trees[1].branch, 'hermes/feat')
})

test('parseWorktrees: detached + locked flags', () => {
  const out = ['worktree /repo/wt', 'HEAD abc', 'detached', 'locked reason', ''].join('\n')
  const trees = parseWorktrees(out)

  assert.equal(trees.length, 1)
  assert.equal(trees[0].detached, true)
  assert.equal(trees[0].locked, true)
  assert.equal(trees[0].branch, null)
})

test('parseWorktrees: empty input', () => {
  assert.deepEqual(parseWorktrees(''), [])
})

test('ensureGitRepo: inits a plain dir with a root commit so worktrees branch', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-wt-'))
  const git = (...args) => execFileSync('git', args, { cwd: dir }).toString().trim()

  try {
    await ensureGitRepo('git', dir)
    assert.match(git('rev-parse', '--verify', 'HEAD'), /^[0-9a-f]{7,}$/)

    // The whole point: a worktree can now branch off the seeded root commit.
    execFileSync('git', ['worktree', 'add', '-b', 'wt', path.join(dir, '.worktrees', 'wt')], { cwd: dir })
    assert.ok(fs.existsSync(path.join(dir, '.worktrees', 'wt')))

    // Idempotent: an already-committed repo gets no extra commit.
    await ensureGitRepo('git', dir)
    assert.equal(git('rev-list', '--count', 'HEAD'), '1')
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('switchBranch: switches a normal checkout branch', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-switch-'))
  const git = (...args) => execFileSync('git', args, { cwd: dir }).toString().trim()

  try {
    await ensureGitRepo('git', dir)
    execFileSync('git', ['branch', 'feature'], { cwd: dir })

    await switchBranch(dir, 'feature', 'git')

    assert.equal(git('branch', '--show-current'), 'feature')
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('listBranches: lists locals and flags the checked-out branch', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-branches-'))

  try {
    await ensureGitRepo('git', dir)
    const current = execFileSync('git', ['branch', '--show-current'], { cwd: dir }).toString().trim()
    execFileSync('git', ['branch', 'feature'], { cwd: dir })

    const branches = await listBranches(dir, 'git')
    const names = branches.map(b => b.name).sort()

    assert.deepEqual(names, [current, 'feature'].sort())
    // The repo's own checkout is flagged; the unused branch is convertible.
    assert.equal(branches.find(b => b.name === current).checkedOut, true)
    assert.equal(branches.find(b => b.name === current).isDefault, true)
    assert.equal(fs.realpathSync(branches.find(b => b.name === current).worktreePath), fs.realpathSync(dir))
    assert.equal(branches.find(b => b.name === 'feature').checkedOut, false)
    assert.equal(branches.find(b => b.name === 'feature').isDefault, false)
    assert.equal(branches.find(b => b.name === 'feature').worktreePath, null)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('listBranches: flags a free default branch as default, not checked out', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-branches-default-'))
  const git = (...args) => execFileSync('git', args, { cwd: dir }).toString().trim()

  try {
    await ensureGitRepo('git', dir)
    const trunk = git('branch', '--show-current')
    execFileSync('git', ['switch', '-c', 'rawr'], { cwd: dir })

    const branches = await listBranches(dir, 'git')
    const defaultBranch = branches.find(b => b.name === trunk)

    assert.equal(defaultBranch.checkedOut, false)
    assert.equal(defaultBranch.isDefault, true)
    assert.equal(defaultBranch.worktreePath, null)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('listBranches: a branch claimed by a worktree is flagged checked out', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-branches-wt-'))

  try {
    await ensureGitRepo('git', dir)
    execFileSync('git', ['branch', 'feature'], { cwd: dir })
    // addWorktree converts the existing "feature" branch into a worktree.
    const result = await addWorktree(dir, { existingBranch: 'feature' }, 'git')

    assert.equal(result.branch, 'feature')
    assert.ok(fs.existsSync(result.path))

    const branches = await listBranches(dir, 'git')

    assert.equal(branches.find(b => b.name === 'feature').checkedOut, true)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('listBranches: empty on a non-repo path', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-nonrepo-'))

  try {
    assert.deepEqual(await listBranches(dir, 'git'), [])
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('addWorktree: existingBranch checks the branch out without a new branch', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-convert-'))
  const git = (...args) => execFileSync('git', args, { cwd: dir }).toString().trim()

  try {
    await ensureGitRepo('git', dir)
    execFileSync('git', ['branch', 'cool/feature'], { cwd: dir })

    const before = git('branch', '--list').split('\n').length
    const result = await addWorktree(dir, { existingBranch: 'cool/feature' }, 'git')

    // No new branch was created — only the existing one is checked out.
    assert.equal(git('branch', '--list').split('\n').length, before)
    assert.equal(result.branch, 'cool/feature')
    // Dir is named off the branch slug, nested under the main repo's .worktrees.
    assert.match(result.path, /[/\\]\.worktrees[/\\]cool-feature/)
    assert.equal(
      execFileSync('git', ['branch', '--show-current'], { cwd: result.path }).toString().trim(),
      'cool/feature'
    )
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('addWorktree: existing default branch switches the main checkout, not .worktrees/main', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-convert-default-'))
  const git = (...args) => execFileSync('git', args, { cwd: dir }).toString().trim()

  try {
    await ensureGitRepo('git', dir)
    const trunk = git('branch', '--show-current')
    execFileSync('git', ['switch', '-c', 'rawr'], { cwd: dir })

    const result = await addWorktree(dir, { existingBranch: trunk }, 'git')

    assert.equal(result.branch, trunk)
    assert.equal(fs.realpathSync(result.path), fs.realpathSync(dir))
    assert.equal(git('branch', '--show-current'), trunk)
    assert.equal(fs.existsSync(path.join(dir, '.worktrees', trunk)), false)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('listBaseBranches: lists local branches and flags the default', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-base-branches-'))
  const git = (...args) => execFileSync('git', args, { cwd: dir }).toString().trim()

  try {
    await ensureGitRepo('git', dir)
    const trunk = git('branch', '--show-current')
    execFileSync('git', ['branch', 'feature'], { cwd: dir })

    const branches = await listBaseBranches(dir, 'git')
    const names = branches.map(b => b.name).sort()

    assert.deepEqual(names, [trunk, 'feature'].sort())
    // No remote → all local.
    assert.equal(
      branches.every(b => !b.isRemote),
      true
    )
    // The trunk is flagged as the default.
    assert.equal(branches.find(b => b.name === trunk).isDefault, true)
    assert.equal(branches.find(b => b.name === 'feature').isDefault, false)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('listBaseBranches: empty on a non-repo path', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-base-nonrepo-'))

  try {
    assert.deepEqual(await listBaseBranches(dir, 'git'), [])
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('addWorktree: base param branches off a specified local branch', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-base-add-'))
  const git = (...args) => execFileSync('git', args, { cwd: dir }).toString().trim()

  try {
    await ensureGitRepo('git', dir)
    execFileSync('git', ['branch', 'staging'], { cwd: dir })

    const result = await addWorktree(
      dir,
      { base: 'staging', branch: 'new-from-staging', name: 'new-from-staging' },
      'git'
    )

    assert.equal(result.branch, 'new-from-staging')
    assert.equal(git('-C', result.path, 'merge-base', 'HEAD', 'staging').length > 0, true)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('addWorktree: base origin/main does not set up upstream tracking', async () => {
  // Two repos: a bare "remote" and a clone, so origin/main resolves as a
  // remote-tracking ref — the condition that triggers auto-tracking.
  const remoteDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-remote-'))
  const cloneDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-clone-'))
  const git = (...args) => execFileSync('git', args, { cwd: cloneDir }).toString().trim()

  try {
    // Seed the remote with a commit on main. Inline identity so it works
    // on CI runners with no global git config.
    execFileSync('git', ['init', '-b', 'main', remoteDir])
    execFileSync('git', [
      '-C',
      remoteDir,
      '-c',
      'user.email=hermes@localhost',
      '-c',
      'user.name=Hermes',
      'commit',
      '--allow-empty',
      '-m',
      'root'
    ])

    // Clone so origin/main exists as a remote-tracking ref.
    execFileSync('git', ['clone', remoteDir, cloneDir])

    const result = await addWorktree(
      cloneDir,
      { base: 'origin/main', branch: 'feature-branch', name: 'feature-branch' },
      'git'
    )

    assert.equal(result.branch, 'feature-branch')

    // The new branch must NOT have an upstream — like `git checkout origin/main
    // && git checkout -b feature-branch`, not `git worktree add -b … origin/main`.
    let hasUpstream = true

    try {
      execFileSync('git', ['-C', result.path, 'rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}'])
    } catch {
      hasUpstream = false
    }

    assert.equal(hasUpstream, false)
  } finally {
    fs.rmSync(remoteDir, { recursive: true, force: true })
    fs.rmSync(cloneDir, { recursive: true, force: true })
  }
})

// A tag-pinned narrow clone (`--single-branch --branch v0`), the shape older
// installers made: remote.origin.fetch maps only the tag, so no branch has a
// tracking ref. The remote has `main` and `feature` one commit past the tag.
// Returns both paths and the tip. The caller must remove them.
const IDENT = ['-c', 'user.email=hermes@localhost', '-c', 'user.name=Hermes']

function seedNarrowClone(label) {
  const remoteDir = fs.mkdtempSync(path.join(os.tmpdir(), `hermes-${label}-remote-`))
  const cloneDir = fs.mkdtempSync(path.join(os.tmpdir(), `hermes-${label}-clone-`))
  execFileSync('git', ['init', '-q', '-b', 'main', remoteDir])
  execFileSync('git', ['-C', remoteDir, ...IDENT, 'commit', '-q', '--allow-empty', '-m', 'root'])
  execFileSync('git', ['-C', remoteDir, 'tag', 'v0'])
  execFileSync('git', ['-C', remoteDir, ...IDENT, 'commit', '-q', '--allow-empty', '-m', 'tip'])
  execFileSync('git', ['-C', remoteDir, 'branch', 'feature'])
  const tip = execFileSync('git', ['-C', remoteDir, 'rev-parse', 'HEAD']).toString().trim()

  execFileSync('git', ['clone', '-q', '--single-branch', '--branch', 'v0', remoteDir, cloneDir])

  return { cloneDir, remoteDir, tip }
}

test('addWorktree: base origin/main resolves on a tag-pinned narrow clone', async () => {
  // A by-name `git fetch origin main` on a tag-only refspec writes FETCH_HEAD
  // and never origin/main.
  const { cloneDir, remoteDir, tip } = seedNarrowClone('narrow-base')

  try {
    const result = await addWorktree(cloneDir, { base: 'origin/main', branch: 'from-main', name: 'from-main' }, 'git')

    const head = execFileSync('git', ['-C', result.path, 'rev-parse', 'HEAD']).toString().trim()

    assert.equal(head, tip)
    assert.equal(result.branch, 'from-main')
    assert.throws(() =>
      execFileSync('git', ['-C', cloneDir, 'rev-parse', '--abbrev-ref', '--symbolic-full-name', 'from-main@{u}'], {
        stdio: 'ignore'
      })
    )
  } finally {
    fs.rmSync(remoteDir, { recursive: true, force: true })
    fs.rmSync(cloneDir, { recursive: true, force: true })
  }
})

const fetchConfig = cloneDir =>
  execFileSync('git', ['-C', cloneDir, 'config', '--get-all', 'remote.origin.fetch']).toString().trim()

test('addWorktree: a remote branch on a tag-pinned narrow clone becomes a tracking local branch', async () => {
  const { cloneDir, remoteDir, tip } = seedNarrowClone('narrow-convert')

  try {
    const result = await addWorktree(cloneDir, { existingBranch: 'origin/feature' }, 'git')

    const inTree = (...args) =>
      execFileSync('git', ['-C', result.path, ...args])
        .toString()
        .trim()

    assert.equal(result.branch, 'feature')
    assert.equal(inTree('rev-parse', 'HEAD'), tip)
    assert.equal(inTree('rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}'), 'origin/feature')
  } finally {
    fs.rmSync(remoteDir, { recursive: true, force: true })
    fs.rmSync(cloneDir, { recursive: true, force: true })
  }
})

test('addWorktree: a remote branch that does not exist leaves the fetch config alone', async () => {
  // A configured refspec whose source is missing makes every later plain
  // `git fetch` fail, so a typo must never be registered on the remote.
  const narrow = seedNarrowClone('narrow-typo')
  const normal = seedRemoteAndClone('normal-typo', [])

  try {
    for (const cloneDir of [narrow.cloneDir, normal.cloneDir]) {
      const before = fetchConfig(cloneDir)

      await assert.rejects(addWorktree(cloneDir, { existingBranch: 'origin/typo' }, 'git'))
      assert.equal(fetchConfig(cloneDir), before)
      execFileSync('git', ['-C', cloneDir, 'fetch', '-q'])
    }
  } finally {
    for (const dir of [narrow.remoteDir, narrow.cloneDir, normal.remoteDir, normal.cloneDir]) {
      fs.rmSync(dir, { recursive: true, force: true })
    }
  }
})

test('addWorktree: converting a remote branch on a normal clone adds no fetch refspec', async () => {
  const { cloneDir, remoteDir } = seedRemoteAndClone('normal-convert-config', ['teammate-work'])

  try {
    const before = fetchConfig(cloneDir)

    await addWorktree(cloneDir, { existingBranch: 'origin/teammate-work' }, 'git')
    assert.equal(fetchConfig(cloneDir), before)
  } finally {
    fs.rmSync(remoteDir, { recursive: true, force: true })
    fs.rmSync(cloneDir, { recursive: true, force: true })
  }
})

test('addWorktree: a local slash branch named like a remote branch stays local', async () => {
  // Without this, "origin/feature" (a local branch) became a new `feature`
  // tracking the remote branch of the same name, after a network fetch.
  const { cloneDir, remoteDir, tip } = seedNarrowClone('narrow-local-slash')

  try {
    execFileSync('git', ['-C', cloneDir, 'branch', 'origin/feature'])
    const local = execFileSync('git', ['-C', cloneDir, 'rev-parse', 'refs/heads/origin/feature']).toString().trim()

    assert.notEqual(local, tip)

    const result = await addWorktree(cloneDir, { existingBranch: 'origin/feature' }, 'git')
    const head = execFileSync('git', ['-C', result.path, 'rev-parse', 'HEAD']).toString().trim()
    const remoteRefs = execFileSync('git', ['-C', cloneDir, 'for-each-ref', 'refs/remotes']).toString().trim()

    assert.equal(result.branch, 'origin/feature')
    assert.equal(head, local)
    assert.equal(remoteRefs, '')
  } finally {
    fs.rmSync(remoteDir, { recursive: true, force: true })
    fs.rmSync(cloneDir, { recursive: true, force: true })
  }
})

test('addWorktree: a valid branch the sanitizer would rewrite is still refreshed as a base', async () => {
  const { cloneDir, remoteDir } = seedRemoteAndClone('plus-base', ['fix+1'])

  try {
    execFileSync('git', ['-C', remoteDir, 'checkout', '-q', 'fix+1'])
    execFileSync('git', ['-C', remoteDir, ...IDENT, 'commit', '-q', '--allow-empty', '-m', 'moved after the clone'])
    const moved = execFileSync('git', ['-C', remoteDir, 'rev-parse', 'HEAD']).toString().trim()

    const result = await addWorktree(cloneDir, { base: 'origin/fix+1', branch: 'x', name: 'x' }, 'git')
    const head = execFileSync('git', ['-C', result.path, 'rev-parse', 'HEAD']).toString().trim()

    assert.equal(head, moved)
  } finally {
    fs.rmSync(remoteDir, { recursive: true, force: true })
    fs.rmSync(cloneDir, { recursive: true, force: true })
  }
})

test('addWorktree: a glob base is not turned into a fetch of every branch', async () => {
  const { cloneDir, remoteDir } = seedNarrowClone('narrow-glob')

  try {
    await assert.rejects(addWorktree(cloneDir, { base: 'origin/*', name: 'glob' }, 'git'))

    const remoteRefs = execFileSync('git', ['-C', cloneDir, 'for-each-ref', 'refs/remotes']).toString().trim()

    assert.equal(remoteRefs, '')
  } finally {
    fs.rmSync(remoteDir, { recursive: true, force: true })
    fs.rmSync(cloneDir, { recursive: true, force: true })
  }
})

// A pair of repos: a bare "remote" with `main` and the extra branches in
// `branches`, plus a clone of it. Returns both paths. The caller must remove
// them.
function seedRemoteAndClone(label, branches) {
  const remoteDir = fs.mkdtempSync(path.join(os.tmpdir(), `hermes-${label}-remote-`))
  const cloneDir = fs.mkdtempSync(path.join(os.tmpdir(), `hermes-${label}-clone-`))

  const remoteGit = (...args) =>
    execFileSync('git', ['-C', remoteDir, ...args])
      .toString()
      .trim()

  execFileSync('git', ['init', '-b', 'main', remoteDir])
  remoteGit('-c', 'user.email=hermes@localhost', '-c', 'user.name=Hermes', 'commit', '--allow-empty', '-m', 'root')

  for (const branch of branches) {
    remoteGit('branch', branch)
  }

  execFileSync('git', ['clone', remoteDir, cloneDir])

  return { cloneDir, remoteDir }
}

test('listBranches: offers remote branches that have no local counterpart', async () => {
  const { cloneDir, remoteDir } = seedRemoteAndClone('branches-remote', ['teammate-work'])

  try {
    const branches = await listBranches(cloneDir, 'git')
    const byName = new Map(branches.map(b => [b.name, b]))

    // The teammate's branch is only on the remote. The list therefore offers it
    // by its remote-tracking name, with a flag that lets the UI say "track
    // remote".
    const remoteOnly = byName.get('origin/teammate-work')

    assert.ok(remoteOnly)
    assert.equal(remoteOnly.isRemote, true)
    assert.equal(remoteOnly.checkedOut, false)
    assert.equal(remoteOnly.isDefault, false)
    assert.equal(remoteOnly.worktreePath, null)

    // `main` is checked out locally, so it shows once as a local branch.
    // "origin/main" is a duplicate of a branch that is already in the list.
    assert.equal(byName.get('main').isRemote, false)
    assert.equal(byName.has('origin/main'), false)

    // "origin/HEAD" is an alias for the default branch of the remote. It is not
    // a branch.
    assert.equal(
      branches.some(b => b.name.endsWith('/HEAD')),
      false
    )
  } finally {
    fs.rmSync(remoteDir, { recursive: true, force: true })
    fs.rmSync(cloneDir, { recursive: true, force: true })
  }
})

test('addWorktree: a remote branch becomes a local branch tracking it', async () => {
  const { cloneDir, remoteDir } = seedRemoteAndClone('convert-remote', ['teammate-work'])

  try {
    const result = await addWorktree(cloneDir, { existingBranch: 'origin/teammate-work' }, 'git')

    const inTree = (...args) =>
      execFileSync('git', ['-C', result.path, ...args])
        .toString()
        .trim()

    // The worktree is on a local branch that has the name of the remote one. It
    // is not on a detached HEAD, which is the result of a checkout of
    // "origin/teammate-work".
    assert.equal(result.branch, 'teammate-work')
    assert.equal(inTree('branch', '--show-current'), 'teammate-work')
    assert.match(result.path, /[/\\]\.worktrees[/\\]teammate-work/)

    // The branch tracks the remote branch, so push and pull work with no more
    // setup.
    assert.equal(inTree('rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}'), 'origin/teammate-work')
  } finally {
    fs.rmSync(remoteDir, { recursive: true, force: true })
    fs.rmSync(cloneDir, { recursive: true, force: true })
  }
})

test('addWorktree: a remote default branch gets its own worktree, not a home switch', async () => {
  const { cloneDir, remoteDir } = seedRemoteAndClone('convert-remote-default', [])

  const git = (...args) =>
    execFileSync('git', ['-C', cloneDir, ...args])
      .toString()
      .trim()

  try {
    // Move the main checkout off `main`, which makes "origin/main" convertible.
    // The local `main` is then free, but the request names the remote-tracking
    // ref.
    git('switch', '-c', 'rawr')
    git('branch', '-D', 'main')

    const result = await addWorktree(cloneDir, { existingBranch: 'origin/main' }, 'git')

    // "switch home" applies to a local default branch. A remote ref always gets
    // a new worktree, so the main checkout stays where the user put it.
    assert.equal(result.branch, 'main')
    assert.notEqual(fs.realpathSync(result.path), fs.realpathSync(cloneDir))
    assert.equal(git('branch', '--show-current'), 'rawr')
  } finally {
    fs.rmSync(remoteDir, { recursive: true, force: true })
    fs.rmSync(cloneDir, { recursive: true, force: true })
  }
})

test('switchBranch: non-repo dir short-circuits instead of throwing', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-sw-'))

  try {
    // A plain folder pinned as a project (no .git): its lane label is the
    // folder basename, not a branch — switching must no-op, not error, so
    // callers like "+" new session can proceed with a plain session.
    const result = await switchBranch(dir, '国创大赛', 'git')

    assert.deepEqual(result, { branch: null })
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('switchBranch: repo dir still validates the branch name and switches', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-sw-'))

  try {
    execFileSync('git', ['init', '-b', 'main'], { cwd: dir })
    execFileSync('git', ['config', 'user.email', 't@example.com'], { cwd: dir })
    execFileSync('git', ['config', 'user.name', 'test'], { cwd: dir })
    execFileSync('git', ['commit', '--allow-empty', '-m', 'root'], { cwd: dir })

    // Existing behaviour preserved: an illegal branch name still errors.
    await assert.rejects(() => switchBranch(dir, '///', 'git'), /Branch name is required/)

    // And switching to a real branch still works.
    const result = await switchBranch(dir, 'main', 'git')
    assert.deepEqual(result, { branch: 'main' })
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

// Exact same 20-path parity vector asserted in
// tests/hermes_cli/test_worktree_lifecycle.py.
const FNV1A_PARITY_VECTORS: Array<[string, string, number, number]> = [
  ['/', '/', 705468254, 24254],
  ['/repo/.worktrees/feat-a', '/repo/.worktrees/feat-a', 3426949478, 24478],
  ['/repo/.worktrees/feat-a/', '/repo/.worktrees/feat-a', 3426949478, 24478],
  ['/repo//.worktrees/./feat-a', '/repo/.worktrees/feat-a', 3426949478, 24478],
  ['/repo/sub/../.worktrees/feat-b', '/repo/.worktrees/feat-b', 3410171859, 24859],
  ['/workspace/wt-p0-worktree-lifecycle', '/workspace/wt-p0-worktree-lifecycle', 295605217, 24217],
  ['/workspace/wt-p0-inline-diff-review', '/workspace/wt-p0-inline-diff-review', 3861790023, 24023],
  ['/home/dev/my project/.worktrees/branch-1', '/home/dev/my project/.worktrees/branch-1', 2343543345, 24345],
  ['/srv/app/.worktrees/subagent-01234567', '/srv/app/.worktrees/subagent-01234567', 873649153, 24153],
  ['/srv/app/.worktrees/subagent-89abcdef', '/srv/app/.worktrees/subagent-89abcdef', 4001957601, 24601],
  [
    '/opt/repos/hermes-agent/.worktrees/fix-utf8-åäö',
    '/opt/repos/hermes-agent/.worktrees/fix-utf8-åäö',
    937307044,
    24044
  ],
  [
    '/opt/repos/hermes-agent/.worktrees/日本語-ブランチ',
    '/opt/repos/hermes-agent/.worktrees/日本語-ブランチ',
    688011444,
    24444
  ],
  ['/var/lib/ci/workspace-alpha/.worktrees/pr-42', '/var/lib/ci/workspace-alpha/.worktrees/pr-42', 2059532272, 24272],
  ['/var/lib/ci/workspace-beta/.worktrees/pr-42', '/var/lib/ci/workspace-beta/.worktrees/pr-42', 376157556, 24556],
  ['C:\\Users\\dev\\repo\\.worktrees\\feat-win', 'C:/Users/dev/repo/.worktrees/feat-win', 4281742425, 24425],
  ['c:/Users/dev/repo/.worktrees/feat-win/', 'C:/Users/dev/repo/.worktrees/feat-win', 4281742425, 24425],
  ['D:\\work\\a\\..\\b\\.worktrees\\task-9', 'D:/work/b/.worktrees/task-9', 2868306633, 24633],
  ['/a/b/c/d/e/f/g/h/i/j/.worktrees/deep-tree', '/a/b/c/d/e/f/g/h/i/j/.worktrees/deep-tree', 285225627, 24627],
  [
    '/repo/.worktrees/feat_with_underscores.and.dots-123',
    '/repo/.worktrees/feat_with_underscores.and.dots-123',
    564407567,
    24567
  ],
  ['/repo/.worktrees/edge-case@2026+build#7', '/repo/.worktrees/edge-case@2026+build#7', 4151808203, 24203]
]

test('fnv1a32 & preferredWorktreePort: exact 20-path parity with Python', () => {
  assert.equal(FNV1A_PARITY_VECTORS.length, 20)

  for (const [rawPath, expectedCanonical, expectedHash, expectedPort] of FNV1A_PARITY_VECTORS) {
    const canonical = canonicalWorktreePath(rawPath)

    assert.equal(canonical, expectedCanonical, `canonical mismatch for ${rawPath}`)
    assert.equal(fnv1a32(canonical), expectedHash, `hash mismatch for ${rawPath}`)
    const port = preferredWorktreePort(rawPath)

    assert.equal(port, expectedPort, `port mismatch for ${rawPath}`)
    assert.ok(port >= 24000 && port < 25000)
  }
})

test('allocateWorktreePort: persists metadata and falls back linearly on bind collision', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-wt-port-'))

  try {
    execFileSync('git', ['init', '-b', 'main'], { cwd: dir })
    execFileSync('git', ['config', 'user.email', 't@example.com'], { cwd: dir })
    execFileSync('git', ['config', 'user.name', 'test'], { cwd: dir })
    execFileSync('git', ['commit', '--allow-empty', '-m', 'root'], { cwd: dir })

    const wtDir = path.join(dir, '.worktrees', 'port-collision')

    execFileSync('git', ['worktree', 'add', wtDir, '-b', 'feat/port-collision'], { cwd: dir })

    const preferred = preferredWorktreePort(wtDir)
    const expectedNext = 24000 + ((preferred - 24000 + 1) % 1000)

    const blocker = net.createServer()

    await new Promise<void>((resolve, reject) => {
      blocker.once('error', reject)
      blocker.listen({ host: '127.0.0.1', port: preferred, exclusive: true }, () => resolve())
    })

    try {
      const allocated = await allocateWorktreePort(wtDir, {
        probeBind: true,
        branch: 'feat/port-collision',
        baseRef: 'main'
      })

      assert.equal(allocated, expectedNext)

      const gitDir = await resolveWorktreeGitDir(wtDir)

      assert.ok(gitDir)
      assert.ok(fs.existsSync(path.join(gitDir, 'hermes-worktree-meta.json')))

      const meta = await readWorktreeMeta(wtDir)

      assert.ok(meta)
      assert.equal(meta.port, expectedNext)
      assert.equal(meta.branch, 'feat/port-collision')
      assert.equal(meta.baseRef, 'main')

      // Re-allocating reuses the persisted free port deterministically.
      assert.equal(await allocateWorktreePort(wtDir, { probeBind: true }), expectedNext)
    } finally {
      await new Promise<void>(resolve => blocker.close(() => resolve()))
    }
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('addWorktree & seedWorktreeIncludes: seeds .env* with 0600, enforces guardrails, and returns port', async () => {
  const outer = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-wt-seed-'))
  const dir = path.join(outer, 'repo')

  fs.mkdirSync(dir)

  try {
    execFileSync('git', ['init', '-b', 'main'], { cwd: dir })
    execFileSync('git', ['config', 'user.email', 't@example.com'], { cwd: dir })
    execFileSync('git', ['config', 'user.name', 'test'], { cwd: dir })
    fs.writeFileSync(path.join(dir, '.env.example'), 'KEY=example\n')
    fs.writeFileSync(
      path.join(dir, '.gitignore'),
      '.env\n.env.*\n!.env.example\n*.local.json\nnode_modules/\n.venv/\ntarget/\n.next/\ndist/\n'
    )
    execFileSync('git', ['add', '.'], { cwd: dir })
    execFileSync('git', ['commit', '-m', 'init'], { cwd: dir })

    // Valid ignored .env
    fs.writeFileSync(path.join(dir, '.env'), 'SECRET=hello\n')
    // Unmatched ignored file
    fs.writeFileSync(path.join(dir, 'extra.local.json'), '{"skip":true}\n')
    // Symlink escaping repo root
    const outsideFile = path.join(outer, 'outside-secret.txt')

    fs.writeFileSync(outsideFile, 'ESCAPE=1\n')
    fs.symlinkSync(outsideFile, path.join(dir, '.env.escape'))
    // > 10 MB file
    const hugePath = path.join(dir, '.env.huge')
    const fd = fs.openSync(hugePath, 'w')

    fs.writeSync(fd, Buffer.from('x'), 0, 1, 10 * 1024 * 1024 + 1)
    fs.closeSync(fd)

    // Heavy directories
    for (const heavy of ['node_modules', '.venv', 'target', '.next', 'dist']) {
      fs.mkdirSync(path.join(dir, heavy), { recursive: true })
      fs.writeFileSync(path.join(dir, heavy, '.env'), `HEAVY=${heavy}\n`)
    }

    const res = await addWorktree(dir, { name: 'seeded' }, 'git')

    assert.ok(res.port >= 24000 && res.port < 25000)
    assert.deepEqual(res.seededFiles, ['.env'])
    assert.equal(fs.readFileSync(path.join(res.path, '.env'), 'utf8'), 'SECRET=hello\n')
    assert.equal(fs.existsSync(path.join(res.path, 'extra.local.json')), false)
    assert.equal(fs.existsSync(path.join(res.path, '.env.escape')), false)
    assert.equal(fs.existsSync(path.join(res.path, '.env.huge')), false)

    for (const heavy of ['node_modules', '.venv', 'target', '.next', 'dist']) {
      assert.equal(fs.existsSync(path.join(res.path, heavy, '.env')), false)
    }

    if (process.platform !== 'win32') {
      assert.equal(fs.statSync(path.join(res.path, '.env')).mode & 0o777, 0o600)
    }

    // Re-seeding must never overwrite existing files.
    fs.writeFileSync(path.join(res.path, '.env'), 'EDITED=keep\n')
    const second = await seedWorktreeIncludes(dir, res.path, 'git')

    assert.deepEqual(second, [])
    assert.equal(fs.readFileSync(path.join(res.path, '.env'), 'utf8'), 'EDITED=keep\n')
  } finally {
    fs.rmSync(outer, { recursive: true, force: true })
  }
})

test('seedWorktreeIncludes: custom .worktreeinclude supports globs, negation, and explicit heavy dirs', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-wt-custom-'))

  try {
    execFileSync('git', ['init', '-b', 'main'], { cwd: dir })
    execFileSync('git', ['config', 'user.email', 't@example.com'], { cwd: dir })
    execFileSync('git', ['config', 'user.name', 'test'], { cwd: dir })
    fs.writeFileSync(path.join(dir, '.gitignore'), '*.local.json\nsecrets/\ndist/\nnode_modules/\n')
    fs.writeFileSync(
      path.join(dir, '.worktreeinclude'),
      '# custom rules\n*.local.json\nsecrets/**\n!secrets/skip.txt\ndist/.env\n'
    )
    execFileSync('git', ['add', '.'], { cwd: dir })
    execFileSync('git', ['commit', '-m', 'init'], { cwd: dir })

    fs.writeFileSync(path.join(dir, 'app.local.json'), '{"a":1}\n')
    fs.mkdirSync(path.join(dir, 'secrets'), { recursive: true })
    fs.writeFileSync(path.join(dir, 'secrets', 'keep.txt'), 'keep\n')
    fs.writeFileSync(path.join(dir, 'secrets', 'skip.txt'), 'skip\n')
    fs.mkdirSync(path.join(dir, 'dist'), { recursive: true })
    fs.writeFileSync(path.join(dir, 'dist', '.env'), 'DIST=1\n')
    fs.mkdirSync(path.join(dir, 'node_modules'), { recursive: true })
    fs.writeFileSync(path.join(dir, 'node_modules', '.env'), 'NM=1\n')

    const res = await addWorktree(dir, { name: 'custom-inc' }, 'git')

    assert.ok(res.seededFiles.includes('app.local.json'))
    assert.ok(res.seededFiles.includes('secrets/keep.txt'))
    assert.ok(res.seededFiles.includes('dist/.env'))
    assert.equal(res.seededFiles.includes('secrets/skip.txt'), false)
    assert.equal(fs.existsSync(path.join(res.path, 'secrets', 'skip.txt')), false)
    assert.equal(fs.existsSync(path.join(res.path, 'node_modules', '.env')), false)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('inspectWorktreeBeforeClose: clean, dirty, unpushed (upstream & no-upstream), and detached HEAD', async () => {
  const outer = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-wt-inspect-'))
  const remoteDir = path.join(outer, 'remote.git')
  const repoDir = path.join(outer, 'repo')

  try {
    execFileSync('git', ['init', '--bare', '-b', 'main', remoteDir])
    execFileSync('git', ['clone', remoteDir, repoDir])
    execFileSync('git', ['config', 'user.email', 't@example.com'], { cwd: repoDir })
    execFileSync('git', ['config', 'user.name', 'test'], { cwd: repoDir })
    fs.writeFileSync(path.join(repoDir, 'file.txt'), 'v1\n')
    execFileSync('git', ['add', '.'], { cwd: repoDir })
    execFileSync('git', ['commit', '-m', 'initial'], { cwd: repoDir })
    execFileSync('git', ['push', '-u', 'origin', 'main'], { cwd: repoDir })

    // 1. Clean worktree with no upstream
    const wt = await addWorktree(repoDir, { name: 'inspect-wt', branch: 'feat/inspect-wt' }, 'git')
    const clean = await inspectWorktreeBeforeClose(wt.path, 'git')

    assert.equal(clean.dirtyFiles, 0)
    assert.equal(clean.unpushedCommits, 0)
    assert.equal(clean.branch, 'feat/inspect-wt')
    assert.equal(clean.port, wt.port)

    // 2. Dirty worktree (1 modified tracked + 1 untracked = 2 dirty files)
    fs.writeFileSync(path.join(wt.path, 'file.txt'), 'v2\n')
    fs.writeFileSync(path.join(wt.path, 'untracked.txt'), 'new\n')
    const dirty = await inspectWorktreeBeforeClose(wt.path, 'git')

    assert.equal(dirty.dirtyFiles, 2)
    assert.equal(dirty.unpushedCommits, 0)

    // 3. Unpushed commits without upstream (compared against baseRef)
    execFileSync('git', ['add', '.'], { cwd: wt.path })
    execFileSync('git', ['commit', '-m', 'commit 1'], { cwd: wt.path })
    fs.writeFileSync(path.join(wt.path, 'file2.txt'), 'two\n')
    execFileSync('git', ['add', '.'], { cwd: wt.path })
    execFileSync('git', ['commit', '-m', 'commit 2'], { cwd: wt.path })

    const noUpstream = await inspectWorktreeBeforeClose(wt.path, 'git')

    assert.equal(noUpstream.dirtyFiles, 0)
    assert.equal(noUpstream.unpushedCommits, 2)
    assert.equal(noUpstream.branch, 'feat/inspect-wt')

    // 4. Push branch to establish upstream -> unpushed becomes 0, then add 1 local commit -> unpushed becomes 1
    execFileSync('git', ['push', '-u', 'origin', 'feat/inspect-wt'], { cwd: wt.path })
    const pushed = await inspectWorktreeBeforeClose(wt.path, 'git')

    assert.equal(pushed.unpushedCommits, 0)

    fs.writeFileSync(path.join(wt.path, 'file3.txt'), 'three\n')
    execFileSync('git', ['add', '.'], { cwd: wt.path })
    execFileSync('git', ['commit', '-m', 'commit 3'], { cwd: wt.path })

    const aheadOfUpstream = await inspectWorktreeBeforeClose(wt.path, 'git')

    assert.equal(aheadOfUpstream.unpushedCommits, 1)

    // 5. Detached HEAD still inspects dirtyFiles, unpushedCommits, and port accurately
    execFileSync('git', ['checkout', '--detach', 'HEAD'], { cwd: wt.path })
    fs.writeFileSync(path.join(wt.path, 'detached-dirty.txt'), 'x\n')
    const detached = await inspectWorktreeBeforeClose(wt.path, 'git')

    assert.equal(detached.branch, null)
    assert.equal(detached.dirtyFiles, 1)
    assert.equal(detached.unpushedCommits, 3)
    assert.equal(detached.port, wt.port)
  } finally {
    fs.rmSync(outer, { recursive: true, force: true })
  }
})
