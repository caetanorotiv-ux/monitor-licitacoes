"""Monitor de licitações - Prefeitura de Caxias/MA -> aviso no Telegram."""
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL = "https://transparencia.caxias.ma.gov.br/transparencia/licitacoes/processos"
ESTADO = Path(__file__).parent / "vistos.json"
TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
# Pode ter vários IDs separados por vírgula. Ex: "123456789,987654321"
CHAT_IDS = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
# Opcional: palavras separadas por vírgula. Ex: "combustível,informática,obra"
# Vazio = avisa de todas as licitações novas.
PALAVRAS = [p.strip().lower() for p in os.environ.get("KEYWORDS", "").split(",") if p.strip()]
HEADERS = {"User-Agent": "Mozilla/5.0 (monitor-licitacoes; uso pessoal)"}


def baixar_lista():
    r = requests.get(URL, headers=HEADERS, timeout=60)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    itens = {}
    for a in soup.find_all("a", href=re.compile(r"processos\?id=(\d+)")):
        lic_id = re.search(r"id=(\d+)", a["href"]).group(1)
        if lic_id in itens:
            continue
        bloco = a.find_parent("tr") or a.find_parent("li") or a.parent
        texto = re.sub(r"\s+", " ", bloco.get_text(" ", strip=True))
        link = requests.compat.urljoin(URL, a["href"])
        itens[lic_id] = {"texto": texto[:700], "link": link}
    return itens


def enviar(msg):
    ok = 0
    for chat_id in CHAT_IDS:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                data={"chat_id": chat_id, "text": msg, "disable_web_page_preview": "true"},
                timeout=30,
            )
            r.raise_for_status()
            ok += 1
        except requests.RequestException as e:
            print(f"Falha ao enviar para {chat_id}: {e}")
    if ok == 0:
        raise RuntimeError("Não foi possível enviar para ninguém.")


def main():
    if not TOKEN or not CHAT_IDS:
        sys.exit("Defina TELEGRAM_TOKEN e TELEGRAM_CHAT_ID.")

    itens = baixar_lista()
    if not itens:
        sys.exit("Nenhuma licitação encontrada: o site pode ter mudado de layout.")

    if os.environ.get("TESTE") == "1":
        lic_id = max(itens, key=int)
        item = itens[lic_id]
        enviar(f"🧪 TESTE - é assim que uma licitação nova aparece:\n\n"
               f"🆕 Nova licitação (Caxias-MA)\n\n{item['texto']}\n\n🔗 {item['link']}")
        print("Mensagem de teste enviada.")
        return

    primeira_vez = not ESTADO.exists()
    vistos = set(json.loads(ESTADO.read_text())) if not primeira_vez else set()

    if primeira_vez:
        ESTADO.write_text(json.dumps(sorted(itens, key=int)))
        enviar(f"✅ Monitor ativado. {len(itens)} licitações já existentes foram "
               f"registradas. A partir de agora você só recebe as novas.")
        return

    novos = [i for i in sorted(itens, key=int) if i not in vistos]
    for lic_id in novos:
        item = itens[lic_id]
        if PALAVRAS and not any(p in item["texto"].lower() for p in PALAVRAS):
            vistos.add(lic_id)  # não interessa, mas não reavalia depois
            continue
        enviar(f"🆕 Nova licitação (Caxias-MA)\n\n{item['texto']}\n\n🔗 {item['link']}")
        vistos.add(lic_id)
        ESTADO.write_text(json.dumps(sorted(vistos, key=int)))  # salva a cada envio
        time.sleep(1)

    ESTADO.write_text(json.dumps(sorted(vistos | set(itens), key=int)))
    print(f"{len(novos)} novas encontradas.")


if __name__ == "__main__":
    main()
