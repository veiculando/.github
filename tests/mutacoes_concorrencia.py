"""Confere que os testes de TestODeployToleraConcorrencia tem dentes.

Um teste que passa com o arquivo certo E com o errado nao protege nada, e
descobrir isso costuma acontecer tarde: no proximo conflito real, com um
deploy pela metade. Aqui cada invariante da classe e violado de proposito e
o teste correspondente TEM que reprovar.

Nao roda sob pytest (nao se chama `test_*`): cada caso copia o repositorio e
invoca pytest de novo, o que e lento demais para a suite normal. Rode a mao
ou pelo CI:

    python tests/mutacoes_concorrencia.py
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
WF = Path(".github/workflows/_deploy-vm.yml")
CLASSE = "tests/test_workflow_contract.py::TestODeployToleraConcorrencia"


def mover_invocador(c: str) -> str:
    """Recorta o passo do invocador e o reinsere depois de quem ja o usa."""
    inicio = c.index("      - name: Preparar o invocador de run-command\n")
    fim = c.index("      - name: Login no Azure (OIDC)\n")
    bloco = c[inicio:fim]
    resto = c[:inicio] + c[fim:]
    destino = resto.index("      - name: Registrar o digest atual\n")
    return resto[:destino] + bloco + resto[destino:]


CASOS = [
    (
        "concurrency removido",
        lambda c: re.sub(
            r"    concurrency:\n      group:[^\n]*\n      cancel-in-progress:[^\n]*\n",
            "", c),
        "test_o_job_serializa_por_vm",
    ),
    (
        "cancel-in-progress ligado",
        lambda c: c.replace("cancel-in-progress: false", "cancel-in-progress: true"),
        "test_deploys_enfileiram_em_vez_de_cancelar",
    ),
    (
        "uma invocacao escapa do invocador",
        lambda c: c.replace(
            'saida=$(bash "$INVOCADOR" "$RG" "$VM" "$roteiro")',
            'saida=$(az vm run-command invoke -g "$RG" -n "$VM" --scripts "$roteiro")',
            1),
        "test_nenhuma_invocacao_escapa_do_invocador",
    ),
    (
        "guarda do Conflict removida",
        lambda c: c.replace(
            "if ! printf '%s' \"$saida\" | grep -qE 'Conflict|execution is in progress'; then",
            "if false; then"),
        "test_so_o_conflito_e_retentado",
    ),
    (
        "teto de tentativas removido",
        lambda c: c.replace('MAX="${INVOCAR_TENTATIVAS:-10}"', "MAX=999999"),
        "test_o_retry_tem_teto",
    ),
    (
        "invocador movido para depois do primeiro uso",
        mover_invocador,
        "test_o_invocador_e_escrito_antes_do_primeiro_uso",
    ),
]


def aplicar(mutacao, teste: str) -> str:
    tmp = tempfile.mkdtemp()
    try:
        destino = Path(tmp) / "repo"
        shutil.copytree(
            RAIZ, destino,
            ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"),
        )
        alvo = destino / WF
        corpo = alvo.read_text(encoding="utf-8").replace("\r\n", "\n")
        novo = mutacao(corpo)
        if novo == corpo:
            return "MUTACAO INERTE"
        alvo.write_text(novo, encoding="utf-8", newline="")
        r = subprocess.run(
            [sys.executable, "-m", "pytest", f"{CLASSE}::{teste}", "-q"],
            cwd=destino, capture_output=True, text=True,
        )
        return "pegou" if r.returncode != 0 else "NAO PEGOU"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ok = 0
    for nome, mutacao, teste in CASOS:
        resultado = aplicar(mutacao, teste)
        print(f"{resultado:14} {nome}")
        ok += resultado == "pegou"
    print()
    print(f"{ok}/{len(CASOS)} invariantes reprovaram a mutacao")
    return 0 if ok == len(CASOS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
