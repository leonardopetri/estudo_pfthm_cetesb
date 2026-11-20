import argparse
import glob
import json
import math
import os
import re
import sys
import time
import unicodedata
import urllib.parse
from pathlib import Path

import numpy as np
import pandas as pd
import pyodbc
from dotenv import load_dotenv
from sqlalchemy import Date, NVARCHAR, create_engine, text

DATA_VERSION = "2026-11-17-v4-ipt-cod"
COLLECTION_KEY = ["codigo_ponto", "data_coleta", "hora_coleta"]
CORE_PARAMETERS = {
    "ph", "oxigenio_dissolvido", "dbo_5_20", "fosforo_total", "turbidez",
    "nitrogenio_amoniacal", "nitrogenio_nitrato", "nitrogenio_nitrito",
    "nitrogenio_kjeldahl", "nitrogenio_total", "clorofila_a", "feofitina_a",
    "solido_total", "solido_dissolvido_total", "potencial_de_formacao_de_thm",
    "carbono_organico_total", "carbono_organico_dissolvido", "temperatura_da_agua",
    "temperatura_do_ar", "escherichia_coli", "cor_verdadeira",
}


def prepare_measurements(df_all: pd.DataFrame, censor_factor=None) -> pd.DataFrame:
    """Preserva limites e unidades; valores censurados não são medições exatas."""
    if censor_factor is not None and not 0 <= censor_factor <= 1:
        raise ValueError("O fator de censura deve estar entre 0 e 1.")
    df = df_all.copy()
    # TA98/TA100 + S9 e - S9 são ensaios diferentes.
    df["param_slug"] = df["parametro"].map(
        lambda name: slugify(re.sub(r"\s+-\s+", " menos ", str(name)).replace("+", " mais "))
    )
    df["qualificador"] = df["sinal"].fillna("").str.strip().replace(
        {"": "=", "≤": "<=", "≥": ">="}
    )
    unit = df["unidade"].fillna("").str.strip().str.replace("μ", "µ", regex=False)
    mass = unit.map({"ng/L": 1e-6, "µg/L": 1e-3, "ug/L": 1e-3, "mg/L": 1.0, "g/L": 1000.0})
    micro = df["param_slug"].isin(
        ["potencial_de_formacao_de_thm", "clorofila_a", "feofitina_a"]
    )
    df["unidade_padronizada"] = unit.where(mass.isna(), "mg/L")
    df.loc[micro & mass.notna(), "unidade_padronizada"] = "µg/L"
    factor = mass.where(~micro, mass * 1000).fillna(1.0)
    conductivity = unit.map({"µS/cm": 1.0, "uS/cm": 1.0, "mS/cm": 1000.0})
    factor = factor.where(conductivity.isna(), conductivity)
    df.loc[conductivity.notna(), "unidade_padronizada"] = "µS/cm"
    df["limite_numerico"] = df["valor_numerico"] * factor
    df["valor_ajustado"] = df["limite_numerico"].where(df["qualificador"].eq("="))
    # Opt-in, apenas para análises exploratórias; nunca altera o valor bruto.
    if censor_factor is not None:
        low = df["qualificador"].isin(["<", "<="])
        df.loc[low, "valor_ajustado"] = df.loc[low, "limite_numerico"] * censor_factor
    numeric = df[df["valor_numerico"].notna()]
    mixed = numeric.groupby("param_slug")["unidade_padronizada"].nunique()
    if mixed.gt(1).any():
        raise ValueError(f"Unidades incompatíveis: {mixed[mixed.gt(1)].index.tolist()}")
    return df


def threshold_result(value, qualifier, limit, *, minimum=False, strict=False):
    """Triagem de um limite com resultado 0, 1 ou indeterminado (NaN)."""
    result = pd.Series(np.nan, index=value.index)
    limit = pd.Series(limit, index=value.index, dtype=float)
    valid = value.notna() & limit.notna()
    exact = valid & qualifier.eq("=")
    if minimum:
        result.loc[exact] = (value.le(limit) if strict else value.lt(limit))[exact].astype(float)
        ok = (qualifier.eq(">") & value.ge(limit)) | (
            qualifier.eq(">=") & (value.gt(limit) if strict else value.ge(limit))
        )
        bad = (qualifier.eq("<") & value.le(limit)) | (
            qualifier.eq("<=") & (value.le(limit) if strict else value.lt(limit))
        )
    else:
        result.loc[exact] = value.gt(limit)[exact].astype(float)
        ok = qualifier.isin(["<", "<="]) & value.le(limit)
        bad = (qualifier.eq(">") & value.ge(limit)) | (qualifier.eq(">=") & value.gt(limit))
    result.loc[valid & ok] = 0.0
    result.loc[valid & bad] = 1.0
    return result


def add_conama_screening(base: pd.DataFrame) -> None:
    """Resolução 357/2005: triagem por amostra, não declaração de conformidade."""
    cls = base["classe"].fillna("").str.lower()
    fresh = cls.isin(["classe 1", "classe 2", "classe 3", "classe 4"])
    brackish = cls.isin(["salobra 1", "salobra 2", "salobra 3"])
    number = pd.to_numeric(cls.str.extract(r"(\d+)$", expand=False), errors="coerce")
    base["classe_conama_num"] = number.where(fresh | brackish)
    base["categoria_agua"] = np.select([fresh, brackish], ["Doce", "Salobra"], default="Não avaliada")
    environment = base["tipo_de_sistema_hidrico"].fillna("").map(slugify)
    lentic = environment.str.contains("lentico") & ~environment.str.contains("intermedi")
    lotic = environment.str.contains("lotico")
    intermediate = environment.str.contains("intermedi")

    def limits(fresh_map, brackish_map=None):
        return number.map(fresh_map).where(fresh, number.map(brackish_map or {}).where(brackish))

    def assess(slug, lim, minimum=False, strict=False):
        value = base.get(f"lim_{slug}", pd.Series(np.nan, index=base.index))
        qual = base.get(f"qual_{slug}", pd.Series(None, index=base.index, dtype=object))
        return threshold_result(value, qual, lim, minimum=minimum, strict=strict)

    od_lim = limits({1: 6, 2: 5, 3: 4, 4: 2}, {1: 5, 2: 4, 3: 3})
    base["violacao_od"] = assess("oxigenio_dissolvido", od_lim, minimum=True)
    class4 = fresh & number.eq(4)
    base.loc[class4, "violacao_od"] = assess("oxigenio_dissolvido", od_lim, minimum=True, strict=True)[class4]
    base["violacao_dbo"] = assess("dbo_5_20", limits({1: 3, 2: 5, 3: 10}))
    base["violacao_turbidez"] = assess("turbidez", limits({1: 40, 2: 100, 3: 100}))
    p_limit = limits({}, {1: .124, 2: .186})
    for env_mask, mapping in [
        (lentic, {1: .02, 2: .03, 3: .05}),
        (intermediate, {1: .025, 2: .05, 3: .075}),
        (lotic, {1: .1, 2: .1, 3: .15}),
    ]:
        mask = fresh & env_mask
        p_limit.loc[mask] = number.map(mapping)[mask]
    base["violacao_fosforo"] = assess("fosforo_total", p_limit)
    ph = base.get("val_ph", pd.Series(np.nan, index=base.index)).where(
        base.get("qual_ph", pd.Series(None, index=base.index, dtype=object)).eq("=")
    )
    ammonia = limits({}, {1: .4, 2: .7})
    for mask, values in [(fresh & number.isin([1, 2]), [3.7, 2, 1, .5]),
                         (fresh & number.eq(3), [13.3, 5.6, 2.2, 1])]:
        ph_limit = pd.Series(np.select([ph.le(7.5), ph.le(8), ph.le(8.5)], values[:3], default=values[3]), index=base.index)
        ammonia.loc[mask] = ph_limit.where(ph.notna())[mask]
    base["violacao_amonia"] = assess("nitrogenio_amoniacal", ammonia)
    low = assess("ph", limits({1: 6, 2: 6, 3: 6, 4: 6}, {1: 6.5, 2: 6.5, 3: 5}), minimum=True)
    high = assess("ph", limits({1: 9, 2: 9, 3: 9, 4: 9}, {1: 8.5, 2: 8.5, 3: 9}))
    base["violacao_ph"] = np.where(low.eq(1) | high.eq(1), 1.0, np.where(low.eq(0) & high.eq(0), 0.0, np.nan))
    # E. coli não substitui automaticamente os limites de coliformes e sua regra temporal.
    base["violacao_ecoli"] = np.nan
    cols = ["violacao_od", "violacao_dbo", "violacao_turbidez", "violacao_fosforo", "violacao_amonia", "violacao_ph"]
    base["qtd_parametros_conama_avaliados"] = base[cols].notna().sum(axis=1)
    base["qtd_violacoes_conama"] = base[cols].sum(axis=1, min_count=1)
    base["perc_violacoes_conama"] = 100 * base["qtd_violacoes_conama"] / base["qtd_parametros_conama_avaliados"].replace(0, np.nan)
    base["status_conama"] = np.where(base["qtd_parametros_conama_avaliados"].gt(0), "Triagem parcial por amostra", "Indeterminado")


