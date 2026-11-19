import json
import math
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from sqlalchemy import Integer, Numeric, inspect, text

from pipeline_cetesb import DATA_VERSION, build_sqlalchemy_engine, normalize_map_cache
from avaliar_modelos_ipt import evaluate

app = Flask(__name__)

CACHE_FILE = Path(__file__).parent / "cache_mapa_sp.parquet"
DATA_STORE = {
    "df": None,
    "init_payload": None,
    "station_records": {},
}


def clean_val(v):
    if v is None or pd.isna(v):
        return None
    if isinstance(v, (np.integer, int)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        if math.isnan(v) or math.isinf(v):
            return None
        return round(float(v), 4)
    return str(v)


def restore_sql_numeric_types(df, columns):
    """O driver retorna object/None para colunas numéricas inteiramente NULL."""
    for column in columns:
        name = column["name"]
        if name in df and isinstance(column["type"], (Integer, Numeric)):
            df[name] = pd.to_numeric(df[name], errors="raise")
    return df


def fetch_merged_from_azure_sql() -> pd.DataFrame:
    load_dotenv(override=True)
    conn_str = os.getenv("AZURE_SQL_CONNECTION_STRING", "")
    engine = build_sqlalchemy_engine(conn_str)

    query = text(
        """
        SELECT
            e.codigo_ponto,
            e.sistema_hidrico,
            e.municipio,
            e.uf,
            e.classe,
            e.captacao_bin,
            e.data_coleta,
            e.hora_coleta,
            e.ano,
            e.mes,
            e.trimestre,
            e.estacao_hidrologica,
            e.is_periodo_chuvoso,
            e.i_1_jfm,
            e.i_2_amj,
            e.i_3_jas,
            e.i_4_ond,
            e.latitude_decimal,
            e.longitude_decimal,
            e.lat_x_long,
            e.altitude_m,
            e.chuva_24h,
            e.coloracao_texto,
            e.coloracao_alterada_bin,
            e.temperatura_ar_c,
            e.temperatura_agua_c,
            e.pfthm_observado_ug_l,
            e.cod_observado_mg_l,
            e.cot_observado_mg_l,
            e.cod_utilizado_mg_l,
            e.cor_verdadeira_mg_pt_l,
            e.uv254_equivalente_cm_1,
            e.uv254_equivalente_quadrado,
            e.flag_alto_cot_5mgl,
            e.dbo_mg_l,
            e.dbo_utilizada_mg_l,
            e.dbo_quadrado,
            e.od_mg_l,
            e.od_quadrado,
            e.od_percentual_saturacao,
            e.fosforo_total_mg_l,
            e.fosforo_total_quadrado,
            e.ph,
            e.turbidez_unt,
            e.ecoli_ufc_100ml,
            e.nitrogenio_amoniacal_mg_l,
            e.nitrato_mg_l,
            e.nitrito_mg_l,
            e.nitrogenio_kjeldahl_mg_l,
            e.nitrogenio_total_mg_l,
            e.solido_total_mg_l,
            e.solido_dissolvido_total_mg_l,
            e.solido_suspenso_estimado_mg_l,
            e.clorofila_a_ug_l,
            e.iet_medio,
            e.classificacao_iet,
            e.iqa_estimado,
            e.classificacao_iqa,
            e.flag_iqa_otimo,
            e.aluminio_dissolvido_mg_l,
            e.cobre_dissolvido_mg_l,
            e.ferro_dissolvido_mg_l,
            e.manganes_total_mg_l,
            e.zinco_total_mg_l,
            e.cadmio_total_mg_l,
            e.chumbo_total_mg_l,
            e.cromio_total_mg_l,
            e.mercurio_total_mg_l,
            e.niquel_total_mg_l,
            e.cobre_total_mg_l,
            e.fenois_totais_mg_l,
            e.surfactantes_mg_l,
            e.ecotox_ceriodaphnia_cronico_bin,
            p.pfthm_predito_modelo_cot_ug_l,
            p.pfthm_predito_modelo_cod_ug_l,
            p.pfthm_predito_consenso_ug_l,
            p.pfthm_final_ug_l,
            p.thm_eta_observado_ug_l,
            p.thm_eta_predito_ug_l,
            p.thm_eta_final_ug_l,
            p.risco_observado_thm_100_ms,
            p.risco_observado_thm_80_usepa,
            p.logit_modelo_espacial_ipt,
            p.prob_risco_thm_espacial_ipt,
            p.logit_modelo_fisico_quimico_ipt,
            p.prob_risco_thm_fisico_quimico_ipt,
            p.prob_risco_pfthm_400_regressao,
            p.prob_risco_thm_consolidada,
            p.predicao_risco_thm_100_ms_bin,
            p.predicao_risco_thm_80_usepa_bin,
            p.faixa_risco_ipt
            ,e.versao_dados
            ,e.origem_nitrogenio_total
            ,e.status_iqa
            ,e.status_conama
            ,e.iet_componentes_disponiveis
            ,p.status_modelo_fq
            ,p.status_modelo_espacial
            ,p.status_modelo_pfthm
            ,p.status_thm_eta
            ,p.excede_pfthm_400_proxy_ipt_bin
            ,p.excede_thm_estimado_100_bin
            ,p.excede_thm_estimado_80_bin
            ,p.faixa_risco_espacial_ipt
            ,p.faixa_risco_fq_ipt
            ,e.iqa_experimental
            ,p.origem_pfthm
            ,p.predicao_risco_thm_100_ms_espacial_bin
            ,e.qtd_parametros_conama_avaliados
            ,e.qtd_violacoes_conama
            ,p.thm_eta_estimado_reta_historica_ug_l
            ,p.modelo_pfthm_ipt
            ,p.pfthm_ipt_bruto_ug_l
            ,p.pfthm_predito_ipt_ug_l
            ,p.pfthm_modelo_1_ipt_bruto_ug_l
            ,p.pfthm_modelo_2_ipt_bruto_ug_l
            ,p.pfthm_modelo_1_ipt_ug_l
            ,p.pfthm_modelo_2_ipt_ug_l
            ,p.aplicacao_modelo_pfthm
        FROM dbo.estudo_thm_base e
        INNER JOIN dbo.predicoes_risco_thm p
            ON e.codigo_ponto = p.codigo_ponto
           AND e.data_coleta = p.data_coleta
           AND ISNULL(e.hora_coleta, '') = ISNULL(p.hora_coleta, '')
        ORDER BY e.codigo_ponto, e.data_coleta, e.hora_coleta
        """
    )

    for attempt in range(1, 5):
        try:
            with engine.connect() as conn:
                inspector = inspect(conn)
                columns = inspector.get_columns("estudo_thm_base", schema="dbo")
                columns += inspector.get_columns("predicoes_risco_thm", schema="dbo")
                df = pd.read_sql(query, conn)
            return normalize_map_cache(restore_sql_numeric_types(df, columns))
        except Exception as exc:
            if attempt == 4:
                raise
            print(f"[Tentativa {attempt}/4] Aguardando conexao com Azure SQL Server ({exc.__class__.__name__})...")
            time.sleep(5)
    return pd.DataFrame()


def build_memory_structures(df: pd.DataFrame):
    df = df.copy()
    df = df[df["latitude_decimal"].notna() & df["longitude_decimal"].notna()].copy()
    df["data_coleta_str"] = pd.to_datetime(df["data_coleta"]).dt.strftime("%Y-%m-%d")
    df["ano"] = df["ano"].astype(int)
    df["mes"] = df["mes"].astype(int)
    df["trimestre"] = df["trimestre"].astype(int)
    df["period_quarter"] = df["ano"].astype(str) + "-T" + df["trimestre"].astype(str)
    df["period_month"] = df["ano"].astype(str) + "-" + df["mes"].apply(lambda m: f"{m:02d}")
    df["period_year"] = df["ano"].astype(str)

    stations_meta = []
    for code, grp in df.groupby("codigo_ponto", sort=True):
        first = grp.sort_values(["data_coleta", "hora_coleta"]).iloc[-1]
        stations_meta.append(
            {
                "codigo_ponto": code,
                "sistema_hidrico": clean_val(first["sistema_hidrico"]),
                "municipio": clean_val(first["municipio"]),
                "uf": clean_val(first["uf"]),
                "classe": clean_val(first["classe"]),
                "captacao_bin": int(first["captacao_bin"]) if pd.notna(first["captacao_bin"]) else 0,
                "lat": round(float(first["latitude_decimal"]), 6),
                "lng": round(float(first["longitude_decimal"]), 6),
                "altitude_m": clean_val(first["altitude_m"]),
                "total_coletas": int(len(grp)),
                "primeira_coleta": str(grp["data_coleta_str"].min()),
                "ultima_coleta": str(grp["data_coleta_str"].max()),
                "prob_media_risco": clean_val(grp["prob_risco_thm_consolidada"].mean()),
                "pfthm_max": clean_val(grp["pfthm_final_ug_l"].max()),
                "iqa_medio": clean_val(grp["iqa_estimado"].mean()),
            }
        )

    def build_snapshots(period_col: str) -> tuple[list[str], dict]:
        periods = sorted(df[period_col].dropna().unique().tolist())
        dedup = df.sort_values(["data_coleta_str", "hora_coleta"], kind="stable", na_position="first").drop_duplicates(
            [period_col, "codigo_ponto"], keep="last"
        )
        snapshots = {}
        for per, grp in dedup.groupby(period_col):
            p_dict = {}
            for row in grp.itertuples(index=False):
                p_dict[row.codigo_ponto] = [
                    row.data_coleta_str,
                    clean_val(row.prob_risco_thm_fisico_quimico_ipt),
                    clean_val(row.pfthm_final_ug_l),
                    clean_val(row.pfthm_observado_ug_l),
                    clean_val(row.thm_eta_final_ug_l),
                    clean_val(row.cot_observado_mg_l),
                    clean_val(row.cod_utilizado_mg_l),
                    clean_val(row.iqa_estimado),
                    clean_val(row.iet_medio),
                    clean_val(row.od_mg_l),
                    clean_val(row.dbo_mg_l),
                    clean_val(row.fosforo_total_mg_l),
                    clean_val(row.ph),
                    clean_val(row.turbidez_unt),
                    int(row.chuva_24h) if pd.notna(row.chuva_24h) else None,
                    clean_val(row.faixa_risco_ipt),
                    clean_val(row.classificacao_iqa),
                    clean_val(row.estacao_hidrologica),
                    clean_val(row.prob_risco_thm_espacial_ipt),
                    clean_val(row.pfthm_predito_ipt_ug_l),
                    clean_val(row.origem_pfthm),
                    clean_val(row.modelo_pfthm_ipt),
                    clean_val(row.status_modelo_pfthm),
                    clean_val(row.pfthm_modelo_1_ipt_ug_l),
                    clean_val(row.pfthm_modelo_2_ipt_ug_l),
                    clean_val(row.aplicacao_modelo_pfthm),
                ]
            snapshots[per] = p_dict
        return periods, snapshots

    q_periods, q_snaps = build_snapshots("period_quarter")
    m_periods, m_snaps = build_snapshots("period_month")
    y_periods, y_snaps = build_snapshots("period_year")

    station_records = {}
    df_clean = df.drop(columns=["data_coleta"], errors="ignore").copy()
    df_clean["data_coleta"] = df_clean["data_coleta_str"]
    df_clean = df_clean.replace({np.nan: None, np.inf: None, -np.inf: None})
    for code, grp in df_clean.sort_values(["data_coleta_str", "hora_coleta"], na_position="first").groupby("codigo_ponto", sort=False):
        station_records[code] = grp.to_dict(orient="records")

    ana_json_path = Path(__file__).parent / "ana_etas_sp.json"
    ana_etas = []
    ana_relations = {}
    if ana_json_path.exists():
        with open(ana_json_path, "r", encoding="utf-8") as f:
            ana_payload = json.load(f)
            ana_etas = ana_payload.get("ana_etas", [])
            ana_relations = {
                code: relation for code, relation in ana_payload.get("relations_by_station", {}).items()
                if relation.get("vinculo_confirmado") is False
                and relation.get("distancia_km", float("inf")) <= 15
            }

    DATA_STORE["df"] = df
    DATA_STORE["station_records"] = station_records
    DATA_STORE["init_payload"] = {
        "stations": stations_meta,
        "periods": {
            "quarter": q_periods,
            "month": m_periods,
            "year": y_periods,
        },
        "snapshots": {
            "quarter": q_snaps,
            "month": m_snaps,
            "year": y_snaps,
        },
        "municipios": sorted(df["municipio"].dropna().unique().tolist()),
        "classes": sorted(df["classe"].dropna().unique().tolist()),
        "ana_etas": ana_etas,
        "ana_relations": ana_relations,
        "total_coletas": len(df),
        "versao_dados": DATA_VERSION,
        "avaliacao_modelos_ipt": evaluate(df),
        "aviso_metodologico": "Equações IPT COD em todos os pontos com COD exato. Fora dos pontos originais: extrapolação experimental não validada. Risco THM não calculado.",
    }


def load_data(force_db: bool = False):
    if not force_db and CACHE_FILE.exists():
        print(f"Carregando dados do cache local: {CACHE_FILE} ...")
        df = pd.read_parquet(CACHE_FILE)
    else:
        print("Consultando dbo.estudo_thm_base e dbo.predicoes_risco_thm no Azure SQL Server...")
        df = fetch_merged_from_azure_sql()
        validate_data_version(df)
        temporary = CACHE_FILE.with_suffix(".tmp.parquet")
        df.to_parquet(temporary, index=False)
        os.replace(temporary, CACHE_FILE)
        print(f"Cache salvo em {CACHE_FILE} ({len(df):,} registros).")
    validate_data_version(df)
    build_memory_structures(df)
    print(f"Dados prontos em memoria: {len(DATA_STORE['init_payload']['stations'])} pontos e {len(df):,} coletas.")


def validate_data_version(df):
    if df.empty or "versao_dados" not in df or not df["versao_dados"].eq(DATA_VERSION).fillna(False).all():
        raise ValueError("Dados anteriores à revisão. Execute python reprocessar_local.py.")


@app.route("/")
def index():
    return render_template("mapa_sp.html")


@app.route("/api/init")
def api_init():
    if DATA_STORE["init_payload"] is None:
        load_data(force_db=False)
    return jsonify(DATA_STORE["init_payload"])


@app.route("/api/station/<codigo_ponto>")
def api_station(codigo_ponto: str):
    if DATA_STORE["init_payload"] is None:
        load_data(force_db=False)
    code = codigo_ponto.upper().strip()
    recs = DATA_STORE["station_records"].get(code, [])
    return jsonify({"codigo_ponto": code, "total": len(recs), "measurements": recs})


@app.route("/api/refresh-db", methods=["POST"])
def api_refresh_db():
    load_data(force_db=True)
    return jsonify(
        {
            "status": "ok",
            "stations": len(DATA_STORE["init_payload"]["stations"]),
            "rows": len(DATA_STORE["df"]),
        }
    )


if __name__ == "__main__":
    load_data(force_db=False)
    port = int(os.getenv("PORT", "5050"))
    app.run(host="127.0.0.1", port=port, debug=False)
