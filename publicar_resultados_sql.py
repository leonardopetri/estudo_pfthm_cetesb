"""Publica os resultados locais validados e recria o cache a partir do Azure SQL."""
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
import shutil
import sys
import time
import re

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import Date, NVARCHAR, text

from pipeline_cetesb import DATA_VERSION, build_sqlalchemy_engine, sql_frame

ROOT = Path(__file__).parent
RESULTS = ROOT / "resultados_v2"
TABLES = [
    "features_coletas_wide", "features_resumo_parametros",
    "estudo_thm_base", "predicoes_risco_thm",
    "ana_captacoes_etas_sp", "relacao_cetesb_ana_etas",
]


def identifier(value):
    return "[" + value.replace("]", "]]") + "]"


def load_tables():
    manifest = json.loads((RESULTS / "manifesto.json").read_text(encoding="utf-8"))
    if manifest["versao_dados"] != DATA_VERSION:
        raise ValueError("Artefatos locais com versão incompatível.")
    for name, digest in manifest["codigo_sha256"].items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Código alterado após o reprocessamento: {name}")
    for source in manifest["fontes"]:
        if hashlib.sha256((ROOT / source["arquivo"]).read_bytes()).hexdigest() != source["sha256"]:
            raise ValueError(f"Fonte alterada após o reprocessamento: {source['arquivo']}")
    frames = {name: pd.read_parquet(RESULTS / f"{name}.parquet") for name in TABLES[:4]}
    ana = json.loads((ROOT / "ana_etas_sp.json").read_text(encoding="utf-8"))
    frames["ana_captacoes_etas_sp"] = pd.DataFrame(ana["ana_etas"])
    frames["relacao_cetesb_ana_etas"] = pd.DataFrame(ana["relations_by_station"].values())
    for name, frame in frames.items():
        if frame.empty:
            raise ValueError(f"Tabela vazia: {name}")
        if frame.shape[1] > 1024:
            raise ValueError(f"Tabela excede o limite de colunas SQL Server: {name}")
        if name in TABLES[:4] and name != "features_resumo_parametros":
            if frame.duplicated(["codigo_ponto", "data_coleta", "hora_coleta"]).any():
                raise ValueError(f"Chaves de coleta duplicadas em {name}")
        for col in frame.select_dtypes(include=["float64", "float32"]):
            frame[col] = frame[col].replace([np.inf, -np.inf], np.nan)
    relations = frames["relacao_cetesb_ana_etas"]
    if relations["vinculo_confirmado"].ne(False).any() or relations["distancia_km"].gt(15).any():
        raise ValueError("Relações ANA incompatíveis com a metodologia revisada.")
    return frames


