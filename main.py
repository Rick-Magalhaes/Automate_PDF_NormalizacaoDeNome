"""
Renomeador de Procurações em PDF (Docusign)
============================================
Extrai CPF e 6 votos de procurações assinadas via Docusign e renomeia o arquivo:
    132.713.690-20 - A, Fel, JNEY, NV, A, A.pdf

Arquitetura de extração — dupla estratégia:
  1. Texto linear (extract_text): captura a maioria dos casos com boa fidelidade.
  2. Espacial por coordenadas (extract_words): fallback para blocos embaralhados.

Tratamento de itens ausentes/corrompidos:
  - Se um número de deliberação (1-6) não for encontrado no texto → NV
  - Se a deliberação existir mas não tiver opção marcada → NV
  - Se a deliberação existir mas o bloco estiver corrompido (Docusign collapse) → NV

Renomeação por CPF:
  - CPF normalizado para o formato XXX.XXX.XXX-XX
  - Nome de arquivo: "132.713.690-20 - A, Fel, JNEY, NV, A, A.pdf"
"""

import re
import os
import csv
import logging
from pathlib import Path

import pdfplumber
from unidecode import unidecode
from tkinter import Tk, filedialog


# =============================================================================
# CONFIGURAÇÃO
# =============================================================================

ESCRITORIOS = [
    ("machado meyer",      "MM"),
    ("sacramone",          "SOB"),
    ("costa tavares",      "CTP"),
    ("pinheiro guimaraes", "PGA"),
    ("pinheiro guimar",    "PGA"),   # truncado pelo Docusign
    ("felsberg",           "Fel"),
    ("br partners",        "BR"),
    ("journey capital",    "JNEY"),
    ("virtus br",          "VIR"),
    ("g5 partners",        "G5"),
    ("sob",                "SOB"),
    ("g5",                 "G5"),
]

LOG_FILE  = "processamento.log"
CSV_ERROS = "erros.csv"

# Número de deliberações esperadas no template
NUM_DELIBERACOES = 6

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


# =============================================================================
# UTILITÁRIOS
# =============================================================================

def norm(texto: str) -> str:
    return unidecode(texto).lower().strip()

def limpar_underlines(texto: str) -> str:
    return re.sub(r"_+", " ", texto).strip()

def formatar_cpf(cpf_raw: str) -> str | None:
    """Normaliza qualquer formato de CPF para XXX.XXX.XXX-XX."""
    digits = re.sub(r"\D", "", cpf_raw)
    if len(digits) != 11:
        return None
    return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"

def caminho_unico(caminho: Path) -> Path:
    if not caminho.exists():
        return caminho
    i = 1
    while True:
        novo = caminho.with_stem(f"{caminho.stem}_{i}")
        if not novo.exists():
            return novo
        i += 1


# =============================================================================
# EXTRAÇÃO DE CPF
# =============================================================================

def extrair_cpf(texto: str) -> str | None:
    """
    Extrai o primeiro CPF válido (11 dígitos) do texto.
    Mantém suporte ao Docusign (_1_3_2...) e agora aceita
    qualquer separador entre os números.
    """

    # Remove underlines SEM quebrar os números
    texto_sem_under = re.sub(r"_", "", texto)

    for t in [texto_sem_under, texto]:
        m = re.search(r"\d{3}\D*\d{3}\D*\d{3}\D*\d{2}", t)
        if m:
            return formatar_cpf(re.sub(r"\D", "", m.group()))

    return None


# =============================================================================
# CLASSIFICAÇÃO DE VOTOS
# =============================================================================

def sigla_escritorio(texto: str) -> str | None:
    t = norm(texto)
    for nome, sigla in ESCRITORIOS:
        if nome in t:
            return sigla
    return None

