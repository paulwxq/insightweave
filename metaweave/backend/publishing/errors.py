"""发布域共享异常（doc 16 §5）。

`db_loader` 的提交函数与 `publish_service` 都要抛"步骤未就绪"，异常类
放在这个中立模块，避免 `db_loader` 反向依赖 `publish_service` 形成
循环 import；`publish_service` re-export 对外保持原引用路径。
"""


class PublicationStepNotReadyError(RuntimeError):
    """所选步骤最近一次采集尚未成功，或读取输入后已被重跑。"""


class JobNotPublishableError(PublicationStepNotReadyError):
    """采集作业存在 failed 或 running 的步骤行，整个 Job 不能进入正式发布（doc 20.1 §11.2）。

    继承“步骤未就绪”，使现有发布路径的透传分支同样适用；发布服务与 API 另有更具体的处理。
    """
