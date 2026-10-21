<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import AuthGate from './components/AuthGate.vue'
import WorkspaceView from './views/WorkspaceView.vue'
import { useConversationStore } from './stores/conversation'
import { useAuthStore } from './stores/auth'
import { localSessions } from './stores/persistence'

const conversation = useConversationStore()
const auth = useAuthStore()
const authReady = ref(false)

watch(() => auth.user, (user, previous) => {
  if (user) {
    localSessions.setScope(user.id)
    conversation.hydrate()
  } else if (previous) {
    conversation.discardInMemory()
    localSessions.setScope()
  }
})

onMounted(async () => {
  await auth.initialize()
  authReady.value = true
})
</script>

<template>
  <main v-if="!authReady" class="auth-loading" role="status">正在安全检查登录状态…</main>
  <WorkspaceView v-else-if="auth.user" />
  <AuthGate v-else />
</template>

<style>
.auth-loading { display: grid; min-height: 100dvh; place-items: center; color: var(--text-secondary); background: var(--bg-app); font-size: 15px; }
</style>
