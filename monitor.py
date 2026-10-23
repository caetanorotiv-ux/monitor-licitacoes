"""Monitor de licitações -> Telegram.

Fontes:
  1. Prefeitura de Caxias-MA (portal da transparência)
  2. PNCP - Portal Nacional de Contratações Públicas (filtrado por UF, padrão MA)
"""
import json
import unicodedata
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
def sem_acento(t):
    return "".join(c for c in unicodedata.normalize("NFD", str(t).lower()) if unicodedata.category(c) != "Mn")


PALAVRAS = [sem_acento(p.strip()) for p in os.environ.get("KEYWORDS", "").split(",") if p.strip()]
TESTE = os.environ.get("TESTE") == "1"
# Modo retroativo: preenche o MAPA com as licitações dos últimos N dias, sem enviar nada ao Telegram
RETRO = int(os.environ.get("MAPA_RETRO") or 0)

# --- PNCP ---
PNCP_UFS = [u.strip().upper() for u in (os.environ.get("PNCP_UF") or "MA,TO,PI,CE").split(",") if u.strip()]
# 1 Leilão eletr., 2 Diálogo comp., 3 Concurso, 4 Concorrência eletr., 5 Concorrência pres.,
# 6 Pregão eletr., 7 Pregão pres., 8 Dispensa, 9 Inexigibilidade, 12 Credenciamento...
PNCP_MODALIDADES = [int(m) for m in (os.environ.get("PNCP_MODALIDADES") or "4,5,6,7").split(",") if m.strip()]
PNCP_DIAS = int(os.environ.get("PNCP_DIAS") or 2)        # janela de publicação consultada
# Fonte (sistema onde a licitação roda) aceita no PNCP. Padrão: Compras.gov.br e Portal de Compras Públicas.
# Para receber de qualquer fonte, defina a variável PNCP_FONTES com o valor: todas
FONTES_PADRAO = "compras.gov.br,comprasnet.gov.br,portal de compras publicas,portaldecompraspublicas"
_f = os.environ.get("PNCP_FONTES") or FONTES_PADRAO
PNCP_FONTES = [] if sem_acento(_f) in ("todas", "todos", "*") else [sem_acento(t) for t in _f.split(",") if t.strip()]
PNCP_INTERVALO = int(os.environ.get("PNCP_INTERVALO") or 0)  # minutos entre consultas ao PNCP (nas execuções agendadas)
MAX_ENVIO = int(os.environ.get("MAX_ENVIO") or 15)       # limite de mensagens por rodada/fonte

HEADERS = {"User-Agent": "Mozilla/5.0 (monitor-licitacoes; uso pessoal)"}
BRT = timezone(timedelta(hours=-3))


# ---------------------------------------------------------------- utilidades
class LimiteRitmo(Exception):
    """O site respondeu 429 (excesso de requisições)."""


def get_retry(url, params=None, tentativas=3):
    erro = None
    for i in range(tentativas):
        espera = 5 * (i + 1)
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=60)
            if r.status_code == 429:
                erro = LimiteRitmo("HTTP 429 - limite de requisições do site")
                try:
                    espera = min(int(r.headers.get("Retry-After", espera)), 60)
                except ValueError:
                    pass
            elif r.status_code >= 500:
                erro = RuntimeError(f"HTTP {r.status_code} em {url.split('?')[0]}")
            else:
                return r
        except requests.RequestException as e:
            erro = e
        if i < tentativas - 1:
            time.sleep(espera)
    raise erro


def limpar(texto):
    """Nunca deixa o token nem IDs de chat aparecerem nos logs (repositório público)."""
    texto = str(texto)
    for segredo in [TOKEN] + CHAT_IDS:
        if segredo:
            texto = texto.replace(segredo, "***")
    return texto


