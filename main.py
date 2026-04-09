"""
Renomeador de Procurações em PDF (Docusign)
============================================
Extrai nome, CPF e 6 votos de procurações assinadas via Docusign e
renomeia o arquivo:
    Carlos Augusto - A, Fel, MM, AB, R, R.pdf

Estratégia de extração de votos — espacial por coordenadas:
  O Docusign embaralha o fluxo de texto em alguns blocos. Por isso
  usamos extract_words() e agrupamos por posição (top/x0) para
  reconstruir as linhas na ordem visual correta.
  
  Fluxo:
    1. Agrupa palavras em linhas pela coordenada top (±5px).
    2. Detecta cabeçalhos de deliberação (linhas com número 1-6).
    3. Detecta linhas de opção marcada: começam com "(X" seguido de ")".
    4. Associa cada opção marcada à deliberação cujo cabeçalho
       aparece imediatamente antes dela no eixo Y.
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

def limpar_underlines(texto): return re.sub(r"_", "", texto)

def limpar_nome(nome: str) -> str:
    nome = limpar_underlines(nome)
    nome = re.sub(r"\b(null|cliente|cpf|cnpj|rg|inscrito[a]?)\b", "", nome, flags=re.IGNORECASE)
    nome = re.sub(r"\s{2,}", " ", nome)
    return nome.strip().title()

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
# EXTRAÇÃO DE DADOS BÁSICOS (texto linear)
# =============================================================================

def texto_completo(pdf) -> str:
    return "\n".join(p.extract_text() or "" for p in pdf.pages)

def extrair_cpf(texto: str) -> str | None:
    # Tenta no texto original e também sem underlines
    for t in [texto, limpar_underlines(texto)]:
        m = re.search(r"\d{3}[\.\s]?\d{3}[\.\s]?\d{3}[\-\s]?\d{2}", t)
        if m:
            return re.sub(r"\s", "", m.group()).strip()
    return None

def extrair_nome(texto: str) -> str | None:
    # 1. Campo "NOME:" com possíveis underlines do Docusign
    m = re.search(
        r"\bNOME\b\s*[:\-]?\s*([_A-ZÀ-Úa-zà-ú][_A-ZÀ-Úa-zà-ú\s]{4,})",
        texto, re.IGNORECASE
    )
    if m:
        candidato = m.group(1).split("\n")[0]
        resultado = limpar_nome(candidato)
        if len(resultado.split()) >= 2:
            return resultado

    # 2. Padrão "Fulano de Tal, inscrito(a) no CPF" (sem underlines)
    texto_limpo = limpar_underlines(texto)
    m = re.search(
        r"([A-ZÀ-Ú][a-zà-ú]+(?:\s+(?:da|de|do|dos|das|e|di|von|van)?\s*[A-ZÀ-Úa-zà-ú]+){1,6})"
        r"\s*,?\s*inscrito",
        texto_limpo, re.IGNORECASE
    )
    if m:
        return limpar_nome(m.group(1))

    # 3. Fallback: primeira linha toda em maiúsculas com ≥ 3 palavras
    for linha in limpar_underlines(texto).splitlines():
        linha = linha.strip()
        if linha.isupper() and len(linha.split()) >= 3 and "CPF" not in linha:
            return limpar_nome(linha)

    return None


# =============================================================================
# EXTRAÇÃO DE VOTOS — ABORDAGEM ESPACIAL POR COORDENADAS
# =============================================================================

# Detecta início de número de deliberação em uma linha reconstruída
CABECALHO_NUM = re.compile(r"^\s*[\(\s]*([1-6])[\)\s]*[\.\-]?\s+\w", re.IGNORECASE)

# Detecta linha de opção marcada: começa com "(X" e depois ")"
# O Docusign separa "(X" e ")" como palavras distintas, mas ao reconstruir
# a linha elas ficam juntas: "(X )" ou "(X)"
OPCAO_MARCADA = re.compile(r"^\s*\(\s*[xX]\s*\)", re.IGNORECASE)


def sigla_escritorio(texto: str) -> str | None:
    t = norm(texto)
    for nome, sigla in ESCRITORIOS:
        if nome in t:
            return sigla
    return None

def classificar_opcao(texto_linha: str, num_delib: int) -> str | None:
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
    if "aprova" in t:
        return "A"
    if "reprova" in t or "rejeita" in t:
        return "R"
    if "abstem" in t or "abstencao" in t:
        return "AB"
    return None


def reconstruir_linhas(pagina) -> list[dict]:
    """
    Usa extract_words() e agrupa por top (±5px) para reconstruir
    linhas na ordem visual real (não na ordem do fluxo de texto).
    Retorna lista de dicts {top_global, text} ordenada por top.
    """
    palavras = pagina.extract_words(keep_blank_chars=False) or []
    if not palavras:
        return []

    # Agrupa palavras em linhas por proximidade vertical
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
        # Ordena palavras da linha da esquerda para a direita
        grupo.sort(key=lambda w: w["x0"])
        texto = " ".join(w["text"] for w in grupo)
        top = grupo[0]["top"]
        linhas.append({"top": top, "text": texto})

    return sorted(linhas, key=lambda l: l["top"])


def extrair_votos(pdf) -> list:
    """
    Reconstrói linhas por coordenadas em todas as páginas,
    detecta cabeçalhos de deliberação e opções marcadas,
    e associa cada marcação à deliberação correta pela posição Y.
    """
    # Coleta todas as linhas de todas as páginas com top acumulado
    todas_linhas: list[dict] = []
    offset = 0.0

    for pagina in pdf.pages:
        linhas = reconstruir_linhas(pagina)
        for l in linhas:
            todas_linhas.append({
                "top":  l["top"] + offset,
                "text": l["text"]
            })
        offset += float(pagina.height or 842)

    # Identifica cabeçalhos e opções marcadas
    cabecalhos: list[dict] = []   # {top, num}
    marcadas:   list[dict] = []   # {top, text}
    vistos_cab: set = set()

    for l in todas_linhas:
        texto = l["text"]

        m = CABECALHO_NUM.match(texto)
        if m:
            num = int(m.group(1))
            if num not in vistos_cab:
                cabecalhos.append({"top": l["top"], "num": num})
                vistos_cab.add(num)
            continue

        if OPCAO_MARCADA.match(texto):
            marcadas.append({"top": l["top"], "text": texto})

    # Para cada opção marcada, encontra a deliberação anterior mais próxima
    resultados: dict[int, str] = {}

    for m in marcadas:
        # Deliberações cujo cabeçalho está ANTES (ou no mesmo nível) da opção
        anteriores = [c for c in cabecalhos if c["top"] <= m["top"] + 10]
        if not anteriores:
            continue
        cab = max(anteriores, key=lambda c: c["top"])
        num = cab["num"]

        if num in resultados:
            continue  # já temos voto para esta deliberação

        sigla = classificar_opcao(m["text"], num)
        if sigla:
            resultados[num] = sigla
            logging.info("Deliberação %d | top=%.0f | texto='%s' | sigla=%s",
                         num, m["top"], m["text"][:60], sigla)
# Se os resultados por proximidade ficarem incompletos,
# associa as marcadas em ordem às deliberações sem voto
    faltantes = [i for i in range(1, 7) if i not in resultados]
    sobras = [m for m in marcadas if classificar_opcao(m["text"], 99)]  # não associadas
    for num, m in zip(faltantes, sobras):
     s = classificar_opcao(m["text"], num)
     if s:
         resultados[num] = s
    return [resultados.get(i) for i in range(1, 7)]


# =============================================================================
# VALIDAÇÃO E LOG DE ERROS
# =============================================================================

def validar(nome, cpf, votos) -> list:
    erros = []
    if not nome:
        erros.append("Nome não encontrado")
    if not cpf:
        erros.append("CPF não encontrado")
    nulos = [i + 1 for i, v in enumerate(votos) if v is None]
    if nulos:
        erros.append(f"Voto(s) ausente(s) nas deliberações: {nulos}")
    return erros

def registrar_erro(arquivo: str, erros: list) -> None:
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

def montar_nome_arquivo(nome: str, votos: list) -> str:
    votos_str = ", ".join(v if v else "?" for v in votos)
    return f"{nome} - {votos_str}.pdf"

def processar_pdf(caminho: Path) -> None:
    try:
        with pdfplumber.open(caminho) as pdf:
            texto = texto_completo(pdf)

            if not texto.strip():
                raise ValueError("PDF sem texto extraível (possivelmente escaneado)")

            nome  = extrair_nome(texto)
            cpf   = extrair_cpf(texto)
            votos = extrair_votos(pdf)

        logging.info("%s | OK | votos=%s", caminho.name, votos)
        print(f"  → {caminho.name} | votos={votos}")

        erros = validar(nome, cpf, votos)
        if erros:
            registrar_erro(caminho.name, erros)
            print(f"  ⚠  {caminho.name} → {erros}")
            return

        novo_nome    = montar_nome_arquivo(nome, votos)
        novo_caminho = caminho_unico(caminho.with_name(novo_nome))
        os.rename(caminho, novo_caminho)
        print(f"  ✓  {novo_caminho.name}")

    except Exception as exc:
        registrar_erro(caminho.name, [str(exc)])
        print(f"  ✗  {caminho.name} → {exc}")
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