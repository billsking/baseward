# 目录结构

[English](architecture.md) | [简体中文](architecture.zh-CN.md)

```
app/
  main.py            # 应用组装、生命周期、异常处理
  config.py          # GATEWAY_ 环境变量
  netip.py           # 来源 IP 归一化与白名单文本解析（纯函数）
  netallow.py        # 来源 IP 白名单的运行期真源：DB 优先、env 回落、TTL 缓存
  services.py        # 服务容器
  db/                # 管理库 ORM、建表、种子管理员
  security/          # AES-GCM/HKDF/bcrypt、管理员会话、Bearer 中间件
  policy/engine.py   # SQL AST 策略引擎
  policy/scope.py    # Token 表/字段授权校验与 SELECT * 改写
  masking/           # 自动/手动规则、AST 脱敏计划
  datasources/       # CRUD、连接测试、连接池；databases.py 为可访问库与库级策略的唯一入口
  tokens/            # Token 生命周期与四级授权范围模型
  audit/             # 审计写入/查询/清理
  mcp/               # FastMCP 工具、执行管线与元数据检索（introspect.py）
  admin/             # 管理端路由、模板、本地静态资源
migrations/          # 001 初始化 / 002 导出通道 / 003 凭证来源 IP 绑定 / 004 设置值放宽到 1024 / 005 多库可访问库清单与审计库字段
scripts/gen_master_key.py
tests/unit/          # 不依赖 MySQL
tests/integration/   # 需测试 MySQL
tests/perf/          # 性能压测脚本
```
