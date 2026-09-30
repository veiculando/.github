"""Contrato do workflow preview-logs (VEI-RD-102).

Cada classe corresponde a um cenario BDD do card. O workflow executa algo
numa VM com identidade privilegiada; o que se defende aqui e que ele so le
logs, so de servicos conhecidos, e nunca devolve segredo em claro.
"""

import os
import re
import shutil
import subprocess

import pytest

from conftest import RAIZ, carregar, gatilhos, texto

WF = "preview-logs.yml"
REMOTO = RAIZ / ".github" / "scripts" / "preview-logs-remote.sh"
WHITELIST = ["bff", "exibidora", "app", "edge", "core-api", "migrator"]
BASH = shutil.which("bash")


def passos():
    return carregar(WF)["jobs"]["logs"]["steps"]


def indice(pred):
    for i, p in enumerate(passos()):
        if pred(p):
            return i
    raise AssertionError("passo nao encontrado")


def validar(**inputs):
    """Roda o passo de validacao do workflow com os inputs dados."""
    passo = passos()[indice(lambda p: p.get("name", "").startswith("Validar"))]
    env = {**os.environ, "SERVICO": "bff", "SINCE": "30m", "TAIL": "500", "TRACE": ""}
    env.update({k.upper(): v for k, v in inputs.items()})
    return subprocess.run([BASH, "-c", passo["run"]], env=env, capture_output=True, text=True)


def remoto(*args):
    return subprocess.run([BASH, str(REMOTO), *args], capture_output=True, text=True)


def mascarar(linha: str) -> str:
    r = subprocess.run(
        [BASH, "-c", f'PREVIEW_LOGS_SOURCE_ONLY=1 . "{REMOTO.as_posix()}"; mascarar'],
        input=linha, capture_output=True, text=True, check=True)
    return r.stdout


class TestGatilhoEInputs:
    def test_so_dispara_manualmente(self):
        assert set(gatilhos(carregar(WF))) == {"workflow_dispatch"}

    def test_servico_e_choice_com_a_whitelist(self):
        servico = gatilhos(carregar(WF))["workflow_dispatch"]["inputs"]["servico"]
        assert servico["type"] == "choice"
        assert servico["options"] == WHITELIST

    def test_autentica_por_oidc(self):
        assert carregar(WF)["permissions"] == {"contents": "read", "id-token": "write"}


@pytest.mark.skipif(BASH is None, reason="bash indisponivel")
class TestServicoForaDaWhitelistFalhaSemTocarAVm:
    """BDD: servico fora da whitelist falha sem executar nada na VM."""

    def test_validacao_vem_antes_do_login_no_azure(self):
        validacao = indice(lambda p: p.get("name", "").startswith("Validar"))
        login = indice(lambda p: str(p.get("uses", "")).startswith("azure/login"))
        assert validacao < login

    @pytest.mark.parametrize("entrada", [
        {"servico": "db"},
        {"servico": "bff; rm -rf /"},
        {"since": "30m; id"},
        {"since": "$(id)"},
        {"tail": "5001"},
        {"tail": "0"},
        {"tail": "10 && id"},
        {"trace": "abc|id"},
        {"trace": "$(whoami)"},
    ])
    def test_input_invalido_reprova(self, entrada):
        assert validar(**entrada).returncode != 0

    @pytest.mark.parametrize("servico", WHITELIST)
    def test_input_valido_passa(self, servico):
        r = validar(servico=servico, trace="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
        assert r.returncode == 0, r.stdout + r.stderr

    @pytest.mark.parametrize("args", [
        ["SERVICO=db", "SINCE=30m", "TAIL=10", "TRACE="],
        ["SERVICO=bff", "SINCE=1d", "TAIL=10", "TRACE="],
        ["SERVICO=bff", "SINCE=30m", "TAIL=9999", "TRACE="],
        ["SERVICO=bff", "SINCE=30m", "TAIL=10", "TRACE=a b"],
    ])
    def test_script_remoto_revalida_antes_do_docker(self, args):
        r = remoto(*args)
        assert r.returncode == 64
        assert "PREVIEW_LOGS=invalid-input" in r.stdout


    def test_script_remoto_aceita_trace_omitido(self):
        # O Azure descarta o parametro TRACE= vazio; com 3 argumentos o script
        # tem de seguir ate a validacao, nao morrer com "unbound variable".
        r = remoto("SERVICO=db", "SINCE=30m", "TAIL=10")
        assert r.returncode == 64, r.stdout + r.stderr
        assert "PREVIEW_LOGS=invalid-input servico" in r.stdout


class TestNenhumInputViraComando:
    """BDD: nao ha parametro que permita comando arbitrario na VM."""

    def test_inputs_nunca_sao_interpolados_em_run(self):
        for p in passos():
            assert "${{" not in p.get("run", ""), f"expressao dentro de run: {p.get('name')}"

    def test_script_remoto_e_o_arquivo_versionado(self):
        run = passos()[indice(lambda p: p.get("name") == "Ler logs na VM")]["run"]
        assert "--scripts @.github/scripts/preview-logs-remote.sh" in run
        assert len(re.findall(r"--scripts", run)) == 1

    def test_script_remoto_nao_le_nem_altera_o_stack(self):
        codigo = "\n".join(l for l in REMOTO.read_text(encoding="utf-8").splitlines()
                           if not l.lstrip().startswith("#"))
        for proibido in [".env", "stack.env", "docker-compose", "compose up", "compose down",
                         "eval", "rm ", "docker exec", "docker run", "docker stop", "docker rm"]:
            assert proibido not in codigo, proibido


@pytest.mark.skipif(BASH is None, reason="bash indisponivel")
class TestSegredosSaemMascarados:
    """BDD: 'Authorization: Bearer x.y.z' sai mascarado."""

    @pytest.mark.parametrize("linha, segredo", [
        ("Authorization: Bearer x.y.z", "x.y.z"),
        ("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln fim", "eyJhbGciOiJIUzI1NiJ9"),
        ("Server=db;User Id=sa;Password=S3nh@Forte;Encrypt=True", "S3nh@Forte"),
        ("pwd=abc123;", "abc123"),
        ("DefaultEndpointsProtocol=https;AccountKey=QmFzZTY0S2V5PT0=;", "QmFzZTY0S2V5PT0="),
        ('{"access_token":"segredo-opaco"}', "segredo-opaco"),
    ])
    def test_mascara(self, linha, segredo):
        saida = mascarar(linha)
        assert segredo not in saida
        assert "***" in saida

    def test_nao_mascara_traceid(self):
        trace = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        assert trace in mascarar(f"Erro nao tratado traceId={trace}")


class TestSaida:
    def test_publica_artifact_e_exige_marcador(self):
        t = texto(WF)
        assert "actions/upload-artifact@v4" in t
        assert "preview-logs.txt" in t
        assert "grep -Fq 'PREVIEW_LOGS=ok'" in t
