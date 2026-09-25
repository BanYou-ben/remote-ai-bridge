<template>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand">
        <span class="brand-mark" aria-hidden="true">RB</span>
        <div>
          <strong>Remote AI Bridge</strong>
          <span>远程连接与诊断控制台</span>
        </div>
      </div>
      <nav aria-label="Primary navigation">
        <RouterLink to="/">总览</RouterLink>
        <RouterLink to="/profiles">连接配置</RouterLink>
        <RouterLink to="/setup">添加连接</RouterLink>
      </nav>
      <p class="sidebar-note">本地 Web 控制台</p>
    </aside>
    <main class="main-content">
      <RouterView />
    </main>
  </div>
</template>
