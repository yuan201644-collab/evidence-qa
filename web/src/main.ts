import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import './styles/global.css'
import { initTheme } from './utils/theme'

// 样式之后初始化：此时 data-theme 已由 index.html 的内联脚本设好，
// 这里只负责接管系统主题变化监听。
initTheme()

createApp(App).use(createPinia()).mount('#app')
