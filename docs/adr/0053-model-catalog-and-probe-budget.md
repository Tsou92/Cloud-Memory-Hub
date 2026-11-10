# ADR-0053：模型列表选择与连接测试修复

日期：2026-11-10。状态：实施中。关联 FR-01、FR-02、FR-07、FR-14、ENG-01。

## 证据与范围

固定上游与补丁 0026 只有手填模型名，`model_probe._models_request` 只用于验证给定模型，不向网页返回候选列表。NAS 真实合成测试观察到：Anthropic 生成 HTTP 200，但 64 token 的探针返回 `max_tokens` 和空正文；改为 512 token、明确只返回 JSON 后完整成功。相同凭证调用列表时，标准 x-api-key 返回 401，同一地址的 Bearer 认证返回列表，包含用户指定模型。不能将生成探测失败映射为地址错误或模型不存在。

继续复用现有管理表单、鉴权、URL 校验、加密凭证库和 HTTP 客户端；不新建配置存储、不导入历史。协议参考 [Models API](https://platform.claude.com/docs/en/api/models/list)。列表结果只作选择候选，真实生成测试仍决定是否可激活。

## 增量协议

新增管理员保护的 `POST /api/v1/model-catalog`。输入为 `provider_kind`、`base_url`、可选 `api_key`（SecretStr），以及可选 `profile_id`、`expected_profile_revision`。新表单可在尚未选择模型时获取列表；本操作不保存配置、不激活、不发送历史或生成请求。

复用保存的凭证必须匹配 profile 的 revision、provider 和原地址；变更地址时不得把原密钥发往新地址。新输入的密钥仅用于本次请求。沿用 HTTPS/本地 HTTP 校验，禁止重定向。Anthropic 根地址、`/v1` 和 `/v1/messages` 规范化至同一 `/v1/models`；先使用既有标准认证，仅列表 GET 返回 401/403 时允许对同一 URL 用相同密钥作一次 Bearer 兼容重试。生成请求仍只使用 Anthropic Messages 协议。

返回 `{code, models:[{id,display_name}], has_more}`；code 为 `ok`、`unsupported`、`missing_api_key`、`unreachable`、`unauthorized`、`invalid_response`、`rate_limited`、`insufficient_balance` 或 `profile_changed`。响应正文有大小上限，候选数和字段长度有界，不转发服务错误正文、密钥或认证头。列表不提供或失败时可继续手填。

页面提供获取/刷新及候选选择，保留可编辑模型名。地址、协议或密钥变更清除旧列表，迟到响应不得覆盖新表单；不会自动更换用户已选模型。只依据探针 stage.code 显示具体失败，不用 capability=false 推断模型不存在或网络失败。

## 兼容与验收

Anthropic 探针使用 512 token 及明确的纯 JSON 提示，仍要求完整响应和正确 JSON；不把截断当成功。无数据库迁移、无 MCP/Skill 合同变更。增量上游补丁保留 0026 原制品，升级前备份并保留旧镜像。

验证覆盖列表认证、地址规范化、保存凭证边界、错误与响应上限、前端选择/手填/迟到响应、64 token 问题的回归和已有模型探测。NAS 真实验证单独记录，不把合成服务测试当真实服务通过。
