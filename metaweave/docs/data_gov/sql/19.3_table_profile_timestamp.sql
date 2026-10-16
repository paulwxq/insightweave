-- doc 19.3 平台库表结构变更：table_profile 生成时间列改名与类型变更
--
-- 执行前置（doc 19.3 §6）：
--   * 应用代码与实际表结构必须在同一次切换中保持一致；
--   * 测试环境明确不迁移旧测试值：DROP 旧列 + ADD 可空新列，同一事务执行；
--   * 新列不设默认值，避免冒充画像生成时间；重新发布前的行为 NULL。
--
-- 其余 9 个带时区列已具备六位微秒精度（atttypmod = -1 与显式 (6) 等价），
-- 本脚本不处理。

BEGIN;

ALTER TABLE public.table_profile DROP COLUMN generated_timestamp_local;
ALTER TABLE public.table_profile ADD COLUMN generated_timestamp timestamptz(6) NULL;

COMMIT;

-- 验收（改造后应全部 PASS）：
--   1) 列类型 10 行全 PASS：见 doc 19.3 §5 第一个只读查询；
--   2) 旧列已删除：
--      SELECT CASE WHEN NOT EXISTS (
--          SELECT 1 FROM pg_catalog.pg_attribute
--          WHERE attrelid = 'public.table_profile'::regclass
--            AND attname = 'generated_timestamp_local'
--            AND attnum > 0 AND NOT attisdropped
--      ) THEN 'PASS' ELSE 'FAIL' END AS old_column_removed;
