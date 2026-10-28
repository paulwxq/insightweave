-- =====================================================================
-- MetaWeave 平台库目标结构（doc 6.1 §1–§9、§13–§18、§12 视图）
--
-- 本文件只包含「新对象」的静态 DDL（序列、十五张表、索引、视图）。
-- 旧表改名（<原表名>_old_<yyyyMMddHHmmss>）及其冲突索引/约束/序列/视图的
-- 处理由 scripts/rebuild_platform_schema.py 在同一事务中先生成并执行；
-- 全新空库没有旧对象时本文件可直接使用（doc 6.1 §10.0）。
--
-- 执行顺序：数据源 → 工作区 → 采集作业/版本 → 采集运行/步骤 →
--   发布作业/版本 → 发布运行/步骤 → 五张内容表 → 视图。
-- =====================================================================

-- ---------------------------------------------------------------------
-- 序列（短 ID 生成；OWNED BY 各自主键列）
-- ---------------------------------------------------------------------

CREATE SEQUENCE public.db_source_info_id_seq AS bigint START WITH 1;
CREATE SEQUENCE public.workspace_info_id_seq AS bigint START WITH 1;
CREATE SEQUENCE public.collection_job_info_id_seq AS bigint START WITH 1;
CREATE SEQUENCE public.publish_job_info_id_seq AS bigint START WITH 1;

-- ---------------------------------------------------------------------
-- 数据源（doc 6.1 §13）
-- ---------------------------------------------------------------------

CREATE TABLE public.db_source_info (
    source_id text PRIMARY KEY
        DEFAULT ('db_src_' || nextval('public.db_source_info_id_seq')),
    display_name text NOT NULL,
    description text,
    db_type text NOT NULL,
    host text NOT NULL,
    port integer NOT NULL,
    db_name text NOT NULL,
    username text NOT NULL,
    password_ciphertext text NOT NULL,
    sslmode text,
    db_version text,
    selected_schemas text[] NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_by_user_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT db_source_info_id_format
        CHECK (source_id ~ '^db_src_[1-9][0-9]*$'),
    CONSTRAINT db_source_info_name_nonblank
        CHECK (length(btrim(display_name)) > 0),
    CONSTRAINT db_source_info_type_check
        CHECK (db_type = 'postgresql'),
    CONSTRAINT db_source_info_host_nonblank
        CHECK (length(btrim(host)) > 0),
    CONSTRAINT db_source_info_port_check
        CHECK (port BETWEEN 1 AND 65535),
    CONSTRAINT db_source_info_db_name_nonblank
        CHECK (length(btrim(db_name)) > 0),
    CONSTRAINT db_source_info_username_nonblank
        CHECK (length(btrim(username)) > 0),
    CONSTRAINT db_source_info_sslmode_check
        CHECK (
            sslmode IS NULL
            OR sslmode IN (
                'disable', 'allow', 'prefer',
                'require', 'verify-ca', 'verify-full'
            )
        ),
    CONSTRAINT db_source_info_schemas_nonempty
        CHECK (cardinality(selected_schemas) > 0),
    CONSTRAINT db_source_info_schemas_no_null
        CHECK (array_position(selected_schemas, NULL) IS NULL)
);

ALTER SEQUENCE public.db_source_info_id_seq
    OWNED BY public.db_source_info.source_id;

CREATE INDEX db_source_info_connection_idx
    ON public.db_source_info (host, port, db_name, username);

COMMENT ON TABLE public.db_source_info IS
    '数据库数据源登记：保存连接、凭据密文及受管 Schema 范围；source_id 使用序列生成的 text 短 ID';
COMMENT ON COLUMN public.db_source_info.source_id IS
    '数据源主键，text 类型；由独立序列生成 db_src_1 等值，创建后保持不变；不使用 UUID';
COMMENT ON COLUMN public.db_source_info.display_name IS
    '数据源显示名称，不能为空；不要求全局唯一';
COMMENT ON COLUMN public.db_source_info.description IS
    '可选数据源说明；可作为 Domain 首次生成的兜底输入，不随工作区 YAML 编辑或重跑自动更新';
COMMENT ON COLUMN public.db_source_info.db_type IS
    '数据库类型，当前固定为 postgresql';
COMMENT ON COLUMN public.db_source_info.host IS
    '源数据库连接地址，保留合法输入值，不是平台库地址';
COMMENT ON COLUMN public.db_source_info.port IS
    '源数据库端口，范围 1 至 65535';
COMMENT ON COLUMN public.db_source_info.db_name IS
    '源数据库名称，保留合法名称中的空格和大小写';
COMMENT ON COLUMN public.db_source_info.username IS
    '源数据库连接用户名，明文保存，保留合法名称中的空格和大小写';
COMMENT ON COLUMN public.db_source_info.password_ciphertext IS
    '应用层 Fernet 加密后的源库密码；不保存密码明文或密钥';
COMMENT ON COLUMN public.db_source_info.sslmode IS
    '可选 libpq SSL 模式，规范化后保存；为空时建连不传此选项，数据库不设默认值';
COMMENT ON COLUMN public.db_source_info.db_version IS
    '最近一次成功登记或修改连接时读取的数据库版本，可为空；不是每次查询实时探测值';
COMMENT ON COLUMN public.db_source_info.selected_schemas IS
    '受管 Schema 名称数组，不能为空且不得含 NULL；名称合法性、重复检查和源库权限由服务校验';
COMMENT ON COLUMN public.db_source_info.is_active IS
    '数据源是否启用，登记默认启用；按既有规则限制禁用来源的采集和发布';
COMMENT ON COLUMN public.db_source_info.created_by_user_id IS
    '登记创建人 UUID；当前没有登录身份时为空，不新增用户表外键';
COMMENT ON COLUMN public.db_source_info.created_at IS
    '数据源登记时间，由插入默认值设置';
COMMENT ON COLUMN public.db_source_info.updated_at IS
    '数据源信息或启停状态最后修改时间；插入时默认 now()，后续由应用显式更新';
