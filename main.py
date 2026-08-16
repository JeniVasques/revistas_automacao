import requests
import gspread
import re
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from google.oauth2.service_account import Credentials
from datetime import datetime


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
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8"
})


def carregar_dados_existentes(saida):
    """Lê a planilha atual e retorna {link: [linha1, linha2, ...]} das linhas já OK."""
    valores = saida.get_all_values()

    dados_ok = {}
    for linha in valores[1:]:  # pula o cabeçalho
        if len(linha) >= 7 and linha[6].strip().upper() == "OK":
            link = linha[5]
            dados_ok.setdefault(link, []).append(linha[:7])

    return dados_ok

def extrair_qualis(texto):

    texto_upper = texto.upper()

    for match_qualis in re.finditer(r'QUALIS', texto_upper):
        inicio = match_qualis.start()
        janela = texto_upper[inicio:inicio + 60]
        m = re.search(r'\b([ABC][1-4]|C)\b', janela)
        if m:
            return m.group(1)

    return "-"

def extrair_issn(texto, sopa):
    metas = [
        "citation_issn",
        "dc.identifier.issn",
        "DC.Identifier.ISSN"
    ]

    for meta_name in metas:
        meta = sopa.find("meta", attrs={"name": meta_name})
        if meta and meta.get("content"):
            return meta["content"].strip()

    padroes = [
        r'e-?ISSN[:\s]*([\d]{4}-[\dX]{4})',
        r'ISSN\s*(?:atual|antigo|impress[oa]|online|eletr[oô]nico)?\s*[:\s]*([\d]{4}-[\dX]{4})',    
    ]

    for padrao in padroes:
        match = re.search(padrao, texto, re.IGNORECASE)
        if match:
            if match.groups():
                return match.group(1)
            #return match.group(0)
    return "-"

def limpar_data(data_string):

    if not data_string or data_string == "-":
        return "-"

    data_string = re.sub(r'^\s*(at[éeé]|para|de|o|dia|dia\s+)\s*', '', data_string, flags=re.IGNORECASE)
    data_string = data_string.strip(" :;-.,")

    meses_pt = {
        'janeiro': '01', 'fevereiro': '02', 'março': '03', 'marco': '03',
        'abril': '04', 'maio': '05', 'junho': '06',
        'julho': '07', 'agosto': '08', 'setembro': '09',
        'outubro': '10', 'novembro': '11', 'dezembro': '12'
    }

    # NOVO: tenta primeiro data por extenso, tipo "30 de janeiro de 2025"
    match_extenso = re.search(
        r'(\d{1,2})\s+de\s+([a-zçã]+)\s+de\s+(\d{4})',
        data_string,
        re.IGNORECASE
    )
    if match_extenso:
        dia, mes_nome, ano = match_extenso.groups()
        mes_num = meses_pt.get(mes_nome.lower())
        if mes_num:
            try:
                data_obj = datetime.strptime(f"{int(dia):02d}/{mes_num}/{ano}", "%d/%m/%Y")
                return data_obj.strftime("%d/%m/%Y")
            except:
                pass

    # Formatos numéricos (como já estava)
    formatos_numericos = [
        (r'(\d{1,2})/(\d{1,2})/(\d{4})', "%d/%m/%Y"),
        (r'(\d{1,2})-(\d{1,2})-(\d{4})', "%d-%m-%Y"),
        (r'(\d{4})-(\d{1,2})-(\d{1,2})', "%Y-%m-%d"),
    ]

    for padrao, formato in formatos_numericos:
        match = re.search(padrao, data_string)
        if match:
            try:
                data_obj = datetime.strptime(match.group(0), formato)
                return data_obj.strftime("%d/%m/%Y")
            except:
                continue

    return "-"

