const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const { chromium } = require('playwright')

const outputDir = path.resolve(process.env.UI_OUTPUT_DIR || path.join(__dirname, '../../agent_temp/frontend_redesign/output'))
const base = process.env.UI_BASE_URL || 'http://127.0.0.1:5184'
const checks = []
const reportPath = path.join(outputDir, process.argv.includes('--live') ? 'live-checks.json' : 'controlled-checks.json')
const scope = process.argv.includes('--live') ? '真实在线界面检查（两个开发样例）' : '受控接口界面验证，非系统效果实验'
const record = (name) => { checks.push(name); console.log('PASS ' + name) }
const trace = [{ step_id: 1, tool: 'nl2sql', input: { question: '界面验证数据' }, output: { rows: 3 }, elapsed_ms: 42, hit: [] }]
const fixture = {
  answer: '界面验证：共 3 条数据记录 [1]。',
  trace,
  refs: [{ kind: 'sql', snippet: '界面验证记录，非真实业务结果。', table: 'customer', column: 'customer_id', row_pk: '1' }],
  sql: 'SELECT customer_id FROM customer',
  need_clarify: false,
  result_set: { id: 'ui-fixture', row_count: 3, complete: true, page_size: 2, expires_at: '2099-01-01T00:00:00Z' },
}
const factReviewRefs = [
  fixture.refs[0],
  { kind: 'doc', doc_id: 'HT-UI-001', doc_title: '事实核验界面夹具', page: 1, section_path: '2.1', char_start: 0, char_end: 12, bboxes: [], page_width: null, page_height: null, extraction_method: 'plain', snippet: '修复时限 8 小时' },
]
const factReview = (verification_mode = 'strict') => ({
  schema_version: '2', coverage: 'cross_source_work_order_sla', verification_mode, verification_scope: 'fact_dependencies_and_business_checks',
  scope_note: '只展示结构化账本记录，不代表其他答案文字均已核验。',
  items: [
    { fact_id: 'work_order_facts', label: '工单基本信息与事件', summary: '工单 WO-1 对应设备 EQ-1。', status: 'supported', execution_status: 'completed', evidence_link_status: 'complete', checks: [], evidence_ref_indices: [0], reason_code: null, reason: null },
    { fact_id: 'sla_verdict', label: '修复时限判断', summary: '修复耗时 7 小时，合同限时 8 小时。', status: 'supported', execution_status: 'completed', evidence_link_status: 'partial', checks: [{ code: 'service_restored_within_limit', result: 'passed' }], evidence_ref_indices: [1], reason_code: null, reason: null },
    { fact_id: 'invalid_ref_fixture', label: '无效索引防护', status: 'supported', execution_status: 'completed', evidence_link_status: 'complete', checks: [], evidence_ref_indices: [8], reason_code: null, reason: null },
  ],
})
const singleSourceReview = (coverage, verification_scope, label, code) => ({
  schema_version: '2', coverage, verification_mode: 'source_specific', verification_scope,
  scope_note: '此卡只记录本路径实际产生的执行检查，不代表答案语义已独立验证。',
  items: [{
    fact_id: `${coverage}-fixture`, label, summary: '界面验证用受控数据。', status: 'candidate',
    execution_status: 'completed', evidence_link_status: 'complete',
    checks: [{ code, result: 'passed' }], evidence_ref_indices: [0], reason_code: null, reason: null,
  }],
})
const toolErrorFixture = {
  answer: 'SQL 执行失败：此错误文本不应显示为普通回答。',
  trace: [{ step_id: 3, tool: 'nl2sql', input: {}, output: { error: 'controlled database failure' }, elapsed_ms: 2, hit: [] }],
  refs: [], sql: null, need_clarify: false,
}
const unstructuredDatabaseFixture = {
  status: 'connected', backend: 'pgvector', label: '文档向量库已连接', database: 'chinook', schema: 'public',
  corpus_document_count: 2, indexed_document_count: 2, chunk_count: 8, vector_extension_ready: true, warning: null,
  checked_at: '2026-10-13T00:00:00Z',
  tables: [{ schema: 'public', name: 'chunk_vectors', description: '文档切片与向量', column_count: 2, columns: [] }],
  documents: [
    { doc_id: 'KB-001_demo', title: '维保服务合同', extension: 'pdf', indexed_chunk_count: 5 },
    { doc_id: 'KB-010_demo', title: '问数服务接口说明', extension: 'md', indexed_chunk_count: 3 },
  ],
}

async function checkOverflow(page, name) {
  const dimensions = await page.evaluate(() => ({ actual: document.documentElement.scrollWidth, width: innerWidth }))
  assert.ok(dimensions.actual <= dimensions.width + 1, name + ': horizontal overflow ' + JSON.stringify(dimensions))
  record(name + ' 无页面横向溢出')
}

