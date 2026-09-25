import { beforeEach, describe, expect, it, vi } from 'vitest'

import client, { normalizeBackendError } from '../src/api/client.js'
import { getHealth, getProfile, getRuntime, listProfiles, listRuntime } from '../src/api/rab.js'

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