COMMENT ON INDEX public.db_source_info_connection_idx IS
    '连接四元组的非唯一查重索引；支持提示重复登记，不禁止确认后登记相同连接';

-- ---------------------------------------------------------------------
-- 工作区（doc 6.1 §1）
-- ---------------------------------------------------------------------

CREATE TABLE public.workspace_info (
    workspace_id text PRIMARY KEY
        DEFAULT ('ws_' || nextval('public.workspace_info_id_seq')),
    source_id text NOT NULL,
    workspace_name text NOT NULL,
    workspace_desc text,
    created_by_user_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT workspace_info_id_format
        CHECK (workspace_id ~ '^ws_[1-9][0-9]*$'),
    CONSTRAINT workspace_info_source_nonblank
        CHECK (length(btrim(source_id)) > 0),
    CONSTRAINT workspace_info_name_nonblank
        CHECK (length(btrim(workspace_name)) > 0),
    CONSTRAINT workspace_info_source_name_key
        UNIQUE (source_id, workspace_name)
);

ALTER SEQUENCE public.workspace_info_id_seq
    OWNED BY public.workspace_info.workspace_id;

COMMENT ON TABLE public.workspace_info IS
    '工作区：归属于一个数据源，隔离并复用该工作区内的采集产物；可先于作业和运行创建';
COMMENT ON COLUMN public.workspace_info.workspace_id IS
    '工作区全局唯一短 ID，格式如 ws_1；由独立序列生成';
COMMENT ON COLUMN public.workspace_info.source_id IS
    '所属数据源 ID，如 db_src_1；未来可引用其他类型的数据源';
COMMENT ON COLUMN public.workspace_info.workspace_name IS
    '工作区名称，同一数据源下唯一，如生产或测试';
COMMENT ON COLUMN public.workspace_info.workspace_desc IS
    '工作区用途说明';
COMMENT ON COLUMN public.workspace_info.created_by_user_id IS
    '创建人用户 UUID；当前没有用户身份时可为空';
COMMENT ON COLUMN public.workspace_info.created_at IS '工作区创建时间';
COMMENT ON COLUMN public.workspace_info.updated_at IS
    '工作区最后修改时间；由应用在更新名称或说明时写入';

-- ---------------------------------------------------------------------
-- 采集作业与配置版本（doc 6.1 §2–§3）
-- ---------------------------------------------------------------------

CREATE TABLE public.collection_job_info (
    collection_job_id text PRIMARY KEY
        DEFAULT ('coll_job_' || nextval('public.collection_job_info_id_seq')),
    workspace_id text NOT NULL
        REFERENCES public.workspace_info (workspace_id),
    job_name text NOT NULL,
    job_desc text,
    created_by_user_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT collection_job_info_id_format
        CHECK (collection_job_id ~ '^coll_job_[1-9][0-9]*$'),
    CONSTRAINT collection_job_info_name_nonblank
        CHECK (length(btrim(job_name)) > 0),
    CONSTRAINT collection_job_info_workspace_name_key
        UNIQUE (workspace_id, job_name)
);

ALTER SEQUENCE public.collection_job_info_id_seq
    OWNED BY public.collection_job_info.collection_job_id;

COMMENT ON TABLE public.collection_job_info IS
    '采集作业的稳定身份与基本信息；实际执行配置保存在 collection_job_revision';
COMMENT ON COLUMN public.collection_job_info.collection_job_id IS
    '采集作业全局唯一短 ID，如 coll_job_1；修改配置时保持不变';
COMMENT ON COLUMN public.collection_job_info.workspace_id IS
    '所属工作区；数据源经 workspace_info 查询，创建后不得更换';
COMMENT ON COLUMN public.collection_job_info.job_name IS
    '采集作业名称，同一工作区内唯一';
COMMENT ON COLUMN public.collection_job_info.job_desc IS
    '采集作业的用途说明，不作为步骤运行参数';
COMMENT ON COLUMN public.collection_job_info.created_by_user_id IS
    '作业创建人 UUID；当前没有用户身份时可为空';
COMMENT ON COLUMN public.collection_job_info.created_at IS '作业创建时间';
COMMENT ON COLUMN public.collection_job_info.updated_at IS
    '作业信息或执行配置最后保存时间；由应用在更新时写入';

CREATE TABLE public.collection_job_revision (
    collection_job_id text NOT NULL
        REFERENCES public.collection_job_info (collection_job_id),
    revision_no integer NOT NULL,
    steps text[] NOT NULL,
    step_config jsonb,
    change_note text,
    created_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT collection_job_revision_pkey
        PRIMARY KEY (collection_job_id, revision_no),
    CONSTRAINT collection_job_revision_no_positive
        CHECK (revision_no > 0),
    CONSTRAINT collection_job_revision_steps_nonempty
        CHECK (cardinality(steps) > 0),
    CONSTRAINT collection_job_revision_steps_no_null
        CHECK (array_position(steps, NULL) IS NULL),
    CONSTRAINT collection_job_revision_steps_allowed
        CHECK (steps <@ ARRAY[
            'db', 'ddl', 'json', 'md',
            'domain', 'dim', 'rel', 'sql'
        ]::text[]),
    CONSTRAINT collection_job_revision_config_object
        CHECK (
            step_config IS NULL
            OR jsonb_typeof(step_config) = 'object'
        )
);

COMMENT ON TABLE public.collection_job_revision IS
    '采集作业不可变的默认执行配置；同一作业按 revision_no 递增保存历史版本';
COMMENT ON COLUMN public.collection_job_revision.collection_job_id IS
    '所属采集作业 ID；与 revision_no 组成联合主键';
COMMENT ON COLUMN public.collection_job_revision.revision_no IS
    '作业内从 1 开始递增的配置版本号';
COMMENT ON COLUMN public.collection_job_revision.steps IS
    '该版本允许执行的采集步骤；运行时可选其子集，后端按依赖关系拓扑排序';
