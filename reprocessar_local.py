"""Reprocessamento auditável e local; não lê credenciais nem publica no Azure."""
import argparse
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

import numpy as np
import pandas as pd

from consolidar_brutos_excel import fast_read_cetesb_xlsx
from pipeline_cetesb import (
    COLLECTION_KEY, DATA_VERSION, build_estudo_thm_tables,
    build_features_coletas_wide, build_features_resumo_parametros,
    discover_excel_files, prepare_measurements,
    build_map_cache,
)


def write_parquet(frame, path):
    frame = frame.copy()
    for col in frame.select_dtypes(include="object"):
        frame[col] = frame[col].astype("string")
    frame.to_parquet(path, index=False)


def process_station(job):
    code, paths, measurements, censor_factor = job
    raw_parts, sources = [], []
    for path in paths:
        raw = fast_read_cetesb_xlsx(str(path))
        if not raw["codigo_ponto"].eq(code).all():
            raise ValueError(f"Ponto incompatível com o nome do arquivo: {path.name}")
        raw_parts.append(raw)
        sources.append({
            "arquivo": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "linhas": len(raw), "periodo_de": str(raw["periodo_de"].min()),
            "periodo_ate": str(raw["periodo_ate"].max()),
        })
    raw = pd.concat(raw_parts, ignore_index=True)
    treated = prepare_measurements(raw, censor_factor)
    units = treated[treated.valor_numerico.notna()].groupby("param_slug")["unidade_padronizada"].first().to_dict()
    write_parquet(treated, measurements / f"{code}.parquet")
    wide = build_features_coletas_wide(raw, censor_factor)
    wide, study, predictions = build_estudo_thm_tables(wide)
    summary = build_features_resumo_parametros(raw, censor_factor)
    return len(raw), sources, units, wide, study, predictions, summary


def reprocess(input_dir, output_dir, cache_file, workers=4, censor_factor=None):
    if workers < 1:
        raise ValueError("Número de processos deve ser positivo.")
    if censor_factor is not None and not 0 <= censor_factor <= 1:
        raise ValueError("O fator de censura deve estar entre 0 e 1.")
    files = [Path(p) for p in discover_excel_files(str(input_dir))]
    if not files:
        raise ValueError("Nenhum arquivo bruto encontrado.")
    # Agrupa períodos do mesmo ponto antes de calcular estatísticas e defasagens.
    groups = defaultdict(list)
    for path in files:
        groups[path.name.split(" - ")[0]].append(path)
    output_dir = Path(output_dir).resolve()
    cache_file = Path(cache_file).resolve()
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    with tempfile.TemporaryDirectory(prefix=".reprocessamento_", dir=output_dir.parent) as temporary:
        staging = Path(temporary) / "resultados"
        staging.mkdir()
        measurements = staging / "medicoes_tratadas"
        measurements.mkdir()
        wide_parts, study_parts, prediction_parts, summary_parts = [], [], [], []
        manifest = {
            "versao_dados": DATA_VERSION,
            "gerado_em_utc": datetime.now(timezone.utc).isoformat(),
            "codigo_sha256": {
                name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                for name in ["pipeline_cetesb.py", "consolidar_brutos_excel.py", "reprocessar_local.py"]
            },
            "fontes": [], "unidades_por_parametro": {},
            "politica_censura": "Valores exatos nas estimativas; limites e qualificadores preservados",
            "censor_factor": censor_factor,
            "modelos_thm": "Duas equações IPT COD em todos os pontos com COD exato; extrapolação identificada; probabilidades de risco nulas",
        }
        rows = 0
        jobs = [(code, paths, measurements, censor_factor) for code, paths in sorted(groups.items())]
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for index, result in enumerate(pool.map(process_station, jobs), 1):
                count, sources, units, wide, study, predictions, summary = result
                rows += count
                manifest["fontes"].extend(sources)
                for param, unit in units.items():
                    previous = manifest["unidades_por_parametro"].setdefault(param, unit)
                    if previous != unit:
                        raise ValueError(f"Unidades incompatíveis entre pontos: {param}: {previous} / {unit}")
                wide_parts.append(wide)
                study_parts.append(study)
                prediction_parts.append(predictions)
                if not summary.empty:
                    summary_parts.append(summary)
                if index % 50 == 0 or index == len(groups):
                    print(f"[{index}/{len(groups)}] {rows:,} medições processadas", flush=True)

        wide = pd.concat(wide_parts, ignore_index=True)
        study = pd.concat(study_parts, ignore_index=True)
        predictions = pd.concat(prediction_parts, ignore_index=True)
        summary = pd.concat(summary_parts, ignore_index=True) if summary_parts else pd.DataFrame()
        if wide.duplicated(COLLECTION_KEY).any():
            raise ValueError("Chaves de coleta duplicadas; publicação local interrompida.")
        cache = build_map_cache(study, predictions)
        for name, frame in [
            ("features_coletas_wide", wide), ("estudo_thm_base", study),
            ("predicoes_risco_thm", predictions), ("features_resumo_parametros", summary),
            ("cache_mapa_sp", cache),
        ]:
            write_parquet(frame, staging / f"{name}.parquet")
        manifest["totais"] = {
            "arquivos_brutos": len(files), "medicoes": rows,
            "coletas": len(wide), "pontos": wide.codigo_ponto.nunique(),
            "nitrogenio_total_medido": int(wide.origem_nitrogenio_total.eq("Medido").sum()),
            "nitrogenio_total_soma_completa": int(wide.origem_nitrogenio_total.eq("Soma completa").sum()),
            "nitrogenio_total_indeterminado": int(wide.origem_nitrogenio_total.eq("Indeterminado").sum()),
            "probabilidades_thm_publicadas": int(predictions.prob_risco_thm_consolidada.notna().sum()),
        }
        (staging / "manifesto.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # Só substitui artefatos após o processamento e todas as verificações.
        if output_dir.exists():
            os.replace(output_dir, output_dir.with_name(output_dir.name + f".backup_{stamp}"))
        os.replace(staging, output_dir)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        if cache_file.exists():
            shutil.copy2(cache_file, cache_file.with_name(cache_file.name + f".backup_{stamp}"))
        cache_tmp = cache_file.with_suffix(".tmp.parquet")
        shutil.copy2(output_dir / "cache_mapa_sp.parquet", cache_tmp)
        os.replace(cache_tmp, cache_file)
        print(json.dumps(manifest["totais"], ensure_ascii=False, indent=2))
        print(f"Artefatos locais: {output_dir}")
        return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=Path(__file__).parent)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent / "resultados_v2")
    parser.add_argument("--cache-file", type=Path, default=Path(__file__).parent / "cache_mapa_sp.parquet")
    parser.add_argument("--workers", type=int, default=4, help="Processos locais independentes (padrão: 4).")
    parser.add_argument("--censor-factor", type=float, default=None)
    args = parser.parse_args()
    reprocess(args.input_dir, args.output_dir, args.cache_file, args.workers, args.censor_factor)


if __name__ == "__main__":
    main()