def slugify(value: str) -> str:
    text_val = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    text_val = re.sub(r"[^a-zA-Z0-9]+", "_", text_val).strip("_").lower()
    return re.sub(r"_+", "_", text_val)


def sanitize_table_name(file_path: str) -> str:
    stem = Path(file_path).stem
    clean = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    clean = re.sub(r"[^a-zA-Z0-9]+", "_", clean).strip("_")
    if clean and clean[0].isdigit():
        clean = f"tb_{clean}"
    return clean


def resolve_odbc_driver(conn_str: str) -> str:
    installed = pyodbc.drivers()
    preferred = [
        "ODBC Driver 18 for SQL Server",
        "ODBC Driver 17 for SQL Server",
        "SQL Server Native Client 11.0",
        "SQL Server",
    ]
    best_driver = next((d for d in preferred if d in installed), None)
    if not best_driver:
        return conn_str

    match = re.search(r"Driver=\{?([^};]+)\}?", conn_str, flags=re.IGNORECASE)
    if match:
        current_driver = match.group(1).strip()
        if current_driver not in installed:
            print(f"  [Aviso] Driver '{current_driver}' não instalado. Utilizando '{best_driver}'.")
            conn_str = re.sub(
                r"Driver=\{?[^};]+\}?",
                f"Driver={{{best_driver}}}",
                conn_str,
                flags=re.IGNORECASE,
            )
    else:
        conn_str = f"Driver={{{best_driver}}};{conn_str}"
    return conn_str


def build_sqlalchemy_engine(conn_str: str):
    conn_str = conn_str.strip().strip('"').strip("'")
    conn_str = re.sub(r"^AZURE_SQL_CONNECTION_STRING\s*=\s*", "", conn_str, flags=re.IGNORECASE).strip('"').strip("'")
    if not conn_str:
        raise ValueError("Connection string vazia. Defina AZURE_SQL_CONNECTION_STRING no arquivo .env ou via --conn-str.")
    if conn_str.startswith("mssql+pyodbc://") or conn_str.startswith("mssql://"):
        return create_engine(conn_str, fast_executemany=True, connect_args={"timeout": 60})
    conn_str = resolve_odbc_driver(conn_str)
    conn_str = re.sub(r"Connection\s+Timeout\s*=\s*\d+\s*;?", "", conn_str, flags=re.IGNORECASE)
    quoted = urllib.parse.quote_plus(conn_str)
    url = f"mssql+pyodbc:///?odbc_connect={quoted}"
    return create_engine(url, fast_executemany=True, connect_args={"timeout": 60})


def parse_dms_coord(dms_str: str, is_negative: bool = True) -> float:
    if pd.isna(dms_str) or not str(dms_str).strip():
        return np.nan
    parts = [float(p.replace(",", ".")) for p in re.findall(r"\d+(?:[.,]\d+)?", str(dms_str))]
    if not parts:
        return np.nan
    deg = parts[0]
    minutes = parts[1] if len(parts) > 1 else 0.0
    seconds = parts[2] if len(parts) > 2 else 0.0
    decimal = deg + (minutes / 60.0) + (seconds / 3600.0)
    return -decimal if is_negative else decimal


def parse_numeric_value(val) -> float:
    if pd.isna(val):
        return np.nan
    s = str(val).strip().replace(",", ".")
    if not s:
        return np.nan
    try:
        return float(s)
    except ValueError:
        return np.nan


def parse_hour_decimal(time_str: str) -> float:
    if pd.isna(time_str) or not str(time_str).strip():
        return np.nan
    parts = str(time_str).strip().split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        return round(h + m / 60.0, 4)
    except ValueError:
        return np.nan


def load_and_clean_raw_file(file_path: str) -> pd.DataFrame:
    df = pd.read_excel(file_path, dtype=str)
    return clean_raw_frame(df, file_path)


def clean_raw_frame(df: pd.DataFrame, file_path: str) -> pd.DataFrame:
    df = df.copy()
    df.columns = [slugify(c) for c in df.columns]
    required = {"codigo_ponto", "data_coleta", "hora_coleta", "parametro", "sinal", "valor", "unidade", "altitude", "latitude", "longitude"}
    if not required.issubset(df.columns):
        raise ValueError(f"Arquivo bruto inválido {file_path}: faltam {sorted(required - set(df.columns))}")
    for col in df.columns:
        df[col] = df[col].astype(str).str.strip().replace({"nan": None, "None": None, "": None})

    date_cols = ["periodo_de", "periodo_ate", "data_coleta", "inicio_operacao", "fim_operacao"]
    for dcol in date_cols:
        if dcol in df.columns:
            parsed = pd.to_datetime(df[dcol], format="%d/%m/%Y", errors="coerce")
            iso = pd.to_datetime(df[dcol], format="ISO8601", errors="coerce")
            df[dcol] = parsed.fillna(iso).dt.date
    if df["codigo_ponto"].isna().any() or df["data_coleta"].isna().any():
        raise ValueError(f"Ponto ou data de coleta ausente/inválido em {file_path}")

    df["abaixo_limite_deteccao"] = df["sinal"].isin(["<", "<=", "≤"]).astype(int)
    df["valor_numerico"] = df["valor"].apply(parse_numeric_value)
    df["altitude_m"] = df["altitude"].apply(parse_numeric_value)
    df["latitude_decimal"] = df["latitude"].apply(lambda x: parse_dms_coord(x, is_negative=True))
    df["longitude_decimal"] = df["longitude"].apply(lambda x: parse_dms_coord(x, is_negative=True))
    df["arquivo_origem"] = Path(file_path).name
    return df


