import requests
import gspread
import re
import time
import unicodedata
import difflib
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from google.oauth2.service_account import Credentials
from datetime import datetime
import os
import json


# =====================================================
# CONFIGURAÇÃO DE IDIOMAS
# =====================================================
# Idiomas processados normalmente. Qualquer outro (ex.: francês) vira FORA_DO_ESCOPO.
IDIOMAS_SUPORTADOS = {"pt", "es", "en"}


# =====================================================
# SESSÃO HTTP (ANTI-BLOQUEIO)
# =====================================================
session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,es;q=0.8,en;q=0.7"
})


def requisitar_com_retry(link, tentativas=2, timeout=30):
    """Tenta buscar o link algumas vezes antes de desistir (protege contra timeouts pontuais)."""
    ultimo_erro = None
    for tentativa in range(1, tentativas + 1):
        try:
            return session.get(link, timeout=timeout)
        except requests.exceptions.Timeout as e:
            ultimo_erro = e
            print(f"   Timeout na tentativa {tentativa}/{tentativas}, tentando de novo...")
    raise ultimo_erro


NUM_COLUNAS = 8  # DATA, REVISTA, TEMA, QUALIS, ISSN, LINK, STATUS, IDIOMA


def carregar_dados_existentes(saida):
    """Lê a planilha atual e retorna {link: [linha1, linha2, ...]} das linhas já OK."""
    valores = saida.get_all_values()

    dados_ok = {}
    for linha in valores[1:]:  # pula o cabeçalho
        if len(linha) >= 7 and linha[6].strip().upper() == "OK":
            link = linha[5]
            # Completa linhas antigas (sem coluna IDIOMA) até 8 colunas
            linha_completa = (linha + [""] * NUM_COLUNAS)[:NUM_COLUNAS]
            dados_ok.setdefault(link, []).append(linha_completa)

    return dados_ok


# =====================================================
# UTILITÁRIOS DE TEXTO
# =====================================================
def sem_acento(texto):
    return "".join(
        c for c in unicodedata.normalize("NFD", texto)
        if unicodedata.category(c) != "Mn"
    )



# =====================================================
# PLANILHA DE REFERÊNCIA (REVISTA | QUALIS | ISSN)
# =====================================================
# Aba na MESMA planilha do projeto: coluna A = revista, B = qualis, C = ISSN
ABA_REFERENCIA = "QUALIS - ISSN"

QUALIS_VALIDOS = {"A1", "A2", "A3", "A4", "B1", "B2", "B3", "B4", "C"}


