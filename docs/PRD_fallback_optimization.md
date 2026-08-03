# PRD: OpenCode Free 兜底 + 9 项优化

## 项目名称
`zhongzhuan_fallback_v2` — Python / aiohttp / httpx / SQLite

## 产品目标
1. 用户不配置任何 key 时，自动启用 OpenCode Free 免费兜底上游，开箱即用
2. 修复健康状态机、调度器、Sticky Session 等 9 项已识别的优化点
3. 保持向后兼容，不破坏现有配置和数据库

## 用户故事
1. **新用户开箱即用**：我下载 zhongzhuan 后不想配置任何 key，直接启动就能用免费模型
2. **管理员可关闭兜底**：我担心免费上游不稳定，可以在 config.yaml 关闭兜底
3. **开发者可观察状态**：我能从 /v1/models 看到兜底模型，从日志看到兜底 key 被使用
4. **运维可持久化状态**：服务重启后 key 健康状态（包括学到的限流配额）不丢失

## 需求池
### P0（必须）
- OpenCode Free 兜底上游注入（无 key 时自动启用）
- 优化点1: scheduler.score() 利用 status + TPM
- 优化点2: 提取 _classify_failure 消除重复
- 优化点3: reload_keys 重置 invalid 状态
- 优化点9: 补充状态机/learn_rate_limits/sticky 单元测试

### P1（重要）
- 优化点4: 健康状态持久化到 SQLite
- 优化点5: Sticky session 后台定时清理
- 优化点6: learn_rate_limits 一次构建 lowercase dict

### P2（可选）
- 优化点7: _round_robin_counters 清理
- 优化点8: 429 响应带 X-Zhongzhuan-Reason 头

## UI 设计草案
无前端 UI 变更。兜底模型在 /v1/models 中以 `oc-*` 前缀暴露。

## 待确认问题
- 无（用户已明确"全修复"+"扣出来做兜底"）
