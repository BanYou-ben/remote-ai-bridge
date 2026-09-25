import { beforeEach, describe, expect, it, vi } from 'vitest'

import client, { normalizeBackendError } from '../src/api/client.js'
import {
  connectRuntime,
  confirmHost,
  deleteProfile,
  discoverLocalProxy,
  disconnectRuntime,
  getHealth,
  getProfile,
  getRuntime,
  listProfiles,
  listRuntime,
  prepareHost,
  runDoctor,
  setupManagedProfile,
  updateProfile,
} from '../src/api/rab.js'

describe('RAB API client', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('uses the Vite proxy base URL', () => {
    expect(client.defaults.baseURL).toBe('/api')
    expect(client.defaults.timeout).toBe(10000)
  })

  it.each([
    [getHealth, '/health'],
    [listProfiles, '/profiles'],
    [listRuntime, '/runtime'],
  ])('calls the expected collection endpoint', async (request, path) => {
    const get = vi.spyOn(client, 'get').mockResolvedValue({ data: { ok: true } })
    await request()
    expect(get).toHaveBeenCalledWith(path)
  })

  it('encodes profile names for detail requests', async () => {
    const get = vi.spyOn(client, 'get').mockResolvedValue({ data: {} })
    await getProfile('team/profile')
    await getRuntime('team/profile')
    expect(get).toHaveBeenNthCalledWith(1, '/profiles/team%2Fprofile')
    expect(get).toHaveBeenNthCalledWith(2, '/runtime/team%2Fprofile')
  })

  it.each([
    [connectRuntime, '/runtime/team%2Fprofile/connect'],
    [disconnectRuntime, '/runtime/team%2Fprofile/disconnect'],
    [runDoctor, '/doctor/team%2Fprofile'],
  ])('posts to the expected action endpoint', async (request, path) => {
    const post = vi.spyOn(client, 'post').mockResolvedValue({ data: { ok: true } })
    await request('team/profile')
    expect(post).toHaveBeenCalledWith(path)
  })

  it.each([
    [prepareHost, '/setup/host/prepare'],
    [confirmHost, '/setup/host/confirm'],
    [discoverLocalProxy, '/setup/local-proxy/discover'],
    [setupManagedProfile, '/setup/managed'],
  ])('posts setup payloads to the expected endpoint', async (request, path) => {
    const post = vi.spyOn(client, 'post').mockResolvedValue({ data: { ok: true } })
    const payload = { marker: 'safe' }
    await request(payload)
    expect(post).toHaveBeenCalledWith(path, payload)
  })

  it('updates and deletes encoded profile names', async () => {
    const patch = vi.spyOn(client, 'patch').mockResolvedValue({ data: {} })
    const remove = vi.spyOn(client, 'delete').mockResolvedValue({ data: {} })
    await updateProfile('team/profile', { remote_port: 17891 })
    await deleteProfile('team/profile')
    expect(patch).toHaveBeenCalledWith('/profiles/team%2Fprofile', { remote_port: 17891 })
    expect(remove).toHaveBeenCalledWith('/profiles/team%2Fprofile')
  })

  it('normalizes FastAPI structured errors', () => {
    expect(
      normalizeBackendError({
        response: {
          data: {
            error: {
              code: 'PROFILE_NOT_FOUND',
              message: 'profile was not found',
              retryable: false,
              details: { name: 'missing' },
            },
          },
        },
      }),
    ).toEqual({
      code: 'PROFILE_NOT_FOUND',
      message: 'profile was not found',
      retryable: false,
      details: { name: 'missing' },
    })
  })

  it('normalizes network failures without exposing Axios internals', () => {
    expect(normalizeBackendError(new Error('connect ECONNREFUSED'))).toEqual({
      code: 'BACKEND_UNAVAILABLE',
      message: 'Backend unavailable.',
      retryable: true,
      details: {},
    })
  })
})
