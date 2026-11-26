import { useCallback, useEffect, useState } from 'react'

import { Codicon } from '@/components/ui/codicon'
import { isMissingRpcMethod } from '@/lib/gateway-rpc'
import { notifyError } from '@/store/notifications'
import type { AgentProfileRoute } from '@/store/profile'
import {
  deleteProjectService,
  listProjectServices,
  openServicePreview,
  type ProjectService,
  type ProjectServiceLogs,
  type ProjectServicePortMode,
  readProjectServiceLogs,
  restartProjectService,
  saveProjectService,
  startProjectService,
  stopProjectService
} from '@/store/project-services'

function statusDotClass(status: ProjectService['status']): string {
  if (status === 'running') {
    return 'bg-emerald-500'
  }

  if (status === 'exited') {
    return 'bg-amber-500'
  }

  return 'bg-muted-foreground/40'
}

export function ProjectServicesSection({
  projectKey,
  worktreePath,
  owner
}: {
  projectKey: string
  worktreePath: null | string
  owner?: AgentProfileRoute
}) {
  const [services, setServices] = useState<ProjectService[]>([])
  const [unsupported, setUnsupported] = useState(false)
  const [open, setOpen] = useState(true)
  const [adding, setAdding] = useState(false)
  const [busyId, setBusyId] = useState<null | string>(null)

  const [name, setName] = useState('')
  const [command, setCommand] = useState('')
  const [cwdRel, setCwdRel] = useState('')
  const [portMode, setPortMode] = useState<ProjectServicePortMode>('auto')
  const [fixedPort, setFixedPort] = useState('')

  const [logServiceId, setLogServiceId] = useState<null | string>(null)
  const [logData, setLogData] = useState<null | ProjectServiceLogs>(null)

  const refresh = useCallback(async () => {
    if (!projectKey) {
      return
    }

    try {
      const res = await listProjectServices(projectKey, worktreePath, owner)

      setUnsupported(false)
      setServices(res.services)
    } catch (err) {
      if (isMissingRpcMethod(err)) {
        setUnsupported(true)
      }
    }
  }, [owner, projectKey, worktreePath])

  useEffect(() => {
    void refresh()
    const timer = window.setInterval(() => {
      void refresh()
    }, 5000)

    return () => window.clearInterval(timer)
  }, [refresh])

  useEffect(() => {
    if (!logServiceId) {
      setLogData(null)

      return
    }

    let cancelled = false

    const pollLogs = async () => {
      try {
        const res = await readProjectServiceLogs(logServiceId, worktreePath, 16_384, owner)

        if (!cancelled) {
          setLogData(res)
        }
      } catch {
        // Keep last log snapshot on transient poll errors.
      }
    }

    void pollLogs()
    const timer = window.setInterval(() => {
      void pollLogs()
    }, 2000)

    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [logServiceId, owner, worktreePath])

  if (unsupported || !worktreePath) {
    return null
  }

  const handleAdd = async (event: React.FormEvent) => {
    event.preventDefault()

    const trimmedName = name.trim()
    const trimmedCmd = command.trim()

    if (!trimmedName || !trimmedCmd) {
      return
    }

    setBusyId('__add__')

    try {
      const parsedPort = portMode === 'fixed' ? Number.parseInt(fixedPort.trim(), 10) : null

      await saveProjectService(
        {
          command: trimmedCmd,
          cwd_rel: cwdRel.trim(),
          fixed_port: Number.isFinite(parsedPort) ? parsedPort : null,
          name: trimmedName,
          port_mode: portMode,
          project_key: projectKey,
          worktree_path: worktreePath
        },
        owner
      )
      setName('')
      setCommand('')
      setCwdRel('')
      setPortMode('auto')
      setFixedPort('')
      setAdding(false)
      await refresh()
    } catch (err) {
      notifyError(err, 'Could not save service')
    } finally {
      setBusyId(null)
    }
  }

  const runAction = async (id: string, action: () => Promise<unknown>, fallbackError: string) => {
    setBusyId(id)

    try {
      await action()
      await refresh()
    } catch (err) {
      notifyError(err, fallbackError)
    } finally {
      setBusyId(null)
    }
  }

  const runningCount = services.filter(svc => svc.status === 'running').length

  return (
    <div className="mt-2 border-t border-border/40 pt-2">
      <div className="flex items-center justify-between px-2 py-1 text-[11px] font-medium text-muted-foreground">
        <button
          className="flex items-center gap-1.5 hover:text-foreground"
          onClick={() => setOpen(v => !v)}
          type="button"
        >
          <Codicon name={open ? 'chevron-down' : 'chevron-right'} size={12} />
          <span>Services</span>
          {runningCount > 0 && (
            <span className="rounded bg-emerald-500/15 px-1 text-[10px] text-emerald-600 dark:text-emerald-400">
              {runningCount}
            </span>
          )}
        </button>
        <button
          className="rounded p-0.5 hover:bg-muted hover:text-foreground"
          onClick={() => {
            setOpen(true)
            setAdding(v => !v)
          }}
          title="Add service"
          type="button"
        >
          <Codicon name={adding ? 'close' : 'add'} size={12} />
        </button>
      </div>

      {open && (
        <div className="space-y-1 px-1.5 pb-1">
          {adding && (
            <form className="space-y-1.5 rounded-md border border-border/60 bg-muted/30 p-2 text-xs" onSubmit={handleAdd}>
              <input
                className="w-full rounded border border-border/60 bg-background px-2 py-1 text-xs"
                onChange={e => setName(e.target.value)}
                placeholder="Service name (e.g. Web)"
                required
                value={name}
              />
              <input
                className="w-full rounded border border-border/60 bg-background px-2 py-1 font-mono text-[11px]"
                onChange={e => setCommand(e.target.value)}
                placeholder="Command (e.g. npm run dev -- --port $PORT)"
                required
                value={command}
              />
              <div className="flex gap-1.5">
                <input
                  className="min-w-0 flex-1 rounded border border-border/60 bg-background px-2 py-1 text-[11px]"
                  onChange={e => setCwdRel(e.target.value)}
                  placeholder="Subdir (optional)"
                  value={cwdRel}
                />
                <select
                  className="rounded border border-border/60 bg-background px-1.5 py-1 text-[11px]"
                  onChange={e => setPortMode(e.target.value as ProjectServicePortMode)}
                  value={portMode}
                >
                  <option value="auto">Auto port</option>
                  <option value="fixed">Fixed</option>
                  <option value="none">No port</option>
                </select>
                {portMode === 'fixed' && (
                  <input
                    className="w-16 rounded border border-border/60 bg-background px-1.5 py-1 font-mono text-[11px]"
                    max={65535}
                    min={1}
                    onChange={e => setFixedPort(e.target.value)}
                    placeholder="3000"
                    required
                    type="number"
                    value={fixedPort}
                  />
                )}
              </div>
              <div className="flex justify-end gap-1.5">
                <button
                  className="rounded px-2 py-0.5 text-[11px] text-muted-foreground hover:text-foreground"
                  onClick={() => setAdding(false)}
                  type="button"
                >
                  Cancel
                </button>
                <button
                  className="rounded bg-primary px-2 py-0.5 text-[11px] text-primary-foreground disabled:opacity-50"
                  disabled={busyId === '__add__'}
                  type="submit"
                >
                  Save
                </button>
              </div>
            </form>
          )}

          {services.length === 0 && !adding && (
            <div className="px-1.5 py-0.5 text-[11px] text-muted-foreground/70">No services configured</div>
          )}

          {services.map(svc => {
            const isRunning = svc.status === 'running'
            const isBusy = busyId === svc.id
            const showingLogs = logServiceId === svc.id

            return (
              <div className="rounded-md border border-transparent hover:border-border/50 hover:bg-muted/30" key={svc.id}>
                <div className="flex items-center gap-1.5 px-1.5 py-1 text-xs">
                  <span className={`h-2 w-2 shrink-0 rounded-full ${statusDotClass(svc.status)}`} title={svc.status} />
                  <span className="min-w-0 flex-1 truncate font-medium" title={`${svc.name}: ${svc.command}`}>
                    {svc.name}
                  </span>
                  {isRunning && svc.port ? (
                    <button
                      className="shrink-0 rounded bg-primary/10 px-1.5 py-0.5 font-mono text-[10px] text-primary hover:bg-primary/20"
                      onClick={() => void openServicePreview(svc.name, svc.port!)}
                      title={`Open http://127.0.0.1:${svc.port} in Preview`}
                      type="button"
                    >
                      :{svc.port}
                    </button>
                  ) : null}
                  <div className="flex shrink-0 items-center gap-0.5">
                    {isRunning ? (
                      <>
                        <button
                          className="rounded p-0.5 text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-40"
                          disabled={isBusy}
                          onClick={() =>
                            void runAction(
                              svc.id,
                              () => restartProjectService(svc.id, worktreePath, owner),
                              'Could not restart service'
                            )
                          }
                          title="Restart service"
                          type="button"
                        >
                          <Codicon name="debug-restart" size={12} />
                        </button>
                        <button
                          className="rounded p-0.5 text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-40"
                          disabled={isBusy}
                          onClick={() =>
                            void runAction(
                              svc.id,
                              () => stopProjectService(svc.id, worktreePath, owner),
                              'Could not stop service'
                            )
                          }
                          title="Stop service"
                          type="button"
                        >
                          <Codicon name="debug-stop" size={12} />
                        </button>
                      </>
                    ) : (
                      <button
                        className="rounded p-0.5 text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-40"
                        disabled={isBusy}
                        onClick={() =>
                          void runAction(
                            svc.id,
                            () => startProjectService(svc.id, worktreePath, owner),
                            'Could not start service'
                          )
                        }
                        title="Start service"
                        type="button"
                      >
                        <Codicon name="play" size={12} />
                      </button>
                    )}
                    <button
                      className={`rounded p-0.5 hover:bg-muted ${showingLogs ? 'text-primary' : 'text-muted-foreground hover:text-foreground'}`}
                      onClick={() => setLogServiceId(prev => (prev === svc.id ? null : svc.id))}
                      title="Toggle service logs"
                      type="button"
                    >
                      <Codicon name="output" size={12} />
                    </button>
                    <button
                      className="rounded p-0.5 text-muted-foreground hover:bg-muted hover:text-destructive disabled:opacity-40"
                      disabled={isBusy}
                      onClick={() =>
                        void runAction(
                          svc.id,
                          async () => {
                            if (logServiceId === svc.id) {
                              setLogServiceId(null)
                            }

                            await deleteProjectService(svc.id, owner)
                          },
                          'Could not delete service'
                        )
                      }
                      title="Delete service"
                      type="button"
                    >
                      <Codicon name="trash" size={12} />
                    </button>
                  </div>
                </div>

                {showingLogs && (
                  <div className="border-t border-border/40 bg-background/80 p-1.5">
                    <div className="mb-1 flex items-center justify-between text-[10px] text-muted-foreground">
                      <span>Logs ({logData?.status ?? svc.status})</span>
                      {logData?.truncated && <span>tail capped</span>}
                    </div>
                    <pre className="max-h-36 overflow-auto whitespace-pre-wrap break-all font-mono text-[10px] leading-snug text-foreground/90">
                      {logData?.content?.trim() ? logData.content : '(no output yet)'}
                    </pre>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