def calc_do_saturation(temp_c: pd.Series, altitude_m: pd.Series) -> pd.Series:
    t_k = temp_c + 273.15
    ln_cs = (
        -139.34411
        + (1.575701e5 / t_k)
        - (6.642308e7 / (t_k ** 2))
        + (1.243800e10 / (t_k ** 3))
        - (8.621949e11 / (t_k ** 4))
    )
    cs_sea = np.exp(ln_cs)
    alt = altitude_m
    pressure_factor = (1.0 - (0.0001148 * alt))
    return cs_sea * pressure_factor


def classify_iet(iet_val: float) -> str:
    if pd.isna(iet_val):
        return None
    if iet_val <= 47:
        return "Ultraoligotrofico"
    if iet_val <= 52:
        return "Oligotrofico"
    if iet_val <= 59:
        return "Mesotrofico"
    if iet_val <= 63:
        return "Eutrofico"
    if iet_val <= 67:
        return "Supereutrofico"
    return "Hipereutrofico"


def estimate_iqa_subindex_series(df_wide: pd.DataFrame) -> pd.Series:
    subindices = []
    weights = []

    if "od_percentual_saturacao" in df_wide.columns:
        od_sat = df_wide["od_percentual_saturacao"].clip(lower=0, upper=140)
        q_od = 100.0 * np.exp(-((od_sat - 100.0) ** 2) / 2500.0)
        subindices.append(q_od)
        weights.append(0.17)

    if "val_escherichia_coli" in df_wide.columns:
        ecoli = df_wide["val_escherichia_coli"].clip(lower=1.0)
        q_ecoli = (98.03 - 36.45 * np.log10(ecoli) + 3.138 * (np.log10(ecoli) ** 2)).clip(lower=3.0, upper=100.0)
        subindices.append(q_ecoli)
        weights.append(0.15)

    if "val_ph" in df_wide.columns:
        ph = df_wide["val_ph"]
        q_ph = (100.0 * np.exp(-((ph - 7.4) ** 2) / 4.5)).clip(lower=2.0, upper=100.0)
        subindices.append(q_ph)
        weights.append(0.12)

    if "val_dbo_5_20" in df_wide.columns:
        dbo = df_wide["val_dbo_5_20"].clip(lower=0.0)
        q_dbo = (102.6 * np.exp(-0.1101 * dbo)).clip(lower=2.0, upper=100.0)
        subindices.append(q_dbo)
        weights.append(0.10)

    if "val_fosforo_total" in df_wide.columns:
        ptot = df_wide["val_fosforo_total"].clip(lower=0.0)
        q_p = (79.7 * ((ptot + 0.08) ** -0.45)).clip(lower=5.0, upper=100.0)
        subindices.append(q_p)
        weights.append(0.10)

    if "val_nitrogenio_total_calc" in df_wide.columns:
        ntot = df_wide["val_nitrogenio_total_calc"].clip(lower=0.0)
        q_n = (100.0 * np.exp(-0.08 * ntot)).clip(lower=2.0, upper=100.0)
        subindices.append(q_n)
        weights.append(0.10)

    if "val_turbidez" in df_wide.columns:
        turb = df_wide["val_turbidez"].clip(lower=0.0)
        q_turb = (97.34 * np.exp(-0.01139 * turb - 0.04917 * np.sqrt(turb))).clip(lower=5.0, upper=100.0)
        subindices.append(q_turb)
        weights.append(0.08)

    if "val_solido_total" in df_wide.columns:
        st = df_wide["val_solido_total"].clip(lower=0.0)
        q_st = (80.26 * np.exp(-0.00107 * st) + 15.0 * np.exp(-0.01 * st)).clip(lower=20.0, upper=100.0)
        subindices.append(q_st)
        weights.append(0.08)

    if not subindices:
        return pd.Series(np.nan, index=df_wide.index)

    q_matrix = pd.concat(subindices, axis=1)
    w_array = np.array(weights)

    def calc_row_iqa(row):
        valid_mask = row.notna().values
        if valid_mask.sum() < 5:
            return np.nan
        w_valid = w_array[valid_mask]
        w_norm = w_valid / w_valid.sum()
        q_valid = row.values[valid_mask].astype(float)
        return float(np.prod(q_valid ** w_norm))

    return q_matrix.apply(calc_row_iqa, axis=1)


def classify_iqa(iqa_val: float) -> str:
    if pd.isna(iqa_val):
        return None
    if iqa_val > 79:
        return "Otima"
    if iqa_val > 51:
        return "Boa"
    if iqa_val > 36:
        return "Regular"
    if iqa_val > 19:
        return "Ruim"
    return "Pessima"


