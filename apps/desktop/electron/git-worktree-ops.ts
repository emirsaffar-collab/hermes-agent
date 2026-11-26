// Git-driven worktree operations for the desktop "Start work" flow: spin up a
// fresh worktree the lightest way (`git worktree add -b`), list real worktrees,
// and remove them. Git is the source of truth; the renderer just drives these.

import { execFile } from 'node:child_process'
import fs from 'node:fs'
import net from 'node:net'
import path from 'node:path'

import { resolveRequestedPathForIpc } from './hardening'
import { execGit } from './no-console-git'

const WORKTREE_PORT_BASE = 24000
const WORKTREE_PORT_SPAN = 1000
const WORKTREE_PORT_PROBE_SLOTS = 32
const FNV1A_32_OFFSET = 0x811c9dc5
const FNV1A_32_PRIME = 0x01000193
const WORKTREE_META_FILENAME = 'hermes-worktree-meta.json'
const WORKTREE_INCLUDE_MAX_FILE_BYTES = 10 * 1024 * 1024
const WORKTREE_INCLUDE_DEFAULT_PATTERNS = ['.env*']
const WORKTREE_INCLUDE_HEAVY_DIRS = new Set(['node_modules', '.venv', 'target', '.next', 'dist'])
const WIN_DRIVE_RE = /^([A-Za-z]):(\/.*)?$/
const GLOB_META_RE = /[*?[]/

export interface WorktreeMeta {
  version: number
  port: number
  worktreePath: string
  updatedAt: string
  branch?: string
  baseRef?: string
  [key: string]: unknown
}

export interface AllocateWorktreePortOptions {
  probeBind?: boolean
  branch?: string
  baseRef?: string
  gitBin?: string
}

export interface WorktreeCloseInspection {
  dirtyFiles: number
  unpushedCommits: number
  branch: string | null
  port: number | null
}

function execFileGit(
  gitBin: string,
  args: string[],
  cwd: string
): Promise<{ code: number; stdout: string; stderr: string }> {
  return new Promise(resolve => {
    execFile(
      gitBin,
      args,
      {
        cwd,
        encoding: 'utf8',
        maxBuffer: 16 * 1024 * 1024,
        timeout: 30_000,
        windowsHide: true
      },
      (error, stdout, stderr) => {
        if (error) {
          const exitCode = typeof error.code === 'number' ? error.code : 1

          resolve({
            code: exitCode,
            stdout: typeof stdout === 'string' ? stdout : '',
            stderr: typeof stderr === 'string' ? stderr : error.message
          })

          return
        }

        resolve({
          code: 0,
          stdout: typeof stdout === 'string' ? stdout : '',
          stderr: typeof stderr === 'string' ? stderr : ''
        })
      }
    )
  })
}

// Compute unsigned 32-bit FNV-1a hash of UTF-8 text. Matches Python `fnv1a_32`
// in `hermes_cli/worktree_ops.py` bit-for-bit across ASCII and UTF-8 strings.
function fnv1a32(text: string): number {
  const bytes = Buffer.from(String(text), 'utf8')
  let hash = FNV1A_32_OFFSET

  for (let i = 0; i < bytes.length; i += 1) {
    hash ^= bytes[i]
    hash = Math.imul(hash, FNV1A_32_PRIME) >>> 0
  }

  return hash >>> 0
}

function normalizeWinDrivePath(drive: string, restRaw: string | undefined): string {
  const rest = (restRaw || '/').replace(/^\/+/, '/')
  let normRest = path.posix.normalize(rest)

  if (normRest === '.') {
    normRest = '/'
  }

  if (!normRest.startsWith('/')) {
    normRest = `/${normRest}`
  }

  if (normRest.length > 1) {
    normRest = normRest.replace(/\/+$/, '')
  }

  return `${drive.toUpperCase()}:${normRest}`
}

// Normalize `wtPath` to a canonical POSIX absolute path for deterministic hashing.
// Matches Python `canonical_worktree_path` in `hermes_cli/worktree_ops.py`.
function canonicalWorktreePath(wtPath: string): string {
  const raw = String(wtPath || '').trim()

  if (!raw) {
    return '/'
  }

  let normalized = raw.replace(/\\/g, '/')
  const winMatch = WIN_DRIVE_RE.exec(normalized)

  if (winMatch) {
    return normalizeWinDrivePath(winMatch[1], winMatch[2])
  }

  if (!normalized.startsWith('/')) {
    normalized = path.resolve(raw).replace(/\\/g, '/')
  }

  if (fs.existsSync(normalized)) {
    try {
      normalized = fs.realpathSync(normalized).replace(/\\/g, '/')
    } catch {
      // Keep lexical normalization when realpath cannot resolve.
    }
  }

  const winAfter = WIN_DRIVE_RE.exec(normalized)

  if (winAfter) {
    return normalizeWinDrivePath(winAfter[1], winAfter[2])
  }

  normalized = normalized.replace(/^\/+/, '/')
  let norm = path.posix.normalize(normalized)

  if (norm.length > 1) {
    norm = norm.replace(/\/+$/, '')
  }

  return norm
}

function preferredWorktreePort(wtPath: string): number {
  const canonical = canonicalWorktreePath(wtPath)

  return WORKTREE_PORT_BASE + (fnv1a32(canonical) % WORKTREE_PORT_SPAN)
}

function isValidWorktreePort(value: unknown): value is number {
  return (
    typeof value === 'number' &&
    Number.isInteger(value) &&
    value >= WORKTREE_PORT_BASE &&
    value < WORKTREE_PORT_BASE + WORKTREE_PORT_SPAN
  )
}

function isTcpPortAvailable(port: number, host = '127.0.0.1'): Promise<boolean> {
  return new Promise(resolve => {
    const server = net.createServer()

    server.unref()
    server.once('error', () => {
      resolve(false)
    })
    server.listen({ host, port, exclusive: true }, () => {
      server.close(() => resolve(true))
    })
  })
}

async function resolveWorktreeGitDir(wtPath: string, gitBin = 'git'): Promise<string | null> {
  if (!wtPath || !fs.existsSync(wtPath)) {
    return null
  }

  const dotGit = path.join(wtPath, '.git')

  try {
    if (fs.existsSync(dotGit)) {
      const stat = fs.statSync(dotGit)

      if (stat.isDirectory()) {
        return fs.realpathSync(dotGit)
      }

      if (stat.isFile()) {
        const content = fs
          .readFileSync(dotGit, 'utf8')
          .replace(/^\uFEFF/, '')
          .trim()

        if (content.startsWith('gitdir:')) {
          const target = content.slice('gitdir:'.length).trim()

          if (target) {
            return fs.realpathSync(path.resolve(wtPath, target))
          }
        }
      }
    }

    const res = await execFileGit(gitBin, ['rev-parse', '--git-dir'], wtPath)

    if (res.code === 0 && res.stdout.trim()) {
      return fs.realpathSync(path.resolve(wtPath, res.stdout.trim()))
    }
  } catch {
    return null
  }

  return null
}

async function readWorktreeMeta(wtPath: string, gitBin = 'git'): Promise<WorktreeMeta | null> {
  const gitDir = await resolveWorktreeGitDir(wtPath, gitBin)

  if (!gitDir) {
    return null
  }

  const metaPath = path.join(gitDir, WORKTREE_META_FILENAME)

  try {
    if (!fs.existsSync(metaPath)) {
      return null
    }

    const raw = fs.readFileSync(metaPath, 'utf8').replace(/^\uFEFF/, '')
    const parsed: unknown = JSON.parse(raw)

    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return parsed as WorktreeMeta
    }
  } catch {
    return null
  }

  return null
}

