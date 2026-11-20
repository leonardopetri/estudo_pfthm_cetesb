"""Inferência congelada do modelo selecionado; não ajusta coeficientes ao abrir o mapa.

Fonte: resultados_recalibracao/pfthm_recal_20261119T221745Z/modelos_e_avaliacao.json.
Coeficientes padronizados preservam a mesma aritmética da avaliação publicada.
"""
import numpy as np
import pandas as pd

MODEL_VERSION = "pfthm_recal_20261119T221745Z"
CACHE_VERSION = "mapa-recalibrado-v1"
COD_CENTER = 4.020734497290789
COD_SCALE = 3.4721882021928048
COEFFICIENTS = np.array([332.59808115590903, 151.22328496781554, -3.3844508127423296])
COD_MIN, COD_MAX = 1., 159.
TRAINING_POINTS = frozenset("""
ACLA00500 ANOR02300 ARPE02800 ATIB02010 ATIB02030 ATIB02035 ATIB02065 ATIB02800
BACO02950 BATA02050 BITQ00100 CACH00500 CACH00892 CAMO00900 CARO02800 CASC02050
CFUG02900 COGR00900 COTI03900 CPIV02130 CRUM02080 CRUM02500 CUBA02700 CXBU02800
GERT02500 GRAN00400 GUAO02600 GUAR00900 GUAT02800 IRIS02100 JAGJ00200 JAGR02010
JAGR02200 JAGR02500 JAGR02800 JCRE00100 JCRE00200 JCRE00500 JNDI00500 JQJU00900
JUNA02125 LAGE02500 LARO02500 LENS02500 MOCA02990 MOGU02300 PADO02500 PADO02600
PARB02050 PARB02310 PARB02490 PARB02530 PARB02600 PCAB02100 PDIZ00700 PEBA00900
PEIX02100 PEXE02100 PGUI02700 PIAL02900 PIRE02900 PITA02450 PORA02700 RAIN00880
RCAB00900 RGDE02030 RGDE02900 RICO02600 RMAR02900 RPRE02200 SAFO00300 SOBU02800
SOIT02900 SOMI02850 SORO02300 SORO02700 TAIM00800 TIET02050 TITR02100 TIVA02700
TOCA02900 XOTE02500
""".split())

MODEL_INFO = {
    "versao": MODEL_VERSION,
    "nome": "Quadrático recalibrado — experimental",
    "equacao": "PFTHM = 152,945813 + 45,810177 × COD − 0,280725389 × COD²",
    "cod_min_mg_l": COD_MIN,
    "cod_max_mg_l": COD_MAX,
    "coletas_ajuste": 3322,
    "pontos_ajuste": 82,
    "teste_temporal": {"n": 1105, "mae_ug_l": 120.69285980065702, "r2": 0.28760742974490994},
    "teste_espacial": {"n": 1170, "mae_ug_l": 131.8424878469254, "r2": 0.2895447171755583},
}


def apply_selected_model(frame):
    """Aplica a todo ponto elegível, inclusive novos códigos, usando somente COD exato.

As colunas históricas são preservadas. Campos *_mapa combinam medição e
recalibração da MESMA coleta, sem recorrer às equações antigas como fallback.
"""
    result = frame.copy()
    cod = pd.to_numeric(result["cod_observado_mg_l"], errors="raise")
    valid = cod.notna() & np.isfinite(cod) & cod.ge(0)
    valid &= pd.to_datetime(result.data_coleta, errors="coerce").notna()
    raw = pd.Series(np.nan, index=result.index, dtype=float)
    z = (cod.loc[valid].to_numpy(dtype=float) - COD_CENTER) / COD_SCALE
    raw.loc[valid] = np.column_stack([np.ones(len(z)), z, z**2]) @ COEFFICIENTS
    in_range = cod.between(COD_MIN, COD_MAX)
    accepted = valid & in_range & raw.ge(0) & np.isfinite(raw)
    known = result.codigo_ponto.isin(TRAINING_POINTS)
    result["pfthm_recalibrado_bruto_ug_l"] = raw
    result["pfthm_recalibrado_ug_l"] = raw.where(accepted)
    result["ponto_no_ajuste_recalibrado_bin"] = known.astype("int64")
    result["fora_faixa_cod_recalibrado_bin"] = (~in_range).astype(float).where(valid)
    result["status_modelo_recalibrado"] = "Indeterminado: COD exato/data não disponível"
    result.loc[valid, "status_modelo_recalibrado"] = "Inválido: resultado negativo ou não finito"
    result.loc[valid & ~in_range, "status_modelo_recalibrado"] = "Fora da faixa de COD do ajuste; consultar resultado bruto"
    result.loc[accepted & known, "status_modelo_recalibrado"] = "Experimental: ponto com observações no ajuste; consultar desempenho de teste"
    result.loc[accepted & ~known, "status_modelo_recalibrado"] = "Experimental: transferência para ponto sem observações no ajuste"
    observed = pd.to_numeric(result["pfthm_observado_ug_l"], errors="raise")
    result["pfthm_mapa_ug_l"] = observed.combine_first(result.pfthm_recalibrado_ug_l)
    result["origem_pfthm_mapa"] = "Indeterminado"
    result.loc[accepted, "origem_pfthm_mapa"] = "Estimado recalibrado"
    result.loc[observed.notna(), "origem_pfthm_mapa"] = "Medido"
    result["thm_eta_mapa_estimado_ug_l"] = ((result.pfthm_mapa_ug_l - 21.334) / 4.4).clip(lower=0)
    result["versao_modelo_recalibrado"] = MODEL_VERSION
    result["versao_cache_mapa"] = CACHE_VERSION
    return result
