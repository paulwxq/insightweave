"""DefaultReadinessBackend：readiness 的真实依赖读取（doc 2 §5.5）。

所有查询都是只读的：登记/发布记录走平台库参数化 SQL；Milvus 经
DbMilvusRetrieval 按源枚举；Neo4j 经注入配置的连接管理器按 source_id
过滤；业务库检查使用与运行期相同的只读事务入口（SET TRANSACTION READ
ONLY + SET LOCAL statement_timeout）。任何"无法完成读取"统一抛
ReadinessError（503），确定性结论（不存在/停用/类型不符/配置非法）以
结构化状态返回，不靠消息字符串分类。
"""

from __future__ import annotations

from typing import Any, Optional

from src.api.v2.readiness import (
    MilvusFacts,
    Neo4jFacts,
    PublishRecords,
    ReadinessError,
    RegistrationLookup,
    strict_column_key,
    strict_table_key,
)
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.api.v2.backend")

_PUBLISH_QUERY = (
    "SELECT step_name, status, is_current_publish, publish_id, job_id, created_at "
    "FROM public.publish_info WHERE source_id = %s"
)


def _base_config():
    """源无关基础配置（doc 2 §5.3）：milvus/embedding/neo4j 段与源无关。"""
    from src.modules.db_deepagent.config import load_base_db_agent_config

    return load_base_db_agent_config()


def _platform_conninfo() -> str:
    from src.modules.db_deepagent.source_registry import _platform_conninfo as _impl

    # Web 请求可能先于任何配置读取到达：先加载基础配置确保 .env 已进
    # os.environ（_impl 直接读环境变量；CLI/其余入口经 load_base_db_agent_config）
    _base_config()
    return _impl()


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _source_logical(entity_id: Any, source_id: str) -> str | None:
    """entity_id 去本源前缀后的逻辑键；无前缀时返回 None（回退业务 id）。"""
    if isinstance(entity_id, str) and entity_id.startswith(f"{source_id}:"):
        return entity_id[len(source_id) + 1 :]
    return None


def _edge_endpoint_keys(
    table_entity: Any,
    table_id: Any,
    table_source: Any,
    column_entity: Any,
    column_id: Any,
    column_source: Any,
    source_id: str,
    db_name: str,
) -> Optional[tuple[str, tuple[str, str]]]:
    """HAS_COLUMN 端点键解析（doc 2 §5.5）：两端点必须归属当前源——
    跨源端点不回退业务 id（守住源隔离边界）；键不可解析或父子表键
    不一致返回 None，调用方计为无效边。"""
    if table_source != source_id or column_source != source_id:
        return None
    table_logical = _source_logical(table_entity, source_id)
    table_key = strict_table_key(
        table_logical if table_logical is not None else _as_str(table_id), db_name
    )
    if table_key is None:
        return None
    column_logical = _source_logical(column_entity, source_id)
    column_key = strict_column_key(
        column_logical if column_logical is not None else _as_str(column_id), db_name
    )
    if column_key is None or table_key != column_key[0]:
        return None
    return table_key, column_key