def normalizar_nome_revista(nome):
    """Minúsculas, sem acento, sem pontuação e sem espaços repetidos (para comparar nomes)."""
    n = sem_acento(str(nome).lower())
    n = re.sub(r"[^a-z0-9\s]", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def listar_issns(texto):
    """Retorna todos os ISSNs válidos de um texto, no formato 0000-000X."""
    return [f"{a}-{b.upper()}" for a, b in re.findall(r"(\d{4})\s*-?\s*(\d{3}[\dXx])", str(texto))]


def carregar_tabela_referencia(sh, aba_nome=ABA_REFERENCIA):
    """Lê a aba de referência uma única vez. Retorna {'por_nome': {...}, 'por_issn': {...}}."""
    tabela = {"por_nome": {}, "por_issn": {}}
    try:
        abas = sh.worksheets()
        alvo = re.sub(r"[^a-z0-9]", "", sem_acento(aba_nome.lower()))
        aba = next((a for a in abas if re.sub(r"[^a-z0-9]", "", sem_acento(a.title.lower())) == alvo), None)
        if aba is None:
            print(f"AVISO: aba de referência '{aba_nome}' não encontrada. Abas existentes: {[a.title for a in abas]}")
            return tabela
        linhas = aba.get_all_values()
    except Exception as e:
        print(f"AVISO: não consegui ler a aba de referência '{aba_nome}': {repr(e)[:200]}")
        return tabela

    for linha in linhas:
        linha = (linha + ["", "", ""])[:3]
        nome = linha[0].strip()
        qualis = linha[1].strip().upper()
        issns = listar_issns(linha[2])

        if qualis not in QUALIS_VALIDOS:
            qualis = "-"
        if not nome or (qualis == "-" and not issns):
            continue  # cabeçalho ou linha vazia

        registro = {"revista": nome, "qualis": qualis, "issn": issns[0] if issns else "-"}
        tabela["por_nome"].setdefault(normalizar_nome_revista(nome), registro)
        for i in issns:
            tabela["por_issn"].setdefault(i, registro)

    print(f"Tabela de referência carregada: {len(tabela['por_nome'])} revistas, {len(tabela['por_issn'])} ISSNs")
    return tabela


def complementar_com_referencia(revista, qualis, issn, tabela):
    """
    Preenche SOMENTE campos que estão '-'. Nunca sobrescreve o que veio da página.
    Ordem: ISSN -> nome exato -> nome parecido (>= 90%).
    """
    if not tabela or not tabela["por_nome"]:
        return revista, qualis, issn

    registro = None

    # 1. Pelo ISSN encontrado na página
    for i in listar_issns(issn) if issn != "-" else []:
        if i in tabela["por_issn"]:
            registro = tabela["por_issn"][i]
            break

    # 2. Pelo nome exato / 3. nome parecido
    if not registro and revista != "-":
        nome_norm = normalizar_nome_revista(revista)
        registro = tabela["por_nome"].get(nome_norm)
        if not registro:
            parecido = difflib.get_close_matches(nome_norm, list(tabela["por_nome"].keys()), n=1, cutoff=0.9)
            if parecido:
                registro = tabela["por_nome"][parecido[0]]

    if not registro:
        return revista, qualis, issn

    if revista == "-":
        revista = registro["revista"]
    if qualis == "-" and registro["qualis"] != "-":
        qualis = registro["qualis"]
    if issn == "-" and registro["issn"] != "-":
        issn = registro["issn"]

    return revista, qualis, issn


# =====================================================
# DETECÇÃO DE IDIOMA
# =====================================================
# Palavras curtas e frequentes, típicas de cada idioma (evita palavras que existem em vários).
MARCADORES_IDIOMA = {
    "pt": [" não ", " são ", " também ", " até ", " dos ", " das ", " pelo ", " pela ",
           " uma ", " com ", " e ", " submissão", " artigos ", " prazo ", " dossiê", " edição"],
    "es": [" el ", " los ", " las ", " del ", " y ", " una ", " con ", " también ", " hasta ",
           " convocatoria", " plazo ", " artículos", " envío", " fecha ", " monográfico"],
    "en": [" the ", " and ", " of ", " for ", " with ", " is ", " are ",
           " submission", " deadline", " call for papers", " journal ", " issue "],
    "fr": [" le ", " les ", " et ", " des ", " du ", " une ", " pour ", " avec ",
           " appel à", " revue ", " numéro ", " soumission"],
}

DOMINIOS_IDIOMA = {
    ".br": "pt", ".pt": "pt",
    ".es": "es", ".ar": "es", ".mx": "es", ".cl": "es", ".co": "es",
    ".pe": "es", ".uy": "es", ".ve": "es", ".ec": "es", ".bo": "es", ".py": "es", ".cr": "es",
    ".fr": "fr", ".be": "fr",
}


def detectar_idioma(sopa, texto_pagina, url):
    """
    Retorna 'pt', 'es', 'en', 'fr' (ou outro código vindo do <html lang>).
    Ordem de confiança: texto da página (se tiver sinal suficiente) > atributo lang > domínio.
    """
    texto_norm = " " + re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", texto_pagina.lower())) + " "

    scores = {
        idioma: sum(texto_norm.count(p) for p in palavras)
        for idioma, palavras in MARCADORES_IDIOMA.items()
    }
    total = sum(scores.values())
    idioma_texto = max(scores, key=scores.get) if total else None

    # 1. Texto com sinal forte decide (o conteúdo do edital importa mais que a interface do site)
    if idioma_texto and total >= 15:
        return idioma_texto

    # 2. Atributo lang do <html> (OJS sempre preenche: "pt-BR", "es-ES", "en-US"...)
    html = sopa.find("html")
    if html:
        lang = (html.get("lang") or html.get("xml:lang") or "").strip().lower()
        if lang:
            return lang.split("-")[0].split("_")[0]

    # 3. Texto com sinal fraco
    if idioma_texto:
        return idioma_texto

    # 4. Domínio
    dominio = urlparse(url).netloc.lower()
    for sufixo, idioma in DOMINIOS_IDIOMA.items():
        if dominio.endswith(sufixo):
            return idioma

    return "pt"


# =====================================================
# METADADOS OJS
# =====================================================
def extrair_metadados_ojs(sopa):
    """Extrai, de uma vez só, as meta tags padrão do OJS (fonte mais confiável de dados)."""
    dados = {}

    campos = {
        "citation_journal_title": "revista",
        "citation_title": "tema",
        "citation_issn": "issn"
    }

    for campo, nome in campos.items():
        meta = sopa.find("meta", attrs={"name": campo})
        if meta and meta.get("content"):
            dados[nome] = meta["content"].strip()

    return dados


def extrair_qualis(texto):

    texto_upper = texto.upper()

    for match_qualis in re.finditer(r'QUALIS', texto_upper):
        inicio = match_qualis.start()
        janela = texto_upper[inicio:inicio + 150]
        m = re.search(r'\b([AB][1-4]|C)\b', janela)
        if m:
            return m.group(1)

    return "-"


def extrair_issn(texto, sopa, metadados_ojs=None):

    # 1. Fonte mais confiável: metadados OJS já extraídos uma vez no início do processamento
    if metadados_ojs and metadados_ojs.get("issn"):
        return metadados_ojs["issn"]

    metas = [
        "citation_issn",
        "dc.identifier.issn",
        "DC.Identifier.ISSN",
        "prism.issn",
        "prism.eIssn",
    ]

    for meta_name in metas:
        meta = sopa.find("meta", attrs={"name": meta_name})
        if meta and meta.get("content"):
            return meta["content"].strip()

    padroes = [
        r'e-?ISSN[:\s]*([\d]{4}-[\dX]{4})',
        r'ISSN-L[:\s]*([\d]{4}-[\dX]{4})',
        # PT / ES / EN: impresso/impreso/print, online/en línea/electronic...
        r'ISSN\s*(?:\(?\s*(?:atual|antigo|impress[oa]|impres[oa]|print|online|en\s+l[íi]nea|'
        r'eletr[oô]nic[oa]|electr[oó]nic[oa]|electronic|digital)\s*\)?)?\s*[:\s]*([\d]{4}-[\dX]{4})',
    ]

    for padrao in padroes:
        match = re.search(padrao, texto, re.IGNORECASE)
        if match:
            if match.groups():
                return match.group(1)

    return "-"


# =====================================================
# DATAS (PT / ES / EN)
# =====================================================
MESES_COMPLETOS = {
    # PT
    'janeiro': 1, 'fevereiro': 2, 'marco': 3, 'abril': 4, 'maio': 5, 'junho': 6,
    'julho': 7, 'agosto': 8, 'setembro': 9, 'outubro': 10, 'novembro': 11, 'dezembro': 12,
    # ES
    'enero': 1, 'febrero': 2, 'marzo': 3, 'mayo': 5, 'junio': 6, 'julio': 7,
    'septiembre': 9, 'setiembre': 9, 'octubre': 10, 'noviembre': 11, 'diciembre': 12,
    # EN
    'january': 1, 'february': 2, 'march': 3, 'april': 4, 'may': 5, 'june': 6,
    'july': 7, 'august': 8, 'september': 9, 'october': 10, 'november': 11, 'december': 12,
}

# Abreviações de 3 letras (cobre PT, ES e EN): jan/ene, fev/feb, abr/apr, mai/may, ago/aug, set/sep, out/oct, dez/dic/dec
MESES_ABREV = {
    'jan': 1, 'ene': 1, 'fev': 2, 'feb': 2, 'mar': 3, 'abr': 4, 'apr': 4,
    'mai': 5, 'may': 5, 'jun': 6, 'jul': 7, 'ago': 8, 'aug': 8,
    'set': 9, 'sep': 9, 'out': 10, 'oct': 10, 'nov': 11,
    'dez': 12, 'dic': 12, 'dec': 12,
}


def mes_para_numero(nome):
    n = sem_acento(nome.lower().strip(" .,"))
    if n in MESES_COMPLETOS:
        return MESES_COMPLETOS[n]
    # Só aceita abreviação se a palavra for curta ("Sept", "ago."), para não confundir com "mayor", "marco" etc.
    if len(n) <= 4 and n[:3] in MESES_ABREV:
        return MESES_ABREV[n[:3]]
    return None


def montar_data(dia, mes, ano):
    try:
        return datetime(int(ano), int(mes), int(dia)).strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        return None


_MES = r'[A-Za-zÀ-ÿ]{3,10}\.?'
_ORD = r'(?:st|nd|rd|th|º|°|ª)?'

# Regex usada para LOCALIZAR datas no texto (PT, ES e EN)
PADRAO_DATA = (
    r'\d{4}\s*-\s*\d{1,2}\s*-\s*\d{1,2}'                                   # 2026-09-30
    r'|\d{1,2}\s*[/.-]\s*\d{1,2}\s*[/.-]\s*\d{4}'                          # 30/09/2026, 30.09.2026
    rf'|\d{{1,2}}{_ORD}\s+(?:de\s+|of\s+)?{_MES},?\s+(?:de\s+|del\s+)?\d{{4}}'  # 30 de septiembre de 2026 / 30th of September, 2026
    rf'|{_MES}\s+\d{{1,2}}{_ORD},?\s+\d{{4}}'                               # September 30, 2026
)


def limpar_data(data_string, idioma="pt"):
    """
    Converte uma data em PT, ES ou EN para dd/mm/aaaa.
    Em datas numéricas ambíguas (ex.: 05/06/2026), usa dd/mm para pt/es e mm/dd para en.
    """
    if not data_string or data_string.strip() == "-":
        return "-"

    s = data_string.strip()
    # \b evita cortar o "O" de "October" ou o "de" de "december"
    s = re.sub(r'^\s*(?:at[ée]|hasta(?:\s+el)?|para|by|until|on|de|el|o|dia)\b\s*', '', s, flags=re.IGNORECASE)
    s = re.sub(r'(\d{1,2})(?:st|nd|rd|th|º|°|ª)', r'\1', s)   # 30th -> 30
    s = re.sub(r'\bof\b', ' ', s, flags=re.IGNORECASE)
    s = re.sub(r'\s+', ' ', s).strip(" :;-.,")

    # 1. ISO: 2026-09-30
    m = re.search(r'(\d{4})\s*-\s*(\d{1,2})\s*-\s*(\d{1,2})', s)
    if m:
        data = montar_data(m.group(3), m.group(2), m.group(1))
        if data:
            return data

    # 2. "30 de septiembre de 2026" / "30 de janeiro de 2025" / "30 September, 2026"
    m = re.search(r'(\d{1,2})\s+(?:de\s+)?([A-Za-zÀ-ÿ]+\.?),?\s+(?:de\s+|del\s+)?(\d{4})', s, re.IGNORECASE)
    if m:
        mes = mes_para_numero(m.group(2))
        if mes:
            data = montar_data(m.group(1), mes, m.group(3))
            if data:
                return data

    # 3. "September 30, 2026" / "Sept. 30 2026"
    m = re.search(r'([A-Za-zÀ-ÿ]+\.?)\s+(\d{1,2}),?\s+(\d{4})', s, re.IGNORECASE)
    if m:
        mes = mes_para_numero(m.group(1))
        if mes:
            data = montar_data(m.group(2), mes, m.group(3))
            if data:
                return data

    # 4. Numérico: 30/09/2026, 30-09-2026, 30.09.2026 (ou 09/30/2026 em inglês)
    m = re.search(r'(\d{1,2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{4})', s)
    if m:
        a, b, ano = int(m.group(1)), int(m.group(2)), m.group(3)
        if a > 12:
            ordens = [(a, b)]            # só pode ser dd/mm
        elif b > 12:
            ordens = [(b, a)]            # só pode ser mm/dd
        elif idioma == "en":
            ordens = [(b, a), (a, b)]    # ambíguo: padrão americano primeiro
        else:
            ordens = [(a, b), (b, a)]    # ambíguo: padrão latino primeiro
        for dia, mes in ordens:
            data = montar_data(dia, mes, ano)
            if data:
                return data

    return "-"


PALAVRAS_CHAVE_CONTEXTO = [
    # PT
    'submiss', 'envio', 'prazo', 'recebimento', 'recepção de artigos', 'data limite',
    # ES
    'fecha límite', 'fecha limite', 'plazo', 'recepción', 'recepcion', 'envío', 'cierre',
    'convocatoria', 'hasta el',
    # EN
    'deadline', 'call for papers', 'submission', 'submit', 'due', 'closing',
]


def extrair_data_contextual(texto_pagina, idioma="pt"):

    # Quebra por fim de frase (ponto seguido de maiúscula), sem quebrar "Sept. 30" ou "30.09.2026"
    frases = re.split(r'\.\s+(?=[A-ZÀ-Ý])', texto_pagina)

    for frase in frases:
        frase_lower = frase.lower()

        if any(p in frase_lower for p in PALAVRAS_CHAVE_CONTEXTO):
            for match in re.finditer(PADRAO_DATA, frase, re.IGNORECASE):
                data_limpa = limpar_data(match.group(0), idioma)
                if data_limpa != "-":
                    print(f"   Data contextual encontrada: {data_limpa}")
                    return data_limpa

    return "-"


# Frases-chave em ordem de prioridade: as mais específicas primeiro, as genéricas no fim.
FRASES_PRAZO = [
    # PT
    r'prazo\s+final(?:\s+para)?\s+(?:a\s+)?(?:submiss[ãa]o|envio)',
    r'data\s+(?:limite|final)\s+(?:para\s+)?(?:a\s+)?(?:submiss[ãa]o|envio)',
    r'prazo\s+(?:para\s+|de\s+)?(?:submiss[ãa]o|submiss[õo]es|envio)',
    # ES
    r'fecha\s+l[íi]mite(?:\s+(?:de|para)\s+(?:(?:el|la)\s+)?(?:env[íi]o|recepci[óo]n|presentaci[óo]n|postulaci[óo]n))?',
    r'plazo\s+(?:de|para)\s+(?:(?:el|la)\s+)?(?:env[íi]o|recepci[óo]n|presentaci[óo]n|postulaci[óo]n)',
    r'cierre\s+de\s+(?:la\s+)?convocatoria',
    r'recepci[óo]n\s+de\s+(?:art[íi]culos|manuscritos|originales|trabajos)(?:\s+hasta)?',
    # EN
    r'submission\s+deadline',
    r'deadline\s+for\s+(?:full\s+)?(?:submissions?|manuscripts?|papers?|articles?)',
    r'(?:full\s+)?(?:papers?|manuscripts?|submissions?|articles?)\s+(?:are\s+)?due',
    r'submissions?\s+(?:close|closes|are\s+open\s+until|accepted\s+until|open\s+until)',
    r'closing\s+date',
    r'due\s+date',
    # Genéricas (qualquer idioma)
    r'submiss[ãa]o\s+(?:at[ée]|de\s+trabalhos|de\s+artigos)?',
    r'deadline',
    r'plazo',
]


def extrair_data_submissao_especifica(sopa, texto_pagina, url, idioma="pt"):

    for frase in FRASES_PRAZO:
        for match_frase in re.finditer(frase, texto_pagina, re.IGNORECASE):
            inicio = match_frase.end()
            janela = texto_pagina[inicio:inicio + 60]

            # Testa todas as datas da janela (a primeira pode ser falso positivo, ex. "Volume 12 2026")
            for match_data in re.finditer(PADRAO_DATA, janela, re.IGNORECASE):
                data_limpa = limpar_data(match_data.group(0), idioma)
                if data_limpa != "-":
                    print(f"   Data encontrada ('{frase}'): {match_data.group(0)} -> {data_limpa}")
                    return data_limpa

    return extrair_data_contextual(texto_pagina, idioma)


# =====================================================
# NOME DA REVISTA
# =====================================================
def extrair_nome_revista_simplificado(sopa, url, metadados_ojs=None):

    # 1. Fonte mais confiável: metadados OJS já extraídos uma vez no início do processamento
    if metadados_ojs and metadados_ojs.get("revista"):
        nome = metadados_ojs["revista"]
        if len(nome) > 3:
            return nome

    meta_prioritarias = [
        "citation_journal_title",
        "dc.title"
    ]

    for meta_name in meta_prioritarias:
        meta = sopa.find("meta", attrs={"name": meta_name})
        if meta and meta.get("content"):
            nome = meta["content"].strip()
            if len(nome) > 3:
                return nome

    # 1b. Título da página
    if sopa.title:
        title_text = sopa.title.get_text(strip=True)
        if "|" in title_text:
            partes = title_text.split("|")
            for parte in reversed(partes):
                parte_limpa = parte.strip()
                if parte_limpa and len(parte_limpa) > 3:
                    return parte_limpa
        elif title_text:
            return title_text

    # 2. Meta tags
    meta_tags = [
        {"name": "citation_journal_title"},
        {"property": "og:site_name"},
        {"name": "dc.title"},
    ]

    for meta in meta_tags:
        tag = sopa.find("meta", attrs=meta)
        if tag and tag.get("content"):
            conteudo = tag["content"].strip()
            if conteudo and len(conteudo) > 3:
                return conteudo

    # 3. H1 principal
    h1 = sopa.find("h1")
    if h1:
        texto = h1.get_text(strip=True)
        termos_navegacao = [
            # PT
            'anúncios', 'notícias', 'avisos', 'acesso', 'login', 'cadastro',
            'buscar', 'pesquisa', 'informações', 'sobre',
            # ES
            'avisos', 'noticias', 'anuncios', 'acceso', 'registro', 'buscar',
            'búsqueda', 'información', 'acerca de',
            # EN
            'announcement', 'announcements', 'news', 'register', 'search', 'about',
        ]
        if len(texto) > 5 and texto.lower().strip() not in termos_navegacao:
            return texto

    # 4. Do URL
    try:
        parsed_url = urlparse(url)
        dominio = parsed_url.netloc

        if "periodicos.uff.br" in dominio:
            return "ZIZ - Revista Discente de Ciência Política"
        elif "periodicos.ufop.br" in dominio:
            return "Caletroscópio"
        elif "ojs.uel.br" in dominio and "boitata" in url:
            return "Boitatá"
        elif "periodicos.pucpr.br" in dominio and "aurora" in url:
            return "Revista de Filosofia Aurora"
        elif "e-publicacoes.uerj.br" in dominio and "intellectus" in url:
            return "Intellèctus"
        elif "periodicos.unir.br" in dominio:
            return "RE-UNIR - Revista do Centro de Estudos da Linguagem da Universidade Federal de Rondônia"
        elif "periodicos.ufs.br" in dominio and "tomo" in url:
            return "Revista TOMO"
        elif "educacaobasicaemfoco.net.br" in dominio:
            return "Revista Educação Básica em Foco"
        elif "e-publicacoes.uerj.br" in dominio and "pensaresemrevista" in url:
            return "Pensares em Revista"
        elif "periodicos.ufpe.br" in dominio and "debatesinsubmissos" in url:
            return "Revista Debates Insubmissos"
        elif "seer.ufrgs.br" in dominio and "iluminuras" in url:
            return "ILUMINURAS"
        elif "e-publicacoes.uerj.br" in dominio and "cadernoseminal" in url:
            return "Caderno Seminal"
        elif "sites.google.com" in dominio and "fulordemulungu" in url:
            return "Fulor de Mulungu"
        elif "periodicos.ufac.br" in dominio and "COMMUNITAS" in url:
            return "Communitas"
        elif "periodicos.pucminas.br" in dominio and "estudosinternacionais" in url:
            return "Estudos Internacionais: revista de relações internacionais da PUC Minas"
        elif "e-publicacoes.uerj.br" in dominio and "palimpsesto" in url:
            return "Palimpsesto - Revista do Programa de Pós-Graduação em Letras da UERJ"
    except:
        pass

    return "-"


# =====================================================
# MÚLTIPLAS CHAMADAS NA MESMA PÁGINA
# =====================================================
def extrair_multiplas_chamadas(texto, idioma="pt"):

    chamadas = []
    texto_norm = re.sub(r'\s+', ' ', texto)

    def adicionar(tema, data_bruta, tamanho_minimo=0):
        tema = tema.strip()
        data = limpar_data(data_bruta.strip(), idioma)
        if data != "-" and len(tema) > tamanho_minimo:
            chamadas.append({"tema": tema, "data": data})

    # ---------- PT ----------
    # Padrão 1: "Dossiê <título> Organização(es/as): <...> Prazo (variações): <data>"
    padrao1 = (
        r'Dossiê\s+'
        r'(.+?)'
        r'\s+Organi[zs]a[çc][ãa]o(?:res|ras)?:'
        r'.*?'
        r'Prazo(?:\s+(?:para|de)\s+(?:envio(?:\s+dos\s+artigos)?|submiss[ãa]o))?\s*:\s*'
        r'([^\.]+)'
    )

    for match in re.finditer(padrao1, texto_norm, re.IGNORECASE | re.DOTALL):
        tema = re.split(r'\d{4}-\d{2}-\d{2}', match.group(1).strip())[0]
        adicionar(tema, match.group(2))

    if chamadas:
        return chamadas

    # Padrão 2: lista numerada "- 42 (maio-ago 2026) - "título" - Chamada: dd/mm/aaaa a dd/mm/aaaa"
    padrao2 = (
        r'[-–]\s*\d+\s*\([^)]*\)\s*[-–]\s*'
        r'[“"]([^”"]+)[”"]'
        r'\s*[-–]\s*Chamada:\s*'
        r'\d{1,2}/\d{1,2}/\d{4}\s*a\s*'
        r'(\d{1,2}/\d{1,2}/\d{4})'
    )

    for match in re.finditer(padrao2, texto_norm, re.IGNORECASE):
        adicionar(match.group(1), match.group(2))

    if chamadas:
        return chamadas

    # Padrão 3: "EDIÇÃO <período> – a ser publicada... <TEMA> Doutor(a)... Envio de artigos até <data>."
    padrao3 = (
        r'a ser publicada\s+no\s+m[êe]s\s+(?:de\s+)?[a-zçã]+\s+de\s+\d{4}\s+'
        r'(.+?)\s+'
        r'(?:Doutor|Doutora|Doutorando|Doutoranda|Mestr|Especialista)'
        r'.*?'
        r'Envio de artigos at[ée]\s*(\d{1,2}/\d{1,2}/\d{4})'
    )

    for match in re.finditer(padrao3, texto_norm, re.IGNORECASE | re.DOTALL):
        adicionar(match.group(1), match.group(2), tamanho_minimo=10)

    if chamadas:
        return chamadas

    # Padrão 4: Dossiê "título" ... Prazo (para envio dos artigos/submissão): dd-mm-aaaa
    padrao4 = (
        r'Dossiê\s*["“]([^"”]+)["”]'
        r'.*?'
        r'Prazo\s+(?:para\s+envio\s+dos\s+artigos|para\s+submiss[ãa]o|de\s+submiss[ãa]o)?\s*:\s*'
        r'(\d{1,2}[-/]\d{1,2}[-/]\d{4})'
    )

    for match in re.finditer(padrao4, texto_norm, re.IGNORECASE | re.DOTALL):
        adicionar(match.group(1), match.group(2), tamanho_minimo=10)

    if chamadas:
        return chamadas

    # ---------- ES ----------
    # Padrão 5: Dossier/Monográfico "título" ... Fecha límite / Plazo ...: <data>
    # (.{0,600}? limita a distância entre título e prazo, para não casar título de uma chamada com prazo da próxima)
    padrao5 = (
        r'(?:Dossier|Monogr[áa]fico|N[úu]mero\s+especial)\s*[:\-–]?\s*["“«]([^"”»]+)["”»]'
        r'.{0,600}?'
        r'(?:Fecha\s+l[íi]mite|Plazo|Cierre)[^:]{0,40}:\s*'
        rf'({PADRAO_DATA})'
    )

    for match in re.finditer(padrao5, texto_norm, re.IGNORECASE | re.DOTALL):
        adicionar(match.group(1), match.group(2), tamanho_minimo=10)

    if chamadas:
        return chamadas

    # ---------- EN ----------
    # Padrão 6: Special Issue (on) "title" ... Deadline / Due ...: <date>
    padrao6 = (
        r'(?:Special\s+Issue|Thematic\s+(?:Issue|Section)|Special\s+Section)\s*(?:on)?\s*[:\-–]?\s*["“]([^"”]+)["”]'
        r'.{0,600}?'
        r'(?:Deadline|Due|Closing\s+date)[^:]{0,40}:\s*'
        rf'({PADRAO_DATA})'
    )

    for match in re.finditer(padrao6, texto_norm, re.IGNORECASE | re.DOTALL):
        adicionar(match.group(1), match.group(2), tamanho_minimo=10)

    return chamadas


# =====================================================
# TEMA
# =====================================================
PREFIXOS_GENERICOS = [
    # PT
    r'^chamada\s+de\s+artigos\s*[-:–]\s*',
    r'^chamada\s*[-:–]\s*',
    # ES
    r'^convocatoria(?:\s+(?:de|para)\s+(?:art[íi]culos|trabajos|manuscritos))?\s*[-:–]\s*',
    # EN
    r'^call\s+for\s+(?:papers|articles|submissions)\s*[-:–]\s*',
    r'^cfp\s*[-:–]\s*',
]


def limpar_prefixo_generico(titulo):
    t = titulo.strip()
    for padrao in PREFIXOS_GENERICOS:
        t = re.sub(padrao, '', t, flags=re.IGNORECASE).strip()
    return t


MARCADORES_FIM_CONTEUDO = [
    # PT
    r'\bSaiba mais\b',
    r'\bEnviar Submiss[ãa]o\b',
    r'\bIdioma\b',
    r'\bM[ée]tricas\b',
    r'\bIndexadores\b',
    r'\bPalavras-chave\b',
    r'\bDiret[óo]rios\b',
    # ES
    r'\bLeer m[áa]s\b',
    r'\bEnviar (?:un )?(?:art[íi]culo|env[íi]o)\b',
    r'\bPalabras clave\b',
    r'\bIndexaci[óo]n\b',
    # EN
    r'\bRead more\b',
    r'\bMake a Submission\b',
    r'\bMetrics\b',
    r'\bIndexing\b',
    # "Language" e "Keywords" ficaram de fora de propósito: aparecem em títulos reais
    # de dossiês ("Language and Power"...) e cortariam o tema no meio.
]


def cortar_lixo_final(texto):
    for marcador in MARCADORES_FIM_CONTEUDO:
        match = re.search(marcador, texto, re.IGNORECASE)
        if match:
            texto = texto[:match.start()].strip(" -:;.,")
    return texto


# Onde o tema termina e começa a parte de prazos/instruções
CORTE_INSTRUCOES = r'\b(?:deadline|submission\s+deadline|due\s+date|plazo|fecha\s+l[íi]mite|prazo)\b'


def extrair_tema_avancado(sopa, texto_pagina, metadados_ojs=None):

    # 1. Fonte mais confiável: metadados OJS já extraídos uma vez no início do processamento
    titulo_ojs = metadados_ojs.get("tema") if metadados_ojs else None
    meta_content = titulo_ojs

    if not meta_content:
        meta = sopa.find("meta", attrs={"name": "citation_title"})
        if meta and meta.get("content"):
            meta_content = meta["content"].strip()

    if meta_content:
        titulo = limpar_prefixo_generico(meta_content)

        termos_genericos = [
            # PT
            "resultado", "edital", "revista", "dossiês temáticos", "dossies tematicos",
            # ES
            "convocatoria", "convocatorias", "anuncios", "noticias", "avisos",
            # EN
            "announcement", "announcements", "calls", "call for papers", "news",
        ]

        if (
            len(titulo) > 15
            and not any(t == titulo.lower().strip() for t in termos_genericos)
        ):
            return cortar_lixo_final(titulo)

    # 2. Padrões específicos
    padroes = [
        # PT
        r'Dossiê\s+Temático\s+n?\.?\s*\d+\s*[-:]\s*(.+?)(?:\d{4}-\d{2}-\d{2}|$)',
        r'["“«]([^"”»]{30,300})["”»]',
        r'Temática:\s*"([^"]+)"',
        r'Tema:\s*"([^"]+)"',
        r'Dossiê:\s*"([^"]+)"',
        r'Dossiê\s*n\.\s*\d+[^:]*:\s*[“"]([^”"]+)[”"]',
        r'CHAMADA\s+DE\s+ARTIGOS\s+PARA\s+DOSSIÊ[:\s]+([^\n]{20,500})',
        r'Chamada\s+para\s+Dossiê\s+([^\.]{20,300})',
        r'Número\s+Especial[:\s]+([^\.]{20,300})',
        # ES
        r'(?:Dossier|Monogr[áa]fico)\s*(?:tem[áa]tico)?\s*[:\-–]?\s*["“«]([^"”»]{15,300})["”»]',
        r'Convocatoria\s+(?:para\s+(?:el\s+)?)?(?:dossier|monogr[áa]fico|n[úu]mero\s+especial)\s*[:\-–]?\s*([^\.]{20,300})',
        r'(?:Tem[áa]tica|Tema)\s*:\s*["“«]?([^"”»\.]{15,300})',
        # EN
        r'Special\s+Issue\s*(?:on)?\s*[:\-–]?\s*["“]([^"”]{15,300})["”]',
        r'Call\s+for\s+(?:Papers|Articles|Submissions)\s*[:\-–]\s*([^\.]{20,300})',
        r'Thematic\s+(?:Issue|Section)\s*[:\-–]\s*([^\.]{20,300})',
        r'Special\s+Issue[:\s]+([^\.]{20,300})',
    ]

    lixo = [
        "started at", "calls were launched", "average of",
        "journal", "submission", "editorial", "volume", "issue",
        "revista", "envío de", "envio de",
    ]

    for padrao in padroes:
        for match in re.finditer(padrao, texto_pagina, re.IGNORECASE):
            tema = re.sub(r'\s+', ' ', match.group(1)).strip()
            # Remove a parte de instruções que às vezes vem colada ("... Deadline: 30/09")
            tema = re.split(CORTE_INSTRUCOES, tema, flags=re.IGNORECASE)[0].strip(" -:;.,")
            if len(tema) <= 10:
                continue
            if any(x in tema.lower() for x in lixo):
                continue
            return tema

    # 3. Fallback via <title> da página
    if sopa.title:
        title_text = sopa.title.get_text(" ", strip=True)
        if "|" in title_text:
            candidato = title_text.split("|")[0].strip()
            candidato = limpar_prefixo_generico(candidato)
            if len(candidato) > 10:
                return cortar_lixo_final(candidato)

    # 4. Busca em títulos H1/H2/H3
    titulos = sopa.find_all(['h1', 'h2', 'h3'])
    candidatos = []

    palavras = [
        # PT
        'dossiê', 'dossie', 'edição especial', 'número especial', 'chamada',
        # ES
        'dossier', 'monográfico', 'monografico', 'convocatoria', 'numero especial',
        # EN
        'call for papers', 'special issue', 'thematic issue', 'special section',
    ]

    for titulo in titulos:
        texto = titulo.get_text(" ", strip=True)
        texto_lower = texto.lower()

        if any(p in texto_lower for p in palavras):
            texto_limpo = limpar_prefixo_generico(texto)
            if len(texto_limpo) > 15:
                candidatos.append(texto_limpo)

    if candidatos:
        candidatos.sort(key=len, reverse=True)
        return cortar_lixo_final(candidatos[0])

    print("\n[SEM TEMA]")
    print(texto_pagina[:1000])
    print("\n")

    return "-"


def calcular_status(revista, tema, data, qualis, issn, idioma="pt"):
    """
    Critério único de OK/PARCIAL.
    Qualis só é exigido para revistas em português: páginas estrangeiras
    praticamente nunca citam o Qualis (que é uma classificação da CAPES).
    """
    obrigatorios = [revista, tema, data, issn]
    if idioma == "pt":
        obrigatorios.append(qualis)

    if all(campo != "-" for campo in obrigatorios):
        return "OK"
    return "PARCIAL"


# =====================================================
# PROCESSAMENTO PRINCIPAL
# =====================================================
PADROES_BLOQUEIO = [
    r'certificando de que voc[êe] n[ãa]o [ée] um bot',
    r'anubis',
    r'checking your browser',
    r'verificando (?:su|el) navegador',
    r'this site uses anubis',
    r'ce site utilise anubis',
    r'just a moment',
]


def linha_vazia(link, status="PARCIAL", idioma="-"):
    return ["-", "-", "-", "-", "-", link, status, idioma]


def processar_links(links, tabela_referencia=None):
    """Processa uma lista de links"""

    dados_saida = []

    for i, link in enumerate(links, 1):
        # Pausa entre requisições (no início do laço, para valer também quando há 'continue')
        if i > 1:
            time.sleep(1.5)

        print(f"\n[{i}/{len(links)}] Processando: {link}")

        try:
            r = requisitar_com_retry(link)

            if r.status_code != 200:
                print(f"   ERRO {r.status_code}")
                dados_saida.append(linha_vazia(link))
                continue

            sopa = BeautifulSoup(r.content, 'html.parser', from_encoding='utf-8')
            for tag in sopa(['script', 'style', 'nav', 'footer', 'header', 'noscript', 'iframe']):
                tag.decompose()

            texto_pagina = sopa.get_text(" ", strip=True)

            if len(texto_pagina) < 100:
                print("   CONTEÚDO INSUFICIENTE")
                dados_saida.append(linha_vazia(link))
                continue

            texto_lower_check = texto_pagina.lower()
            if any(re.search(p, texto_lower_check) for p in PADROES_BLOQUEIO):
                print("   BLOQUEADO POR ANTI-BOT")
                dados_saida.append(linha_vazia(link))
                continue

            idioma = detectar_idioma(sopa, texto_pagina, link)
            print(f"   Idioma detectado: {idioma}")

            if idioma not in IDIOMAS_SUPORTADOS:
                print("   FORA DO ESCOPO (idioma não suportado)")
                dados_saida.append(linha_vazia(link, "FORA_DO_ESCOPO", idioma))
                continue

            # Extrai os metadados OJS uma única vez, e reaproveita nas funções abaixo
            metadados_ojs = extrair_metadados_ojs(sopa)

            revista = extrair_nome_revista_simplificado(sopa, link, metadados_ojs)
            issn = extrair_issn(texto_pagina, sopa, metadados_ojs)
            qualis = extrair_qualis(texto_pagina)
            revista, qualis, issn = complementar_com_referencia(revista, qualis, issn, tabela_referencia)

            chamadas = extrair_multiplas_chamadas(texto_pagina, idioma)

            if chamadas:
                for chamada in chamadas:
                    status = calcular_status(revista, chamada["tema"], chamada["data"], qualis, issn, idioma)
                    dados_saida.append([
                        chamada["data"], revista, chamada["tema"],
                        qualis, issn, link, status, idioma
                    ])
                continue

            data_submissao = extrair_data_submissao_especifica(sopa, texto_pagina, link, idioma)
            tema = extrair_tema_avancado(sopa, texto_pagina, metadados_ojs)

            status = calcular_status(revista, tema, data_submissao, qualis, issn, idioma)

            dados_saida.append([
                data_submissao, revista, tema,
                qualis, issn, link, status, idioma
            ])

        except requests.exceptions.Timeout:
            print("   TIMEOUT")
            dados_saida.append(linha_vazia(link))
        except Exception as e:
            print(f"  ERRO: {str(e)[:80]}")
            dados_saida.append(linha_vazia(link))

    return dados_saida


def formatar_planilha(saida, total_linhas):
    """Aplica cores condicionais, cabeçalho em negrito e congela a primeira linha."""

    sheet_id = saida.id

    def regra_cor(status, cor, indice):
        return {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{
                        "sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": total_linhas + 1,
                        "startColumnIndex": 0, "endColumnIndex": NUM_COLUNAS
                    }],
                    "booleanRule": {
                        "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": f'=$G2="{status}"'}]},
                        "format": {"backgroundColor": cor}
                    }
                },
                "index": indice
            }
        }

    requests_formatacao = [
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.85, "green": 0.85, "blue": 0.85},
                        "textFormat": {"bold": True}
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat)"
            }
        },
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": 1}
                },
                "fields": "gridProperties.frozenRowCount"
            }
        },
        regra_cor("OK", {"red": 0.80, "green": 0.94, "blue": 0.80}, 0),
        regra_cor("PARCIAL", {"red": 1.0, "green": 0.95, "blue": 0.70}, 1),
        regra_cor("FORA_DO_ESCOPO", {"red": 0.90, "green": 0.90, "blue": 0.90}, 2),
        {
            "autoResizeDimensions": {
                "dimensions": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": NUM_COLUNAS}
            }
        }
    ]

    saida.spreadsheet.batch_update({"requests": requests_formatacao})