def enviar(msg):
    ok = 0
    for chat_id in CHAT_IDS:
        for tentativa in range(4):
            try:
                r = requests.post(
                    f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                    data={"chat_id": chat_id, "text": msg[:4000], "disable_web_page_preview": "true"},
                    timeout=30,
                )
                if r.status_code == 429:  # limite de ritmo do Telegram: espera o tempo pedido
                    try:
                        espera = int(r.json().get("parameters", {}).get("retry_after", 5))
                    except Exception:
                        espera = 5
                    if espera > 60:
                        raise RuntimeError(f"Telegram pediu espera de {espera}s")
                    time.sleep(espera + 1)
                    continue
                r.raise_for_status()
                ok += 1
                break
            except RuntimeError:
                raise
            except requests.RequestException as e:
                print(f"Falha ao enviar para um destino: {limpar(e)}")
                break
    if ok == 0:
        raise RuntimeError("Não foi possível enviar para ninguém.")


def passa_filtro(texto):
    texto = sem_acento(texto)
    # palavra inteira (aceita plural): "ovos" não casa com "novos", "trave" não casa com "através"
    return not PALAVRAS or any(re.search(r"\b" + re.escape(p) + r"s?\b", texto) for p in PALAVRAS)


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


# ------------------------------------------------------------ registro para o mapa
MAPA_DADOS = BASE / "docs" / "data.json"
MAPA_MAX = 3000          # no máximo N licitações guardadas
MAPA_DIAS = 90           # e só as dos últimos N dias
TEMAS = {                # tema -> palavras (sem acento); a página colore por tema
    "esportivo": ["material esportivo", "materiais esportivos", "artigo esportivo", "artigos esportivos",
                  "equipamento esportivo", "equipamentos esportivos", "bola", "futsal", "quimono", "trofeu", "medalha"],
    "alimentos": ["genero alimenticio", "generos alimenticios", "alimentacao escolar", "merenda", "hortifruti",
                  "cesta basica", "carne", "frango", "pescado", "leite", "arroz", "feijao", "macarrao",
                  "biscoito", "farinha", "acucar", "ovos"],
    "vestuario": ["confeccao", "fardamento", "uniforme", "camisa", "camiseta", "calca", "bermuda", "agasalho",
                  "jaleco", "vestuario", "tecido", "calcado", "tenis"],
}


def temas_de(texto):
    t = sem_acento(texto)
    return [tema for tema, ps in TEMAS.items() if any(re.search(r"\b" + re.escape(p) + r"s?\b", t) for p in ps)]


def registrar(rec, quando=None):
    """Guarda a licitação em docs/data.json (alimenta a página do mapa).
    quando: data real da publicação (modo retroativo). Sem ela, usa o momento do envio."""
    try:
        dados = ler_json(MAPA_DADOS, {"itens": []})
        if quando and any(i.get("id") == rec["id"] for i in dados.get("itens", [])):
            return False  # já está no mapa; mantém o registro original
        itens = [i for i in dados.get("itens", []) if i.get("id") != rec["id"]]
        rec["enviado"] = quando or datetime.now(BRT).isoformat(timespec="seconds")
        rec["temas"] = temas_de(rec.get("objeto", ""))
        itens.insert(0, rec)
        limite = (datetime.now(BRT) - timedelta(days=MAPA_DIAS)).isoformat()
        itens = [i for i in itens if i.get("enviado", "") >= limite][:MAPA_MAX]
        MAPA_DADOS.parent.mkdir(exist_ok=True)
        MAPA_DADOS.write_text(json.dumps({"atualizado": datetime.now(BRT).isoformat(timespec="seconds"), "itens": itens},
                                         ensure_ascii=False, separators=(",", ":")))
        return True
    except Exception as e:  # o mapa nunca pode derrubar o monitor
        print(f"[mapa] não foi possível registrar: {limpar(e)}")
        return False


def rec_caxias(lic_id, item):
    return {"id": f"CAXIAS-{lic_id}", "fonte": "Prefeitura de Caxias", "uf": "MA",
            "municipio": "Caxias", "ibge": "2103000", "orgao": "Prefeitura Municipal de Caxias",
            "objeto": item["texto"][:400], "modalidade": "", "valor": None,
            "publicacao": "", "link": item["link"]}


