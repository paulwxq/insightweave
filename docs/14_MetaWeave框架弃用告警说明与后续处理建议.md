# 14. MetaWeave 框架弃用告警说明与后续处理建议

记录日期：2026-11-18。

状态：已核实告警来源，暂缓代码调整。本次只记录事实和后续处理建议。

## 1. 背景与当前结论

在提交 `e70391fd4e92e035e3dcf7b50048c6c67d97b39c` 的[自动发布流水线](https://github.com/paulwxq/insightweave/actions/runs/44232946662)中，MetaWeave 离线测试结果为：

```text
2716 passed, 28 skipped, 4 xfailed, 259 warnings
```

本次流水线成功，部署后的应用容器健康，浏览器基础读取验收通过。这些弃用告警没有造成本次测试或发布失败；上述验证也不能证明全部业务场景都已覆盖。

“弃用”（deprecated）表示框架目前仍支持这种接口，但建议逐步迁移到替代接口。后续升级依赖时，需要重新检查兼容性；本次没有查到必须立即迁移的移除期限。

**259 是本次测试汇总的警告次数，包含重复提示，不代表 259 个独立问题，也不代表服务启动了 259 次或执行了 259 次恢复扫描。**目前按兼容性维护事项记录，不为消除日志而立即重构。

## 2. 259 条警告的组成

根据本次流水线的 warnings summary，各项计数如下：

| 来源 | 本次次数 | 说明 |
| --- | ---: | --- |
| `backend/api/main.py:50` 注册启动事件 | 129 | 测试多次创建应用时，调用已弃用的 `FastAPI.on_event()` |
| FastAPI 内部 `applications.py` 转调路由注册 | 129 | 同一次应用层注册继续调用已弃用的 `APIRouter.on_event()`，产生第二条提示 |
| Starlette 测试客户端导入 | 1 | 当前测试环境使用 `httpx` 兼容路径，提示后续采用 `httpx2` |
| 合计 | **259** | **129 + 129 + 1** |

两组 129 次分别按测试文件汇总，涉及资产目录、数据源、工作区、作业和部署门闩等测试。相同的应用工厂在这些测试中被多次调用，同一处旧接口因而反复发出警告。

`on_event` 的提示在**注册事件处理函数时**产生。即使测试通过 `METAWEAVE_DELETION_STARTUP_SCAN=0` 关闭扫描执行，应用工厂仍会注册该处理函数，因此仍可能产生弃用提示。不能据此判断关闭开关失效。

上述数字仅对应本次测试及其警告过滤设置；测试数量、框架版本或过滤规则变化后，次数可以变化。以后应按告警来源判断，不能把“必须恰好 259 条”作为验收条件。

## 3. 启动事件告警及 FastAPI 官方建议

### 3.1 当前项目的写法

[MetaWeave 应用工厂](../metaweave/backend/api/main.py)目前通过以下装饰器注册启动恢复扫描：

```python
@app.on_event("startup")
def _deletion_startup_scan() -> None:
    ...
```

该函数在应用启动时调用 `backend.deletion.service.startup_scan(settings)`，用于修复删除任务的遗留状态。当前约定包括：

- `METAWEAVE_DELETION_STARTUP_SCAN=0` 时不执行扫描。
- 扫描只修复任务状态，不自动执行实际资源清理。
- 扫描异常记录日志，不阻断应用启动。

本次告警针对的是**注册启动逻辑的接口写法**，没有证据表明删除任务恢复逻辑本身因该告警失效。

### 3.2 官方建议

FastAPI 官方已将旧的 startup/shutdown 事件注册方式标为弃用，建议通过 `FastAPI(lifespan=...)` 管理应用启动和退出操作。[官方 Lifespan Events 文档](https://fastapi.tiangolo.com/advanced/events/#alternative-events-deprecated)

官方同时明确：配置 `lifespan` 后，旧的 startup/shutdown 事件处理函数不会再执行。因此以后迁移时，应把恢复扫描完整接入新的启动流程，不能只增加 `lifespan` 参数而把原处理函数留在旧装饰器下。

## 4. 测试客户端告警与上游提示

本次测试另有 1 条 `StarletteDeprecationWarning`：当前测试客户端使用 `httpx`，上游提示采用 `httpx2`。

核查时本地 MetaWeave 环境为 FastAPI `0.141.1`、Starlette `1.6.0`、httpx `0.28.1`，未安装 httpx2。Starlette `1.6.0` 的[官方源码](https://github.com/Kludex/starlette/blob/1.6.0/starlette/testclient.py)会优先导入 `httpx2`；缺少该包时兼容导入 `httpx`，并发出上述提示。这与当前测试仍能通过相符。

该提示来自测试客户端依赖，不意味着 MetaWeave 业务 API 请求已失败。后续处理应先核实目标 Starlette/FastAPI 版本和测试依赖的兼容性，再调整开发依赖及锁文件。仅凭一句提示直接安装新包，不能视为已经完成兼容性验证。

## 5. 后续处理建议

当前建议保留记录，待相关依赖升级或安排兼容性维护时处理。无需新增模块、发布服务或生命周期管理框架。

| 事项 | 建议落点与处理方式 |
| --- | --- |
| 启动事件迁移 | 修改 `metaweave/backend/api/main.py`，将现有恢复扫描接入 `lifespan`，保留开关、异常处理和启动执行时机 |
| 启动行为回归 | 检查并补充 `metaweave/tests/unit/test_deletion_service.py`、`test_deploy_gate_wiring.py` 等相关测试；涉及启动生命周期的测试须真正进入并退出应用生命周期，不能只创建客户端就宣称启动逻辑已覆盖 |
| 测试客户端兼容 | 按实际兼容性需要调整 `metaweave/pyproject.toml` 的开发依赖和 `metaweave/uv.lock`，不顺带升级整套依赖 |

届时验证重点为：启用时扫描按预期执行、关闭时不执行、异常不阻断启动、不新增实际清理动作，并保持部署门闩及现有 API 行为正常。执行受影响测试和现有 MetaWeave 受维护离线套件，对比告警来源是否消除。

不建议通过全局屏蔽弃用警告掩盖该事项。迁移完成的依据应是行为回归通过、对应旧接口已替换，而非仅让日志不再显示告警。

## 6. 本次记录的边界

本次只新增本文档，未修改应用、依赖、测试或发布脚本，未触发新的部署。真实业务全链路、失败回退及备份恢复验收仍按 [doc 13](13_项目Docker容器化与本地CI_CD发布设计.md) 的范围分别安排，不能用这次告警说明替代。
