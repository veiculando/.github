"""Contrato dos workflows reutilizaveis de build e deploy (VEI-RD-74).

Cada teste corresponde a um cenario BDD do card. O que eles defendem, em uma
frase: producao precisa poder responder "de qual commit eu vim?", e o deploy
nao pode resolver uma tag que se move debaixo dele.
"""

import re

import pytest
import yaml

from conftest import DIR_WORKFLOWS, carregar, entradas, gatilhos, texto

BUILD = "_docker-build-push.yml"
DEPLOY = "_deploy-vm.yml"

# `uses: veiculando/.github/.github/workflows/<arquivo>@<ref>`
REF_INTERNA = re.compile(
    r"uses:\s*veiculando/\.github/\.github/workflows/(?P<arquivo>[\w.-]+)@"
)


class TestCalleesExistem:
    """BDD: os workflows chamados pelo `docker.yml` existem.

    A unica execucao do `docker.yml` (run #1, 20/08) falhou porque chamava dois
    workflows que nunca foram escritos. Um chamador sem chamado.
    """

    @pytest.mark.parametrize("nome", [BUILD, DEPLOY])
    def test_arquivo_existe(self, nome):
        assert (DIR_WORKFLOWS / nome).is_file(), (
            f"{nome} nao existe; qualquer repositorio que o chame falha no "
            f"momento em que o workflow e resolvido"
        )

    @pytest.mark.parametrize("nome", [BUILD, DEPLOY])
    def test_e_reutilizavel(self, nome):
        assert "workflow_call" in gatilhos(carregar(nome)), (
            f"{nome} precisa declarar `on: workflow_call` para ser chamavel"
        )

    def test_toda_referencia_interna_aponta_para_arquivo_existente(self):
        """Nenhum workflow deste repositorio chama um irmao inexistente."""
        quebradas = []
        for caminho in sorted(DIR_WORKFLOWS.glob("*.yml")):
            for linha in caminho.read_text(encoding="utf-8").splitlines():
                # Comentarios citam o formato `uses:` para explica-lo; aqui so
                # contam as chamadas de verdade.
                if linha.lstrip().startswith("#"):
                    continue
                m = REF_INTERNA.search(linha)
                if m and not (DIR_WORKFLOWS / m.group("arquivo")).is_file():
                    quebradas.append(f"{caminho.name} -> {m.group('arquivo')}")
        assert not quebradas, f"referencias quebradas: {quebradas}"


class TestBuildPublicaArtefatoRastreavel:
    """BDD: cada commit produz um artefato identificavel pelo seu SHA."""

    def test_declara_as_entradas_do_contrato(self):
        declaradas = entradas(carregar(BUILD))
        for esperada in ("image-name", "source-dir", "dockerfile", "acr-login-server"):
            assert esperada in declaradas, f"falta a entrada `{esperada}`"

    def test_expoe_o_digest_publicado(self):
        """Sem o digest de saida o chamador nao tem o que passar ao deploy."""
        saidas = gatilhos(carregar(BUILD))["workflow_call"].get("outputs") or {}
        assert "digest" in saidas

    def test_autentica_por_oidc(self):
        """`id-token: write` e o que permite login sem segredo de longa duracao."""
        assert carregar(BUILD).get("permissions", {}).get("id-token") == "write"

    def test_clona_e_constroi_no_diretorio_que_o_chamador_nomeia(self):
        """Mesma convencao do `_dotnet-ci.yml`, que clona em `app-dir`.

        O `docker.yml` do commit b3cfdde passa `source-dir: Veiculando` porque
        e assim que os workflows desta organizacao ja funcionam. Se o checkout
        cair na raiz e o `file` nao levar o prefixo, todo chamador que use a
        convencao deixa de achar o proprio Dockerfile.
        """
        corpo = texto(BUILD)
        assert "path: ${{ inputs.source-dir }}" in corpo
        assert "file: ${{ inputs.source-dir }}/${{ inputs.dockerfile }}" in corpo

    def test_marca_a_imagem_com_o_sha_completo(self):
        corpo = texto(BUILD)
        assert "sha-${{ github.sha }}" in corpo, (
            "a tag precisa derivar do SHA completo do commit; `github.sha` "
            "abreviado ou substituido por um contador reintroduz o `gh-NN` "
            "que nao rastreia nada"
        )


class TestPromocaoNaoRefaz_Build:
    """BDD: uma tag git republica a MESMA imagem, sem reconstruir.

    Reconstruir a partir do mesmo fonte nao garante o mesmo binario. Se a
    promocao rebuilda, a imagem homologada e a imagem promovida sao duas
    imagens diferentes com o mesmo nome.
    """

    def test_aceita_um_digest_de_origem(self):
        declaradas = entradas(carregar(BUILD))
        assert "promote-from-digest" in declaradas

    def test_aceita_as_tags_a_aplicar(self):
        declaradas = entradas(carregar(BUILD))
        assert "promote-tags" in declaradas

    def test_o_build_nao_roda_em_modo_promocao(self):
        """O passo de build precisa ser condicionado a ausencia do digest."""
        wf = carregar(BUILD)
        passos = wf["jobs"]["build-push"]["steps"]
        build = [p for p in passos if "build-push-action" in str(p.get("uses", ""))]
        assert build, "nao encontrei o passo de build"
        for passo in build:
            assert "promote-from-digest == ''" in str(passo.get("if", "")), (
                "o build precisa ser pulado quando ha digest de origem"
            )

    def test_a_promocao_usa_import_no_registry(self):
        """`az acr import` copia server-side: nao ha pull, build nem push."""
        assert "az acr import" in texto(BUILD)


class TestDeployNaoResolveTagMovel:
    """BDD: o deploy nunca resolve `latest` nem qualquer tag movel.

    Producao roda hoje `homolog-api:gh-20` e ninguem sabe de qual commit veio.
    O deploy tem que receber um digest, que e imutavel por construcao.
    """

    def test_recebe_digest_e_nao_tag(self):
        declaradas = entradas(carregar(DEPLOY))
        assert "image-digest" in declaradas
        assert "image-tag" not in declaradas, (
            "aceitar uma tag abre a porta para `latest` entrar por parametro"
        )

    def test_o_digest_e_obrigatorio(self):
        assert entradas(carregar(DEPLOY))["image-digest"]["required"] is True

    def test_nenhuma_tag_movel_no_corpo(self):
        corpo = texto(DEPLOY)
        for proibida in (":latest", ":main", ":develop"):
            assert proibida not in corpo, f"tag movel `{proibida}` no deploy"

    def test_valida_o_formato_do_digest(self):
        """Um digest malformado precisa reprovar antes de tocar a VM."""
        assert "sha256:" in texto(DEPLOY)


class TestTodosOsWorkflowsSaoValidos:
    """Rede de seguranca: um YAML quebrado aqui derruba todos os chamadores."""

    @pytest.mark.parametrize(
        "caminho", sorted(DIR_WORKFLOWS.glob("*.yml")), ids=lambda p: p.name
    )
    def test_yaml_parseia(self, caminho):
        wf = yaml.safe_load(caminho.read_text(encoding="utf-8"))
        assert isinstance(wf, dict)
        assert gatilhos(wf), f"{caminho.name} nao declara gatilhos"