def extrair_data_contextual(texto_pagina):

    palavras_chave = [
        'submiss',
        'submissão',
        'Submissões',
        'envio',
        'deadline',
        'prazo',
        'call for papers',
        'recebimento',
        'recepção de artigos',
        'recebimento de artigos',
        'data limite',
    ]

    linhas = re.split(r'[\n\.]', texto_pagina)

    for linha in linhas:

        linha_lower = linha.lower()

        if any(p in linha_lower for p in palavras_chave):

            datas = re.findall(
                r'\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{1,2}\s+de\s+[a-zç]+\s+de\s+\d{4}',
                linha,
                re.IGNORECASE
            )

            for data in datas:

                data_limpa = limpar_data(data)

                if data_limpa != "-":
                    print(f"   Data contextual encontrada: {data_limpa}")
                    return data_limpa

    return "-"

def extrair_data_submissao_especifica(sopa, texto_pagina, url):

    # Frases-chave em ordem de prioridade: a mais específica primeiro.
    # A primeira frase que aparecer no texto E tiver uma data logo depois vence.
    frases_prioritarias = [
        r'[Pp]razo\s+final(?:\s+para)?\s+(?:submiss[ãa]o|envio)',
        r'[Dd]ata\s+(?:limite|final)\s+(?:para\s+)?(?:submiss[ãa]o|envio)',
        r'[Pp]razo\s+(?:para\s+)?(?:submiss[ãa]o|submiss[õo]es|envio)',
        r'[Ss]ubmiss[ãa]o\s+(?:at[ée]|de\s+trabalhos|de\s+artigos)?',
        r'[Dd]eadline',
        r'[Cc]losing\s+[Dd]ate',
        r'[Dd]ue\s+[Dd]ate',
    ]

    padrao_data = (
        r'\d{1,2}\s*[/-]\s*\d{1,2}\s*[/-]\s*\d{4}'
        r'|\d{1,2}\s+de\s+[a-zçã]+\s+de\s+\d{4}'
        r'|[A-Za-z]+\s+\d{1,2},?\s+\d{4}'
    )

    for frase in frases_prioritarias:
        for match_frase in re.finditer(frase, texto_pagina, re.IGNORECASE):
            inicio = match_frase.end()
            janela = texto_pagina[inicio:inicio + 40]  # só o trecho logo depois da frase-chave

            match_data = re.search(padrao_data, janela, re.IGNORECASE)
            if match_data:
                data_limpa = limpar_data(match_data.group(0))
                if data_limpa != "-":
                    print(f"   Data encontrada ('{frase}'): {match_data.group(0)} -> {data_limpa}")
                    return data_limpa

    # Só cai no método genérico se NENHUMA frase-chave prioritária deu resultado
    data_contextual = extrair_data_contextual(texto_pagina)
    if data_contextual != "-":
        return data_contextual

    return "-"

def extrair_nome_revista_simplificado(sopa, url):
    
    meta_prioritarias = [
    "citation_journal_title",
    "dc.title"
    ]

    for meta_name in meta_prioritarias:
        meta = sopa.find("meta", attrs={"name": meta_name} )
        if meta and meta.get("content"):
            nome = meta["content"].strip()
            if len(nome) > 3:
                return nome
    
    # 1. Título da página
    if sopa.title:
        title_text = sopa.title.get_text(strip=True)
        if "|" in title_text:
            partes = title_text.split("|")
            # Tira o que parece ser nome da revista (geralmente último)
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
            'anúncios', 'announcement', 'notícias', 'news', 'avisos',
            'acesso', 'login', 'cadastro', 'buscar', 'pesquisa',
            'informações', 'about', 'sobre'
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

