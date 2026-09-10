"""Helpers compartilhados pelos testes de contrato dos workflows.

Os workflows reutilizaveis desta organizacao sao consumidos por outros
repositorios via `uses: veiculando/.github/.github/workflows/X@main`. Um erro
aqui nao quebra este repositorio: quebra o pipeline de quem chama, no momento
do deploy. Por isso o contrato e testado aqui, na origem.
"""

from pathlib import Path

import pytest
import yaml

RAIZ = Path(__file__).resolve().parents[1]
DIR_WORKFLOWS = RAIZ / ".github" / "workflows"


def carregar(nome: str) -> dict:
    """Carrega um workflow pelo nome do arquivo."""
    return yaml.safe_load((DIR_WORKFLOWS / nome).read_text(encoding="utf-8"))


def texto(nome: str) -> str:
    return (DIR_WORKFLOWS / nome).read_text(encoding="utf-8")


def gatilhos(wf: dict) -> dict:
    """Devolve o bloco de gatilhos.

    PyYAML segue YAML 1.1, onde `on:` sem aspas vira o booleano True. Todo
    acesso a esse bloco passa por aqui para nao depender de qual das duas
    chaves o arquivo produziu.
    """
    if "on" in wf:
        return wf["on"]
    return wf[True]


def entradas(wf: dict) -> dict:
    return gatilhos(wf)["workflow_call"].get("inputs") or {}


@pytest.fixture(scope="session")
def dir_workflows() -> Path:
    return DIR_WORKFLOWS
