/**
 * 问题灵感库（输入框左侧四角星按钮的数据源）。
 *
 * 导师第一次打开界面最大的问题不是「答案对不对」，而是「我不知道能问什么」。
 * 首页只有两张示例卡，覆盖不了系统的真实能力面。这份清单按能力分组，
 * 每组附一句「看什么」——把评委/导师的注意力引到该看的地方，
 * 让演示顺带完成核心架构与功能的验证。
 *
 * 问题内容的原则：全部来自评测用例或真实跑通过的问句，不放没验证过的句子。
 * 前端点击行为 = 填入输入框（与首页示例卡一致），发不发由用户决定。
 */

export interface GalleryQuestion {
  question: string
  /** 可选：这句的说明，悬停可见 */
  note?: string
}

export interface GalleryGroup {
  id: string
  title: string
  /** 这组验证什么能力（对应架构与赛题功能项） */
  purpose: string
  /** 演示时该看哪里 */
  watch: string
  icon: string
  questions: GalleryQuestion[]
}

export const QUESTION_GALLERY: GalleryGroup[] = [
  {
    id: 'data',
    title: '查业务数据',
    purpose: '自然语言转 SQL（NL2SQL 主线）',
    watch: '答案旁的「Chinook 示例库」来源徽标；点开回答依据能看生成的 SQL 与数据表；「查看处理详情」有完整 5 步工具链。',
    icon: 'database',
    questions: [
      { question: '平台上一共有多少位客户？' },
      { question: '2023 年一整年一共开了多少张发票？' },
      { question: '按国家分组，客户分别有多少？', note: '分组聚合，结果以数据表呈现' },
    ],
  },
  {
    id: 'doc',
    title: '查文档条款',
    purpose: '文档检索与原文溯源（RAG 主线，含多格式语料）',
    watch: '答案旁的「文档语料库」徽标；点引用会打开 PDF 原文并高亮到具体页与条款；第三条问的是 .docx 文档——语料库不止 PDF。',
    icon: 'document',
    questions: [
      { question: '智能计算设备年度维保服务合同里，质保期是多久？' },
      { question: '机房日常巡检的时间安排是怎样的？' },
      { question: '数据分级分类管理规范把数据分成哪几级？', note: '来自 .docx 语料，验证多格式覆盖' },
    ],
  },
  {
    id: 'multiturn',
    title: '多轮追问',
    purpose: '上下文保持（赛题「连续 5 轮以上准确」项）',
    watch: '按顺序连着问：第 2 问只说「他」，不说人名。展开处理详情，第一步「要素识别与改写」会把上一问与追问拼在一起——这就是接上文的证据。',
    icon: 'chat',
    questions: [
      { question: '客户 Frank Harris 是谁？', note: '第 1 问：先建立上下文' },
      { question: '他总共消费了多少？', note: '第 2 问：「他」指上一问的人' },
      { question: '改成看发票张数', note: '第 3 问：改口径，不加主语' },
    ],
  },
  {
    id: 'clarify',
    title: '主动澄清',
    purpose: '信息不足时反问，而不是硬猜一个答案（含相对时间口径澄清）',
    watch: '系统不会硬答，会明确指出缺了什么并请你补充；在输入框直接回答补充信息，会话会接着走。第 3 条验证的是「时间词有歧义也要先问口径」，而不是拿一个默认年份硬算出碰巧的数字。',
    icon: 'help',
    questions: [
      { question: '帮我看下增长率', note: '故意不说哪项指标、哪个时间段' },
      { question: '2024 年销售冠军是谁，并且他的成功方法是什么', note: '「成功方法」不在数据库里' },
      { question: '去年有多少员工？', note: '「去年」必须先澄清口径（去年内新入职，还是去年期间在职），不硬算' },
    ],
  },
  {
    id: 'boundary',
    title: '能力边界与如实说明',
    purpose: '数据里没有依据时如实说，不拿全量数字冒充答案',
    watch: '答案不会给出 59（全量客户数），而是明确说明「相关业务表没有可用的时间依据」；核验卡会把该条条件标为未落实。这是「宁可说不知道，不编一个数字」的证据。',
    icon: 'boundary',
    questions: [
      { question: '2023 年有多少客户？', note: 'customer 表没有时间列——系统应说明时间条件无法落实，而不是返回全量 59' },
      { question: '今年至今开了多少张发票？', note: '「至今」需要截止锚点；答案附「未完全证实」的说明，不冒充精确结果' },
    ],
  },
  {
    id: 'conditions',
    title: '多轮条件继承',
    purpose: '追问不加限定词时，已确认的条件自动延续；换对象时不串条件',
    watch: '第 2 问只说「只看巴西的呢？」，没有重复 2023——展开处理详情可以看到条件档案把上一轮的时间条件继承过来（SQL 同时含 2023 与 Brazil）。第 3 问换成员工，2023 不会默默跟过去。',
    icon: 'chat',
    questions: [
      { question: '2023 年一整年一共开了多少张发票？', note: '第 1 问：建立时间条件' },
      { question: '只看巴西的呢？', note: '第 2 问：只加国家，时间条件自动继承' },
      { question: '那有多少位员工？', note: '第 3 问：换对象——2023 不该跟到员工上' },
    ],
  },
  {
    id: 'robust',
    title: '抗噪输入',
    purpose: '错别字、口语别名、相对时间词的鲁棒性',
    watch: '三句都不是「标准问法」：错别字、别名（买家=客户）、时间词（去年会按锚点折算成具体年份）。答案应当与规范问法一致。',
    icon: 'shield',
    questions: [
      { question: '平台上一共由多少位客户？', note: '「由」是错别字' },
      { question: '买家一共有多少位？', note: '「买家」是别名，映射到客户表' },
      { question: '去年一整年一共开了多少张发票？', note: '「去年」按时间锚点折算' },
    ],
  },
]