COMMENT ON COLUMN public.collection_job_revision.step_config IS
    '各步骤的默认运行参数；无额外参数时为 SQL NULL；当前可保存 domain.description 作为首次生成输入，不保存工作区 YAML 内容';
COMMENT ON COLUMN public.collection_job_revision.change_note IS
    '本次配置变更说明，可为空';
COMMENT ON COLUMN public.collection_job_revision.created_at IS
    '该配置版本创建时间；版本不原地更新，因此不设 updated_at';

-- ---------------------------------------------------------------------
-- 采集运行与步骤（doc 6.1 §4–§5）
-- ---------------------------------------------------------------------

CREATE TABLE public.collection_run_info (
    collection_run_id uuid PRIMARY KEY,
    collection_job_id text NOT NULL,
    revision_no integer NOT NULL,
    trigger_type text NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    run_by_user_id uuid,
    message text,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,

    CONSTRAINT collection_run_info_revision_fk
        FOREIGN KEY (collection_job_id, revision_no)
        REFERENCES public.collection_job_revision
            (collection_job_id, revision_no),
    CONSTRAINT collection_run_info_trigger_type_check
        CHECK (trigger_type IN ('manual', 'scheduled')),
    CONSTRAINT collection_run_info_status_check
        CHECK (status IN (
            'pending', 'running', 'success', 'partial_success', 'failed'
        )),
    CONSTRAINT collection_run_info_time_order_check
        CHECK (
            started_at IS NULL OR finished_at IS NULL
            OR finished_at >= started_at
        )
);

CREATE INDEX collection_run_info_job_created_idx
    ON public.collection_run_info (collection_job_id, created_at DESC);

COMMENT ON TABLE public.collection_run_info IS
    '一次采集执行一行；逐步骤执行结果保存在 collection_run_step';
COMMENT ON COLUMN public.collection_run_info.collection_run_id IS
    '本次采集执行的 UUID；每次手工或定时执行均生成新值';
COMMENT ON COLUMN public.collection_run_info.collection_job_id IS
    '所属采集作业 ID；与 revision_no 一起定位本次采用的配置版本';
COMMENT ON COLUMN public.collection_run_info.revision_no IS
    '运行启动时确定的采集作业版本号；运行期间不随作业修改而变化';
COMMENT ON COLUMN public.collection_run_info.trigger_type IS
    '触发方式：manual 手工或 scheduled 定时';
COMMENT ON COLUMN public.collection_run_info.status IS
    '整体状态：pending 待执行、running 执行中、success 全部成功、partial_success 部分成功、failed 结束且没有步骤成功';
COMMENT ON COLUMN public.collection_run_info.run_by_user_id IS
    '手工执行的发起人 UUID；定时运行可为空';
COMMENT ON COLUMN public.collection_run_info.message IS
    '无法归属某个步骤的运行消息或失败原因';
COMMENT ON COLUMN public.collection_run_info.created_at IS
    '运行记录创建时间，可早于实际开始时间';
COMMENT ON COLUMN public.collection_run_info.started_at IS
    '本次运行实际开始执行的时间，尚未开始时为空';
COMMENT ON COLUMN public.collection_run_info.finished_at IS
    '本次运行结束时间，尚未结束时为空';

CREATE TABLE public.collection_run_step (
    collection_run_id uuid NOT NULL
        REFERENCES public.collection_run_info (collection_run_id),
    step_name text NOT NULL,
    step_config jsonb,
    status text NOT NULL DEFAULT 'pending',
    message text,
    input_manifest jsonb,
    output_manifest jsonb,
    started_at timestamptz,
    finished_at timestamptz,

    CONSTRAINT collection_run_step_pkey
        PRIMARY KEY (collection_run_id, step_name),
    CONSTRAINT collection_run_step_name_check
        CHECK (step_name IN (
            'db', 'ddl', 'json', 'md',
            'domain', 'dim', 'rel', 'sql'
        )),
    CONSTRAINT collection_run_step_status_check
        CHECK (status IN (
            'pending', 'running', 'success', 'failed', 'blocked'
        )),
    CONSTRAINT collection_run_step_config_object_check
        CHECK (
            step_config IS NULL
            OR jsonb_typeof(step_config) = 'object'
        ),
    CONSTRAINT collection_run_step_time_order_check
        CHECK (
            started_at IS NULL OR finished_at IS NULL
            OR finished_at >= started_at
        )
);

COMMENT ON TABLE public.collection_run_step IS
    '一次采集运行中每个已选步骤的一行记录；未选步骤不建行';
COMMENT ON COLUMN public.collection_run_step.collection_run_id IS
    '所属采集运行 UUID；与 step_name 组成联合主键';
COMMENT ON COLUMN public.collection_run_step.step_name IS
    '本次选中的采集步骤，须属于运行引用的作业版本允许的步骤';
COMMENT ON COLUMN public.collection_run_step.step_config IS
    '本次实际生效的步骤参数；无额外参数时为 SQL NULL，不表示继承默认参数';
COMMENT ON COLUMN public.collection_run_step.status IS
    '步骤状态：pending、running、success、failed 或 blocked；blocked 未执行，不写结果标志文件';
COMMENT ON COLUMN public.collection_run_step.message IS
    '该步骤的执行结果、失败或阻断原因';
COMMENT ON COLUMN public.collection_run_step.input_manifest IS
    '可空；实际读取的工作区文件清单，每项记录相对路径及所在目录 marker 对应的 collection_run_id；忽略点号开头的文件和目录';
COMMENT ON COLUMN public.collection_run_step.output_manifest IS
    '可空；本采集步骤成功生成并通过检查的文件相对路径清单；忽略点号开头的文件和目录';
COMMENT ON COLUMN public.collection_run_step.started_at IS
    '该步骤实际启动时间，未启动时为空';
COMMENT ON COLUMN public.collection_run_step.finished_at IS
    '该步骤完成或被阻断的时间，待执行或执行中时为空';

-- ---------------------------------------------------------------------
-- 发布作业与配置版本（doc 6.1 §6–§7）
-- ---------------------------------------------------------------------

