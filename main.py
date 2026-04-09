import re
import os
import csv
import logging
from pathlib import Path
import pdfplumber
from unidecode import unidecode
from tkinter import Tk, filedialog

# =========================
# CONFIG
# =========================
MAP_ESCRITORIOS = {
    "felsberg": "Fel",
    "sob": "SOB",
    "machado meyer": "MM",
    "machado": "MM",
    "pga": "PGA",
    "ctp": "CTP",
    "br partners": "BR",
    "journey capital": "JNEY",
    "virtus br": "VIR",
    "g5 partners": "G5"
}

LOG_FILE = "processamento.log"
CSV_ERROS = "erros.csv"

# =========================
# LOGGING
# =========================
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

# =========================
# UTIL
# =========================
def normalizar(texto: str) -> str:
    return unidecode(texto.lower())


def limpar_nome(nome: str) -> str:
    nome = re.sub(r"\s+", " ", nome.strip())

    # remove lixo grudado (null, cliente, etc)
    nome = re.sub(r"(null|cliente|cpf|rg|inscrito)", "", nome, flags=re.IGNORECASE)

    # remove múltiplos espaços novamente
    nome = re.sub(r"\s+", " ", nome)

    return nome.title().strip()


def tem_marcacao(texto: str) -> bool:
    texto = normalizar(texto)
    return bool(re.search(r"\(\s*x\s*\)|\bx\b", texto))


def gerar_nome_unico(caminho: Path) -> Path:
    contador = 1
    novo = caminho

    while novo.exists():
        novo = caminho.with_stem(f"{caminho.stem}_{contador}")
        contador += 1

    return novo


# =========================
# EXTRAÇÃO
# =========================
def extrair_texto_pdf(caminho: Path) -> str:
    texto = []
    with pdfplumber.open(caminho) as pdf:
        for pagina in pdf.pages:
            texto.append(pagina.extract_text() or "")
    return "\n".join(texto)


def extrair_cpf(texto: str) -> str:
    match = re.search(r"\d{3}\.?\d{3}\.?\d{3}-?\d{2}", texto)
    return match.group() if match else None


def extrair_nome(texto: str) -> str:
    # Nome: Fulano
    match = re.search(r"nome[:\s]+([A-ZÀ-Ú\s]+)", texto, re.IGNORECASE)
    if match:
        return limpar_nome(match.group(1))

    # Fulano..., inscrito
    match = re.search(r"([A-ZÀ-Ú\s]{10,}),?\s+inscrito", texto, re.IGNORECASE)
    if match:
        return limpar_nome(match.group(1))

    # fallback: linha em caixa alta
    for linha in texto.split("\n"):
        linha = linha.strip()

        if (
            len(linha) > 10
            and linha.isupper()
            and len(linha.split()) >= 3
            and "CPF" not in linha
        ):
            return limpar_nome(linha)

    return None


# =========================
# VOTOS
# =========================
def extrair_voto_simples(bloco: str) -> str:
    linhas = bloco.split("\n")

    for linha in linhas:
        linha_norm = normalizar(linha)

        if not tem_marcacao(linha):
            continue

        if "aprova" in linha_norm:
            return "A"
        if "reprova" in linha_norm:
            return "R"
        if "abstem" in linha_norm or "abst" in linha_norm:
            return "AB"

    return None


def extrair_escritorio(bloco: str) -> str:
    linhas = bloco.split("\n")

    for linha in linhas:
        linha_norm = normalizar(linha)

        if not tem_marcacao(linha):
            continue

        # abstenção
        if "abstem" in linha_norm:
            return "AB"

        # escritórios
        for nome, sigla in MAP_ESCRITORIOS.items():
            if normalizar(nome) in linha_norm:
                return sigla

    return None


def separar_itens(texto: str):
    itens = re.split(r"\(\s*\d+\s*\)", texto)

    if len(itens) < 6:
        itens = re.split(r"\n\s*\d+[\.\)]", texto)

    return itens[1:7]


def extrair_votos(texto: str):
    itens = separar_itens(texto)

    votos = []

    for i, item in enumerate(itens, start=1):
        if i in [2, 3]:
            voto = extrair_escritorio(item)
        else:
            voto = extrair_voto_simples(item)

        votos.append(voto)

    return votos


# =========================
# VALIDAÇÃO
# =========================
def validar_dados(nome, cpf, votos):
    erros = []

    if not nome:
        erros.append("Nome não encontrado")

    if not cpf:
        erros.append("CPF não encontrado")

    if not votos or any(v is None for v in votos):
        erros.append("Falha na leitura dos votos")

    return erros


# =========================
# CSV ERROS
# =========================
def salvar_erro_csv(arquivo, erros):
    existe = os.path.exists(CSV_ERROS)

    with open(CSV_ERROS, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        if not existe:
            writer.writerow(["arquivo", "erro"])

        for erro in erros:
            writer.writerow([arquivo, erro])


# =========================
# PROCESSAMENTO
# =========================
def montar_nome(nome, votos):
    votos_limpos = [v if v else "?" for v in votos]
    return f"{nome} - {', '.join(votos_limpos)}.pdf"


def processar_pdf(caminho: Path):
    try:
        texto = extrair_texto_pdf(caminho)

        if not texto.strip():
            raise ValueError("PDF sem texto (possível scan)")

        logging.info(f"\n--- TEXTO {caminho.name} ---\n{texto[:1000]}")

        nome = extrair_nome(texto)
        cpf = extrair_cpf(texto)
        votos = extrair_votos(texto)

        erros = validar_dados(nome, cpf, votos)

        if erros:
            logging.warning(f"{caminho.name} - {erros}")
            salvar_erro_csv(caminho.name, erros)
            print(f" ERRO: {caminho.name} -> {erros}")
            return

        novo_nome = montar_nome(nome, votos)
        novo_caminho = gerar_nome_unico(caminho.with_name(novo_nome))

        os.rename(caminho, novo_caminho)

        logging.info(f"OK: {caminho.name} -> {novo_nome}")
        print(f" {novo_nome}")

    except Exception as e:
        logging.error(f"{caminho.name} - {str(e)}")
        salvar_erro_csv(caminho.name, [str(e)])
        print(f" FALHA: {caminho.name} -> {e}")


# =========================
# INTERFACE
# =========================
def selecionar_pasta():
    root = Tk()
    root.withdraw()
    pasta = filedialog.askdirectory(title="Selecione a pasta com PDFs")
    return Path(pasta) if pasta else None


# =========================
# MAIN
# =========================
def main():
    pasta = selecionar_pasta()

    if not pasta:
        print("Nenhuma pasta selecionada.")
        return

    arquivos = list(pasta.glob("*.pdf"))

    print(f"\n Processando {len(arquivos)} arquivos...\n")

    for arquivo in arquivos:
        processar_pdf(arquivo)

    print("\n Finalizado!")
    print(f" Log: {LOG_FILE}")
    print(f" Erros: {CSV_ERROS}")


if __name__ == "__main__":
    main()