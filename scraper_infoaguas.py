"""Baixa relatórios de Qualidade de Águas Superficiais do sistema InfoÁguas (CETESB).

Fluxo:
  1. Login no navegador (Playwright) com credenciais do .env ou prompt;
  2. Carrega o catálogo de pontos de monitoramento (tabela /Monitoramento);
  3. Filtra por UGRHI (--ugrhi) ou por códigos de ponto (--pontos);
  4. Para cada ponto: POST /MonitoramentoModal + GET /Download, salvando um
     .xlsx por ponto na convenção "{CODIGO_PONTO} - {anoInicio}-{anoFim}.xlsx".

Uso:
  python scraper_infoaguas.py --list-ugrhis
  python scraper_infoaguas.py --ugrhi MANTIQUEIRA --data-inicio 01/01/2020 --data-fim 31/12/2025
  python scraper_infoaguas.py --pontos PDIZ00700,PRAT02400 --output-dir .
"""

import argparse
import getpass
import os
import json
import hashlib
import random
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

BASE_URL = "https://sistemainfoaguas.cetesb.sp.gov.br"
RELATORIO_URL = f"{BASE_URL}/AguasSuperficiais/RelatorioQualidadeAguasSuperficiais"
MONITORAMENTO_URL = f"{RELATORIO_URL}/Monitoramento"
MODAL_URL = f"{RELATORIO_URL}/MonitoramentoModal"
DOWNLOAD_URL = f"{RELATORIO_URL}/Download"
LOGIN_URL = f"{BASE_URL}/Login/Index"

REQUEST_DELAY_S = 1.5
MAX_RETRIES = 3
CATALOG_CSV = "pontos_monitoramento.csv"
# Limite do servidor: "o período selecionado não poderá ter um intervalo superior a 5 anos".
MAX_JANELA_ANOS = 5
SEM_DADOS = "__SEM_DADOS__"
REQUEST_TIMEOUT_MS = 90_000
# Falhas seguidas indicam instabilidade do servidor: pausa longa e, se persistir, aborta a execução.
FALHAS_PARA_PAUSA = 3
PAUSA_INSTABILIDADE_S = (180, 360)
FALHAS_PARA_ABORTAR = 8

# Formatos de data aceitos pelo endpoint; tentados em ordem até retornar "success".
DATE_FORMATS = ["%d/%m/%Y", "%d-%m-%Y"]

# FiltroTipo do formulario de busca -> nome do campo enviado no POST.
FILTRO_TIPO = {"ugrhi": "1", "sistema": "2", "municipio": "3"}
FILTRO_CAMPO = {"ugrhi": "NUGRHI", "sistema": "DSISTMHIDRC", "municipio": "DMUNCP"}


