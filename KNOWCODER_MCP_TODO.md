# KnowCoder MCP 实施清单

本清单记录独立 MCP 的实际完成状态。详细设计讨论保存在原项目的 `MCP_DESIGN_QA.md`。

## 运行协议

- [x] 使用 `.knowcoder_workspace` 作为唯一运行目录。
- [x] 分离 `task_id`、`workspace_id` 和不透明的 `continuation_token`。
- [x] 用最小 JSON 状态记录任务阶段和版本。
- [x] 用独立 `lease.json` 保存 Worker 心跳。
- [x] 状态文件通过临时文件和原子替换写入。
- [x] 使用跨进程文件锁保护任务和 Workspace。
- [x] 同一 Workspace 只允许一个写任务。
- [x] 同一任务只允许一个长等待。
- [x] 并发长等待会立即返回明确冲突，不会重复占用宿主请求。
- [x] MCP 重连后可通过令牌或任务列表继续。

## 公共工具

- [x] 只公开 `start_workspace_task`。
- [x] 只公开 `wait_for_task_update`。
- [x] 只公开 `submit_review_decision`。
- [x] 只公开 `read_workspace`。
- [x] 只公开 `find_workspace_tasks`。
- [x] 只公开 `stop_task`。
- [x] 移除旧公共工具、`build_mode` 和扩展工具注册层。
- [x] 工具错误说明操作、阶段、原因和可重试性。
- [x] 大文件读取支持分页。

## 长任务、确认与恢复

- [x] 长阶段由独立后台 Worker 执行。
- [x] 问题和 Schema 完成后进入安静的 `waiting`。
- [x] 审阅页保持只读，确认与自然语言修改回到宿主对话。
- [x] 每版审阅页保存为 Workspace 内独立的静态 HTML，不依赖临时 Review Server。
- [x] Review 决定使用 `expected_version` 防止旧页面覆盖新状态。
- [x] `stop_task` 停止 Worker 并保留已发布 Workspace；停止状态为终态。
- [x] 失败任务通过原令牌恢复当前阶段。

## Workspace 与增量调研

- [x] 增量调研原地更新同一个 Workspace ID。
- [x] 只向抽取阶段分配新增或受影响的来源。
- [x] 实体按类型和规范化名称合并。
- [x] 关系按类型、端点和关键属性合并。
- [x] 来源正文变化时保留旧版本并标记 `superseded`。
- [x] 每次完整发布保存版本快照并更新 `current.json`。
- [x] 候选 Workspace 校验通过后才替换公开版本。
- [x] 发布指针写入失败时恢复上一公开版本并清理未完成快照。

## Prompt 与校验

- [x] 七个 Subagent Prompt 使用统一章节和顺序。
- [x] 每个 Prompt 明确责任、输入、流程、文件、完成条件、工具和独立示例。
- [x] Prompt 与 Validator 契约由自动检查保持一致。
- [x] Validator 公共行为保留在 `BaseValidator` 和 `ArtifactValidator`。
- [x] 首次生成后最多执行一轮定向格式修复。
- [x] 业务错误、外部 API 错误和格式错误分开处理。
- [x] 移除旧 Schema 自动补字段、改名和占位说明代码，只保留语义蓝图编译路径。
- [x] README 中的内部路径和平台专属路径明确报错，不再静默删除。

## 配置、重试和安装

- [x] 使用用户级 `config.py` 配置两个模型和 Serper。
- [x] 配置缺失时逐项报错。
- [x] 外部模型、Serper 和网页抓取使用 1、2、4、8、16 秒重试序列。
- [x] 提供 `knowcoder-mcp serve` 和 `knowcoder-mcp doctor`。
- [x] 提供 macOS 和 Windows 的 `uv tool` 安装脚本。
- [x] README 包含手动安装和 AI 代安装流程。
- [x] README 包含 Codex、Claude Code 和 Claude Work/Desktop 注册方法。
- [x] 移除重复的 SQLite 图检查点状态。

## 验收

- [x] Ruff 静态检查通过。
- [x] Builder 单元、契约和集成测试全部通过。
- [x] 安装后的 Wheel 包含 Harness、Prompt、Skill 和六个 MCP 工具。
- [ ] 使用真实 API 和第三方宿主完成端到端验收。

最后一项需要用户提供有效配置并授权实际 API 消耗，不属于代码静态验收。
