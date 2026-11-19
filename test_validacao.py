"""Regressões da revisão; somente dados locais/sintéticos e respostas simuladas."""
from datetime import date
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

import pipeline_cetesb as pipeline
from consolidar_brutos_excel import export_generator_to_single_excel, fast_read_cetesb_xlsx
from reprocessar_local import build_map_cache
from scraper_infoaguas import InfoaguasScraper, SEM_DADOS, split_periodo
from integrar_ana_etas import build_cetesb_ana_relations, fetch_arcgis_layer


def raw_frame(values, *, code="TEST00001", cls="Classe 2", environment="Rio (Lótico)", hour="10:00"):
    records = []
    for param, value, unit, sign in values:
        records.append({
            "codigo_ponto": code, "data_coleta": date(2020, 1, 1), "hora_coleta": hour,
            "cod_interaguas": "1", "tipo_rede": "Rede Básica", "ugrhi": "01",
            "status_ponto": "Ativo", "sistema_hidrico": "Rio Teste",
            "tipo_de_sistema_hidrico": environment, "classe": cls, "municipio": "Teste",
            "uf": "SP", "latitude_decimal": -23., "longitude_decimal": -46.,
            "altitude_m": 700., "localizacao": "Teste", "captacao": "S",
            "parametro": param, "valor": str(value), "valor_numerico": float(value),
            "sinal": sign, "abaixo_limite_deteccao": int(sign == "<"),
            "unidade": unit, "tipo_parametro": "Físico-Químico",
        })
    return pd.DataFrame(records)


class RecalibrationTests(unittest.TestCase):
    @staticmethod
    def fixture():
        rows = []
        for station in range(40):
            for year in range(2018, 2025):
                for month in [1, 4, 7, 10]:
                    cod = 2 + ((station + year + month) % 9) / 2
                    rows.append({"codigo_ponto": f"SITE{station:04}",
                                 "data_coleta": f"{year}-{month:02}-01", "hora_coleta": "10:00",
                                 "cod_observado_mg_l": cod,
                                 "pfthm_observado_ug_l": 100 + 20*cod + 30*(month == 1) + 4*station})
        return pd.DataFrame(rows)

    def test_recalibration_reserved_targets_cannot_change_fits_or_selection(self):
        from recalibrar_pfthm import calibrate, prepare, split_masks
        frame = self.fixture()
        before, first = calibrate(frame)
        masks = split_masks(prepare(frame))
        excluded = masks["teste_temporal"] | masks["teste_espacial"]
        frame.loc[excluded, "pfthm_observado_ug_l"] += 10000
        frame["pfthm_final_ug_l"] = 1e99  # jamais usar estimativas como alvo
        after, second = calibrate(frame)
        self.assertEqual(first["modelo_selecionado"], second["modelo_selecionado"])
        self.assertEqual(first["coeficientes_finais_ajustados_sem_testes"],
                         second["coeficientes_finais_ajustados_sem_testes"])
        self.assertFalse(before.loc[excluded, "usado_ajuste_final_bin"].any())
        pd.testing.assert_series_equal(before.pfthm_recalibrado_bruto_ug_l, after.pfthm_recalibrado_bruto_ug_l)

    def test_recalibration_coefficients_and_unknown_point_effect(self):
        from recalibrar_pfthm import fit_model, prepare, predict, design
        frame = self.fixture()
        cod = frame.cod_observado_mg_l
        frame["pfthm_observado_ug_l"] = 100 + 20*cod + 30*pd.to_datetime(frame.data_coleta).dt.month.eq(1)
        frame = prepare(frame)
        linear = fit_model(frame, "linear_jfm")
        np.testing.assert_allclose(list(linear["coeficientes_unidades_originais"].values()), [100, 20, 30], atol=1e-9)
        frame["pfthm_observado_ug_l"] = 200 + 10*cod + 2*cod**2
        quadratic = fit_model(frame, "quadratico")
        np.testing.assert_allclose(list(quadratic["coeficientes_unidades_originais"].values()), [200, 10, 2], atol=1e-9)
        mixed = fit_model(frame, "misto", 10)
        unknown = frame.iloc[:1].copy()
        unknown["codigo_ponto"] = "UNSEEN"
        matrix = design(unknown, mixed)
        self.assertEqual(matrix[:, 3:].sum(), 0)
        np.testing.assert_allclose(predict(unknown, mixed), matrix[:, :3] @ np.array(mixed["coeficientes_padronizados"][:3]))

    def test_recalibration_missing_cod_and_frozen_temporal_boundary(self):
        from recalibrar_pfthm import calibrate, prepare, split_masks
        frame = self.fixture()
        frame.loc[0, "cod_observado_mg_l"] = np.nan
        out, report = calibrate(frame)
        self.assertTrue(pd.isna(out.pfthm_recalibrado_ug_l.iloc[0]))
        self.assertIn("COD", out.status_predicao.iloc[0])
        for model in report["coeficientes_finais_ajustados_sem_testes"].values():
            self.assertLess(model["ultima_data_ajuste"], "2023-01-01")
            self.assertFalse(set(model["pontos_ajuste"]) & set(report["pontos_reservados_teste_espacial"]))
        masks = split_masks(prepare(frame))
        self.assertFalse((masks["ajuste_final"] & (masks["teste_temporal"] | masks["teste_espacial"])).any())