def classificar_opcao_linha(texto_linha: str, num_delib: int) -> str | None:
    """
    Dado o texto de uma linha de opção marcada, retorna a sigla do voto.
    Para deliberações 2 e 3 (que escolhem escritório), prioriza nome do escritório.
    """
    t = norm(texto_linha)
    if num_delib in (2, 3):
        sigla = sigla_escritorio(t)
        if sigla:
            return sigla
        if "reprova" in t or "rejeita" in t:
            return "R"
        if "abstem" in t or "abstencao" in t:
            return "AB"
        if "aprova" in t:
            return "A"
        return None
    # Deliberações 1, 4, 5, 6: apenas aprova/reprova/abstém
    if "aprova" in t:
        return "A"
    if "reprova" in t or "rejeita" in t:
        return "R"
    if "abstem" in t or "abstencao" in t:
        return "AB"
    return None


# =============================================================================
# EXTRAÇÃO POR TEXTO LINEAR (estratégia primária)
# =============================================================================

# Detecta linha com opção marcada: "(X )" ou "(X)" no início, case-insensitive
OPCAO_MARCADA_RE = re.compile(r"^\s*\(\s*[xX]\s*\)\s*(.+)", re.IGNORECASE)

# Detecta cabeçalho de deliberação: linha começando com "(N)" onde N é 1-6
CABECALHO_LINEAR_RE = re.compile(r"^\s*\(?\s*([1-6])\s*\)?\s*[\.\-]?\s+\w", re.IGNORECASE)


def texto_completo(pdf) -> str:
    return "\n".join(p.extract_text() or "" for p in pdf.pages)


def extrair_votos_linear(texto: str) -> dict[int, str]:
    """
    Estratégia 1: percorre linhas do texto extraído em ordem e associa
    cada opção marcada (X) à deliberação cujo cabeçalho aparece antes dela.
    Retorna dict {num_delib: sigla}.
    """
    resultados: dict[int, str] = {}
    delib_atual: int | None = None
    delib_presente: set[int] = set()  # quais deliberações aparecem no texto

    for linha in texto.splitlines():
        linha_strip = linha.strip()
        if not linha_strip:
            continue

        # Verifica se é cabeçalho de deliberação
        m_cab = CABECALHO_LINEAR_RE.match(linha_strip)
        if m_cab:
            num = int(m_cab.group(1))
            delib_atual = num
            delib_presente.add(num)
            continue

        # Verifica se é opção marcada
        m_opc = OPCAO_MARCADA_RE.match(linha_strip)
        if m_opc and delib_atual is not None:
            conteudo = m_opc.group(1)
            sigla = classificar_opcao_linha(conteudo, delib_atual)
            if sigla and delib_atual not in resultados:
                resultados[delib_atual] = sigla
                logging.info("LINEAR | Del %d | '%s' → %s", delib_atual, conteudo[:50], sigla)

    logging.info("Deliberações encontradas no texto: %s", sorted(delib_presente))
    return resultados, delib_presente


# =============================================================================
# EXTRAÇÃO ESPACIAL POR COORDENADAS (estratégia de fallback)
# =============================================================================

# (X no início da palavra-grupo indica opção marcada
OPCAO_MARCADA_WORD_RE = re.compile(r"^\s*\(\s*[xX]", re.IGNORECASE)

# Cabeçalho de deliberação: linha cujo início tem número 1-6 isolado
CABECALHO_WORD_RE = re.compile(r"^\s*[\(\s]*([1-6])[\)\s]*[\.\-]?\s+\w", re.IGNORECASE)


def reconstruir_linhas_pagina(pagina) -> list[dict]:
    """
    Usa extract_words() e agrupa por top (±5px) para reconstruir
    linhas na ordem visual real. Retorna lista de dicts {top, text}.
    """
    palavras = pagina.extract_words(keep_blank_chars=False) or []
    if not palavras:
        return []

    grupos: list[list] = []
    for p in sorted(palavras, key=lambda w: w["top"]):
        colocado = False
        for grupo in grupos:
            if abs(p["top"] - grupo[0]["top"]) <= 5:
                grupo.append(p)
                colocado = True
                break
        if not colocado:
            grupos.append([p])

    linhas = []
    for grupo in grupos:
        grupo.sort(key=lambda w: w["x0"])
        texto = " ".join(w["text"] for w in grupo)
        top = grupo[0]["top"]
        linhas.append({"top": top, "text": texto})

    return sorted(linhas, key=lambda l: l["top"])


