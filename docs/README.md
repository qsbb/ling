# 文档索引

## 安装与运营

| 目标 | 文档 |
| --- | --- |
| 从安装到第一次对话 | [项目 README](../README.md#快速开始) |
| 了解能做什么、需要哪些外部服务 | [功能与使用边界](FEATURES.md) |
| 源码与镜像部署、接入 QQ、备份、恢复或排障 | [部署手册](DEPLOYMENT.md) |
| 手动升级、旧检出迁移和网页更新 | [升级与自动更新](UPDATES.md) |
| 查环境变量、界面设置或安全报告方式 | [环境变量示例](../deploy/env.example) · [设置分类](SETTINGS-TAXONOMY.md) · [安全报告](../SECURITY.md) |

## 开发与接口

| 目标 | 文档 |
| --- | --- |
| 搭建开发环境、跑测试、提交变更 | [贡献指南](../CONTRIBUTING.md) |
| 接入 HTTP API、核对数据结构 | [Web API](API-WEB-V1.md) · [v1 契约及示例](../contracts/examples/README.md) |
| 修改 Web 或移动端 | [Web 控制台](../clients/web-console/README.md) · [Flutter 客户端](../clients/mobile/README.md) |
| 理解架构取舍与运行规则 | [架构决策](adr/README.md) · [身份与多用户](IDENTITY-MULTIUSER-DESIGN.md) · [主动性](AUTONOMY-DESIGN.md) · [群聊轮询](GROUP-POLLING-DESIGN.md) |
| 跑浏览器验收 | [浏览器 E2E](../tests/e2e/README.md) |

## 验收记录

- [发布验收](evidence/release-0.0.9-acceptance.md)：按时间和提交记录构建、运行及未通过项。
- [一行首装与升级](evidence/single-command-install-upgrade.md)：隔离环境的安装、数据保留和回退边界。

操作步骤以部署和升级指南为入口；设计、调研与验收文件保留各自写作或测试时的上下文，不作为当前生产状态说明。