class PipelineTests(unittest.TestCase):
    def test_ipt_cod_equations_and_explicit_extrapolation(self):
        codes = pd.Series(["ANOR02300", "ANOR02300", "ARPE02800", "OTHER", "ARPE02800"])
        cod = pd.Series([5., 5., 5., 5., 1.5])
        dates = pd.Series(["2020-01-01", "2020-04-01", "2020-01-01", "2020-01-01", "2020-01-01"])
        result = pipeline.predict_pfthm_ipt(codes, cod, dates)
        np.testing.assert_allclose(result.pfthm_predito_ipt_ug_l.iloc[:3], [596.5, 314.5, 279.5])
        self.assertEqual(result.pfthm_predito_ipt_ug_l.iloc[3], 596.5)
        self.assertEqual(result.pfthm_modelo_2_ipt_ug_l.iloc[3], 279.5)
        self.assertIn("Extrapolação", result.status_modelo_pfthm.iloc[3])
        self.assertLess(result.pfthm_ipt_bruto_ug_l.iloc[4], 0)
        self.assertTrue(pd.isna(result.pfthm_predito_ipt_ug_l.iloc[4]))
        self.assertIn("Inválido", result.status_modelo_pfthm.iloc[4])

    def test_ipt_measured_precedence_and_predicted_fallback(self):
        raw = raw_frame([("Carbono Orgânico Dissolvido", 5, "mg/L", None),
                         ("Potencial de Formação de THM", 420, "µg/L", None)], code="ANOR02300")
        _, _, pred = pipeline.build_estudo_thm_tables(pipeline.build_features_coletas_wide(raw))
        self.assertEqual(pred.pfthm_predito_ipt_ug_l.iloc[0], 596.5)
        self.assertEqual(pred.pfthm_final_ug_l.iloc[0], 420)
        self.assertEqual(pred.origem_pfthm.iloc[0], "Medido")
        raw = raw.iloc[:1]
        _, _, pred = pipeline.build_estudo_thm_tables(pipeline.build_features_coletas_wide(raw))
        self.assertEqual(pred.pfthm_final_ug_l.iloc[0], 596.5)
        self.assertEqual(pred.origem_pfthm.iloc[0], "Estimado IPT COD")
        self.assertTrue(pred.thm_eta_observado_ug_l.isna().all())
        self.assertTrue(pred.prob_risco_thm_consolidada.isna().all())

    def test_ipt_requires_exact_cod_without_cot_substitution(self):
        raw = raw_frame([("Carbono Orgânico Dissolvido", 5, "mg/L", "<"),
                         ("Carbono Orgânico Total", 6, "mg/L", None)], code="ARPE02800")
        _, _, pred = pipeline.build_estudo_thm_tables(pipeline.build_features_coletas_wide(raw, .5))
        self.assertTrue(pred.pfthm_predito_ipt_ug_l.isna().all())
        self.assertTrue(pred.pfthm_final_ug_l.isna().all())
        result = pipeline.predict_pfthm_ipt(pd.Series(["ANOR02300", "ARPE02800"]),
                                           pd.Series([5., -1.]), pd.Series([None, "2020-01-01"]))
        self.assertTrue(result.pfthm_predito_ipt_ug_l.isna().all())

    def test_global_and_partitioned_computations_agree(self):
        a = raw_frame([("Fósforo Total", .05, "mg/L", None),
                       ("Nitrogênio Total", 2.4, "mg/L", None),
                       ("pH", 7.2, "U.pH", None)], code="A")
        b = raw_frame([("Oxigênio Dissolvido", 4.5, "mg/L", None),
                       ("DBO (5, 20)", 8, "mg/L", None)], code="B", cls="Classe 3")
        later = a.copy()
        later["data_coleta"] = date(2020, 2, 1)
        later.loc[later.parametro.eq("Fósforo Total"), "valor_numerico"] = .1
        a = pd.concat([a, later], ignore_index=True)
        combined = pipeline.build_estudo_thm_tables(
            pipeline.build_features_coletas_wide(pd.concat([a, b], ignore_index=True)))
        parts = [pipeline.build_estudo_thm_tables(pipeline.build_features_coletas_wide(x)) for x in [a, b]]
        for index, global_result in enumerate(combined):
            partitioned = pd.concat([part[index] for part in parts], ignore_index=True)
            # Colunas opcionais inteiramente ausentes equivalem a valores nulos.
            partitioned = partitioned.reindex(columns=global_result.columns)
            pd.testing.assert_frame_equal(
                global_result, partitioned, check_dtype=False, check_exact=False, atol=1e-12, rtol=1e-12)

    def test_sql_and_local_cache_share_all_fields_and_types(self):
        d = raw_frame([("Potencial de Formação de THM", 461.334, "µg/L", None)])
        _, study, pred = pipeline.build_estudo_thm_tables(pipeline.build_features_coletas_wide(d))
        local = pipeline.build_map_cache(study, pred)
        sql = local.copy()
        sql["data_coleta"] = pd.to_datetime(sql["data_coleta"]).dt.date
        sql["ano"] = sql["ano"].astype("int32")
        sql = sql[list(reversed(sql.columns))]
        pd.testing.assert_frame_equal(local, pipeline.normalize_map_cache(sql))
        for col in ["excede_thm_estimado_100_bin", "iqa_experimental",
                    "qtd_parametros_conama_avaliados", "thm_eta_estimado_reta_historica_ug_l"]:
            self.assertIn(col, local)
        pred = pd.concat([pred, pred], ignore_index=True)
        with self.assertRaises(pd.errors.MergeError):
            pipeline.build_map_cache(study, pred)

    def test_sql_types_preserve_unicode_dates_and_missing_values(self):
        from publicar_resultados_sql import sql_frame as publish_sql_frame
        self.assertIs(pipeline.sql_frame, publish_sql_frame)
        original = pd.DataFrame({"data_coleta": ["2020-01-01"], "descricao": ["µg/L — não"],
                                 "valor": [np.inf], "ausente": pd.Series([None], dtype="string")})
        converted, types = pipeline.sql_frame(original)
        self.assertEqual(converted.data_coleta.iloc[0], date(2020, 1, 1))
        self.assertEqual(converted.descricao.iloc[0], "µg/L — não")
        self.assertTrue(pd.isna(converted.valor.iloc[0]))
        self.assertIsNone(converted.ausente.iloc[0])
        self.assertEqual(str(types["descricao"]), "NVARCHAR(10)")

    def test_sql_null_numeric_columns_keep_numeric_cache_types(self):
        from app_mapa_sp import restore_sql_numeric_types
        from sqlalchemy import Float, NVARCHAR
        raw = raw_frame([("Potencial de Formação de THM", 300, "µg/L", None)])
        _, study, pred = pipeline.build_estudo_thm_tables(pipeline.build_features_coletas_wide(raw))
        expected = pipeline.build_map_cache(study, pred)
        driver_frame = expected.copy()
        numeric_nulls = [col for col in expected.select_dtypes(include="number") if expected[col].isna().all()]
        for col in numeric_nulls:
            driver_frame[col] = pd.Series([None], dtype=object)
        columns = [{"name": col, "type": Float()} for col in numeric_nulls]
        columns.append({"name": "classificacao_iqa", "type": NVARCHAR(1)})
        restored = pipeline.normalize_map_cache(restore_sql_numeric_types(driver_frame, columns))
        pd.testing.assert_frame_equal(expected, restored)

    def test_saved_sql_cache_matches_local_parquet(self):
        import app_mapa_sp as map_app
        from reprocessar_local import write_parquet
        raw = raw_frame([("Potencial de Formação de THM", 300, "µg/L", None)])
        _, study, pred = pipeline.build_estudo_thm_tables(pipeline.build_features_coletas_wide(raw))
        expected = pipeline.build_map_cache(study, pred)
        with tempfile.TemporaryDirectory() as folder:
            local_file = Path(folder) / "local.parquet"
            sql_file = Path(folder) / "sql.parquet"
            write_parquet(expected, local_file)
            with patch.object(map_app, "CACHE_FILE", sql_file), \
                    patch.object(map_app, "fetch_merged_from_azure_sql", return_value=expected.copy()), \
                    patch.object(map_app, "build_memory_structures"), \
                    patch.dict(map_app.DATA_STORE, {"init_payload": {"stations": []}}):
                map_app.load_data(force_db=True)
            pd.testing.assert_frame_equal(pd.read_parquet(local_file), pd.read_parquet(sql_file))

    def test_measured_total_and_complete_sum_only(self):
        d = raw_frame([("Nitrogênio Total", 2.4, "mg/L", None),
                       ("Nitrogênio-Nitrato", .36, "mg/L", None),
                       ("Nitrogênio-Nitrito", .05, "mg/L", None)])
        w = pipeline.build_features_coletas_wide(d)
        self.assertEqual(w.val_nitrogenio_total_calc.iloc[0], 2.4)
        self.assertEqual(w.origem_nitrogenio_total.iloc[0], "Medido")
        partial = pipeline.build_features_coletas_wide(d.iloc[1:])
        self.assertTrue(pd.isna(partial.val_nitrogenio_total_calc.iloc[0]))
        complete = raw_frame([("Nitrogênio Kjeldahl", 2, "mg/L", None),
                              ("Nitrogênio-Nitrato", .4, "mg/L", None),
                              ("Nitrogênio-Nitrito", .1, "mg/L", None)])
        self.assertAlmostEqual(pipeline.build_features_coletas_wide(complete).val_nitrogenio_total_calc.iloc[0], 2.5)

    def test_censored_measurement_preserves_bound_without_fabricating_exact_value(self):
        d = raw_frame([("Fósforo Total", .05, "mg/L", "<"),
                       ("DBO (5, 20)", 6, "mg/L", ">")])
        w = pipeline.build_features_coletas_wide(d)
        self.assertTrue(pd.isna(w.val_fosforo_total.iloc[0]))
        self.assertTrue(pd.isna(w.val_dbo_5_20.iloc[0]))
        self.assertEqual(w.lim_fosforo_total.iloc[0], .05)
        self.assertEqual(w.qual_dbo_5_20.iloc[0], ">")
        self.assertEqual(w.violacao_fosforo.iloc[0], 0)
        self.assertEqual(w.violacao_dbo.iloc[0], 1)
        summary = pipeline.build_features_resumo_parametros(d)
        self.assertEqual(summary.amostras_na_estatistica.sum(), 0)
        self.assertEqual(summary.total_amostras.sum(), 2)
        self.assertTrue(summary.media.isna().all())

    def test_unit_conversion_and_mutagenicity_assays(self):
        d = raw_frame([("Bentazona", 1000, "ng/L", None),
                       ("Potencial de Formação de THM", .4, "mg/L", None),
                       ("TA98 + S9", 1200, "Revertentes/L", None),
                       ("TA98 - S9", 840, "Revertentes/L", None)])
        w = pipeline.build_features_coletas_wide(d)
        self.assertAlmostEqual(w.val_bentazona.iloc[0], .001)
        self.assertEqual(w.val_potencial_de_formacao_de_thm.iloc[0], 400)
        self.assertEqual(w.val_ta98_mais_s9.iloc[0], 1200)
        self.assertEqual(w.val_ta98_menos_s9.iloc[0], 840)

    def test_conductivity_scales_are_normalized(self):
        a = raw_frame([("Condutividade", 1.2, "mS/cm", None)], code="A")
        b = raw_frame([("Condutividade", 1200, "µS/cm", None)], code="B")
        w = pipeline.build_features_coletas_wide(pd.concat([a, b], ignore_index=True))
        self.assertTrue(w.val_condutividade.eq(1200).all())

    def test_conflicting_units_and_duplicate_measurements_fail(self):
        d = raw_frame([("X", 1, "mg/L", None), ("X", 2, "UFC/100mL", None)])
        with self.assertRaisesRegex(ValueError, "Unidades incompatíveis"):
            pipeline.prepare_measurements(d)
        d["unidade"] = "mg/L"
        with self.assertRaisesRegex(ValueError, "duplicadas"):
            pipeline.build_features_coletas_wide(d)

    def test_lentic_iet_and_missing_hour(self):
        d = raw_frame([("Fósforo Total", .09, "mg/L", None),
                       ("Clorofila-a", 23.52, "µg/L", None)],
                      environment="Reservatório (Lêntico)", hour=None)
        w = pipeline.build_features_coletas_wide(d)
        self.assertAlmostEqual(w.iet_medio.iloc[0], 61.97353066594)
        self.assertEqual(w.classificacao_iet.iloc[0], "Eutrofico")
        self.assertTrue(pd.isna(w.hora_coleta.iloc[0]))
        self.assertEqual(w.val_fosforo_total.iloc[0], .09)

    def test_conama_classes_environments_and_missing_ph(self):
        values = [("Oxigênio Dissolvido", 4.5, "mg/L", None),
                  ("DBO (5, 20)", 8, "mg/L", None),
                  ("Fósforo Total", .05, "mg/L", None),
                  ("Nitrogênio Amoniacal", 1, "mg/L", None)]
        w = pipeline.build_features_coletas_wide(raw_frame(values, cls="Classe 3"))
        self.assertEqual(w.violacao_od.iloc[0], 0)
        self.assertEqual(w.violacao_dbo.iloc[0], 0)
        self.assertTrue(pd.isna(w.violacao_amonia.iloc[0]))
        w = pipeline.build_features_coletas_wide(raw_frame(values, environment="Reservatório (Lêntico)"))
        self.assertEqual(w.violacao_fosforo.iloc[0], 1)
        w = pipeline.build_features_coletas_wide(raw_frame(values, cls="Salobra 2"))
        self.assertEqual(w.violacao_od.iloc[0], 0)
        self.assertEqual(w.violacao_amonia.iloc[0], 1)
        self.assertTrue(pd.isna(w.violacao_dbo.iloc[0]))
        self.assertTrue(pd.isna(w.iet_medio.iloc[0]))
        w = pipeline.build_features_coletas_wide(raw_frame(values, cls="Classe Especial"))
        self.assertEqual(w.qtd_parametros_conama_avaliados.iloc[0], 0)
        self.assertTrue(pd.isna(w.qtd_violacoes_conama.iloc[0]))

    def test_interval_boundary_and_strict_minimum(self):
        values = pd.Series([5., 5., 5., 5., 5.])
        signs = pd.Series(["=", "<", "<=", ">", ">="])
        result = pipeline.threshold_result(values, signs, 5.)
        np.testing.assert_allclose(result, [0, 0, 0, 1, np.nan], equal_nan=True)
        result = pipeline.threshold_result(values, signs, 5., minimum=True, strict=True)
        np.testing.assert_allclose(result, [1, 1, 1, 0, np.nan], equal_nan=True)

    def test_unvalidated_models_do_not_invent_observations_or_zero_risk(self):
        d = raw_frame([("Potencial de Formação de THM", 461.334, "µg/L", None)])
        w, study, pred = pipeline.build_estudo_thm_tables(pipeline.build_features_coletas_wide(d))
        self.assertAlmostEqual(pred.thm_eta_estimado_reta_historica_ug_l.iloc[0], 100)
        for column in ["prob_risco_thm_consolidada", "predicao_risco_thm_100_ms_bin",
                       "risco_observado_thm_100_ms", "thm_eta_observado_ug_l"]:
            self.assertTrue(pred[column].isna().all(), column)
        self.assertTrue(study.chuva_24h.isna().all())
        self.assertTrue(study.iqa_estimado.isna().all())
        d["sinal"] = "<"
        w, study, pred = pipeline.build_estudo_thm_tables(pipeline.build_features_coletas_wide(d, censor_factor=.5))
        self.assertTrue(study.pfthm_observado_ug_l.isna().all())
        self.assertTrue(pred.thm_eta_final_ug_l.isna().all())

    def test_snapshot_is_one_real_row(self):
        from app_mapa_sp import build_memory_structures, DATA_STORE
        d = raw_frame([("Potencial de Formação de THM", 200, "µg/L", None)])
        _, study, pred = pipeline.build_estudo_thm_tables(pipeline.build_features_coletas_wide(d))
        first = build_map_cache(study, pred)
        second = first.copy()
        second["data_coleta"] = "2020-01-20"
        second["pfthm_observado_ug_l"] = np.nan
        second["pfthm_final_ug_l"] = np.nan
        with patch("app_mapa_sp.json.load", return_value={}):
            build_memory_structures(pd.concat([first, second], ignore_index=True))
        snapshot = DATA_STORE["init_payload"]["snapshots"]["month"]["2020-01"]["TEST00001"]
        self.assertEqual(snapshot[0], "2020-01-20")
        self.assertIsNone(snapshot[2])
        self.assertIsNone(snapshot[3])
        self.assertIsNone(snapshot[14])

    def test_actual_workbooks_regressions(self):
        root = Path(__file__).parent
        path = root / "BILL02030 - 2010-2026.xlsx"
        if not path.exists():
            self.skipTest("Amostra CETESB não disponível")
        raw = fast_read_cetesb_xlsx(str(path))
        wide = pipeline.build_features_coletas_wide(raw)
        row = wide.loc[wide.data_coleta.eq(date(2016, 1, 14))].iloc[0]
        self.assertEqual(row.val_nitrogenio_total_calc, 2.4)
        row = wide.loc[wide.data_coleta.eq(date(2010, 1, 28))].iloc[0]
        self.assertAlmostEqual(row.iet_medio, 61.97353066594)
        self.assertIn("val_ta98_mais_s9", wide)
        minimal = root / "ABAN02790 - 2010-2026.xlsx"
        if minimal.exists():
            _, study, pred = pipeline.build_estudo_thm_tables(
                pipeline.build_features_coletas_wide(fast_read_cetesb_xlsx(str(minimal)))
            )
            self.assertTrue(pred.prob_risco_thm_consolidada.isna().all())


