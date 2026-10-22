"""Monitor de licitações -> Telegram.

Fontes:
  1. Prefeitura de Caxias-MA (portal da transparência)
  2. PNCP - Portal Nacional de Contratações Públicas (filtrado por UF, padrão MA)
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).parent
TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
# Vários IDs separados por vírgula. Ex: "123456789,-1001234567890"
CHAT_IDS = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
# Palavras-chave opcionais (vírgula). Vazio = avisa tudo.
PALAVRAS = [p.strip().lower() for p in os.environ.get("KEYWORDS", "").split(",") if p.strip()]
TESTE = os.environ.get("TESTE") == "1"

# --- PNCP ---
PNCP_UF = (os.environ.get("PNCP_UF") or "MA").strip().upper()
# 1 Leilão eletr., 2 Diálogo comp., 3 Concurso, 4 Concorrência eletr., 5 Concorrência pres.,
# 6 Pregão eletr., 7 Pregão pres., 8 Dispensa, 9 Inexigibilidade, 12 Credenciamento...
PNCP_MODALIDADES = [int(m) for m in (os.environ.get("PNCP_MODALIDADES") or "4,5,6,7").split(",") if m.strip()]
PNCP_DIAS = int(os.environ.get("PNCP_DIAS") or 2)        # janela de publicação consultada
MAX_ENVIO = int(os.environ.get("MAX_ENVIO") or 25)       # limite de mensagens por rodada/fonte

HEADERS = {"User-Agent": "Mozilla/5.0 (monitor-licitacoes; uso pessoal)"}
BRT = timezone(timedelta(hours=-3))


# ---------------------------------------------------------------- utilidades
def get_retry(url, params=None, tentativas=3):
    erro = None
    for i in range(tentativas):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=60)
            if r.status_code < 500:
                return r
            erro = RuntimeError(f"HTTP {r.status_code} em {url}")
        except requests.RequestException as e:
            erro = e
        time.sleep(3 * (i + 1))
    raise erro


def enviar(msg):
    ok = 0
    for chat_id in CHAT_IDS:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                data={"chat_id": chat_id, "text": msg[:4000], "disable_web_page_preview": "true"},
                timeout=30,
            )
            r.raise_for_status()
            ok += 1
        except requests.RequestException as e:
            print(f"Falha ao enviar para {chat_id}: {e}")
    if ok == 0:
        raise RuntimeError("Não foi possível enviar para ninguém.")


def passa_filtro(texto):
    return not PALAVRAS or any(p in texto.lower() for p in PALAVRAS)


def ler_json(caminho, padrao):
    return json.loads(caminho.read_text()) if caminho.exists() else padrao


def br_data(s):
    if not s:
        return "-"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}:\d{2}))?", s)
    if not m:
        return s
    d = f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    return f"{d} {m.group(4)}" if m.group(4) and m.group(4) != "00:00" else d


def br_valor(v):
    if v in (None, "", 0):
        return "não informado"
    return "R$ " + f"{float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# ------------------------------------------------- fonte 1: prefeitura de Caxias
CAXIAS_URL = "https://transparencia.caxias.ma.gov.br/transparencia/licitacoes/processos"
CAXIAS_ESTADO = BASE / "vistos.json"


def caxias_lista():
    r = get_retry(CAXIAS_URL)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    itens = {}
    for a in soup.find_all("a", href=re.compile(r"processos\?id=(\d+)")):
        lic_id = re.search(r"id=(\d+)", a["href"]).group(1)
        if lic_id in itens:
            continue
        bloco = a.find_parent("tr") or a.find_parent("li") or a.parent
        texto = re.sub(r"\s+", " ", bloco.get_text(" ", strip=True))
        itens[lic_id] = {"texto": texto[:700], "link": requests.compat.urljoin(CAXIAS_URL, a["href"])}
    return itens


def caxias_data_publicacao(item):
    m = re.search(r"PUBLICA..O:\s*(\d{2})/(\d{2})/(\d{4})", item["texto"])
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else ""


def fonte_caxias():
    itens = caxias_lista()
    if not itens:
        raise RuntimeError("Caxias: nenhuma licitação encontrada (layout pode ter mudado).")

    def msg(item, prefixo=""):
        return f"{prefixo}🆕 Nova licitação (Prefeitura de Caxias-MA)\n\n{item['texto']}\n\n🔗 {item['link']}"

    if TESTE:
        # a mais recente pela DATA DE PUBLICAÇÃO (id só desempata)
        recente = max(itens, key=lambda i: (caxias_data_publicacao(itens[i]), int(i)))
        enviar(msg(itens[recente], "🧪 TESTE - é assim que aparece:\n\n"))
        return

    if not CAXIAS_ESTADO.exists():
        CAXIAS_ESTADO.write_text(json.dumps(sorted(itens, key=int)))
        enviar(f"✅ Monitor ativado (Prefeitura de Caxias). {len(itens)} licitações já existentes "
               f"foram registradas. A partir de agora você só recebe as novas.")
        return

    vistos = set(ler_json(CAXIAS_ESTADO, []))
    novos = [i for i in sorted(itens, key=int) if i not in vistos]
    enviados = 0
    for lic_id in novos:
        if not passa_filtro(itens[lic_id]["texto"]):
            vistos.add(lic_id)
            continue
        if enviados >= MAX_ENVIO:
            break  # o restante sai na próxima rodada
        enviar(msg(itens[lic_id]))
        vistos.add(lic_id)
        enviados += 1
        CAXIAS_ESTADO.write_text(json.dumps(sorted(vistos, key=int)))
        time.sleep(1)
    CAXIAS_ESTADO.write_text(json.dumps(sorted(vistos, key=int)))
    print(f"[Caxias] {len(novos)} novas, {enviados} enviadas.")


# ------------------------------------------------------------- fonte 2: PNCP
PNCP_API = "https://pncp.gov.br/api/consulta/v1/contratacoes/publicacao"
PNCP_ESTADO = BASE / "vistos_pncp.json"


def pncp_buscar():
    hoje = datetime.now(BRT).date()
    ini = (hoje - timedelta(days=PNCP_DIAS - 1)).strftime("%Y%m%d")
    fim = hoje.strftime("%Y%m%d")
    achados = {}
    for modalidade in PNCP_MODALIDADES:
        pagina = 1
        while True:
            params = {"dataInicial": ini, "dataFinal": fim, "codigoModalidadeContratacao": modalidade,
                      "uf": PNCP_UF, "pagina": pagina, "tamanhoPagina": 50}
            r = get_retry(PNCP_API, params)
            if r.status_code == 204 or not r.content:
                break
            r.raise_for_status()
            j = r.json()
            for x in j.get("data") or []:
                if x.get("numeroControlePNCP"):
                    achados[x["numeroControlePNCP"]] = x
            if pagina >= int(j.get("totalPaginas") or 1):
                break
            pagina += 1
            time.sleep(0.5)
    return achados


def pncp_texto_busca(x):
    org = x.get("orgaoEntidade") or {}
    uni = x.get("unidadeOrgao") or {}
    return f"{x.get('objetoCompra','')} {org.get('razaoSocial','')} {uni.get('municipioNome','')}"


def pncp_msg(x, prefixo=""):
    org = x.get("orgaoEntidade") or {}
    uni = x.get("unidadeOrgao") or {}
    controle = x.get("numeroControlePNCP", "")
    m = re.match(r"(\d{14})-\d-(\d+)/(\d{4})", controle)
    link = f"https://pncp.gov.br/app/editais/{m.group(1)}/{m.group(3)}/{int(m.group(2))}" if m else "https://pncp.gov.br"
    objeto = re.sub(r"\s+", " ", x.get("objetoCompra") or "").strip()
    if len(objeto) > 600:
        objeto = objeto[:600] + "…"
    return (
        f"{prefixo}🆕 Nova licitação - PNCP ({PNCP_UF})\n\n"
        f"🏛 {org.get('razaoSocial','-')}\n"
        f"📍 {uni.get('municipioNome','-')}/{uni.get('ufSigla', PNCP_UF)}\n"
        f"📋 {x.get('modalidadeNome','-')} nº {x.get('numeroCompra','-')}/{x.get('anoCompra','-')}\n\n"
        f"📝 {objeto}\n\n"
        f"💰 {br_valor(x.get('valorTotalEstimado'))}\n"
        f"📅 Publicação: {br_data(x.get('dataPublicacaoPncp'))}\n"
        f"⏰ Propostas: {br_data(x.get('dataAberturaProposta'))} até {br_data(x.get('dataEncerramentoProposta'))}\n\n"
        f"🔗 {link}"
    )


def fonte_pncp():
    achados = pncp_buscar()
    ordenados = sorted(achados.values(), key=lambda x: x.get("dataPublicacaoPncp") or "")

    if TESTE:
        if ordenados:
            enviar(pncp_msg(ordenados[-1], "🧪 TESTE - é assim que aparece:\n\n"))
        else:
            enviar("🧪 TESTE PNCP: nenhuma licitação encontrada na janela consultada.")
        return

    hoje = datetime.now(BRT).date()
    vistos = ler_json(PNCP_ESTADO, None)  # dict {id: data}

    if vistos is None:
        PNCP_ESTADO.write_text(json.dumps({k: hoje.isoformat() for k in achados}))
        enviar(f"✅ PNCP ativado ({PNCP_UF}). {len(achados)} licitações dos últimos {PNCP_DIAS} dias "
               f"foram registradas. A partir de agora você só recebe as novas.")
        return

    enviados = 0
    novos = [x for x in ordenados if x["numeroControlePNCP"] not in vistos]
    for x in novos:
        cid = x["numeroControlePNCP"]
        if not passa_filtro(pncp_texto_busca(x)):
            vistos[cid] = hoje.isoformat()
            continue
        if enviados >= MAX_ENVIO:
            break
        enviar(pncp_msg(x))
        vistos[cid] = hoje.isoformat()
        enviados += 1
        PNCP_ESTADO.write_text(json.dumps(vistos))
        time.sleep(1)

    limite = (hoje - timedelta(days=20)).isoformat()  # limpa registros antigos
    vistos = {k: v for k, v in vistos.items() if v >= limite}
    PNCP_ESTADO.write_text(json.dumps(vistos))
    print(f"[PNCP] {len(novos)} novas, {enviados} enviadas.")


# ------------------------------------------------------------------------ main
def main():
    if not TOKEN or not CHAT_IDS:
        sys.exit("Defina TELEGRAM_TOKEN e TELEGRAM_CHAT_ID.")
    erros = []
    for nome, fonte in (("Caxias", fonte_caxias), ("PNCP", fonte_pncp)):
        try:
            fonte()
        except Exception as e:  # uma fonte com problema não derruba a outra
            print(f"[{nome}] ERRO: {e}")
            erros.append(nome)
    if erros:
        sys.exit(1)


if __name__ == "__main__":
    main()