async function writeWorktreeMeta(wtPath: string, meta: WorktreeMeta, gitBin = 'git'): Promise<string | null> {
  const gitDir = await resolveWorktreeGitDir(wtPath, gitBin)

  if (!gitDir) {
    return null
  }

  const metaPath = path.join(gitDir, WORKTREE_META_FILENAME)
  const tmpPath = path.join(gitDir, `.${WORKTREE_META_FILENAME}.${process.pid}.${Date.now()}.tmp`)

  try {
    fs.writeFileSync(tmpPath, `${JSON.stringify(meta, null, 2)}\n`, 'utf8')
    fs.renameSync(tmpPath, metaPath)

    return metaPath
  } catch {
    try {
      if (fs.existsSync(tmpPath)) {
        fs.unlinkSync(tmpPath)
      }
    } catch {
      // Best-effort temp cleanup.
    }

    return null
  }
}

async function allocateWorktreePort(wtPath: string, options: AllocateWorktreePortOptions = {}): Promise<number> {
  const probeBind = options.probeBind !== false
  const gitBin = options.gitBin || 'git'
  const canonical = canonicalWorktreePath(wtPath)
  const existing: Partial<WorktreeMeta> = (await readWorktreeMeta(wtPath, gitBin)) || {}
  const savedPort = existing.port

  let chosen: number | null = null

  if (isValidWorktreePort(savedPort)) {
    if (!probeBind || (await isTcpPortAvailable(savedPort))) {
      chosen = savedPort
    }
  }

  if (chosen === null) {
    const baseOffset = fnv1a32(canonical) % WORKTREE_PORT_SPAN
    const preferred = WORKTREE_PORT_BASE + baseOffset

    if (!probeBind) {
      chosen = preferred
    } else {
      for (let slot = 0; slot < WORKTREE_PORT_PROBE_SLOTS; slot += 1) {
        const candidate = WORKTREE_PORT_BASE + ((baseOffset + slot) % WORKTREE_PORT_SPAN)

        if (await isTcpPortAvailable(candidate)) {
          chosen = candidate

          break
        }
      }

      if (chosen === null) {
        chosen = preferred
      }
    }
  }

  const meta: WorktreeMeta = {
    ...existing,
    version: 1,
    port: chosen,
    worktreePath: canonical,
    updatedAt: new Date().toISOString()
  }

  if (options.branch) {
    meta.branch = options.branch
  }

  if (options.baseRef) {
    meta.baseRef = options.baseRef
  }

  await writeWorktreeMeta(wtPath, meta, gitBin)

  return chosen
}

function isWithinRoot(candidate: string, root: string): boolean {
  const rel = path.relative(root, candidate)

  return rel === '' || (!rel.startsWith('..') && !path.isAbsolute(rel))
}

function pathExistsOrSymlink(target: string): boolean {
  try {
    fs.lstatSync(target)

    return true
  } catch {
    return false
  }
}