def verify_sql(conn, schema, frames):
    for name, frame in frames.items():
        count = conn.execute(text(f"SELECT COUNT_BIG(*) FROM {identifier(schema)}.{identifier(name)}")).scalar_one()
        if count != len(frame):
            raise ValueError(f"Contagem divergente: {name}: {count} / {len(frame)}")
    for name in ["features_coletas_wide", "estudo_thm_base"]:
        invalid = conn.execute(text(
            f"SELECT COUNT_BIG(*) FROM {identifier(schema)}.{identifier(name)} "
            "WHERE versao_dados IS NULL OR versao_dados <> :version"
        ), {"version": DATA_VERSION}).scalar_one()
        if invalid:
            raise ValueError("Versão incorreta no banco.")
    base = f"{identifier(schema)}.[estudo_thm_base]"
    n = conn.execute(text(f"SELECT nitrogenio_total_mg_l FROM {base} "
                          "WHERE codigo_ponto='BILL02030' AND data_coleta='20160114'")).scalar_one()
    iet = conn.execute(text(f"SELECT iet_medio FROM {base} "
                            "WHERE codigo_ponto='BILL02030' AND data_coleta='20100128'")).scalar_one()
    if not np.isclose(n, 2.4) or not np.isclose(iet, 61.97):
        raise ValueError("Regressão nos exemplos de nitrogênio ou IET.")
    invalid = conn.execute(text(
        f"SELECT COUNT_BIG(*) FROM {identifier(schema)}.[predicoes_risco_thm] "
        "WHERE prob_risco_thm_consolidada IS NOT NULL OR "
        "predicao_risco_thm_100_ms_bin IS NOT NULL OR thm_eta_observado_ug_l IS NOT NULL"
    )).scalar_one()
    if invalid:
        raise ValueError("Resultados não validados publicados como medições ou risco.")
    prediction_table = f"{identifier(schema)}.[predicoes_risco_thm]"
    invalid = conn.execute(text(
        f"SELECT COUNT_BIG(*) FROM {prediction_table} WHERE "
        "(pfthm_predito_ipt_ug_l IS NOT NULL AND "
        "(cod_observado_mg_l IS NULL OR pfthm_predito_ipt_ug_l < 0)) OR "
        "(pfthm_modelo_1_ipt_ug_l < 0 OR pfthm_modelo_2_ipt_ug_l < 0) OR "
        "(origem_pfthm = N'Estimado IPT COD' AND "
        "(pfthm_observado_ug_l IS NOT NULL OR pfthm_predito_ipt_ug_l IS NULL))"
    )).scalar_one()
    available = conn.execute(text(
        f"SELECT COUNT_BIG(*) FROM {prediction_table} WHERE pfthm_predito_ipt_ug_l IS NOT NULL"
    )).scalar_one()
    if invalid or available != int(frames["predicoes_risco_thm"].pfthm_predito_ipt_ug_l.notna().sum()):
        raise ValueError("Estimativas IPT COD inconsistentes na publicação.")


