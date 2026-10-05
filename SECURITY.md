# 安全披露

如果你发现 Ringharness 中的安全问题（越权、注入、密钥泄露、可伪造 DONE、绕过租约/fence 等），请**不要**开公开 Issue。

## 报告方式

1. 使用 GitHub 仓库的 **Security → Advisories → Report a vulnerability**（仓库启用后），或
2. 通过仓库 Owner 的私信 / 约定安全邮箱联系维护者。

请包含：影响版本/commit、复现步骤、预期与实际行为、是否已在外部披露。

## 我们会做什么

- 确认后给出修复时间表与缓解措施。
- 在补丁就绪前请勿公开完整 exploit。
- 修复后可在 Release / Advisory 中致谢（经你同意）。

## 范围外

- 仅影响你本地错误配置的问题（缺 JWT、指向生产库等）请走普通 Issue。
- 依赖 CVE：优先提 Dependabot PR；若可被组合成对 Ringharness 的真实攻击链，再按上文披露。