def build_features_coletas_wide(df_all: pd.DataFrame, censor_factor=None) -> pd.DataFrame:
    df = prepare_measurements(df_all, censor_factor)
    # Pivot preserva coletas cuja hora não foi informada.
    df["hora_coleta"] = df["hora_coleta"].fillna("__SEM_HORA__")
    duplicates = df.duplicated(COLLECTION_KEY + ["param_slug"], keep=False)
    if duplicates.any():
        raise ValueError("Medições duplicadas por ponto/data/hora/parâmetro; resolver antes do pivot.")

    meta_cols = [
        "codigo_ponto",
        "data_coleta",
        "hora_coleta",
        "cod_interaguas",
        "tipo_rede",
        "ugrhi",
        "status_ponto",
        "sistema_hidrico",
        "tipo_de_sistema_hidrico",
        "classe",
        "municipio",
        "uf",
        "latitude_decimal",
        "longitude_decimal",
        "altitude_m",
        "localizacao",
        "captacao",
    ]
    key_cols = ["codigo_ponto", "data_coleta", "hora_coleta"]

    base = (
        df[meta_cols]
        .drop_duplicates(subset=key_cols)
        .sort_values(by=["codigo_ponto", "data_coleta", "hora_coleta"])
        .reset_index(drop=True)
    )

    dt_series = pd.to_datetime(base["data_coleta"])
    base["ano"] = dt_series.dt.year
    base["mes"] = dt_series.dt.month
    base["trimestre"] = dt_series.dt.quarter
    base["semestre"] = np.where(base["mes"] <= 6, 1, 2)
    base["dia_do_ano"] = dt_series.dt.dayofyear
    base["hora_decimal"] = base["hora_coleta"].apply(parse_hour_decimal)
    base["mes_sin"] = np.sin(2.0 * np.pi * base["mes"] / 12.0)
    base["mes_cos"] = np.cos(2.0 * np.pi * base["mes"] / 12.0)
    base["is_periodo_chuvoso"] = base["mes"].isin([10, 11, 12, 1, 2, 3]).astype(int)
    base["estacao_hidrologica"] = np.where(base["is_periodo_chuvoso"] == 1, "Chuvosa", "Seca")
    base["captacao_bin"] = (base["captacao"].str.upper() == "S").astype(int)

    censor_agg = (
        df.groupby(key_cols, dropna=False)
        .agg(
            qtd_parametros_medidos=("parametro", "count"),
            qtd_parametros_censurados=("abaixo_limite_deteccao", "sum"),
        )
        .reset_index()
    )
    censor_agg["perc_parametros_censurados"] = (
        censor_agg["qtd_parametros_censurados"] / censor_agg["qtd_parametros_medidos"] * 100.0
    ).round(2)
    base = base.merge(censor_agg, on=key_cols, how="left")

    num_df = df[df["valor_numerico"].notna()].copy()
    for source, prefix in [
        ("valor_ajustado", "val"), ("limite_numerico", "lim"),
        ("qualificador", "qual"), ("unidade_padronizada", "unid"),
    ]:
        data = num_df if prefix == "val" else num_df[num_df["param_slug"].isin(CORE_PARAMETERS)]
        pivot = data.pivot(index=key_cols, columns="param_slug", values=source)
        pivot.columns = [f"{prefix}_{c}" for c in pivot.columns]
        base = base.merge(pivot.reset_index(), on=key_cols, how="left", validate="one_to_one")

    cat_df = df[df["valor_numerico"].isna()].copy()
    if not cat_df.empty:
        pivot_cat = cat_df.pivot_table(
            index=key_cols,
            columns="param_slug",
            values="valor",
            aggfunc="first",
        ).reset_index()
        if "chuvas_nas_ultimas_24h" in pivot_cat.columns:
            pivot_cat["chuva_ultimas_24h_bin"] = (
                pivot_cat["chuvas_nas_ultimas_24h"].astype(str).str.lower().map({"sim": 1, "nao": 0, "não": 0})
            )
        if "ens_ecotoxic_c_ceriodaphnia_dubia" in pivot_cat.columns:
            pivot_cat["ecotox_ceriodaphnia_cronico_bin"] = np.where(
                pivot_cat["ens_ecotoxic_c_ceriodaphnia_dubia"].isna(),
                np.nan,
                np.where(
                    pivot_cat["ens_ecotoxic_c_ceriodaphnia_dubia"].astype(str).str.lower().str.contains("nao|não"),
                    0,
                    1,
                ),
            )
        if "coloracao" in pivot_cat.columns:
            pivot_cat["coloracao_texto"] = pivot_cat["coloracao"]
            pivot_cat["coloracao_alterada_bin"] = np.where(
                pivot_cat["coloracao"].isna(),
                np.nan,
                np.where(pivot_cat["coloracao"].astype(str).str.lower() == "incolor", 0, 1),
            )
        keep_cat_cols = key_cols + [
            c
            for c in [
                "chuva_ultimas_24h_bin",
                "ecotox_ceriodaphnia_cronico_bin",
                "coloracao_texto",
                "coloracao_alterada_bin",
            ]
            if c in pivot_cat.columns
        ]
        base = base.merge(pivot_cat[keep_cat_cols], on=key_cols, how="left")

    def get_col(col_name: str) -> pd.Series:
        return base[col_name] if col_name in base.columns else pd.Series(np.nan, index=base.index)

    n_amon = get_col("val_nitrogenio_amoniacal")
    n_nitra = get_col("val_nitrogenio_nitrato")
    n_nitri = get_col("val_nitrogenio_nitrito")
    n_kjel = get_col("val_nitrogenio_kjeldahl")
    p_tot = get_col("val_fosforo_total")
    p_orto = get_col("val_fosforo_ortofosfato")
    s_tot = get_col("val_solido_total")
    s_diss = get_col("val_solido_dissolvido_total")
    cond = get_col("val_condutividade")
    cod = get_col("val_carbono_organico_dissolvido")
    cot = get_col("val_carbono_organico_total")
    temp_agua = get_col("val_temperatura_da_agua")
    temp_ar = get_col("val_temperatura_do_ar")
    od = get_col("val_oxigenio_dissolvido")
    clorofila = get_col("val_clorofila_a")
    dbo = get_col("val_dbo_5_20")
    ecoli = get_col("val_escherichia_coli")
    ph = get_col("val_ph")
    turb = get_col("val_turbidez")

    base["val_nitrogenio_inorganico_dissolvido"] = n_amon + n_nitra + n_nitri
    measured_n = get_col("val_nitrogenio_total")
    summed_n = n_kjel + n_nitra + n_nitri
    base["val_nitrogenio_total_calc"] = measured_n.combine_first(summed_n)
    base["origem_nitrogenio_total"] = np.select(
        [measured_n.notna(), summed_n.notna()], ["Medido", "Soma completa"], default="Indeterminado"
    )
    base["val_nitrogenio_organico"] = (n_kjel - n_amon).where(n_kjel.ge(n_amon))
    base["razao_n_p"] = base["val_nitrogenio_total_calc"] / p_tot.replace(0, np.nan)
    base["razao_amoniacal_nitrato"] = n_amon / n_nitra.replace(0, np.nan)
    base["razao_ortofosfato_fosforo_total"] = p_orto / p_tot.replace(0, np.nan)
    base["val_solido_suspenso_estimado"] = (s_tot - s_diss).where(s_tot.ge(s_diss))
    base["razao_sdt_condutividade"] = s_diss / cond.replace(0, np.nan)
    base["razao_cod_cot"] = cod / cot.replace(0, np.nan)
    base["delta_temp_ar_agua"] = temp_ar - temp_agua

    base["od_saturacao_teorica_mg_l"] = calc_do_saturation(temp_agua, base["altitude_m"])
    base["od_percentual_saturacao"] = (od / base["od_saturacao_teorica_mg_l"]) * 100.0
    base["deficit_od_mg_l"] = base["od_saturacao_teorica_mg_l"] - od

    p_ug_l = (p_tot * 1000.0).where(p_tot.gt(0))
    cl_ug_l = clorofila.where(clorofila.gt(0))
    environment = base["tipo_de_sistema_hidrico"].fillna("").map(slugify)
    lentic = environment.str.contains("lentico")
    lotic = environment.str.contains("lotico")
    fresh = ~base["classe"].fillna("").str.lower().str.contains("salobra|salina")
    base["iet_fosforo"] = pd.Series(np.where(lentic,
        10 * (6 - (1.77 - .42 * np.log(p_ug_l)) / np.log(2)),
        10 * (6 - (.42 - .36 * np.log(p_ug_l)) / np.log(2)) - 20), index=base.index).where((lentic | lotic) & fresh)
    base["iet_clorofila"] = pd.Series(np.where(lentic,
        10 * (6 - (.92 - .34 * np.log(cl_ug_l)) / np.log(2)),
        10 * (6 - (-.7 - .6 * np.log(cl_ug_l)) / np.log(2)) - 20), index=base.index).where((lentic | lotic) & fresh)
    base["iet_medio"] = base[["iet_fosforo", "iet_clorofila"]].mean(axis=1)
    base["iet_componentes_disponiveis"] = base[["iet_fosforo", "iet_clorofila"]].notna().sum(axis=1)
    base["classificacao_iet"] = base["iet_medio"].apply(classify_iet)

    base["iqa_experimental"] = estimate_iqa_subindex_series(base)
    # Não publicar a aproximação como IQA oficial nem utilizá-la como preditora IPT.
    base["iqa_estimado"] = np.nan
    base["status_iqa"] = "Não calculado: metodologia oficial pendente de validação"
    base["classificacao_iqa"] = base["iqa_estimado"].apply(classify_iqa)

    add_conama_screening(base)

    base = base.sort_values(by=["codigo_ponto", "data_coleta", "hora_coleta"]).reset_index(drop=True)
    dt_sorted = pd.to_datetime(base["data_coleta"])
    base["dias_desde_ultima_coleta"] = dt_sorted.groupby(base["codigo_ponto"]).diff().dt.days

    core_lag_params = [
        "ph",
        "oxigenio_dissolvido",
        "dbo_5_20",
        "fosforo_total",
        "turbidez",
        "escherichia_coli",
        "condutividade",
        "temperatura_da_agua",
    ]
    for p_slug in core_lag_params:
        col = f"val_{p_slug}"
        if col in base.columns:
            grp = base.groupby("codigo_ponto")[col]
            base[f"lag1_{p_slug}"] = grp.shift(1)
            base[f"delta_{p_slug}"] = base[col] - base[f"lag1_{p_slug}"]
            base[f"mm4_{p_slug}"] = grp.transform(lambda s: s.rolling(window=4, min_periods=1).mean())

    base["hora_coleta"] = base["hora_coleta"].replace("__SEM_HORA__", None)
    base["versao_dados"] = DATA_VERSION
    return base


