/** 品牌图标版项目流程图生成器，默认输出 v0.3；可传入其他输出主文件名。 */
const fs = require('node:fs');
const path = require('node:path');
const out = __dirname;
const stem = process.argv[2] || '项目整体流程图_v0.3';
const versionLabel = stem.match(/v\d+(?:\.\d+)?$/)?.[0] || 'v0.3';
const W = 2600, H = 2460;
const C = { ink:'#213c53', muted:'#607589', edge:'#ccd8e2', green:'#46866f', greenBg:'#f1f8f4', blue:'#3c77ad', blueBg:'#f0f6fc', teal:'#338b96', tealBg:'#eef8f8', amber:'#b98249', amberBg:'#fcf6ef', purple:'#80649b', purpleBg:'#f7f3fb', red:'#b66c51' };
const parts = [];
let uid = 0;
const esc = v => String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
function rect(x,y,w,h,fill='#fff',stroke=C.edge,r=12,dash=false) { parts.push(`<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="${r}" fill="${fill}" stroke="${stroke}" stroke-width="1.7"${dash?' stroke-dasharray="10 7"':''}/>`); }
function txt(t,x,y,size=21,color=C.ink,bold=false,anchor='start',owner='') { parts.push(`<text x="${x}" y="${y}" font-size="${size}" fill="${color}" font-weight="${bold?700:400}" text-anchor="${anchor}"${owner?` data-card="${owner}"`:''}>${esc(t)}</text>`); }
function line(points,color=C.blue,dash=false,arrow=true,small=false) { parts.push(`<polyline points="${points.map(p=>p.join(',')).join(' ')}" fill="none" stroke="${color}" stroke-width="${small?2:3}" stroke-linejoin="round"${dash?' stroke-dasharray="9 7"':''}${arrow?` marker-end="url(#${small?'s':'a'}${color.slice(1)})"`:''}/>`); }
function icon(kind,x,y,color) {
  const brandIcons = {
    factflow:'fact-flow.svg', sourcebridge:'source-bridge.svg', metricalign:'metric-align.svg',
    evidenceledger:'evidence-ledger.svg', boundedverify:'bounded-verify.svg',
    queryguard:'query-guard.svg', doclens:'doc-lens.svg', gap2graph:'gap2graph.svg',
  };
  if (brandIcons[kind]) {
    const iconPath=path.resolve(__dirname,'../../web/public/brand/icons',brandIcons[kind]);
    const source=fs.readFileSync(iconPath,'utf8');
    const inner=source.replace(/^.*?<svg[^>]*>/s,'').replace(/<\/svg>\s*$/,'');
    parts.push(`<g transform="translate(${x} ${y}) scale(.82)">${inner}</g>`);
    return;
  }
  const shapes = {
    db:'<ellipse cx="20" cy="9" rx="15" ry="6"/><path d="M5 9v22c0 8 30 8 30 0V9M5 20c0 8 30 8 30 0"/>',
    doc:'<path d="M9 3h17l8 8v26H9zM26 3v9h8M15 19h13M15 25h13M15 31h9"/>',
    search:'<circle cx="17" cy="17" r="12"/><path d="m26 26 11 11"/>',
    check:'<path d="m20 3 15 6v11c0 9-7 15-15 18C12 35 5 29 5 20V9zM11 20l6 6 13-14"/>',
    chat:'<path d="M4 5h32v23H17l-9 9v-9H4zM11 13h18M11 20h12"/>',
    flow:'<rect x="14" y="2" width="13" height="10" rx="2"/><rect x="2" y="28" width="13" height="10" rx="2"/><rect x="25" y="28" width="13" height="10" rx="2"/><path d="M20 12v9H8v7M20 21h12v7"/>',
    clock:'<circle cx="20" cy="20" r="16"/><path d="M20 9v12l9 5"/>',
    grid:'<rect x="3" y="5" width="34" height="30" rx="3"/><path d="M3 15h34M3 25h34M14 5v30M25 5v30"/>',
  };
  parts.push(`<g transform="translate(${x} ${y}) scale(.82)" stroke="${color}" stroke-width="2.7" fill="none" stroke-linecap="round" stroke-linejoin="round">${shapes[kind]||shapes.doc}</g>`);
}
function card(x,y,w,h,title,body,{color=C.blue,fill='#fff',kind='doc',dash=false,size=21,titleSize=25}={}) {
  const id='card'+(++uid);
  parts.push(`<g id="${id}" data-box="${x},${y},${w},${h}">`);
  rect(x,y,w,h,fill,dash?color:C.edge,12,dash); icon(kind,x+15,y+9,color);
  txt(title,x+68,y+36,titleSize,color,true,'start',id);
  body.forEach((t,i)=>txt(t,x+20,y+(h<100?63:67)+i*(h<100?25:27),size,C.ink,false,'start',id));
  parts.push('</g>');
  return { x,y,w,h,id,cx:x+w/2,bottom:y+h };
}
function panel(x,y,w,h,title,color,fill,note='') {
  rect(x,y,w,h,fill,C.edge,14);
  parts.push(`<path d="M${x+14} ${y}H${x+w-14}Q${x+w} ${y} ${x+w} ${y+14}V${y+51}H${x}V${y+14}Q${x} ${y} ${x+14} ${y}Z" fill="${color}"/>`);
  txt(title,x+20,y+35,29,'#fff',true);
  if(note) txt(note,x+w-18,y+34,19,'#fff',false,'end');
}
parts.push(`<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-labelledby="title desc"><title id="title">知证：多模态可解释精准问数问答项目整体流程图</title><desc id="desc">分开展示离线数据准备、在线问答编排、SQL工具、文档检索工具、隔离跨源事实核验、结果与证据展示、模型和工程支撑及离线评测。KAG使用虚线标记未接入规划。</desc><defs>${Object.values(C).filter(v=>/^#[0-9a-f]{6}$/i.test(v)).map(v=>['a','s'].map(m=>`<marker id="${m}${v.slice(1)}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="${m==='s'?3:7}" markerHeight="${m==='s'?3:7}" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="${v}"/></marker>`).join('')).join('')}</defs><g font-family="Microsoft YaHei, PingFang SC, Noto Sans CJK SC, sans-serif"><rect width="${W}" height="${H}" fill="#f8fafc"/>`);
const markPath=path.resolve(__dirname,'../../web/public/brand/verimind-mark.svg');
const markData=`data:image/svg+xml;base64,${fs.readFileSync(markPath).toString('base64')}`;
parts.push(`<image href="${markData}" x="2222" y="18" width="62" height="62"/>`);
txt('知证 VeriMind',2510,59,23,C.ink,true,'end');
txt('多模态数据驱动的可解释精准问数 / 问答智能体',W/2,68,49,C.ink,true,'middle');
txt('知证 · 数据准备 → 问题理解与工具编排 → 查询 / 检索 / 跨源核验 → 有来源依据的回答',W/2,111,25,C.muted,false,'middle');

// I 数据准备是独立离线过程，不画成每次用户提问必经的一段。
panel(30,140,2540,325,'一、数据接入与准备',C.green,C.greenBg,'离线准备；更新数据或索引时执行');
const prepXs=[50,687,1324,1961], prepW=589;
const db=[
  ['结构化业务数据',['PostgreSQL：Chinook / AdventureWorks','跨源试验使用独立业务库与数据范围'],'db'],
  ['读取数据库结构',['表、字段、类型、主外键与结构目录','保留原始业务数据；不改写为文档'],'grid'],
  ['业务语义与关系信息',['中文描述、实体别名、指标与时间口径','外键关系用于候选定位及关联规划'],'flow'],
  ['查询侧可用资源',['结构目录 + 别名词典 + 关联关系','业务数据由工具 A 执行 SQL 时只读访问'],'db'],
];
const docs=[
  ['非结构化原始文档',['PDF / Word / Markdown / TXT','扫描 PDF 按页进入文字识别流程'],'doc'],
  ['解析与文本规范化',['文本层抽取；扫描页离线文字识别','断行合并、表格还原、保留来源定位'],'doc'],
  ['目录识别与层级切片 · 任务 7',['书签 → 编号规则 → 字号容错','保留文档、章节、页码与可得坐标'],'flow'],
  ['双通道索引与原文定位',['关键词索引 + bge 向量 / pgvector','原文及片段定位供工具 B 检索与引用'],'search'],
];
for(let i=0;i<4;i++) {
  card(prepXs[i],207,prepW,104,db[i][0],db[i][1],{color:C.green,kind:db[i][2],size:20,titleSize:24});
  card(prepXs[i],338,prepW,104,docs[i][0],docs[i][1],{color:C.teal,kind:docs[i][2],size:20,titleSize:24});
  if(i<3) {line([[prepXs[i]+prepW+6,259],[prepXs[i+1]-9,259]],C.green);line([[prepXs[i]+prepW+6,390],[prepXs[i+1]-9,390]],C.teal);}
}

// II 交互与理解。澄清回到用户，经新的输入继续，不直接生成缺条件 SQL。
panel(30,505,2540,255,'二、用户交互与问答编排',C.blue,C.blueBg,'在线入口：Vue 工作台 / FastAPI 接口');
const px=[50,687,1324,1961];
card(px[0],573,589,139,'用户问题与会话上下文',['自然语言输入、历史轮次与已补充条件','会话隔离、取消 / 替换问题、上下文恢复','单源问题走相应工具的短路径'],{kind:'chat'});
card(px[1],573,589,139,'要素识别与标准化 · 任务 1',['识别对象、指标、时间与筛选条件','别名 / 错别字 / 相对时间规范化','保留原意，不补造缺失业务条件'],{kind:'search'});
card(px[2],573,589,139,'语义检查与澄清 · 任务 3',['检查指标口径、维度、比较时间范围','条件不足：请求补充；条件齐全：继续','用户补充后恢复原问题及任务状态'],{kind:'check'});
card(px[3],573,589,139,'意图路由与执行计划',['依据数据源元信息和问法选择工具','普通查询 / 文档问答：工具调用计划','已启用跨源场景：显式事实依赖计划'],{kind:'flow'});
for(let i=0;i<3;i++)line([[px[i]+589+6,641],[px[i+1]-9,641]],C.blue);
line([[1618,717],[1618,739],[345,739],[345,717]],C.amber);
rect(605,725,770,28,C.blueBg,C.blueBg,0);txt('缺少条件 → 向用户澄清；补充后的新一轮输入继续处理',990,746,19,C.amber,false,'middle');
txt('选择对应路径；工具不必全部调用，也不固定并行',1300,794,22,C.muted,false,'middle');
line([[2255,762],[2255,804],[430,804],[430,824]],C.blue);
line([[1300,804],[1300,824]],C.teal);
line([[2170,804],[2170,824]],C.purple);

// III 三个执行面板。C 内标明复用 A/B，不将多个工具误画成毫无依赖的并行链。
panel(30,830,805,770,'三 A、自然语言转 SQL（NL2SQL）',C.blue,C.blueBg,'查询工具');
panel(865,830,805,770,'三 B、检索增强问答（RAG）',C.teal,C.tealBg,'文档工具');
panel(1700,830,870,770,'三 C、跨源事实核验',C.purple,C.purpleBg,'显式启用的隔离实验路径');
const sqlX=50, ragX=885, crossX=1720, sqlW=765,ragW=765,crossW=830;
const sy=[904,1029,1154,1279,1404];
const sqlCards=[
 ['表与字段候选定位 · 任务 4',['结合结构元信息、中文描述与业务别名','排除干扰项；候选相似不等于关系成立'],'queryguard'],
 ['外键关联与查询计划 · 任务 5',['选择关联键和关联路径，明确聚合粒度','无法确认关系时请求澄清或限定回答'],'factflow'],
 ['生成候选 SQL',['国产语言模型基于结构目录与计划生成','支持单表、多表关联、聚合及嵌套查询'],'doc'],
 ['执行前校验与有限修复',['sqlglot 语法树：只读、表字段、关联语义','仅可修复的失败回传重写；尝试次数有限'],'boundedverify'],
 ['只读执行与结果管理',['PostgreSQL：事务只读、超时及容量限制','保留 SQL / 结果来源；分页、导出与截断提示'],'db'],
];
const ragCards=[
 ['选择单轮或多跳检索',['单轮直接检索；多跳按配置启用','多跳分解子问题，使用前跳事实构造后跳查询'],'factflow'],
 ['关键词 + 语义双通道召回',['BM25 关键词与本地 bge 向量检索','按排名融合（RRF），保留片段来源'],'doclens'],
 ['重排与上下文组织',['本地 bge 重排；合并、去重及引用链组织','多跳按充分性与跳数预算决定是否继续检索'],'boundedverify'],
 ['有依据的答案生成',['国产语言模型只基于给定检索上下文回答','资料不足时说明缺口；多跳汇合后生成答案'],'chat'],
 ['引用解析与来源投影',['答案引用标记对应实际使用的文档片段','文档 / 页码 / 章节 / 字符区间 / 可得框坐标'],'evidenceledger'],
];
for(let i=0;i<5;i++){
  card(sqlX,sy[i],sqlW,103,sqlCards[i][0],sqlCards[i][1],{kind:sqlCards[i][2],color:C.blue,size:21});
  card(ragX,sy[i],ragW,103,ragCards[i][0],ragCards[i][1],{kind:ragCards[i][2],color:C.teal,size:21});
  if(i<4){line([[432,sy[i]+106],[432,sy[i+1]-6]],C.blue);line([[1267,sy[i]+106],[1267,sy[i+1]-6]],C.teal);}
}
// 多跳回路位于右侧留白，非所有单轮问答必经。
line([[809,1313],[824,1313],[824,1189],[809,1189]],C.amber,false,true,true);
line([[1628,1180],[1658,1180],[1658,938],[1630,938]],C.amber);
txt('继续',1646,1089,16,C.amber,false,'end');
txt('下一跳',1646,1111,16,C.amber,false,'end');
txt('校验 / 执行报错：仅可修复错误有限回传到 SQL 生成',432,1548,19,C.blue,false,'middle');
txt('索引按需准备 / 加载；每次提问不重复解析与 OCR',1267,1548,19,C.teal,false,'middle');
// C 中的事实计划只覆盖当前场景，不声称已具备任意业务的通用自动分解。
const cy=[904,1009,1114,1219,1324,1429];
const crossCards=[
 ['① 读取对象事实 · 调用工具 A',['工单 → 设备编号 / 开单时间 / 设备客户','明确业务编号；同名对象仅作为候选'],'factflow'],
 ['② 核验跨源桥接关系 · 调用工具 A',['设备 → 合同：受控依据、客户一致、候选唯一','分别检查映射有效期、合同有效期及合同状态'],'sourcebridge'],
 ['③ 核验事件与时间事实 · 调用工具 A',['检查关键事件、先后顺序、时区及实际耗时','前置事实不受支持时跳过依赖它的后续任务'],'metricalign'],
 ['④ 定向检索并核验规则 · 调用工具 B',['只查候选合同；核对设备范围和修复时限','识别特别约定、未约定及未消解的条款冲突'],'doclens'],
 ['⑤ 对齐口径并形成判定',['统一指标定义、起止事件、单位及适用范围','确定性计算与比较；不借用响应 / 到场时限'],'metricalign'],
 ['⑥ 事实—证据关联与限定输出',['每项已核验事实关联实际 SQL / 文档证据','保留已确认部分；说明缺失、冲突或阻断原因'],'evidenceledger'],
];
for(let i=0;i<6;i++){
  card(crossX,cy[i],crossW,98,crossCards[i][0],crossCards[i][1],{kind:crossCards[i][2],color:C.purple,size:20,titleSize:24});
  if(i<5)line([[2135,cy[i]+99],[2135,cy[i+1]-3]],C.purple,false,true,true);
}
txt('当前事实计划覆盖工单—合同；通用跨业务规划仍需扩展',2135,1550,19,C.purple,false,'middle');
txt('阻断后限定回答；补查和重试受次数 / 时间预算约束',2135,1581,19,C.muted,false,'middle');

// 单源结果与跨源结果汇入统一响应，不强制单源结果经过跨源核验。
line([[432,1603],[432,1630],[1267,1630]],C.blue,false,false);
line([[1267,1603],[1267,1657]],C.teal);
line([[2135,1603],[2135,1630],[1267,1630]],C.purple);
txt('各路径返回答案候选、实际结果与来源；单源问答无需绕行跨源核验',1300,1624,20,C.muted,false,'middle');
panel(30,1670,2540,210,'四、答案整合、可解释性与用户呈现',C.green,C.greenBg,'统一响应：answer / sql / refs / trace / need_clarify');
const outputs=[
 ['回答与结果',['完整回答 / 已确认部分 / 澄清与失败说明','查询表格、分页、完整结果导出与截断提醒'],'chat'],
 ['结构化数据证据',['实际 SQL、表 / 字段 / 记录与查询条件','查询零结果须关联实际执行事实'],'db'],
 ['文档证据与原文查看',['引用卡片、章节与页码、文本片段定位','有真实框坐标时显示原文区域高亮'],'doc'],
 ['执行轨迹（Trace）',['代码记录工具调用、入参出参、耗时和命中','展示实际执行过程，支持检查失败原因'],'flow'],
];
for(let i=0;i<4;i++) card(prepXs[i],1737,prepW,113,outputs[i][0],outputs[i][1],{kind:outputs[i][2],color:C.green,size:20,titleSize:24});

panel(30,1915,2540,190,'五、模型与工程支撑',C.amber,C.amberBg,'能力支撑关系；不是新增的用户操作步骤');
card(50,1982,589,110,'国产语言模型（LLM）',['支撑 SQL 生成、文档回答及多跳规划','模型服务可配置；调用设超时与重试'],{color:C.amber,kind:'chat',size:20,titleSize:24});
card(687,1982,589,110,'本地向量、重排与文字识别',['bge：文档向量化与重排；OCR：扫描页','离线文字识别与本地小模型推理'],{color:C.amber,kind:'search',size:20,titleSize:24});
card(1324,1982,589,110,'运行与配置管理',['FastAPI / Vue / PostgreSQL / Docker','统一工具协议、配置开关与异常记录'],{color:C.amber,kind:'flow',size:20,titleSize:24});
card(1961,1982,589,110,'知识增强生成（KAG）· 支线',['独立实验：RAG 证据缺口时考虑图谱补充','接入主链路为规划；模拟图工具不代表接入'],{color:C.purple,kind:'gap2graph',size:20,titleSize:24,dash:true});

panel(30,2140,2540,230,'六、独立离线评测与改进',C.blue,C.blueBg,'标准答案仅供评分；不进入模型输入或检索索引');
const ex=[50,560,1070,1580,2090], ew=460;
const ev=[
 ['题集与判据',['数据来源、问题与参考答案分离','区分开发回归和新留出题'],'doc'],
 ['版本冻结与预检',['代码、数据、模型配置、评分口径','只读连接与索引预检；登记摘要'],'check'],
 ['运行真实系统',['主线回归 / 同条件配对实验','封存原始回答、错误、耗时与轨迹'],'flow'],
 ['评分、盲审与核验',['结果匹配、忠实度、引用与鲁棒性','独立盲审与分歧裁决；核验封存'],'check'],
 ['分析与版本改进',['逐题错误归因、效率瓶颈与限制','修复后另版本回归；新题再复验'],'clock'],
];
for(let i=0;i<5;i++){
  card(ex[i],2208,ew,115,ev[i][0],ev[i][1],{color:C.blue,kind:ev[i][2],size:19,titleSize:24});
  if(i<4)line([[ex[i]+ew+7,2260],[ex[i+1]-9,2260]],C.blue);
}
txt('覆盖基础 SQL / 文档问答、任务 1 / 3 / 4 / 5 / 7、五轮以上对话、鲁棒性及两档数据规模效率分析',1300,2350,21,C.blue,false,'middle');
line([[70,2410],[140,2410]],C.blue);txt('实线：已实现模块的处理关系',153,2417,20,C.muted);
line([[800,2410],[870,2410]],C.amber);txt('橙色回路：满足条件时的澄清 / 多跳',883,2417,20,C.muted);
line([[1650,2410],[1720,2410]],C.purple,true);txt('紫色虚框：尚未接入的增强规划',1733,2417,20,C.muted);
txt(`${versionLabel} · 2026-10-12 · 按当前工作区实现绘制；能力验收情况以对应测试记录为准`,1300,2448,18,C.muted,false,'middle');
parts.push('</g></svg>');
const svg=parts.join('\n');
fs.writeFileSync(path.join(out,stem+'.svg'),svg,'utf8');
const html=`<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${stem}</title><style>html,body{margin:0;background:#e9eef3;font-family:"Microsoft YaHei",sans-serif}nav{position:sticky;top:0;background:#fff;padding:12px 22px;box-shadow:0 1px 8px #cad4df;z-index:1;display:flex;gap:12px;align-items:center}button,a{border:1px solid #ccd8e2;border-radius:7px;background:white;color:#213c53;padding:7px 12px;font-size:14px;text-decoration:none;cursor:pointer}main{padding:16px}svg{display:block;width:100%;height:auto;background:#fff;box-shadow:0 3px 16px #c9d3dd}body.export{background:#f8fafc}body.export nav{display:none}body.export main{padding:0}body.export svg{width:${W}px;box-shadow:none}@media print{nav{display:none}main{padding:0}svg{box-shadow:none}}</style></head><body><nav><strong>${stem}</strong><button onclick="setZoom(.6)">适应屏幕</button><button onclick="setZoom(1)">100%</button><button onclick="setZoom(1.5)">150%</button><a href="${stem}.svg" download>下载矢量图</a><span style="color:#607589">实线：已实现处理关系　紫色虚框：KAG 接入规划</span></nav><main>${svg}</main><script>function setZoom(v){document.querySelector('svg').style.width=v===.6?'100%':(${W}*v)+'px'}if(location.search.includes('export'))document.body.classList.add('export');</script></body></html>`;
fs.writeFileSync(path.join(out,stem+'.html'),html,'utf8');
console.log(JSON.stringify({svg:path.join(out,stem+'.svg'),html:path.join(out,stem+'.html'),width:W,height:H,cards:uid}));
