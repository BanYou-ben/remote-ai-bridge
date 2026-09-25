import axios from 'axios'

export function normalizeBackendError(error) {
  const body = error?.response?.data?.error
  if (body && typeof body === 'object') {
    return {
      code: typeof body.code === 'string' ? body.code : 'BACKEND_ERROR',
      message: typeof body.message === 'string' ? body.message : 'Backend request failed.',
      retryable: body.retryable === true,
      details: body.details && typeof body.details === 'object' ? body.details : {},
    }
  }
  return {
    code: 'BACKEND_UNAVAILABLE',
    message: 'Backend unavailable.',
    retryable: true,
    details: {},
  }
}

const client = axios.create({
  baseURL: '/api',
  timeout: 10000,
})

client.interceptors.response.use(
  (response) => response,
  (error) => Promise.reject(normalizeBackendError(error)),
)

export default client