def extrair_multiplas_chamadas(texto):

    chamadas = []
    texto_norm = re.sub(r'\s+', ' ', texto)

    # Padrão 1: "Dossiê <título> Organização: <...> Prazo: <data>"
    padrao1 = (
        r'Dossiê\s+'
        r'(.+?)'
        r'\s+Organização:'
        r'.*?'
        r'Prazo:\s*'
        r'([^\.]+)'
    )

    for match in re.finditer(padrao1, texto_norm, re.IGNORECASE | re.DOTALL):
        tema = match.group(1).strip()
        tema = re.split(r'\d{4}-\d{2}-\d{2}', tema)[0].strip()
        data = limpar_data(match.group(2).strip())
        if data != "-":
            chamadas.append({"tema": tema, "data": data})

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
        tema = match.group(1).strip()
        data = limpar_data(match.group(2).strip())  # pega a data FINAL da janela ("a dd/mm/aaaa")
        if data != "-":
            chamadas.append({"tema": tema, "data": data})

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
        tema = match.group(1).strip()
        data = limpar_data(match.group(2).strip())
        if data != "-" and len(tema) > 10:
            chamadas.append({"tema": tema, "data": data})

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
        tema = match.group(1).strip()
        data = limpar_data(match.group(2).strip())
        if data != "-" and len(tema) > 10:
            chamadas.append({"tema": tema, "data": data})

    if chamadas:
        return chamadas

    return chamadas


PREFIXOS_GENERICOS = [
    r'^chamada\s+de\s+artigos\s*[-:–]\s*',
    r'^chamada\s*[-:–]\s*',
    r'^call\s+for\s+papers\s*[-:–]\s*',
]

def limpar_prefixo_generico(titulo):
    t = titulo.strip()
    for padrao in PREFIXOS_GENERICOS:
        t = re.sub(padrao, '', t, flags=re.IGNORECASE).strip()
    return t

def eh_conteudo_pt_br(texto_pagina, url):
    """Retorna True se a página parece estar em português; False caso contrário."""

    url_lower = url.lower()

    # Camada 1: domínios conhecidos de outros países/idiomas
    dominios_nao_pt = [
        '.es/', '.ar/', '.mx/', '.cl/', '.co/', '.pe/', '.uy/', '.fr/',
        'openedition.org', 'erudit.org', 'fecyt.es', 'rieoei.org',
        'uned.es', 'conicet.gov.ar', 'unal.edu.co'
    ]
    for dominio in dominios_nao_pt:
        if dominio in url_lower:
            return False

    # Camada 2: heurística por palavras-chave típicas de cada idioma
    texto_lower = " " + texto_pagina.lower() + " "

    palavras_pt = [' submissão', ' artigos', ' prazo ', ' revista ', ' dossiê', ' chamada de', ' edição', ' até ']
    palavras_es = [' convocatoria', ' plazo ', ' artículo', ' número ', ' recepción', ' presentación', ' revista de la']
    palavras_en = [' deadline', ' submission', ' call for papers', ' journal ', ' issue ']
    palavras_fr = [' appel à', ' articles', ' numéro ', ' revue ', ' soumission']

    scores = {
        'pt': sum(texto_lower.count(p) for p in palavras_pt),
        'es': sum(texto_lower.count(p) for p in palavras_es),
        'en': sum(texto_lower.count(p) for p in palavras_en),
        'fr': sum(texto_lower.count(p) for p in palavras_fr),
    }

    if max(scores.values()) == 0:
        return True  # sem sinal claro, deixa passar (não bloqueia por engano)

    idioma_dominante = max(scores, key=scores.get)
    return idioma_dominante == 'pt'

MARCADORES_FIM_CONTEUDO = [
    r'\bSaiba mais\b',
    r'\bEnviar Submiss[ãa]o\b',
    r'\bIdioma\b',
    r'\bM[ée]tricas\b',
    r'\bIndexadores\b',
    r'\bPalavras-chave\b',
    r'\bDiret[óo]rios\b',
]

def cortar_lixo_final(texto):
    for marcador in MARCADORES_FIM_CONTEUDO:
        match = re.search(marcador, texto, re.IGNORECASE)
        if match:
            texto = texto[:match.start()].strip(" -:;.,")
    return texto