def extrair_votos_espacial(pdf) -> dict[int, str]:
    """
    Estratégia 2 (fallback espacial): reconstrói linhas por coordenadas
    e associa cada marcação (X) à deliberação anterior mais próxima no eixo Y.
    """
    todas_linhas: list[dict] = []
    offset = 0.0

    for pagina in pdf.pages:
        linhas = reconstruir_linhas_pagina(pagina)
        for l in linhas:
            todas_linhas.append({"top": l["top"] + offset, "text": l["text"]})
        offset += float(pagina.height or 842)

    cabecalhos: list[dict] = []
    marcadas: list[dict] = []
    vistos_cab: set = set()

    for l in todas_linhas:
        texto = l["text"]
        m = CABECALHO_WORD_RE.match(texto)
        if m:
            num = int(m.group(1))
            if num not in vistos_cab:
                cabecalhos.append({"top": l["top"], "num": num})
                vistos_cab.add(num)
            continue
        if OPCAO_MARCADA_WORD_RE.match(texto):
            marcadas.append({"top": l["top"], "text": texto})

    resultados: dict[int, str] = {}
    for m in marcadas:
        anteriores = [c for c in cabecalhos if c["top"] <= m["top"] + 10]
        if not anteriores:
            continue
        cab = max(anteriores, key=lambda c: c["top"])
        num = cab["num"]
        if num in resultados:
            continue
        sigla = classificar_opcao_linha(m["text"], num)
        if sigla:
            resultados[num] = sigla
            logging.info("ESPACIAL | Del %d | top=%.0f | '%s' → %s",
                         num, m["top"], m["text"][:60], sigla)

    return resultados, vistos_cab


# =============================================================================
# DETECÇÃO DE DELIBERAÇÕES PRESENTES NO DOCUMENTO
# =============================================================================

def detectar_deliberacoes_presentes(texto: str) -> set[int]:
    """
    Verifica quais números de deliberação (1-6) aparecem no texto.
    Usa múltiplos padrões para sobreviver ao embaralhamento do Docusign.
    """
    presentes = set()
    for num in range(1, NUM_DELIBERACOES + 1):
        # Padrão 1: "(N)" ou "(N )" ou "( N )" normais
        p1 = re.compile(rf"\(\s*{num}\s*\)", re.IGNORECASE)
        # Padrão 2: "((( N )))" embaralhado pelo Docusign
        p2 = re.compile(rf"\({2,}\s*{num}\s*\){2,}", re.IGNORECASE)
        # Padrão 3: número isolado antes de "Em relação"
        p3 = re.compile(rf"\b{num}\b.{{0,20}}Em rela", re.IGNORECASE)
        if p1.search(texto) or p2.search(texto) or p3.search(texto):
            presentes.add(num)
    return presentes


# =============================================================================
# ESTRATÉGIA COMBINADA
# =============================================================================

