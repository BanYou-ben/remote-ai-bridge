import { createApp } from 'vue'

import App from './App.vue'
import router from './router/index.js'
import './styles/base.css'
import './styles/app.css'
import './styles/obsidian.css'

createApp(App).use(router).mount('#app')