def main():
    frames = load_tables()
    load_dotenv(ROOT / ".env", override=True)
    engine = build_sqlalchemy_engine(os.getenv("AZURE_SQL_CONNECTION_STRING", ""))
    for attempt in range(1, 5):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            break
        except Exception:
            if attempt == 4:
                print("Banco indisponível após quatro tentativas; nenhuma publicação iniciada.")
                engine.dispose()
                raise SystemExit(1) from None
            print(f"Aguardando disponibilidade do banco ({attempt}/4)...", flush=True)
            time.sleep(10)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    staging = f"cetesb_publicacao_{stamp}"
    backup = f"cetesb_backup_{stamp}"
    receipt = {
        "versao_dados": DATA_VERSION, "inicio": datetime.now().isoformat(),
        "schema_temporario": staging, "schema_backup": backup,
        "tabelas": {name: {"linhas": len(df), "colunas": len(df.columns)} for name, df in frames.items()},
        "status": "preparando",
    }
    receipt_path = RESULTS / f"publicacao_sql_{stamp}.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        permissions = {}
        with engine.begin() as conn:
            conn.execute(text("SET LOCK_TIMEOUT 60000"))
            for name in TABLES:
                foreign_keys = conn.execute(text(
                    "SELECT COUNT(*) FROM sys.foreign_keys "
                    "WHERE parent_object_id=OBJECT_ID(:name) OR referenced_object_id=OBJECT_ID(:name)"
                ), {"name": f"dbo.{name}"}).scalar_one()
                if foreign_keys:
                    raise ValueError(f"Dependências de chave estrangeira exigem migração específica: {name}")
                permissions[name] = conn.execute(text(
                    "SELECT p.state, p.permission_name, u.name AS grantee, p.minor_id "
                    "FROM sys.database_permissions p JOIN sys.database_principals u ON u.principal_id=p.grantee_principal_id "
                    "WHERE p.class=1 AND p.major_id=OBJECT_ID(:name)"
                ), {"name": f"dbo.{name}"}).mappings().all()
                if any(row["minor_id"] for row in permissions[name]):
                    raise ValueError(f"Permissões por coluna exigem migração específica: {name}")
            conn.execute(text(f"CREATE SCHEMA {identifier(staging)}"))

        for name, frame in frames.items():
            data, types = sql_frame(frame)
            print(f"Gravando {staging}.{name}: {len(data):,} linhas, {len(data.columns)} colunas", flush=True)
            data.to_sql(name, engine, schema=staging, if_exists="fail", index=False, dtype=types, chunksize=250)
            print(f"Concluída a carga de {name}", flush=True)
        with engine.connect() as conn:
            verify_sql(conn, staging, frames)
        print("Tabelas temporárias verificadas. Publicando em dbo com backup transacional...", flush=True)
        with engine.begin() as conn:
            conn.execute(text("SET LOCK_TIMEOUT 60000"))
            conn.execute(text(f"CREATE SCHEMA {identifier(backup)}"))
            for name in TABLES:
                if conn.execute(text("SELECT OBJECT_ID(:name, 'U')"), {"name": f"dbo.{name}"}).scalar():
                    conn.execute(text(f"ALTER SCHEMA {identifier(backup)} TRANSFER [dbo].{identifier(name)}"))
                conn.execute(text(f"ALTER SCHEMA [dbo] TRANSFER {identifier(staging)}.{identifier(name)}"))
                for perm in permissions[name]:
                    action = "DENY" if perm["state"] == "D" else "GRANT"
                    grant_option = " WITH GRANT OPTION" if perm["state"] == "W" else ""
                    conn.execute(text(
                        f"{action} {perm['permission_name']} ON OBJECT::[dbo].{identifier(name)} "
                        f"TO {identifier(perm['grantee'])}{grant_option}"
                    ))
            verify_sql(conn, "dbo", frames)
            conn.execute(text(f"DROP SCHEMA {identifier(staging)}"))
        receipt["status"] = "tabelas_publicadas"
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        from app_mapa_sp import CACHE_FILE, DATA_STORE, app, load_data
        if CACHE_FILE.exists():
            shutil.copy2(CACHE_FILE, CACHE_FILE.with_name(CACHE_FILE.name + f".antes_sql_{stamp}"))
        print("Recriando o Parquet pela consulta às tabelas publicadas no banco...", flush=True)
        load_data(force_db=True)
        # DATA_STORE["df"] exclui a coleta sem coordenadas; o Parquet a preserva.
        cache_rows = len(pd.read_parquet(CACHE_FILE))
        if cache_rows != len(frames["estudo_thm_base"]):
            raise ValueError("Contagem do cache difere da tabela publicada.")
        for endpoint in ["/", "/api/init", "/api/station/BILL02030"]:
            if app.test_client().get(endpoint).status_code != 200:
                raise ValueError(f"Falha ao verificar {endpoint}")
        receipt.update({
            "status": "concluido", "fim": datetime.now().isoformat(),
            "parquet": str(CACHE_FILE), "linhas_parquet": cache_rows,
            "sha256_parquet": hashlib.sha256(CACHE_FILE.read_bytes()).hexdigest(),
        })
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
    except Exception as exc:
        receipt["erro_tipo"] = type(exc).__name__
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        # Nunca imprimir connection strings, parâmetros de conexão ou valores do .env.
        print(f"Publicação interrompida: {type(exc).__name__}. Etapa: {receipt['status']}.")
        if isinstance(exc, ValueError):
            print(str(exc))
        elif getattr(exc, "orig", None):
            args = exc.orig.args
            print("SQLSTATE:", str(args[0]) if args else "indisponível")
            message = str(args[1]) if len(args) > 1 else ""
            print("Códigos SQL:", re.findall(r"\((\d+)\)", message))
            print("Indicadores:", [word for word in ["timeout", "permission", "CREATE SCHEMA", "first statement", "syntax", "not currently available"]
                                    if word.lower() in message.lower()])
        raise SystemExit(1) from None
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
