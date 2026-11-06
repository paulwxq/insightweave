-- doc 10.2 §8：发布并行安全前置的增量 schema 变更（幂等，可重复执行）。
-- 1) publish_run_info 新增 execution_plan JSONB（旧记录 NULL = legacy serial）；
-- 2) 新增 publish_target_guard 目标保留表（发布与删除统一使用）。
-- 不修改数据行；沿用 upgrade_deletion_schema 的事务内 --apply 模式。

ALTER TABLE public.publish_run_info
    ADD COLUMN IF NOT EXISTS execution_plan jsonb;

CREATE TABLE IF NOT EXISTS public.publish_target_guard (
    target_key text PRIMARY KEY,
    owner_kind text NOT NULL,
    owner_id text NOT NULL,
    reserved_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT publish_target_guard_owner_kind_check
        CHECK (owner_kind IN ('publish_run', 'deletion_task'))
);

CREATE INDEX IF NOT EXISTS publish_target_guard_owner_idx
    ON public.publish_target_guard (owner_kind, owner_id);

COMMENT ON TABLE public.publish_target_guard IS
    'Neo4j 目标保留记录（doc 10.2 §5.1）：目标 advisory lock 失锁/崩溃后的持久占位，遗留记录对任何重新进入的执行尝试一律生效，人工核验后清除';