def rec_pncp(x):
    org, uni = x.get("orgaoEntidade") or {}, x.get("unidadeOrgao") or {}
    cid = x.get("numeroControlePNCP", "")
    m = re.match(r"(\d{14})-\d-(\d+)/(\d{4})", cid)
    return {"id": cid, "fonte": "PNCP", "uf": uni.get("ufSigla") or x.get("_uf", ""),
            "municipio": uni.get("municipioNome", ""), "ibge": str(uni.get("codigoIbge") or ""),
            "orgao": org.get("razaoSocial", ""),
            "objeto": re.sub(r"\s+", " ", x.get("objetoCompra") or "").strip()[:400],
            "modalidade": f"{x.get('modalidadeNome', '')} {x.get('numeroCompra', '')}/{x.get('anoCompra', '')}".strip(),
            "valor": x.get("valorTotalEstimado"), "publicacao": x.get("dataPublicacaoPncp", ""),
            "sistema": x.get("usuarioNome") or "",
            "link": f"https://pncp.gov.br/app/editais/{m.group(1)}/{m.group(3)}/{int(m.group(2))}" if m else "https://pncp.gov.br"}


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
        registrar(rec_caxias(lic_id, itens[lic_id]))
        vistos.add(lic_id)
        enviados += 1
        CAXIAS_ESTADO.write_text(json.dumps(sorted(vistos, key=int)))
        time.sleep(3)
    CAXIAS_ESTADO.write_text(json.dumps(sorted(vistos, key=int)))
    print(f"[Caxias] {len(novos)} novas, {enviados} enviadas.")


# ------------------------------------------------------------- fonte 2: PNCP
PNCP_API = "https://pncp.gov.br/api/consulta/v1/contratacoes/publicacao"
PNCP_ESTADO = BASE / "vistos_pncp.json"
PNCP_ERROS = {}  # uf -> último erro (para o modo teste explicar)


