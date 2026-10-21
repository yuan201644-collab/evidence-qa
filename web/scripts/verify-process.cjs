/**
 * 处理过程展示的专项验证：等待期实时、答案后只留一处。
 *
 * 为什么需要这个脚本：verify-workspace.cjs 检查的是「界面能用」。过程这块曾经
 * 出过两类它抓不到的问题：
 * 1. 答案到达时整块过程被销毁（`progressByMessage` 被 finally 无条件删除），
 *    但从截图上完全看不出 —— 有过程和没过程都"正常"。
 * 2. 反过来，过程在答案上下各显示一遍，截图里两处都好看，只有数元素个数才发现重复。
 *
 * 所以这个脚本直接数 DOM 元素与量高度，把主观感受变成可断言的数字。
 * 当前契约：等待中显示完整时间线；答案到达后上方整块移除，处理过程只在下方的
 * 「查看处理详情」里呈现（它带每步输入/输出，是可解释性评分的硬证据）。
 *
 * 协议：POST /api/ask/stream，帧格式 `data: {json}\n\n`，
 * 事件类型 stage / complete / end（与 web/scripts/verify-motion.cjs 一致）。
 *
 * 为什么用 addInitScript 覆写 fetch 而不是 page.route：
 * Playwright 的 route.fulfill 传 ReadableStream 作 body 时会等流结束才把响应交给页面，
 * 阶段帧全部被缓冲，测不到「等待中」这个中间态（实测 window.__frames 为空数组）。
 * verify-motion.cjs 用的就是 fetch 级覆写，这里沿用同一套已验证的做法。
 */
const { chromium } = require('playwright')
const path = require('path')
const fs = require('fs')

const base = process.env.UI_BASE_URL || 'http://127.0.0.1:5184'
const out = process.env.UI_OUTPUT_DIR || path.join(__dirname, '..', '..', 'agent_temp', 'process-check')
fs.mkdirSync(out, { recursive: true })

/*
 * 阶段事件按**真实抓取的顺序**排列，含嵌套的外层/内层关系。
 *
 * 为什么不用一组扁平样例：这次要护住的恰恰是嵌套。
 * 下面的顺序来自 `agent_temp/sse-raw.log`（一次真实文档问答的 SSE 抓包）：
 * 外层 `tool_doc_search_1` 先 started，内层的检索/重排/生成在它内部开合，
 * 最后外层才 completed。这个顺序会让界面同时出现两个「进行中」。
 */
const STAGES = [
  ['s1', 'understand', '识别问题要素', 120],
  ['s2', 'route', '选择查询与检索路径', 90],
  ['s3', 'tool_doc_search_1', '检索并核对文档', 9800],
  ['s4', 'rag_retrieve', '检索相关文档片段', 83],
  ['s5', 'rag_rerank', '排序并筛选证据片段', 4700],
  ['s6', 'rag_generate', '依据证据生成回答', 3600],
  ['s7', 'assemble', '关联证据并整理回答', 210],
]

/** 停在「最后一个内层子步骤开转」那一刻：此时 s3 与 s6 都在进行中。 */
const NESTED_HOLD = 's6'

/** 嵌套时序：[id, stage_id, label, status, elapsed?]。s3 在 s4..s6 之后才收尾。 */
const NESTED_SEQUENCE = [
  ['s1', 'understand', '识别问题要素', 'started'],
  ['s1', 'understand', '识别问题要素', 'completed', 120],
  ['s2', 'route', '选择查询与检索路径', 'started'],
  ['s2', 'route', '选择查询与检索路径', 'completed', 90],
  ['s3', 'tool_doc_search_1', '检索并核对文档', 'started'],
  ['s4', 'rag_retrieve', '检索相关文档片段', 'started'],
  ['s4', 'rag_retrieve', '检索相关文档片段', 'completed', 83],
  ['s5', 'rag_rerank', '排序并筛选证据片段', 'started'],
  ['s5', 'rag_rerank', '排序并筛选证据片段', 'completed', 4700],
  ['s6', 'rag_generate', '依据证据生成回答', 'started'],
  ['s6', 'rag_generate', '依据证据生成回答', 'completed', 3600],
  ['s3', 'tool_doc_search_1', '检索并核对文档', 'completed', 9800],
  ['s7', 'assemble', '关联证据并整理回答', 'started'],
  ['s7', 'assemble', '关联证据并整理回答', 'completed', 210],
]

