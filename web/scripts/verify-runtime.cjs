const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')

async function main() {
  const base = process.env.UI_BASE_URL || 'http://127.0.0.1:5184'
  const webRoot = fs.realpathSync(path.resolve(__dirname, '..'))
  const expected = JSON.parse(fs.readFileSync(path.join(webRoot, 'public/frontend-version.json'), 'utf8'))
  const response = await fetch(new URL('/__verimind_frontend', base), { signal: AbortSignal.timeout(5000) })
  assert.equal(response.status, 200, '前端身份接口必须可用')
  const actual = await response.json()
  assert.equal(actual.schema, expected.schema, '不能复用无版本标识的旧前端')
  assert.equal(actual.ui_id, expected.ui_id, '前端页面版本不一致')
  assert.deepEqual(actual.features, expected.features, '三类问题入口声明不一致')
  assert.equal(path.resolve(actual.source_root).toLowerCase(), webRoot.toLowerCase(), '端口指向另一份工作目录')
  const module = await fetch(new URL('/src/views/WorkspaceView.vue', base), { signal: AbortSignal.timeout(5000) })
  assert.equal(module.status, 200)
  const text = await module.text()
  assert.ok(text.includes('先查清事实') && text.includes('跨源问题'), '实际加载模块必须含新版三个入口')
  assert.ok(!text.includes('问题有方向'), '不允许回退到旧双入口首页')
  console.log('PASS 前端版本、实际源码目录、三类入口及加载模块一致')
}

main().catch((error) => { console.error(error.message); process.exitCode = 1 })