function worktreeIncludeEntries(repoRoot: string): { hasCustom: boolean; patterns: string[] } {
  const includeFile = path.join(repoRoot, '.worktreeinclude')

  if (!fs.existsSync(includeFile)) {
    return { hasCustom: false, patterns: [...WORKTREE_INCLUDE_DEFAULT_PATTERNS] }
  }

  try {
    if (!fs.statSync(includeFile).isFile()) {
      return { hasCustom: false, patterns: [...WORKTREE_INCLUDE_DEFAULT_PATTERNS] }
    }

    const raw = fs.readFileSync(includeFile, 'utf8').replace(/^\uFEFF/, '')

    const patterns = raw
      .split(/\r?\n/)
      .map(line => line.trim())
      .filter(line => Boolean(line) && !line.startsWith('#'))

    return { hasCustom: true, patterns }
  } catch {
    return { hasCustom: false, patterns: [...WORKTREE_INCLUDE_DEFAULT_PATTERNS] }
  }
}

function escapeRegexChar(ch: string): string {
  return ch.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function globToRegex(pattern: string): RegExp {
  let i = 0
  const n = pattern.length
  const parts: string[] = ['^']

  while (i < n) {
    const c = pattern[i]

    if (c === '*') {
      if (i + 1 < n && pattern[i + 1] === '*') {
        if (i + 2 < n && pattern[i + 2] === '/') {
          parts.push('(?:.*/)?')
          i += 3
        } else {
          parts.push('.*')
          i += 2
        }
      } else {
        parts.push('[^/]*')
        i += 1
      }
    } else if (c === '?') {
      parts.push('[^/]')
      i += 1
    } else if (c === '[') {
      let j = i + 1

      if (j < n && (pattern[j] === '!' || pattern[j] === '^')) {
        j += 1
      }

      if (j < n && pattern[j] === ']') {
        j += 1
      }

      while (j < n && pattern[j] !== ']') {
        j += 1
      }

      if (j >= n) {
        parts.push(escapeRegexChar(c))
        i += 1
      } else {
        let inner = pattern.slice(i + 1, j)

        if (inner.startsWith('!')) {
          inner = `^${inner.slice(1)}`
        }

        parts.push(`[${inner}]`)
        i = j + 1
      }
    } else {
      parts.push(escapeRegexChar(c))
      i += 1
    }
  }

  parts.push('$')

  return new RegExp(parts.join(''))
}

function matchSingleIncludePattern(relPosix: string, pattern: string): boolean {
  const pat = pattern.trim().replace(/\\/g, '/')

  if (!pat) {
    return false
  }

  const anchored = pat.startsWith('/')
  const dirOnly = pat.endsWith('/')
  const cleaned = pat.replace(/^\/+/, '').replace(/\/+$/, '')

  if (!cleaned) {
    return false
  }

  if (dirOnly) {
    if (cleaned.includes('/') || anchored) {
      if (relPosix === cleaned || relPosix.startsWith(`${cleaned}/`)) {
        return true
      }

      return globToRegex(`${cleaned}/**`).test(relPosix)
    }

    const segRe = globToRegex(cleaned)

    return relPosix
      .split('/')
      .slice(0, -1)
      .some(seg => segRe.test(seg))
  }

  if (!cleaned.includes('/') && !anchored) {
    const regex = globToRegex(cleaned)
    const basename = relPosix.includes('/') ? relPosix.slice(relPosix.lastIndexOf('/') + 1) : relPosix

    if (regex.test(relPosix) || regex.test(basename)) {
      return true
    }

    if (!GLOB_META_RE.test(cleaned)) {
      return relPosix.startsWith(`${cleaned}/`)
    }

    return false
  }

  if (globToRegex(cleaned).test(relPosix)) {
    return true
  }

  if (!GLOB_META_RE.test(cleaned)) {
    return relPosix.startsWith(`${cleaned}/`)
  }

  return false
}

function patternExplicitlyNamesDir(pattern: string, dirName: string): boolean {
  const cleaned = pattern.trim().replace(/\\/g, '/').replace(/^\/+/, '').replace(/\/+$/, '')

  return cleaned.split('/').filter(Boolean).includes(dirName)
}

function copySeedFile(srcResolved: string, dst: string, wtRootResolved: string, relPosix: string): boolean {
  try {
    const stat = fs.statSync(srcResolved)

    if (!stat.isFile()) {
      return false
    }

    if (pathExistsOrSymlink(dst)) {
      return false
    }

    if (stat.size > WORKTREE_INCLUDE_MAX_FILE_BYTES) {
      return false
    }

    const dstParent = path.dirname(dst)

    if (!isWithinRoot(path.resolve(dst), wtRootResolved)) {
      return false
    }

    fs.mkdirSync(dstParent, { recursive: true })
    const dstParentResolved = fs.realpathSync(dstParent)

    if (!isWithinRoot(dstParentResolved, wtRootResolved)) {
      return false
    }

    fs.copyFileSync(srcResolved, dst, fs.constants.COPYFILE_EXCL)

    if (path.posix.basename(relPosix).startsWith('.env') && process.platform !== 'win32') {
      fs.chmodSync(dst, 0o600)
    }

    return true
  } catch {
    return false
  }
}

async function seedWorktreeIncludes(mainRootPath: string, wtPath: string, gitBin = 'git'): Promise<string[]> {
  const mainRootResolved = fs.realpathSync(mainRootPath)
  const wtRootResolved = fs.realpathSync(wtPath)
  const { hasCustom, patterns } = worktreeIncludeEntries(mainRootResolved)

  if (patterns.length === 0) {
    return []
  }

  const seeded: string[] = []
  const seededSet = new Set<string>()
  const symlinkedDirs: string[] = []

  // Pass 1: explicit non-glob literal entries in `.worktreeinclude` (symlinks
  // directories and copies explicitly named untracked files).
  if (hasCustom) {
    for (const entry of patterns) {
      if (entry.startsWith('!') || GLOB_META_RE.test(entry)) {
        continue
      }

      const cleanedEntry = entry.replace(/\\/g, '/').replace(/^\/+/, '')

      if (!cleanedEntry) {
        continue
      }

      const src = path.resolve(mainRootResolved, cleanedEntry)
      const dst = path.resolve(wtRootResolved, cleanedEntry)

      if (!isWithinRoot(dst, wtRootResolved)) {
        continue
      }

      let srcResolved: string

      try {
        if (!fs.existsSync(src)) {
          continue
        }

        srcResolved = fs.realpathSync(src)
      } catch {
        continue
      }

      if (!isWithinRoot(srcResolved, mainRootResolved)) {
        continue
      }

      const relPosix = cleanedEntry.replace(/\/+$/, '')

      try {
        const stat = fs.statSync(srcResolved)

        if (stat.isDirectory()) {
          if (!pathExistsOrSymlink(dst)) {
            fs.mkdirSync(path.dirname(dst), { recursive: true })

            try {
              fs.symlinkSync(srcResolved, dst, process.platform === 'win32' ? 'junction' : 'dir')
            } catch {
              if (process.platform !== 'win32') {
                continue
              }

              fs.cpSync(srcResolved, dst, { recursive: true })
            }

            symlinkedDirs.push(relPosix)

            if (!seededSet.has(relPosix)) {
              seededSet.add(relPosix)
              seeded.push(relPosix)
            }
          }
        } else if (stat.isFile()) {
          if (copySeedFile(srcResolved, dst, wtRootResolved, relPosix) && !seededSet.has(relPosix)) {
            seededSet.add(relPosix)
            seeded.push(relPosix)
          }
        }
      } catch {
        continue
      }
    }
  }

  // Pass 2: gitignored files discovered via `git ls-files -z --others --ignored --exclude-standard`.
  const lsRes = await execFileGit(
    gitBin,
    ['ls-files', '-z', '--others', '--ignored', '--exclude-standard'],
    mainRootResolved
  )

  if (lsRes.code !== 0) {
    return seeded
  }

  for (const relRaw of lsRes.stdout.split('\0')) {
    if (!relRaw) {
      continue
    }

    const relPosix = relRaw.replace(/\\/g, '/').replace(/^\/+/, '')

    if (!relPosix || seededSet.has(relPosix)) {
      continue
    }

    const parts = relPosix.split('/')

    if (parts.includes('..') || parts[0] === '.git' || parts[0] === '.worktrees') {
      continue
    }

    if (symlinkedDirs.some(dir => relPosix === dir || relPosix.startsWith(`${dir}/`))) {
      continue
    }

    let matchedPattern: string | null = null

    for (const pat of patterns) {
      if (pat.startsWith('!')) {
        if (matchSingleIncludePattern(relPosix, pat.slice(1))) {
          matchedPattern = null
        }
      } else if (matchSingleIncludePattern(relPosix, pat)) {
        matchedPattern = pat
      }
    }

    if (!matchedPattern) {
      continue
    }

    const parentParts = parts.slice(0, -1)
    let blockedHeavy = false

    for (const seg of parentParts) {
      if (WORKTREE_INCLUDE_HEAVY_DIRS.has(seg)) {
        if (!(hasCustom && patternExplicitlyNamesDir(matchedPattern, seg))) {
          blockedHeavy = true

          break
        }
      }
    }

    if (blockedHeavy) {
      continue
    }

    const src = path.resolve(mainRootResolved, relPosix)
    const dst = path.resolve(wtRootResolved, relPosix)

    let srcResolved: string

    try {
      if (!fs.existsSync(src)) {
        continue
      }

      srcResolved = fs.realpathSync(src)
    } catch {
      continue
    }

    if (!isWithinRoot(srcResolved, mainRootResolved)) {
      continue
    }

    if (copySeedFile(srcResolved, dst, wtRootResolved, relPosix)) {
      seededSet.add(relPosix)
      seeded.push(relPosix)
    }
  }

  return seeded
}

async function inspectWorktreeBeforeClose(wtPath: string, gitBin = 'git'): Promise<WorktreeCloseInspection> {
  const resolved = resolveRequestedPathForIpc(wtPath, { purpose: 'Worktree close inspect' })

  const [statusRes, branchRes, meta] = await Promise.all([
    execFileGit(gitBin, ['status', '--porcelain'], resolved),
    execFileGit(gitBin, ['symbolic-ref', '--quiet', '--short', 'HEAD'], resolved),
    readWorktreeMeta(resolved, gitBin)
  ])

  const dirtyFiles = statusRes.code === 0 ? statusRes.stdout.split(/\r?\n/).filter(line => line.length > 0).length : 0

  const branch = branchRes.code === 0 && branchRes.stdout.trim() ? branchRes.stdout.trim() : null
  const port = meta && isValidWorktreePort(meta.port) ? meta.port : null

  let unpushedCommits = 0
  const upstreamRes = await execFileGit(gitBin, ['rev-list', '--count', '@{upstream}..HEAD'], resolved)

  if (upstreamRes.code === 0) {
    const parsed = Number.parseInt(upstreamRes.stdout.trim(), 10)

    unpushedCommits = Number.isFinite(parsed) && parsed > 0 ? parsed : 0
  } else {
    const baseCandidates: string[] = []

    if (meta && typeof meta.baseRef === 'string' && meta.baseRef.trim()) {
      baseCandidates.push(meta.baseRef.trim())
    }

    baseCandidates.push('origin/HEAD', 'origin/main', 'origin/master', 'main', 'master')

    let resolvedCount: number | null = null

    for (const candidate of baseCandidates) {
      if (branch && candidate === branch) {
        continue
      }

      const revRes = await execFileGit(gitBin, ['rev-list', '--count', `${candidate}..HEAD`], resolved)

      if (revRes.code === 0) {
        const parsed = Number.parseInt(revRes.stdout.trim(), 10)

        resolvedCount = Number.isFinite(parsed) && parsed > 0 ? parsed : 0

        break
      }
    }

    if (resolvedCount === null) {
      const remotesRes = await execFileGit(gitBin, ['remote'], resolved)

      if (remotesRes.code === 0 && remotesRes.stdout.trim()) {
        const fallbackRes = await execFileGit(gitBin, ['rev-list', '--count', 'HEAD', '--not', '--remotes'], resolved)

        if (fallbackRes.code === 0) {
          const parsed = Number.parseInt(fallbackRes.stdout.trim(), 10)

          resolvedCount = Number.isFinite(parsed) && parsed > 0 ? parsed : 0
        }
      }
    }

    unpushedCommits = resolvedCount ?? 0
  }

  return {
    dirtyFiles,
    unpushedCommits,
    branch,
    port
  }
}

function runGit(gitBin, args, cwd): Promise<string> {
  return execGit(gitBin, args, { cwd, timeoutMs: 30_000 }).then(result => {
    if (result.code !== 0) {
      const error = new Error(result.stderr || `git exited ${result.code}`) as Error & { stderr?: string }

      error.stderr = result.stderr
      throw error
    }

    return result.stdout
  })
}

// Fetch `<remote>/<branch>` by explicit refspec; true when the remote has the
// branch. A tag-pinned narrow clone maps only the tag in remote.<remote>.fetch,
// so a by-name fetch writes FETCH_HEAD without creating the tracking ref
// (#125686). Same refspec as `hermes update`: the `+` matters on a depth-1
// clone, where the new tip need not descend from the old one.
async function fetchTrackingRef(gitBin, root, remote, branch): Promise<boolean> {
  return gitOk(gitBin, ['fetch', remote, `+refs/heads/${branch}:refs/remotes/${remote}/${branch}`], root)
}

// Parse `git worktree list --porcelain`. The first record is the main worktree.
function parseWorktrees(out) {
  const trees = []
  let cur = null

  for (const line of out.split('\n')) {
    if (line.startsWith('worktree ')) {
      if (cur) {
        trees.push(cur)
      }

      cur = { path: line.slice(9).trim(), branch: null, detached: false, bare: false, locked: false }
    } else if (!cur) {
      continue
    } else if (line.startsWith('branch ')) {
      cur.branch = line
        .slice(7)
        .trim()
        .replace(/^refs\/heads\//, '')
    } else if (line === 'detached') {
      cur.detached = true
    } else if (line === 'bare') {
      cur.bare = true
    } else if (line.startsWith('locked')) {
      cur.locked = true
    }
  }

  if (cur) {
    trees.push(cur)
  }

  return trees
}

async function listWorktrees(repoPath, gitBin) {
  let resolved

  try {
    resolved = resolveRequestedPathForIpc(repoPath, { purpose: 'Worktree list' })
  } catch {
    return []
  }

  try {
    const out = await runGit(gitBin, ['worktree', 'list', '--porcelain'], resolved)

    return parseWorktrees(out).map((tree, index) => ({
      path: tree.path,
      branch: tree.branch,
      isMain: index === 0,
      detached: tree.detached,
      locked: tree.locked
    }))
  } catch {
    return []
  }
}

// A git-ref-safe branch name (spaces → "-", drop forbidden chars, trim edges),
// or "" when nothing usable remains. Mirrors the renderer's `gitRef`, so a bad
// value can't reach `git` no matter the caller (the GUI also enforces live).
function sanitizeBranch(name) {
  return String(name || '')
    .replace(/\s+/g, '-')
    .replace(/[^\w./-]/g, '')
    .replace(/-{2,}/g, '-')
    .replace(/\/{2,}/g, '/')
    .replace(/\.{2,}/g, '.')
    .replace(/^[-./]+|[-./]+$/g, '')
}

function slugify(name) {
  const slug = String(name || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40)
    .replace(/-+$/g, '')

  return slug || 'work'
}

const TRUNK_BRANCHES = ['main', 'master']

async function gitLine(gitBin, args, cwd) {
  try {
    return (await runGit(gitBin, args, cwd)).trim()
  } catch {
    return ''
  }
}

// True when the command exits 0. Use this function and not `gitLine` for a
// `--quiet` probe. A `--quiet` probe prints nothing when it finds the ref, and
// that output is the same as the output of a failure.
async function gitOk(gitBin, args, cwd) {
  try {
    await runGit(gitBin, args, cwd)

    return true
  } catch {
    return false
  }
}

// The remote that a ref belongs to ("origin" for "origin/main"), or "" when the
// name is not a remote-tracking ref in this repo. This function asks git. It
// does not assume that the remote has the name "origin", because a repo can
// give its remotes any name.
async function remoteOfRef(gitBin, cwd, name) {
  if (!name.includes('/')) {
    return ''
  }

  if (!(await gitOk(gitBin, ['show-ref', '--verify', '--quiet', `refs/remotes/${name}`], cwd))) {
    return ''
  }

  return name.slice(0, name.indexOf('/'))
}

async function defaultBranch(gitBin, cwd) {
  const remote = (
    await gitLine(gitBin, ['symbolic-ref', '--quiet', '--short', 'refs/remotes/origin/HEAD'], cwd)
  ).replace(/^origin\//, '')

  if (remote) {
    return remote
  }

  const configured = await gitLine(gitBin, ['config', '--get', 'init.defaultBranch'], cwd)

  if (configured) {
    return configured
  }

  for (const branch of TRUNK_BRANCHES) {
    if (await gitLine(gitBin, ['show-ref', '--verify', `refs/heads/${branch}`], cwd)) {
      return branch
    }
  }

  return ''
}

// A brand-new project folder isn't a git repo — and a freshly-init'd one has no
// commit to branch from — so `git worktree add` would fail. Make the dir a repo
// with a root commit on the user's behalf so worktrees "just work". No-op for a
// repo that already has commits; never touches the user's files (the seed commit
// is `--allow-empty`), and never inits a dir that already lives inside a repo.
async function ensureGitRepo(gitBin, dir) {
  let needsRoot = false

  try {
    const inside = (await runGit(gitBin, ['rev-parse', '--is-inside-work-tree'], dir)).trim()

    if (inside !== 'true') {
      await runGit(gitBin, ['init'], dir)
      needsRoot = true
    } else {
      // Repo exists; a worktree still needs a HEAD to branch from.
      try {
        await runGit(gitBin, ['rev-parse', '--verify', 'HEAD'], dir)
      } catch {
        needsRoot = true
      }
    }
  } catch {
    await runGit(gitBin, ['init'], dir)
    needsRoot = true
  }

  if (needsRoot) {
    // Inline identity so the seed commit lands even with no global git config.
    await runGit(
      gitBin,
      [
        '-c',
        'user.email=hermes@localhost',
        '-c',
        'user.name=Hermes',
        'commit',
        '--allow-empty',
        '-m',
        'Initial commit'
      ],
      dir
    )
  }
}

// Resolve the repo's MAIN worktree root, so `.worktrees/` always nests under the
// primary checkout even when called from a linked worktree.
async function mainRoot(gitBin, cwd) {
  const list = await listWorktrees(cwd, gitBin)
  const main = list.find(tree => tree.isMain)

  return main ? main.path : cwd
}

function uniqueDir(base) {
  let dir = base
  let n = 1

  while (fs.existsSync(dir)) {
    n += 1
    dir = `${base}-${n}`
  }

  return dir
}

async function addExistingBranchWorktree(gitBin, root, name) {
  const requested = sanitizeBranch(name)

  if (!requested) {
    throw new Error('Branch name is required.')
  }

  // "origin/feature" is a remote-tracking ref and not a branch that git can
  // check out. `git worktree add <dir> origin/feature` detaches HEAD. Make a
  // local branch with the same short name that tracks the remote ref. This is
  // what `git switch feature` does for a branch on exactly one remote.
  let remote = await remoteOfRef(gitBin, root, requested)
  let fetched = false

  if (
    !remote &&
    requested.includes('/') &&
    !(await gitOk(gitBin, ['show-ref', '--verify', '--quiet', `refs/heads/${requested}`], root))
  ) {
    // A tag-pinned narrow clone has no tracking ref for any branch, so the
    // ref-based reading above misreads "origin/feature" as a local branch. When
    // no such local branch exists and the remote carries the branch, fetching
    // it creates the ref; otherwise keep the local-branch reading and its error.
    const maybeRemote = requested.slice(0, requested.indexOf('/'))

    if (
      (await gitLine(gitBin, ['remote', 'get-url', maybeRemote], root)) &&
      (await fetchTrackingRef(gitBin, root, maybeRemote, requested.slice(maybeRemote.length + 1)))
    ) {
      remote = maybeRemote
      fetched = true
    }
  }

  const branch = remote ? requested.slice(remote.length + 1) : requested

  if (!remote && branch === (await defaultBranch(gitBin, root))) {
    await runGit(gitBin, ['switch', branch], root)
    const port = await allocateWorktreePort(root, { branch, gitBin })

    return { path: root, branch, repoRoot: root, port, seededFiles: [] as string[] }
  }

  const dir = uniqueDir(path.join(root, '.worktrees', slugify(branch)))

  if (remote) {
    // Best effort freshness: after a failure (offline, branch gone from the
    // remote) the last known ref is still there to branch from.
    fetched = fetched || (await fetchTrackingRef(gitBin, root, remote, branch))

    if (!(await gitOk(gitBin, ['worktree', 'add', '--track', '-b', branch, dir, requested], root))) {
      // `--track` needs remote.<remote>.fetch to map the ref back to a remote
      // branch; a narrow clone maps only its tag. Branch untracked, then
      // register the branch and wire upstream, but only for a branch the fetch
      // just proved exists: a configured refspec whose source is gone makes
      // every later plain `git fetch` fail.
      await runGit(gitBin, ['worktree', 'add', '-b', branch, dir, requested], root)

      if (fetched) {
        await gitOk(gitBin, ['remote', 'set-branches', '--add', remote, branch], root)
        await gitOk(gitBin, ['branch', `--set-upstream-to=${requested}`, branch], root)
      }
    }

    const seededFiles = await seedWorktreeIncludes(root, dir, gitBin)
    const port = await allocateWorktreePort(dir, { branch, baseRef: requested, gitBin })

    return { path: dir, branch, repoRoot: root, port, seededFiles }
  }

  await runGit(gitBin, ['worktree', 'add', dir, branch], root)
  const seededFiles = await seedWorktreeIncludes(root, dir, gitBin)
  const port = await allocateWorktreePort(dir, { branch, gitBin })

  return { path: dir, branch, repoRoot: root, port, seededFiles }
}

async function addWorktree(repoPath, options, gitBin) {
  const resolved = resolveRequestedPathForIpc(repoPath, { purpose: 'Worktree add' })
  // A new project's folder may not be a git repo yet — init it (with a root
  // commit) so the worktree has something to branch from.
  await ensureGitRepo(gitBin, resolved)
  const root = await mainRoot(gitBin, resolved)
  const opts = options || {}

  if (opts.existingBranch) {
    return addExistingBranchWorktree(gitBin, root, opts.existingBranch)
  }

  const slug = slugify(opts.name || `work-${Date.now().toString(36)}`)
  const branch = sanitizeBranch(opts.branch) || `hermes/${slug}`
  const dir = uniqueDir(path.join(root, '.worktrees', slug))

  const args = ['worktree', 'add', '-b', branch, dir]
  let baseRef: string | undefined

  if (opts.base) {
    // Remote-tracking branches may be stale or missing if the user hasn't
    // fetched recently. When the base is an `origin/…` ref, fetch just that
    // branch so `git worktree add -b new origin/main` works against the
    // latest remote commit. Local branches are used as-is.
    const base = String(opts.base)

    baseRef = base

    if (base.startsWith('origin/')) {
      const remoteBranch = base.slice('origin/'.length)

      // `base` comes straight from IPC, and inside a refspec a glob such as
      // "origin/*" would fetch every branch: only fetch valid branch names.
      // The fetch is best effort; git uses the local ref or raises a clear
      // error below if it is entirely missing.
      if (await gitOk(gitBin, ['check-ref-format', '--branch', remoteBranch], root)) {
        await fetchTrackingRef(gitBin, root, 'origin', remoteBranch)
      }

      // When branching off a remote-tracking ref, git auto-sets up tracking
      // (e.g. `new-branch` → tracks `origin/main`). The user almost certainly
      // wants a standalone local branch — like `git checkout origin/main &&
      // git checkout -b new-branch` — not a branch silently wired to the
      // remote's upstream. `--no-track` prevents that.
      args.push('--no-track')
    }

    args.push(base)
  } else {
    baseRef = (await gitLine(gitBin, ['rev-parse', 'HEAD'], root)) || undefined
  }

  try {
    await runGit(gitBin, args, root)
  } catch (err) {
    // Branch name may already exist — retry checking out the existing branch
    // into a fresh worktree dir instead of failing the whole flow.
    if (/already exists/i.test(err.stderr || '')) {
      await runGit(gitBin, ['worktree', 'add', dir, branch], root)
      baseRef = undefined
    } else {
      throw err
    }
  }

  const seededFiles = await seedWorktreeIncludes(root, dir, gitBin)
  const port = await allocateWorktreePort(dir, { branch, baseRef, gitBin })

  return { path: dir, branch, repoRoot: root, port, seededFiles }
}

async function removeWorktree(repoPath, worktreePath, options, gitBin) {
  const resolvedRepo = resolveRequestedPathForIpc(repoPath, { purpose: 'Worktree remove (repo)' })
  const resolvedTree = resolveRequestedPathForIpc(worktreePath, { purpose: 'Worktree remove (tree)' })
  const root = await mainRoot(gitBin, resolvedRepo)
  const args = ['worktree', 'remove']

  if (options && options.force) {
    args.push('--force')
  }

  args.push(resolvedTree)
  await runGit(gitBin, args, root)

  return { removed: resolvedTree }
}

// List the branches for the "convert a branch into a worktree" picker, most
// recently committed first. The local heads come first. Then come the
// remote-tracking refs that have no local branch yet. This is the same set that
// the base-branch picker offers, so "convert" can reach a teammate's branch
// that the user did not check out.
// Each branch carries a flag for a checkout in a worktree, and the path of that
// worktree. Empty on a non-repo or a remote backend, where the probe cannot
// run.
async function listBranches(repoPath, gitBin) {
  let resolved

  try {
    resolved = resolveRequestedPathForIpc(repoPath, { purpose: 'Branch list' })
  } catch {
    return []
  }

  try {
    // Both children own cwd handles: a failed probe must still wait for its
    // sibling before the caller may remove or switch the repository directory.
    const probes = await Promise.allSettled([
      runGit(gitBin, ['for-each-ref', '--format=%(refname:short)', '--sort=-committerdate', 'refs/heads'], resolved),
      runGit(gitBin, ['for-each-ref', '--format=%(refname:short)', '--sort=-committerdate', 'refs/remotes'], resolved)
    ])

    const [local, remote] = probes

    if (local.status === 'rejected' || remote.status === 'rejected') {
      return []
    }

    const [localOut, remoteOut] = [local.value, remote.value]

    const trees = await listWorktrees(resolved, gitBin)
    const pathByBranch = new Map(trees.filter(tree => tree.branch).map(tree => [tree.branch, tree.path]))
    const trunk = await defaultBranch(gitBin, resolved)

    const names = (out: string) =>
      out
        .split('\n')
        .map(line => line.trim())
        .filter(Boolean)

    const locals = names(localOut)
    const localSet = new Set(locals)

    const remotes = names(remoteOut).filter(name => {
      // "origin/HEAD" is a symbolic alias for the default branch of the remote.
      // It is not a branch, and it shows in the list as a duplicate.
      if (name.endsWith('/HEAD')) {
        return false
      }

      // The user reaches a remote branch that they track locally through its
      // local head. To list both is noise, and a checkout of the
      // remote-tracking ref detaches HEAD.
      return !localSet.has(name.slice(name.indexOf('/') + 1))
    })

    return [
      ...locals.map(name => ({
        name,
        checkedOut: pathByBranch.has(name),
        isDefault: Boolean(trunk && name === trunk),
        isRemote: false,
        worktreePath: pathByBranch.get(name) || null
      })),
      ...remotes.map(name => ({
        // A remote branch has no local checkout, and it cannot be the local
        // trunk. It is therefore never checked out and never the default.
        name,
        checkedOut: false,
        isDefault: false,
        isRemote: true,
        worktreePath: null
      }))
    ]
  } catch {
    return []
  }
}

async function switchBranch(repoPath, branch, gitBin) {
  const resolved = resolveRequestedPathForIpc(repoPath, { purpose: 'Branch switch' })

  // Sidebar lanes exist for plain folders too (non-repo explicit projects),
  // and their lane label is the folder basename — not a branch. `git switch`
  // there is meaningless, and sanitizing that label would throw a misleading
  // "Branch name is required." — so short-circuit for non-repo roots and let
  // callers (e.g. "+" new session on the project lane) proceed with a plain
  // session instead of aborting.
  let inside = 'false'

  try {
    inside = (await runGit(gitBin, ['rev-parse', '--is-inside-work-tree'], resolved)).trim()
  } catch {
    // Not a git repo (or git unavailable): fall through to the short-circuit.
  }

  if (inside !== 'true') {
    return { branch: null }
  }

  const target = sanitizeBranch(branch)

  if (!target) {
    throw new Error('Branch name is required.')
  }

  await runGit(gitBin, ['switch', target], resolved)

  return { branch: target }
}

// Branches the new worktree can be based on: local heads + remote-tracking
// refs. Listed most-recently-committed first; the remote's default branch
// (origin/HEAD) is flagged so the UI can preselect it. Empty on a non-repo /
// remote backend where the probe can't run.
async function listBaseBranches(repoPath, gitBin) {
  let resolved

  try {
    resolved = resolveRequestedPathForIpc(repoPath, { purpose: 'Base branch list' })
  } catch {
    return []
  }

  try {
    const out = await runGit(
      gitBin,
      [
        'for-each-ref',
        '--format=%(refname:short)\t%(committerdate:iso)',
        '--sort=-committerdate',
        'refs/heads',
        'refs/remotes'
      ],
      resolved
    )

    const remoteDefault = await gitLine(
      gitBin,
      ['symbolic-ref', '--quiet', '--short', 'refs/remotes/origin/HEAD'],
      resolved
    )

    const localDefault = await defaultBranch(gitBin, resolved)

    return out
      .split('\n')
      .map(line => line.trim())
      .filter(Boolean)
      .map(line => {
        const [name] = line.split('\t')

        return {
          name,
          isRemote: name.startsWith('origin/'),
          // origin/HEAD when a remote exists; otherwise the local default
          // (main/master/init.defaultBranch) so a no-remote repo still flags
          // its trunk.
          isDefault: Boolean(
            (remoteDefault && name === remoteDefault) || (!remoteDefault && localDefault && name === localDefault)
          )
        }
      })
  } catch {
    return []
  }
}

export {
  addWorktree,
  allocateWorktreePort,
  canonicalWorktreePath,
  ensureGitRepo,
  fnv1a32,
  inspectWorktreeBeforeClose,
  listBaseBranches,
  listBranches,
  listWorktrees,
  parseWorktrees,
  preferredWorktreePort,
  readWorktreeMeta,
  removeWorktree,
  resolveWorktreeGitDir,
  sanitizeBranch,
  seedWorktreeIncludes,
  switchBranch,
  writeWorktreeMeta
}
