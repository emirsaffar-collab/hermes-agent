import { isMissingRpcMethod } from '@/lib/gateway-rpc'
import { reachablePreviewUrl } from '@/lib/preview-reach'
import { activeGateway, ensureActiveGatewayOpen, requestGatewayForAgent } from '@/store/gateway'
import { openPreview } from '@/store/preview'
import type { AgentProfileRoute } from '@/store/profile'
import { projectProfile } from '@/store/projects'

export type ProjectServicePortMode = 'auto' | 'fixed' | 'none'
export type ProjectServiceStatus = 'exited' | 'running' | 'stopped'

export interface ProjectServiceRun {
  run_id: string
  service_id: string
  worktree_path: string
  pid: number
  pgid: null | number
  port: null | number
  log_path: string
  status: ProjectServiceStatus
  started_at: number
  stopped_at: null | number
}

export interface ProjectService {
  id: string
  project_key: string
  name: string
  command: string
  cwd_rel: string
  port_mode: ProjectServicePortMode
  fixed_port: null | number
  env: Record<string, string>
  created_at: number
  updated_at: number
  status: ProjectServiceStatus
  port: null | number
  pid: null | number
  run: null | ProjectServiceRun
}

export interface ProjectRunningServiceSummary {
  id: string
  name: string
  project_key: string
  run_id: string
  pid: number
  port: null | number
}

export interface ProjectServiceLogs {
  service_id: string
  run_id: null | string
  status: ProjectServiceStatus
  content: string
  truncated: boolean
  byte_size: number
}

export interface SaveProjectServiceInput {
  project_key: string
  id?: null | string
  name: string
  command: string
  cwd_rel?: string
  port_mode?: ProjectServicePortMode
  fixed_port?: null | number
  env?: Record<string, string>
  worktree_path?: null | string
}

async function serviceRpc<T>(
  method: string,
  params: Record<string, unknown>,
  owner?: AgentProfileRoute
): Promise<T> {
  if (owner) {
    return requestGatewayForAgent<T>(owner.connectionId, owner.profile, method, {
      ...params,
      profile: owner.profile
    })
  }

  let gateway = activeGateway()

  if (!gateway || gateway.connectionState !== 'open') {
    gateway = await ensureActiveGatewayOpen()
  }

  if (!gateway) {
    throw new Error('Hermes gateway is not connected')
  }

  const profile = projectProfile()

  return gateway.request<T>(method, profile ? { ...params, profile } : params)
}

export async function listProjectServices(
  projectKey: string,
  worktreePath?: null | string,
  owner?: AgentProfileRoute
): Promise<{ running_for_worktree: ProjectRunningServiceSummary[]; services: ProjectService[] }> {
  return serviceRpc<{ running_for_worktree: ProjectRunningServiceSummary[]; services: ProjectService[] }>(
    'projects.services.list',
    { project_key: projectKey, worktree_path: worktreePath || null },
    owner
  )
}

export async function listRunningServicesForWorktree(
  worktreePath: string,
  owner?: AgentProfileRoute
): Promise<ProjectRunningServiceSummary[]> {
  try {
    const res = await serviceRpc<{ running_for_worktree?: ProjectRunningServiceSummary[] }>(
      'projects.services.list',
      { project_key: null, worktree_path: worktreePath },
      owner
    )

    return Array.isArray(res?.running_for_worktree) ? res.running_for_worktree : []
  } catch (err) {
    if (isMissingRpcMethod(err)) {
      return []
    }

    return []
  }
}

export async function saveProjectService(
  input: SaveProjectServiceInput,
  owner?: AgentProfileRoute
): Promise<ProjectService> {
  const res = await serviceRpc<{ service: ProjectService }>(
    'projects.services.save',
    {
      command: input.command,
      cwd_rel: input.cwd_rel ?? '',
      env: input.env ?? {},
      fixed_port: input.fixed_port ?? null,
      id: input.id ?? null,
      name: input.name,
      port_mode: input.port_mode ?? 'auto',
      project_key: input.project_key,
      worktree_path: input.worktree_path ?? null
    },
    owner
  )

  return res.service
}

export async function deleteProjectService(
  serviceId: string,
  owner?: AgentProfileRoute
): Promise<{ deleted: string; project_key: string; stopped: number }> {
  return serviceRpc<{ deleted: string; project_key: string; stopped: number }>(
    'projects.services.delete',
    { service_id: serviceId },
    owner
  )
}

export async function startProjectService(
  serviceId: string,
  worktreePath: string,
  owner?: AgentProfileRoute
): Promise<ProjectService> {
  const res = await serviceRpc<{ service: ProjectService }>(
    'projects.services.start',
    { service_id: serviceId, worktree_path: worktreePath },
    owner
  )

  return res.service
}

export async function stopProjectService(
  serviceId: string,
  worktreePath?: null | string,
  owner?: AgentProfileRoute
): Promise<{ service: null | ProjectService; stopped: number }> {
  return serviceRpc<{ service: null | ProjectService; stopped: number }>(
    'projects.services.stop',
    { service_id: serviceId, worktree_path: worktreePath ?? null },
    owner
  )
}

export async function stopServicesForWorktree(
  worktreePath: string,
  owner?: AgentProfileRoute
): Promise<number> {
  try {
    const res = await serviceRpc<{ stopped?: number }>(
      'projects.services.stop',
      { service_id: null, worktree_path: worktreePath },
      owner
    )

    return typeof res?.stopped === 'number' ? res.stopped : 0
  } catch (err) {
    if (isMissingRpcMethod(err)) {
      return 0
    }

    throw err
  }
}

export async function restartProjectService(
  serviceId: string,
  worktreePath: string,
  owner?: AgentProfileRoute
): Promise<ProjectService> {
  const res = await serviceRpc<{ service: ProjectService }>(
    'projects.services.restart',
    { service_id: serviceId, worktree_path: worktreePath },
    owner
  )

  return res.service
}

export async function readProjectServiceLogs(
  serviceId: string,
  worktreePath?: null | string,
  tailBytes = 16_384,
  owner?: AgentProfileRoute
): Promise<ProjectServiceLogs> {
  return serviceRpc<ProjectServiceLogs>(
    'projects.services.logs',
    { service_id: serviceId, tail_bytes: tailBytes, worktree_path: worktreePath ?? null },
    owner
  )
}

/** Open a running service's local port in the Preview rail, auto-tunneling over SSH in remote mode. */
export async function openServicePreview(name: string, port: number): Promise<string> {
  const rawUrl = `http://127.0.0.1:${port}`
  const reachable = await reachablePreviewUrl(rawUrl)

  openPreview({
    kind: 'url',
    label: `${name} (:${port})`,
    source: reachable,
    url: reachable
  })

  return reachable
}
