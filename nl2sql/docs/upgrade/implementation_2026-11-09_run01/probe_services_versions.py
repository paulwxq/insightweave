"""Non-sensitive service version probe: prints versions/hosts/ports/db names only."""
import os
from dotenv import load_dotenv

load_dotenv('/opt/projects/insightweave/nl2sql/.env')

def mask(s):  # host/port/db names are non-sensitive; keep as-is
    return s

lines = []

def probe_pg(tag, host, port, dbname, user):
    import psycopg
    try:
        with psycopg.connect(host=host, port=port, dbname=dbname, user=user,
                             password=os.environ.get(f"{tag}_PASSWORD", os.environ.get("DB_PASSWORD","")),
                             connect_timeout=5) as conn:
            ver = conn.execute("select version()").fetchone()[0]
            try:
                ext = conn.execute("select extversion from pg_extension where extname='vector'").fetchone()
                extv = ext[0] if ext else "not-installed"
            except Exception as e:
                extv = f"query-failed: {type(e).__name__}"
            lines.append(f"PG[{tag}] host={host}:{port} db={dbname} server={ver.split(',')[0]} pgvector_ext={extv}")
    except Exception as e:
        lines.append(f"PG[{tag}] host={host}:{port} db={dbname} FAILED: {type(e).__name__}: {e}")

# platform db
probe_pg("PLATFORM_DB", os.getenv("PLATFORM_DB_HOST"), os.getenv("PLATFORM_DB_PORT"), os.getenv("PLATFORM_DB_NAME"), os.getenv("PLATFORM_DB_USER"))
# langgraph checkpoint db
probe_pg("LANGGRAPH_DB", os.getenv("LANGGRAPH_DB_HOST"), os.getenv("LANGGRAPH_DB_PORT"), os.getenv("LANGGRAPH_DB_NAME"), os.getenv("LANGGRAPH_DB_USER"))
# business db
probe_pg("DB", os.getenv("DB_HOST"), os.getenv("DB_PORT"), os.getenv("DB_NAME"), os.getenv("DB_USER"))

# milvus
try:
    from pymilvus import connections, utility
    connections.connect(host=os.getenv("MILVUS_HOST"), port=os.getenv("MILVUS_PORT"),
                        user=os.getenv("MILVUS_USER"), password=os.getenv("MILVUS_PASSWORD"),
                        db_name=os.getenv("MILVUS_DATABASE", "default"))
    lines.append(f"Milvus host={os.getenv('MILVUS_HOST')}:{os.getenv('MILVUS_PORT')} db={os.getenv('MILVUS_DATABASE','default')} server_version={utility.get_server_version()}")
    connections.disconnect("default")
except Exception as e:
    lines.append(f"Milvus FAILED: {type(e).__name__}: {e}")

# neo4j
try:
    from neo4j import GraphDatabase
    drv = GraphDatabase.driver(os.getenv("NEO4J_URI"), auth=(os.getenv("NEO4J_USER"), os.getenv("NEO4J_PASSWORD")))
    drv.verify_connectivity()
    with drv.session(database=os.getenv("NEO4J_DATABASE")) as s:
        ver = s.run("CALL dbms.components() YIELD name, versions RETURN versions").single()[0]
    lines.append(f"Neo4j uri={os.getenv('NEO4J_URI')} db={os.getenv('NEO4J_DATABASE')} server_version={ver}")
    drv.close()
except Exception as e:
    lines.append(f"Neo4j FAILED: {type(e).__name__}: {e}")

print("\n".join(lines))