def extrair_votos(pdf) -> list[str]:
    """
    Combina estratégias linear e espacial.
    Para cada deliberação 1-6:
      - Se não detectada no documento → NV (item removido pelos usuários)
      - Se detectada mas sem voto marcado → NV (não votou / corrompido)
      - Se detectada com voto → sigla correspondente
    Retorna lista de 6 strings (nunca None).
    """
    texto = texto_completo(pdf)

    # Detecta quais deliberações existem de fato no documento
    presentes = detectar_deliberacoes_presentes(texto)
    logging.info("Deliberações detectadas no documento: %s", sorted(presentes))

    # Estratégia 1: linear
    resultados_linear, _ = extrair_votos_linear(texto)

    # Estratégia 2: espacial (para complementar gaps)
    resultados_espacial, _ = extrair_votos_espacial(pdf)

    # Mescla: linear tem prioridade, espacial preenche ausências
    resultados_final: dict[int, str] = {}
    for num in range(1, NUM_DELIBERACOES + 1):
        if num not in presentes:
            # Deliberação removida do documento pelos usuários
            resultados_final[num] = "NV"
            logging.info("Del %d → NV (não encontrada no documento)", num)
        elif num in resultados_linear:
            resultados_final[num] = resultados_linear[num]
        elif num in resultados_espacial:
            resultados_final[num] = resultados_espacial[num]
        else:
            # Deliberação existe mas o voto não foi detectável (bloco corrompido)
            resultados_final[num] = "NV"
            logging.info("Del %d → NV (presente mas voto não identificado)", num)

    return [resultados_final[i] for i in range(1, NUM_DELIBERACOES + 1)]


# =============================================================================
# VALIDAÇÃO E LOG DE ERROS
# =============================================================================

def validar(cpf: str | None, votos: list[str]) -> list[str]:
    erros = []
    if not cpf:
        erros.append("CPF não encontrado")
    return erros  # votos NV não são mais considerados erros — são esperados


def registrar_erro(arquivo: str, erros: list[str]) -> None:
    existe = os.path.exists(CSV_ERROS)
    with open(CSV_ERROS, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not existe:
            writer.writerow(["arquivo", "erro"])
        for erro in erros:
            writer.writerow([arquivo, erro])
            logging.warning("%s | %s", arquivo, erro)


# =============================================================================
# PROCESSAMENTO PRINCIPAL
# =============================================================================

def montar_nome_arquivo(cpf: str, votos: list[str]) -> str:
    votos_str = ", ".join(votos)
    return f"{cpf} - {votos_str}.pdf"


def processar_pdf(caminho: Path) -> None:
    try:
        with pdfplumber.open(caminho) as pdf:
            texto = texto_completo(pdf)

            if not texto.strip():
                raise ValueError("PDF sem texto extraível (possivelmente escaneado)")

            cpf   = extrair_cpf(texto)
            votos = extrair_votos(pdf)

        logging.info("%s | OK | cpf=%s | votos=%s", caminho.name, cpf, votos)
        print(f"  → {caminho.name}")
        print(f"     CPF   : {cpf or 'NÃO ENCONTRADO'}")
        print(f"     Votos : {votos}")

        erros = validar(cpf, votos)
        if erros:
            registrar_erro(caminho.name, erros)
            print(f"  ⚠  Erros: {erros}")
            return

        novo_nome    = montar_nome_arquivo(cpf, votos)
        novo_caminho = caminho_unico(caminho.with_name(novo_nome))
        os.rename(caminho, novo_caminho)
        print(f"  ✓  Renomeado → {novo_caminho.name}\n")

    except Exception as exc:
        registrar_erro(caminho.name, [str(exc)])
        print(f"  ✗  {caminho.name} → {exc}\n")
        logging.error("%s | %s", caminho.name, exc, exc_info=True)


# =============================================================================
# INTERFACE
# =============================================================================

def selecionar_pasta() -> Path | None:
    root = Tk()
    root.withdraw()
    pasta = filedialog.askdirectory(title="Selecione a pasta com as procurações")
    root.destroy()
    return Path(pasta) if pasta else None


def main() -> None:
    pasta = selecionar_pasta()
    if not pasta:
        print("Nenhuma pasta selecionada. Encerrando.")
        return

    arquivos = sorted(pasta.glob("*.pdf"))
    if not arquivos:
        print("Nenhum PDF encontrado na pasta selecionada.")
        return

    print(f"\nProcessando {len(arquivos)} arquivo(s)...\n")
    for arq in arquivos:
        processar_pdf(arq)

    print(f"\nFinalizado! Verifique '{LOG_FILE}' e '{CSV_ERROS}' para detalhes.")


if __name__ == "__main__":
    main()