async function controlled(browser) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce', serviceWorkers: 'block' })
  const page = await context.newPage()
  await page.addInitScript((finalResponse) => {
    const nativeFetch = window.fetch.bind(window)
    window.fetch = (input, init) => {
      const url = typeof input === 'string' ? input : input instanceof Request ? input.url : String(input)
      const body = typeof init?.body === 'string' ? init.body : ''
      if (!url.endsWith('/api/ask/stream') || !body.includes('进度自动滚动验证')) return nativeFetch(input, init)
      const encoder = new TextEncoder()
      const frame = (event) => encoder.encode(`data: ${JSON.stringify(event)}\n\n`)
      const stream = new ReadableStream({
        async start(controller) {
          for (let index = 0; index < 18; index += 1) {
            const stage = { id: `scroll-${index}`, stage_id: 'scroll_check', label: `自动滚动阶段 ${String(index + 1).padStart(2, '0')}`, status: 'started' }
            controller.enqueue(frame({ type: 'stage', ...stage }))
            await new Promise((resolve) => setTimeout(resolve, 38))
            controller.enqueue(frame({ type: 'stage', ...stage, status: 'completed', elapsed_ms: 38 }))
            await new Promise((resolve) => setTimeout(resolve, 38))
          }
          await new Promise((resolve) => setTimeout(resolve, 1800))
          controller.enqueue(frame({ type: 'complete', response: finalResponse }))
          controller.enqueue(frame({ type: 'end' }))
          controller.close()
        },
      })
      return Promise.resolve(new Response(stream, { status: 200, headers: { 'content-type': 'text/event-stream' } }))
    }
  }, fixture)
  const errors = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.route('**/api/auth/me', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ user: { id: 'ui-check-admin', username: 'ui-check-admin', role: 'admin', active: true, created_at: '2026-10-14T00:00:00Z' } }),
  }))
  await page.route('**/api/database/status', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ status: 'connected', database: 'chinook', catalog_database: 'chinook', label: 'Chinook 示例库（PostgreSQL）', dialect: 'postgres', table_count: 2, checked_at: '2026-10-13T00:00:00Z' }),
  }))
  await page.route('**/api/database/schema', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({
      database: 'chinook', label: 'Chinook 示例库（PostgreSQL）', dialect: 'postgres', contract_version: 'fixture-1', table_count: 2,
      tables: [
        { schema: 'public', name: 'album', description: '专辑目录', column_count: 3, columns: [
          { name: 'album_id', type: 'INT', nullable: false, is_primary_key: true, description: '专辑主键', foreign_key_to: null },
          { name: 'artist_id', type: 'INT', nullable: false, is_primary_key: false, description: '所属艺人标识', foreign_key_to: 'artist.artist_id' },
          { name: 'title', type: 'VARCHAR(160)', nullable: false, is_primary_key: false, description: '专辑标题', foreign_key_to: null },
        ] },
        { schema: 'public', name: 'artist', description: '艺人目录', column_count: 2, columns: [
          { name: 'artist_id', type: 'INT', nullable: false, is_primary_key: true, description: '艺人主键', foreign_key_to: null },
          { name: 'name', type: 'VARCHAR(120)', nullable: false, is_primary_key: false, description: '艺人名称', foreign_key_to: null },
        ] },
      ],
    }),
  }))
  await page.route('**/api/database/unstructured/status', (route) => route.fulfill({
    contentType: 'application/json', body: JSON.stringify(unstructuredDatabaseFixture),
  }))
  await page.route('**/api/database/unstructured', (route) => route.fulfill({
    contentType: 'application/json', body: JSON.stringify(unstructuredDatabaseFixture),
  }))
  let kagReadinessReady = false
  await page.route('**/api/kag/status', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify(kagReadinessReady
      ? { ready: true, status: 'ready', reason_code: null }
      : { ready: false, status: 'not_configured', reason_code: 'runtime_not_configured' }),
  }))
  let crossSourceReadiness = 'ready'
  let crossSourceReadinessCalls = 0
  await page.route('**/api/capabilities/cross-source-demo', (route) => {
    crossSourceReadinessCalls += 1
    return route.fulfill({
      contentType: 'application/json', body: JSON.stringify({ status: crossSourceReadiness }),
    })
  })
  await page.route('**/api/models', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({
      default_model_id: 'deepseek-v4-1-flash',
      models: [
        { id: 'deepseek-v4-1-flash', label: 'DeepSeek-V4.1-Flash', provider: '火山方舟', model: 'deepseek-v4.1-flash', configured: true, selected: true },
        { id: 'glm-4-flash', label: 'GLM-4-Flash', provider: '智谱', model: 'glm-4-flash', configured: true, selected: false },
        { id: 'future-model-v9', label: 'Future Model V9', provider: '示例服务商', model: 'future-model-v9', configured: false, selected: false },
      ],
    }),
  }))
  const editableModelProfiles = [
    { id: 'deepseek-v4-1-flash', label: 'DeepSeek-V4.1-Flash', provider: '火山方舟', model: 'deepseek-v4.1-flash', base_url: 'https://ark.example/v1', configured: true, has_api_key: true },
    { id: 'glm-4-flash', label: 'GLM-4-Flash', provider: '智谱', model: 'glm-4-flash', base_url: 'https://open.bigmodel.cn/api/paas/v4', configured: true, has_api_key: true },
    { id: 'future-model-v9', label: 'Future Model V9', provider: '示例服务商', model: 'future-model-v9', base_url: 'https://future.example/v1', configured: false, has_api_key: false },
  ]
  const modelConfigWrites = []
  await page.route((url) => {
    const pathname = new URL(url).pathname
    return pathname === '/api/model-config' || /^\/api\/model-config\/[^/]+$/.test(pathname)
  }, async (route) => {
    const request = route.request()
    if (request.method() === 'GET') {
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ profiles: editableModelProfiles }) })
    }
    if (request.method() === 'PUT') {
      const profileId = new URL(request.url()).pathname.split('/').pop()
      const payload = request.postDataJSON()
      modelConfigWrites.push({ profileId, payload })
      const profile = editableModelProfiles.find((item) => item.id === profileId)
      assert.ok(profile, '配置请求只能更新已支持的模型')
      profile.model = payload.model
      profile.base_url = payload.base_url
      profile.configured = true
      profile.has_api_key = Boolean(payload.api_key) || profile.has_api_key
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ profile }) })
    }
    return route.fulfill({ status: 405, contentType: 'application/json', body: '{"detail":"method not allowed"}' })
  })
  await page.addInitScript(() => {
    window.__threadScrollCalls = []
    const nativeScrollTo = Element.prototype.scrollTo
    Element.prototype.scrollTo = function (...args) {
      if (this.classList.contains('thread')) {
        const options = args[0]
        window.__threadScrollCalls.push({
          behavior: typeof options === 'object' && options ? options.behavior ?? 'auto' : 'auto',
        })
      }
      return nativeScrollTo.apply(this, args)
    }
  })
  // 使用仓库已有十页 PDF 检查翻页；回答与坐标仍是明确标注的界面夹具。
  // PDF 字节有两种来路，两者都算「受控夹具」，都不该碰后端鉴权端点：
  //   ① 远程模式（默认，VITE_REMOTE_DOCUMENTS 未置 false）：走 /api/documents/*/file，由下面 context.route 兜住；
  //   ② 静态模式（dev server 带 VITE_REMOTE_DOCUMENTS=false）：直接取同源 /documents/*.pdf（web/public 下，不过鉴权）。
  // 静态模式的存在是因为 pdf.js 在 Web Worker 里取字节，worker 请求 page.route 拦不到（见 to-all 意见）。
  let pdfFixtureRequests = 0
  let staticPdfRequests = 0
  page.on('request', (request) => {
    if (/\/documents\/[^/]+\.pdf(?:\?.*)?$/.test(new URL(request.url()).pathname)) staticPdfRequests += 1
  })
  await context.route(/\/api\/documents\/.*\/preview(?:\?.*)?$/, (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ kind: 'pdf', file_name: 'KB-003_10页规模版.pdf' }),
  }))
  await context.route(/\/api\/documents\/.*\/file(?:\?.*)?$/, (route) => {
    pdfFixtureRequests += 1
    return route.fulfill({ contentType: 'application/pdf', path: path.resolve(__dirname, '../../data/corpus/scale/KB-003_10页规模版.pdf') })
  })
  let mode = 'success'
  let release
  const seenModelProfiles = []
  const seenFactReviewVersions = []
  const seenRetrievalModes = []
  await page.route('**/api/ask/stream', async (route) => {
    seenModelProfiles.push(route.request().headers()['x-model-profile'] || null)
    seenFactReviewVersions.push(route.request().headers()['x-askdata-fact-review'] || null)
    seenRetrievalModes.push(route.request().postDataJSON().retrieval_mode || 'standard')
    const request = route.request().postDataJSON()
    if (mode === 'hold') await new Promise((resolve) => { release = resolve })
    if (request.question.includes('标题状态隐藏验证')) await new Promise((resolve) => setTimeout(resolve, 350))
    if (mode === 'legacy' || mode === 'legacy-tool-error') return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"stream route not installed yet"}' })
    if (mode === 'error') return route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'internal error' }) })
    const pdfRef = { kind: 'doc', doc_id: 'KB-003_数据中台技术白皮书', doc_title: '界面验证 PDF（仅检查翻页与引用）', page: 1, section_path: '界面验证', char_start: null, char_end: null, bboxes: [[30, 30, 80, 50]], page_width: 595, page_height: 842, extraction_method: 'text_layer', snippet: '此处为界面测试夹具，不是业务回答。' }
    const body = mode === 'tool-error' ? toolErrorFixture : request.question.includes('事实核验消融展示') ? {
      ...fixture, answer: '界面验证：消融模式事实记录。', refs: factReviewRefs, result_set: null,
      fact_review: factReview('ablation'),
    } : request.question.includes('事实核验展示') ? {
      ...fixture, answer: '界面验证：严格模式事实记录。', refs: factReviewRefs, result_set: null,
      fact_review: factReview('strict'),
    } : request.question.includes('问数核验展示') ? {
      ...fixture, answer: '界面验证：数据库执行记录。', result_set: null,
      fact_review: singleSourceReview('structured_query', 'query_execution_and_evidence', '数据库查询结果', 'query_execution'),
    } : request.question.includes('问答核验展示') ? {
      ...fixture, answer: '界面验证：文档引用记录。', refs: [factReviewRefs[1]], result_set: null,
      fact_review: singleSourceReview('document_qa', 'document_retrieval_and_citation', '文档依据与引用', 'citation_reference_linkage'),
    } : request.question.includes('PDF交互') ? {
      answer: '界面验证 PDF 引用切换 [1] [2]。', trace: [], refs: [pdfRef, { ...pdfRef, snippet: '同页另一条界面测试引用。', bboxes: [[90, 30, 140, 50]] }], sql: null, need_clarify: false,
    } : request.question.includes('文档片段') ? {
      answer: '界面验证：引用片段可查看 [1]。',
      trace: [{ step_id: 1, tool: 'doc_search', input: {}, output: {}, elapsed_ms: 50, hit: [] }],
      refs: [{ kind: 'doc', doc_id: 'ui-fixture-doc', doc_title: '界面验证文档（非业务数据）', page: null, section_path: '服务范围', char_start: 0, char_end: 20, bboxes: [], page_width: null, page_height: null, extraction_method: 'plain', snippet: '用于检查来源面板的显示和交互。' }],
      sql: null, need_clarify: false,
    } : request.question.includes('补充口径') ? { ...fixture, answer: '请补充需要查询的时间范围。', refs: [], result_set: null, need_clarify: true } : fixture
    const events = [
      { type: 'stage', id: 'fixture-stage-1', stage_id: 'fixture', label: '界面验证处理阶段', status: 'started' },
      { type: 'stage', id: 'fixture-stage-1', stage_id: 'fixture', label: '界面验证处理阶段', status: 'completed', elapsed_ms: 50 },
      { type: 'complete', response: body },
      { type: 'end' },
    ]
    const stream = events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join('')
    return route.fulfill({ status: 200, contentType: 'text/event-stream', body: stream }).catch(() => {})
  })
  await page.route('**/api/ask', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(mode === 'legacy-tool-error' ? toolErrorFixture : fixture),
  }))
  await page.route('**/api/results/ui-fixture**', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname.endsWith('export.csv')) return route.fulfill({ contentType: 'text/csv', body: 'customer_id\n1\n2\n3\n' })
    const offset = Number(url.searchParams.get('offset') || 0)
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ ...fixture.result_set, columns: ['customer_id'], rows: offset ? [{ customer_id: 3 }] : [{ customer_id: 1 }, { customer_id: 2 }], offset, next_offset: offset ? null : 2 }) })
  })
  await page.goto(base)
  await page.locator('.welcome').waitFor()
  await page.getByRole('button', { name: '选择问答模型' }).waitFor()
  await page.getByRole('button', { name: '打开数据库连接状态' }).waitFor()
  // 首页不强制弹指南；用户可从顶栏主动打开。
  assert.equal(await page.locator('.guide').count(), 0, '首次进入工作台不应被指南弹窗阻挡')
  assert.match(await page.locator('.database-entry').innerText(), /已连接 · 文档库已连接/)
  const databaseIcon = page.locator('.database-entry__icon img')
  await databaseIcon.evaluate((image) => image.decode())
  assert.equal(await databaseIcon.evaluate((image) => image.naturalWidth > 0 && image.naturalHeight > 0), true)
  await page.getByRole('button', { name: '打开数据库连接状态' }).click()
  const databaseDialog = page.getByRole('dialog', { name: '数据库概览' })
  await databaseDialog.waitFor()
  await databaseDialog.getByText('已连接', { exact: true }).first().waitFor()
  const databaseRetry = databaseDialog.getByRole('button', { name: '重新检查' })
  await databaseRetry.focus()
  await page.keyboard.press('Shift+Tab')
  const lastDatabaseControl = databaseDialog.locator('button').last()
  assert.equal(await lastDatabaseControl.evaluate((element) => element === document.activeElement), true, '数据库面板反向 Tab 应循环到末尾控件')
  await lastDatabaseControl.focus()
  await page.keyboard.press('Tab')
  assert.equal(await databaseRetry.evaluate((element) => element === document.activeElement), true, '数据库面板正向 Tab 应循环回首个控件')
  await page.screenshot({ path: path.join(outputDir, 'database-panel-1440.png') })
  assert.match(await databaseDialog.innerText(), /结构化业务库[\s\S]*已连接[\s\S]*文档向量库[\s\S]*已连接[\s\S]*跨源演示库[\s\S]*已就绪/)
  assert.equal(await databaseDialog.locator('.database-status-card').count(), 3)
  await databaseDialog.getByRole('tab', { name: /业务表/ }).click()
  await databaseDialog.locator('.database-table__toggle').first().click()
  assert.ok(await databaseDialog.locator('.database-column').count() > 1, '数据库面板应展示表字段详情')
  await databaseDialog.getByRole('tab', { name: /文档索引/ }).click()
  assert.equal(await databaseDialog.locator('.database-document').count(), 2, '数据库面板应展示文档索引清单')
  await databaseDialog.getByRole('tab', { name: /跨源演示库/ }).click()
  assert.equal(await databaseDialog.locator('.database-document').count(), 5, '数据库面板应列出隔离跨源库的五类表')
  assert.match(await databaseDialog.innerText(), /cross_source_demo\.contract_equipment[\s\S]*受控映射/)
  await page.keyboard.press('Escape')
  await databaseDialog.waitFor({ state: 'detached' })
  record('来源目录平等展示业务库、文档资料与隔离跨源库；可查看跨源表结构说明并关闭')
  assert.match(await page.getByRole('button', { name: '选择问答模型' }).innerText(), /DeepSeek-V4\.1-Flash/)
  await page.getByRole('button', { name: '选择问答模型' }).click()
  await page.getByRole('menuitemradio', { name: /GLM-4-Flash/ }).click()
  assert.match(await page.getByRole('button', { name: '选择问答模型' }).innerText(), /GLM-4-Flash/)
  record('模型菜单展示服务端可用项并切换选择，密钥不进入界面')
  assert.equal(await page.locator('.evidence').count(), 0)
  const brandMark = page.locator('.brand__mark img')
  const favicon = page.locator('link[rel="icon"]')
  assert.match(await fs.readFile(path.resolve(__dirname, '../index.html'), 'utf8'), /href="\/brand\/verimind-mark\.svg\?v=\d+"/)
  assert.match(await favicon.getAttribute('href'), /^\/brand\/verimind-mark\.svg\?v=\d+$/)
  assert.match(await brandMark.getAttribute('src'), /^(?:\/brand\/verimind-mark\.svg|data:image\/svg\+xml)/)
  await brandMark.evaluate((image) => image.decode())
  const fevaIcons = page.locator('.architecture-button img')
  assert.equal(await fevaIcons.count(), 1, 'FEVA 说明只保留在顶栏次级入口，不占据首页主操作区')
  const workspaceView = await fs.readFile(path.resolve(__dirname, '../src/views/WorkspaceView.vue'), 'utf8')
  assert.equal((workspaceView.match(/src="\/brand\/icons\/feva-architecture\.svg"/g) || []).length, 1)
  await fevaIcons.evaluateAll((images) => Promise.all(images.map((image) => image.decode())))
  assert.equal(await fevaIcons.evaluateAll((images) => images.every((image) => image.naturalWidth > 0 && image.naturalHeight > 0)), true)
  assert.equal(await page.getByRole('button', { name: '中级任务' }).count(), 0)
  assert.equal(await page.getByRole('button', { name: '系统评测' }).count(), 0)
  assert.match(await page.locator('.welcome').innerText(), /先查清事实，\s*再给可核对的答案。[\s\S]*问数[\s\S]*问答[\s\S]*跨源问题[\s\S]*当前资料范围[\s\S]*回答怎样形成/)
  assert.equal(await page.locator('.source-scope__item').count(), 3, '首页应平等展示业务数据、文档资料与跨源演示库')
  assert.match(await page.locator('.source-scope').innerText(), /跨源演示库[\s\S]*独立 PostgreSQL[\s\S]*已连接/)
  assert.equal(await page.locator('.example-card').count(), 3, '首页必须有问数、问答、跨源问题三个入口')
  assert.equal(await page.locator('.example-card--featured').count(), 1, '跨源问题入口应单独突出')
  assert.equal(await page.locator('.example-card__question').count(), 5, '三个问题类型均应展示候选例问')
  const featuredQuestionBounds = await page.locator('.example-card--featured .example-card__question').boundingBox()
  const featureTagBounds = await page.locator('.example-card--featured .example-card__feature-tag').boundingBox()
  assert.ok(featuredQuestionBounds && featureTagBounds && featureTagBounds.y >= featuredQuestionBounds.y + featuredQuestionBounds.height, '跨源重点标签不得遮挡例问按钮')
  record('首页按问题事实组织问数、问答、跨源三个入口，跨源入口突出且内部测评入口隐藏')
  record('FEVA 整体架构图标仅在顶栏次级系统说明入口加载')
  await page.getByRole('button', { name: '查看系统说明' }).click()
  await page.getByRole('dialog', { name: 'FEVA 事实—证据核验架构' }).waitFor()
  const architectureDialog = page.getByRole('dialog', { name: 'FEVA 事实—证据核验架构' })
  assert.match(await architectureDialog.locator('.architecture-dialog__intro').innerText(), /单源问数或问答仍走各自路径；当前专用跨源流程用于工单—合同场景/)
  assert.equal(await architectureDialog.locator('.feva-brief').count(), 0, '系统说明总览应保持精简，长说明放入模块详情')
  await page.getByRole('tab', { name: '模块目录' }).click()
  assert.equal(await page.locator('.architecture-module__icon img').count(), 8)
  await page.waitForFunction(() => [...document.querySelectorAll('.architecture-module__icon img')].every((image) => image.complete && image.naturalWidth > 0))
  assert.deepEqual(await page.locator('.architecture-module__icon img').evaluateAll((images) => images.filter((image) => !image.complete || image.naturalWidth === 0).map((image) => image.src)), [])
  for (const [name, concept] of [['FactFlow', 'F · 事实（Fact）'], ['EvidenceLedger', 'E · 证据（Evidence）'], ['SourceBridge', 'V · 核验（Verification）'], ['MetricAlign', 'A · 对齐（Alignment）']]) {
    const moduleCard = architectureDialog.locator('.architecture-module').filter({ hasText: name }).first()
    await moduleCard.locator('.architecture-module__toggle').click()
    assert.equal(await moduleCard.locator('.architecture-module__concept').innerText(), `FEVA · ${concept}`, `${name} 应在展开详情中解释 FEVA 概念`)
  }
  assert.match(await page.locator('.architecture-module').last().innerText(), /KAG增强路线[\s\S]*用户可选·服务未就绪/)
  await page.getByRole('button', { name: '关闭架构导览' }).click()
  record('项目主标志、浏览器图标与 8 个架构模块图标正常加载，KAG 状态文字保留')
  await page.screenshot({ path: path.join(outputDir, 'welcome-1440.png') })
  const retrievalSelect = page.getByRole('combobox', { name: '文档检索模式' })
  const deepOptionState = await retrievalSelect.locator('option[value="deep"]').evaluate((option) => ({ disabled: option.disabled, disabledAttribute: option.getAttribute('disabled'), selectDisabled: option.closest('select')?.disabled }))
  assert.equal(deepOptionState.disabled, true, 'KAG 未就绪时深度检索不得启用：' + JSON.stringify(deepOptionState))
  assert.equal(await retrievalSelect.inputValue(), 'standard', '标准检索应为默认模式')
  record('KAG 未就绪时深度检索禁用，标准检索保持默认')
  record('首页默认关闭证据面板，三个问题类型与候选例问可见')
  await checkOverflow(page, '1440 桌面首页')

  const welcomeInput = page.getByRole('textbox', { name: '输入问题' })
  const crossSourceQuestion = '工单 WO-2026-0001 是否在合同修复时限内完成？修复耗时是多少？'
  const crossSourceExample = page.getByRole('button', { name: crossSourceQuestion })
  await crossSourceExample.waitFor({ state: 'visible' })
  await crossSourceExample.click()
  assert.equal(await welcomeInput.inputValue(), crossSourceQuestion, '例问只应预填，不应自动发送')
  await welcomeInput.fill('')
  record('专用跨源演示就绪时，可预填真实工单—合同样例，不自动发送')
  crossSourceReadiness = 'disabled'
  await page.reload()
  await page.locator('.welcome').waitFor()
  const unavailableCrossSourceExample = page.getByRole('button', { name: crossSourceQuestion })
  await page.getByText('演示数据未连接', { exact: true }).waitFor()
  assert.equal(await unavailableCrossSourceExample.isDisabled(), true, '专用演示数据不可用时跨源例问必须禁用')
  const readinessCallsBeforeRetry = crossSourceReadinessCalls
  const readinessRetryResponse = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/capabilities/cross-source-demo')
  await page.getByRole('button', { name: '重新检查跨源演示数据' }).click()
  await readinessRetryResponse
  assert.equal(crossSourceReadinessCalls, readinessCallsBeforeRetry + 1, '只在用户主动重试时重新探测跨源数据')
  record('专用跨源演示未配置时保留说明但禁用例问，避免误导用户')
  crossSourceReadiness = 'ready'
  await page.reload()
  await page.locator('.welcome').waitFor()
  await page.getByText('演示链路已就绪', { exact: true }).waitFor()

  await page.getByRole('button', { name: '使用指南' }).click()
  await page.getByRole('dialog').waitFor()
  await page.getByRole('button', { name: '关闭使用指南' }).waitFor()
  await page.keyboard.press('Shift+Tab')
  assert.equal(await page.locator('.guide__confirm').evaluate((el) => el === document.activeElement), true)
  await page.keyboard.press('Tab')
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute('aria-label')), '关闭使用指南')
  await page.keyboard.press('Escape')
  await page.waitForFunction(() => !document.querySelector('.guide-backdrop'))
  assert.equal(await page.getByRole('dialog').count(), 0)
  record('使用指南可打开及 Esc 关闭')

  // 示例卡现在的行为是「把问句填进输入框」而非直接发送 —— 用户要先看一眼
  // 再决定问不问。所以这里必须显式按一次 Enter 才算提交。
  // 顺带断言填入本身有效：点完卡，输入框里应当已经有那句问句。
  await page.getByRole('button', { name: '平台上一共有多少位客户？', exact: true }).click()
  const box = page.getByRole('textbox', { name: '输入问题' })
  assert.ok((await box.inputValue()).length > 0, '点示例卡应把问句填入输入框')
  await box.press('Enter')
  await page.getByText('界面验证：共 3 条数据记录', { exact: false }).waitFor()
  const sessionTitle = page.locator('.session-context__title')
  assert.ok((await sessionTitle.textContent())?.trim(), '当前会话题目应显示在顶部导航旁')
  assert.equal(await page.locator('.session-context__count').count(), 0, '顶部不再显示提问次数')
  const breadcrumbRight = await page.locator('.breadcrumb').evaluate((element) => element.getBoundingClientRect().right)
  const sessionContextLeft = await page.locator('.session-context').evaluate((element) => element.getBoundingClientRect().left)
  assert.ok(sessionContextLeft > breadcrumbRight, '会话题目应位于“智能问答”右侧并与导航区分')
  assert.equal(await page.locator('.session-context').evaluate((element) => getComputedStyle(element).borderLeftWidth), '1px', '会话标题用细分隔线区分，不显示卡片框')
  const desktopContextWidth = await page.locator('.session-context').evaluate((element) => element.getBoundingClientRect().width)
  assert.ok(desktopContextWidth >= 260, `桌面会话题目应保留足够显示空间，当前 ${desktopContextWidth}px`)
  const dataType = page.locator('.bubble.assistant').last().locator('.answer-type')
  assert.equal(await dataType.textContent(), '数据查询')
  assert.ok((await dataType.getAttribute('class')).includes('answer-type--data'))
  const answerBubble = page.locator('.bubble.assistant').last()
  assert.equal(await answerBubble.locator('.answer-label').innerText(), '回答')
  assert.match(await answerBubble.locator('.citations header').innerText(), /回答依据/)
  assert.deepEqual(await dataType.evaluate((element) => ({ color: getComputedStyle(element).color, background: getComputedStyle(element).backgroundColor, fontSize: getComputedStyle(element).fontSize })), { color: 'rgb(255, 255, 255)', background: 'rgb(37, 75, 166)', fontSize: '14px' })
  assert.equal(seenModelProfiles[0], 'glm-4-flash', '已选模型应随该次问答请求发送给后端')
  assert.equal(seenFactReviewVersions[0], '2', '前端应显式协商事实核验响应版本')
  await page.getByRole('button', { name: '查看引用 1', exact: true }).click()
  await page.getByRole('region', { name: '来源证据面板' }).waitFor()
  await page.locator('.sql-card pre').waitFor()
  assert.equal(await page.locator('.sql-card pre').textContent(), fixture.sql)
  record('问数、行字段证据与查询语句展示')
  assert.equal(await page.locator('.trace-path').count(), 0, '折叠时不预先展示处理路径')
  await page.getByRole('button', { name: '查看处理详情' }).click()
  assert.equal(await page.locator('.trace-path').count(), 1, '展开后展示实际处理路径')
  await page.locator('.trace-path').waitFor()
  await page.locator('.trace-step__head').first().click()
  await page.locator('.trace-step__detail').waitFor()
  assert.match(await page.locator('.trace-step__detail').textContent(), /nl2sql/)
  record('处理路径与真实结构的输入输出可展开')
  await page.getByRole('button', { name: '下一页', exact: true }).click()
  await page.getByText('第 2 / 2 页').waitFor()
  const download = page.waitForEvent('download')
  await page.getByRole('button', { name: '下载 CSV' }).click()
  assert.equal((await download).suggestedFilename(), 'query-result.csv')
  record('结果分页及 CSV 导出交互')
  await page.screenshot({ path: path.join(outputDir, 'fixture-sql-1440.png') })
  await checkOverflow(page, '1440 桌面证据布局')
  await page.getByRole('button', { name: '关闭证据面板', exact: true }).click()
  assert.equal(await page.locator('.evidence').count(), 0)
  record('关闭证据后恢复完整答案区')

  const input = page.getByRole('textbox', { name: '输入问题' })
  await input.fill('事实核验展示')
  await input.press('Enter')
  const strictReview = page.locator('.fact-review').last()
  await strictReview.waitFor()
  assert.equal(await strictReview.getByText('事实已核验 · 证据完整', { exact: true }).count(), 1, '仅完整支持且引用可定位的严格事实可显示通过')
  assert.equal(await strictReview.getByText('证据支持', { exact: true }).count(), 2, '证据不全或引用索引无效不得显示严格通过')
  assert.equal(await strictReview.getByRole('button', { name: '查看customer · customer_id', exact: true }).count(), 1, '事实证据按钮须绑定有效的最终引用索引')
  assert.equal(await strictReview.getByRole('button', { name: '查看来源 9', exact: true }).count(), 0, '越界索引不能渲染成可点击来源')
  await strictReview.scrollIntoViewIfNeeded()
  await page.screenshot({ path: path.join(outputDir, 'fixture-fact-review-strict-1440.png') })
  await strictReview.getByRole('button', { name: '查看customer · customer_id', exact: true }).click()
  await page.getByRole('region', { name: '来源证据面板' }).waitFor()
  await page.getByRole('button', { name: '关闭证据面板', exact: true }).click()
  record('事实卡只对严格、支持、完整且可定位的证据显示通过；来源链接可打开')

  await input.fill('事实核验消融展示')
  await input.press('Enter')
  const ablationReview = page.locator('.fact-review').last()
  await ablationReview.waitFor()
  assert.equal(await ablationReview.getByText('消融对照模式', { exact: true }).count(), 1)
  assert.equal(await ablationReview.getByText('事实已核验 · 证据完整', { exact: true }).count(), 0, '消融模式不得显示严格核验通过')
  assert.equal(seenFactReviewVersions.at(-1), '2')
  record('消融对照事实卡明确标注模式且不展示严格通过状态')

  await input.fill('问数核验展示')
  await input.press('Enter')
  const queryReview = page.locator('.fact-review').last()
  await queryReview.waitFor()
  assert.equal(await queryReview.getByText('事实处理与证据记录', { exact: true }).count(), 1)
  assert.equal(await queryReview.getByText('来源执行记录 · 未作事实级判定', { exact: true }).count(), 1)
  assert.equal(await queryReview.getByText('尚未作事实级判定', { exact: true }).count(), 1)
  record('问数核验卡明确区分查询检查与答案语义判断')

  await input.fill('问答核验展示')
  await input.press('Enter')
  const documentReview = page.locator('.fact-review').last()
  await documentReview.waitFor()
  assert.equal(await documentReview.getByText('事实处理与证据记录', { exact: true }).count(), 1)
  assert.equal(await documentReview.getByText('来源执行记录 · 未作事实级判定', { exact: true }).count(), 1)
  assert.equal(await documentReview.getByText('尚未作事实级判定', { exact: true }).count(), 1)
  record('文档问答核验卡说明引用定位不等同于语义验证')

  await input.fill('文档片段验证')
  await input.press('Enter')
  await page.getByText('界面验证：引用片段可查看', { exact: false }).waitFor()
  const documentType = page.locator('.bubble.assistant').last().locator('.answer-type')
  assert.equal(await documentType.textContent(), '文档查询')
  assert.ok((await documentType.getAttribute('class')).includes('answer-type--document'))
  assert.deepEqual(await documentType.evaluate((element) => ({ color: getComputedStyle(element).color, background: getComputedStyle(element).backgroundColor, fontSize: getComputedStyle(element).fontSize })), { color: 'rgb(49, 91, 199)', background: 'rgb(255, 255, 255)', fontSize: '14px' })
  await page.getByRole('button', { name: '查看引用 1', exact: true }).last().click()
  await page.locator('.doc-fallback blockquote').waitFor()
  assert.match(await page.locator('.doc-fallback').textContent(), /未提供页码/)
  record('无页码文档展示片段，不伪造页码')
  await page.keyboard.press('Escape')

  await input.fill('PDF交互验证')
  await input.press('Enter')
  const latest = page.locator('.bubble.assistant').last()
  await latest.getByRole('button', { name: '查看引用 1', exact: true }).click()
  await page.locator('.pdf-page canvas').waitFor().catch(async (error) => {
    const diagnostic = await page.locator('.viewer').innerText().catch(() => '证据查看器未创建')
    console.error('PDF diagnostic:', diagnostic, errors)
    await page.screenshot({ path: path.join(outputDir, 'pdf-failure.png') })
    throw error
  })
  assert.ok(
    pdfFixtureRequests >= 1 || staticPdfRequests >= 1,
    'PDF 界面检查必须使用本地受控 PDF 夹具，不访问后端认证端点',
  )
  await latest.getByRole('button', { name: '查看引用 2', exact: true }).click()
  await page.locator('.pdf-page canvas').waitFor()
  assert.equal(await page.locator('.highlight-layer i').count(), 1)
  await page.getByRole('button', { name: '下一页文档' }).click()
  await page.getByText('第 2 页', { exact: false }).waitFor()
  await page.waitForFunction(() => !document.querySelector('.highlight-layer'))
  await page.getByRole('button', { name: '返回引用页' }).click()
  await page.locator('.pdf-page canvas').waitFor()
  await page.getByRole('button', { name: '关闭证据面板', exact: true }).click()
  await latest.getByRole('button', { name: '查看引用 1', exact: true }).click()
  await page.locator('.pdf-page canvas').waitFor()
  assert.equal(await page.getByRole('button', { name: '下一页文档' }).isEnabled(), true)
  await page.getByRole('button', { name: '关闭证据面板', exact: true }).click()
  record('PDF 同页引用切换、翻页不误高亮、缓存页数恢复')

  await input.fill('补充口径')
  await input.press('Enter')
  await page.locator('.clarify').waitFor()
  record('澄清提示可继续输入')
  await input.fill('中文输入未完成')
  const before = await page.locator('.bubble.user').count()
  await input.evaluate((element) => element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', isComposing: true, bubbles: true })))
  assert.equal(await page.locator('.bubble.user').count(), before)
  record('中文输入法选字 Enter 不误提交')

  mode = 'error'
  await input.fill('错误展示验证')
  await input.press('Enter')
  await page.getByText('本次回答未能完成。请重试；若持续失败，请检查后端与模型服务。').waitFor()
  assert.equal(await input.isEnabled(), true)
  mode = 'success'
  await page.getByRole('button', { name: '重新尝试' }).last().click()
  await page.locator('.bubble.assistant').last().getByText('界面验证：共 3 条数据记录', { exact: false }).waitFor()
  record('500 友好提示、输入恢复、重试成功')

  mode = 'tool-error'
  await input.fill('HTTP 200 工具失败展示验证')
  await input.press('Enter')
  const toolErrorBubble = page.locator('.bubble.error').last()
  await toolErrorBubble.getByText('本次查询未能完成，系统没有将工具错误当作答案。请稍后重试；若持续失败，请检查服务状态。').waitFor()
  assert.equal(await toolErrorBubble.getByText('SQL 执行失败：此错误文本不应显示为普通回答。', { exact: true }).count(), 0)
  record('HTTP 200 但 trace 标记工具失败时，显示错误状态而不把错误文本当答案')

  mode = 'legacy-tool-error'
  await input.fill('旧接口 HTTP 200 工具失败展示验证')
  await input.press('Enter')
  const legacyToolErrorBubble = page.locator('.bubble.error').last()
  await legacyToolErrorBubble.getByText('本次查询未能完成，系统没有将工具错误当作答案。请稍后重试；若持续失败，请检查服务状态。').waitFor()
  assert.equal(await legacyToolErrorBubble.getByText('SQL 执行失败：此错误文本不应显示为普通回答。', { exact: true }).count(), 0)
  record('兼容旧接口时也能从 trace 识别工具失败')
  mode = 'success'

  mode = 'legacy'
  await input.fill('旧后端兼容回退验证')
  await input.press('Enter')
  await page.locator('.bubble.assistant').last().getByText('界面验证：共 3 条数据记录', { exact: false }).waitFor()
  mode = 'success'
  record('旧后端暂未提供阶段流时，自动兼容原问答接口')

  await page.setViewportSize({ width: 1024, height: 768 })
  await page.locator('.bubble.assistant').last().getByRole('button', { name: '查看引用 1', exact: true }).click()
  await page.locator('.sql-card').waitFor()
  await checkOverflow(page, '1024 平板证据浮层')
  await page.getByRole('button', { name: '关闭证据面板', exact: true }).click()
  await page.setViewportSize({ width: 1440, height: 900 })

  mode = 'hold'
  await input.fill('停止等待验证')
  await input.press('Enter')
  await page.getByRole('button', { name: '停止等待', exact: true }).waitFor()
  await page.getByRole('button', { name: '停止等待', exact: true }).click()
  await page.getByText('已停止等待本次回答。').waitFor()
  assert.equal(await input.isEnabled(), true)
  if (release) release()
  record('停止等待释放输入区')

  await page.clock.install()
  mode = 'hold'
  await input.fill('等待超时验证')
  await input.press('Enter')
  await page.getByRole('button', { name: '停止等待', exact: true }).waitFor()
  await page.clock.fastForward(180001)
  await page.getByText('等待回答已超过 3 分钟，请稍后重试或简化问题。').waitFor()
  assert.equal(await input.isEnabled(), true)
  if (release) release()
  record('三分钟请求上限及超时恢复（浏览器时钟推进）')
  mode = 'success'
  await page.reload()
  await page.getByText('等待回答已超过 3 分钟，请稍后重试或简化问题。').waitFor()
  record('刷新后会话可恢复且没有孤立等待状态')
  await page.getByRole('button', { name: '新建问答', exact: true }).click()
  await page.locator('.welcome').waitFor()
  assert.equal(await page.locator('.evidence').count(), 0)
  await page.setViewportSize({ width: 1920, height: 1080 })
  await page.screenshot({ path: path.join(outputDir, 'welcome-1920.png') })
  await checkOverflow(page, '1920 桌面首页')
  assert.equal(await page.locator('.sidebar-toggle').count(), 1)
  assert.equal(await page.locator('.sidebar-collapse').count(), 0)
  const toggleLeftGap = await page.locator('.sidebar-toggle').evaluate((element) => element.getBoundingClientRect().left - document.querySelector('.sidebar').getBoundingClientRect().right)
  assert.ok(toggleLeftGap >= 0 && toggleLeftGap <= 26, `顶栏折页图标距侧栏边缘 ${toggleLeftGap}px，预期不超过 26px`)
  const breadcrumbGap = await page.locator('.breadcrumb').evaluate((element) => element.getBoundingClientRect().left - document.querySelector('.sidebar-toggle').getBoundingClientRect().right)
  assert.ok(breadcrumbGap >= 0 && breadcrumbGap <= 24, `面包屑距折页图标 ${breadcrumbGap}px，预期不超过 24px`)
  record('全平台只显示一个图标按钮，且靠近侧栏边缘')
  record('工作台面包屑紧邻侧栏折页图标')
  for (const width of [1920, 1024]) {
    await page.setViewportSize({ width, height: 900 })
    const sidebarToggle = page.locator('.sidebar-toggle')
    assert.equal(await sidebarToggle.getAttribute('aria-expanded'), 'true')
    await sidebarToggle.click()
    assert.equal(await sidebarToggle.getAttribute('aria-label'), '展开左侧会话栏')
    await page.waitForTimeout(240)
    const collapsedTrack = await page.locator('.workspace').evaluate((element) => getComputedStyle(element).gridTemplateColumns.split(' ')[0])
    assert.equal(collapsedTrack, '0px')
    await sidebarToggle.click()
    await page.waitForTimeout(240)
    assert.equal(await sidebarToggle.getAttribute('aria-expanded'), 'true')
    record(`${width} 桌面/平板可收起并重新展开左侧会话栏`)
  }
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({ path: path.join(outputDir, 'welcome-mobile.png') })
  await checkOverflow(page, '390 手机首页')
  const mobileWelcomeScroll = page.locator('.welcome-scroll')
  await page.locator('.example-card--featured').evaluate((element) => element.scrollIntoView({ block: 'center' }))
  await page.screenshot({ path: path.join(outputDir, 'welcome-mobile-cross-source.png') })
  const mobileFeaturedBounds = await page.locator('.example-card--featured').boundingBox()
  assert.ok(mobileFeaturedBounds && mobileFeaturedBounds.y >= 0 && mobileFeaturedBounds.y < 844, '手机端跨源重点卡应可滚动到并展示')
  await mobileWelcomeScroll.evaluate((element) => { element.scrollTop = 0 })
  await page.getByRole('button', { name: '展开左侧会话栏', exact: true }).click()
  const collapseSidebar = page.locator('.sidebar-toggle')
  assert.equal(await collapseSidebar.getAttribute('aria-label'), '收起左侧会话栏')
  await collapseSidebar.click()
  assert.equal(await page.locator('.workspace').evaluate((element) => element.classList.contains('sidebar-open')), false)
  await input.fill('文档片段验证')
  await input.press('Enter')
  await page.getByRole('button', { name: '查看引用 1', exact: true }).click()
  await page.locator('.doc-fallback').waitFor()
  const mobileContext = page.locator('.session-context')
  const mobileContextBox = await mobileContext.boundingBox()
  const mobileActionsBox = await page.locator('.topbar__actions').boundingBox()
  assert.ok(mobileContextBox.width >= 180, `手机会话题目应保留可读宽度，当前 ${mobileContextBox.width}px`)
  assert.ok(mobileActionsBox.y >= mobileContextBox.y + mobileContextBox.height, '手机操作按钮应另占一行，为会话题目留出宽度')
  await page.screenshot({ path: path.join(outputDir, 'fixture-mobile-evidence.png') })
  await checkOverflow(page, '390 手机证据浮层')
  await page.getByRole('button', { name: '关闭证据面板', exact: true }).click()
  record('手机会话栏可通过顶栏单一图标按钮收起，证据面板可关闭')
  await page.getByRole('button', { name: '展开左侧会话栏', exact: true }).click()
  await page.getByRole('button', { name: '打开数据库连接状态' }).click()
  const mobileDatabaseDialog = page.getByRole('dialog', { name: '数据库概览' })
  await mobileDatabaseDialog.getByText('已连接', { exact: true }).first().waitFor()
  assert.equal(await mobileDatabaseDialog.locator('.database-status-card').count(), 3)
  await page.waitForFunction(() => getComputedStyle(document.querySelector('.database-backdrop')).opacity === '1')
  await page.screenshot({ path: path.join(outputDir, 'database-panel-mobile.png') })
  await checkOverflow(page, '390 手机数据库面板')
  await mobileDatabaseDialog.getByRole('tab', { name: /跨源演示库/ }).click()
  assert.equal(await mobileDatabaseDialog.locator('.database-document').count(), 5, '手机来源目录应能查看五类跨源表')
  await page.keyboard.press('Escape')
  await page.getByRole('dialog', { name: '数据库概览' }).waitFor({ state: 'detached' })
  await page.locator('.sidebar-toggle').click()
  record('手机来源目录平等展示三类来源并可查看跨源表，无横向溢出')

  await page.setViewportSize({ width: 1440, height: 900 })
  await page.emulateMedia({ reducedMotion: 'no-preference' })
  const shortSessionId = 'ui-scroll-short-session'
  const longSessionId = 'ui-scroll-long-session'
  const testSessions = [
    {
      id: shortSessionId, title: '短会话滚动检查', updatedAt: '2026-10-12T10:00:00.000Z',
      messages: [
        { id: 'short-user', role: 'user', content: '短问题', createdAt: '2026-10-12T09:59:00.000Z' },
        { id: 'short-answer', role: 'assistant', content: '短回答', createdAt: '2026-10-12T09:59:30.000Z' },
      ],
    },
    {
      id: longSessionId, title: '长会话滚动检查', updatedAt: '2026-10-11T10:00:00.000Z',
      messages: Array.from({ length: 24 }, (_, index) => ({
        id: `long-message-${index}`, role: index % 2 === 0 ? 'user' : 'assistant',
        content: `历史消息 ${index + 1}：用于验证切换会话后直接定位到最新内容，而不是先从第一条开始播放滚动。`,
        createdAt: new Date(Date.UTC(2026, 9, 11, 9, index)).toISOString(),
      })),
    },
  ]
  await page.evaluate(({ sessions, activeId }) => {
    localStorage.setItem('kb.sessions:user-ui-check-admin', JSON.stringify(sessions))
    localStorage.setItem('kb.session_id:user-ui-check-admin', activeId)
  }, { sessions: testSessions, activeId: shortSessionId })
  await page.reload()
  await page.getByRole('heading', { name: '短会话滚动检查' }).waitFor()
  await page.evaluate(() => { window.__threadScrollCalls = [] })
  await page.locator('.session-select').filter({ hasText: '长会话滚动检查' }).click()
  await page.getByRole('heading', { name: '长会话滚动检查' }).waitFor()
  await page.waitForFunction(() => window.__threadScrollCalls.length > 0)
  const sessionScroll = await page.evaluate(() => {
    const thread = document.querySelector('.thread')
    return {
      behavior: window.__threadScrollCalls.at(-1).behavior,
      top: thread.scrollTop,
      maxTop: thread.scrollHeight - thread.clientHeight,
    }
  })
  assert.equal(sessionScroll.behavior, 'auto', '切换会话应直接定位，不播放平滑滚动')
  assert.ok(Math.abs(sessionScroll.top - sessionScroll.maxTop) <= 1, '切换会话后应立即显示最新消息')
  record('切换到长历史会话时无顶部到末尾的滚动过程')

  await page.getByRole('button', { name: '设置', exact: true }).click()
  const settings = page.getByRole('dialog', { name: '设置' })
  await settings.waitFor()
  const modelConfigSection = settings.getByRole('region', { name: 'API 与模型配置' })
  await modelConfigSection.waitFor()
  assert.equal(await modelConfigSection.locator('.settings-model').count(), 3, '设置页应按服务端模型注册表动态生成配置卡片')
  assert.equal(await modelConfigSection.locator('.settings-model[data-profile-id="future-model-v9"]').count(), 1, '新增注册模型无需改动设置页组件')
  const glmConfig = modelConfigSection.locator('.settings-model[data-profile-id="glm-4-flash"]')
  await glmConfig.getByLabel('API 地址').fill('https://glm.example/v4')
  await glmConfig.getByLabel('模型 ID').fill('glm-4-flash-ui-test')
  await glmConfig.getByLabel('API 密钥').fill('ui-secret-must-not-persist')
  await glmConfig.getByRole('button', { name: '保存配置' }).click()
  await glmConfig.getByText('配置已保存，可在顶栏模型菜单中选择。').waitFor()
  assert.deepEqual(modelConfigWrites.at(-1), {
    profileId: 'glm-4-flash',
    payload: { model: 'glm-4-flash-ui-test', base_url: 'https://glm.example/v4', api_key: 'ui-secret-must-not-persist' },
  })
  assert.equal(await glmConfig.getByLabel('API 密钥').inputValue(), '', '保存成功后应清空密钥输入框')
  assert.equal(await page.evaluate(() => Array.from({ length: localStorage.length }, (_, index) => localStorage.getItem(localStorage.key(index))).join('\n').includes('ui-secret-must-not-persist')), false, 'API 密钥不能保存在浏览器本地存储')
  record('设置页可保存 API 地址、模型 ID 与密钥，密钥保存后清空且不写入浏览器存储')
  await page.getByRole('button', { name: '清空对话和本地设置' }).click()
  const clearConfirm = settings.getByRole('alertdialog', { name: '确认清空本机数据？' })
  await clearConfirm.waitFor()
  const confirmBounds = await clearConfirm.evaluate((element) => {
    const rect = element.getBoundingClientRect()
    const bodyRect = element.closest('.settings__body').getBoundingClientRect()
    return { top: rect.top, bottom: rect.bottom, bodyTop: bodyRect.top, bodyBottom: bodyRect.bottom }
  })
  assert.ok(confirmBounds.top >= confirmBounds.bodyTop - 1 && confirmBounds.bottom <= confirmBounds.bodyBottom + 1, '清空确认卡片应自动滚动到完整可见区域')
  assert.equal(await clearConfirm.getByRole('button', { name: '取消' }).evaluate((element) => element === document.activeElement), true, '焦点应落在安全的取消按钮')
  await settings.getByRole('button', { name: '取消', exact: true }).click()
  assert.equal(await page.evaluate(() => JSON.parse(localStorage.getItem('kb.sessions:user-ui-check-admin') || '[]').length), 2, '取消确认后会话应保留')
  await settings.getByRole('button', { name: '清空对话和本地设置' }).click()
  await settings.getByRole('button', { name: '确认清空', exact: true }).click()
  await settings.getByText('已清空对话和本地设置', { exact: true }).waitFor()
  await settings.getByRole('button', { name: '完成并关闭' }).click()
  await settings.waitFor({ state: 'detached' })
  const clearedState = await page.evaluate(() => ({
    sessions: JSON.parse(localStorage.getItem('kb.sessions:user-ui-check-admin') || '[]'),
    activeId: localStorage.getItem('kb.session_id:user-ui-check-admin'),
    model: localStorage.getItem('verimind.modelProfileId:user-ui-check-admin'),
    theme: localStorage.getItem('askdata-theme'),
    appliedTheme: document.documentElement.dataset.theme,
    systemTheme: matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light',
  }))
  assert.equal(clearedState.sessions.length, 1, '清理后只保留一个空白会话供继续提问')
  assert.equal(clearedState.sessions[0].messages.length, 0)
  assert.equal(clearedState.activeId, clearedState.sessions[0].id)
  assert.equal(clearedState.model, null)
  assert.equal(clearedState.theme, 'system')
  assert.equal(clearedState.appliedTheme, clearedState.systemTheme)
  assert.match(await page.getByRole('button', { name: '选择问答模型' }).innerText(), /DeepSeek-V4\.1-Flash/)
  record('设置可取消或确认清空会话，并恢复本地模型选择与主题默认值')
  // —— 使用指南由用户主动打开，顺序引导不阻挡首次提问 ——
  const beforeGuideUserCount = await page.locator('.bubble.user').count()
  await page.getByRole('button', { name: '使用指南' }).click()
  await page.locator('.guide').waitFor()
  assert.match(await page.locator('.guide__eyebrow').innerText(), /第 1 步/)
  assert.match(await page.locator('.guide h2').innerText(), /能答什么/)
  await page.locator('.guide__examples').getByText(/工单 WO-2026-0001/).waitFor()
  await page.getByRole('button', { name: '下一步' }).click()
  assert.match(await page.locator('.guide h2').innerText(), /怎么看答案/)
  assert.match(await page.locator('.guide__pane').innerText(), /Beta 测试路径[\s\S]*稳定性与效率尚未充分验证/)
  await page.getByRole('tab', { name: '第 1 步：能答什么' }).click()
  assert.match(await page.locator('.guide h2').innerText(), /能答什么/)
  record('使用指南由顶栏主动打开，步骤可前进并查看前序内容')
  const exampleText = (await page.locator('.guide__examples button').first().innerText()).trim()
  await page.locator('.guide__examples button').first().click()
  await page.locator('.guide').waitFor({ state: 'detached' })
  assert.equal(await page.getByRole('textbox', { name: '输入问题' }).inputValue(), exampleText, '试一试填入输入框')
  assert.equal(await page.locator('.bubble.user').count(), beforeGuideUserCount, '指南示例只填入、不自动发送')
  record('指南示例填入输入框且不自动发送')
  await page.reload()
  await page.locator('.welcome').waitFor()
  assert.equal(await page.locator('.guide').count(), 0, '刷新后仍直接进入工作台，不强制弹出指南')
  await page.getByRole('button', { name: '设置' }).click()
  await page.getByRole('dialog', { name: '设置' }).waitFor()
  assert.equal(await page.getByText(/每次打开网页时自动弹出使用指南/).count(), 0, '设置不再展示已移除的自动弹出开关')
  await page.locator('.settings .close-button').click()
  await page.locator('.settings').waitFor({ state: 'detached' })
  record('使用指南设置保持简洁，自动弹出选项已移除')
  kagReadinessReady = true
  await page.reload()
  await page.locator('.welcome').waitFor()
  const deepSelect = page.getByRole('combobox', { name: '文档检索模式' })
  assert.equal(await deepSelect.locator('option[value="deep"]').innerText(), 'KAG 补充深度检索', '检索选项应明确说明 KAG 是补充路线')
  await deepSelect.selectOption('deep')
  const betaDialog = page.getByRole('dialog', { name: '启用 KAG 补充深度检索？' })
  await betaDialog.waitFor()
  assert.match(await betaDialog.innerText(), /稳定性与效率尚未充分验证/)
  assert.equal(await deepSelect.inputValue(), 'standard', '确认前必须保留标准检索')
  await betaDialog.getByRole('button', { name: '继续标准检索' }).click()
  await betaDialog.waitFor({ state: 'detached' })
  assert.equal(await deepSelect.inputValue(), 'standard', '取消 Beta 提示后仍使用标准检索')
  await deepSelect.selectOption('deep')
  await betaDialog.waitFor()
  await betaDialog.getByRole('button', { name: '确认使用 KAG 补充检索' }).click()
  await betaDialog.waitFor({ state: 'detached' })
  assert.equal(await deepSelect.inputValue(), 'deep', '明确确认后才切换到深度检索')
  const deepRequest = page.waitForRequest((request) => {
    if (!request.url().endsWith('/api/ask/stream')) return false
    try { return request.postDataJSON().retrieval_mode === 'deep' } catch { return false }
  })
  await page.getByRole('textbox', { name: '输入问题' }).fill('深度检索模式请求验证')
  await page.getByRole('button', { name: '发送问题' }).click()
  await deepRequest
  assert.equal(seenRetrievalModes.at(-1), 'deep', '用户选择的深度检索必须进入问答请求')
  record('KAG Beta 提示可取消或确认；仅确认后请求才携带 retrieval_mode=deep')
  await page.getByRole('textbox', { name: '输入问题' }).fill('进度自动滚动验证')
  await page.getByRole('button', { name: '发送问题' }).click()
  const liveTimeline = page.locator('.progress-timeline')
  await liveTimeline.waitFor()
  await page.waitForFunction(() => document.querySelectorAll('.progress-stage').length >= 12)
  await page.waitForFunction(() => {
    const element = document.querySelector('.progress-timeline')
    return element && element.scrollTop >= element.scrollHeight - element.clientHeight - 3
  }, undefined, { timeout: 1800 })
  const timelineScroll = await liveTimeline.evaluate((element) => ({ top: element.scrollTop, max: element.scrollHeight - element.clientHeight }))
  assert.ok(timelineScroll.max > 0, '长检索流程应产生可滚动时间线')
  assert.ok(timelineScroll.top >= timelineScroll.max - 3, '新增处理阶段后时间线应跟随到底部：' + JSON.stringify(timelineScroll))
  const threadScroll = await page.locator('.thread').evaluate((element) => ({ top: element.scrollTop, max: element.scrollHeight - element.clientHeight }))
  assert.ok(threadScroll.top >= threadScroll.max - 3, '新增处理阶段后对话区应跟随最新内容')
  await page.locator('.thinking__head').waitFor({ state: 'detached' })
  record('长检索阶段自动跟随到最新步骤，且尊重减少动态效果设置')
  await page.getByRole('textbox', { name: '输入问题' }).fill('标题状态隐藏验证')
  await page.getByRole('button', { name: '发送问题' }).click()
  await page.locator('.thinking__head').waitFor()
  assert.equal(await page.locator('.working-state').count(), 0, '处理中状态只在回答卡内展示，不重复占用标题行')
  await page.locator('.thinking__head').waitFor({ state: 'detached' })
  record('请求处理中时标题行不重复显示状态')
  assert.deepEqual(errors, [])
  record('受控界面检查无浏览器运行错误')
  await context.close()
}

