<script setup>
import { computed } from 'vue'

import { useManagedSetup } from '../composables/useManagedSetup.js'

const setup = useManagedSetup()
const visibleSteps = [1, 2, 4, 5, 6]

const stepMeta = [
  { internal: 1, label: '服务器', title: '服务器信息', note: '填写主机、用户名与 SSH 端口。' },
  { internal: 2, label: '验证', title: '验证主机身份', note: '核对 SSH 主机密钥指纹。' },
  { internal: 4, label: '代理', title: '检测本地代理', note: '选择后端确认可用的本地代理。' },
  { internal: 5, label: '凭据', title: '创建托管连接', note: '仅在本次请求中使用 SSH 密码。' },
  { internal: 6, label: '完成', title: '连接配置已创建', note: '检查结果并进入配置详情。' },
]

const currentMeta = computed(
  () => stepMeta.find((item) => item.internal === setup.currentStep.value) ?? stepMeta[0],
)

const currentVisibleStep = computed(
  () => stepMeta.findIndex((item) => item.internal === setup.currentStep.value) + 1,
)
</script>

<template>
  <div class="setup-page">
    <header class="page-header setup-page-header">
      <div>
        <p class="eyebrow">NEW CONNECTION</p>
        <h1>添加托管连接</h1>
        <p>验证服务器身份、检测本地代理并创建由 Remote AI Bridge 管理的连接配置。</p>
      </div>
    </header>

    <section class="setup-progress-shell" aria-label="设置进度">
      <div class="setup-progress-copy">
        <p class="panel-kicker">SETUP PROGRESS</p>
        <strong>第 {{ currentVisibleStep }} 步，共 5 步</strong>
        <span>{{ currentMeta.title }}</span>
      </div>

      <ol class="wizard-progress setup-progress-track">
        <li
          v-for="(step, index) in visibleSteps"
          :key="step"
          data-test="wizard-step"
          :data-label="stepMeta[index].label"
          :aria-label="`${index + 1}. ${stepMeta[index].label}`"
          :aria-current="setup.currentStep.value === step ? 'step' : undefined"
          :class="{ active: setup.currentStep.value === step, complete: setup.currentStep.value > step }"
        >
          {{ index + 1 }}
        </li>
      </ol>
    </section>

    <div class="setup-workspace">
      <section class="setup-main-card">
        <div v-if="setup.error.value" class="structured-error setup-structured-error" role="alert">
          <strong>{{ setup.error.value.code }}</strong>
          <span>{{ setup.error.value.message }}</span>
          <pre v-if="Object.keys(setup.error.value.details || {}).length">{{ JSON.stringify(setup.error.value.details, null, 2) }}</pre>
        </div>

        <form v-if="setup.currentStep.value === 1" class="form-grid setup-form" @submit.prevent="setup.prepare">
          <div class="setup-section-heading wide">
            <div class="setup-section-number">01</div>
            <div>
              <p class="panel-kicker">SERVER</p>
              <h2>服务器信息</h2>
              <p>先建立 SSH 握手并读取主机指纹，不会在此步骤发送密码。</p>
            </div>
          </div>

          <label>
            <span>配置名称</span>
            <input v-model.trim="setup.form.name" data-test="name" required autocomplete="off" placeholder="例如：lab-server">
          </label>
          <label>
            <span>主机地址</span>
            <input v-model.trim="setup.form.host" data-test="host" required autocomplete="off" placeholder="例如：10.102.128.6">
          </label>
          <label>
            <span>用户名</span>
            <input v-model.trim="setup.form.username" data-test="username" required autocomplete="username" placeholder="SSH 用户名">
          </label>
          <label>
            <span>SSH 端口</span>
            <input v-model.number="setup.form.port" data-test="ssh-port" required type="number" min="1" max="65535">
          </label>

          <div class="setup-inline-note wide">
            <span class="setup-note-icon" aria-hidden="true"></span>
            <div>
              <strong>第一步只读取服务器身份</strong>
              <p>不会发送 SSH 密码，也不会创建或覆盖任何远端凭据。</p>
            </div>
          </div>

          <div class="form-actions wide setup-form-actions">
            <button class="primary-button" data-test="prepare" type="submit" :disabled="setup.submitting.value">
              {{ setup.submitting.value ? '正在验证……' : '验证主机身份 →' }}
            </button>
          </div>
        </form>

        <div v-else-if="setup.currentStep.value === 2" class="wizard-step setup-step">
          <div class="setup-section-heading">
            <div class="setup-section-number">02</div>
            <div>
              <p class="panel-kicker">VERIFY HOST</p>
              <h2>主机身份验证</h2>
              <p>请通过可信渠道核对以下 SSH 主机指纹。</p>
            </div>
          </div>

          <div class="fingerprint-card">
            <div class="fingerprint-card-top">
              <span class="security-lock-mark" aria-hidden="true"></span>
              <div>
                <strong>SSH Host Fingerprint</strong>
                <span>确认服务器身份后才能继续认证</span>
              </div>
            </div>
            <dl class="detail-grid host-fingerprint">
              <div><dt>主机</dt><dd>{{ setup.hostPreparation.value.host }}</dd></div>
              <div><dt>端口</dt><dd>{{ setup.hostPreparation.value.port }}</dd></div>
              <div><dt>密钥类型</dt><dd>{{ setup.hostPreparation.value.key_type }}</dd></div>
              <div class="wide"><dt>主机密钥指纹</dt><dd class="monospace fingerprint-value">{{ setup.hostPreparation.value.fingerprint }}</dd></div>
            </dl>
          </div>

          <p v-if="setup.confirmationRequired.value" class="security-notice">
            该主机尚未受信任。只有在你通过可信渠道核对并确认指纹后，才允许继续密码认证。
          </p>

          <div class="form-actions setup-form-actions">
            <button
              v-if="setup.confirmationRequired.value"
              class="primary-button"
              data-test="confirm-fingerprint"
              type="button"
              :disabled="setup.submitting.value"
              @click="setup.confirmFingerprint"
            >
              确认此主机指纹
            </button>
            <button
              v-else
              class="primary-button"
              data-test="accept-known-host"
              type="button"
              @click="setup.acceptKnownFingerprint"
            >
              继续检测本地代理 →
            </button>
            <button class="secondary-button" data-test="edit-server" type="button" @click="setup.editServer">修改服务器信息</button>
          </div>
        </div>

        <div v-else-if="setup.currentStep.value === 4" class="wizard-step setup-step">
          <div class="setup-section-heading">
            <div class="setup-section-number">03</div>
            <div>
              <p class="panel-kicker">DISCOVER PROXY</p>
              <h2>本地代理检测</h2>
              <p>候选端口完全由后端返回；失败候选不能被选择。</p>
            </div>
          </div>

          <div v-if="!setup.proxyDiscovery.value" class="proxy-discovery-empty">
            <div class="proxy-route-graphic" aria-hidden="true">
              <span></span><i></i><span></span>
            </div>
            <div>
              <strong>检测本地代理</strong>
              <p>验证 TCP、HTTP CONNECT 与 Endpoint 可达性后，选择可用于 Reverse Tunnel 的代理端口。</p>
            </div>
            <button class="primary-button" data-test="discover-proxy" type="button" :disabled="setup.submitting.value" @click="setup.discoverProxy">
              {{ setup.submitting.value ? '正在检测……' : '开始检测' }}
            </button>
          </div>

          <div v-else class="setup-proxy-table">
            <div class="table-wrap">
              <table>
                <thead><tr><th>选择</th><th>端口</th><th>TCP</th><th>HTTP Proxy</th><th>Endpoint</th></tr></thead>
                <tbody>
                  <tr v-for="candidate in setup.proxyDiscovery.value.candidates" :key="candidate.port">
                    <td><input type="radio" name="proxy" :data-test="`proxy-${candidate.port}`" :value="candidate.port" :checked="setup.selectedProxy.value === candidate.port" :disabled="!setup.isHealthyCandidate(candidate)" @change="setup.selectProxy(candidate)"></td>
                    <td class="monospace">{{ candidate.host }}:{{ candidate.port }}</td>
                    <td>{{ candidate.tcp_reachable === false ? '失败' : '通过' }}</td>
                    <td>{{ candidate.connect_reachable === false ? '失败' : '通过' }}</td>
                    <td>{{ candidate.endpoint_reachable === false ? '失败' : '通过' }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>

          <p v-if="setup.selectedProxy.value" class="selection-summary">已选择本地代理：127.0.0.1:{{ setup.selectedProxy.value }}</p>

          <div class="form-actions setup-form-actions">
            <button v-if="setup.proxyDiscovery.value" class="secondary-button" type="button" :disabled="setup.submitting.value" @click="setup.discoverProxy">重新检测</button>
            <button class="primary-button" data-test="continue-setup" type="button" :disabled="!setup.selectedProxy.value" @click="setup.continueToSetup">继续 →</button>
            <button class="secondary-button" data-test="edit-server" type="button" @click="setup.editServer">修改服务器信息</button>
          </div>
        </div>

        <form v-else-if="setup.currentStep.value === 5" class="form-grid setup-form" @submit.prevent="setup.submit">
          <div class="setup-section-heading wide">
            <div class="setup-section-number">04</div>
            <div>
              <p class="panel-kicker">CREDENTIALS</p>
              <h2>创建托管连接</h2>
              <p>密码仅用于本次认证，请求完成后会清空且不会持久化。</p>
            </div>
          </div>

          <label class="wide setup-password-field">
            <span>SSH 密码</span>
            <input v-model="setup.form.password" data-test="password" required type="password" autocomplete="current-password" placeholder="仅用于本次认证">
            <small>密码不会写入 localStorage、sessionStorage 或日志。</small>
          </label>
          <label>
            <span>起始远端端口</span>
            <input v-model.number="setup.form.startRemotePort" type="number" min="1" max="65535">
          </label>
          <label>
            <span>最大端口尝试数</span>
            <input v-model.number="setup.form.maxRemotePortAttempts" type="number" min="1">
          </label>
          <label class="wide">
            <span>Endpoint Probe URL</span>
            <input v-model.trim="setup.form.endpointProbeUrl" type="url" required>
          </label>
          <label class="checkbox-field wide setup-checkbox-field">
            <input v-model="setup.form.autoReconnect" type="checkbox">
            <span>自动重连</span>
          </label>

          <div class="setup-inline-note wide is-secure">
            <span class="setup-note-icon" aria-hidden="true"></span>
            <div>
              <strong>受控的凭据使用边界</strong>
              <p>托管 SSH 凭据由后端安全流程创建；网页不会展示私钥、公钥内容或保存密码。</p>
            </div>
          </div>

          <div class="form-actions wide setup-form-actions">
            <button class="primary-button" data-test="submit-setup" type="submit" :disabled="setup.submitting.value">
              {{ setup.submitting.value ? '正在创建……' : '创建连接配置 →' }}
            </button>
          </div>
        </form>

        <div v-else class="wizard-step setup-step setup-complete">
          <div class="setup-complete-mark" aria-hidden="true">✓</div>
          <div class="setup-complete-heading">
            <p class="panel-kicker">COMPLETE</p>
            <h2>连接配置已创建</h2>
            <p>托管 SSH 凭据已安全配置，可以开始连接与诊断。</p>
          </div>

          <dl class="detail-grid setup-result-grid">
            <div><dt>配置名称</dt><dd>{{ setup.result.value.name }}</dd></div>
            <div><dt>主机</dt><dd>{{ setup.result.value.host }}</dd></div>
            <div><dt>SSH 端口</dt><dd>{{ setup.result.value.ssh_port }}</dd></div>
            <div><dt>本地代理</dt><dd>{{ setup.result.value.local_proxy_host }}:{{ setup.result.value.local_proxy_port }}</dd></div>
            <div><dt>远端端口</dt><dd>{{ setup.result.value.remote_bind_host }}:{{ setup.result.value.remote_port }}</dd></div>
            <div><dt>自动重连</dt><dd>{{ setup.result.value.auto_reconnect ? '已开启' : '已关闭' }}</dd></div>
          </dl>

          <div class="form-actions setup-form-actions">
            <RouterLink class="primary-button button-link" :to="`/profiles/${encodeURIComponent(setup.result.value.name)}`">查看配置</RouterLink>
            <RouterLink class="secondary-button button-link" to="/">返回总览</RouterLink>
          </div>
        </div>
      </section>

      <aside class="setup-guide-panel">
        <div class="setup-guide-current">
          <p class="panel-kicker">SETUP GUIDE</p>
          <span class="setup-guide-step">0{{ currentVisibleStep }}</span>
          <h2>{{ currentMeta.title }}</h2>
          <p>{{ currentMeta.note }}</p>
        </div>

        <ol class="setup-guide-list">
          <li
            v-for="(meta, index) in stepMeta"
            :key="meta.internal"
            :class="{
              active: setup.currentStep.value === meta.internal,
              complete: setup.currentStep.value > meta.internal,
            }"
          >
            <span>{{ index + 1 }}</span>
            <div>
              <strong>{{ meta.label }}</strong>
              <small>{{ meta.title }}</small>
            </div>
          </li>
        </ol>

        <div class="setup-security-card">
          <span class="security-lock-mark" aria-hidden="true"></span>
          <div>
            <p class="panel-kicker">SECURITY</p>
            <strong>先验证身份，再使用密码</strong>
            <p>主机指纹必须显式确认。SSH 密码只用于创建托管凭据，不会在网页中持久化。</p>
          </div>
        </div>
      </aside>
    </div>
  </div>
</template>