def extrair_tema_avancado(sopa, texto_pagina):

    meta = sopa.find("meta", attrs={"name": "citation_title"})

    if meta and meta.get("content"):
        titulo = limpar_prefixo_generico(meta["content"].strip())

        termos_genericos = [
            "resultado", "edital", "announcement", "revista",
            "dossiês temáticos", "dossies tematicos", "calls",
        ]

        if (
            len(titulo) > 15
            and not any(t == titulo.lower().strip() for t in termos_genericos)
        ):
            return cortar_lixo_final(titulo)

    # 2. Padrões específicos
    padroes = [
        r'Dossiê\s+Temático\s+n?\.?\s*\d+\s*[-:]\s*(.+?)(?:\d{4}-\d{2}-\d{2}|$)',
        r'["“]([^"”]{30,300})["”]',
        r'Temática:\s*"([^"]+)"',
        r'Tema:\s*"([^"]+)"',
        r'Dossiê:\s*"([^"]+)"',
        r'Dossiê\s*n\.\s*\d+[^:]*:\s*[“"]([^”"]+)[”"]',
        r'CHAMADA\s+DE\s+ARTIGOS\s+PARA\s+DOSSIÊ[:\s]+([^\n]{20,500})',
        r'Chamada\s+para\s+Dossiê\s+([^\.]{20,300})',
        r'Número\s+Especial[:\s]+([^\.]{20,300})',
        r'Special\s+Issue[:\s]+([^\.]{20,300})',
    ]

    for padrao in padroes:
        match = re.search(
            padrao,
            texto_pagina,
            re.IGNORECASE
        )

        if match:
            tema = match.group(1).strip()
            tema = re.sub(r'\s+', ' ', tema)
            if len(tema) <= 10:
                continue
            lixo = [
                "started at","calls were launched","average of","journal","submission","editorial","volume", "issue"
            ]

            if any(x in tema.lower() for x in lixo):
                continue

            return tema

    if sopa.title:
        title_text = sopa.title.get_text(" ", strip=True)
        if "|" in title_text:
            candidato = title_text.split("|")[0].strip()
            candidato = limpar_prefixo_generico(candidato)
            if len(candidato) > 10:
                return cortar_lixo_final(candidato)

    # 3. Busca em títulos H1/H2/H3
    titulos = sopa.find_all(['h1', 'h2', 'h3'])
    candidatos = []

    palavras = [
        'dossiê','dossie','call for papers','special issue',
        'edição especial','número especial','thematic issue',
        'chamada'
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

def extrair_tema_simplificado(sopa):
    
    titulos = sopa.find_all(['h1','h2','h3'])

    for titulo in titulos:

        texto = titulo.get_text(" ", strip=True)

        if any(x in texto.lower() for x in [
            'dossiê',
            'dossie',
            'call for papers',
            'chamada',
            'special issue',
            'edição especial',
            'numero tematico',
            'número temático',
            'thematic issue'
        ]):

            return texto

def extrair_metadados_ojs(sopa):

    dados = {}

    campos = {
        "citation_journal_title": "revista",
        "citation_title": "tema",
        "citation_issn": "issn"
    }

    for campo, nome in campos.items():

        meta = sopa.find(
            "meta",
            attrs={"name": campo}
        )

        if meta and meta.get("content"):
            dados[nome] = meta["content"].strip()

    return dados

# =====================================================
# PROCESSAMENTO PRINCIPAL
# =====================================================
def processar_links(links):
    """Processa uma lista de links"""
    
    dados_saida = []
    
    for i, link in enumerate(links, 1):
        print(f"\n[{i}/{len(links)}] Processando: {link}")
        
        try:
            # Requisição
            r = session.get(link, timeout=30)
            
            # Verifica status
            if r.status_code == 404:
                print("   ERRO 404")
                dados_saida.append(["-", "-", "-", "-", "-", link, "PARCIAL"])
                continue
            elif r.status_code != 200:
                print(f"   ERRO {r.status_code}")
                dados_saida.append(["-", "-", "-", "-", "-", link, "PARCIAL"])
                continue
            
            # Parse
            sopa = BeautifulSoup(r.content, 'html.parser', from_encoding='utf-8')
            for tag in sopa([
                    'script',
                    'style',
                    'nav',
                    'footer',
                    'header',
                    'noscript',
                    'iframe'
            ]):   
                tag.decompose()

            texto_pagina = sopa.get_text(" ", strip=True)
            
            if len(texto_pagina) < 100:
                print("   CONTEÚDO INSUFICIENTE")
                dados_saida.append(["-", "-", "-", "-", "-", link, "PARCIAL"])
                continue

            PADROES_BLOQUEIO = [
            r'certificando de que voc[êe] n[ãa]o [ée] um bot',
            r'anubis',
            r'checking your browser',
            r'this site uses anubis',
            r'ce site utilise anubis',
            ]

            texto_lower_check = texto_pagina.lower()
            if any(re.search(p, texto_lower_check) for p in PADROES_BLOQUEIO):
                print("   BLOQUEADO POR ANTI-BOT")
                dados_saida.append(["-", "-", "-", "-", "-", link, "PARCIAL"])
                continue    
            
            # ===== NOVO: filtro de escopo PT-BR =====
            if not eh_conteudo_pt_br(texto_pagina, link):
                print("   FORA DO ESCOPO (não PT-BR)")
                dados_saida.append(["-", "-", "-", "-", "-", link, "FORA_DO_ESCOPO"])
                continue
            # ===== fim do bloco novo =====

            # Extrai informações
            data_submissao = extrair_data_submissao_especifica(sopa, texto_pagina, link)
            revista = extrair_nome_revista_simplificado(sopa, link)
            
            chamadas = extrair_multiplas_chamadas(texto_pagina)

            if chamadas:
                issn = extrair_issn(texto_pagina, sopa)
                qualis = extrair_qualis(texto_pagina)

                for chamada in chamadas:
                    if revista != "-" and chamada["tema"] != "-" and chamada["data"] != "-" and qualis != "-" and issn != "-":
                        status = "OK"
                    else:
                        status = "PARCIAL"
                    dados_saida.append([
                        chamada["data"], revista,
                        chamada["tema"], qualis,
                        issn, link, status
                    ])
                continue

            tema = extrair_tema_avancado(
                sopa,
                texto_pagina
            )

            issn = extrair_issn(texto_pagina, sopa)
            qualis = extrair_qualis(texto_pagina)
            
            # Determina status
            if revista != "-" and tema != "-" and data_submissao != "-" and qualis != "-" and issn != "-":
                status = "OK"
            else:
                status = "PARCIAL"
            
            dados_saida.append([
                data_submissao,
                revista,
                tema,
                qualis,  # QUALIS
                issn,
                link,
                status
            ])
            
        except requests.exceptions.Timeout:
            print("   TIMEOUT")
            dados_saida.append(["-", "-", "-", "-", "-", link, "PARCIAL"])
        except Exception as e:
            print(f"  ERRO: {str(e)[:80]}")
            dados_saida.append(["-", "-", "-", "-", "-", link, "PARCIAL"])
    
    return dados_saida

def formatar_planilha(saida, total_linhas):
    """Aplica cores condicionais, cabeçalho em negrito e congela a primeira linha."""

    sheet_id = saida.id

    requests_formatacao = [
        # Cabeçalho em negrito com fundo cinza
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": 1
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.85, "green": 0.85, "blue": 0.85},
                        "textFormat": {"bold": True}
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat)"
            }
        },
        # Congela a primeira linha (cabeçalho)
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": 1}
                },
                "fields": "gridProperties.frozenRowCount"
            }
        },
        # Cor verde clara para linhas com status "OK" (coluna G, índice 6)
        {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": total_linhas + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": 7
                    }],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": '=$G2="OK"'}]
                        },
                        "format": {
                            "backgroundColor": {"red": 0.80, "green": 0.94, "blue": 0.80}
                        }
                    }
                },
                "index": 0
            }
        },
        # Cor amarela clara para "PARCIAL"
        {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": total_linhas + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": 7
                    }],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": '=$G2="PARCIAL"'}]
                        },
                        "format": {
                            "backgroundColor": {"red": 1.0, "green": 0.95, "blue": 0.70}
                        }
                    }
                },
                "index": 1
            }
        },
        # Cor cinza clara para "FORA_DO_ESCOPO"
        {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": total_linhas + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": 7
                    }],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": '=$G2="FORA_DO_ESCOPO"'}]
                        },
                        "format": {
                            "backgroundColor": {"red": 0.90, "green": 0.90, "blue": 0.90}
                        }
                    }
                },
                "index": 2
            }
        },
        # Auto-ajusta largura das colunas
        {
            "autoResizeDimensions": {
                "dimensions": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": 7
                }
            }
        }
    ]

    saida.spreadsheet.batch_update({"requests": requests_formatacao})




