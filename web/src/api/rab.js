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
