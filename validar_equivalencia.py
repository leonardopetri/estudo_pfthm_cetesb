"""Compara todos os valores dos artefatos e, opcionalmente, do SQL (somente leitura)."""
import argparse
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
import hashlib
import json
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import inspect, text

from consolidar_brutos_excel import fast_read_cetesb_xlsx
from pipeline_cetesb import (
    COLLECTION_KEY, build_sqlalchemy_engine, discover_excel_files,
    load_and_clean_raw_file, normalize_map_cache, sql_frame,
)

ROOT = Path(__file__).parent
ANALYTICAL = ["features_coletas_wide", "features_resumo_parametros", "estudo_thm_base", "predicoes_risco_thm"]
SUMMARY_KEY = ["codigo_ponto", "sistema_hidrico", "classe", "municipio", "tipo_parametro", "parametro", "unidade"]


def verify_reader(path):
    standard = load_and_clean_raw_file(path)
    fast = fast_read_cetesb_xlsx(path)
    pd.testing.assert_frame_equal(standard, fast, check_dtype=False, check_exact=True)
    return len(standard)


def keys_for(name):
    if name == "features_resumo_parametros":
        return SUMMARY_KEY
    if name == "ana_captacoes_etas_sp":
        return ["id_captacao_ana"]
    if name == "relacao_cetesb_ana_etas":
        return ["codigo_ponto"]
    return COLLECTION_KEY


def indexed(frame, keys):
    frame = frame.copy()
    for col in keys:
        if col == "data_coleta":
            frame[col] = pd.to_datetime(frame[col]).dt.strftime("%Y-%m-%d")
        frame[col] = frame[col].astype("string")
    if frame.duplicated(keys).any():
        raise AssertionError(f"Chaves duplicadas: {keys}")
    return frame.set_index(keys, drop=False)


def compare_values(expected, actual, keys):
    if set(expected.columns) != set(actual.columns):
        raise AssertionError(f"Colunas divergentes: {sorted(set(expected.columns) ^ set(actual.columns))}")
    left, right = indexed(expected, keys), indexed(actual, keys)
    if len(left) != len(right) or not right.index.isin(left.index).all():
        raise AssertionError("Quantidade ou chaves divergentes.")
    left = left.reindex(right.index)
    max_delta = 0.0
    for column in left:
        a, b = left[column], right[column]
        if not np.array_equal(a.isna().to_numpy(), b.isna().to_numpy()):
            raise AssertionError(f"Ausências divergentes na coluna {column}")
        valid = a.notna()
        if pd.api.types.is_numeric_dtype(a):
            av = a[valid].to_numpy(dtype=float)
            bv = pd.to_numeric(b[valid], errors="raise").to_numpy(dtype=float)
            if len(av):
                delta = float(np.max(np.abs(av - bv)))
                max_delta = max(max_delta, delta)
                if not np.isclose(av, bv, atol=1e-12, rtol=1e-12).all():
                    raise AssertionError(f"Valores divergentes em {column}; diferença máxima {delta}")
        elif not a[valid].astype("string").equals(b[valid].astype("string")):
            raise AssertionError(f"Textos/datas divergentes na coluna {column}")
    return max_delta


