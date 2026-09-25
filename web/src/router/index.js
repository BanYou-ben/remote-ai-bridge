import { createRouter, createWebHashHistory } from 'vue-router'

import DashboardView from '../views/DashboardView.vue'
import ProfileDetailView from '../views/ProfileDetailView.vue'
import ProfilesView from '../views/ProfilesView.vue'
import SetupView from '../views/SetupView.vue'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', name: 'dashboard', component: DashboardView },
    { path: '/profiles', name: 'profiles', component: ProfilesView },
    { path: '/profiles/:name', name: 'profile-detail', component: ProfileDetailView, props: true },
    { path: '/setup', name: 'setup', component: SetupView },
  ],
})

export default router
