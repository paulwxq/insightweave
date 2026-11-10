# 服务端版本基线（2026-11-09，探测脚本 /tmp/probe_services.py，经既有 .env 凭据机制，未输出密钥）

| 服务 | 地址(脱敏:host:port) | 实际版本 | 备注 |
| --- | --- | --- | --- |
| PostgreSQL (平台库) | 172.29.128.1:5432 / metaweave | PostgreSQL 18.1 (x86_64-windows) | pgvector 扩展未安装 |
| PostgreSQL (checkpoint 库) | 172.29.128.1:5432 / lg_persist | PostgreSQL 18.1 | pgvector 扩展未安装 |
| PostgreSQL (业务库) | 172.29.128.1:5432 / orders | PostgreSQL 18.1 | pgvector 扩展未安装；default_transaction_read_only 预期 on |
| Milvus | 172.29.128.1:19530 / db=nl2sql | v2.5.2 | pymilvus 兼容表对应 2.5.x；当前客户端 2.6.9（见正文决策） |
| Neo4j | bolt://172.29.128.1:7687 / neo4j | 5.26.19 | Python driver 6.x 支持 5.26+ |

- 后端 8002 当前空闲（用户已停止旧服务）；metaweave 8001/5173 为用户自启，不动。
- shell 环境存在 MILVUS_*/NEO4J_* 导出变量；测试运行不 source .env、不开 live 开关，与历史 1166 收集口径对照。
