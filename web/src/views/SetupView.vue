<script setup>
import { useManagedSetup } from '../composables/useManagedSetup.js'

const setup = useManagedSetup()
const visibleSteps = [1, 2, 4, 5, 6]
</script>

<template>
  <header class="page-header">
    <div>
      <p class="eyebrow">添加连接</p>
      <h1>添加托管连接</h1>
      <p>验证服务器身份、检测本地代理并创建由 Remote AI Bridge 管理的连接配置。</p>
    </div>
  </header>

  <ol class="wizard-progress" aria-label="设置进度">
    <li v-for="(step, index) in visibleSteps" :key="step" data-test="wizard-step" :class="{ active: setup.currentStep.value === step, complete: setup.currentStep.value > step }">
      {{ index + 1 }}
    </li>
  </ol>

  <section class="section-card setup-card">
    <div v-if="setup.error.value" class="structured-error" role="alert">
      <strong>{{ setup.error.value.code }}</strong>
      <span>{{ setup.error.value.message }}</span>
      <pre v-if="Object.keys(setup.error.value.details || {}).length">{{ JSON.stringify(setup.error.value.details, null, 2) }}</pre>
    </div>

    <form v-if="setup.currentStep.value === 1" class="form-grid" @submit.prevent="setup.prepare">
      <div class="section-heading wide"><div><h2>服务器信息</h2><p>先建立 SSH 握手并读取主机指纹，不会在此步骤发送密码。</p></div></div>
      <label>配置名称<input v-model.trim="setup.form.name" data-test="name" required autocomplete="off"></label>
      <label>主机地址<input v-model.trim="setup.form.host" data-test="host" required autocomplete="off"></label>
      <label>用户名<input v-model.trim="setup.form.username" data-test="username" required autocomplete="username"></label>
      <label>SSH 端口<input v-model.number="setup.form.port" data-test="ssh-port" required type="number" min="1" max="65535"></label>
      <div class="form-actions wide"><button class="primary-button" data-test="prepare" type="submit" :disabled="setup.submitting.value">{{ setup.submitting.value ? '正在验证……' : '验证主机身份' }}</button></div>
    </form>

    <div v-else-if="setup.currentStep.value === 2" class="wizard-step">
      <div class="section-heading"><div><h2>主机身份验证</h2><p>请通过可信渠道核对以下指纹。</p></div></div>
      <dl class="detail-grid host-fingerprint">
        <div><dt>主机</dt><dd>{{ setup.hostPreparation.value.host }}</dd></div>
        <div><dt>端口</dt><dd>{{ setup.hostPreparation.value.port }}</dd></div>
        <div><dt>密钥类型</dt><dd>{{ setup.hostPreparation.value.key_type }}</dd></div>
        <div class="wide"><dt>主机密钥指纹</dt><dd class="monospace fingerprint-value">{{ setup.hostPreparation.value.fingerprint }}</dd></div>
      </dl>
      <p v-if="setup.confirmationRequired.value" class="security-notice">该主机尚未受信任。只有在你核对指纹后，才允许继续密码认证。</p>
      <div class="form-actions">
        <button v-if="setup.confirmationRequired.value" class="primary-button" data-test="confirm-fingerprint" type="button" :disabled="setup.submitting.value" @click="setup.confirmFingerprint">确认此主机指纹</button>
        <button v-else class="primary-button" data-test="accept-known-host" type="button" @click="setup.acceptKnownFingerprint">继续检测本地代理</button>
        <button class="secondary-button" data-test="edit-server" type="button" @click="setup.editServer">修改服务器信息</button>
      </div>
    </div>

    <div v-else-if="setup.currentStep.value === 4" class="wizard-step">
      <div class="section-heading"><div><h2>本地代理检测</h2><p>候选端口完全由后端返回；失败候选不能被选择。</p></div></div>
      <button v-if="!setup.proxyDiscovery.value" class="primary-button" data-test="discover-proxy" type="button" :disabled="setup.submitting.value" @click="setup.discoverProxy">{{ setup.submitting.value ? '正在检测……' : '开始检测' }}</button>
      <div v-else class="table-wrap">
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
      <p v-if="setup.selectedProxy.value" class="selection-summary">已选择本地代理：127.0.0.1:{{ setup.selectedProxy.value }}</p>
      <div class="form-actions">
        <button v-if="setup.proxyDiscovery.value" class="secondary-button" type="button" :disabled="setup.submitting.value" @click="setup.discoverProxy">重新检测</button>
        <button class="primary-button" data-test="continue-setup" type="button" :disabled="!setup.selectedProxy.value" @click="setup.continueToSetup">继续</button>
        <button class="secondary-button" data-test="edit-server" type="button" @click="setup.editServer">修改服务器信息</button>
      </div>
    </div>

    <form v-else-if="setup.currentStep.value === 5" class="form-grid" @submit.prevent="setup.submit">
      <div class="section-heading wide"><div><h2>创建托管连接</h2><p>密码仅用于本次认证，请求完成后会清空且不会持久化。</p></div></div>
      <label class="wide">SSH 密码<input v-model="setup.form.password" data-test="password" required type="password" autocomplete="current-password"></label>
      <label>起始远端端口<input v-model.number="setup.form.startRemotePort" type="number" min="1" max="65535"></label>
      <label>最大端口尝试数<input v-model.number="setup.form.maxRemotePortAttempts" type="number" min="1"></label>
      <label class="wide">Endpoint Probe URL<input v-model.trim="setup.form.endpointProbeUrl" type="url" required></label>
      <label class="checkbox-field wide"><input v-model="setup.form.autoReconnect" type="checkbox"> 自动重连</label>
      <div class="form-actions wide"><button class="primary-button" data-test="submit-setup" type="submit" :disabled="setup.submitting.value">{{ setup.submitting.value ? '正在创建……' : '创建连接配置' }}</button></div>
    </form>

    <div v-else class="wizard-step setup-complete">
      <div class="section-heading"><div><h2>连接配置已创建</h2><p>托管 SSH 凭据已安全配置。</p></div></div>
      <dl class="detail-grid">
        <div><dt>配置名称</dt><dd>{{ setup.result.value.name }}</dd></div>
        <div><dt>主机</dt><dd>{{ setup.result.value.host }}</dd></div>
        <div><dt>SSH 端口</dt><dd>{{ setup.result.value.ssh_port }}</dd></div>
        <div><dt>本地代理</dt><dd>{{ setup.result.value.local_proxy_host }}:{{ setup.result.value.local_proxy_port }}</dd></div>
        <div><dt>远端端口</dt><dd>{{ setup.result.value.remote_bind_host }}:{{ setup.result.value.remote_port }}</dd></div>
        <div><dt>自动重连</dt><dd>{{ setup.result.value.auto_reconnect ? '已开启' : '已关闭' }}</dd></div>
      </dl>
      <div class="form-actions">
        <RouterLink class="primary-button button-link" :to="`/profiles/${encodeURIComponent(setup.result.value.name)}`">查看配置</RouterLink>
        <RouterLink class="secondary-button button-link" to="/">返回总览</RouterLink>
      </div>
    </div>
  </section>
</template>