CREATE TABLE public.publish_job_info (
    publish_job_id text PRIMARY KEY
        DEFAULT ('pub_job_' || nextval('public.publish_job_info_id_seq')),
    workspace_id text NOT NULL
        REFERENCES public.workspace_info (workspace_id),
    job_name text NOT NULL,
    job_desc text,
    created_by_user_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT publish_job_info_id_format
        CHECK (publish_job_id ~ '^pub_job_[1-9][0-9]*$'),
    CONSTRAINT publish_job_info_name_nonblank
        CHECK (length(btrim(job_name)) > 0),
    CONSTRAINT publish_job_info_workspace_name_key
        UNIQUE (workspace_id, job_name)
);

ALTER SEQUENCE public.publish_job_info_id_seq
    OWNED BY public.publish_job_info.publish_job_id;

COMMENT ON TABLE public.publish_job_info IS
    '发布作业的稳定身份与基本信息；默认发布配置保存在 publish_job_revision';
COMMENT ON COLUMN public.publish_job_info.publish_job_id IS
    '发布作业全局唯一短 ID，如 pub_job_1；修改配置时保持不变';
COMMENT ON COLUMN public.publish_job_info.workspace_id IS
    '所属工作区；发布时读取该工作区通过校验的产物，创建后不得更换';
COMMENT ON COLUMN public.publish_job_info.job_name IS
    '发布作业名称，同一工作区内唯一';
COMMENT ON COLUMN public.publish_job_info.job_desc IS
    '发布作业的用途说明，不作为步骤运行参数';
COMMENT ON COLUMN public.publish_job_info.created_by_user_id IS
    '作业创建人 UUID；当前没有用户身份时可为空';
COMMENT ON COLUMN public.publish_job_info.created_at IS '作业创建时间';
COMMENT ON COLUMN public.publish_job_info.updated_at IS
    '作业信息或默认发布配置最后保存时间；由应用在更新时写入';

CREATE TABLE public.publish_job_revision (
    publish_job_id text NOT NULL
        REFERENCES public.publish_job_info (publish_job_id),
    revision_no integer NOT NULL,
    steps text[] NOT NULL,
    step_config jsonb,
    change_note text,
    created_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT publish_job_revision_pkey
        PRIMARY KEY (publish_job_id, revision_no),
    CONSTRAINT publish_job_revision_no_positive
        CHECK (revision_no > 0),
    CONSTRAINT publish_job_revision_steps_nonempty
        CHECK (cardinality(steps) > 0),
    CONSTRAINT publish_job_revision_steps_no_null
        CHECK (array_position(steps, NULL) IS NULL),
    CONSTRAINT publish_job_revision_steps_allowed
        CHECK (steps <@ ARRAY[
            'db', 'ddl', 'json', 'domain',
            'table_schema', 'dim_value', 'rel', 'sql'
        ]::text[]),
    CONSTRAINT publish_job_revision_config_object
        CHECK (
            step_config IS NULL
            OR jsonb_typeof(step_config) = 'object'
        )
);

COMMENT ON TABLE public.publish_job_revision IS
    '发布作业不可变的默认执行配置；同一作业按 revision_no 递增保存历史版本';
COMMENT ON COLUMN public.publish_job_revision.publish_job_id IS
    '所属发布作业 ID；与 revision_no 组成联合主键';
COMMENT ON COLUMN public.publish_job_revision.revision_no IS
    '作业内从 1 开始递增的配置版本号';
COMMENT ON COLUMN public.publish_job_revision.steps IS
    '该版本允许发布的步骤；默认运行执行全部，手工运行可选择子集';
COMMENT ON COLUMN public.publish_job_revision.step_config IS
    '各步骤默认发布参数；无额外参数时为 SQL NULL，不保存工作区 domain、rel、dim_value 等产物文件内容';
COMMENT ON COLUMN public.publish_job_revision.change_note IS
    '本次默认发布配置变更说明，可为空';
COMMENT ON COLUMN public.publish_job_revision.created_at IS
    '该配置版本创建时间；版本不原地更新，因此不设 updated_at';

-- ---------------------------------------------------------------------
-- 发布运行与步骤（doc 6.1 §8–§9）
-- ---------------------------------------------------------------------

CREATE TABLE public.publish_run_info (
    publish_run_id uuid PRIMARY KEY,
    publish_job_id text NOT NULL,
    revision_no integer NOT NULL,
    trigger_type text NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    run_by_user_id uuid,
    message text,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,

    CONSTRAINT publish_run_info_revision_fk
        FOREIGN KEY (publish_job_id, revision_no)
        REFERENCES public.publish_job_revision
            (publish_job_id, revision_no),
    CONSTRAINT publish_run_info_trigger_type_check
        CHECK (trigger_type IN ('manual', 'scheduled')),
    CONSTRAINT publish_run_info_status_check
        CHECK (status IN (
            'pending', 'running', 'success', 'partial_success', 'failed'
        )),
    CONSTRAINT publish_run_info_time_order_check
        CHECK (
            started_at IS NULL OR finished_at IS NULL
            OR finished_at >= started_at
        )
);

CREATE INDEX publish_run_info_job_created_idx
    ON public.publish_run_info (publish_job_id, created_at DESC);

COMMENT ON TABLE public.publish_run_info IS
    '一次发布执行一行；逐步骤发布结果保存在 publish_run_step';
COMMENT ON COLUMN public.publish_run_info.publish_run_id IS
    '本次发布执行的 UUID；每次手工或定时发布均生成新值';
COMMENT ON COLUMN public.publish_run_info.publish_job_id IS
    '所属发布作业 ID；与 revision_no 一起定位本次采用的配置版本';
COMMENT ON COLUMN public.publish_run_info.revision_no IS
    '运行启动时确定的发布作业版本号；运行期间不随作业修改而变化';
COMMENT ON COLUMN public.publish_run_info.trigger_type IS
    '触发方式：manual 手工或 scheduled 定时';
COMMENT ON COLUMN public.publish_run_info.status IS
    '整体状态：pending 待执行、running 执行中、success 全部成功、partial_success 部分成功、failed 结束且没有步骤成功';