const RESPONSE = {
  answer: '根据文档片段，质保期限为：自设备验收合格之日起 36 个月。',
  refs: [],
  sql: null,
  need_clarify: false,
  suggestions: [],
  trace: [{ step_id: 1, tool: 'llm_generate', input: {}, output: { text: 'x' }, elapsed_ms: 12, hit: [] }],
}

/*
 * 接续提示的夹具：第 3 轮，且 `previous_question` 是**累积文本**（带接缝换行）。
 *
 * 为什么特意带两行：后端送的 `previous_question` 是到上一轮为止的完整累积串
 * （`app/memory.py:155 _combine` 用 `\n用户补充或修改：` 拼接，`app/orchestrator.py:117`
 * 按接缝数算 round_index）。原样铺到界面上是一大段带换行的文本，反而看不懂
 * 「接着哪一句」——前端 `continuationBrief` 只取首行并折算补充轮数。
 * 夹具必须还原这个形状，否则测不到那段折叠逻辑。
 */
const CONTINUATION_RESPONSE = {
  ...RESPONSE,
  context_continuation: {
    previous_question: '平台上一共有多少位客户？\n用户补充或修改：只看美国的',
    round_index: 3,
  },
}

/** 首轮不接续：后端此时不返回 `context_continuation`，界面不该出现任何接续提示。 */
const FIRST_TURN_RESPONSE = { ...RESPONSE }

