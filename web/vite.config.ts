import { readFileSync, realpathSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

const webRoot = realpathSync(fileURLToPath(new URL('.', import.meta.url)))
const identity = JSON.parse(readFileSync(new URL('./public/frontend-version.json', import.meta.url), 'utf8'))

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, webRoot, 'VITE_')
  const backendTarget = env.VITE_BACKEND_TARGET || 'http://127.0.0.1:8000'
  return {
  root: webRoot,
  plugins: [vue(), {
    name: 'verimind-frontend-identity',
    configureServer(server) {
      // 本机启动器核对实际源码目录；不进入问答接口或用户页面。
      server.middlewares.use('/__verimind_frontend', (_request, response) => {
        response.setHeader('Content-Type', 'application/json; charset=utf-8')
        response.setHeader('Cache-Control', 'no-store')
        response.end(JSON.stringify({ ...identity, source_root: webRoot.replaceAll('\\', '/') }))
      })
    },
  }],
  optimizeDeps: { exclude: ['pdfjs-dist'] },
  server: {
    host: '127.0.0.1',
    port: 5184,
    strictPort: true,
    proxy: {
      // changeOrigin 会把 Host 改写成目标（127.0.0.1:8000），而后端的
      // _verify_same_origin() 要求 Origin 的 netloc 逐字等于 Host —— 浏览器发的是
      // dev server 的地址，于是登录等写接口在 dev 下必然 403「请求来源校验失败」。
      // 关掉 changeOrigin 保留原始 Host 即可让校验成立（后端优先读 x-forwarded-host，
      // 不手工写死代理 Host：常规入口固定 5184，受控测试可显式使用其他端口。
      '/api': { target: backendTarget, changeOrigin: false },
      '/health': { target: backendTarget, changeOrigin: false },
    },
    watch: {
      // 编辑器（含 AI 工具）保存文件时会先写一个 `<名字>.<pid>.<uuid>.tmpdir/` 临时目录
      // 再改名，Windows 上监视这些瞬时路径会抛 EBUSY 并让 dev server **整个进程崩溃**
      // ——不是页面报错，是进程直接退出，且退出后连热更新一起没了。
      // 只忽略 scripts/ 是不够的：这类临时目录会出现在任何被编辑的目录下（src/ 下也踩过）。
      // 也试过 `**/*.tmpdir/**`，仍然会崩 —— chokidar 对**新出现的隐藏目录**会先 watch
      // 再匹配忽略规则，匹配来不及。只有把点开头的整棵子树排除掉才真正拦住。
      ignored: ['**/scripts/**', '**/.*', '**/.*/**', '**/*.tmp', '**/*.tmpdir/**'],
    },
  },
  }
})
