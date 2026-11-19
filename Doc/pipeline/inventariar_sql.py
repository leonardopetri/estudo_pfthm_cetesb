import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from sqlalchemy import text
from pipeline_cetesb import build_sqlalchemy_engine

QUERY = """
SELECT s.name AS esquema, t.name AS tabela, c.column_id AS ordinal,
       c.name AS coluna, ty.name AS tipo, c.max_length AS comprimento_bytes,
       c.precision AS precisao, c.scale AS escala, c.is_nullable AS aceita_nulo,
       c.is_identity AS identidade, c.is_computed AS computada,
       c.collation_name AS collation,
       (SELECT SUM(p.rows) FROM sys.partitions p
        WHERE p.object_id=t.object_id AND p.index_id IN (0,1)) AS linhas_metadados
FROM sys.tables t
JOIN sys.schemas s ON t.schema_id=s.schema_id
JOIN sys.columns c ON c.object_id=t.object_id
JOIN sys.types ty ON c.user_type_id=ty.user_type_id
WHERE t.is_ms_shipped=0
ORDER BY s.name,t.name,c.column_id
"""

KEY_QUERY = """
SELECT s.name AS esquema, t.name AS tabela, i.name AS indice,
       i.is_primary_key AS chave_primaria, i.is_unique AS unico,
       c.name AS coluna, ic.key_ordinal AS ordem
FROM sys.tables t
JOIN sys.schemas s ON t.schema_id=s.schema_id
JOIN sys.indexes i ON i.object_id=t.object_id
JOIN sys.index_columns ic ON ic.object_id=i.object_id AND ic.index_id=i.index_id
JOIN sys.columns c ON c.object_id=ic.object_id AND c.column_id=ic.column_id
WHERE t.is_ms_shipped=0 AND i.is_unique=1 AND ic.key_ordinal>0
ORDER BY s.name,t.name,i.name,ic.key_ordinal
"""


def main():
    load_dotenv(ROOT / ".env", override=False)
    value = os.environ.get("AZURE_SQL_CONNECTION_STRING")
    if not value:
        print("SQL não consultado: configuração de conexão ausente.")
        return 2
    engine = build_sqlalchemy_engine(value)
    try:
        with engine.connect() as conn:
            columns = [dict(r) for r in conn.execute(text(QUERY)).mappings()]
            keys = [dict(r) for r in conn.execute(text(KEY_QUERY)).mappings()]
        result = {
            "consultado_em_utc": datetime.now(timezone.utc).isoformat(),
            "modo": "Somente SELECT em sys.*; sem leitura de valores de negócio ou escrita SQL.",
            "limite_visibilidade": "Objetos visíveis ao usuário conectado; não comprova acesso a todos os schemas.",
            "consulta_colunas": QUERY,
            "consulta_chaves": KEY_QUERY,
            "colunas": columns,
            "chaves": keys,
        }
        output = Path(__file__).with_name("sql_inventario.json")
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"SQL: {len({(r['esquema'], r['tabela']) for r in columns})} tabelas, "
              f"{len(columns)} colunas; {len(keys)} componentes de índices únicos.")
        print("Inventário salvo sem credenciais ou endereço do servidor.")
        return 0
    except Exception as exc:
        print(f"Consulta SQL indisponível ({type(exc).__name__}); detalhes da conexão não serão exibidos.")
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