COMMENT ON COLUMN public.publish_run_info.run_by_user_id IS
    '手工发布的发起人 UUID；定时运行可为空';
COMMENT ON COLUMN public.publish_run_info.message IS
    '无法归属某个步骤的发布消息或失败原因';
COMMENT ON COLUMN public.publish_run_info.created_at IS
    '发布运行记录创建时间，可早于实际开始时间';
COMMENT ON COLUMN public.publish_run_info.started_at IS
    '本次发布实际开始执行的时间，尚未开始时为空';
COMMENT ON COLUMN public.publish_run_info.finished_at IS
    '本次发布结束时间，尚未结束时为空';

CREATE TABLE public.publish_run_step (
    publish_run_id uuid NOT NULL
        REFERENCES public.publish_run_info (publish_run_id),
    step_name text NOT NULL,
    step_config jsonb,
    status text NOT NULL DEFAULT 'pending',
    message text,
    input_manifest jsonb,
    target_manifest jsonb,
    started_at timestamptz,
    finished_at timestamptz,

    CONSTRAINT publish_run_step_pkey
        PRIMARY KEY (publish_run_id, step_name),
    CONSTRAINT publish_run_step_name_check
        CHECK (step_name IN (
            'db', 'ddl', 'json', 'domain',
            'table_schema', 'dim_value', 'rel', 'sql'
        )),
    CONSTRAINT publish_run_step_status_check
        CHECK (status IN (
            'pending', 'running', 'success', 'failed', 'blocked'
        )),
    CONSTRAINT publish_run_step_config_object_check
        CHECK (
            step_config IS NULL
            OR jsonb_typeof(step_config) = 'object'
        ),
    CONSTRAINT publish_run_step_time_order_check
        CHECK (
            started_at IS NULL OR finished_at IS NULL
            OR finished_at >= started_at
        )
);

COMMENT ON TABLE public.publish_run_step IS
    '一次发布运行中每个已选步骤的一行记录；未选步骤不建行';
COMMENT ON COLUMN public.publish_run_step.publish_run_id IS
    '所属发布运行 UUID；与 step_name 组成联合主键';
COMMENT ON COLUMN public.publish_run_step.step_name IS
    '本次选中的发布步骤，须属于运行引用的作业版本允许的步骤';
COMMENT ON COLUMN public.publish_run_step.step_config IS
    '本次实际生效的发布参数；无额外参数时为 SQL NULL，不表示继承默认参数';
COMMENT ON COLUMN public.publish_run_step.status IS
    '步骤状态：pending、running、success、failed 或 blocked；blocked 未执行；发布结果仅入库，不生成发布 marker';
COMMENT ON COLUMN public.publish_run_step.message IS
    '该步骤的发布结果、失败或阻断原因';
COMMENT ON COLUMN public.publish_run_step.input_manifest IS
    '可空；实际读取的工作区文件清单，每项记录相对路径及所在目录 marker 对应的 collection_run_id；忽略点号开头的文件和目录';
COMMENT ON COLUMN public.publish_run_step.target_manifest IS
    '可空；本步骤实际写入或尝试写入的 PostgreSQL、Milvus 或 Neo4j 等下游目标清单；成功与否由步骤 status 表达';
COMMENT ON COLUMN public.publish_run_step.started_at IS
    '该步骤实际启动时间，未启动时为空';
COMMENT ON COLUMN public.publish_run_step.finished_at IS
    '该步骤完成或被阻断的时间，待执行或执行中时为空';

-- ---------------------------------------------------------------------
-- 五张已发布内容表（doc 6.1 §14–§18）
-- ---------------------------------------------------------------------

CREATE TABLE public.db_profile (
    source_id text PRIMARY KEY,
    publish_run_id uuid NOT NULL,
    schema_version integer NOT NULL,
    collected_at timestamptz NOT NULL,
    db_type text NOT NULL,
    host text NOT NULL,
    port integer NOT NULL,
    db_name text NOT NULL,
    server_version text,
    server_version_full text,
    os_platform text,
    size_bytes bigint NOT NULL,
    size_scope text NOT NULL,
    record_count bigint,
    selected_schemas text[] NOT NULL,
    schemas jsonb NOT NULL,
    ordinary_table_count bigint NOT NULL,
    partitioned_table_count bigint NOT NULL,
    partition_child_count bigint NOT NULL,
    view_count bigint NOT NULL,
    materialized_view_count bigint NOT NULL,
    object_count bigint NOT NULL,

    CONSTRAINT db_profile_publish_run_fk
        FOREIGN KEY (publish_run_id)
        REFERENCES public.publish_run_info (publish_run_id),
    CONSTRAINT db_profile_port_check
        CHECK (port BETWEEN 1 AND 65535),
    CONSTRAINT db_profile_size_check
        CHECK (size_bytes >= 0)
);

COMMENT ON TABLE public.db_profile IS
    '每源一行的当前数据库画像；同源各工作区共用，重复发布替换当前内容';
COMMENT ON COLUMN public.db_profile.source_id IS
    '数据源 text 短 ID；保留每源一行的主键';
COMMENT ON COLUMN public.db_profile.publish_run_id IS
    '最后成功写入本行的发布运行 UUID；工作区和采集输入通过运行关联查询';
COMMENT ON COLUMN public.db_profile.schema_version IS
    '数据库画像文件的整数格式版本';
COMMENT ON COLUMN public.db_profile.collected_at IS
    '数据库画像产物中的采集时间，带时区';
COMMENT ON COLUMN public.db_profile.db_type IS
    '产物记录的数据库类型';
COMMENT ON COLUMN public.db_profile.host IS
    '产物记录的源数据库连接地址';
COMMENT ON COLUMN public.db_profile.port IS
    '产物记录的源数据库端口';
COMMENT ON COLUMN public.db_profile.db_name IS
    '产物记录的数据库名';
COMMENT ON COLUMN public.db_profile.server_version IS
    '数据库版本摘要，可为空';
