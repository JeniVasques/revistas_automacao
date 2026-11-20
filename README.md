# Automação de Editais de Revistas

Programa em Python que lê páginas de chamadas de artigos (editais e dossiês temáticos) de revistas acadêmicas e preenche automaticamente uma planilha do Google Sheets com as informações principais de cada chamada.

Projeto desenvolvido no âmbito do PIBITI.

## O que o programa faz

Para cada link de chamada listado na planilha, o programa extrai:

| Campo | Descrição |
|---|---|
| Data de submissão | Prazo final para envio de artigos (`dd/mm/aaaa`) |
| Revista | Nome da revista |
| Tema | Título do dossiê ou da chamada |
| Qualis | Classificação Qualis (A1–A4, B1–B4, C) |
| ISSN/ISBN | Código de identificação da revista |
| Link | Endereço da chamada |
| Status | `OK`, `PARCIAL` ou `FORA_DO_ESCOPO` |
| Idioma | `pt`, `es`, `en` (ou outro idioma detectado) |

### Principais recursos

- **Leitura de metadados OJS** (Open Journal Systems): usa as meta tags padrão (`citation_journal_title`, `citation_title`, `citation_issn`) quando existem, e recorre a padrões de texto quando não existem.
- **Datas em português, espanhol e inglês**, em vários formatos (numérico, por extenso e abreviado).
- **Várias chamadas na mesma página:** quando um link traz vários dossiês, cada um vira uma linha.
- **Complemento por planilha de referência:** quando a página não informa Qualis ou ISSN, o programa busca na aba `QUALIS - ISSN` (por ISSN, por nome exato ou por nome parecido). Valores encontrados na página nunca são sobrescritos pela referência.
- **Status simples:** `OK` quando todos os campos obrigatórios estão preenchidos, `PARCIAL` quando falta algum e `FORA_DO_ESCOPO` para idiomas não suportados. O Qualis só é exigido para revistas em português.
- **Edições manuais preservadas:** linhas marcadas como `OK` são mantidas nas próximas execuções, para que correções feitas à mão não se percam.
- **Planilha organizada:** cores por status, cabeçalho fixo e ordenação (OK, depois PARCIAL, depois FORA_DO_ESCOPO).
- **Execução automática** diária pelo GitHub Actions.

## Estrutura da planilha

O programa espera uma planilha do Google Sheets com três abas:

| Aba | Conteúdo |
|---|---|
| `Entrada` | Links das chamadas, um por linha, na coluna A (a linha 1 é o cabeçalho) |
| `Saída` | Resultado gerado pelo programa (é reescrita a cada execução) |
| `QUALIS - ISSN` | Referência com 3 colunas: **A** revista, **B** Qualis, **C** ISSN |

## Como usar

### 1. Pré-requisitos

- Python 3.11 ou superior
- Uma conta de serviço (Service Account) do Google Cloud, com as APIs Google Sheets e Google Drive ativadas
- A planilha compartilhada, como **editor**, com o e-mail da conta de serviço (campo `client_email` do arquivo JSON)

### 2. Instalação

```bash
git clone https://github.com/JeniVasques/revistas_automacao.git
cd revistas_automacao
pip install -r requirements.txt
```

### 3. Configuração

No `main.py`, ajuste o ID da sua planilha em `gc.open_by_key("...")`. O ID é o trecho da URL entre `/d/` e `/edit`.

**Credenciais:** nunca coloque o arquivo JSON da conta de serviço no repositório.

- **No seu computador:** salve o arquivo JSON na pasta do projeto, com o nome indicado no `main.py`. O `.gitignore` já impede que ele seja enviado ao Git.
- **No GitHub Actions:** guarde o conteúdo completo do JSON no Secret `GOOGLE_CREDENTIALS`.

### 4. Execução local

```bash
python main.py
```

### 5. Execução automática (GitHub Actions)

O workflow em `.github/workflows/rodar.yml` roda o programa todos os dias às 06:00 UTC (03:00 no horário de Brasília) e também pode ser disparado manualmente em **Actions → Run workflow**.

Para ativar:
1. Vá em **Settings → Secrets and variables → Actions**.
2. Crie o Secret `GOOGLE_CREDENTIALS` com o conteúdo do arquivo JSON da conta de serviço.

## Limitações conhecidas

- **Sites com proteção anti-bot** (como o Anubis) bloqueiam o acesso. Esses links ficam como `PARCIAL` e precisam ser preenchidos manualmente.
- **Páginas com estrutura incomum** (listas de anúncios sem título de chamada, por exemplo) podem ficar sem tema.
- **Datas:** quando a página traz várias datas, o programa pode escolher a errada. Vale conferir linhas com prazos muito antigos.
- **Nomes de revistas:** o complemento pela referência depende de o nome da página ser parecido com o da planilha de referência. Abreviações e nomes em outros idiomas podem não casar.
- **Sites fora do ar** ou com falha de conexão ficam como `PARCIAL`. Uma nova execução costuma resolver.

## Trabalhos futuros

- Ampliar o suporte a outros idiomas (hoje: português, espanhol e inglês).
- Tratar sites com proteção anti-bot.
- Melhorar o casamento de nomes de revistas com a base de referência.
- Aumentar o conjunto de padrões de extração de datas e temas.

## Autoria

- Autoria: Jenifer Gomes Vasques 
- Orientação: Maria Nilvane
- Instituição: UFAM

## Licença
MIT