def pncp_buscar(dias=None):
    """Retorna (achados, falhas). achados: {id: licitação}; falhas: UFs com erro de consulta."""
    hoje = datetime.now(BRT).date()
    ini = (hoje - timedelta(days=(dias or PNCP_DIAS) - 1)).strftime("%Y%m%d")
    fim = hoje.strftime("%Y%m%d")
    achados, falhas = {}, set()
    # gira a ordem dos estados a cada rodada, para nenhum ficar sempre por último
    rot = int(time.time() // 300) % len(PNCP_UFS)
    ordem = PNCP_UFS[rot:] + PNCP_UFS[:rot]
    bloqueado = False
    for uf in ordem:
        if bloqueado:  # o PNCP já mandou parar: não insiste, tenta na próxima rodada
            falhas.add(uf)
            PNCP_ERROS.setdefault(uf, "limite de requisições do PNCP (429)")
            continue
        for modalidade in PNCP_MODALIDADES:
            time.sleep(1.5)  # ritmo mais lento para respeitar o limite do PNCP
            try:
                pagina = 1
                while True:
                    params = {"dataInicial": ini, "dataFinal": fim, "codigoModalidadeContratacao": modalidade,
                              "uf": uf, "pagina": pagina, "tamanhoPagina": 50}
                    r = get_retry(PNCP_API, params)
                    if r.status_code == 204 or not r.content:
                        break
                    r.raise_for_status()
                    j = r.json()
                    for x in j.get("data") or []:
                        if x.get("numeroControlePNCP"):
                            x["_uf"] = uf
                            achados[x["numeroControlePNCP"]] = x
                    if pagina >= int(j.get("totalPaginas") or 1):
                        break
                    pagina += 1
                    time.sleep(1.5)
            except LimiteRitmo as e:
                print(f"[PNCP] limite de requisições atingido em {uf}/modalidade {modalidade}; "
                      f"pausando as consultas desta rodada.")
                falhas.add(uf)
                PNCP_ERROS.setdefault(uf, "limite de requisições do PNCP (429)")
                bloqueado = True
                break
            except Exception as e:
                print(f"[PNCP] falha em {uf}/modalidade {modalidade}: {limpar(e)}")
                falhas.add(uf)
                PNCP_ERROS.setdefault(uf, limpar(e))
    return achados, falhas


def passa_fonte(x):
    """True se a licitação vem de uma das fontes aceitas (campo 'usuarioNome' do PNCP = 'Fonte' do site)."""
    if not PNCP_FONTES:
        return True
    t = sem_acento(f"{x.get('usuarioNome') or ''} {x.get('linkSistemaOrigem') or ''}")
    return any(f in t for f in PNCP_FONTES)


def resumo_fontes(lista, n=8):
    from collections import Counter
    c = Counter((x.get("usuarioNome") or "(sem fonte)").strip() for x in lista)
    return ", ".join(f"{k} ({v})" for k, v in c.most_common(n))


def pncp_texto_busca(x):
    org = x.get("orgaoEntidade") or {}
    uni = x.get("unidadeOrgao") or {}
    return f"{x.get('objetoCompra','')} {org.get('razaoSocial','')} {uni.get('municipioNome','')}"


def pncp_msg(x, prefixo=""):
    org = x.get("orgaoEntidade") or {}
    uni = x.get("unidadeOrgao") or {}
    uf = uni.get("ufSigla") or x.get("_uf", "")
    controle = x.get("numeroControlePNCP", "")
    m = re.match(r"(\d{14})-\d-(\d+)/(\d{4})", controle)
    link = f"https://pncp.gov.br/app/editais/{m.group(1)}/{m.group(3)}/{int(m.group(2))}" if m else "https://pncp.gov.br"
    objeto = re.sub(r"\s+", " ", x.get("objetoCompra") or "").strip()
    if len(objeto) > 600:
        objeto = objeto[:600] + "…"
    return (
        f"{prefixo}🆕 Nova licitação - PNCP ({uf})\n\n"
        f"🏛 {org.get('razaoSocial','-')}\n"
        f"📍 {uni.get('municipioNome','-')}/{uf}\n"
        f"📋 {x.get('modalidadeNome','-')} nº {x.get('numeroCompra','-')}/{x.get('anoCompra','-')}\n\n"
        f"📝 {objeto}\n\n"
        f"💰 {br_valor(x.get('valorTotalEstimado'))}\n"
        f"📅 Publicação: {br_data(x.get('dataPublicacaoPncp'))}\n"
        f"⏰ Propostas: {br_data(x.get('dataAberturaProposta'))} até {br_data(x.get('dataEncerramentoProposta'))}\n"
        f"🖥 Fonte: {x.get('usuarioNome') or '-'}\n\n"
        f"🔗 {link}"
    )


def pncp_carregar_estado():
    e = ler_json(PNCP_ESTADO, None)
    if e is None:
        return {"ufs": [], "ids": {}}
    if "ids" not in e:  # formato antigo: só MA, sem lista de estados
        return {"ufs": ["MA"], "ids": e}
    return e


def pncp_deve_rodar():
    """Só limita o PNCP se PNCP_INTERVALO > 5 for definido; por padrão consulta sempre."""
    if TESTE or PNCP_INTERVALO <= 5 or os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        return True  # padrão: sempre consulta
    return datetime.now(BRT).minute % PNCP_INTERVALO < 5


def fonte_pncp():
    if not pncp_deve_rodar():
        print("[PNCP] pulando esta rodada (intervalo entre consultas).")
        return
    achados, falhas = pncp_buscar()
    ordenados = sorted(achados.values(), key=lambda x: x.get("dataPublicacaoPncp") or "")

    if TESTE:
        sem = []
        for uf in PNCP_UFS:
            todas_uf = [x for x in ordenados if x["_uf"] == uf]
            do_uf = [x for x in todas_uf if passa_fonte(x)]
            if do_uf:
                enviar(pncp_msg(do_uf[-1], "🧪 TESTE - é assim que aparece:\n\n"))
            elif uf in falhas:
                sem.append(f"{uf}: a consulta ao PNCP falhou ({PNCP_ERROS.get(uf, 'erro')})")
            elif todas_uf:
                sem.append(f"{uf}: {len(todas_uf)} licitações na janela, nenhuma das fontes aceitas")
            else:
                sem.append(f"{uf}: nenhuma licitação nos últimos {PNCP_DIAS} dias nas modalidades {PNCP_MODALIDADES}")
        resumo = f"fontes aceitas: {', '.join(PNCP_FONTES) or 'todas'}\nfontes encontradas na janela: {resumo_fontes(ordenados) or 'nenhuma'}"
        enviar("🧪 TESTE PNCP\n" + ("\n".join(f"• {l}" for l in sem) + "\n\n" if sem else "") + resumo)
        return

    if falhas and not achados:
        raise RuntimeError(f"PNCP: consulta falhou para {', '.join(sorted(falhas))}.")

    hoje = datetime.now(BRT).date()
    estado = pncp_carregar_estado()
    ids = estado["ids"]

    def salvar():
        PNCP_ESTADO.write_text(json.dumps(estado))

    # Estados novos: registra o que já existe SEM enviar (evita inundar o grupo)
    novas_ufs = [u for u in PNCP_UFS if u not in estado["ufs"] and u not in falhas]
    if novas_ufs:
        qtd = 0
        for x in ordenados:
            if x["_uf"] in novas_ufs and x["numeroControlePNCP"] not in ids:
                ids[x["numeroControlePNCP"]] = hoje.isoformat()
                qtd += 1
        estado["ufs"] += novas_ufs
        salvar()
        enviar(f"✅ PNCP ativado para {', '.join(novas_ufs)}. {qtd} licitações dos últimos "
               f"{PNCP_DIAS} dias foram registradas. A partir de agora você só recebe as novas.")

    enviados = 0
    novos = [x for x in ordenados
             if x["_uf"] in estado["ufs"] and x["numeroControlePNCP"] not in ids]
    for x in novos:
        cid = x["numeroControlePNCP"]
        if not passa_fonte(x) or not passa_filtro(pncp_texto_busca(x)):
            ids[cid] = hoje.isoformat()
            continue
        if enviados >= MAX_ENVIO:
            break  # o restante sai na próxima rodada
        try:
            enviar(pncp_msg(x))
        except RuntimeError as e:
            print(f"[PNCP] envio interrompido ({limpar(e)}); o restante sai na próxima rodada.")
            break
        registrar(rec_pncp(x))
        ids[cid] = hoje.isoformat()
        enviados += 1
        salvar()
        time.sleep(3)

    limite = (hoje - timedelta(days=20)).isoformat()  # limpa registros antigos
    estado["ids"] = {k: v for k, v in ids.items() if v >= limite}
    salvar()
    print(f"[PNCP] {len(novos)} novas, {enviados} enviadas. Falhas: {sorted(falhas) or 'nenhuma'}")
    if novos:
        print(f"[PNCP] fontes entre as novas: {resumo_fontes(novos)}")


# ------------------------------------------- fonte 3: Diário Oficial dos Municípios (FAMEM)
# Lê SOMENTE a página inicial do site (pública). Não acessa os PDFs automaticamente.
FAMEM_URL = "https://diariooficial.famem.org.br/"
FAMEM_ESTADO = BASE / "vistos_famem.json"


def famem_edicoes():
    r = get_retry(FAMEM_URL)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    edicoes = {}
    for a in soup.find_all("a", href=True):
        texto = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        m = re.search(r"N[ºo°]\.?\s*(\d{3,5})\s*/\s*(\d{4})", texto)
        if not m:
            # o número pode estar só no link do PDF (…edicao-3962-assinado.pdf)
            m2 = re.search(r"edicao-(\d{3,5})", a["href"])
            if not m2:
                continue
            num, ano = m2.group(1), str(datetime.now(BRT).year)
        else:
            num, ano = m.group(1), m.group(2)
        d = re.search(r"\d{1,2} de [a-zç]+ de \d{4}", texto, re.I)
        edicoes.setdefault(num, {"ano": ano, "data": d.group(0) if d else "",
                                 "link": requests.compat.urljoin(FAMEM_URL, a["href"])})
    return edicoes


def famem_msg(num, e, prefixo=""):
    data = f" - {e['data']}" if e["data"] else ""
    return (f"{prefixo}📰 Nova edição do Diário Oficial dos Municípios (FAMEM-MA)\n\n"
            f"Edição nº {num}/{e['ano']}{data}\n\n🔗 {e['link']}")


def fonte_famem():
    hora = datetime.now(BRT).hour
    if not TESTE and not (6 <= hora < 22):  # diário sai em dias úteis; evita consultas de madrugada
        return
    edicoes = famem_edicoes()
    if not edicoes:
        raise RuntimeError("FAMEM: nenhuma edição encontrada (layout pode ter mudado).")
    if TESTE:
        num = max(edicoes, key=int)
        enviar(famem_msg(num, edicoes[num], "🧪 TESTE - é assim que aparece:\n\n"))
        return
    if not FAMEM_ESTADO.exists():
        FAMEM_ESTADO.write_text(json.dumps(sorted(edicoes, key=int)))
        enviar("✅ Aviso do Diário Oficial dos Municípios (FAMEM) ativado. "
               "Você receberá uma mensagem a cada nova edição.")
        return
    vistos = set(ler_json(FAMEM_ESTADO, []))
    novas = [n for n in sorted(edicoes, key=int) if n not in vistos]
    for n in novas[-5:]:  # no máximo as 5 mais recentes
        enviar(famem_msg(n, edicoes[n]))
        vistos.add(n)
        FAMEM_ESTADO.write_text(json.dumps(sorted(vistos, key=int)[-60:]))
        time.sleep(2)
    if novas:
        vistos |= set(novas)
        FAMEM_ESTADO.write_text(json.dumps(sorted(vistos, key=int)[-60:]))
    print(f"[FAMEM] {len(novas)} novas edições.")


def retroativo(dias):
    """Preenche o mapa com as licitações dos últimos N dias. NÃO envia nada ao Telegram
    e NÃO altera o histórico de 'já vistos'."""
    hoje = datetime.now(BRT).date()
    corte = (hoje - timedelta(days=dias - 1)).isoformat()
    novos = {"PNCP": 0, "Caxias": 0}
    achados, falhas = pncp_buscar(dias)
    for x in achados.values():
        if passa_fonte(x) and passa_filtro(pncp_texto_busca(x)) and registrar(rec_pncp(x), quando=x.get("dataPublicacaoPncp") or None):
            novos["PNCP"] += 1
    try:
        itens = caxias_lista()
        for lic_id, it in itens.items():
            d = caxias_data_publicacao(it)
            if d and d >= corte and passa_filtro(it["texto"]):
                if registrar(rec_caxias(lic_id, it), quando=d + "T00:00:00-03:00"):
                    novos["Caxias"] += 1
    except Exception as e:
        print(f"[retro] Caxias falhou: {limpar(e)}")
        falhas.add("Caxias")
    print(f"[retro] últimos {dias} dias: {novos['PNCP']} do PNCP e {novos['Caxias']} de Caxias adicionadas ao mapa. "
          f"Consultas com falha: {sorted(falhas) or 'nenhuma'}")
    if falhas:
        print("[retro] Algo falhou (provável limite do PNCP): rode de novo em alguns minutos; o que já entrou não duplica.")


# ------------------------------------------------------------------------ main
def main():
    if not TOKEN or not CHAT_IDS:
        sys.exit("Defina TELEGRAM_TOKEN e TELEGRAM_CHAT_ID.")
    if RETRO:
        retroativo(RETRO)
        return
    erros = []
    for nome, fonte in (("Caxias", fonte_caxias), ("PNCP", fonte_pncp), ("FAMEM", fonte_famem)):
        try:
            fonte()
        except Exception as e:  # uma fonte com problema não derruba a outra
            print(f"[{nome}] ERRO: {limpar(e)}")
            erros.append(nome)
    if erros:
        sys.exit(1)


if __name__ == "__main__":
    main()
