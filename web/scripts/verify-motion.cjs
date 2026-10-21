/* 动效交互验证：使用明确标注的受控响应，不发模型请求，不计作业务效果。 */
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const { chromium } = require('playwright')

const base = process.env.UI_BASE_URL || 'http://127.0.0.1:5184'
const out = path.resolve(process.env.UI_OUTPUT_DIR || path.join(__dirname, '../../agent_temp/motion-check'))
const results = []

async function settle(page) {
  await page.evaluate(async () => {
    const finite = document.getAnimations().filter((item) => item.effect?.getComputedTiming().iterations !== Infinity)
    await Promise.allSettled(finite.map((item) => item.finished))
  })
}
async function noOverflow(page) {
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), '页面不能因位移动效产生横向溢出')
}

async function check(browser, reduced, dark, mobile) {
  const name = `${reduced ? 'reduce' : 'motion'}-${dark ? 'dark' : 'light'}-${mobile ? 'mobile' : 'desktop'}`
  const context = await browser.newContext({
    viewport: mobile ? { width: 390, height: 844 } : { width: 1440, height: 900 },
    reducedMotion: reduced ? 'reduce' : 'no-preference', colorScheme: dark ? 'dark' : 'light',
    serviceWorkers: 'block',
    ...(process.argv.includes('--video') && name === 'motion-light-desktop' ? { recordVideo: { dir: path.join(out, 'video'), size: { width: 1440, height: 900 } } } : {}),
  })
  const page = await context.newPage()
  const errors = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.route('**/api/auth/me', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ user: { id: 'ui-check-user', username: 'ui-check-user', role: 'user', active: true, created_at: '2026-10-14T00:00:00Z' } }),
  }))
  await page.addInitScript(() => {
    window.__motionStarts = []
    document.addEventListener('animationstart', (event) => window.__motionStarts.push(event.animationName))
    window.__streamProgressTest = false
    const nativeFetch = window.fetch.bind(window)
    window.fetch = (input, init) => {
      const url = new URL(typeof input === 'string' ? input : input.url, location.href)
      if (url.pathname !== '/api/ask/stream' || !window.__streamProgressTest) return nativeFetch(input, init)
      const body = new ReadableStream({
        start(controller) {
          const event = { type: 'stage', id: 'live-stage-1', stage_id: 'rag_retrieve', label: '检索相关文档片段', status: 'started' }
          controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`))
          init?.signal?.addEventListener('abort', () => {
            try { controller.error(new DOMException('已停止等待', 'AbortError')) } catch { /* stream may already be cancelled */ }
          }, { once: true })
        },
      })
      return Promise.resolve(new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } }))
    }
  })
  await page.route('**/health', (route) => route.fulfill({ contentType: 'application/json', body: '{"status":"ok"}' }))
  await page.route('**/api/database/status', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ status: 'connected', database: 'chinook', catalog_database: 'chinook', label: 'Chinook 示例库（PostgreSQL）', dialect: 'postgres', table_count: 1, checked_at: '2026-10-13T00:00:00Z' }),
  }))
  await page.route('**/api/database/schema', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ database: 'chinook', label: 'Chinook 示例库（PostgreSQL）', dialect: 'postgres', contract_version: 'motion-fixture', table_count: 1, tables: [
      { schema: 'public', name: 'artist', description: '动效夹具结构', column_count: 1, columns: [{ name: 'artist_id', type: 'INT', nullable: false, is_primary_key: true, description: '主键', foreign_key_to: null }] },
    ] }),
  }))
  await page.route('**/api/database/unstructured/status', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ status: 'memory', backend: 'memory', label: '文档检索使用内存模式', checked_at: '2026-10-13T00:00:00Z' }),
  }))
  await page.route('**/api/capabilities/cross-source-demo', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ status: 'unavailable' }),
  }))
  await page.route('**/api/models', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({
      default_model_id: 'deepseek-v4-1-flash',
      models: [
        { id: 'deepseek-v4-1-flash', label: 'DeepSeek-V4.1-Flash', provider: '火山方舟', model: 'deepseek-v4.1-flash', configured: true, selected: true },
        { id: 'glm-4-flash', label: 'GLM-4-Flash', provider: '智谱', model: 'glm-4-flash', configured: true, selected: false },
      ],
    }),
  }))
  await page.route('**/api/ask/stream', async (route) => {
    const response = {
      suggestions: [
        { question: '继续核对智能计算设备年度维保服务合同中的质保期适用范围和例外条款？', reason: '依据刚才引用的第二章质保条款。' },
        { question: 'GPU 加速卡的质保期是否与其他设备不同？', reason: '依据刚才召回的质保期限差异。' },
      ],
      answer: '动效验证夹具：原文片段可以核对 [1]。', sql: null, need_clarify: false,
      refs: [{ kind: 'doc', doc_id: 'motion-fixture', doc_title: '动效验证文档（非业务数据）', page: null, snippet: '受控动效检查片段。', section_path: '检查说明', bboxes: [] }],
      trace: [{ step_id: 1, tool: 'doc_search', input: { question: '动效验证夹具' }, output: {}, elapsed_ms: 12, hit: [] }],
    }
    const events = [
      { type: 'stage', id: 'fixture-stage-1', stage_id: 'fixture', label: '读取受控文档', status: 'started' },
      { type: 'stage', id: 'fixture-stage-1', stage_id: 'fixture', label: '读取受控文档', status: 'completed', elapsed_ms: 12 },
      { type: 'complete', response },
      { type: 'end' },
    ]
    await route.fulfill({ contentType: 'text/event-stream', body: events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join('') }).catch(() => {})
  })
  try {
    await page.goto(base)
    await page.locator('.welcome').waitFor()
    await settle(page)
    const starts = await page.evaluate(() => window.__motionStarts)
    if (reduced) assert.equal(starts.length, 0, '减少动态效果时不播放入场动画')
    else assert.ok(starts.length > 0, '普通模式实际播放入场动画')
    assert.equal(await page.locator('#welcome-title').evaluate((el) => getComputedStyle(el).opacity), '1')
    assert.equal(await page.evaluate(() => document.getAnimations().filter((item) => item.playState === 'running' && item.effect?.target instanceof Element && item.effect.target.closest('.welcome')).length), 0, '静态首页不循环播放装饰动画')
    await noOverflow(page)

    if (!mobile) {
      await page.locator('.example-card').first().hover()
      await settle(page)
      const transform = await page.locator('.example-card__icon').first().evaluate((el) => getComputedStyle(el).transform)
      if (reduced) assert.ok(transform === 'none' || transform === 'matrix(1, 0, 0, 1, 0, 0)')
      else assert.notEqual(transform, 'none', '悬停图标应有短促位移反馈')
      if (reduced) {
        assert.equal(await page.locator('.brand__mark').evaluate((el) => getComputedStyle(el, '::after').animationName), 'none')
      } else {
        await page.locator('.brand').hover()
        await page.waitForFunction(() => getComputedStyle(document.querySelector('.brand__mark'), '::after').animationName === 'vm-blade')
        await settle(page)
        await page.mouse.move(700, 40)
      }
    }

    const opener = page.getByRole('button', { name: '查看系统说明' })
    await opener.click()
    await settle(page)
    assert.equal(await page.getByRole('dialog', { name: 'FEVA 事实—证据核验架构' }).isVisible(), true)
    assert.equal(await page.getByRole('button', { name: '关闭架构导览' }).evaluate((el) => el === document.activeElement), true)
    await page.keyboard.press('Escape')
    await page.waitForFunction(() => !document.querySelector('.architecture-backdrop'))
    assert.equal(await opener.evaluate((el) => el === document.activeElement), true, '弹窗退出后焦点恢复')

    if (mobile) {
      await page.getByRole('button', { name: '展开左侧会话栏' }).click()
      await page.locator('.sidebar-open .database-entry').waitFor()
    }
    const databaseOpener = page.locator('.database-entry')
    await databaseOpener.scrollIntoViewIfNeeded()
    assert.equal(await databaseOpener.getAttribute('aria-label'), '打开数据库连接状态')
    await databaseOpener.click()
    const databaseDialog = page.getByRole('dialog', { name: '数据库概览' })
    await databaseDialog.getByText('已连接', { exact: true }).waitFor()
    assert.equal(await databaseDialog.getByRole('button', { name: '关闭数据库面板' }).evaluate((el) => el === document.activeElement), true)
    const motionBase = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--motion-base').trim())
    assert.equal(motionBase, reduced ? '0ms' : '200ms', '数据库面板使用遵循减少动态效果设置的公共动效参数')
    await settle(page)
    await page.keyboard.press('Escape')
    await databaseDialog.waitFor({ state: 'detached' })
    if (mobile) assert.equal(await page.locator('.sidebar-toggle').evaluate((el) => el === document.activeElement), true, '手机侧栏收起后焦点回到可见的侧栏开关')
    else assert.equal(await databaseOpener.evaluate((el) => el === document.activeElement), true, '数据库面板退出后焦点恢复到打开入口')
    await page.evaluate(() => { window.__streamProgressTest = true })
    const input = page.getByRole('textbox', { name: '输入问题' })
    await input.fill('动效验证等待（受控夹具）')
    await input.press('Enter')
    await page.locator('.thinking').waitFor()
    await page.waitForFunction(() => document.querySelector('.thinking-mark i'))
    await page.locator('.progress-stage__label', { hasText: '检索相关文档片段' }).waitFor()
    assert.equal(await page.locator('.progress-stage--started').count(), 1, '后端阶段事件到达时应显示正在执行的时间线节点')
    const loops = await page.locator('.progress-stage--started .progress-stage__rail i').evaluate((el) => el.getAnimations().filter((item) => item.effect?.getComputedTiming().iterations === Infinity).length)
    assert.equal(loops, reduced ? 0 : 1, '等待动画遵循减少动态效果设置')
    await settle(page)
    await page.screenshot({ path: path.join(out, `${name}-progress.png`) })
    await page.getByRole('button', { name: '停止等待', exact: true }).click()
    await page.evaluate(() => { window.__streamProgressTest = false })
    await page.waitForFunction(() => !document.querySelector('.thinking'))
    assert.equal(await input.isEnabled(), true)

    await input.fill('动效验证回答（受控夹具）')
    await input.press('Enter')
    await page.getByText('动效验证夹具：原文片段可以核对', { exact: false }).waitFor()
    await settle(page)
    await page.getByRole('button', { name: '查看引用 1', exact: true }).last().click()
    await page.locator('.doc-fallback').waitFor()
    await settle(page)
    assert.equal(await page.locator('.evidence').evaluate((el) => getComputedStyle(el).transform), 'none', '证据面板落定后不残留变换')
    if (mobile) {
      await page.getByRole('button', { name: '关闭证据面板' }).click()
      await page.locator('.evidence').waitFor({ state: 'detached' })
      const suggestionList = page.locator('.suggestions__list')
      await suggestionList.waitFor()
      assert.equal(await page.locator('.suggestion-item').count(), 2, '手机端保留多条可横向浏览的追问建议')
      assert.equal(await suggestionList.evaluate((el) => getComputedStyle(el).flexWrap), 'nowrap', '手机端建议保持单行横向布局')
      assert.equal(await page.locator('.suggestion-chip__text').first().evaluate((el) => getComputedStyle(el).whiteSpace), 'nowrap', '长问题单行省略，避免建议区纵向增长')
      assert.equal(await page.locator('.suggestion-chip__reason').first().evaluate((el) => getComputedStyle(el).display), 'none', '依据默认收起，不占用手机屏幕高度')
      await page.locator('.suggestion-reason-toggle').first().click()
      await page.locator('.suggestions__reason-panel').getByText('依据刚才引用的第二章质保条款。', { exact: true }).waitFor()
      assert.equal(await page.locator('.suggestions__reason-panel').isVisible(), true, '点击依据图标后可查看追问来源')
      await page.locator('.suggestion-reason-toggle').first().click()
      assert.equal(await page.locator('.suggestions__reason-panel').count(), 0, '依据说明可以再次收起')
      const mobileNote = page.locator('.composer-note-mobile')
      assert.equal(await mobileNote.locator('> p').isVisible(), false, '手机端回答提示默认折叠')
      await mobileNote.locator('summary').click()
      assert.equal(await mobileNote.evaluate((el) => el.open), true, '点击提示后可展开完整说明')
      assert.equal(await mobileNote.locator('> p').isVisible(), true)
      await mobileNote.locator('summary').click()
      assert.equal(await mobileNote.evaluate((el) => el.open), false, '提示说明可以再次收起')
      await noOverflow(page)
      await page.screenshot({ path: path.join(out, name + '-composer.png') })
    }
    await settle(page)
    await noOverflow(page)
    await page.screenshot({ path: path.join(out, `${name}.png`) })
    await page.keyboard.press('Escape')
    assert.equal(await page.locator('.evidence').count(), 0)
    await page.getByRole('button', { name: '查看处理详情', exact: false }).last().click()
    await page.locator('.trace__body').waitFor()
    await settle(page)
    await noOverflow(page)
    assert.deepEqual(errors, [])
    const record = { name, status: 'passed', entry_animation_events: starts.length, browser_errors: errors }
    results.push(record)
    console.log('PASS ' + name + ' 入场、弹窗/数据库面板焦点与动效、等待与停止、回答及证据、窄屏布局')
  } finally { await context.close() }
}

;(async () => {
  await fs.mkdir(out, { recursive: true })
  const browser = await chromium.launch({ channel: process.env.UI_BROWSER_CHANNEL || (process.platform === 'win32' ? 'msedge' : undefined), headless: true })
  try {
    for (const reduced of [false, true]) {
      for (const dark of [false, true]) await check(browser, reduced, dark, false)
      await check(browser, reduced, false, true)
    }
    await fs.writeFile(path.join(out, 'motion-checks.json'), JSON.stringify({ status: 'passed', generated_at: new Date().toISOString(), target: base, scope: '受控界面动效验证，非模型或业务效果实验', results }, null, 2))
  } finally { await browser.close() }
})().catch(async (error) => {
  await fs.mkdir(out, { recursive: true })
  await fs.writeFile(path.join(out, 'motion-checks.json'), JSON.stringify({ status: 'failed', results, error: error.message }, null, 2))
  console.error(error)
  process.exitCode = 1
})
