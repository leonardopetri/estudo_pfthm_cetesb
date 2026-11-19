"""Executa scraper_infoaguas.py para as UGRHIs 01 a 22, com pausa aleatória entre execuções.

Toda a saída (deste script e do scraper) é exibida no terminal e gravada em
logs/baixar_ugrhis_AAAAMMDD_HHMMSS.log.

Uso:
  python baixar_todas_ugrhis.py
  python baixar_todas_ugrhis.py --inicio 10 --fim 22
"""

import argparse
import os
import random
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SCRAPER = Path(__file__).with_name("scraper_infoaguas.py")
LOG_DIR = Path(__file__).with_name("logs")
PAUSA_MIN_S = 30
PAUSA_MAX_S = 180


class Logger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.file = open(path, "a", encoding="utf-8")

    def write(self, line: str) -> None:
        print(line, flush=True)
        self.file.write(line + "\n")
        self.file.flush()

    def log(self, msg: str) -> None:
        self.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}")

    def close(self) -> None:
        self.file.close()


def run_scraper(cmd: list[str], logger: Logger) -> int:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        cmd,
        cwd=SCRAPER.parent,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        for line in proc.stdout:
            logger.write(line.rstrip("\n"))
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait()
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Baixa os relatórios InfoÁguas de várias UGRHIs em sequência.")
    parser.add_argument("--inicio", type=int, default=1, help="Primeira UGRHI (padrão: 1).")
    parser.add_argument("--fim", type=int, default=22, help="Última UGRHI (padrão: 22).")
    parser.add_argument("--data-inicio", default="01/01/2010", help="Data inicial dd/mm/aaaa (padrão: 01/01/2010).")
    parser.add_argument("--data-fim", default=datetime.now().strftime("%d/%m/%Y"), help="Data final dd/mm/aaaa (padrão: hoje).")
    parser.add_argument("--overwrite", action="store_true", help="Atualiza também arquivos com manifesto válido.")
    args = parser.parse_args()
    if not 1 <= args.inicio <= args.fim <= 22:
        parser.error("Use 1 <= inicio <= fim <= 22.")

    sys.stdout.reconfigure(errors="replace")
    logger = Logger(LOG_DIR / f"baixar_ugrhis_{datetime.now():%Y%m%d_%H%M%S}.log")
    logger.log(f"Log gravado em {logger.path}")

    ugrhis = [f"{n:02d}" for n in range(args.inicio, args.fim + 1)]
    resultados = {}
    try:
        for i, ugrhi in enumerate(ugrhis):
            logger.log(f"Iniciando UGRHI {ugrhi} ({i + 1}/{len(ugrhis)})")
            cmd = [sys.executable, str(SCRAPER), "--ugrhi", ugrhi,
                   f"--data-inicio={args.data_inicio}", f"--data-fim={args.data_fim}"]
            if args.overwrite:
                cmd.append("--overwrite")
            code = run_scraper(cmd, logger)
            resultados[ugrhi] = code
            logger.log(f"UGRHI {ugrhi} finalizada com código {code}" + (" (houve falhas)" if code else ""))

            if i < len(ugrhis) - 1:
                pausa = random.randint(PAUSA_MIN_S, PAUSA_MAX_S)
                logger.log(f"Aguardando {pausa // 60}min {pausa % 60}s antes da próxima UGRHI...")
                time.sleep(pausa)
    except KeyboardInterrupt:
        logger.log("Interrompido pelo usuário (Ctrl+C).")
        logger.close()
        sys.exit(130)

    falhas = [u for u, c in resultados.items() if c]
    logger.log(f"Concluído. {len(ugrhis) - len(falhas)} UGRHI(s) sem falhas.")
    if falhas:
        logger.log(
            f"UGRHIs com falhas (rode novamente para completar; arquivos existentes são pulados): {', '.join(falhas)}"
        )
    logger.close()
    if falhas:
        sys.exit(2)


if __name__ == "__main__":
    main()