# =====================================================
# FUNÇÃO PRINCIPAL
# =====================================================
def main():
    # =====================================================
    # GOOGLE API
    # =====================================================
    SCOPES = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    
    credentials = Credentials.from_service_account_file(
        "automacaoeditais-8f0db36dc8e9.json",
        scopes=SCOPES
    )
    
    gc = gspread.authorize(credentials)
    
    # =====================================================
    # PLANILHA
    # =====================================================
    sh = gc.open_by_key("1HeDMxxmC9xhjta6IjSJ8FLj9mBc77aK0P4NAS5XIJ2I")
    entrada = sh.worksheet("Entrada")
    saida = sh.worksheet("Saída")
    
    # Lê links da planilha
    links = entrada.col_values(1)[1:100] 

    links_originais = len(links)
    links = list(dict.fromkeys(links))
    duplicatas_removidas = links_originais - len(links)

    if duplicatas_removidas > 0:
        print(f"AVISO: {duplicatas_removidas} link(s) duplicado(s) removido(s) da lista de entrada.")
    
    print("=" * 80)
    print("INICIANDO PROCESSAMENTO DE DATAS DE SUBMISSÃO")
    print("=" * 80)

    # Processa os links
    # Carrega linhas já OK (robô ou humano) — não serão sobrescritas
    dados_ok_existentes = carregar_dados_existentes(saida)

    # Processa os links
    dados_processados = processar_links(links)

    # Preserva a versão OK já existente, casando pelo TEMA (já que um link pode ter várias chamadas)
    for i, linha in enumerate(dados_processados):
        link = linha[5]
        tema_novo = linha[2]

        if link in dados_ok_existentes:
            candidatas = dados_ok_existentes[link]

            # Tenta achar a linha OK com o mesmo tema
            match = next((c for c in candidatas if c[2].strip() == tema_novo.strip()), None)

            # Se o link só tem 1 chamada, não precisa casar por tema
            if not match and len(candidatas) == 1:
                match = candidatas[0]

            if match:
                dados_processados[i] = match

    # Ordena: OK primeiro, depois PARCIAL, depois FORA_DO_ESCOPO
    ordem_status = {"OK": 0, "PARCIAL": 1, "FORA_DO_ESCOPO": 2}
    dados_processados.sort(key=lambda linha: ordem_status.get(linha[6], 99))

    # Limpa e escreve na planilha
    saida.clear()

    saida.append_row([
        "DATA DE SUBMISSÃO",
        "REVISTA", 
        "TEMA", 
        "QUALIS", 
        "ISSN/ISBN", 
        "LINK PARA ACESSO", 
        "STATUS"
    ])
    
    # Atualiza com os dados
    if dados_processados:
        saida.update(f"A2:G{len(dados_processados)+1}", dados_processados)
        formatar_planilha(saida, len(dados_processados))
    
    print("=" * 80)
    print(f"\nPROCESSAMENTO CONCLUIDO!")
    print(f"Links processados: {len(dados_processados)}")
    
    # Estatísticas
    ok_count = sum(1 for linha in dados_processados if linha[6] == "OK")
    parcial_count = sum(1 for linha in dados_processados if linha[6] == "PARCIAL")
    fora_escopo_count = sum(1 for linha in dados_processados if linha[6] == "FORA_DO_ESCOPO")

    print(f"OK: {ok_count}")
    print(f"PARCIAL: {parcial_count}")
    print(f"FORA DO ESCOPO (não PT-BR): {fora_escopo_count}")

# EXECUÇÃO
if __name__ == "__main__":
    main()