def normalize(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", text).strip().upper()


def parse_date(value: str) -> date:
    return datetime.strptime(value.strip(), "%d/%m/%Y").date()


def is_login_response(response) -> bool:
    return "/Login" in response.url


def split_periodo(ini: date, fim: date) -> list[tuple[date, date]]:
    """Divide o período em janelas de até MAX_JANELA_ANOS (limite do servidor)."""
    if ini > fim:
        raise ValueError("A data inicial deve ser anterior ou igual à final.")
    chunks = []
    start = ini
    while start <= fim:
        try:
            end = date(start.year + MAX_JANELA_ANOS, start.month, start.day)
        except ValueError:
            end = date(start.year + MAX_JANELA_ANOS, 2, 28)
        end = min(end, fim)
        chunks.append((start, end))
        start = end + timedelta(days=1)
    return chunks


def short_error(exc: Exception) -> str:
    text = str(exc)
    title = re.search(r"<title>(.*?)</title>", text, re.S)
    if title:
        text = f"erro do servidor: {title.group(1).strip()}"
    return text.strip().splitlines()[0][:200] if text.strip() else exc.__class__.__name__


class InfoaguasScraper:
    def __init__(self, page, email: str, senha: str, browser=None):
        self.page = page
        self.request = page.context.request
        self.browser = browser
        self.email = email
        self.senha = senha
        self.date_format = None
        self.filtro_form: dict = {}

    def reset_session(self) -> None:
        """Descarta a sessão atual (cookies) e faz login de novo do zero."""
        print("  Reiniciando sessão (novo login)...")
        if self.browser is not None:
            try:
                self.page.context.close()
            except Exception:
                pass
            self.page = self.browser.new_context().new_page()
            self.request = self.page.context.request
        self.login()
        self._init_consulta()

    # ------------------------------------------------------------ filtros

    def fetch_filter_options(self) -> dict:
        """Lê os <select> da página do relatório: NUGRHI, DSISTMHIDRC, DMUNCP."""
        resp = self.request.get(RELATORIO_URL)
        if is_login_response(resp):
            self.login()
            resp = self.request.get(RELATORIO_URL)
        soup = BeautifulSoup(resp.text(), "html.parser")
        options = {}
        for campo in FILTRO_CAMPO.values():
            sel = soup.find("select", {"name": campo})
            if sel is None:
                continue
            options[campo] = {
                o.get_text(strip=True): (o.get("value") or o.get_text(strip=True))
                for o in sel.find_all("option")
            }
        return options

    # ------------------------------------------------------------------ login

    def login(self) -> None:
        print("Acessando página de login...")
        self.page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
        self.page.fill("#Email", self.email)
        self.page.fill("#Senha", self.senha)
        self.page.click('#form0 button[type="submit"]')
        try:
            self.page.wait_for_url(lambda url: "/Login" not in url, timeout=30_000)
        except PlaywrightTimeoutError:
            erro_visivel = self.page.locator("#erro").is_visible()
            msg = "credenciais recusadas pelo site" if erro_visivel else "timeout aguardando redirecionamento"
            raise RuntimeError(f"Falha no login ({msg}). Verifique INFOAGUAS_EMAIL/INFOAGUAS_SENHA.")
        print(f"Login efetuado como {self.email}.")

    # --------------------------------------------------------------- catálogo

    def _init_consulta(self) -> None:
        form = {"TipoConsulta": "Monitoramento", "FiltroTipo": "0"}
        form.update(self.filtro_form)
        resp = self.request.post(
            RELATORIO_URL,
            form=form,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        if is_login_response(resp):
            self.login()
            resp = self.request.post(
                RELATORIO_URL,
                form=form,
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        if not resp.ok:
            raise RuntimeError(f"Falha ao iniciar consulta de monitoramento: HTTP {resp.status}")

    def fetch_catalog(self) -> pd.DataFrame:
        self._init_consulta()
        resp = self.request.get(MONITORAMENTO_URL)
        if is_login_response(resp):
            self.login()
            self._init_consulta()
            resp = self.request.get(MONITORAMENTO_URL)
        if not resp.ok:
            raise RuntimeError(f"Falha ao carregar catálogo de pontos: HTTP {resp.status}")
        return self._parse_catalog(resp.text())

    def _parse_catalog(self, html: str) -> pd.DataFrame:
        soup = BeautifulSoup(html, "html.parser")
        tbody = soup.find("tbody")
        if tbody is None:
            raise RuntimeError("Tabela de pontos de monitoramento não encontrada na resposta.")

        rows = tbody.find_all("tr")
        codigos_interaguas = []
        cells_data = []
        for tr in rows:
            hidden = tr.find("input", {"value": True})
            codigos_interaguas.append(hidden["value"].strip() if hidden else None)
            cells_data.append([td.get_text(strip=True) for td in tr.find_all("td")])

        from io import StringIO

        tables = pd.read_html(StringIO(html))
        tabela = max(tables, key=len)

        if len(codigos_interaguas) == len(tabela):
            tabela["cod_interaguas"] = codigos_interaguas
        else:
            raise RuntimeError("Catálogo com códigos ocultos desalinhados; extração interrompida.")
        if tabela["cod_interaguas"].isna().any() or tabela["cod_interaguas"].duplicated().any():
            raise RuntimeError("Catálogo com identificadores ausentes ou duplicados.")

        tabela.columns = [normalize(c) for c in tabela.columns]
        col_map = {}
        for col in tabela.columns:
            if col == "COD_INTERAGUAS":
                col_map[col] = "cod_interaguas"
            elif "PONTO" in col and "codigo_ponto" not in col_map.values():
                col_map[col] = "codigo_ponto"
            elif col == "CODIGO" and "codigo_ponto" not in col_map.values():
                col_map[col] = "codigo_ponto"
            elif "UGRHI" in col:
                col_map[col] = "ugrhi"
            elif "MUNICIPIO" in col:
                col_map[col] = "municipio"
            elif "SIST" in col:
                col_map[col] = "sistema_hidrico"
            elif "LOCALIZACAO" in col:
                col_map[col] = "localizacao"
            elif "INICIO" in col:
                col_map[col] = "data_inicio"
            elif "FIM" in col:
                col_map[col] = "data_fim"
            elif "STATUS" in col:
                col_map[col] = "status"
        tabela = tabela.rename(columns=col_map)
        print(f"  Catálogo carregado: {len(tabela)} pontos. Colunas: {list(tabela.columns)}")
        return tabela

    # --------------------------------------------------------------- download

    def _post_modal(self, cod_interaguas: str, ini: str, fim: str) -> str:
        resp = self.request.post(
            MODAL_URL,
            form={"DataInicial": ini, "DataFinal": fim, "CodigoPonto": cod_interaguas},
            timeout=REQUEST_TIMEOUT_MS,
        )
        if is_login_response(resp):
            raise RuntimeError("SESSAO_EXPIRADA")
        if not resp.ok:
            raise RuntimeError(f"MonitoramentoModal: HTTP {resp.status}")
        return resp.text()

    def _check_modal_response(self, html: str) -> str:
        texto = html.strip().strip('"').lower()
        if texto == "success":
            return "success"
        message = normalize(BeautifulSoup(html, "html.parser").get_text(" ", strip=True)).rstrip(".!")
        if message in {
            "NAO EXISTEM INFORMACOES PARA O PERIODO SELECIONADO",
            "NAO HA INFORMACOES PARA O PERIODO SELECIONADO",
            "NAO FORAM ENCONTRADAS INFORMACOES PARA O PERIODO SELECIONADO",
            "NAO EXISTEM DADOS PARA O PERIODO SELECIONADO",
        }:
            return SEM_DADOS
        soup = BeautifulSoup(html, "html.parser")
        erro = soup.find(attrs={"data-valmsg-for": True})
        msg = erro.get_text(strip=True) if erro else html[:200]
        raise RuntimeError(f"MonitoramentoModal respondeu '{msg}' (esperado 'success')")

    def _generate_report(self, cod_interaguas: str, ini_date: date, fim_date: date) -> str:
        formats = [self.date_format] if self.date_format else DATE_FORMATS
        last_exc: Exception | None = None
        for fmt in formats:
            ini = ini_date.strftime(fmt)
            fim = fim_date.strftime(fmt)
            try:
                status = self._check_modal_response(self._post_modal(cod_interaguas, ini, fim))
            except RuntimeError as exc:
                last_exc = exc
                continue
            if status == "success":
                self.date_format = fmt
            return status
        raise last_exc

    def _download_chunk_bytes(self, cod_interaguas: str, ini: date, fim: date):
        status = self._generate_report(cod_interaguas, ini, fim)
        if status == SEM_DADOS:
            return SEM_DADOS
        resp = self.request.get(DOWNLOAD_URL, timeout=120_000)
        if is_login_response(resp):
            raise RuntimeError("SESSAO_EXPIRADA")
        if not resp.ok:
            raise RuntimeError(f"Download: HTTP {resp.status}")
        body = resp.body()
        if not body[:2] == b"PK":
            raise RuntimeError("resposta do download não é um .xlsx válido")
        return body

    def download_point(self, codigo_ponto: str, cod_interaguas: str, chunks: list, dest: Path, overwrite=False) -> str:
        if not chunks:
            raise ValueError("Nenhuma janela de extração informada.")
        manifest = dest.with_suffix(".download.json")
        if dest.exists() and manifest.exists() and not overwrite:
            try:
                meta = json.loads(manifest.read_text(encoding="utf-8"))
                if (meta["codigo_ponto"] == codigo_ponto
                        and meta["cod_interaguas"] == str(cod_interaguas)
                        and meta["inicio"] == str(chunks[0][0])
                        and meta["fim"] == str(chunks[-1][1])
                        and meta["sha256"] == hashlib.sha256(dest.read_bytes()).hexdigest()):
                    return "existente"
            except (ValueError, KeyError, OSError):
                pass

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                frames = []
                windows = []
                for j, (ini, fim) in enumerate(chunks):
                    body = self._download_chunk_bytes(cod_interaguas, ini, fim)
                    windows.append({"inicio": str(ini), "fim": str(fim), "status": "sem_dados" if body == SEM_DADOS else "baixado"})
                    if body != SEM_DADOS:
                        frame = pd.read_excel(BytesIO(body), dtype=str)
                        from pipeline_cetesb import clean_raw_frame
                        checked = clean_raw_frame(frame, dest.name)
                        if not checked["codigo_ponto"].eq(codigo_ponto).all():
                            raise ValueError("O download contém um ponto diferente do solicitado.")
                        dates = pd.to_datetime(checked["data_coleta"])
                        if not dates.between(pd.Timestamp(ini), pd.Timestamp(fim)).all():
                            raise ValueError("O download contém coletas fora da janela solicitada.")
                        frames.append(frame)
                    if j < len(chunks) - 1:
                        time.sleep(REQUEST_DELAY_S)
                if not frames:
                    if dest.exists():
                        raise RuntimeError("Resposta sem dados conflitante com arquivo existente; original preservado.")
                    return "sem_dados"
                df = pd.concat(frames, ignore_index=True)
                for col in df.columns:
                    c = normalize(col)
                    if "PERIODO DE" in c:
                        df[col] = chunks[0][0].strftime("%d/%m/%Y")
                    elif "PERIODO ATE" in c:
                        df[col] = chunks[-1][1].strftime("%d/%m/%Y")
                df = df.drop_duplicates()
                temporary = dest.with_name(dest.stem + ".partial.xlsx")
                try:
                    df.to_excel(temporary, index=False)
                    digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
                    os.replace(temporary, dest)
                finally:
                    temporary.unlink(missing_ok=True)
                meta = {"codigo_ponto": codigo_ponto, "cod_interaguas": str(cod_interaguas),
                        "inicio": str(chunks[0][0]), "fim": str(chunks[-1][1]),
                        "sha256": digest, "janelas": windows, "extraido_em": datetime.now().isoformat()}
                tmp_meta = manifest.with_suffix(".tmp")
                tmp_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(tmp_meta, manifest)
                return "baixado"
            except Exception as exc:
                if str(exc) == "SESSAO_EXPIRADA":
                    print("  Sessão expirada, refazendo login...")
                    self.login()
                    continue
                if attempt == MAX_RETRIES:
                    raise RuntimeError(short_error(exc)) from None
                espera = 10 * attempt
                print(f"  Tentativa {attempt}/{MAX_RETRIES} falhou ({short_error(exc)}); aguardando {espera}s...")
                time.sleep(espera)
                try:
                    self.reset_session()
                except Exception as reset_exc:
                    print(f"  Falha ao reiniciar sessão ({short_error(reset_exc)}).")
        return "erro"


# --------------------------------------------------------------------------- CLI


def resolve_credentials() -> tuple[str, str]:
    email = os.getenv("INFOAGUAS_EMAIL", "").strip()
    senha = os.getenv("INFOAGUAS_SENHA", "")
    if not email:
        email = input("E-mail InfoÁguas: ").strip()
    if not senha:
        senha = getpass.getpass("Senha InfoÁguas: ")
    if not email or not senha:
        print("Credenciais não informadas.", file=sys.stderr)
        sys.exit(1)
    return email, senha


def filter_catalog(catalog: pd.DataFrame, pontos: list[str] | None) -> pd.DataFrame:
    if not pontos:
        return catalog
    wanted = {normalize(p) for p in pontos}
    if "codigo_ponto" not in catalog.columns:
        raise RuntimeError("Coluna de código do ponto não identificada no catálogo.")
    mask = catalog["codigo_ponto"].astype(str).map(normalize).isin(wanted)
    missing = wanted - set(catalog.loc[mask, "codigo_ponto"].astype(str).map(normalize))
    if missing:
        raise ValueError(f"Pontos solicitados não encontrados no catálogo: {sorted(missing)}")
    return catalog[mask]


def resolve_filter(options: dict, kind: str, user_value: str) -> str:
    """Resolve o texto informado pelo usuário para o value do <select> correspondente."""
    campo = FILTRO_CAMPO[kind]
    rotulos = options.get(campo, {})
    needle = normalize(user_value)
    for rotulo, value in rotulos.items():
        if normalize(rotulo) == needle:
            return value
    if needle.isdigit():
        for rotulo, value in rotulos.items():
            prefixo = re.match(r"0*(\d+)", rotulo.strip())
            if prefixo and int(prefixo.group(1)) == int(needle):
                return value
    matches = [(r, v) for r, v in rotulos.items() if needle in normalize(r)]
    if len(matches) == 1:
        return matches[0][1]
    if len(matches) > 1:
        lista = ", ".join(r for r, _ in matches[:10])
        raise RuntimeError(f"Filtro '{user_value}' ambíguo ({len(matches)} opções): {lista}...")
    raise RuntimeError(f"Filtro '{user_value}' não encontrado em {campo}. Use --list-ugrhis para ver as opções.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Baixa relatórios .xlsx de Qualidade de Águas Superficiais do InfoÁguas (CETESB)."
    )
    parser.add_argument("--ugrhi", default=None, help="Filtra pontos por UGRHI (ex.: '01' ou 'MANTIQUEIRA').")
    parser.add_argument("--municipio", default=None, help="Filtra pontos por município (ex.: 'CAMPOS DO JORDAO').")
    parser.add_argument("--sistema", default=None, help="Filtra pontos por sistema hídrico (ex.: 'Rio Pardo').")
    parser.add_argument(
        "--pontos",
        default=None,
        help="Códigos de ponto separados por vírgula (ex.: PDIZ00700,PRAT02400). Sobrepõe os filtros.",
    )
    parser.add_argument("--data-inicio", default="01/01/2020", help="Data inicial dd/mm/aaaa (padrão: 01/01/2020).")
    parser.add_argument("--data-fim", default=None, help="Data final dd/mm/aaaa (padrão: hoje).")
    parser.add_argument("--output-dir", default=".", help="Diretório de saída dos .xlsx (padrão: atual).")
    parser.add_argument("--list-ugrhis", action="store_true", help="Lista as UGRHIs do catálogo e sai.")
    parser.add_argument("--overwrite", action="store_true", help="Rebaixa arquivos já existentes.")
    parser.add_argument("--headful", action="store_true", help="Abre o navegador visível (depuração).")
    args = parser.parse_args()

    load_dotenv(override=True)
    ini = parse_date(args.data_inicio)
    fim = parse_date(args.data_fim) if args.data_fim else date.today()
    chunks = split_periodo(ini, fim)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    email, senha = resolve_credentials()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headful)
        context = browser.new_context()
        page = context.new_page()
        scraper = InfoaguasScraper(page, email, senha, browser=browser)
        scraper.login()

        options = scraper.fetch_filter_options()

        if args.list_ugrhis:
            print("\nUGRHIs disponíveis:")
            for rotulo in sorted(options.get("NUGRHI", {})):
                print(f"  {rotulo}")
            browser.close()
            return

        filtros = [k for k in ("ugrhi", "municipio", "sistema") if getattr(args, k)]
        if args.pontos and filtros:
            print("  [Aviso] --pontos informado; filtros de UGRHI/município/sistema ignorados.")
            filtros = []
        if len(filtros) > 1:
            print("Informe apenas um filtro por execução (--ugrhi, --municipio ou --sistema).", file=sys.stderr)
            browser.close()
            sys.exit(1)
        if filtros:
            kind = filtros[0]
            valor = resolve_filter(options, kind, getattr(args, kind))
            scraper.filtro_form = {"FiltroTipo": FILTRO_TIPO[kind], FILTRO_CAMPO[kind]: valor}
            print(f"Filtro {kind}: '{getattr(args, kind)}' -> {FILTRO_CAMPO[kind]}={valor}")

        catalog = scraper.fetch_catalog()
        catalog.to_csv(output_dir / CATALOG_CSV, index=False, encoding="utf-8-sig")
        print(f"Catálogo salvo em {output_dir / CATALOG_CSV}")

        pontos_arg = [p.strip() for p in args.pontos.split(",")] if args.pontos else None
        alvo = filter_catalog(catalog, pontos_arg)
        if alvo.empty:
            print("Nenhum ponto de monitoramento corresponde ao filtro informado.")
            browser.close()
            return
        if len(chunks) > 1:
            print(f"Período > {MAX_JANELA_ANOS} anos: será baixado em {len(chunks)} janelas e mesclado por ponto.")
        print(f"\n{len(alvo)} ponto(s) selecionado(s) para download ({ini:%d/%m/%Y} a {fim:%d/%m/%Y}).\n")

        ok = skip = nodata = fail = seguidas = 0
        abortado = False
        for i, row in enumerate(alvo.itertuples(index=False), start=1):
            if seguidas >= FALHAS_PARA_ABORTAR:
                print(f"\n{seguidas} falhas seguidas: servidor instável. Abortando; rode novamente mais tarde.")
                abortado = True
                break
            if seguidas and seguidas % FALHAS_PARA_PAUSA == 0:
                pausa = random.randint(*PAUSA_INSTABILIDADE_S)
                print(f"  {seguidas} falhas seguidas; pausando {pausa // 60}min {pausa % 60}s e reiniciando a sessão...")
                time.sleep(pausa)
                try:
                    scraper.reset_session()
                except Exception as exc:
                    print(f"  Falha ao reiniciar sessão ({short_error(exc)}).")
            reg = row._asdict() if hasattr(row, "_asdict") else dict(zip(alvo.columns, row))
            codigo_ponto = str(reg.get("codigo_ponto") or "").strip() or str(reg.get("cod_interaguas"))
            cod_interaguas = str(reg.get("cod_interaguas") or "").strip()
            nome = f"{codigo_ponto} - {ini.year}-{fim.year}.xlsx"
            dest = output_dir / nome

            print(f"[{i}/{len(alvo)}] {codigo_ponto} (interaguas={cod_interaguas}) -> {nome}")
            try:
                status = scraper.download_point(codigo_ponto, cod_interaguas, chunks, dest, overwrite=args.overwrite)
            except Exception as exc:
                status = f"erro: {exc}"
            if status == "baixado":
                ok += 1
                seguidas = 0
            elif status == "existente":
                skip += 1
                print("  Já existe, pulando.")
                continue
            elif status == "sem_dados":
                nodata += 1
                seguidas = 0
                print("  Sem dados no período informado.")
            else:
                fail += 1
                seguidas += 1
                print(f"  FALHOU: {status}")
            time.sleep(REQUEST_DELAY_S)

        browser.close()

    print(f"\nConcluído: {ok} baixado(s), {skip} já existente(s), {nodata} sem dados, {fail} falha(s).")
    if fail or abortado:
        sys.exit(2)


if __name__ == "__main__":
    main()