def build_features_resumo_parametros(df_all: pd.DataFrame, censor_factor=None) -> pd.DataFrame:
    df = prepare_measurements(df_all, censor_factor)
    df = df[df["valor_numerico"].notna()].copy()
    df["unidade"] = df["unidade_padronizada"]
    df["data_ordinal"] = pd.to_datetime(df["data_coleta"]).map(lambda d: d.toordinal() if pd.notna(d) else np.nan)

    records = []
    group_cols = ["codigo_ponto", "sistema_hidrico", "classe", "municipio", "tipo_parametro", "parametro", "unidade"]
    for keys, grp in df.groupby(group_cols, dropna=False):
        vals = grp["valor_ajustado"].dropna()
        n = len(vals)
        n_cens = int(grp["qualificador"].isin(["<", "<="]).sum())
        n_upper = int(grp["qualificador"].isin([">", ">="]).sum())
        mean_val = float(vals.mean())
        std_val = float(vals.std(ddof=1)) if n > 1 else np.nan
        cv_val = (std_val / mean_val * 100.0) if mean_val != 0 else np.nan
        q25, med, q75 = np.percentile(vals, [25, 50, 75]) if n else (np.nan, np.nan, np.nan)

        slope_anual = np.nan
        valid_time = grp.dropna(subset=["data_ordinal", "valor_ajustado"])
        if len(valid_time) >= 3 and valid_time["data_ordinal"].nunique() >= 2:
            x_years = (valid_time["data_ordinal"] - valid_time["data_ordinal"].min()) / 365.25
            y_vals = valid_time["valor_ajustado"].values
            if np.std(x_years) > 0:
                slope_anual = float(np.polyfit(x_years, y_vals, 1)[0])

        records.append(
            {
                "codigo_ponto": keys[0],
                "sistema_hidrico": keys[1],
                "classe": keys[2],
                "municipio": keys[3],
                "tipo_parametro": keys[4],
                "parametro": keys[5],
                "unidade": keys[6],
                "total_amostras": len(grp),
                "amostras_na_estatistica": n,
                "qtd_abaixo_ld": n_cens,
                "qtd_acima_limite": n_upper,
                "perc_abaixo_ld": round(n_cens / len(grp) * 100.0, 2),
                "metodo_censura": "Somente exatos" if censor_factor is None else f"Exploratório: limite inferior x {censor_factor}",
                "media": round(mean_val, 6),
                "desvio_padrao": round(std_val, 6),
                "coef_variacao_perc": round(cv_val, 2) if pd.notna(cv_val) else np.nan,
                "minimo": round(float(vals.min()), 6),
                "p25": round(float(q25), 6),
                "mediana": round(float(med), 6),
                "p75": round(float(q75), 6),
                "iqr": round(float(q75 - q25), 6),
                "maximo": round(float(vals.max()), 6),
                "tendencia_anual_slope": round(slope_anual, 6) if pd.notna(slope_anual) else np.nan,
                "primeira_coleta": grp["data_coleta"].min(),
                "ultima_coleta": grp["data_coleta"].max(),
            }
        )

    if not records:
        return pd.DataFrame()
    return pd.DataFrame(records).sort_values(by=["codigo_ponto", "tipo_parametro", "parametro"]).reset_index(drop=True)


def classify_ipt_risk(prob: float) -> str:
    if pd.isna(prob):
        return None
    if prob >= 0.65:
        return "Critico (>=65% - Nivel Municipio Critico IPT)"
    if prob >= 0.40:
        return "Alto (40-65% - Nivel Periodo Chuvoso IPT)"
    if prob >= 0.20:
        return "Moderado (20-40%)"
    return "Baixo (<20%)"


def predict_pfthm_ipt(codes, cod, dates):
    """IPT 125967-205, seção 3, tabela 1, linhas 1/2 (PDF p.13).

    COD em mg/L; PFTHM em µg/L. DOC² da linha 2 é interpretado como COD².
    Ambas as equações são calculadas em todos os pontos com COD exato.
    Fora dos pontos originais, são extrapolações experimentais não validadas.
    Seleção operacional: modelo 2 em ARPE02800, modelo 1 nos demais pontos;
    não há média, seleção por erro observado nem troca silenciosa de modelo.
    Valores negativos/não finitos são preservados no resultado bruto e rejeitados
    como estimativa utilizável. Não se calcula probabilidade a partir de R² ou S.
    """
    cod = pd.to_numeric(cod, errors="coerce")
    dates = pd.to_datetime(dates, errors="coerce")
    use_model_2 = codes.eq("ARPE02800")
    model = pd.Series(np.where(use_model_2, "IPT-T1-2-ARPE02800", "IPT-T1-1-ANOR02300"), index=codes.index)
    status = pd.Series("Indeterminado: COD exato não disponível", index=codes.index)
    available = cod.notna() & np.isfinite(cod) & cod.ge(0)
    raw_1 = (235 + 15.9 * cod + 282 * dates.dt.month.isin([1, 2, 3]).astype(int)).where(available & dates.notna())
    raw_2 = (-1983 + 669 * cod - 43.3 * cod ** 2).where(available)
    pred_1 = raw_1.where(np.isfinite(raw_1) & raw_1.ge(0))
    pred_2 = raw_2.where(np.isfinite(raw_2) & raw_2.ge(0))
    raw = raw_1.where(~use_model_2, raw_2)
    predicted = pred_1.where(~use_model_2, pred_2)
    status.loc[~use_model_2 & available & dates.isna()] = "Indeterminado: data necessária para JFM"
    status.loc[raw.notna()] = "Inválido: equação produziu PFTHM negativo ou não finito"
    original = codes.isin(["ANOR02300", "ARPE02800"])
    status.loc[predicted.notna() & original] = "Estimativa histórica experimental; domínio de calibração não confirmado"
    status.loc[predicted.notna() & ~original] = "Extrapolação experimental para outro ponto; transferência não validada"
    return pd.DataFrame({
        "modelo_pfthm_ipt": model,
        "pfthm_ipt_bruto_ug_l": raw,
        "pfthm_predito_ipt_ug_l": predicted,
        "pfthm_modelo_1_ipt_bruto_ug_l": raw_1,
        "pfthm_modelo_2_ipt_bruto_ug_l": raw_2,
        "pfthm_modelo_1_ipt_ug_l": pred_1,
        "pfthm_modelo_2_ipt_ug_l": pred_2,
        "aplicacao_modelo_pfthm": np.where(original, "Ponto original da equação selecionada", "Extrapolação para outro ponto"),
        "status_modelo_pfthm": status,
    })


