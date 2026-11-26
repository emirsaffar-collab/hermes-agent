import { beforeEach, describe, expect, it, vi } from 'vitest'

const { mockReachablePreviewUrl, mockOpenPreview, mockRequestGatewayForAgent } = vi.hoisted(() => ({
  mockOpenPreview: vi.fn(),
  mockReachablePreviewUrl: vi.fn(),
  mockRequestGatewayForAgent: vi.fn()
}))

vi.mock('@/lib/preview-reach', () => ({
  reachablePreviewUrl: (...args: unknown[]) => mockReachablePreviewUrl(...args)
}))

vi.mock('@/store/preview', () => ({
  openPreview: (...args: unknown[]) => mockOpenPreview(...args)
}))

vi.mock('@/store/gateway', () => ({
  $gateway: { get: () => null, subscribe: () => () => {} },
  activeGateway: () => null,
  ensureActiveGatewayOpen: vi.fn(),
  requestGatewayForAgent: (...args: unknown[]) => mockRequestGatewayForAgent(...args)
}))

vi.mock('@/store/projects', () => ({
  projectProfile: () => 'default'
}))

import { listRunningServicesForWorktree, openServicePreview } from './project-services'

describe('project-services store', () => {
  beforeEach(() => {
    mockReachablePreviewUrl.mockReset()
    mockOpenPreview.mockReset()
    mockRequestGatewayForAgent.mockReset()
  })

  it('routes preview opens through reachablePreviewUrl before openPreview', async () => {
    mockReachablePreviewUrl.mockResolvedValue('http://127.0.0.1:51234')

    const url = await openServicePreview('Web', 24123)

    expect(mockReachablePreviewUrl).toHaveBeenCalledWith('http://127.0.0.1:24123')
    expect(mockOpenPreview).toHaveBeenCalledWith({
      kind: 'url',
      label: 'Web (:24123)',
      source: 'http://127.0.0.1:51234',
      url: 'http://127.0.0.1:51234'
    })
    expect(url).toBe('http://127.0.0.1:51234')
  })

  it('returns running services for a worktree via owner route and fails soft on missing RPC', async () => {
    mockRequestGatewayForAgent.mockResolvedValue({
      running_for_worktree: [{ id: 'svc1', name: 'Web', pid: 42, port: 24100, project_key: 'p1', run_id: 'r1' }]
    })

    const running = await listRunningServicesForWorktree('/repo/wt1', {
      connectionId: 'conn-mini',
      profile: 'default'
    })

    expect(running).toHaveLength(1)
    expect(running[0]?.port).toBe(24100)

    mockRequestGatewayForAgent.mockRejectedValue(new Error('Method not found: projects.services.list'))
    const fallback = await listRunningServicesForWorktree('/repo/wt1', {
      connectionId: 'conn-mini',
      profile: 'default'
    })
    expect(fallback).toEqual([])
  })
})