async function run() {
  const browser = await chromium.launch({ channel: process.env.UI_BROWSER_CHANNEL || 'msedge', headless: true })
  const report = { target: base, checks: [], failures: [] }
  const record = (name, ok, detail) => {
    report.checks.push({ name, ok, detail })
    if (!ok) report.failures.push(name)
    console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? ' — ' + detail : ''}`)
  }

  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  const page = await context.newPage()
  await page.route('**/api/auth/me', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ user: { id: 'ui-check-user', username: 'ui-check-user', role: 'user', active: true, created_at: '2026-10-14T00:00:00Z' } }),
  }))
  const errors = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.addInitScript(({ sequence, holdAfter, response, firstResponse, continuationResponse }) => {
    window.__releaseAnswer = null
    const gate = new Promise((resolve) => { window.__releaseAnswer = resolve })
    const nativeFetch = window.fetch.bind(window)
    /*
     * 第几次问答。第一次答首轮（无接续），之后答「第 3 轮且有累积上下文」，
     * 以便在同一个页面里同时断言「首轮不显示」与「接续显示」两种形态。
     */
    let turn = 0
    window.fetch = (input, init) => {
      const url = new URL(typeof input === 'string' ? input : input.url, location.href)
      if (url.pathname !== '/api/ask/stream') return nativeFetch(input, init)
      turn += 1
      const payload = turn === 1 ? firstResponse : continuationResponse
      const encoder = new TextEncoder()
      const body = new ReadableStream({
        async start(controller) {
          const send = (event) => controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`))
          for (const [id, stage_id, label, status, elapsed_ms] of sequence) {
            send({ type: 'stage', id, stage_id, label, status, elapsed_ms })
            /*
             * 停在「内层子步骤刚开转」那一刻，才能看到外层与内层同时「进行中」。
             *
             * 这正是用户报的现象（截图里「检索并核对文档」与「依据证据生成回答」
             * 两行都在转）。如果一口气把整个序列发完，页面直接跳到完成态，
             * 嵌套就没被真正观测到——脚本会变成永远 PASS 的摆设。
             */
            if (id === holdAfter && status === 'started') await gate
          }
          send({ type: 'complete', response: payload })
          send({ type: 'end' })
          controller.close()
        },
      })
      return Promise.resolve(new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } }))
    }
  }, { sequence: NESTED_SEQUENCE, holdAfter: NESTED_HOLD, response: RESPONSE, firstResponse: FIRST_TURN_RESPONSE, continuationResponse: CONTINUATION_RESPONSE })
  await page.route('**/health', (route) => route.fulfill({ contentType: 'application/json', body: '{"status":"ok"}' }))
  await page.route('**/api/capabilities/cross-source-demo', (route) => route.fulfill({ contentType: 'application/json', body: '{"status":"unavailable"}' }))
  await page.route('**/api/models', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ default_model_id: 'deepseek-v4-1-flash', models: [{ id: 'deepseek-v4-1-flash', label: 'DeepSeek-V4.1-Flash', provider: '火山方舟', model: 'deepseek-v4.1-flash', configured: true, selected: true }] }),
  }))

  try {
    await page.goto(base)
    await page.locator('.welcome').waitFor()
    const input = page.locator('.composer textarea')
    await input.fill('收起行为验证')
    await input.press('Enter')

    // 等待中：完整时间线应在（这是"系统在干活"的唯一可见信号，不能没有）
    await page.locator('.thinking .progress-timeline').waitFor({ timeout: 15000 })
    /*
     * 等「那一刻」真的渲染出来再断言/截图。
     *
     * 事件是流式推的，`progress-timeline` 一出现就断言会抓到只发了前两条的样子；
     * 要等内层子步骤那一行出现在 DOM 里（s6 开转、s3 仍在进行中），
     * 否则截图拍的是开头，看到的仍是"单独一件事在转"，白测。
     */
    await page.locator('.thinking .progress-stage--nested').first().waitFor({ timeout: 15000 })
    await page.waitForTimeout(300)
    const pendingStages = await page.locator('.thinking .progress-stage').count()
    record('等待中显示完整时间线', pendingStages >= 1, `${pendingStages} 个阶段节点`)
    await page.screenshot({ path: path.join(out, 'process-pending.png') })

    /*
     * 嵌套：这一刻外层「检索并核对文档」与内层「依据证据生成回答」同时在转。
     *
     * 这是用户报的现象（截图里两行都显示「进行中」，看着像两件事并行赛跑）。
     * 数据上两处都对，是**嵌套**——外层大步骤要等子步骤做完才结束。
     * 平铺渲染会让人误读，所以要断言缩进层级确实不同。
     */
    const liveDepths = await page.locator('.thinking .progress-stage').evaluateAll(
      (nodes) => nodes.map((node) => ({
        label: (node.querySelector('.progress-stage__label')?.textContent || '').trim(),
        status: (node.querySelector('.progress-stage__status')?.textContent || '').trim(),
        depth: Number(getComputedStyle(node).getPropertyValue('--stage-depth')) || 0,
      })),
    )
    const running = liveDepths.filter((item) => item.status.includes('进行中'))
    record('那一刻确有多个阶段同时进行中', running.length >= 2, `进行中 ${running.length} 个：${running.map((i) => i.label).join('、')}`)

    const depths = new Set(running.map((item) => item.depth))
    record(
      '同时进行中的阶段分属不同层级（嵌套而非并发）',
      running.length >= 2 && depths.size >= 2,
      `进行中层级 ${[...depths].join(' / ')}，分别为 ${running.map((i) => `d${i.depth} ${i.label}`).join('，')}`,
    )

    const nestedIndented = await page.locator('.thinking .progress-stage--nested').first().evaluate(
      (node) => Number(getComputedStyle(node).getPropertyValue('--stage-depth')),
    )
    record('内层阶段带缩进标记', nestedIndented > 0, `内层 --stage-depth = ${nestedIndented}`)

    const hint = (await page.locator('.thinking__note').innerText()).replace(/\s+/g, ' ')
    record('底部文案解释了嵌套', /子步骤/.test(hint) && /同时/.test(hint), hint.slice(0, 80))
    await page.screenshot({ path: path.join(out, 'process-nested.png') })

    await page.evaluate(() => window.__releaseAnswer())
    await page.locator('.vm-turn .content').waitFor({ timeout: 20000 })
    await page.waitForTimeout(600)

    /*
     * 答案到达后，上方过程区块必须消失。
     *
     * 为什么是「必须消失」而不是「收起」：`progress`（SSE 实时阶段）与 `trace`
     * （后端最终埋点的工具链）讲的是同一件事，前者只是后者的粗粒度视图。
     * 两处都留 = 同一份过程看两遍。保留 `trace` 那一份，因为每步的输入/输出
     * 只有它有，那是可解释性评分的硬证据。
     */
    const processExists = await page.locator('.bubble.assistant .process').count()
    record('答案到达后上方过程区块已移除', processExists === 0, `.process 元素 ${processExists} 个（期望 0）`)

    const liveStages = await page.locator('.bubble.assistant .progress-timeline').count()
    record('答案态不再渲染时间线', liveStages === 0, `答案态时间线 ${liveStages} 个（期望 0）`)

    /*
     * 首轮不接续：后端此时不返回 `context_continuation`，界面不该凭空长出一条
     * 「已接续第 N 问」。这是防止把「接续提示」写成无条件渲染的负向断言——
     * 只测「接续时会出现」的话，一个永远显示的角标也能通过。
     */
    const firstTurnBadge = await page.locator('.bubble.assistant .answer-continuation').count()
    record('首轮不显示接续提示', firstTurnBadge === 0, `.answer-continuation ${firstTurnBadge} 个（期望 0）`)


    // 处理过程仍可查：入口是答案下方默认折叠的「查看处理详情」。
    const drawer = page.locator('.bubble.assistant .trace__toggle')
    await drawer.waitFor({ timeout: 10000 })
    const drawerText = (await drawer.innerText()).replace(/\s+/g, ' ')
    record('答案下方保留唯一的处理详情入口', /查看处理详情/.test(drawerText) && /步/.test(drawerText), drawerText)
    record('默认折叠不展示处理路径', await page.locator('.trace-path').count() === 0, 'trace-path 应随详情一并折叠')
    await page.screenshot({ path: path.join(out, 'process-answer.png') })

    await drawer.click()
    await page.waitForTimeout(400)
    const steps = await page.locator('.bubble.assistant .trace-step').count()
    record('处理记录可展开到每一步', steps >= 1, `展开后 ${steps} 步`)
    record('展开详情后展示实际处理路径', await page.locator('.trace-path').count() === 1, `展开后 ${await page.locator('.trace-path').count()} 条路径`)
    await page.screenshot({ path: path.join(out, 'process-trace-open.png') })

    // 键盘可达
    record('处理记录入口可键盘聚焦', await drawer.evaluate((el) => el.tagName.toLowerCase()) === 'button')

    /*
     * 多轮接续提示：第二问起，答案顶部要标出「接着哪一句问的」。
     *
     * 为什么要显示：赛题满分档是「连续 5 轮以上准确」（初赛评分表 L379-382，
     * 具备多轮能力得 3 分，连续 5 轮以上得 5 分）。后端确实把上下文拼进去了，
     * 但那条接缝只存在于送去模型的字符串里，界面上看不出来——评委连点几问
     * 也判断不了这几轮到底接没接上。
     */
    await input.fill('改成按城市分组')
    await input.press('Enter')
    await page.locator('.bubble.assistant .answer-continuation').last().waitFor({ timeout: 20000 })

    const badges = await page.locator('.bubble.assistant .answer-continuation').count()
    record('第二轮起显示接续提示', badges === 1, `.answer-continuation ${badges} 个（期望 1）`)

    const badge = page.locator('.bubble.assistant .answer-continuation').last()
    const badgeText = (await badge.innerText()).replace(/\s+/g, ' ')
    /*
     * round_index=3 → 界面上说「第 2 问」。后端 index 是**本轮序号**，
     * 用户视角的「接着第几问」要减一（本轮接着的是上一问）。
     */
    record(
      '接续提示标出接着第几问',
      /已接续第 2 问/.test(badgeText),
      badgeText,
    )

    /*
     * 累积文本要折叠。后端送来的 previous_question 形如
     * `平台上一共有多少位客户？\n用户补充或修改：只看美国的`，
     * 原样显示会把两轮内容连同接缝一起铺出来；前端只取首行，
     * 并把 `round_index - 2` 折成「（已补充 N 轮）」。
     */
    record(
      '接续提示折叠累积文本只留首问',
      /平台上一共有多少位客户？/.test(badgeText)
        && !/用户补充或修改/.test(badgeText)
        && /已补充 1 轮/.test(badgeText),
      badgeText,
    )

    // title 挂完整原文，鼠标悬停仍能核对到底接了什么。
    const badgeTitle = await badge.getAttribute('title')
    record(
      '接续提示保留了完整原文可供核对',
      typeof badgeTitle === 'string' && badgeTitle.includes('用户补充或修改'),
      String(badgeTitle).replace(/\s+/g, ' ').slice(0, 80),
    )

    await page.screenshot({ path: path.join(out, 'process-continuation.png') })

    record('无浏览器运行错误', errors.length === 0, errors.join(' | '))
  } catch (error) {
    record('脚本执行完成', false, String(error).slice(0, 300))
  }

  report.errors = errors
  fs.writeFileSync(path.join(out, 'process-checks.json'), JSON.stringify(report, null, 2))
  await browser.close()
  console.log(`\nfailures: ${report.failures.length}`)
  if (report.failures.length) process.exitCode = 1
}

run().catch((e) => { console.error(e); process.exitCode = 1 })
