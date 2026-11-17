-- 只增量更新删除功能所需结构；由 scripts/upgrade_deletion_schema.py 在事务中执行。
-- 不删除/改名现有表，不修改现有作业、运行、画像或序列数据。
DO $$
BEGIN
    IF current_database() <> 'metaweave_rehearsal_final' THEN
        RAISE EXCEPTION '目标库必须为 metaweave_rehearsal_final';
    END IF;
    IF to_regclass('public.db_source_info') IS NULL
       OR to_regclass('public.workspace_info') IS NULL
       OR to_regclass('public.collection_job_info') IS NULL
       OR to_regclass('public.publish_job_info') IS NULL THEN
        RAISE EXCEPTION '缺少现有平台基础表，不能执行删除功能增量更新';
    END IF;
END $$;

-- 固定顺序锁定本次修改的主表；等待超时由执行器控制，避免无限阻塞。
LOCK TABLE public.db_source_info, public.workspace_info,
    public.collection_job_info, public.publish_job_info IN ACCESS EXCLUSIVE MODE;

ALTER TABLE public.db_source_info
    ADD COLUMN IF NOT EXISTS deleted_at timestamptz,
    ADD COLUMN IF NOT EXISTS deletion_state text NOT NULL DEFAULT 'active';
ALTER TABLE public.workspace_info
    ADD COLUMN IF NOT EXISTS deleted_at timestamptz,
    ADD COLUMN IF NOT EXISTS deletion_state text NOT NULL DEFAULT 'active';
ALTER TABLE public.collection_job_info
    ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
ALTER TABLE public.publish_job_info
    ADD COLUMN IF NOT EXISTS deleted_at timestamptz;

ALTER TABLE public.db_source_info
    ALTER COLUMN deletion_state SET DEFAULT 'active',
    ALTER COLUMN deletion_state SET NOT NULL,
    ALTER COLUMN host DROP NOT NULL,
    ALTER COLUMN port DROP NOT NULL,
    ALTER COLUMN db_name DROP NOT NULL,
    ALTER COLUMN username DROP NOT NULL,
    ALTER COLUMN password_ciphertext DROP NOT NULL,
    ALTER COLUMN selected_schemas DROP NOT NULL;
ALTER TABLE public.workspace_info
    ALTER COLUMN deletion_state SET DEFAULT 'active',
    ALTER COLUMN deletion_state SET NOT NULL;

-- 允许重复执行；在同一事务内重新校验这些新增 CHECK，不放宽现有字段合法性。
ALTER TABLE public.db_source_info
    DROP CONSTRAINT IF EXISTS db_source_info_deletion_state_check,
    DROP CONSTRAINT IF EXISTS db_source_info_deletion_state_time_check,
    DROP CONSTRAINT IF EXISTS db_source_info_connection_presence_check,
    ADD CONSTRAINT db_source_info_deletion_state_check
        CHECK (deletion_state IN ('active', 'deleting', 'delete_failed', 'deleted')),
    ADD CONSTRAINT db_source_info_deletion_state_time_check CHECK (
        (deleted_at IS NULL AND deletion_state IN ('active', 'deleting', 'delete_failed'))
        OR (deleted_at IS NOT NULL AND deletion_state = 'deleted')
    ),
    ADD CONSTRAINT db_source_info_connection_presence_check CHECK (
        (deleted_at IS NULL AND host IS NOT NULL AND port IS NOT NULL
            AND db_name IS NOT NULL AND username IS NOT NULL
            AND password_ciphertext IS NOT NULL AND selected_schemas IS NOT NULL)
        OR (deleted_at IS NOT NULL AND host IS NULL AND port IS NULL
            AND db_name IS NULL AND username IS NULL AND password_ciphertext IS NULL
            AND selected_schemas IS NULL AND sslmode IS NULL AND db_version IS NULL
            AND description IS NULL AND is_active = false AND deletion_state = 'deleted')
    );
ALTER TABLE public.workspace_info
    DROP CONSTRAINT IF EXISTS workspace_info_deletion_state_check,
    DROP CONSTRAINT IF EXISTS workspace_info_deletion_state_time_check,
    ADD CONSTRAINT workspace_info_deletion_state_check
        CHECK (deletion_state IN ('active', 'deleting', 'delete_failed', 'deleted')),
    ADD CONSTRAINT workspace_info_deletion_state_time_check CHECK (
        (deleted_at IS NULL AND deletion_state IN ('active', 'deleting', 'delete_failed'))
        OR (deleted_at IS NOT NULL AND deletion_state = 'deleted')
    );

CREATE UNIQUE INDEX IF NOT EXISTS workspace_info_active_name_idx
    ON public.workspace_info (source_id, workspace_name) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS collection_job_info_active_name_idx
    ON public.collection_job_info (workspace_id, job_name) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS publish_job_info_active_name_idx
    ON public.publish_job_info (workspace_id, job_name) WHERE deleted_at IS NULL;

ALTER TABLE public.workspace_info
    DROP CONSTRAINT IF EXISTS workspace_info_source_name_key;
ALTER TABLE public.collection_job_info
    DROP CONSTRAINT IF EXISTS collection_job_info_workspace_name_key;
ALTER TABLE public.publish_job_info
    DROP CONSTRAINT IF EXISTS publish_job_info_workspace_name_key;

CREATE TABLE IF NOT EXISTS public.resource_deletion_task (
    task_id uuid PRIMARY KEY,
    action text NOT NULL,
    target_id text NOT NULL,
    source_id text NOT NULL,
    workspace_id text,
    status text NOT NULL DEFAULT 'pending',
    step_results jsonb,
    error_message text,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    queued_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at timestamptz,
    finished_at timestamptz,
    requested_by_user_id uuid,
    CONSTRAINT resource_deletion_task_action_check
        CHECK (action IN ('delete_source', 'delete_workspace', 'clear_source_knowledge')),
    CONSTRAINT resource_deletion_task_status_check
        CHECK (status IN ('pending', 'running', 'failed', 'success')),
    CONSTRAINT resource_deletion_task_workspace_required_check
        CHECK (action != 'delete_workspace' OR workspace_id IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS resource_deletion_task_one_active_source_idx
    ON public.resource_deletion_task (source_id) WHERE status IN ('pending', 'running');

COMMENT ON COLUMN public.db_source_info.deleted_at IS '逻辑删除完成时间；有效源为 NULL';
COMMENT ON COLUMN public.db_source_info.deletion_state IS
    '删除状态 active/deleting/delete_failed/deleted，与 deleted_at 一致';
COMMENT ON COLUMN public.workspace_info.deleted_at IS '工作区逻辑删除完成时间';
COMMENT ON COLUMN public.workspace_info.deletion_state IS
    '删除状态 active/deleting/delete_failed/deleted，与 deleted_at 一致';
COMMENT ON COLUMN public.collection_job_info.deleted_at IS '采集作业逻辑删除完成时间';
COMMENT ON COLUMN public.publish_job_info.deleted_at IS '发布作业逻辑删除完成时间';
COMMENT ON TABLE public.resource_deletion_task IS
    '数据源/工作区删除及知识清理任务，同源 pending/running 至多一条';
COMMENT ON COLUMN public.resource_deletion_task.queued_at IS
    '最近一次入队的数据库时间，重试时显式刷新';