def check_sql_schema(engine, name, frame):
    data, explicit = sql_frame(frame)
    actual = {col["name"]: col["type"] for col in inspect(engine).get_columns(name, schema="dbo")}
    if set(actual) != set(data):
        raise AssertionError(f"Esquema de colunas SQL divergente: {name}")
    for col in data:
        # NVARCHAR sem COLLATE explícito herda a collation do banco; a reflexão
        # acrescenta esse metadado ao texto do tipo. Comparamos tipo e tamanho.
        actual_type = str(actual[col]).upper().split(" COLLATE ", 1)[0]
        if col in explicit:
            wanted = str(explicit[col]).upper()
        elif pd.api.types.is_bool_dtype(data[col]):
            wanted = "BIT"
        elif pd.api.types.is_integer_dtype(data[col]):
            wanted = "INTEGER" if data[col].dtype.itemsize <= 4 else "BIGINT"
        else:
            wanted = "FLOAT"
        if actual_type != wanted:
            raise AssertionError(f"Tipo SQL divergente: {name}.{col}: {actual_type} / {wanted}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, default=ROOT / "resultados_equivalencia")
    parser.add_argument("--reference", type=Path, default=ROOT / "resultados_v2")
    parser.add_argument("--check-readers", action="store_true")
    parser.add_argument("--check-db", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    report = {"data": datetime.now().isoformat(), "status": "em_validacao",
              "candidate": str(args.candidate.resolve()), "reference": str(args.reference.resolve()),
              "tolerancia_numerica": {"absoluta": 1e-12, "relativa": 1e-12}, "comparacoes": {}}
    try:
        if args.check_readers:
            files = discover_excel_files(str(ROOT))
            total = 0
            with ProcessPoolExecutor(max_workers=args.workers) as pool:
                for index, count in enumerate(pool.map(verify_reader, files), 1):
                    total += count
                    if index % 50 == 0 or index == len(files):
                        print(f"Leitores equivalentes: {index}/{len(files)} arquivos, {total:,} medições", flush=True)
            report["leitores"] = {"arquivos": len(files), "medicoes": total, "equivalencia_exata": True}
        frames = {}
        for name in ANALYTICAL:
            expected = pd.read_parquet(args.reference / f"{name}.parquet")
            current = pd.read_parquet(args.candidate / f"{name}.parquet")
            delta = compare_values(expected, current, keys_for(name))
            frames[name] = current
            report["comparacoes"][name] = {"linhas": len(current), "colunas": len(current.columns),
                                         "diferenca_maxima_local": delta}
            print(f"ARTEFATOS OK: {name}, {len(current):,} linhas, todas as colunas", flush=True)
        local_cache = pd.read_parquet(args.candidate / "cache_mapa_sp.parquet")
        old_cache = pd.read_parquet(args.reference / "cache_mapa_sp.parquet")
        compare_values(normalize_map_cache(old_cache), local_cache, COLLECTION_KEY)
        if args.check_db:
            load_dotenv(ROOT / ".env", override=True)
            engine = build_sqlalchemy_engine(os.getenv("AZURE_SQL_CONNECTION_STRING", ""))
            try:
                for attempt in range(4):
                    try:
                        with engine.connect() as conn:
                            conn.execute(text("SELECT 1"))
                        break
                    except Exception:
                        if attempt == 3:
                            raise
                        print("Aguardando disponibilidade do Azure SQL...", flush=True)
                        time.sleep(10)
                ana = json.loads((ROOT / "ana_etas_sp.json").read_text(encoding="utf-8"))
                frames["ana_captacoes_etas_sp"] = pd.DataFrame(ana["ana_etas"])
                frames["relacao_cetesb_ana_etas"] = pd.DataFrame(ana["relations_by_station"].values())
                for name, frame in frames.items():
                    check_sql_schema(engine, name, frame)
                    keys = keys_for(name)
                    expected = indexed(frame, keys)
                    seen, count, maximum = set(), 0, 0.0
                    with engine.connect() as conn:
                        for chunk in pd.read_sql(text(f"SELECT * FROM dbo.[{name}]"), conn, chunksize=1000):
                            actual = indexed(chunk, keys)
                            if seen.intersection(actual.index):
                                raise AssertionError(f"Chaves repetidas no SQL: {name}")
                            seen.update(actual.index)
                            match = expected.loc[actual.index].reset_index(drop=True)
                            maximum = max(maximum, compare_values(match, chunk, keys))
                            count += len(chunk)
                    if count != len(frame):
                        raise AssertionError(f"Quantidade SQL divergente: {name}")
                    report["comparacoes"].setdefault(name, {}).update(
                        linhas_sql=count, esquema_sql_equivalente=True, diferenca_maxima_sql=maximum)
                    print(f"SQL OK: {name}, {count:,} linhas, todos os valores/tipos", flush=True)
                from app_mapa_sp import fetch_merged_from_azure_sql
                sql_cache = fetch_merged_from_azure_sql()
                pd.testing.assert_frame_equal(local_cache, sql_cache, check_exact=False, atol=1e-12, rtol=1e-12)
                report["cache"] = {"linhas": len(sql_cache), "colunas": len(sql_cache.columns),
                                   "valores_tipos_ordem_equivalentes": True}
                print(f"CACHE LOCAL = SQL: {len(sql_cache):,} linhas, {len(sql_cache.columns)} colunas", flush=True)
            finally:
                engine.dispose()
        report["codigo_sha256"] = {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in ["pipeline_cetesb.py", "reprocessar_local.py", "publicar_resultados_sql.py", "app_mapa_sp.py"]
        }
        report["status"] = "equivalente"
    except Exception as exc:
        report["status"] = "falha"
        report["erro_tipo"] = type(exc).__name__
        # Exceções locais de comparação não contêm credenciais de conexão.
        if isinstance(exc, (AssertionError, KeyError)):
            report["erro_comparacao"] = str(exc)[:1500]
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        raise SystemExit(1) from None
    finally:
        (args.candidate / "equivalencia.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("VALIDAÇÃO COMPLETA: equivalente", flush=True)


if __name__ == "__main__":
    main()
