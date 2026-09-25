const SUPERVISOR_STATE_LABELS = Object.freeze({
  READY: '已就绪',
  STARTING: '正在启动',
  CONNECTING: '正在连接',
  DEGRADED: '状态异常',
  FAILED: '失败',
  STOPPING: '正在停止',
  STOPPED: '已停止',
  UNSUPERVISED: '未运行',
})

const PROFILE_TYPE_LABELS = Object.freeze({
  legacy: '旧版',
  managed: '托管配置',
})

const RUNTIME_MESSAGE_LABELS = Object.freeze({
  'runtime evidence exists but the process identity is absent or mismatched':
    '存在运行记录，但对应进程不存在或身份不匹配',
  'profile is not supervised and has no runtime evidence': '当前连接未运行，且没有运行记录',
  'bridge is healthy': '连接运行正常',
  'supervision stopped': '连接已停止',
})

const CHECK_STATUS_LABELS = Object.freeze({
  PASS: '通过',
  FAIL: '失败',
  SKIP: '跳过',
  UNKNOWN: '未知',
})

export function formatSupervisorState(state) {
  return SUPERVISOR_STATE_LABELS[state] ?? state
}

export function formatBoolean(value, variant = 'yes-no') {
  if (value == null) return '未知'
  if (variant === 'enabled') return value ? '已开启' : '已关闭'
  return value ? '是' : '否'
}

export function formatSupervision(value) {
  if (value == null) return '未知'
  return value ? '已受监督' : '未受监督'
}

export function formatProfileType(profileType) {
  return PROFILE_TYPE_LABELS[profileType] ?? profileType
}

export function formatRuntimeMessage(message) {
  if (!message) return '—'
  return RUNTIME_MESSAGE_LABELS[message] ?? message
}

export function formatCheckStatus(status) {
  return CHECK_STATUS_LABELS[status] ?? status
}
