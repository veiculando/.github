"""Confere que os invariantes dos workflows reutilizaveis tem dentes.

Um teste que passa com o arquivo certo E com o errado nao protege nada, e
descobrir isso costuma acontecer tarde: no proximo conflito real, com um deploy
pela metade. Aqui cada invariante e violado de proposito e o teste
correspondente TEM que reprovar.

Nao roda sob pytest (nao se chama `test_*`): cada caso copia o repositorio e
invoca pytest de novo, o que e lento demais para a suite normal. Rode a mao ou
pelo CI:

    python tests/mutacoes.py
"""

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
DEPLOY = Path(".github/workflows/_deploy-vm.yml")
BUILD = Path(".github/workflows/_docker-build-push.yml")
CONC = "tests/test_workflow_contract.py::TestODeployToleraConcorrencia"
CORE = "tests/test_workflow_contract.py::TestOBuildPodeDependerDoCore"
SONDA = "tests/test_workflow_contract.py::TestASondaAlcancaOServico"


def mover_invocador(c: str) -> str:
    """Recorta o passo do invocador e o reinsere depois de quem ja o usa."""
    inicio = c.index("      - name: Preparar o invocador de run-command\n")
    fim = c.index("      - name: Login no Azure (OIDC)\n")
    bloco = c[inicio:fim]
    resto = c[:inicio] + c[fim:]
    destino = resto.index("      - name: Registrar o digest atual\n")
    return resto[:destino] + bloco + resto[destino:]


def core_na_promocao(c: str) -> str:
    """Faz o checkout do core rodar tambem no modo promocao, que nao clona."""
    return c.replace(
        "        if: inputs.promote-from-digest == '' && inputs.needs-core\n"
        "        uses: actions/checkout@v4\n"
        "        with:\n"
        "          repository: ${{ inputs.core-repo }}",
        "        if: inputs.needs-core\n"
        "        uses: actions/checkout@v4\n"
        "        with:\n"
        "          repository: ${{ inputs.core-repo }}",
        1,
    )


CASOS = [
    # ---- concorrencia no deploy -------------------------------------------
    (DEPLOY, CONC, "concurrency removido",
     lambda c: re.sub(
         r"    concurrency:\n      group:[^\n]*\n      cancel-in-progress:[^\n]*\n",
         "", c),
     "test_o_job_serializa_por_vm"),
    (DEPLOY, CONC, "cancel-in-progress ligado",
     lambda c: c.replace("cancel-in-progress: false", "cancel-in-progress: true"),
     "test_deploys_enfileiram_em_vez_de_cancelar"),
    (DEPLOY, CONC, "uma invocacao escapa do invocador",
     lambda c: c.replace(
         'saida=$(bash "$INVOCADOR" "$RG" "$VM" "$roteiro")',
         'saida=$(az vm run-command invoke -g "$RG" -n "$VM" --scripts "$roteiro")',
         1),
     "test_nenhuma_invocacao_escapa_do_invocador"),
    (DEPLOY, CONC, "guarda do Conflict removida",
     lambda c: c.replace(
         "if ! printf '%s' \"$saida\" | grep -qE 'Conflict|execution is in progress'; then",
         "if false; then"),
     "test_so_o_conflito_e_retentado"),
    (DEPLOY, CONC, "teto de tentativas removido",
     lambda c: c.replace('MAX="${INVOCAR_TENTATIVAS:-10}"', "MAX=999999"),
     "test_o_retry_tem_teto"),
    (DEPLOY, CONC, "invocador movido para depois do primeiro uso",
     mover_invocador,
     "test_o_invocador_e_escrito_antes_do_primeiro_uso"),

    # ---- sonda de saude ----------------------------------------------------
    (DEPLOY, SONDA, "roteiro volta a ter curl fixo",
     lambda c: c.replace("if ${sonda}; then echo HEALTH_OK",
                         "if curl -fsS -o /dev/null '${URL}'; then echo HEALTH_OK"),
     "test_o_roteiro_usa_a_sonda_resolvida"),
    (DEPLOY, SONDA, "nome do container da sonda sem validacao",
     lambda c: c.replace(
         '''              echo "::error::health-container invalido: $CONTAINER_SONDA"''',
         '''              echo "::error::valor estranho"'''),
     "test_o_nome_do_container_da_sonda_e_validado"),
    (DEPLOY, SONDA, "sonda no container vira o padrao",
     lambda c: c.replace(
         "        default: ''\n      health-retries:",
         "        default: 'algum-container'\n      health-retries:"),
     "test_quem_publica_porta_nao_e_afetado"),

    # ---- build que depende do core ----------------------------------------
    (BUILD, CORE, "contexto fixo em source-dir",
     lambda c: c.replace(
         "context: ${{ inputs.needs-core && '.' || inputs.source-dir }}",
         "context: ${{ inputs.source-dir }}"),
     "test_o_contexto_muda_quando_o_core_entra"),
    (BUILD, CORE, "pin do core aceita qualquer coisa",
     lambda c: c.replace("grep -Eq '^[0-9a-f]{40}$'", "grep -q ''"),
     "test_o_pin_do_core_e_validado"),
    (BUILD, CORE, "core fixado num branch movel",
     lambda c: c.replace(
         "ref: ${{ steps.coreref.outputs.sha }}", "ref: main"),
     "test_o_core_e_fixado_pelo_arquivo_e_nao_por_branch"),
    (BUILD, CORE, "guard de token removido",
     lambda c: c.replace("Secret 'core-repo-token' ausente", "tudo certo"),
     "test_a_falta_do_token_falha_cedo_e_explicada"),
    (BUILD, CORE, "token do core vira obrigatorio",
     lambda c: c.replace(
         "      core-repo-token:\n        description: >-\n"
         "          Token de leitura do core. Exigido apenas quando `needs-core` e true.\n"
         "        required: false",
         "      core-repo-token:\n        description: >-\n"
         "          Token de leitura do core. Exigido apenas quando `needs-core` e true.\n"
         "        required: true"),
     "test_o_token_do_core_e_opcional"),
    (BUILD, CORE, "checkout do core roda na promocao",
     core_na_promocao,
     "test_o_core_nao_entra_na_promocao"),
]


def aplicar(alvo, classe, mutacao, teste):
    tmp = tempfile.mkdtemp()
    try:
        destino = Path(tmp) / "repo"
        shutil.copytree(
            RAIZ, destino,
            ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"),
        )
        arq = destino / alvo
        corpo = arq.read_text(encoding="utf-8").replace("\r\n", "\n")
        novo = mutacao(corpo)
        if novo == corpo:
            return "MUTACAO INERTE"
        arq.write_text(novo, encoding="utf-8", newline="")
        r = subprocess.run(
            [sys.executable, "-m", "pytest", f"{classe}::{teste}", "-q"],
            cwd=destino, capture_output=True, text=True,
        )
        return "pegou" if r.returncode != 0 else "NAO PEGOU"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ok = 0
    for alvo, classe, nome, mutacao, teste in CASOS:
        resultado = aplicar(alvo, classe, mutacao, teste)
        print(f"{resultado:14} {nome}")
        ok += resultado == "pegou"
    print()
    print(f"{ok}/{len(CASOS)} invariantes reprovaram a mutacao")
    return 0 if ok == len(CASOS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