COMMENT ON COLUMN public.db_profile.server_version_full IS
    '数据库完整版本信息，可为空';
COMMENT ON COLUMN public.db_profile.os_platform IS
    '数据库所在平台信息，可为空';
COMMENT ON COLUMN public.db_profile.size_bytes IS
    '数据库大小，单位字节';
COMMENT ON COLUMN public.db_profile.size_scope IS
    '数据库大小的统计范围';
COMMENT ON COLUMN public.db_profile.record_count IS
    '产物提供的数据库记录总量，未统计时为空';
COMMENT ON COLUMN public.db_profile.selected_schemas IS
    '本次画像使用的受管 Schema 名称数组';
COMMENT ON COLUMN public.db_profile.schemas IS
    '逐 Schema 的对象统计 JSON 数组';
COMMENT ON COLUMN public.db_profile.ordinary_table_count IS
    '普通表数量';
COMMENT ON COLUMN public.db_profile.partitioned_table_count IS
    '分区父表数量';
COMMENT ON COLUMN public.db_profile.partition_child_count IS
    '分区子表数量';
COMMENT ON COLUMN public.db_profile.view_count IS
    '视图数量';
COMMENT ON COLUMN public.db_profile.materialized_view_count IS
    '物化视图数量';
COMMENT ON COLUMN public.db_profile.object_count IS
    '数据库画像统计的对象总数';

CREATE TABLE public.table_definition (
    source_id text NOT NULL,
    schema_name text NOT NULL,
    object_name text NOT NULL,
    define_type text NOT NULL,
    db_name text NOT NULL,
    object_type text NOT NULL,
    define_text text NOT NULL,
    publish_run_id uuid NOT NULL,
    loaded_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT table_definition_pkey
        PRIMARY KEY (source_id, schema_name, object_name, define_type),
    CONSTRAINT table_definition_publish_run_fk
        FOREIGN KEY (publish_run_id)
        REFERENCES public.publish_run_info (publish_run_id),
    CONSTRAINT table_definition_type_ck
        CHECK (define_type IN ('ddl', 'md')),
    CONSTRAINT table_definition_object_type_ck
        CHECK (object_type IN ('table', 'view', 'materialized_view')),
    CONSTRAINT table_definition_names_ck
        CHECK (
            length(btrim(schema_name)) > 0
            AND length(btrim(object_name)) > 0
            AND length(btrim(db_name)) > 0
            AND length(btrim(define_text)) > 0
        )
);

COMMENT ON TABLE public.table_definition IS
    '每源、每对象、每定义类型一行的当前定义文本';
COMMENT ON COLUMN public.table_definition.source_id IS
    '数据源 text 短 ID；联合主键成员';
COMMENT ON COLUMN public.table_definition.schema_name IS
    '对象所属 Schema 原始名称；联合主键成员';
COMMENT ON COLUMN public.table_definition.object_name IS
    '对象原始名称；联合主键成员';
COMMENT ON COLUMN public.table_definition.define_type IS
    '定义类型 ddl 或 md；当前发布只写 ddl，联合主键成员';
COMMENT ON COLUMN public.table_definition.db_name IS
    '产物中的数据库名，不作为内容主键';
COMMENT ON COLUMN public.table_definition.object_type IS
    '对象类型：table、view、materialized_view';
COMMENT ON COLUMN public.table_definition.define_text IS
    '当前定义正文，不能为空';
COMMENT ON COLUMN public.table_definition.publish_run_id IS
    '最后成功写入本行的发布运行 UUID';
COMMENT ON COLUMN public.table_definition.loaded_at IS
    '本行当前内容的发布写入时间，每次替换时更新';

CREATE TABLE public.table_profile (
    source_id text NOT NULL,
    schema_name text NOT NULL,
    object_name text NOT NULL,
    db_name text NOT NULL,
    object_type text NOT NULL,
    publish_run_id uuid NOT NULL,
    metadata_version text NOT NULL,
    generated_timestamp timestamptz,
    loaded_at timestamptz NOT NULL DEFAULT now(),
    object_comment text,
    object_comment_source text,
    total_rows bigint,
    column_count integer NOT NULL,
    sample_method text,
    sample_count bigint,
    has_table_profile boolean NOT NULL,
    table_category text,
    category_confidence numeric(8, 6),
    classification_source text,
    classification_reason text,
    classification_basis text[],
    rule_table_category text,
    rule_confidence numeric(8, 6),
    rule_inference_basis text[],
    primary_key_name text,
    primary_key_columns text[],
    foreign_key_count integer,
    index_count integer,
    constraint_count integer,
    foreign_keys jsonb,
    unique_constraints jsonb,
    indexes jsonb,
    unique_column_sets jsonb,
    json_sample_records jsonb,

    CONSTRAINT table_profile_pkey
        PRIMARY KEY (source_id, schema_name, object_name),
    CONSTRAINT table_profile_publish_run_fk
        FOREIGN KEY (publish_run_id)
        REFERENCES public.publish_run_info (publish_run_id)
);

COMMENT ON TABLE public.table_profile IS
    '每源、每对象一行的当前 JSON 画像；与字段画像在同一发布事务中替换';
COMMENT ON COLUMN public.table_profile.source_id IS
    '数据源 text 短 ID；联合主键成员';
COMMENT ON COLUMN public.table_profile.schema_name IS
    '对象所属 Schema 原始名称；联合主键成员';
COMMENT ON COLUMN public.table_profile.object_name IS
    '对象原始名称；联合主键成员';
COMMENT ON COLUMN public.table_profile.db_name IS
    '产物中的数据库名';
COMMENT ON COLUMN public.table_profile.object_type IS
    '产物中的对象类型';
COMMENT ON COLUMN public.table_profile.publish_run_id IS
    '最后成功写入本行的发布运行 UUID；须与所属字段画像的发布来源一致';
COMMENT ON COLUMN public.table_profile.metadata_version IS
    '表级 JSON 画像格式版本，不与数据库画像的整数 schema_version 混用';