def build_estudo_thm_tables(
    df_wide: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    w = df_wide.copy()

    def col_or_nan(name: str) -> pd.Series:
        values = w[name] if name in w.columns else pd.Series(np.nan, index=w.index)
        qualifier = name.replace("val_", "qual_", 1)
        if name.startswith("val_") and qualifier in w:
            values = values.where(w[qualifier].eq("="))
        return values

    pfthm_obs = col_or_nan("val_potencial_de_formacao_de_thm")
    cod_obs = col_or_nan("val_carbono_organico_dissolvido")
    cot_obs = col_or_nan("val_carbono_organico_total")
    cor_verd = col_or_nan("val_cor_verdadeira")

    cod_utilizado = cod_obs
    uv254_eq = pd.Series(np.nan, index=w.index)
    flag_alto_cot = cot_obs.gt(5).astype(float).where(cot_obs.notna())
    flag_iqa_otimo = pd.Series(np.nan, index=w.index)

    i_1_jfm = (w["trimestre"] == 1).astype(int)
    i_2_amj = (w["trimestre"] == 2).astype(int)
    i_3_jas = (w["trimestre"] == 3).astype(int)
    i_4_ond = (w["trimestre"] == 4).astype(int)
    chuva_24h = col_or_nan("chuva_ultimas_24h_bin")

    lat = col_or_nan("latitude_decimal")
    lon = col_or_nan("longitude_decimal")
    lat_x_long = lat * lon

    dbo_obs = col_or_nan("val_dbo_5_20")
    dbo_imput = dbo_obs
    od_obs = col_or_nan("val_oxigenio_dissolvido")
    od_imput = od_obs
    pt_obs = col_or_nan("val_fosforo_total")
    pt_imput = pt_obs
    ph_obs = col_or_nan("val_ph")
    temp_agua_obs = col_or_nan("val_temperatura_da_agua")

    estudo_base = pd.DataFrame(
        {
            "codigo_ponto": w["codigo_ponto"],
            "sistema_hidrico": w["sistema_hidrico"],
            "municipio": w["municipio"],
            "uf": w["uf"],
            "classe": w["classe"],
            "captacao_bin": w["captacao_bin"],
            "data_coleta": w["data_coleta"],
            "hora_coleta": w["hora_coleta"],
            "ano": w["ano"],
            "mes": w["mes"],
            "trimestre": w["trimestre"],
            "estacao_hidrologica": w["estacao_hidrologica"],
            "is_periodo_chuvoso": w["is_periodo_chuvoso"],
            "i_1_jfm": i_1_jfm,
            "i_2_amj": i_2_amj,
            "i_3_jas": i_3_jas,
            "i_4_ond": i_4_ond,
            "latitude_decimal": lat,
            "longitude_decimal": lon,
            "lat_x_long": lat_x_long,
            "altitude_m": col_or_nan("altitude_m"),
            "chuva_24h": chuva_24h,
            "coloracao_texto": col_or_nan("coloracao_texto"),
            "coloracao_alterada_bin": col_or_nan("coloracao_alterada_bin"),
            "temperatura_ar_c": col_or_nan("val_temperatura_do_ar"),
            "temperatura_agua_c": temp_agua_obs,
            "pfthm_observado_ug_l": pfthm_obs,
            "cod_observado_mg_l": cod_obs,
            "cot_observado_mg_l": cot_obs,
            "cod_utilizado_mg_l": cod_utilizado.round(4),
            "cor_verdadeira_mg_pt_l": cor_verd,
            "uv254_equivalente_cm_1": uv254_eq.round(5),
            "uv254_equivalente_quadrado": (uv254_eq ** 2).round(6),
            "flag_alto_cot_5mgl": flag_alto_cot,
            "dbo_mg_l": dbo_obs,
            "dbo_utilizada_mg_l": dbo_imput.round(4),
            "dbo_quadrado": (dbo_imput ** 2).round(4),
            "od_mg_l": od_obs,
            "od_quadrado": (od_imput ** 2).round(4),
            "od_percentual_saturacao": col_or_nan("od_percentual_saturacao").round(2),
            "fosforo_total_mg_l": pt_obs,
            "fosforo_total_quadrado": (pt_imput ** 2).round(6),
            "ph": ph_obs,
            "turbidez_unt": col_or_nan("val_turbidez"),
            "ecoli_ufc_100ml": col_or_nan("val_escherichia_coli"),
            "nitrogenio_amoniacal_mg_l": col_or_nan("val_nitrogenio_amoniacal"),
            "nitrato_mg_l": col_or_nan("val_nitrogenio_nitrato"),
            "nitrito_mg_l": col_or_nan("val_nitrogenio_nitrito"),
            "nitrogenio_kjeldahl_mg_l": col_or_nan("val_nitrogenio_kjeldahl"),
            "nitrogenio_total_mg_l": col_or_nan("val_nitrogenio_total_calc"),
            "solido_total_mg_l": col_or_nan("val_solido_total"),
            "solido_dissolvido_total_mg_l": col_or_nan("val_solido_dissolvido_total"),
            "solido_suspenso_estimado_mg_l": col_or_nan("val_solido_suspenso_estimado"),
            "clorofila_a_ug_l": col_or_nan("val_clorofila_a"),
            "iet_medio": col_or_nan("iet_medio").round(2),
            "classificacao_iet": col_or_nan("classificacao_iet"),
            "iqa_estimado": col_or_nan("iqa_estimado").round(2),
            "classificacao_iqa": col_or_nan("classificacao_iqa"),
            "flag_iqa_otimo": flag_iqa_otimo,
            "aluminio_dissolvido_mg_l": col_or_nan("val_aluminio_dissolvido"),
            "cobre_dissolvido_mg_l": col_or_nan("val_cobre_dissolvido"),
            "ferro_dissolvido_mg_l": col_or_nan("val_ferro_dissolvido"),
            "manganes_total_mg_l": col_or_nan("val_manganes_total"),
            "zinco_total_mg_l": col_or_nan("val_zinco_total"),
            "cadmio_total_mg_l": col_or_nan("val_cadmio_total"),
            "chumbo_total_mg_l": col_or_nan("val_chumbo_total"),
            "cromio_total_mg_l": col_or_nan("val_cromio_total"),
            "mercurio_total_mg_l": col_or_nan("val_mercurio_total"),
            "niquel_total_mg_l": col_or_nan("val_niquel_total"),
            "cobre_total_mg_l": col_or_nan("val_cobre_total"),
            "fenois_totais_mg_l": col_or_nan("val_fenois_totais"),
            "surfactantes_mg_l": col_or_nan("val_subst_tensoat_reagem_c_azul_metileno"),
            "ecotox_ceriodaphnia_cronico_bin": col_or_nan("ecotox_ceriodaphnia_cronico_bin"),
            "origem_nitrogenio_total": col_or_nan("origem_nitrogenio_total"),
            "iqa_experimental": col_or_nan("iqa_experimental"),
            "status_iqa": col_or_nan("status_iqa"),
            "status_conama": col_or_nan("status_conama"),
            "qtd_parametros_conama_avaliados": col_or_nan("qtd_parametros_conama_avaliados"),
            "qtd_violacoes_conama": col_or_nan("qtd_violacoes_conama"),
            "iet_componentes_disponiveis": col_or_nan("iet_componentes_disponiveis"),
            "versao_dados": DATA_VERSION,
        }
    )

    models = predict_pfthm_ipt(w["codigo_ponto"], cod_obs, w["data_coleta"])
    missing = pd.Series(np.nan, index=w.index)
    pfthm_pred_cot = missing
    pfthm_pred_cod_ipt = pfthm_pred_medio = models["pfthm_predito_ipt_ug_l"]
    pfthm_final = pfthm_obs.combine_first(pfthm_pred_medio)
    thm_eta_observado = missing
    thm_eta_predito = ((pfthm_pred_medio - 21.334) / 4.4).clip(lower=0)
    # Inversa algébrica da figura histórica, não medição nem regressão recalibrada.
    thm_eta_final = ((pfthm_final - 21.334) / 4.4).clip(lower=0)
    logit_espacial_ipt = prob_espacial_ipt = logit_fq_ipt = prob_fq_ipt = missing
    prob_regressao_pfthm_400 = missing
    risco_obs_100_ms = risco_obs_80_usepa = missing
    pred_risco_100_ms_fq_bin = pred_risco_100_ms_espacial_bin = pred_risco_80_usepa_bin = missing

    predicoes_df = pd.DataFrame(
        {
            "codigo_ponto": w["codigo_ponto"],
            "sistema_hidrico": w["sistema_hidrico"],
            "municipio": w["municipio"],
            "classe": w["classe"],
            "captacao_bin": w["captacao_bin"],
            "data_coleta": w["data_coleta"],
            "hora_coleta": w["hora_coleta"],
            "ano": w["ano"],
            "mes": w["mes"],
            "trimestre": w["trimestre"],
            "estacao_hidrologica": w["estacao_hidrologica"],
            "chuva_24h": chuva_24h,
            "cot_observado_mg_l": cot_obs,
            "cod_observado_mg_l": cod_obs,
            "cod_utilizado_mg_l": cod_utilizado.round(3),
            "uv254_equivalente_cm_1": uv254_eq.round(4),
            "flag_alto_cot_5mgl": flag_alto_cot,
            "iqa_estimado": col_or_nan("iqa_estimado").round(2),
            "classificacao_iqa": col_or_nan("classificacao_iqa"),
            "pfthm_observado_ug_l": pfthm_obs,
            "pfthm_predito_modelo_cot_ug_l": np.round(pfthm_pred_cot, 2),
            "pfthm_predito_modelo_cod_ug_l": np.round(pfthm_pred_cod_ipt, 2),
            "pfthm_predito_consenso_ug_l": np.round(pfthm_pred_medio, 2),
            "pfthm_final_ug_l": np.round(pfthm_final, 2),
            "thm_eta_observado_ug_l": np.round(thm_eta_observado, 2),
            "thm_eta_predito_ug_l": np.round(thm_eta_predito, 2),
            "thm_eta_final_ug_l": np.round(thm_eta_final, 2),
            "risco_observado_thm_100_ms": risco_obs_100_ms,
            "risco_observado_thm_80_usepa": risco_obs_80_usepa,
            "logit_modelo_espacial_ipt": np.round(logit_espacial_ipt, 4),
            "prob_risco_thm_espacial_ipt": np.round(prob_espacial_ipt, 4),
            "faixa_risco_espacial_ipt": pd.Series(prob_espacial_ipt, index=w.index).apply(classify_ipt_risk),
            "predicao_risco_thm_100_ms_espacial_bin": pred_risco_100_ms_espacial_bin,
            "logit_modelo_fisico_quimico_ipt": np.round(logit_fq_ipt, 4),
            "prob_risco_thm_fisico_quimico_ipt": np.round(prob_fq_ipt, 4),
            "faixa_risco_fq_ipt": pd.Series(prob_fq_ipt, index=w.index).apply(classify_ipt_risk),
            "prob_risco_pfthm_400_regressao": np.round(prob_regressao_pfthm_400, 4),
            "prob_risco_thm_consolidada": np.round(prob_fq_ipt, 4),
            "predicao_risco_thm_100_ms_bin": pred_risco_100_ms_fq_bin,
            "predicao_risco_thm_80_usepa_bin": pred_risco_80_usepa_bin,
            "faixa_risco_ipt": pd.Series(prob_fq_ipt, index=w.index).apply(classify_ipt_risk),
            "thm_eta_estimado_reta_historica_ug_l": thm_eta_final,
            "excede_thm_estimado_100_bin": thm_eta_final.gt(100).astype(float).where(thm_eta_final.notna()),
            "excede_thm_estimado_80_bin": thm_eta_final.gt(80).astype(float).where(thm_eta_final.notna()),
            "excede_pfthm_400_proxy_ipt_bin": pfthm_obs.gt(400).astype(float).where(pfthm_obs.notna()),
            "origem_pfthm": np.select(
                [pfthm_obs.notna(), pfthm_pred_medio.notna()],
                ["Medido", "Estimado IPT COD"], default="Indeterminado"),
            "modelo_pfthm_ipt": models["modelo_pfthm_ipt"],
            "pfthm_ipt_bruto_ug_l": models["pfthm_ipt_bruto_ug_l"],
            "pfthm_predito_ipt_ug_l": pfthm_pred_medio,
            "pfthm_modelo_1_ipt_bruto_ug_l": models["pfthm_modelo_1_ipt_bruto_ug_l"],
            "pfthm_modelo_2_ipt_bruto_ug_l": models["pfthm_modelo_2_ipt_bruto_ug_l"],
            "pfthm_modelo_1_ipt_ug_l": models["pfthm_modelo_1_ipt_ug_l"],
            "pfthm_modelo_2_ipt_ug_l": models["pfthm_modelo_2_ipt_ug_l"],
            "aplicacao_modelo_pfthm": models["aplicacao_modelo_pfthm"],
            "status_modelo_fq": "Não validado: UV254 medido e coeficientes completos necessários",
            "status_modelo_espacial": "Não validado: IQA oficial e domínio histórico necessários",
            "status_modelo_pfthm": models["status_modelo_pfthm"],
            "status_thm_eta": np.where(pfthm_obs.notna(),
                "Reta histórica a partir de PFTHM medido; THM não medido na ETA",
                np.where(pfthm_pred_medio.notna(),
                    "Duas estimativas encadeadas: IPT COD e reta histórica; THM não medido na ETA",
                    "Indeterminado: sem PFTHM medido ou estimativa utilizável")),
        }
    )

    w["flag_alto_cot_5mgl"] = flag_alto_cot
    w["pfthm_predito_consenso_ug_l"] = np.round(pfthm_pred_medio, 2)
    w["thm_eta_final_ug_l"] = np.round(thm_eta_final, 2)
    w["prob_risco_thm_espacial_ipt"] = np.round(prob_espacial_ipt, 4)
    w["prob_risco_thm_fisico_quimico_ipt"] = np.round(prob_fq_ipt, 4)
    w["prob_risco_thm_consolidada"] = np.round(prob_fq_ipt, 4)
    w["predicao_risco_thm_100_ms_bin"] = pred_risco_100_ms_fq_bin
    w["faixa_risco_ipt"] = predicoes_df["faixa_risco_ipt"]

    return w, estudo_base, predicoes_df


def sql_frame(frame):
    """Contrato de tipos usado pelo pipeline e pela publicação dos Parquets."""
    frame = frame.copy()
    types = {}
    date_columns = {"data_coleta", "primeira_coleta", "ultima_coleta",
                    "periodo_de", "periodo_ate", "inicio_operacao", "fim_operacao"}
    for col in frame.columns:
        if col in date_columns:
            frame[col] = pd.to_datetime(frame[col], errors="raise").dt.date
            types[col] = Date()
        elif pd.api.types.is_object_dtype(frame[col]) or isinstance(frame[col].dtype, pd.StringDtype):
            values = frame[col].dropna().astype(str)
            length = max(1, int(values.str.len().max())) if len(values) else 1
            types[col] = NVARCHAR(length=length if length <= 4000 else None)
            frame[col] = frame[col].astype(object).where(frame[col].notna(), None)
        elif pd.api.types.is_float_dtype(frame[col]):
            frame[col] = frame[col].replace([np.inf, -np.inf], np.nan)
    return frame, types


def normalize_map_cache(frame):
    """Mesmo esquema, tipos e ordenação para caches locais e lidos do SQL."""
    frame = frame.copy()
    frame["data_coleta"] = pd.to_datetime(frame["data_coleta"]).dt.strftime("%Y-%m-%d")
    if frame.duplicated(COLLECTION_KEY).any():
        raise ValueError("Coletas duplicadas no cache do mapa.")
    for col in frame:
        if pd.api.types.is_integer_dtype(frame[col]):
            frame[col] = frame[col].astype("int64")
        elif pd.api.types.is_float_dtype(frame[col]):
            frame[col] = frame[col].astype("float64").replace([np.inf, -np.inf], np.nan)
        else:
            frame[col] = frame[col].astype("string")
    columns = COLLECTION_KEY + sorted(set(frame.columns) - set(COLLECTION_KEY))
    return frame[columns].sort_values(COLLECTION_KEY, na_position="first").reset_index(drop=True)


def build_map_cache(study, predictions):
    from modelo_pfthm_mapa import apply_selected_model
    additional = COLLECTION_KEY + [c for c in predictions if c not in study]
    result = study.merge(predictions[additional], on=COLLECTION_KEY, validate="one_to_one")
    if len(result) != len(study) or len(result) != len(predictions):
        raise ValueError("Chaves divergentes entre base THM e predições.")
    return normalize_map_cache(apply_selected_model(result))


def upload_dataframe_to_sql(df: pd.DataFrame, table_name: str, engine, schema: str = "dbo"):
    df_to_write, types = sql_frame(df)
    df_to_write.to_sql(
        name=table_name,
        con=engine,
        schema=schema,
        if_exists="replace",
        index=False,
        dtype=types,
        chunksize=500,
    )


def discover_excel_files(input_dir: str) -> list[str]:
    pattern = os.path.join(input_dir, "*.xlsx")
    files = sorted(glob.glob(pattern))
    return [
        f
        for f in files
        if not Path(f).name.startswith(("~$", "consolidado", "cetesb_dados_brutos"))
    ]


def main():
    parser = argparse.ArgumentParser(
        description="Importa extrações da CETESB (.xlsx) para o Azure SQL Server e gera tabelas de features."
    )
    parser.add_argument(
        "--input-dir",
        default=str(Path(__file__).parent),
        help="Diretório contendo os arquivos .xlsx da CETESB (padrão: pasta do script).",
    )
    parser.add_argument(
        "--conn-str",
        default=None,
        help="Connection string do Azure SQL Server (sobrescreve AZURE_SQL_CONNECTION_STRING do .env).",
    )
    parser.add_argument(
        "--schema",
        default="dbo",
        help="Schema de destino no banco SQL Server (padrão: dbo).",
    )
    parser.add_argument(
        "--censor-factor",
        type=float,
        default=None,
        help="Opcional, exploratório: fração [0,1] do limite inferior. Padrão: estatísticas apenas de valores exatos.",
    )
    parser.add_argument(
        "--wide-table",
        default="features_coletas_wide",
        help="Nome da tabela consolidada de features por coleta no SQL Server.",
    )
    parser.add_argument(
        "--summary-table",
        default="features_resumo_parametros",
        help="Nome da tabela de resumo estatístico por ponto/parâmetro no SQL Server.",
    )
    parser.add_argument(
        "--study-table",
        default="estudo_thm_base",
        help="Nome da tabela com as variaveis selecionadas para o estudo de THM (IPT).",
    )
    parser.add_argument(
        "--predictions-table",
        default="predicoes_risco_thm",
        help="Nome da tabela com as predicoes de PFTHM, THM na ETA e probabilidades de risco (IPT).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Gera artefatos e cache locais sem gravar no banco.",
    )
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent / "resultados_v2")
    parser.add_argument("--cache-file", type=Path, default=Path(__file__).parent / "cache_mapa_sp.parquet")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--skip-raw", action="store_true", help="Atualiza somente as quatro tabelas analíticas.")
    args = parser.parse_args()

    excel_files = discover_excel_files(args.input_dir)
    if not excel_files:
        print(f"Nenhum arquivo .xlsx encontrado em: {os.path.abspath(args.input_dir)}")
        sys.exit(1)

    # Uma única orquestração: processamento por ponto, sem manter os 1,7 milhão
    # de registros brutos na memória. As regras científicas continuam neste módulo.
    from reprocessar_local import reprocess
    reprocess(Path(args.input_dir), args.output_dir, args.cache_file, args.workers, args.censor_factor)
    df_wide = pd.read_parquet(args.output_dir / "features_coletas_wide.parquet")
    df_estudo_base = pd.read_parquet(args.output_dir / "estudo_thm_base.parquet")
    df_predicoes = pd.read_parquet(args.output_dir / "predicoes_risco_thm.parquet")
    df_summary = pd.read_parquet(args.output_dir / "features_resumo_parametros.parquet")

    print(f"  - Tabela Wide '{args.schema}.{args.wide_table}': {len(df_wide)} coletas x {len(df_wide.columns)} features")
    print(f"  - Tabela Resumo '{args.schema}.{args.summary_table}': {len(df_summary)} registros x {len(df_summary.columns)} colunas")
    print(f"  - Tabela Base do Estudo THM '{args.schema}.{args.study_table}': {len(df_estudo_base)} coletas x {len(df_estudo_base.columns)} colunas")
    print(f"  - Tabela Predicoes THM '{args.schema}.{args.predictions_table}': {len(df_predicoes)} coletas x {len(df_predicoes.columns)} colunas")

    if args.dry_run:
        print("\n[DRY-RUN] Validação concluída com sucesso. Nenhuma escrita foi realizada no banco.")
        return

    load_dotenv(Path(__file__).parent / ".env", override=True)
    conn_str = args.conn_str or os.getenv("AZURE_SQL_CONNECTION_STRING", "")
    engine = build_sqlalchemy_engine(conn_str)

    print("\nConectando ao Azure SQL Server e gravando tabelas por arquivo...")
    for attempt in range(1, 5):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            break
        except Exception as exc:
            if attempt == 4:
                raise
            print(f"  [Tentativa {attempt}/4] Aguardando ativacao do banco no Azure SQL Server ({exc.__class__.__name__})...")
            time.sleep(5)

    for fpath in ([] if args.skip_raw else excel_files):
        tname = sanitize_table_name(fpath)
        df_raw = load_and_clean_raw_file(fpath)
        print(f"  -> Gravando tabela bruta '{args.schema}.{tname}' ({len(df_raw)} linhas)...")
        upload_dataframe_to_sql(df_raw, tname, engine, schema=args.schema)

    print(f"  -> Gravando tabela de features '{args.schema}.{args.wide_table}' ({len(df_wide)} linhas)...")
    upload_dataframe_to_sql(df_wide, args.wide_table, engine, schema=args.schema)

    print(f"  -> Gravando tabela de resumo '{args.schema}.{args.summary_table}' ({len(df_summary)} linhas)...")
    upload_dataframe_to_sql(df_summary, args.summary_table, engine, schema=args.schema)

    print(f"  -> Gravando tabela de base do estudo '{args.schema}.{args.study_table}' ({len(df_estudo_base)} linhas)...")
    upload_dataframe_to_sql(df_estudo_base, args.study_table, engine, schema=args.schema)

    print(f"  -> Gravando tabela de predicoes de THM '{args.schema}.{args.predictions_table}' ({len(df_predicoes)} linhas)...")
    upload_dataframe_to_sql(df_predicoes, args.predictions_table, engine, schema=args.schema)

    engine.dispose()
    print("\nPipeline concluído: tabelas CETESB atualizadas no Azure SQL Server e cache local gerado.")
    print("As tabelas ANA são mantidas por integrar_ana_etas.py --local-only --publish-db.")


if __name__ == "__main__":
    main()
