import argparse
import functools
import glob
import html
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import openpyxl
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import text

from pipeline_cetesb import (
    build_sqlalchemy_engine,
    clean_raw_frame,
    load_and_clean_raw_file,
    parse_dms_coord,
    parse_numeric_value,
    slugify,
)

EXCEL_MAX_ROWS_PER_SHEET = 1_000_000
FEATURE_TABLES = {
    "features_coletas_wide",
    "features_resumo_parametros",
    "estudo_thm_base",
    "predicoes_risco_thm",
}
NS = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def col_letters_to_idx(col_str: str) -> int:
    res = 0
    for ch in col_str:
        res = res * 26 + (ord(ch) - ord("A") + 1)
    return res - 1


@functools.lru_cache(maxsize=4096)
def cached_dms_coord(dms_str: str | None) -> float:
    return parse_dms_coord(dms_str, is_negative=True)


ROW_RE = re.compile(rb"<row\b[^>]*>(.*?)</row>", re.DOTALL)
CELL_RE = re.compile(rb'<c\s+r="([A-Z]+)\d+"([^>]*?)(?:/>|>(.*?)</c>)', re.DOTALL)
VAL_T_RE = re.compile(rb"<t[^>]*>(.*?)</t>", re.DOTALL)
VAL_V_RE = re.compile(rb"<v[^>]*>(.*?)</v>", re.DOTALL)


def fast_read_cetesb_xlsx(file_path: str) -> pd.DataFrame:
    with zipfile.ZipFile(file_path, "r") as z:
        shared_strings = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            shared_strings = [
                "".join(t.text or "" for t in si.findall(".//main:t", NS))
                for si in root.findall("main:si", NS)
            ]

        raw_xml = z.read("xl/worksheets/sheet1.xml")
        row_matches = ROW_RE.findall(raw_xml)
        if not row_matches:
            return pd.DataFrame()

        parsed_rows = []
        max_col = 26
        for r_bytes in row_matches:
            row_vals = [None] * max_col
            for col_bytes, attr_bytes, inner_bytes in CELL_RE.findall(r_bytes):
                col_str = col_bytes.decode("ascii")
                idx = col_letters_to_idx(col_str)
                if idx >= len(row_vals):
                    row_vals.extend([None] * (idx - len(row_vals) + 1))
                    max_col = len(row_vals)
                val = None
                if b't="s"' in attr_bytes:
                    vm = VAL_V_RE.search(inner_bytes)
                    if vm:
                        val = shared_strings[int(vm.group(1))]
                else:
                    tm = VAL_T_RE.findall(inner_bytes)
                    if tm:
                        val = html.unescape(b"".join(tm).decode("utf-8", errors="replace"))
                    else:
                        vm = VAL_V_RE.search(inner_bytes)
                        if vm:
                            val = html.unescape(vm.group(1).decode("utf-8", errors="replace"))
                if val is not None:
                    val = val.strip()
                row_vals[idx] = val if val != "" else None
            parsed_rows.append(row_vals)

        headers = [slugify(h) if h else f"col_{i}" for i, h in enumerate(parsed_rows[0])]
        df = pd.DataFrame(parsed_rows[1:], columns=headers)

    return clean_raw_frame(df, file_path)


def list_local_raw_files(input_dir: str, output_file: str) -> list[str]:
    out_abs = os.path.abspath(output_file)
    pattern = os.path.join(input_dir, "*.xlsx")
    files = sorted(glob.glob(pattern))
    result = []
    for f in files:
        name = Path(f).name
        if name.startswith("~$") or name.startswith("consolidado") or name.startswith("cetesb_dados_brutos"):
            continue
        if os.path.abspath(f) == out_abs:
            continue
        result.append(f)
    return result


def list_db_raw_tables(engine, schema: str = "dbo") -> list[str]:
    for attempt in range(1, 5):
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    text(
                        """
                        SELECT t.name
                        FROM sys.tables t
                        JOIN sys.schemas s ON t.schema_id = s.schema_id
                        WHERE s.name = :schema
                          AND NOT EXISTS (
                              SELECT required.name
                              FROM (VALUES ('codigo_ponto'), ('data_coleta'),
                                  ('hora_coleta'), ('parametro'), ('valor'),
                                  ('sinal'), ('unidade'), ('arquivo_origem')) required(name)
                              WHERE NOT EXISTS (
                                  SELECT 1 FROM sys.columns c
                                  WHERE c.object_id = t.object_id AND c.name = required.name
                              )
                          )
                        ORDER BY t.name
                        """
                    ),
                    {"schema": schema},
                ).fetchall()
            return [r[0] for r in rows if r[0].lower() not in FEATURE_TABLES]
        except Exception as exc:
            if attempt == 4:
                raise
            print(f"  [Tentativa {attempt}/4] Aguardando ativacao do banco no Azure SQL Server ({exc.__class__.__name__})...")
            time.sleep(5)
    return []


