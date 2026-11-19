"""Recalibração isolada de PFTHM; não altera as tabelas históricas nem o mapa.

Protocolo fixado antes do ajuste: treino até 2020; validação 2021–2022;
teste temporal desde 2023. Grupos de pontos por SHA-256: 0=testes espaciais,
1=validação espacial, 2/3/4=desenvolvimento. Testes nunca escolhem parâmetros.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import time

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import text

from avaliar_modelos_ipt import metrics
from pipeline_cetesb import COLLECTION_KEY, DATA_VERSION, build_sqlalchemy_engine, sql_frame

ROOT = Path(__file__).parent
TABLE = "predicoes_pfthm_recalibradas"
COD = "cod_observado_mg_l"
TARGET = "pfthm_observado_ug_l"
LAMBDAS = (1., 10., 100., 1000.)


def point_group(code):
    return int(hashlib.sha256(("pfthm-recal-v1:" + str(code)).encode()).hexdigest()[:8], 16) % 5


def valid_inputs(frame):
    return (frame[COD].notna() & np.isfinite(frame[COD]) & frame[COD].ge(0)
            & pd.to_datetime(frame.data_coleta, errors="coerce").notna())


def prepare(frame):
    frame = frame.copy().reset_index(drop=True)
    if frame.duplicated(COLLECTION_KEY).any():
        raise ValueError("Coletas duplicadas.")
    frame["data_coleta"] = pd.to_datetime(frame.data_coleta)
    frame["grupo"] = frame.codigo_ponto.map(point_group)
    frame["par_exato"] = (valid_inputs(frame) & frame[TARGET].notna()
                          & np.isfinite(frame[TARGET]) & frame[TARGET].ge(0))
    return frame


def split_masks(frame):
    pair = frame.par_exato
    pre2021 = frame.data_coleta.lt("2021-01-01")
    pre2023 = frame.data_coleta.lt("2023-01-01")
    return {
        "treino": pair & frame.grupo.ge(2) & pre2021,
        "validacao_temporal": pair & frame.grupo.ge(2) & ~pre2021 & pre2023,
        "validacao_espacial": pair & frame.grupo.eq(1) & pre2023,
        "ajuste_final": pair & frame.grupo.ne(0) & pre2023,
        "teste_espacial": pair & frame.grupo.eq(0),
        "teste_temporal": pair & frame.grupo.ne(0) & ~pre2023,
    }


def design(frame, model):
    z = (frame[COD].to_numpy(dtype=float) - model["centro_cod"]) / model["escala_cod"]
    jfm = pd.to_datetime(frame.data_coleta).dt.month.isin([1, 2, 3]).to_numpy(dtype=float)
    cols = [np.ones(len(frame)), z, z ** 2 if model["estrutura"] == "quadratico" else jfm]
    if model["estrutura"] == "misto":
        cols.extend(frame.codigo_ponto.eq(code).to_numpy(dtype=float) for code in model["pontos"])
    return np.column_stack(cols)


def fit_model(frame, structure, penalty=0.):
    if frame.empty or not frame.par_exato.all():
        raise ValueError("Ajuste exige exclusivamente pares medidos exatos.")
    model = {
        "estrutura": structure, "lambda": penalty,
        "centro_cod": float(frame[COD].mean()),
        "escala_cod": float(frame[COD].std(ddof=0)) or 1.,
        "cod_min": float(frame[COD].min()), "cod_max": float(frame[COD].max()),
        "pontos": sorted(frame.codigo_ponto.unique().tolist()) if structure == "misto" else [],
        "pontos_ajuste": sorted(frame.codigo_ponto.unique().tolist()),
        "n_ajuste": len(frame), "ultima_data_ajuste": str(frame.data_coleta.max().date()),
    }
    matrix = design(frame, model)
    target = frame[TARGET].to_numpy(dtype=float)
    if structure == "misto":
        regularization = np.zeros((len(model["pontos"]), matrix.shape[1]))
        regularization[:, 3:] = np.eye(len(model["pontos"])) * np.sqrt(penalty)
        matrix = np.vstack([matrix, regularization])
        target = np.concatenate([target, np.zeros(len(model["pontos"]))])
    beta = np.linalg.lstsq(matrix, target, rcond=None)[0]
    model["coeficientes_padronizados"] = beta.tolist()
    center, scale = model["centro_cod"], model["escala_cod"]
    if structure == "quadratico":
        coefficients = {"intercepto": beta[0] - beta[1]*center/scale + beta[2]*center**2/scale**2,
                        "cod": beta[1]/scale - 2*beta[2]*center/scale**2, "cod_quadrado": beta[2]/scale**2}
    else:
        coefficients = {"intercepto": beta[0] - beta[1]*center/scale, "cod": beta[1]/scale, "jfm": beta[2]}
    model["coeficientes_unidades_originais"] = {key: float(value) for key, value in coefficients.items()}
    model["ajustes_por_ponto"] = dict(zip(model["pontos"], beta[3:].tolist()))
    return model


def predict(frame, model):
    result = pd.Series(np.nan, index=frame.index, dtype=float)
    valid = valid_inputs(frame)
    result.loc[valid] = design(frame.loc[valid], model) @ np.array(model["coeficientes_padronizados"])
    return result


def score(frame, predicted):
    if frame.empty:
        return {"n": 0, "pontos": 0, "rmse_ug_l": None}
    result = metrics(frame[TARGET], predicted)
    result.update(pontos=int(frame.codigo_ponto.nunique()),
                  negativos=int(predicted.lt(0).sum()))
    # Cada ponto pesa igualmente nesta métrica suplementar.
    error = pd.DataFrame({"codigo_ponto": frame.codigo_ponto, "erro2": (predicted-frame[TARGET])**2})
    result["rmse_medio_por_ponto_ug_l"] = float(error.groupby("codigo_ponto").erro2.mean().pow(.5).mean())
    return result


def calibrate(frame):
    frame = prepare(frame)
    masks = split_masks(frame)
    if any(int(masks[k].sum()) < 10 for k in masks):
        raise ValueError("Partições insuficientes para o protocolo temporal/espacial fixado.")
    specs = {"modelo1": ("linear_jfm", 0.), "modelo2": ("quadratico", 0.)}
    specs.update({f"misto_lambda_{int(lam)}": ("misto", lam) for lam in LAMBDAS})
    candidates, validation = {}, {}
    for name, (structure, penalty) in specs.items():
        model = fit_model(frame.loc[masks["treino"]], structure, penalty)
        candidates[name] = model
        scores = {split: score(frame.loc[masks[split]], predict(frame.loc[masks[split]], model))
                  for split in ("validacao_temporal", "validacao_espacial")}
        scores["criterio_selecao_rmse"] = float(np.mean([v["rmse_ug_l"] for v in scores.values()]))
        validation[name] = scores
    winner = min(validation, key=lambda n: (validation[n]["criterio_selecao_rmse"], n))
    mixed = min((n for n in validation if n.startswith("misto_")),
                key=lambda n: (validation[n]["criterio_selecao_rmse"], n))
    # Escolha congelada. Reajuste somente com dados anteriores ao teste temporal
    # e sem QUALQUER medição dos pontos reservados ao teste espacial.
    final_names = ["modelo1", "modelo2", mixed]
    final = {name: fit_model(frame.loc[masks["ajuste_final"]], *specs[name]) for name in final_names}
    final_mean = float(frame.loc[masks["ajuste_final"], TARGET].mean())
    training_mean = float(frame.loc[masks["treino"], TARGET].mean())
    validation["media_treino_referencia"] = {
        split: score(frame.loc[masks[split]], pd.Series(training_mean, index=frame.index[masks[split]]))
        for split in ("validacao_temporal", "validacao_espacial")}
    tests = {}
    for split in ("teste_temporal", "teste_espacial"):
        subset = frame.loc[masks[split]]
        cod = subset[COD]
        historical = {
            "ipt_historico_1": 235 + 15.9*cod + 282*subset.data_coleta.dt.month.isin([1, 2, 3]).astype(int),
            "ipt_historico_2": -1983 + 669*cod - 43.3*cod**2,
            "media_ajuste_referencia": pd.Series(final_mean, index=subset.index),
        }
        predictions = {name: predict(subset, model) for name, model in final.items()} | historical
        tests[split] = {name: score(subset, values) for name, values in predictions.items()}
        selected = final[winner]
        accepted = (predictions[winner].ge(0) & np.isfinite(predictions[winner])
                    & subset[COD].between(selected["cod_min"], selected["cod_max"]))
        tests[split]["selecionado_somente_dominio_aceito"] = score(subset.loc[accepted], predictions[winner].loc[accepted])
        tests[split]["cod_fora_da_faixa_ajuste"] = int((~subset[COD].between(selected["cod_min"], selected["cod_max"])).sum())
        seen = subset.codigo_ponto.isin(selected["pontos_ajuste"])
        tests[split]["selecionado_pontos_conhecidos"] = score(subset.loc[seen], predictions[winner].loc[seen])
        tests[split]["selecionado_pontos_novos"] = score(subset.loc[~seen], predictions[winner].loc[~seen])
    output = frame[COLLECTION_KEY + [COD, TARGET]].copy()
    output["grupo_particao_ponto"] = frame.grupo.map({0: "Teste espacial reservado", 1: "Validação espacial", 2: "Desenvolvimento", 3: "Desenvolvimento", 4: "Desenvolvimento"})
    output["etapa_selecao"] = "Sem par medido elegível"
    for split in ("treino", "validacao_temporal", "validacao_espacial", "teste_espacial", "teste_temporal"):
        output.loc[masks[split], "etapa_selecao"] = split
    output["papel_avaliacao_final"] = "Predição sem referência medida elegível"
    for split in ("ajuste_final", "teste_temporal", "teste_espacial"):
        output.loc[masks[split], "papel_avaliacao_final"] = split
    selected = final[winner]
    output["usado_ajuste_final_bin"] = masks["ajuste_final"].astype(int)
    output["ponto_no_ajuste_bin"] = frame.codigo_ponto.isin(selected["pontos_ajuste"]).astype(int)
    output["cod_min_ajuste_mg_l"] = selected["cod_min"]
    output["cod_max_ajuste_mg_l"] = selected["cod_max"]
    output["fora_faixa_cod_ajuste_bin"] = (~frame[COD].between(selected["cod_min"], selected["cod_max"])).astype(float).where(valid_inputs(frame))
    for name, model in final.items():
        column = "misto" if name == mixed else name
        output[f"pfthm_recal_{column}_bruto_ug_l"] = predict(frame, model)
    raw = predict(frame, selected)
    valid = valid_inputs(frame)
    accepted = valid & np.isfinite(raw) & raw.ge(0) & output.fora_faixa_cod_ajuste_bin.eq(0)
    output["pfthm_recalibrado_bruto_ug_l"] = raw
    output["pfthm_recalibrado_ug_l"] = raw.where(accepted)
    output["modelo_selecionado"] = winner
    output["modelo_misto_comparado"] = mixed
    output["status_predicao"] = "Indeterminado: COD exato/data não disponível"
    output.loc[valid, "status_predicao"] = "Inválido: resultado negativo ou não finito"
    output.loc[valid & output.fora_faixa_cod_ajuste_bin.eq(1), "status_predicao"] = "Fora da faixa de COD do ajuste; consultar resultado bruto"
    output.loc[accepted & output.ponto_no_ajuste_bin.eq(1), "status_predicao"] = "Experimental: ponto com observações no ajuste; consultar desempenho de teste"
    output.loc[accepted & output.ponto_no_ajuste_bin.eq(0), "status_predicao"] = "Experimental: transferência para ponto sem observações no ajuste"
    report = {
        "protocolo": "Treino <=2020, validação temporal 2021–2022; hash de ponto 1 validação espacial <=2022, 0 teste espacial em todos os anos. Reajuste <=2022 em grupos 1–4. Teste temporal >=2023 em grupos 1–4.",
        "criterio_selecao": "Menor média dos RMSE brutos das validações temporal e espacial; testes não selecionam modelos.",
        "coeficientes_finais_ajustados_sem_testes": final,
        "media_ajuste_referencia": final_mean,
        "modelo_selecionado": winner, "modelo_misto_comparado": mixed,
        "validacao": validation, "testes_reservados": tests,
        "particoes": {name: {"coletas": int(mask.sum()), "pontos": int(frame.loc[mask, "codigo_ponto"].nunique())}
                      for name, mask in masks.items()},
        "pontos_reservados_teste_espacial": sorted(frame.loc[frame.par_exato & frame.grupo.eq(0), "codigo_ponto"].unique().tolist()),
        "pares_medidos_elegiveis": int(frame.par_exato.sum()),
        "coletas_saida": len(output),
        "estimativas_selecionadas_aceitas": int(accepted.sum()),
        "pontos_com_estimativa": int(frame.loc[accepted, "codigo_ponto"].nunique()),
        "notas": [
            "Alvo exclusivamente PFTHM observado exato; nunca pfthm_final_ug_l.",
            "Coeficientes globais sem penalização; modelo misto penaliza somente interceptos dos pontos (agrupamento parcial). Ponto desconhecido recebe ajuste local zero.",
            "Nenhuma remoção de extremos baseada no erro; métricas brutas incluem negativos e extrapolações de COD.",
            "Colunas brutas permitem auditoria; previsão selecionada exclui negativos/não finitos e COD fora do mínimo/máximo de ajuste.",
            "Previsões de linhas de ajuste são internas à amostra; métricas de teste utilizam somente partições reservadas.",
            "As datas são cortes prospectivos simulados; não é validação prospectiva nem aprovação regulatória.",
            "Não houve ajuste final com dados de teste. Nenhuma concentração de THM na ETA ou probabilidade de potabilidade é calculada.",
        ],
    }
    return output, report


def verify_table(conn, schema, output):
    actual = pd.read_sql(text(f"SELECT * FROM [{schema}].[{TABLE}]"), conn)
    from validar_equivalencia import compare_values
    delta = compare_values(output, actual, COLLECTION_KEY)
    if delta != 0:
        raise ValueError(f"Publicação alterou valores: diferença máxima {delta}")
    return {"linhas": len(actual), "colunas": len(actual.columns), "diferenca_maxima": delta}


def publish(output, directory):
    load_dotenv(ROOT / ".env", override=True)
    engine = build_sqlalchemy_engine(os.getenv("AZURE_SQL_CONNECTION_STRING", ""))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    staging, backup = f"pfthm_recal_stage_{stamp}", f"pfthm_recal_backup_{stamp}"
    receipt = {"tabela": f"dbo.{TABLE}", "status": "preparando", "schema_temporario": staging}
    path = directory / "publicacao_sql.json"
    try:
        for attempt in range(4):
            try:
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                break
            except Exception:
                if attempt == 3:
                    raise
                print("Aguardando disponibilidade do SQL...", flush=True)
                time.sleep(10)
        with engine.begin() as conn:
            dependencies = conn.execute(text("SELECT COUNT(*) FROM sys.foreign_keys WHERE parent_object_id=OBJECT_ID(:name) OR referenced_object_id=OBJECT_ID(:name)"),
                                        {"name": f"dbo.{TABLE}"}).scalar_one()
            if dependencies:
                raise ValueError("Tabela existente com dependências; publicação automática interrompida.")
            permissions = conn.execute(text(
                "SELECT p.state,p.permission_name,u.name AS grantee,p.minor_id FROM sys.database_permissions p "
                "JOIN sys.database_principals u ON u.principal_id=p.grantee_principal_id "
                "WHERE p.class=1 AND p.major_id=OBJECT_ID(:name)"), {"name": f"dbo.{TABLE}"}).mappings().all()
            if any(p["minor_id"] for p in permissions):
                raise ValueError("Permissões por coluna exigem migração específica.")
            conn.execute(text(f"CREATE SCHEMA [{staging}]"))
        data, types = sql_frame(output)
        print(f"Publicando {len(output):,} coletas na tabela separada...", flush=True)
        data.to_sql(TABLE, engine, schema=staging, index=False, if_exists="fail", chunksize=250, dtype=types)
        with engine.connect() as conn:
            receipt["verificacao_previa"] = verify_table(conn, staging, output)
        from publicar_resultados_sql import identifier
        with engine.begin() as conn:
            conn.execute(text("SET LOCK_TIMEOUT 60000"))
            if conn.execute(text("SELECT OBJECT_ID(:name,'U')"), {"name": f"dbo.{TABLE}"}).scalar():
                conn.execute(text(f"CREATE SCHEMA [{backup}]"))
                conn.execute(text(f"ALTER SCHEMA [{backup}] TRANSFER dbo.[{TABLE}]"))
                receipt["schema_backup"] = backup
            conn.execute(text(f"ALTER SCHEMA dbo TRANSFER [{staging}].[{TABLE}]"))
            for permission in permissions:
                action = "DENY" if permission["state"] == "D" else "GRANT"
                grant_option = " WITH GRANT OPTION" if permission["state"] == "W" else ""
                conn.execute(text(f"{action} {permission['permission_name']} ON OBJECT::dbo.[{TABLE}] "
                                  f"TO {identifier(permission['grantee'])}{grant_option}"))
            receipt["verificacao_final"] = verify_table(conn, "dbo", output)
            conn.execute(text(f"DROP SCHEMA [{staging}]"))
        receipt["status"] = "concluido"
    except Exception as exc:
        receipt.update(status="falha", erro_tipo=type(exc).__name__)
        raise
    finally:
        engine.dispose()
        path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publicar-sql", action="store_true")
    args = parser.parse_args()
    source = ROOT / "resultados_v2" / "estudo_thm_base.parquet"
    frame = pd.read_parquet(source)
    if not frame.versao_dados.eq(DATA_VERSION).all():
        raise ValueError("Base com versão incompatível.")
    run_id = datetime.now(timezone.utc).strftime("pfthm_recal_%Y%m%dT%H%M%SZ")
    directory = ROOT / "resultados_recalibracao" / run_id
    directory.mkdir(parents=True, exist_ok=False)
    output, report = calibrate(frame)
    output["versao_modelo"] = run_id
    output["versao_dados_origem"] = DATA_VERSION
    output["data_coleta"] = output.data_coleta.dt.strftime("%Y-%m-%d")
    for col in output.select_dtypes(include="object"):
        output[col] = output[col].astype("string")
    artifact = directory / f"{TABLE}.parquet"
    output.to_parquet(artifact, index=False)
    report.update(versao_modelo=run_id, fonte=str(source), fonte_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                  codigo_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  parquet_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
                  dependencias={name: importlib.metadata.version(name) for name in ["numpy", "pandas", "sqlalchemy", "pyarrow"]})
    (directory / "modelos_e_avaliacao.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ["versao_modelo", "modelo_selecionado", "particoes", "testes_reservados"]}, ensure_ascii=False, indent=2), flush=True)
    if args.publicar_sql:
        publish(output, directory)
    print(f"ARTEFATOS: {directory}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Exceções de conexão não devem revelar a connection string.
        print(f"Recalibração interrompida: {type(error).__name__}", flush=True)
        raise SystemExit(1) from None