class ExtractionTests(unittest.TestCase):
    def test_windows_and_no_data_detection(self):
        windows = split_periodo(date(2010, 1, 1), date(2026, 10, 29))
        self.assertEqual(windows[0][0], date(2010, 1, 1))
        self.assertEqual(windows[-1][1], date(2026, 10, 29))
        for (_, end), (start, _) in zip(windows, windows[1:]):
            self.assertEqual((start - end).days, 1)
        with self.assertRaises(ValueError):
            split_periodo(date(2026, 1, 1), date(2020, 1, 1))
        scraper = InfoaguasScraper.__new__(InfoaguasScraper)
        self.assertEqual(scraper._check_modal_response("Não existem informações para o período selecionado."), SEM_DADOS)
        with self.assertRaises(RuntimeError):
            scraper._check_modal_response("Erro ao recuperar informações para o período selecionado. Tente novamente.")

    def test_failed_download_preserves_previous_file(self):
        scraper = InfoaguasScraper.__new__(InfoaguasScraper)
        scraper._download_chunk_bytes = MagicMock(side_effect=RuntimeError("Servidor indisponível"))
        with tempfile.TemporaryDirectory() as folder, patch("scraper_infoaguas.MAX_RETRIES", 1):
            path = Path(folder) / "TEST00001 - 2020-2026.xlsx"
            path.write_bytes(b"previous")
            with self.assertRaises(RuntimeError):
                scraper.download_point("TEST00001", "1", [(date(2020, 1, 1), date(2026, 1, 1))], path, overwrite=True)
            self.assertEqual(path.read_bytes(), b"previous")

    def test_download_manifest_checks_period_hash_and_point(self):
        frame = pd.DataFrame([{
            "Codigo Ponto": "TEST00001", "Data Coleta": "01/01/2020", "Hora Coleta": "10:00",
            "Parametro": "pH", "Sinal": None, "Valor": "7", "Unidade": "U.pH",
            "Altitude": "700", "Latitude": "23 0 0", "Longitude": "46 0 0",
            "Periodo De": "01/01/2020", "Periodo Ate": "02/01/2020",
        }])
        stream = BytesIO()
        frame.to_excel(stream, index=False)
        scraper = InfoaguasScraper.__new__(InfoaguasScraper)
        scraper._download_chunk_bytes = MagicMock(return_value=stream.getvalue())
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / "TEST00001 - 2020-2020.xlsx"
            windows = [(date(2020, 1, 1), date(2020, 1, 2))]
            self.assertEqual(scraper.download_point("TEST00001", "1", windows, dest), "baixado")
            self.assertEqual(scraper.download_point("TEST00001", "1", windows, dest), "existente")
            self.assertEqual(scraper._download_chunk_bytes.call_count, 1)
            windows = [(date(2020, 1, 1), date(2020, 2, 1))]
            self.assertEqual(scraper.download_point("TEST00001", "1", windows, dest), "baixado")
            self.assertEqual(scraper._download_chunk_bytes.call_count, 2)
            meta = json.loads(dest.with_suffix(".download.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["fim"], "2020-02-01")

    def test_ana_error_and_pagination(self):
        response = MagicMock()
        response.json.return_value = {"error": {"message": "Falha"}}
        with patch("integrar_ana_etas.requests.get", return_value=response):
            with self.assertRaises(RuntimeError):
                fetch_arcgis_layer("https://example.invalid", "1=1")
        response.json.side_effect = [
            {"features": [{"attributes": {"OBJECTID": 1}}], "exceededTransferLimit": True},
            {"features": [{"attributes": {"OBJECTID": 2}}]},
        ]
        with patch("integrar_ana_etas.requests.get", return_value=response):
            self.assertEqual(len(fetch_arcgis_layer("https://example.invalid", "1=1")), 2)

    def test_remote_ana_is_not_linked(self):
        station = pd.DataFrame([{"codigo_ponto": "TEST", "latitude_decimal": -23.,
                                 "longitude_decimal": -46., "municipio": "Teste",
                                 "sistema_hidrico": "Rio Teste"}])
        ana = pd.DataFrame([{"latitude_ana": -25., "longitude_ana": -48., "municipio_ana": "Outro"}])
        self.assertTrue(build_cetesb_ana_relations(station, ana).empty)

    def test_export_rejects_incompatible_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            output = str(Path(folder) / "out.xlsx")
            with self.assertRaisesRegex(ValueError, "Esquema incompatível"):
                export_generator_to_single_excel(
                    [("raw", pd.DataFrame({"parametro": ["pH"]})), ("ANA", pd.DataFrame({"nome_eta": ["ETA"]}))],
                    output,
                )


if __name__ == "__main__":
    unittest.main()