async function live(browser) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' })
  const page = await context.newPage()
  const errors = []
  page.on('pageerror', (error) => errors.push(error.message))
  // 预置"已看过使用指南"标记：live 跑的是真后端，首次指南弹窗会挡住发送按钮，
  // 让 sent 前的 click 一直重试到超时。受控套件在 addInitScript 里也做了同样的事。
  await page.addInitScript(() => localStorage.setItem('askdata-guide-seen', '1'))
  await page.goto(base)
  // 后端启用了登录门，首屏是登录页；没有这一步会一直等 .welcome 到超时。
  // 凭据从环境变量读，**不写进仓库**。
  if (await page.getByRole('heading', { name: '登录知证' }).count()) {
    const username = process.env.UI_LIVE_USERNAME
    const password = process.env.UI_LIVE_PASSWORD
    if (!username || !password) {
      throw new Error('live 模式需要登录，但未提供凭据。请设置 UI_LIVE_USERNAME / UI_LIVE_PASSWORD 环境变量后重跑。')
    }
    await page.getByLabel('用户名').fill(username)
    await page.locator('input[name="password"]').fill(password)
    await page.getByRole('button', { name: '安全登录' }).click()
  }
  await page.locator('.welcome').waitFor()
  const results = []
  for (const [kind, question] of [
    ['doc', '智能计算设备年度维保服务合同里，质保期是多久？'],
    ['sql', '平台上一共有多少位客户？'],
  ]) {
    await page.getByRole('textbox', { name: '输入问题' }).fill(question)
    // 先 await response，再读 body —— 中间一旦有点击/导航，Playwright 会报
    // "Response body is not available for a response that was navigated away from"。
    // 这里把 body 的读取紧跟在 response 之后，不插入任何交互。
    const responsePromise = page.waitForResponse((response) => response.url().endsWith('/api/ask/stream'), { timeout: 190000 })
    await page.getByRole('button', { name: '发送问题' }).click()
    const response = await responsePromise
    const status = response.status()
    const raw = await response.text()
    const completeFrame = raw.split(/\r?\n\r?\n/).find((frame) => frame.includes('"type":"complete"'))
    assert.ok(completeFrame, '流式问答应返回最终回答事件')
    const body = JSON.parse(completeFrame.split(/\r?\n/).find((line) => line.startsWith('data:')).slice(5).trim()).response
    const info = { kind, question, status, answer: body.answer, refs: body.refs?.length ?? 0, tools: body.trace?.map((step) => step.tool) ?? [], suggestions: body.suggestions?.map((item) => item.question) ?? [], result_set: body.result_set ?? null }
    results.push(info)
    console.log(JSON.stringify(info))
    assert.equal(status, 200)
    await page.locator('.bubble.assistant').last().locator('.citation').first().waitFor()
    await page.locator('.bubble.assistant').last().locator('.citation').first().click()
    if (kind === 'doc') {
      if (body.refs[0].page) {
        await page.locator('.pdf-page canvas').waitFor({ timeout: 30000 })
        const size = await page.locator('.pdf-page').evaluate((element) => ({ width: element.getBoundingClientRect().width, parent: element.parentElement.clientWidth }))
        assert.ok(size.width <= size.parent, 'PDF 默认适宽')
        record('真实文档回答、PDF 原文及适宽显示')
        if (body.refs[0].bboxes?.length) {
          assert.ok(await page.locator('.pdf-page .highlight-layer i').count())
          record('真实坐标引用高亮可见')
        }
      } else await page.locator('.doc-fallback').waitFor()
    } else {
      await page.locator('.sql-card pre').waitFor()
      assert.equal(await page.locator('.sql-card pre').textContent(), body.sql)
      if (body.result_set) await page.locator('.result-set tbody td').first().waitFor()
      record('真实数据库回答、查询语句及数据证据')
    }
    await page.screenshot({ path: path.join(outputDir, 'live-' + kind + '-1440.png') })
    await checkOverflow(page, '真实 ' + kind + ' 答案布局')
    await page.getByRole('button', { name: '关闭证据面板', exact: true }).click()
  }
  await fs.writeFile(path.join(outputDir, 'live-results.json'), JSON.stringify({ results, errors }, null, 2))
  assert.deepEqual(errors, [])
  record('真实两条链路无浏览器运行错误')
  await context.close()
}

