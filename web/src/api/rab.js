import client from './client.js'

export async function getHealth() {
  return (await client.get('/health')).data
}

export async function listProfiles() {
  return (await client.get('/profiles')).data
}

export async function getProfile(name) {
  return (await client.get(`/profiles/${encodeURIComponent(name)}`)).data
}

export async function listRuntime() {
  return (await client.get('/runtime')).data
}

export async function getRuntime(name) {
  return (await client.get(`/runtime/${encodeURIComponent(name)}`)).data
}

export async function connectRuntime(name) {
  return (await client.post(`/runtime/${encodeURIComponent(name)}/connect`)).data
}

export async function disconnectRuntime(name) {
  return (await client.post(`/runtime/${encodeURIComponent(name)}/disconnect`)).data
}

export async function runDoctor(name) {
  return (await client.post(`/doctor/${encodeURIComponent(name)}`)).data
}

export async function prepareHost(payload) {
  return (await client.post('/setup/host/prepare', payload)).data
}

export async function confirmHost(payload) {
  return (await client.post('/setup/host/confirm', payload)).data
}

export async function discoverLocalProxy(payload) {
  return (await client.post('/setup/local-proxy/discover', payload)).data
}

export async function setupManagedProfile(payload) {
  return (await client.post('/setup/managed', payload)).data
}

export async function updateProfile(name, changes) {
  return (await client.patch(`/profiles/${encodeURIComponent(name)}`, changes)).data
}

export async function deleteProfile(name) {
  return (await client.delete(`/profiles/${encodeURIComponent(name)}`)).data
}
