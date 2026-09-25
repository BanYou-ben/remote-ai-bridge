<template>
  <div class="app-shell">
    <aside class="sidebar" aria-label="Primary navigation">
      <RouterLink class="brand" to="/" aria-label="Remote AI Bridge 首页">
        <span class="brand-mark" aria-hidden="true">
          <img class="brand-logo-mark" src="/rab-logo-mark.svg" alt="" />
        </span>
        <strong>RAB</strong>
      </RouterLink>

      <nav class="primary-nav">
        <RouterLink class="nav-link" to="/">
          <span class="nav-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24"><path d="M4 11.2 12 4l8 7.2v8.3a.5.5 0 0 1-.5.5h-5v-5h-5v5h-5a.5.5 0 0 1-.5-.5v-8.3Z"/></svg>
          </span>
          <span class="nav-label">总览</span>
        </RouterLink>

        <RouterLink class="nav-link" to="/profiles">
          <span class="nav-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24"><path d="M5 5.5A2.5 2.5 0 0 1 7.5 3h9A2.5 2.5 0 0 1 19 5.5v3A2.5 2.5 0 0 1 16.5 11h-9A2.5 2.5 0 0 1 5 8.5v-3Zm2.5-.5a.5.5 0 0 0-.5.5v3a.5.5 0 0 0 .5.5h9a.5.5 0 0 0 .5-.5v-3a.5.5 0 0 0-.5-.5h-9ZM5 15.5A2.5 2.5 0 0 1 7.5 13h9a2.5 2.5 0 0 1 2.5 2.5v3a2.5 2.5 0 0 1-2.5 2.5h-9A2.5 2.5 0 0 1 5 18.5v-3Zm2.5-.5a.5.5 0 0 0-.5.5v3a.5.5 0 0 0 .5.5h9a.5.5 0 0 0 .5-.5v-3a.5.5 0 0 0-.5-.5h-9Z"/></svg>
          </span>
          <span class="nav-label">连接</span>
        </RouterLink>

        <RouterLink class="nav-link" to="/setup">
          <span class="nav-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24"><path d="M11 5h2v6h6v2h-6v6h-2v-6H5v-2h6V5Z"/></svg>
          </span>
          <span class="nav-label">添加</span>
        </RouterLink>
      </nav>

      <div class="sidebar-footer">
        <span class="local-status-dot" aria-hidden="true"></span>
        <span>LOCAL</span>
      </div>
    </aside>

    <section class="workspace">
      <header class="workspace-bar">
        <div class="workspace-brand">
          <RouterLink class="workspace-logo-link" to="/" aria-label="Remote AI Bridge 首页">
            <img class="workspace-logo" src="/rab-logo-lockup.svg" alt="Remote AI Bridge" />
          </RouterLink>
          <span class="workspace-brand-separator" aria-hidden="true"></span>
          <strong>远程连接控制台</strong>
        </div>
        <div class="workspace-meta">
          <span class="workspace-chip">
            <span class="workspace-chip-dot" aria-hidden="true"></span>
            本地控制台
          </span>
        </div>
      </header>

      <main class="main-content">
        <RouterView />
      </main>
    </section>
  </div>
</template>