class DefaultReadinessBackend:
    """真实依赖实现（测试用 fake 按 ReadinessBackend Protocol 注入）。"""

    # ------------------------------------------------------------ 登记

    def lookup_registration(self, source_id: str) -> RegistrationLookup:
        import psycopg

        from src.modules.db_deepagent.keyspace import db_prefix_problem

        try:
            with psycopg.connect(_platform_conninfo(), connect_timeout=10) as conn:
                from src.modules.db_deepagent.source_registry import _QUERY

                row = conn.execute(_QUERY, (source_id,)).fetchone()
        except psycopg.Error as exc:
            sqlstate = getattr(exc, "sqlstate", None)
            logger.warning(
                "db_source_info 读取失败 source_id=%s: %s%s",
                source_id,
                type(exc).__name__,
                f"（SQLSTATE {sqlstate}）" if sqlstate else "",
            )
            return RegistrationLookup(status="unavailable", detail="平台库读取失败")
        except Exception as exc:  # noqa: BLE001（环境变量缺失等装配问题）
            logger.warning(
                "平台库连接配置不可用 source_id=%s: %s: %s",
                source_id,
                type(exc).__name__,
                exc,
            )
            return RegistrationLookup(
                status="unavailable",
                detail=f"平台库连接配置不可用: {type(exc).__name__}",
            )
        if row is None:
            return RegistrationLookup(status="not_found")
        (
            _sid,
            db_type,
            host,
            port,
            db_name,
            username,
            ciphertext,
            sslmode,
            selected_schemas,
            is_active,
        ) = row
        if not is_active:
            return RegistrationLookup(status="inactive")
        if db_type != "postgresql":
            return RegistrationLookup(
                status="unsupported", detail=f"db_type={db_type!r}（当前仅 postgresql）"
            )
        if not selected_schemas:
            return RegistrationLookup(
                status="config_invalid", detail="selected_schemas 为空"
            )
        if not isinstance(db_name, str) or db_prefix_problem(db_name):
            return RegistrationLookup(
                status="config_invalid", detail="登记 db_name 不能作为知识键前缀"
            )

        from src.modules.db_deepagent.source_registry import (
            SourceRegistration,
            _decrypt_password,
        )

        try:
            password = _decrypt_password(ciphertext)
        except Exception:  # noqa: BLE001（密钥/密文问题统一 unavailable）
            return RegistrationLookup(
                status="unavailable",
                detail="登记密码无法解密（检查 SOURCE_CREDENTIAL_KEY）",
            )
        registration = SourceRegistration(
            source_id=_as_str(_sid),
            db_name=db_name,
            host=_as_str(host),
            port=int(port),
            username=_as_str(username),
            password=password,
            sslmode=sslmode,
            selected_schemas=tuple(selected_schemas or ()),
            is_active=bool(is_active),
        )
        return RegistrationLookup(status="ok", registration=registration)

    # ------------------------------------------------------------ 发布记录

    def load_publish_records(self, source_id: str) -> PublishRecords:
        import psycopg

        try:
            with psycopg.connect(_platform_conninfo(), connect_timeout=10) as conn:
                rows = conn.execute(_PUBLISH_QUERY, (source_id,)).fetchall()
        except Exception as exc:  # noqa: BLE001
            raise ReadinessError(
                f"publish_info 读取失败: {type(exc).__name__}"
            ) from exc
        records = PublishRecords()
        for step_name, status, is_current, publish_id, job_id, created_at in rows:
            records.rows.append(
                {
                    "step_name": _as_str(step_name),
                    "status": _as_str(status),
                    "is_current_publish": bool(is_current),
                    "publish_id": _as_str(publish_id),
                    "job_id": _as_str(job_id),
                    "created_at": (
                        created_at.isoformat() if created_at is not None else None
                    ),
                }
            )
        return records

    # ------------------------------------------------------------ Milvus

    def _open_milvus(self, source_id: str):
        """按源构造 Milvus 检索（独立 alias；调用方负责 close）。"""
        from src.modules.db_deepagent.errors import RetrievalBackendError
        from src.modules.db_deepagent.services import _embedding_profile_and_key
        from src.modules.db_deepagent.services.embedding import DbEmbeddingClient
        from src.modules.db_deepagent.services.milvus_retrieval import DbMilvusRetrieval

        cfg = _base_config()
        try:
            profile, api_key = _embedding_profile_and_key(cfg)
            embedding = DbEmbeddingClient(
                profile,
                api_key=api_key,
                request_timeout_s=cfg.embedding.request_timeout_seconds,
            )
            return DbMilvusRetrieval(cfg, embedding, source_id=source_id)
        except RetrievalBackendError as exc:
            raise ReadinessError(f"Milvus 不可用: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise ReadinessError(
                f"Milvus/Embedding 装配失败: {type(exc).__name__}"
            ) from exc

    def load_milvus_facts(self, source_id: str) -> MilvusFacts:
        lookup = self.lookup_registration(source_id)
        if lookup.status != "ok" or lookup.registration is None:
            raise ReadinessError(f"登记源不可用（{lookup.status}），无法读取 Milvus")
        db_name = lookup.registration.db_name
        facts = MilvusFacts()
        milvus = self._open_milvus(source_id)
        try:
            for record in milvus.iter_source_records(
                "table_schema",
                ["object_id", "parent_id", "object_type", "publish_id"],
            ):
                publish_id = _as_str(record.get("publish_id"))
                facts.table_publish_ids.add(publish_id)
                object_id = record.get("object_id")
                if record.get("object_type") == "table":
                    key = strict_table_key(object_id, db_name)
                    if key is None:
                        facts.unparseable += 1
                        continue
                    facts.table_keys.add(key)
                else:
                    parsed = strict_column_key(object_id, db_name)
                    if parsed is None:
                        facts.unparseable += 1
                        continue
                    # 父子归属核对（doc 2 §5.5）：parent_id 必须解析为该列的表键
                    parent_key = strict_table_key(
                        _as_str(record.get("parent_id")), db_name
                    )
                    if parent_key != parsed[0]:
                        facts.column_parent_mismatch += 1
                    facts.column_keys.add(parsed)
            for record in milvus.iter_source_records(
                "dim_value", ["col_id", "publish_id"]
            ):
                facts.dim_value_count += 1
                facts.dim_value_publish_ids.add(_as_str(record.get("publish_id")))
            for record in milvus.iter_source_records(
                "sql_example", ["example_id", "publish_id"]
            ):
                facts.sql_example_count += 1
                facts.sql_example_publish_ids.add(_as_str(record.get("publish_id")))
        except ReadinessError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ReadinessError(f"Milvus 按源枚举失败: {type(exc).__name__}") from exc
        finally:
            milvus.close()
        return facts

    # ------------------------------------------------------------ Neo4j

    def load_neo4j_facts(self, source_id: str) -> Neo4jFacts:
        lookup = self.lookup_registration(source_id)
        if lookup.status != "ok" or lookup.registration is None:
            raise ReadinessError(f"登记源不可用（{lookup.status}），无法读取 Neo4j")
        db_name = lookup.registration.db_name
        facts = Neo4jFacts()
        from src.modules.db_deepagent.services import _neo4j_manager

        cfg = _base_config()
        manager = _neo4j_manager(cfg)
        try:
            with manager.get_session() as session:
                for label in ("Table", "Column"):
                    result = session.run(
                        f"MATCH (n:{label}) WHERE n.source_id = $sid "
                        "RETURN n.entity_id AS entity_id, n.id AS id, "
                        "n.publish_id AS publish_id",
                        sid=source_id,
                    )
                    for record in result:
                        publish_id = _as_str(record.get("publish_id"))
                        facts.publish_ids.add(publish_id)
                        entity_id = record.get("entity_id")
                        logical = _source_logical(entity_id, source_id)
                        if logical is not None:
                            if label == "Table":
                                key = strict_table_key(logical, db_name)
                            else:
                                key = strict_column_key(logical, db_name)
                            if key is None:
                                facts.unparseable += 1
                                continue
                            if label == "Table":
                                facts.table_keys.add(key)
                            else:
                                facts.column_keys.add(key)
                            continue
                        # entity_id 缺失或不带本源前缀：回退按业务 id 解析，
                        # 两段表键/三段列键即旧格式（doc 2 §5.5 legacy 判定）
                        raw_id = record.get("id")
                        if label == "Table":
                            parsed = strict_table_key(raw_id, db_name)
                        else:
                            parsed = strict_column_key(raw_id, db_name)
                        if parsed is None:
                            facts.unparseable += 1
                        else:
                            facts.legacy_two_segment += 1
                # HAS_COLUMN 端点核对（doc 2 §5.5）：逐边解析端点键，仅当
                # 两端点归属当前源且父子表键一致时计入有效对；否则计无效边
                edge_pairs = session.run(
                    "MATCH (t:Table)-[h:HAS_COLUMN]->(c:Column) "
                    "WHERE h.source_id = $sid "
                    "RETURN t.entity_id AS te, t.id AS ti, t.source_id AS tsid, "
                    "c.entity_id AS ce, c.id AS ci, c.source_id AS csid",
                    sid=source_id,
                )
                for record in edge_pairs:
                    resolved = _edge_endpoint_keys(
                        record.get("te"),
                        record.get("ti"),
                        record.get("tsid"),
                        record.get("ce"),
                        record.get("ci"),
                        record.get("csid"),
                        source_id,
                        db_name,
                    )
                    if resolved is None:
                        facts.has_column_invalid += 1
                        continue
                    facts.has_column_pairs.add(resolved[1])
                facts.has_column_count = (
                    len(facts.has_column_pairs) + facts.has_column_invalid
                )
                edge_result = session.run(
                    "MATCH ()-[r]->() WHERE r.source_id = $sid "
                    "AND type(r) IN ['HAS_COLUMN', 'JOIN_ON'] "
                    "RETURN DISTINCT r.publish_id AS pid",
                    sid=source_id,
                )
                for record in edge_result:
                    facts.publish_ids.add(_as_str(record.get("pid")))
        except Exception as exc:  # noqa: BLE001
            raise ReadinessError(f"Neo4j 按源读取失败: {type(exc).__name__}") from exc
        finally:
            manager.close()
        return facts

    # ------------------------------------------------------------ 业务库

    def check_business_db(self, registration) -> list[dict[str, Any]]:
        from src.modules.db_deepagent.errors import DatasourceMismatchError
        from src.modules.db_deepagent.services.business_db import BusinessDbService
        from src.modules.db_deepagent.source_registry import (
            apply_registration_to_config,
        )

        cfg = apply_registration_to_config(_base_config(), registration)
        svc = BusinessDbService(cfg.business_db)
        try:
            try:
                svc.open()
            except DatasourceMismatchError:
                return [
                    {
                        "name": "business_db.target",
                        "required": True,
                        "state": "fail",
                        "error_code": "business_target_mismatch",
                        "message": f"业务库实际库名与登记 {registration.db_name!r} 不一致",
                        "counts": None,
                    }
                ]
            try:
                with svc._pool.connection() as conn:  # noqa: SLF001（同模块层的只读检查入口）
                    with conn.transaction():
                        conn.execute("SET TRANSACTION READ ONLY")
                        conn.execute("SET LOCAL statement_timeout = 3000")
                        row = conn.execute(
                            "SELECT current_database(), "
                            "current_setting('transaction_read_only'), "
                            "current_setting('statement_timeout')"
                        ).fetchone()
                    idle = conn.info.transaction_status.name == "IDLE"
            except Exception as exc:  # noqa: BLE001
                raise ReadinessError(
                    f"业务库检查未能完成: {type(exc).__name__}"
                ) from exc
        finally:
            svc.close()
        database, readonly, timeout = row
        checks: list[dict[str, Any]] = []

        def _item(
            name: str, ok: bool, error_code: str, message_ok: str, message_bad: str
        ) -> dict[str, Any]:
            return {
                "name": name,
                "required": True,
                "state": "pass" if ok else "fail",
                "error_code": None if ok else error_code,
                "message": message_ok if ok else message_bad,
                "counts": None,
            }

        checks.append(
            _item(
                "business_db.target",
                database == registration.db_name,
                "business_target_mismatch",
                f"业务库目标一致: {database}",
                f"业务库应为 {registration.db_name!r}，实际为 {database!r}",
            )
        )
        checks.append(
            _item(
                "business_db.readonly",
                str(readonly).lower() == "on",
                "business_readonly_invalid",
                "事务内 transaction_read_only=on",
                f"事务内 transaction_read_only={readonly!r}，只读未生效",
            )
        )
        checks.append(
            _item(
                "business_db.timeout",
                _timeout_ms(timeout) == 3000,
                "business_timeout_invalid",
                "事务内 statement_timeout=3s 生效",
                f"事务内 statement_timeout={timeout!r}，超时未按检查值生效",
            )
        )
        checks.append(
            _item(
                "business_db.idle",
                idle,
                "dependency_unavailable",
                "检查后连接回到 IDLE",
                "检查后连接未回到 IDLE（事务未正常收尾）",
            )
        )
        return checks

    # ------------------------------------------------------------ Embedding 探针

    def embedding_probe(self, source_id: str) -> None:
        from src.modules.db_deepagent.errors import EmbeddingConsistencyError
        from src.modules.db_deepagent.services import _embedding_profile_and_key
        from src.modules.db_deepagent.services.embedding import DbEmbeddingClient
        from src.modules.db_deepagent.services.embedding_consistency import (
            check_embedding_consistency,
        )

        cfg = _base_config()
        milvus = self._open_milvus(source_id)
        try:
            profile, api_key = _embedding_profile_and_key(cfg)
            embedding = DbEmbeddingClient(
                profile,
                api_key=api_key,
                request_timeout_s=cfg.embedding.request_timeout_seconds,
            )
            check_embedding_consistency(cfg, profile, embedding, milvus)
        except EmbeddingConsistencyError as exc:
            raise ReadinessError(f"Embedding 一致性检查失败: {exc}") from exc
        except ReadinessError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ReadinessError(
                f"Embedding 探针未能完成: {type(exc).__name__}"
            ) from exc
        finally:
            milvus.close()


def _timeout_ms(value: Any) -> int:
    """statement_timeout 文本（如 '3s'/'3000ms'）→ 毫秒。"""
    text = str(value or "").strip().lower()
    if text.endswith("ms"):
        return int(float(text[:-2]))
    if text.endswith("s"):
        return int(float(text[:-1]) * 1000)
    try:
        return int(float(text))
    except ValueError:
        return -1
