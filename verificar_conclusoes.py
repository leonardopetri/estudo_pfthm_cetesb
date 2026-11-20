"""Confere os números do RESUMO_ARTIGO.md a partir dos artefatos gerados localmente.

Não acessa portal, API ANA nem banco. Lê `resultados_v2/`, a execução mais
recente em `resultados_recalibracao/` e `Doc/pipeline/avaliacao_modelos_ipt.json`.
Sai com código 1 se algum valor esperado divergir.
"""
import argparse
import json
from pathlib import Path
import sys

import pandas as pd

sys.stdout.reconfigure(errors="replace")

ROOT = Path(__file__).parent
PFTHM = "pfthm_observado_ug_l"


def check(label, got, expected, tol=0.0):
    ok = abs(got - expected) <= tol if isinstance(expected, (int, float)) else got == expected
    print(f"[{'OK' if ok else 'DIVERGE'}] {label}: obtido={got} esperado={expected}")
    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resultados", type=Path, default=ROOT / "resultados_v2")
    parser.add_argument("--recalibracao", type=Path, default=None,
                        help="Diretório pfthm_recal_*; padrão: o mais recente em resultados_recalibracao/.")
    args = parser.parse_args()
    results = []

    print("== Seção 1 e 2: aquisição e processamento (manifesto.json)")
    totals = json.loads((args.resultados / "manifesto.json").read_text(encoding="utf-8"))["totais"]
    results += [
        check("arquivos brutos", totals["arquivos_brutos"], 659),
        check("pontos", totals["pontos"], 659),
        check("medições", totals["medicoes"], 1_700_130),
        check("coletas", totals["coletas"], 39_750),
        check("N total medido", totals["nitrogenio_total_medido"], 4_433),
        check("N total soma completa", totals["nitrogenio_total_soma_completa"], 13_519),
        check("N total indeterminado", totals["nitrogenio_total_indeterminado"], 21_798),
        check("probabilidades de risco THM publicadas", totals["probabilidades_thm_publicadas"], 0),
    ]

    print("\n== Seção 2.6: equações IPT (avaliacao_modelos_ipt.json)")
    ipt = json.loads((ROOT / "Doc" / "pipeline" / "avaliacao_modelos_ipt.json").read_text(encoding="utf-8"))["equacoes"]
    e1, e2 = ipt["1"]["avaliacao_desde_2013"], ipt["2"]["avaliacao_desde_2013"]
    results += [
        check("coletas com estimativa modelo 1", ipt["1"]["coletas_com_estimativa"], 13_889),
        check("pontos com estimativa modelo 1", ipt["1"]["pontos_com_estimativa"], 502),
        check("R² modelo 1 em ANOR02300", round(e1["ponto_original"]["metricas_brutas"]["r2"], 3), -0.169),
        check("n modelo 1 em ANOR02300", e1["ponto_original"]["metricas_brutas"]["n"], 16),
        check("R² modelo 2 em ARPE02800", round(e2["ponto_original"]["metricas_brutas"]["r2"], 3), -0.835),
        check("n modelo 2 em ARPE02800", e2["ponto_original"]["metricas_brutas"]["n"], 54),
        check("previsões negativas modelo 2 em ARPE02800", e2["ponto_original"]["resultados_negativos"], 9),
        check("R² modelo 1 transferido", round(e1["outros_pontos"]["metricas_brutas"]["r2"], 3), 0.039),
        check("n modelo 1 transferido", e1["outros_pontos"]["metricas_brutas"]["n"], 4_753),
        check("previsões negativas modelo 2 transferido", e2["outros_pontos"]["resultados_negativos"], 2_647),
        check("RMSE modelo 2 transferido > 21.000", e2["outros_pontos"]["metricas_brutas"]["rmse_ug_l"] > 21_000, True),
    ]

    print("\n== Seção 3: recalibração (modelos_e_avaliacao.json)")
    recal_dir = args.recalibracao or sorted((ROOT / "resultados_recalibracao").glob("pfthm_recal_*"))[-1]
    print(f"execução: {recal_dir.name}")
    rep = json.loads((recal_dir / "modelos_e_avaliacao.json").read_text(encoding="utf-8"))
    parts, coef = rep["particoes"], rep["coeficientes_finais_ajustados_sem_testes"]["modelo2"]
    results += [
        check("pares exatos COD–PFTHM", rep["pares_medidos_elegiveis"], 5_597),
        check("modelo selecionado", rep["modelo_selecionado"], "modelo2"),
        check("treino (coletas, pontos)", (parts["treino"]["coletas"], parts["treino"]["pontos"]), (2_214, 64)),
        check("validação temporal", (parts["validacao_temporal"]["coletas"], parts["validacao_temporal"]["pontos"]), (451, 61)),
        check("validação espacial", (parts["validacao_espacial"]["coletas"], parts["validacao_espacial"]["pontos"]), (657, 17)),
        check("ajuste final", (parts["ajuste_final"]["coletas"], parts["ajuste_final"]["pontos"]), (3_322, 82)),
        check("teste temporal", (parts["teste_temporal"]["coletas"], parts["teste_temporal"]["pontos"]), (1_105, 81)),
        check("teste espacial", (parts["teste_espacial"]["coletas"], parts["teste_espacial"]["pontos"]), (1_170, 24)),
        check("faixa COD do ajuste", (coef["cod_min"], coef["cod_max"]), (1.0, 159.0)),
        check("critério RMSE quadrático", round(rep["validacao"]["modelo2"]["criterio_selecao_rmse"], 2), 164.29),
        check("critério RMSE linear", round(rep["validacao"]["modelo1"]["criterio_selecao_rmse"], 2), 173.92),
        check("estimativas utilizáveis", rep["estimativas_selecionadas_aceitas"], 13_888),
        check("pontos com estimativa", rep["pontos_com_estimativa"], 502),
    ]
    for test, label, expected in [
        ("teste_temporal", "modelo2", (120.69, 188.09, 0.288)),
        ("teste_temporal", "modelo1", (135.06, 204.81, 0.155)),
        ("teste_temporal", "ipt_historico_1", (153.75, 218.85, 0.036)),
        ("teste_espacial", "modelo2", (131.84, 204.25, 0.290)),
        ("teste_espacial", "modelo1", (148.32, 229.23, 0.105)),
        ("teste_espacial", "ipt_historico_1", (161.69, 235.86, 0.053)),
    ]:
        m = rep["testes_reservados"][test][label]
        results.append(check(f"{test}/{label} (MAE, RMSE, R²)",
                             (round(m["mae_ug_l"], 2), round(m["rmse_ug_l"], 2), round(m["r2"], 3)), expected))
    results += [
        check("viés temporal quadrático", round(rep["testes_reservados"]["teste_temporal"]["modelo2"]["vies_ug_l"], 2), -22.03),
        check("viés espacial quadrático", round(rep["testes_reservados"]["teste_espacial"]["modelo2"]["vies_ug_l"], 2), -23.21),
    ]

    print("\n== Seção 4.1: distribuição do PFTHM observado (estudo_thm_base.parquet)")
    base = pd.read_parquet(args.resultados / "estudo_thm_base.parquet")
    obs = base.loc[base[PFTHM].notna()]
    p = obs[PFTHM]
    q = p.quantile([.75, .9, .95, .99]).round(0).astype(int).tolist()
    results += [
        check("medições exatas de PFTHM", len(p), 5_905),
        check("pontos com PFTHM", obs.codigo_ponto.nunique(), 120),
        check("mediana", int(p.median()), 294),
        check("P75/P90/P95/P99", tuple(q), (428, 588, 703, 1_060)),
        check("máximo", int(p.max()), 2_650),
        check("% > 100", round((p > 100).mean() * 100, 1), 95.9),
        check("% > 373,334", round((p > 373.334).mean() * 100, 1), 33.4),
        check("% > 400", round((p > 400).mean() * 100, 1), 28.8),
        check("% > 461,334", round((p > 461.334).mean() * 100, 1), 20.9),
    ]
    by_point = obs.groupby("codigo_ponto")[PFTHM].agg(n="count", mediana="median", pct400=lambda s: (s > 400).mean() * 100)
    pts20 = by_point[by_point.n >= 20]
    top = pts20.sort_values("mediana", ascending=False).head(10)
    results += [
        check("pontos com n ≥ 20", len(pts20), 98),
        check("pontos com mediana > 400", int((pts20.mediana > 400).sum()), 14),
        check("10 maiores medianas", tuple(top.index),
              ("RGDE02030", "AVEC02800", "PCAB02220", "TIET02090", "SORO02300",
               "BMIR02800", "COGR00900", "JNDI00500", "CASC02050", "GUAR00900")),
        check("faixa % > 400 nos 10 maiores",
              (int(round(top.pct400.min())), int(round(top.pct400.max()))), (58, 81)),
    ]
    yearly = obs.groupby("ano")[PFTHM].median()
    recent = yearly.loc[2018:2026]
    results += [
        check("mediana anual 2010–2014 em [215, 253]", (int(yearly.loc[2010:2014].min()), int(yearly.loc[2010:2014].max())), (215, 253)),
        check("mediana anual 2018–2026, exceto 2024, em [308, 383]",
              (int(recent.drop(2024).min()), int(recent.drop(2024).max())), (308, 383)),
        check("mediana anual 2024", int(yearly.loc[2024]), 280),
    ]

    print("\n== Seção 4.1: previsões recalibradas utilizáveis (predicoes_pfthm_recalibradas.parquet)")
    recal = pd.read_parquet(recal_dir / "predicoes_pfthm_recalibradas.parquet")
    u = recal.pfthm_recalibrado_ug_l.dropna()
    results += [
        check("tabela coletas × colunas", recal.shape, (39_750, 23)),
        check("previsões utilizáveis", len(u), 13_888),
        check("pontos com previsão", recal.loc[recal.pfthm_recalibrado_ug_l.notna(), "codigo_ponto"].nunique(), 502),
        check("% previsões > 400", round((u > 400).mean() * 100, 1), 24.7),
        check("mediana das previsões", int(u.median()), 332, tol=1),
        check("P75 das previsões", int(u.quantile(.75)), 398, tol=1),
    ]
    predictions = pd.read_parquet(args.resultados / "predicoes_risco_thm.parquet",
                                  columns=["thm_eta_observado_ug_l", "prob_risco_thm_consolidada"])
    results += [
        check("THM na ETA observado nulo em todas as linhas (seção 4.3)", bool(predictions.thm_eta_observado_ug_l.isna().all()), True),
        check("probabilidade de risco THM nula em todas as linhas (seção 2.6)", bool(predictions.prob_risco_thm_consolidada.isna().all()), True),
    ]

    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} verificações conferem.")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