def export_generator_to_single_excel(
    df_iterator,
    output_path: str,
    max_rows_per_sheet: int = EXCEL_MAX_ROWS_PER_SHEET,
    also_csv: str | None = None,
):
    if not 1 <= max_rows_per_sheet <= 1_048_575:
        raise ValueError("Limite por aba deve estar entre 1 e 1.048.575 (mais cabeçalho).")
    wb = openpyxl.Workbook(write_only=True)
    sheet_idx = 1
    ws = wb.create_sheet(title=f"Dados_Brutos_{sheet_idx}")
    rows_in_current_sheet = 0
    total_rows = 0
    headers = None
    csv_first_chunk = True

    for label, df in df_iterator:
        if df.empty:
            continue

        if headers is None:
            headers = list(df.columns)
            ws.append(headers)
        else:
            if set(df.columns) != set(headers):
                for sheet in wb.worksheets:
                    sheet.close()
                wb.close()
                raise ValueError(f"Esquema incompatível em {label}; nenhuma coluna será descartada.")
            df = df.reindex(columns=headers)

        if also_csv:
            df.to_csv(
                also_csv,
                mode="w" if csv_first_chunk else "a",
                header=csv_first_chunk,
                index=False,
                encoding="utf-8-sig",
            )
            csv_first_chunk = False

        df_clean = df.where(pd.notna(df), None)
        records = df_clean.values.tolist()

        idx = 0
        n_rec = len(records)
        while idx < n_rec:
            space_left = max_rows_per_sheet - rows_in_current_sheet
            if space_left <= 0:
                sheet_idx += 1
                ws = wb.create_sheet(title=f"Dados_Brutos_{sheet_idx}")
                ws.append(headers)
                rows_in_current_sheet = 0
                space_left = max_rows_per_sheet

            batch = records[idx : idx + space_left]
            for row in batch:
                ws.append(row)

            written = len(batch)
            rows_in_current_sheet += written
            total_rows += written
            idx += written

    print(f"\nSalvando arquivo Excel consolidado em: {os.path.abspath(output_path)} ...")
    wb.save(output_path)
    print(
        f"Concluido! Total de {total_rows:,} linhas exportadas em {sheet_idx} aba(s) no arquivo '{output_path}'."
    )


def main():
    parser = argparse.ArgumentParser(
        description="Consolida todas as tabelas/arquivos de dados brutos da CETESB em um unico arquivo Excel (.xlsx)."
    )
    parser.add_argument(
        "--source",
        choices=["local", "db"],
        default="local",
        help="Origem dos dados brutos: 'local' (arquivos .xlsx da pasta) ou 'db' (tabelas brutas do Azure SQL Server). Padrao: local.",
    )
    parser.add_argument(
        "--input-dir",
        default=".",
        help="Diretorio com os arquivos .xlsx (quando --source local).",
    )
    parser.add_argument(
        "--output",
        default="cetesb_dados_brutos_consolidados.xlsx",
        help="Caminho do arquivo Excel unico de saida (padrao: cetesb_dados_brutos_consolidados.xlsx).",
    )
    parser.add_argument(
        "--output-csv",
        default=None,
        help="Opcional: caminho para salvar tambem em um arquivo .csv unico sem limite de abas.",
    )
    parser.add_argument(
        "--max-rows-sheet",
        type=int,
        default=EXCEL_MAX_ROWS_PER_SHEET,
        help="Maximo de linhas por aba no Excel (limite do Excel = 1.048.576; padrao = 1.000.000).",
    )
    parser.add_argument(
        "--schema",
        default="dbo",
        help="Schema do SQL Server (quando --source db).",
    )
    parser.add_argument(
        "--conn-str",
        default=None,
        help="Connection string do Azure SQL Server (quando --source db).",
    )
    parser.add_argument(
        "--limit-files",
        type=int,
        default=None,
        help="Opcional: limita a quantidade de arquivos processados (para teste rapido).",
    )
    args = parser.parse_args()

    t0 = time.time()
    if args.source == "local":
        files = list_local_raw_files(args.input_dir, args.output)
        if args.limit_files:
            files = files[: args.limit_files]
        if not files:
            print("Nenhum arquivo .xlsx bruto encontrado.")
            sys.exit(1)
        print(f"Lendo {len(files)} arquivos .xlsx locais e consolidando em '{args.output}'...")

        def local_gen():
            for i, fpath in enumerate(files, 1):
                try:
                    df = fast_read_cetesb_xlsx(fpath)
                except Exception:
                    df = load_and_clean_raw_file(fpath)
                if i % 50 == 0 or i == len(files):
                    print(f"  [{i}/{len(files)}] Processado: {Path(fpath).name} ({len(df)} linhas)")
                yield Path(fpath).name, df

        export_generator_to_single_excel(
            local_gen(),
            output_path=args.output,
            max_rows_per_sheet=args.max_rows_sheet,
            also_csv=args.output_csv,
        )
    else:
        load_dotenv(override=True)
        conn_str = args.conn_str or os.getenv("AZURE_SQL_CONNECTION_STRING", "")
        engine = build_sqlalchemy_engine(conn_str)
        tables = list_db_raw_tables(engine, schema=args.schema)
        if not tables:
            print("Nenhuma tabela bruta encontrada no banco de dados.")
            sys.exit(1)
        print(f"Lendo {len(tables)} tabelas brutas do Azure SQL Server e consolidando em '{args.output}'...")

        def db_gen():
            with engine.connect() as conn:
                for i, tname in enumerate(tables, 1):
                    df = pd.read_sql(text(f"SELECT * FROM [{args.schema}].[{tname}]"), conn)
                    print(f"  [{i}/{len(tables)}] Tabela lida: {args.schema}.{tname} ({len(df)} linhas)")
                    yield tname, df

        export_generator_to_single_excel(
            db_gen(),
            output_path=args.output,
            max_rows_per_sheet=args.max_rows_sheet,
            also_csv=args.output_csv,
        )

    elapsed = time.time() - t0
    print(f"Tempo total de execucao: {elapsed:.1f}s")


if __name__ == "__main__":
    main()