COMMENT ON COLUMN public.table_profile.generated_timestamp IS
    'JSON 产物生成时间，带时区，产物缺失该值时为空';
COMMENT ON COLUMN public.table_profile.loaded_at IS
    '本行当前内容的发布写入时间';
COMMENT ON COLUMN public.table_profile.object_comment IS
    '对象注释，可为空';
COMMENT ON COLUMN public.table_profile.object_comment_source IS
    '对象注释来源，可为空';
COMMENT ON COLUMN public.table_profile.total_rows IS
    '采集时对象行数，未提供时为空';
COMMENT ON COLUMN public.table_profile.column_count IS
    '本对象画像包含的字段数量';
COMMENT ON COLUMN public.table_profile.sample_method IS
    '画像使用的抽样方式';
COMMENT ON COLUMN public.table_profile.sample_count IS
    '画像实际抽样行数';
COMMENT ON COLUMN public.table_profile.has_table_profile IS
    '产物是否提供非空表级画像节点';
COMMENT ON COLUMN public.table_profile.table_category IS
    '表分类结果';
COMMENT ON COLUMN public.table_profile.category_confidence IS
    '表分类置信度';
COMMENT ON COLUMN public.table_profile.classification_source IS
    '分类来源';
COMMENT ON COLUMN public.table_profile.classification_reason IS
    '分类说明';
COMMENT ON COLUMN public.table_profile.classification_basis IS
    '分类依据数组';
COMMENT ON COLUMN public.table_profile.rule_table_category IS
    '规则分类备份结果';
COMMENT ON COLUMN public.table_profile.rule_confidence IS
    '规则分类置信度';
COMMENT ON COLUMN public.table_profile.rule_inference_basis IS
    '规则分类依据数组';
COMMENT ON COLUMN public.table_profile.primary_key_name IS
    '源对象物理主键约束名称';
COMMENT ON COLUMN public.table_profile.primary_key_columns IS
    '源对象物理主键字段数组；空数组和 SQL NULL 保留各自语义';
COMMENT ON COLUMN public.table_profile.foreign_key_count IS
    '可确定的外键数量';
COMMENT ON COLUMN public.table_profile.index_count IS
    '可确定的索引数量';
COMMENT ON COLUMN public.table_profile.constraint_count IS
    '按既有规则计算的物理约束数量';
COMMENT ON COLUMN public.table_profile.foreign_keys IS
    '源对象外键明细';
COMMENT ON COLUMN public.table_profile.unique_constraints IS
    '源对象唯一约束明细';
COMMENT ON COLUMN public.table_profile.indexes IS
    '源对象索引明细';
COMMENT ON COLUMN public.table_profile.unique_column_sets IS
    '候选唯一列组明细';
COMMENT ON COLUMN public.table_profile.json_sample_records IS
    'JSON 产物 sample_records 节点；不作为当前页面样例数据的读取来源';

CREATE TABLE public.table_column_profile (
    source_id text NOT NULL,
    schema_name text NOT NULL,
    object_name text NOT NULL,
    publish_run_id uuid NOT NULL,
    column_name text NOT NULL,
    ordinal_position integer NOT NULL,
    data_type text NOT NULL,
    is_nullable boolean NOT NULL,
    column_default text,
    column_comment text,
    comment_source text,
    semantic_role text,
    semantic_confidence numeric(8, 6),
    semantic_basis text[],
    null_count bigint,
    null_rate numeric(8, 6),
    unique_count bigint,
    uniqueness numeric(8, 6),
    sample_min text,
    sample_max text,
    value_distribution jsonb,

    CONSTRAINT table_column_profile_pkey
        PRIMARY KEY (source_id, schema_name, object_name, column_name),
    CONSTRAINT table_column_profile_object_fk
        FOREIGN KEY (source_id, schema_name, object_name)
        REFERENCES public.table_profile (source_id, schema_name, object_name)
        ON DELETE CASCADE,
    CONSTRAINT table_column_profile_publish_run_fk
        FOREIGN KEY (publish_run_id)
        REFERENCES public.publish_run_info (publish_run_id)
);

CREATE UNIQUE INDEX table_column_profile_object_order_idx
    ON public.table_column_profile
        (source_id, schema_name, object_name, ordinal_position);

COMMENT ON TABLE public.table_column_profile IS
    '每源、每对象、每字段一行的当前字段画像';
COMMENT ON COLUMN public.table_column_profile.source_id IS
    '数据源 text 短 ID；联合主键和父对象外键成员';
COMMENT ON COLUMN public.table_column_profile.schema_name IS
    '对象所属 Schema 原始名称；联合主键和父对象外键成员';
COMMENT ON COLUMN public.table_column_profile.object_name IS
    '对象原始名称；联合主键和父对象外键成员';
COMMENT ON COLUMN public.table_column_profile.publish_run_id IS
    '最后成功写入本行的发布运行 UUID；须与所属对象行一致';
COMMENT ON COLUMN public.table_column_profile.column_name IS
    '字段原始名称；联合主键成员';
COMMENT ON COLUMN public.table_column_profile.ordinal_position IS
    '产物字段序号；正整数由加载器校验，同对象内唯一';
COMMENT ON COLUMN public.table_column_profile.data_type IS
    '字段数据类型';
COMMENT ON COLUMN public.table_column_profile.is_nullable IS
    '源字段是否允许 NULL';
COMMENT ON COLUMN public.table_column_profile.column_default IS
    '字段默认表达式';
COMMENT ON COLUMN public.table_column_profile.column_comment IS
    '字段注释';
COMMENT ON COLUMN public.table_column_profile.comment_source IS
    '字段注释来源';
COMMENT ON COLUMN public.table_column_profile.semantic_role IS
    '字段语义角色';
COMMENT ON COLUMN public.table_column_profile.semantic_confidence IS
    '字段语义分析置信度';
COMMENT ON COLUMN public.table_column_profile.semantic_basis IS
    '字段语义分析依据数组';
COMMENT ON COLUMN public.table_column_profile.null_count IS
    '空值数量，未统计时为空';
