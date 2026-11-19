import json
import argparse
import math
import os
import re
import sys
import time
import shutil
from datetime import datetime
import unicodedata
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv
from sqlalchemy import text

from pipeline_cetesb import build_sqlalchemy_engine, upload_dataframe_to_sql

ANA_CAPTACOES_URL = "https://www.snirh.gov.br/arcgis/rest/services/Atlas2020/WebMap_Atlas_v1/MapServer/1"
ANA_ETAS_URL = "https://www.snirh.gov.br/arcgis/rest/services/Atlas2020/ETAs_Tratamento/MapServer/0"
LOCAL_ANA_JSON = Path(__file__).parent / "ana_etas_sp.json"


def norm_text(s: str | None) -> str:
    if not s:
        return ""
    t = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode("ascii").lower()
    t = re.sub(r"\([a-z]{2}\)", "", t)
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def haversine_km_vec(lat1: float, lon1: float, lats2: np.ndarray, lons2: np.ndarray) -> np.ndarray:
    r = 6371.0
    phi1 = np.radians(lat1)
    phi2 = np.radians(lats2)
    dphi = np.radians(lats2 - lat1)
    dlambda = np.radians(lons2 - lon1)
    a = np.sin(dphi / 2.0) ** 2 + np.cos(phi1) * np.cos(phi2) * (np.sin(dlambda / 2.0) ** 2)
    return 2.0 * r * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def fetch_arcgis_layer(base_url: str, where: str) -> list[dict]:
    features = []
    offset = 0
    while True:
        params = {
            "where": where,
            "outFields": "*",
            "returnGeometry": "true",
            "outSR": "4326",
            "f": "json",
            "resultOffset": offset,
            "resultRecordCount": 1000,
        }
        params["orderByFields"] = "OBJECTID ASC"
        resp = requests.get(f"{base_url}/query", params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or "error" in data or not isinstance(data.get("features"), list):
            raise RuntimeError("A API ANA retornou erro ou resposta sem lista de feições.")
        batch = data["features"]
        if data.get("exceededTransferLimit") and not batch:
            raise RuntimeError("Paginação ANA interrompida antes de completar os dados.")
        if batch and features and batch == features[-len(batch):]:
            raise RuntimeError("A API ANA repetiu uma página; extração interrompida.")
        features.extend(batch)
        if not data.get("exceededTransferLimit") or not batch:
            break
        offset += len(batch)
    return features


def download_ana_sp_data() -> pd.DataFrame:
    print("Baixando Captações Superficiais e ETAs de São Paulo na API SNIRH/ANA...")
    capt_feats = fetch_arcgis_layer(ANA_CAPTACOES_URL, "cd_ibge LIKE '35%'")
    eta_feats = fetch_arcgis_layer(ANA_ETAS_URL, "cod_tra >= 350000000000 AND cod_tra < 360000000000")
    if not capt_feats or not eta_feats:
        raise RuntimeError("Extração ANA vazia; os dados existentes serão preservados.")
    print(f"  - Captações Superficiais Hidroreferenciadas ANA em SP: {len(capt_feats)}")
    print(f"  - Unidades de Tratamento (ETAs) ANA em SP: {len(eta_feats)}")

    etas_by_ibge_and_name = {}
    for f in eta_feats:
        attr = f.get("attributes", {})
        geom = f.get("geometry", {})
        cod_tra = attr.get("cod_tra")
        if not cod_tra:
            continue
        ibge7 = str(int(cod_tra))[:7]
        nm_eta = norm_text(attr.get("nome_eta"))
        etas_by_ibge_and_name[(ibge7, nm_eta)] = {
            "cod_tra_eta": int(cod_tra),
            "lat_eta_planta": geom.get("y"),
            "lon_eta_planta": geom.get("x"),
            "vz_max_eta_l_s": attr.get("maximaprojeto_l_s"),
            "vz_med_eta_l_s": attr.get("mediaoperacao_l_s"),
            "pop_atendida_eta": attr.get("popucacao_atendida"),
            "municipios_atendidos_eta": attr.get("municipios_atendidos"),
            "tipo_sistema_eta": attr.get("tipo_sistema"),
        }

    records = []
    for f in capt_feats:
        attr = f.get("attributes", {})
        geom = f.get("geometry", {})
        pts = geom.get("points") or []
        if pts:
            lon, lat = float(pts[0][0]), float(pts[0][1])
        else:
            lon, lat = geom.get("x"), geom.get("y")
        if lat is None or lon is None:
            continue

        ibge = str(attr.get("cd_ibge") or "").strip()
        nm_trat = str(attr.get("nm_tratamento") or "ETA Sem Nome").strip()
        eta_match = etas_by_ibge_and_name.get((ibge, norm_text(nm_trat)), {})

        mun_raw = str(attr.get("nm_municipio") or "").strip()
        mun_clean = re.sub(r"\s*\([A-Z]{2}\)\s*$", "", mun_raw).strip()

        vz_max = attr.get("cpt_vz_maximaprojeto_l_s")
        if vz_max is None:
            vz_max = eta_match.get("vz_max_eta_l_s")
        vz_med = attr.get("cpt_vz_mediaoperacao_l_s")
        if vz_med is None:
            vz_med = eta_match.get("vz_med_eta_l_s")

        records.append(
            {
                "id_captacao_ana": str(attr.get("cpt") or attr.get("cod") or attr.get("OBJECTID")),
                "cod_ana": int(attr["cod"]) if attr.get("cod") else None,
                "cd_ibge": ibge,
                "municipio_ana": mun_clean,
                "nome_eta": nm_trat,
                "tipo_tratamento": str(attr.get("tp_tratamento") or "Não informado").strip(),
                "nome_captacao": str(attr.get("nm_captacao") or "").strip(),
                "nome_sistema": str(attr.get("sistema") or "").strip(),
                "manancial_ana": str(attr.get("nm_massa_dagua") or attr.get("nm_fantasia") or "").strip(),
                "tipo_captacao": str(attr.get("tp_captacao") or "Superficial").strip(),
                "tipo_estrutura": str(attr.get("tp_estrutura") or "").strip(),
                "status_manancial": str(attr.get("status_manancial") or "").strip(),
                "vazao_max_projeto_l_s": float(vz_max) if vz_max is not None else np.nan,
                "vazao_media_operacao_l_s": float(vz_med) if vz_med is not None else np.nan,
                "populacao_atendida": round(float(attr["populacao_atendida"]), 0)
                if attr.get("populacao_atendida") is not None
                else np.nan,
                "vulnerabilidade_quantitativa": str(attr.get("cs_quanti") or "").strip(),
                "vulnerabilidade_qualitativa": str(attr.get("cs_quali") or "").strip(),
                "avaliacao_sistema_ana": str(attr.get("gad_sistema") or "").strip(),
                "latitude_ana": round(float(lat), 6),
                "longitude_ana": round(float(lon), 6),
                "latitude_planta_eta": round(float(eta_match["lat_eta_planta"]), 6)
                if eta_match.get("lat_eta_planta") is not None
                else np.nan,
                "longitude_planta_eta": round(float(eta_match["lon_eta_planta"]), 6)
                if eta_match.get("lon_eta_planta") is not None
                else np.nan,
                "origem_coordenada_planta": "ANA ETA" if eta_match.get("lat_eta_planta") is not None else "Indeterminada",
            }
        )

    return pd.DataFrame(records)


def waterbody_similarity(sist_cetesb: str, manancial_ana: str, capt_ana: str) -> bool:
    s1 = norm_text(sist_cetesb)
    s2 = norm_text(manancial_ana) + " " + norm_text(capt_ana)
    stop = {"rio", "ribeirao", "corrego", "represa", "reservatorio", "acude", "barragem", "captacao", "eta", "do", "da", "dos", "das", "de", "sao"}
    tokens1 = {w for w in s1.split() if len(w) >= 4 and w not in stop}
    tokens2 = {w for w in s2.split() if len(w) >= 4 and w not in stop}
    return len(tokens1.intersection(tokens2)) > 0


def build_cetesb_ana_relations(df_cetesb_stations: pd.DataFrame, df_ana: pd.DataFrame) -> pd.DataFrame:
    df_ana = df_ana.dropna(subset=["latitude_ana", "longitude_ana"]).reset_index(drop=True)
    if df_ana.empty:
        raise ValueError("Não há captações ANA com coordenadas para avaliar candidatas.")
    ana_lats = df_ana["latitude_ana"].values
    ana_lons = df_ana["longitude_ana"].values
    ana_muns = [norm_text(m) for m in df_ana["municipio_ana"].values]

    rel_rows = []
    for _, st in df_cetesb_stations.iterrows():
        code = st["codigo_ponto"]
        lat_c = float(st["latitude_decimal"])
        lon_c = float(st["longitude_decimal"])
        mun_c = norm_text(st["municipio"])
        sist_c = str(st["sistema_hidrico"] or "")

        dists = haversine_km_vec(lat_c, lon_c, ana_lats, ana_lons)
        if not np.isfinite(dists).any():
            continue

        scores = dists.copy()
        scores[dists > 15.0] = np.inf
        if not np.isfinite(scores).any():
            continue
        for idx in range(len(df_ana)):
            if dists[idx] <= 25.0:
                if ana_muns[idx] == mun_c:
                    scores[idx] -= 1.5
                if waterbody_similarity(sist_c, df_ana.iloc[idx]["manancial_ana"], df_ana.iloc[idx]["nome_captacao"]):
                    scores[idx] -= 3.0

        best_idx = int(np.argmin(scores))
        best_dist = float(dists[best_idx])
        ana_row = df_ana.iloc[best_idx]
        same_waterbody = waterbody_similarity(sist_c, ana_row["manancial_ana"], ana_row["nome_captacao"])
        same_mun = ana_muns[best_idx] == mun_c

        nivel_vinculo = "Candidata por proximidade (≤ 15 km); conexão não comprovada"

        nearby_indices = np.where(dists <= 15.0)[0]
        nearby_sorted = sorted(nearby_indices, key=lambda i: dists[i])[:5]
        etas_proximas_str = "; ".join(
            f"{df_ana.iloc[i]['nome_eta']} ({df_ana.iloc[i]['municipio_ana']}, {dists[i]:.1f} km)"
            for i in nearby_sorted
        )

        rel_rows.append(
            {
                "codigo_ponto": code,
                "sistema_hidrico_cetesb": sist_c,
                "municipio_cetesb": st["municipio"],
                "classe_cetesb": st["classe"],
                "captacao_cetesb_bin": int(st.get("captacao_bin", 0)),
                "latitude_cetesb": round(lat_c, 6),
                "longitude_cetesb": round(lon_c, 6),
                "id_captacao_ana": ana_row["id_captacao_ana"],
                "nome_eta_ana": ana_row["nome_eta"],
                "tipo_tratamento_eta": ana_row["tipo_tratamento"],
                "nome_captacao_ana": ana_row["nome_captacao"],
                "nome_sistema_ana": ana_row["nome_sistema"],
                "manancial_ana": ana_row["manancial_ana"],
                "municipio_ana": ana_row["municipio_ana"],
                "vazao_max_projeto_l_s": ana_row["vazao_max_projeto_l_s"],
                "vazao_media_operacao_l_s": ana_row["vazao_media_operacao_l_s"],
                "populacao_atendida_ana": ana_row["populacao_atendida"],
                "vulnerabilidade_quantitativa_ana": ana_row["vulnerabilidade_quantitativa"],
                "avaliacao_sistema_ana": ana_row["avaliacao_sistema_ana"],
                "latitude_ana": ana_row["latitude_ana"],
                "longitude_ana": ana_row["longitude_ana"],
                "distancia_km": round(best_dist, 2),
                "mesmo_manancial_bin": None,
                "nome_manancial_semelhante_bin": int(same_waterbody),
                "mesmo_municipio_bin": int(same_mun),
                "nivel_vinculo": nivel_vinculo,
                "vinculo_confirmado": False,
                "qtd_etas_raio_15km": int(len(nearby_indices)),
                "etas_raio_15km_resumo": etas_proximas_str,
            }
        )

    return pd.DataFrame(rel_rows)


def main():
    parser = argparse.ArgumentParser(description="Atualiza candidatas ANA; publicar no banco exige --publish-db.")
    parser.add_argument("--local-only", action="store_true", help="Reutiliza o JSON ANA existente, sem requisições.")
    parser.add_argument("--publish-db", action="store_true")
    args = parser.parse_args()
    if args.local_only:
        df_ana = pd.DataFrame(json.loads(LOCAL_ANA_JSON.read_text(encoding="utf-8"))["ana_etas"])
        # No JSON legado, a captação era usada como localização da planta.
        if "origem_coordenada_planta" not in df_ana:
            fallback = df_ana.latitude_planta_eta.eq(df_ana.latitude_ana) & df_ana.longitude_planta_eta.eq(df_ana.longitude_ana)
            df_ana.loc[fallback, ["latitude_planta_eta", "longitude_planta_eta"]] = np.nan
            df_ana["origem_coordenada_planta"] = np.where(fallback, "Indeterminada", "ANA ETA (extração anterior)")
    else:
        df_ana = download_ana_sp_data()

    cache_parquet = Path(__file__).parent / "cache_mapa_sp.parquet"
    if cache_parquet.exists():
        df_full = pd.read_parquet(cache_parquet)
    else:
        if args.local_only:
            raise ValueError("Cache CETESB local ausente. Execute reprocessar_local.py primeiro.")
        load_dotenv(override=True)
        engine = build_sqlalchemy_engine(os.getenv("AZURE_SQL_CONNECTION_STRING", ""))
        with engine.connect() as conn:
            df_full = pd.read_sql(text("SELECT * FROM dbo.estudo_thm_base"), conn)

    df_stations = (
        df_full[df_full["latitude_decimal"].notna() & df_full["longitude_decimal"].notna()]
        .sort_values(["data_coleta", "hora_coleta"])
        .drop_duplicates("codigo_ponto", keep="last")[
            [
                "codigo_ponto",
                "sistema_hidrico",
                "municipio",
                "classe",
                "captacao_bin",
                "latitude_decimal",
                "longitude_decimal",
            ]
        ]
    )

    print(f"Relacionando {len(df_stations)} pontos da CETESB com as {len(df_ana)} Captações/ETAs da ANA...")
    df_rel = build_cetesb_ana_relations(df_stations, df_ana)

    print("\nResumo dos Vínculos CETESB <-> ANA (Captações / ETAs):")
    print(df_rel["nivel_vinculo"].value_counts().to_string() if not df_rel.empty else "Nenhuma candidata no raio.")

    payload = {
        "ana_etas": df_ana.replace({np.nan: None}).to_dict(orient="records"),
        "metodo_relacao": "Candidatas por proximidade; sem validação de conexão hídrica",
        "relations_by_station": {
            row["codigo_ponto"]: row
            for row in df_rel.replace({np.nan: None}).to_dict(orient="records")
        },
    }
    temporary = LOCAL_ANA_JSON.with_suffix(".tmp")
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, allow_nan=False)
    if LOCAL_ANA_JSON.exists():
        backup = LOCAL_ANA_JSON.with_name(LOCAL_ANA_JSON.name + f".backup_{datetime.now():%Y%m%d_%H%M%S_%f}")
        shutil.copy2(LOCAL_ANA_JSON, backup)
    os.replace(temporary, LOCAL_ANA_JSON)
    print(f"\nArquivo local salvo em: {LOCAL_ANA_JSON}")

    if not args.publish_db:
        return
    load_dotenv(override=True)
    conn_str = os.getenv("AZURE_SQL_CONNECTION_STRING", "")
    if conn_str:
        engine = build_sqlalchemy_engine(conn_str)
        for attempt in range(1, 5):
            try:
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                break
            except Exception as exc:
                if attempt == 4:
                    raise
                print(f"  [Tentativa {attempt}/4] Aguardando banco Azure SQL...")
                time.sleep(5)

        print("Gravando tabelas 'dbo.ana_captacoes_etas_sp' e 'dbo.relacao_cetesb_ana_etas' no Azure SQL Server...")
        upload_dataframe_to_sql(df_ana, "ana_captacoes_etas_sp", engine, schema="dbo")
        upload_dataframe_to_sql(df_rel, "relacao_cetesb_ana_etas", engine, schema="dbo")
        print("Tabelas da ANA gravadas com sucesso no Azure SQL Server!")


if __name__ == "__main__":
    main()
