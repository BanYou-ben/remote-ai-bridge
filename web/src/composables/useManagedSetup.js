import { reactive, ref, watch } from 'vue'

import {
  confirmHost,
  discoverLocalProxy,
  prepareHost,
  setupManagedProfile,
} from '../api/rab.js'

const DEFAULT_ENDPOINT = 'https://api.openai.com/v1/models'

function isHealthyCandidate(candidate) {
  return candidate.tcp_reachable !== false
    && candidate.connect_reachable !== false
    && candidate.endpoint_reachable !== false
}

export function useManagedSetup() {
  const currentStep = ref(1)
  const form = reactive({
    name: '',
    host: '',
    username: '',
    port: 22,
    password: '',
    autoReconnect: true,
    startRemotePort: 17890,
    maxRemotePortAttempts: 20,
    endpointProbeUrl: DEFAULT_ENDPOINT,
  })
  const hostPreparation = ref(null)
  const confirmationRequired = ref(false)
  const confirmedFingerprint = ref('')
  const proxyDiscovery = ref(null)
  const selectedProxy = ref(null)
  const result = ref(null)
  const submitting = ref(false)
  const error = ref(null)

  function clearError() {
    error.value = null
  }

  function invalidateHostConfirmation() {
    hostPreparation.value = null
    confirmationRequired.value = false
    confirmedFingerprint.value = ''
    proxyDiscovery.value = null
    selectedProxy.value = null
    result.value = null
    if (currentStep.value > 1) currentStep.value = 1
  }

  watch(() => [form.host, form.port], invalidateHostConfirmation)

  async function prepare() {
    submitting.value = true
    clearError()
    hostPreparation.value = null
    confirmedFingerprint.value = ''
    try {
      hostPreparation.value = await prepareHost({
        host: form.host,
        username: form.username,
        port: Number(form.port),
      })
      confirmationRequired.value = false
      currentStep.value = 2
    } catch (caught) {
      if (caught.code === 'HOST_KEY_CONFIRMATION_REQUIRED' && caught.details?.fingerprint) {
        hostPreparation.value = {
          host: caught.details.host ?? form.host,
          port: caught.details.port ?? Number(form.port),
          key_type: caught.details.key_type,
          fingerprint: caught.details.fingerprint,
          status: caught.code,
        }
        confirmationRequired.value = true
        currentStep.value = 2
      } else {
        error.value = caught
      }
    } finally {
      submitting.value = false
    }
  }

  async function confirmFingerprint() {
    if (!hostPreparation.value?.fingerprint) return
    submitting.value = true
    clearError()
    try {
      const prepared = await confirmHost({
        host: form.host,
        port: Number(form.port),
        expected_fingerprint: hostPreparation.value.fingerprint,
        accepted: true,
      })
      hostPreparation.value = prepared
      confirmedFingerprint.value = prepared.fingerprint
      confirmationRequired.value = false
      currentStep.value = 4
    } catch (caught) {
      error.value = caught
    } finally {
      submitting.value = false
    }
  }

  function acceptKnownFingerprint() {
    if (!hostPreparation.value?.fingerprint || confirmationRequired.value) return
    confirmedFingerprint.value = hostPreparation.value.fingerprint
    currentStep.value = 4
  }

  function editServer() {
    clearError()
    currentStep.value = 1
  }

  async function discoverProxy() {
    if (!confirmedFingerprint.value) return
    submitting.value = true
    clearError()
    proxyDiscovery.value = null
    selectedProxy.value = null
    try {
      const discovery = await discoverLocalProxy({
        endpoint_probe_url: form.endpointProbeUrl,
      })
      proxyDiscovery.value = discovery
      selectedProxy.value = discovery.selected?.port ?? null
      currentStep.value = 4
    } catch (caught) {
      const candidates = caught.details?.candidates
      if (Array.isArray(candidates)) {
        proxyDiscovery.value = { selected: null, candidates }
      }
      error.value = caught
    } finally {
      submitting.value = false
    }
  }

  function selectProxy(candidate) {
    if (isHealthyCandidate(candidate)) selectedProxy.value = candidate.port
  }

  function continueToSetup() {
    if (confirmedFingerprint.value && selectedProxy.value) {
      clearError()
      currentStep.value = 5
    }
  }

  async function submit() {
    if (!confirmedFingerprint.value || !selectedProxy.value) return
    submitting.value = true
    clearError()
    try {
      result.value = await setupManagedProfile({
        name: form.name,
        host: form.host,
        username: form.username,
        password: form.password,
        port: Number(form.port),
        confirmed_fingerprint: confirmedFingerprint.value,
        selected_local_proxy_port: selectedProxy.value,
        auto_reconnect: form.autoReconnect,
        start_remote_port: Number(form.startRemotePort),
        max_remote_port_attempts: Number(form.maxRemotePortAttempts),
        endpoint_probe_url: form.endpointProbeUrl,
      })
      currentStep.value = 6
    } catch (caught) {
      error.value = caught
    } finally {
      form.password = ''
      submitting.value = false
    }
  }

  return {
    currentStep,
    form,
    hostPreparation,
    confirmationRequired,
    confirmedFingerprint,
    proxyDiscovery,
    selectedProxy,
    result,
    submitting,
    error,
    prepare,
    confirmFingerprint,
    acceptKnownFingerprint,
    editServer,
    discoverProxy,
    selectProxy,
    continueToSetup,
    submit,
    isHealthyCandidate,
  }
}
