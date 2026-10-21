// gymflow-ai Worker —— 合并版
// 保留：原健身问答（POST /api/ask，走 GLM）
// 新增：verimind.gym-flow.xyz 全站反代 → localtunnel 登录网关
//
// 需要在 Worker 的 Variables（变量）里新增一项：
//   TUNNEL_ORIGIN = https://verimind-demo.loca.lt
// （localtunnel 子域若将来变了，只改这一个变量即可）

var __defProp = Object.defineProperty;
var __name = (target, value) => __defProp(target, "name", { value, configurable: true });

// ===== 健身问答部分（原样保留） =====
var SYSTEM_PROMPT = `你是健身减脂辅助顾问，专注回答健身训练、力量三分化（推日/拉日/腿部日）、体态改善（圆肩/溜肩/肱骨前移/肩峰撞击）、日常减脂饮食相关问题。
- 回答精简凝练，适配手机端阅读，通常控制在200字以内；
- 若用户问肩部动作，正常推荐并简要提醒留意肩部受力即可；
- 若用户问非健身类问题，自行礼貌回绝，说明你只解答健身相关问题；
- 减脂饮食建议以日常可操作为主，不追求精确计算。`;
var CORS = { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "Content-Type" };
var json = /* @__PURE__ */ __name((data, status = 200) => new Response(JSON.stringify(data), {
  status,
  headers: { "Content-Type": "application/json", ...CORS }
}), "json");

var rateMap = /* @__PURE__ */ new Map();

// ===== VeriMind 反代部分 =====
const VERIMIND_HOSTS = new Set(["verimind.gym-flow.xyz", "www.verimind.gym-flow.xyz"]);
// 允许访客看到的响应头（去掉 hop-by-hop 与 content-length，fetch 会重算）
const PASS_HEADERS = ["content-type", "cache-control", "content-disposition", "last-modified", "etag", "set-cookie", "location"];

async function handleVerimind(request) {
  const origin = (globalThis.TUNNEL_ORIGIN || "https://verimind-demo.loca.lt").replace(/\/+$/, "");
  const incoming = new URL(request.url);
  const upstreamUrl = origin + incoming.pathname + incoming.search;

  const headers = new Headers();
  for (const h of ["content-type", "accept", "authorization", "x-session-id", "user-agent"]) {
    if (request.headers.get(h)) headers.set(h, request.headers.get(h));
  }

  const init = {
    method: request.method,
    headers,
    redirect: "manual",
  };
  if (!["GET", "HEAD"].includes(request.method)) {
    init.body = await request.arrayBuffer();
  }

  let resp;
  try {
    resp = await fetch(upstreamUrl, init);
  } catch (e) {
    return new Response(
      `<!DOCTYPE html><html lang="zh-CN"><meta charset="utf-8"><body style="font-family:system-ui;display:flex;align-items:center;justify-content:center;height:100vh;color:#5f6368"><div style="text-align:center"><h2>演示服务暂时不在线</h2><p>后端通道未连接，请稍后刷新，或联系演示方重新开启。</p></div></body></html>`,
      { status: 502, headers: { "Content-Type": "text/html; charset=utf-8" } }
    );
  }

  const outHeaders = new Headers();
  for (const h of PASS_HEADERS) {
    const v = resp.headers.get(h);
    if (v) outHeaders.set(h, v);
  }
  // localtunnel 提示页的跳转会带完整外部 URL，重写回本域名以免暴露/绕出
  const loc = outHeaders.get("location");
  if (loc) {
    try {
      const l = new URL(loc, origin);
      outHeaders.set("location", l.pathname + l.search);
    } catch { /* 保持原样 */ }
  }
  if (["GET", "HEAD"].includes(request.method)) {
    outHeaders.delete("content-length"); // 内容可能被改写，交给边缘重算
  }
  return new Response(resp.body, { status: resp.status, headers: outHeaders });
}

// ===== 主入口 =====
var worker_default = {
  async fetch(request, env) {
    const url = new URL(request.url);

    // --- VeriMind 演示反代：整站接管 ---
    if (VERIMIND_HOSTS.has(url.hostname)) {
      if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
      return handleVerimind(request);
    }

    // --- 原健身问答（原样保留） ---
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
    if (request.method !== "POST" || url.pathname !== "/api/ask") {
      const isHealth = url.pathname === "/api/health";
      return json(isHealth ? { status: "ok" } : { error: "Not Found" }, isHealth ? 200 : 404);
    }
    let params;
    try {
      params = await request.json();
    } catch {
      return json({ error: "请求格式错误" }, 400);
    }
    const { password: pwd, deviceId, content } = params;
    if (!pwd || pwd !== env.ACCESS_PASSWORD) return json({ error: "访问密钥错误" }, 403);
    if (!deviceId || !content || !content.trim()) return json({ error: "缺少必填参数: password, deviceId, content" }, 400);
    const maxPerDay = parseInt(env.MAX_PER_DAY || "100", 10);
    const used = rateMap.get(deviceId) || 0;
    if (used >= maxPerDay) return json({ error: "今日使用额度已满，次日自动恢复", usage: { used, limit: maxPerDay } }, 429);
    try {
      const answer = await callGLM(content.trim(), env.GLM_API_KEY);
      rateMap.set(deviceId, used + 1);
      return json({ success: true, answer, usage: { used: used + 1, remaining: maxPerDay - used - 1 } });
    } catch (e) {
      return json({ error: `AI 服务暂时不可用: ${e.message}` }, 500);
    }
  }
};

async function callGLM(content, apiKey) {
  const resp = await fetch("https://open.bigmodel.cn/api/paas/v4/chat/completions", {
    method: "POST",
    headers: { "Content-Type": "application/json", "Authorization": `Bearer ${apiKey}` },
    body: JSON.stringify({
      model: "glm-4-flash",
      messages: [
        { role: "system", content: SYSTEM_PROMPT },
        { role: "user", content }
      ],
      temperature: 0.7,
      max_tokens: 1500
    })
  });
  const data = await resp.json();
  if (data.choices && data.choices[0]) return data.choices[0].message.content.trim();
  throw new Error(data.error?.message || "AI 接口返回异常");
}
__name(callGLM, "callGLM");

export {
  worker_default as default
};
