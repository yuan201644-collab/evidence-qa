# 问数服务 API 接口说明

# 1　接口概览

基础地址：https://api.example.com　版本前缀：/api/v1　鉴权方式：请求头 Authorization: Bearer <token>

# 2　发起问数请求

POST /api/v1/query

请求体字段：question（必填，自然语言问题，最长 500 字符）、database（可选，目标库名称，默认 chinook）、top_k（可选，知识库检索条数，默认 5）、explain（可选，布尔值，是否返回推理链路，默认 false）。

响应体字段：answer（字符串，最终答案）、sql（字符串，生成的结构化查询语句，问答类请求为 null）、refs（数组，知识库引用片段，含 doc、anchor、score 三个字段）、trace（数组，推理链路埋点，explain 为 true 时返回）。

# 3　错误码

400：请求参数不合法。401：鉴权失败，令牌缺失或已过期。404：目标库不存在。422：问题无法解析为可执行的查询。429：触发限流，默认每分钟 60 次。500：服务内部错误。503：依赖的数据库或向量库不可用。

# 4　限流与重试

触发 429 后应等待 1 秒并按指数退避重试，最多重试 3 次。服务端在响应头 X-RateLimit-Remaining 中返回剩余额度。

# 5　版本变更

v1.3 起新增 explain 参数；v1.2 起 refs 中增加 anchor 字段用于定位引用片段所在章节。