async function verifyLoginGate(browser) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } })
  const page = await context.newPage()
  let modelConfigRequests = 0
  page.on('request', (request) => {
    if (new URL(request.url()).pathname.startsWith('/api/model-config')) modelConfigRequests += 1
  })
  await page.route('**/api/auth/me', (route) => route.fulfill({ status: 401, contentType: 'application/json', body: '{"detail":"请先登录"}' }))
  await page.route('**/api/auth/login', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ user: { id: 'ui-check-user', username: 'ui-check-user', role: 'user', active: true, created_at: '2026-10-14T00:00:00Z' } }),
  }))
  await page.route('**/health', (route) => route.fulfill({ contentType: 'application/json', body: '{"status":"ok"}' }))
  await page.route('**/api/models', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ default_model_id: 'deepseek-v4-1-flash', models: [{ id: 'deepseek-v4-1-flash', label: 'DeepSeek-V4.1-Flash', provider: '测试', model: 'fixture', configured: true, selected: true }] }),
  }))
  await page.route('**/api/database/status', (route) => route.fulfill({ contentType: 'application/json', body: '{"status":"disconnected","database":null,"catalog_database":null,"label":"测试数据源","dialect":"postgres","table_count":0,"checked_at":"2026-10-14T00:00:00Z"}' }))
  await page.route('**/api/database/unstructured/status', (route) => route.fulfill({ contentType: 'application/json', body: '{"status":"empty","backend":"none","label":"测试文档源","checked_at":"2026-10-14T00:00:00Z"}' }))
  await page.route('**/api/capabilities/cross-source-demo', (route) => route.fulfill({ contentType: 'application/json', body: '{"status":"disabled"}' }))

  await page.goto(base)
  await page.getByRole('heading', { name: '登录知证' }).waitFor()
  await page.getByLabel('用户名').fill('ui-check-user')
  await page.locator('input[name="password"]').fill('fixture-password')
  await page.getByRole('button', { name: '安全登录' }).click()
  await page.locator('.welcome').waitFor()
  await page.getByRole('button', { name: '设置', exact: true }).click()
  await page.getByRole('dialog', { name: '设置' }).waitFor()
  assert.equal(await page.getByRole('region', { name: 'API 与模型配置' }).count(), 0)
  assert.match(await page.getByRole('region', { name: '模型配置权限' }).innerText(), /管理员统一维护/)
  assert.equal(modelConfigRequests, 0, '普通账号不得请求管理员模型配置接口')
  await context.close()
  console.log('PASS 登录界面与普通账号权限展示')
}

;(async () => {
  await fs.mkdir(outputDir, { recursive: true })
  const browser = await chromium.launch({ channel: process.env.UI_BROWSER_CHANNEL || (process.platform === 'win32' ? 'msedge' : undefined), headless: true })
  try {
    if (process.argv.includes('--live')) await live(browser)
    else {
      await controlled(browser)
      await verifyLoginGate(browser)
    }
    await fs.writeFile(reportPath, JSON.stringify({ status: 'passed', generated_at: new Date().toISOString(), target: base, scope, checks }, null, 2))
  } finally { await browser.close() }
})().catch(async (error) => {
  await fs.mkdir(outputDir, { recursive: true })
  await fs.writeFile(reportPath, JSON.stringify({ status: 'failed', generated_at: new Date().toISOString(), target: base, scope, checks, error: error.message }, null, 2))
  console.error(error)
  process.exitCode = 1
})
