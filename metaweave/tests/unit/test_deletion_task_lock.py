"""doc 8.1 §7.1 任务执行锁键：固定命名空间 SHA-256 前 8 字节有符号 bigint。"""

from uuid import UUID

from backend.deletion.task_lock import task_lock_key

TASK = UUID("12345678-1234-4123-8123-123456789abc")


def test_uuid_object_and_string_forms_agree():
    key_object = task_lock_key(TASK)
    key_lower = task_lock_key(str(TASK))
    key_upper = task_lock_key(str(TASK).upper())
    key_braced = task_lock_key("{" + str(TASK).upper() + "}")
    assert key_object == key_lower == key_upper == key_braced


def test_key_is_signed_64_bit_int():
    key = task_lock_key(TASK)
    assert isinstance(key, int)
    assert -(2**63) <= key < 2**63


def test_distinct_tasks_get_distinct_keys_deterministically():
    a = task_lock_key(UUID("00000000-0000-4000-8000-000000000001"))
    b = task_lock_key(UUID("00000000-0000-4000-8000-000000000002"))
    assert a != b
    # 确定性：同输入重复计算结果一致（跨调用/跨进程一致的基础）。
    assert a == task_lock_key("00000000-0000-4000-8000-000000000001")


def test_known_vector():
    """固定向量：命名空间前缀 + 规范小写 UUID，取前 8 字节大端有符号。"""
    from hashlib import sha256

    payload = ("metaweave:resource_deletion_task:v1:" + str(TASK)).encode("ascii")
    expected = int.from_bytes(sha256(payload).digest()[:8], "big", signed=True)
    assert task_lock_key(TASK) == expected


def test_ensure_lock_connection_alive_probes_and_raises_on_failure():
    from backend.deletion.task_lock import (
        TaskLockLostError,
        ensure_lock_connection_alive,
    )

    class GoodConn:
        def cursor(self):
            class Cur:
                def __enter__(self):
                    return self

                def __exit__(self, *_a):
                    return False

                def execute(self, sql, *_a):
                    assert sql == "SELECT 1"

                def fetchone(self):
                    return (1,)

            return Cur()

    class DeadConn:
        def cursor(self):
            raise RuntimeError("connection already closed")

    ensure_lock_connection_alive(GoodConn())  # 通过，不抛
    import pytest

    with pytest.raises(TaskLockLostError):
        ensure_lock_connection_alive(DeadConn())


def test_release_closes_connection_even_when_unlock_fails():
    from backend.deletion.task_lock import release

    class FailingUnlockConn:
        closed = False

        def cursor(self):
            class Cur:
                def __enter__(self):
                    return self

                def __exit__(self, *_a):
                    return False

                def execute(self, *_a):
                    raise RuntimeError("unlock failed on dead link")

            return Cur()

        def close(self):
            self.closed = True

    conn = FailingUnlockConn()
    import pytest

    with pytest.raises(RuntimeError):
        release(conn, 123)
    assert conn.closed is True
