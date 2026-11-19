"""Avaliação temporal das duas equações históricas, sem ajustar coeficientes."""
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent


def metrics(observed, predicted):
    error = predicted - observed
    denominator = ((observed - observed.mean()) ** 2).sum()
    return {
        "n": len(observed),
        "mae_ug_l": float(error.abs().mean()) if len(error) else None,
        "rmse_ug_l": float(np.sqrt((error ** 2).mean())) if len(error) else None,
        "vies_ug_l": float(error.mean()) if len(error) else None,
        "r2": float(1 - (error ** 2).sum() / denominator) if denominator > 0 else None,
    }


def evaluate(predictions):
    report = {
        "gerado_em_utc": datetime.now(timezone.utc).isoformat(),
        "fonte_modelos": "IPT RT125967-205, seção 3, tabela 1, linhas 1 e 2, PDF p.13",
        "interpretacao": "DOC² da linha 2 interpretado como COD²; coeficientes históricos sem recalibração.",
        "corte_temporal": "2013-01-01",
        "escopo": "Comparação posterior ao ano do relatório; mesma coleta e COD/PFTHM exatos. Separa pontos originais e transferência; não implica aprovação dos modelos.",
        "selecao_operacional": "Modelo 2 em ARPE02800; modelo 1 nos demais. PFTHM medido tem prioridade; sem média ou troca após rejeição.",
        "modelos": {},
        "equacoes": {},
    }
    for number, origin in [(1, "ANOR02300"), (2, "ARPE02800")]:
        raw_col = f"pfthm_modelo_{number}_ipt_bruto_ug_l"
        usable_col = f"pfthm_modelo_{number}_ipt_ug_l"
        pairs = predictions.loc[
            pd.to_datetime(predictions.data_coleta).ge("2013-01-01")
            & predictions.pfthm_observado_ug_l.notna()
            & predictions[raw_col].notna()]
        by_origin = {}
        for label, mask in [("ponto_original", pairs.codigo_ponto.eq(origin)),
                            ("outros_pontos", pairs.codigo_ponto.ne(origin))]:
            subset = pairs.loc[mask]
            by_origin[label] = {
                "metricas_brutas": metrics(subset.pfthm_observado_ug_l, subset[raw_col]),
                "resultados_negativos": int(subset[raw_col].lt(0).sum()),
                "pontos": int(subset.codigo_ponto.nunique()),
            }
        report["equacoes"][str(number)] = {
            "ponto_original": origin,
            "coletas_com_estimativa": int(predictions[usable_col].notna().sum()),
            "pontos_com_estimativa": int(predictions.loc[predictions[usable_col].notna(), "codigo_ponto"].nunique()),
            "resultados_negativos_rejeitados": int(predictions[raw_col].lt(0).sum()),
            "avaliacao_desde_2013": by_origin,
        }
    for code in ["ANOR02300", "ARPE02800"]:
        data = predictions.loc[predictions.codigo_ponto.eq(code)].copy()
        pairs = data.loc[
            pd.to_datetime(data.data_coleta).ge("2013-01-01")
            & data.pfthm_observado_ug_l.notna()
            & data.pfthm_ipt_bruto_ug_l.notna()
        ]
        usable = pairs.loc[pairs.pfthm_predito_ipt_ug_l.notna()]
        report["modelos"][code] = {
            "coletas": len(data),
            "estimativas_utilizaveis": int(data.pfthm_predito_ipt_ug_l.notna().sum()),
            "resultados_negativos_rejeitados": int(data.pfthm_ipt_bruto_ug_l.lt(0).sum()),
            "lacunas_preenchidas": int(data.origem_pfthm.eq("Estimado IPT COD").sum()),
            "pares_teste_com_resultado_negativo": int(pairs.pfthm_ipt_bruto_ug_l.lt(0).sum()),
            # Exibir o erro de TODAS as previsões brutas evita esconder falhas
            # pela seleção posterior de previsões fisicamente aceitáveis.
            "teste_todos_resultados_brutos": metrics(pairs.pfthm_observado_ug_l, pairs.pfthm_ipt_bruto_ug_l),
            "teste_somente_estimativas_utilizaveis": metrics(usable.pfthm_observado_ug_l, usable.pfthm_predito_ipt_ug_l),
            "status": "Experimental; avaliação descritiva, sem aprovação automática para uso regulatório",
        }
    return report


if __name__ == "__main__":
    report = evaluate(pd.read_parquet(ROOT / "resultados_v2" / "predicoes_risco_thm.parquet"))
    output = ROOT / "Doc" / "pipeline" / "avaliacao_modelos_ipt.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