COMMENT ON COLUMN public.table_column_profile.null_rate IS
    '空值率，未统计时为空';
COMMENT ON COLUMN public.table_column_profile.unique_count IS
    '不同值数量，未统计时为空';
COMMENT ON COLUMN public.table_column_profile.uniqueness IS
    '唯一度，未统计时为空';
COMMENT ON COLUMN public.table_column_profile.sample_min IS
    '采样最小值的文本表示';
COMMENT ON COLUMN public.table_column_profile.sample_max IS
    '采样最大值的文本表示';
COMMENT ON COLUMN public.table_column_profile.value_distribution IS
    '字段取值分布 JSON';
COMMENT ON INDEX public.table_column_profile_object_order_idx IS
    '保留当前同对象字段序号唯一性，并支持按字段序号读取';

CREATE TABLE public.table_domain (
    source_id text NOT NULL,
    schema_name text NOT NULL,
    table_name text NOT NULL,
    domain_name text NOT NULL,
    domain_desc text NOT NULL,
    db_name text NOT NULL,
    publish_run_id uuid NOT NULL,
    loaded_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT table_domain_pkey
        PRIMARY KEY (source_id, schema_name, table_name, domain_name),
    CONSTRAINT table_domain_publish_run_fk
        FOREIGN KEY (publish_run_id)
        REFERENCES public.publish_run_info (publish_run_id),
    CONSTRAINT table_domain_names_not_blank
        CHECK (
            length(btrim(schema_name)) > 0
            AND length(btrim(table_name)) > 0
            AND length(btrim(domain_name)) > 0
            AND length(btrim(domain_desc)) > 0
            AND length(btrim(db_name)) > 0
        )
);

COMMENT ON TABLE public.table_domain IS
    '每源当前表与业务领域的成员关系，同一表可归属多个领域';
COMMENT ON COLUMN public.table_domain.source_id IS
    '数据源 text 短 ID；联合主键成员';
COMMENT ON COLUMN public.table_domain.schema_name IS
    '表所属 Schema 原始名称；联合主键成员';
COMMENT ON COLUMN public.table_domain.table_name IS
    '表或对象原始名称；联合主键成员';
COMMENT ON COLUMN public.table_domain.domain_name IS
    '业务领域名称；联合主键成员';
COMMENT ON COLUMN public.table_domain.domain_desc IS
    '业务领域描述，不能为空';
COMMENT ON COLUMN public.table_domain.db_name IS
    'Domain 产物对应的数据库名';
COMMENT ON COLUMN public.table_domain.publish_run_id IS
    '最后成功写入本行的发布运行 UUID，实际 YAML 采集来源见该发布步骤 input_manifest';
COMMENT ON COLUMN public.table_domain.loaded_at IS
    '本行当前领域归属的发布写入时间';

-- ---------------------------------------------------------------------
-- 视图：各工作区各步骤最后一次成功发布（doc 6.1 §12）
-- ---------------------------------------------------------------------

CREATE VIEW public.vw_workspace_latest_successful_publish AS
WITH latest_success AS (
    SELECT DISTINCT ON (
        publish_job.workspace_id,
        publish_step.step_name
    )
        publish_job.workspace_id,
        publish_step.step_name,
        publish_step.publish_run_id,
        publish_step.finished_at AS published_at,
        publish_step.input_manifest
    FROM public.publish_run_step AS publish_step
    JOIN public.publish_run_info AS publish_run
        ON publish_run.publish_run_id = publish_step.publish_run_id
    JOIN public.publish_job_info AS publish_job
        ON publish_job.publish_job_id = publish_run.publish_job_id
    WHERE publish_step.status = 'success'
    ORDER BY
        publish_job.workspace_id,
        publish_step.step_name,
        publish_step.finished_at DESC NULLS LAST,
        -- 完成时间相同时，保证查询结果稳定。
        publish_step.publish_run_id DESC
)
SELECT
    latest.workspace_id,
    latest.step_name,
    latest.publish_run_id,
    latest.published_at,
    (
        SELECT jsonb_agg(
            jsonb_build_object(
                'collection_run_id', source_run.collection_run_id,
                'finished_at', collection_run.finished_at
            )
            ORDER BY
                collection_run.finished_at DESC NULLS LAST,
                source_run.collection_run_id
        )
        FROM (
            -- 同一次采集可能贡献多个文件，来源运行需要去重。
            SELECT DISTINCT
                (input_file.item ->> 'collection_run_id')::uuid
                    AS collection_run_id
            FROM jsonb_array_elements(
                COALESCE(
                    latest.input_manifest -> 'files',
                    '[]'::jsonb
                )
            ) AS input_file(item)
            WHERE input_file.item ->> 'collection_run_id' IS NOT NULL
        ) AS source_run
        LEFT JOIN public.collection_run_info AS collection_run
            ON collection_run.collection_run_id =
                source_run.collection_run_id
    ) AS source_collection_runs
FROM latest_success AS latest;

COMMENT ON VIEW public.vw_workspace_latest_successful_publish IS
    '各工作区、各步骤最后一次成功发布及其实际使用的采集运行；后续失败不覆盖成功记录';

COMMENT ON COLUMN
    public.vw_workspace_latest_successful_publish.workspace_id IS
    '所属工作区 ID';

COMMENT ON COLUMN
    public.vw_workspace_latest_successful_publish.step_name IS
    '发布步骤名称';

COMMENT ON COLUMN
    public.vw_workspace_latest_successful_publish.publish_run_id IS
    '该工作区该步骤最后一次成功发布的运行 UUID';

COMMENT ON COLUMN
    public.vw_workspace_latest_successful_publish.published_at IS
    '该发布步骤成功完成的时间，取 publish_run_step.finished_at';

COMMENT ON COLUMN
    public.vw_workspace_latest_successful_publish.source_collection_runs IS
    '从该成功发布步骤的 input_manifest 提取并去重的采集来源列表；包含 collection_run_id 和整次采集的 finished_at，无可识别来源时为 NULL';