# FUNÇÃO PRINCIPAL
def main():

    SCOPES = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]

    credenciais_env = os.environ.get("GOOGLE_CREDENTIALS")

    if credenciais_env:
        info = json.loads(credenciais_env)
        credentials = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        credentials = Credentials.from_service_account_file(
            "automacaoeditais-8f0db36dc8e9.json",
            scopes=SCOPES
        )

    gc = gspread.authorize(credentials)

    sh = gc.open_by_key("1HeDMxxmC9xhjta6IjSJ8FLj9mBc77aK0P4NAS5XIJ2I")
    entrada = sh.worksheet("Entrada")
    saida = sh.worksheet("Saída")

    links = entrada.col_values(1)[1:100]

    links_originais = len(links)
    links = list(dict.fromkeys(links))
    duplicatas_removidas = links_originais - len(links)

    if duplicatas_removidas > 0:
        print(f"AVISO: {duplicatas_removidas} link(s) duplicado(s) removido(s) da lista de entrada.")

    print("=" * 80)
    print("INICIANDO PROCESSAMENTO DE DATAS DE SUBMISSÃO")
    print("=" * 80)

    dados_ok_existentes = carregar_dados_existentes(saida)

    tabela_referencia = carregar_tabela_referencia(sh)

    dados_processados = processar_links(links, tabela_referencia)

    for i, linha in enumerate(dados_processados):
        link = linha[5]
        tema_novo = linha[2]

        if link in dados_ok_existentes:
            candidatas = dados_ok_existentes[link]
            match = next((c for c in candidatas if c[2].strip() == tema_novo.strip()), None)

            if not match and len(candidatas) == 1:
                match = candidatas[0]

            if match:
                match = list(match)
                if not match[7]:          # linha antiga, sem idioma: usa o detectado agora
                    match[7] = linha[7]
                dados_processados[i] = match

    ordem_status = {"OK": 0, "PARCIAL": 1, "FORA_DO_ESCOPO": 2}
    dados_processados.sort(key=lambda linha: ordem_status.get(linha[6], 99))

    saida.clear()

    saida.append_row([
        "DATA DE SUBMISSÃO",
        "REVISTA",
        "TEMA",
        "QUALIS",
        "ISSN/ISBN",
        "LINK PARA ACESSO",
        "STATUS",
        "IDIOMA",
    ])

    if dados_processados:
        saida.update(range_name=f"A2:H{len(dados_processados)+1}", values=dados_processados)
        formatar_planilha(saida, len(dados_processados))

    print("=" * 80)
    print(f"\nPROCESSAMENTO CONCLUIDO!")
    print(f"Links processados: {len(dados_processados)}")

    contagem_status = {s: sum(1 for l in dados_processados if l[6] == s) for s in ordem_status}
    print(f"OK: {contagem_status['OK']}")
    print(f"PARCIAL: {contagem_status['PARCIAL']}")
    print(f"FORA DO ESCOPO (idioma não suportado): {contagem_status['FORA_DO_ESCOPO']}")

    contagem_idioma = {}
    for l in dados_processados:
        contagem_idioma[l[7]] = contagem_idioma.get(l[7], 0) + 1
    print("Por idioma: " + ", ".join(f"{k}={v}" for k, v in sorted(contagem_idioma.items())))


# EXECUÇÃO
if __name__ == "__main__":
    main()