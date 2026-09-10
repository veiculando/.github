"""Contrato dos workflows reutilizaveis de build e deploy (VEI-RD-74).

Cada teste corresponde a um cenario BDD do card. O que eles defendem, em uma
frase: producao precisa poder responder "de qual commit eu vim?", e o deploy
nao pode resolver uma tag que se move debaixo dele.
"""

import re
import subprocess

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


    def test_registra_o_digest_no_resumo_da_execucao(self):
        """O digest tem que sobreviver ao log, que expira.

        Rollback e redeployar um digest conhecido. Se o unico registro do
        digest for a saida de um passo, dali a algumas semanas a resposta para
        "para onde eu volto?" volta a ser a mesma de hoje: ninguem sabe.
        """
        assert "GITHUB_STEP_SUMMARY" in texto(BUILD)


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

    def test_o_source_do_import_e_qualificado_com_o_login_server(self):
        """`az acr import` recusa `repo@digest` mesmo na propria registry.

        A mensagem que ele devolve - "Source cannot be found. Please
        provide a valid image and source registry or a fully qualified
        source" - nao diz que o que falta e o login server. Foi o que
        reprovou a promocao da v1.0.0 DEPOIS de o login OIDC ja ter
        passado, que e o pior lugar para descobrir isso.
        """
        assert '--source "$SERVER/$IMAGE@$DIGEST"' in texto(BUILD)

    def test_todas_as_tags_da_promocao_sao_processadas(self, tmp_path):
        """Executa o laco de tags de verdade, em vez de olhar o texto.

        A promocao da v1.0.0 ficou VERDE e mesmo assim nao aplicou `latest`:
        `printf '%s'` nao emite newline final, `read` devolve falso no EOF, e
        o corpo do laco nunca roda para o ultimo item. Perder uma tag em
        silencio e pior que falhar - a run diz que promoveu.

        Os dois defeitos anteriores tambem passaram por assert de texto. Este
        roda o comando.

        O script vai para arquivo e e chamado por nome relativo: `bash -c`
        com caminho absoluto tem os argumentos remontados pelo MSYS no
        Windows, e o teste falharia por ambiente, nao por defeito.
        """
        linha = next(
            l for l in texto(BUILD).splitlines() if "while read -r tag" in l
        )
        prefixo = linha.strip().split("while read")[0]
        roteiro = tmp_path / "laco.sh"
        roteiro.write_text(
            'TAGS="1.0.0,latest"\n'
            + prefixo
            + 'while read -r tag; do echo "$tag"; done\n',
            encoding="utf-8",
            newline="\n",
        )
        saida = subprocess.run(
            ["bash", "laco.sh"], cwd=tmp_path, capture_output=True, text=True
        )
        assert saida.stdout.split() == ["1.0.0", "latest"], (
            f"o laco emitiu {saida.stdout.split()!r}; stderr={saida.stderr!r}"
        )

    def test_a_promocao_usa_import_no_registry(self):
        """`az acr import` copia server-side: nao ha pull, build nem push."""
        assert "az acr import" in texto(BUILD)


    def test_nenhum_input_de_um_so_modo_e_obrigatorio(self):
        """O GitHub valida `required` no STARTUP, antes de qualquer `if`.

        Este workflow serve dois modos. Um input exigido apenas pelo build -
        `dockerfile` - marcado como required faz a PROMOCAO nem iniciar:
        `startup_failure` em 0s, sem log de passo nenhum. Foi exatamente o que
        aconteceu com a tag v1.0.0. So o que os DOIS modos usam pode ser
        obrigatorio; o resto e validado em runtime, onde o modo ja e conhecido.
        """
        declaradas = entradas(carregar(BUILD))
        obrigatorias = {n for n, d in declaradas.items() if d.get("required")}
        assert obrigatorias == {"image-name", "acr-login-server"}, (
            f"obrigatorios de mais: {obrigatorias}"
        )

    def test_o_modo_build_exige_dockerfile_em_runtime(self):
        """Tirar o `required` nao pode virar falhar tarde, dentro do docker."""
        corpo = texto(BUILD)
        assert "dockerfile" in corpo and "::error::" in corpo
        assert 'DOCKERFILE' in corpo, "a validacao precisa ler o input"


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


class TestOEnvFileDaMigracaoEParametro:
    """BDD: o gate de migracao nao pode fixar o nome do arquivo de ambiente.

    O passo lia `--env-file .env`. Medido na VM em 10/09/2026, o diretorio do
    stack de producao tem `stack.env` e NAO tem `.env` - o gate falharia na
    primeira execucao, e a falha diria "no such file", nao "migracao reprovou".

    O nome do arquivo e propriedade de quem opera a VM, nao deste workflow.
    """

    def test_o_env_file_e_uma_entrada_declarada(self):
        assert "env-file" in entradas(carregar(DEPLOY)), (
            "o nome do arquivo de ambiente tem que vir de quem chama"
        )

    def test_nao_ha_env_file_fixo_no_corpo(self):
        assert "--env-file .env " not in texto(DEPLOY), (
            "`.env` fixo no corpo: o stack de producao usa `stack.env`"
        )

    def test_o_gate_usa_a_entrada(self):
        corpo = texto(DEPLOY)
        assert "inputs.env-file" in corpo, (
            "a entrada foi declarada mas o gate continua ignorando-a"
        )

    def test_o_gate_reprova_cedo_se_o_env_file_faltar(self):
        """Sem a guarda, o erro vira `no such file` vindo de dentro do
        container - indistinguivel de uma migracao quebrada de verdade."""
        corpo = texto(DEPLOY)
        assert 'if [ -z "${ENVFILE:-}" ]; then' in corpo, (
            "a guarda que reprova antes de tocar a VM sumiu"
        )


class TestAVariavelDeImagemEParametro:
    """BDD: o nome da variavel que o compose interpola vem de quem chama.

    O compose de producao usa uma variavel POR SERVICO (`API_IMAGE_REF`,
    `FS_IMAGE_REF`) porque o stack tem dois servicos e um nome compartilhado
    faria um deploy renderizar o outro servico errado.

    Com `export IMAGE_REF` fixo aqui, o deploy exportaria uma variavel que o
    compose IGNORA: o servico cairia no default e a run ficaria VERDE tendo
    implantado a imagem antiga - o health passa, porque a imagem antiga e
    saudavel. Falha silenciosa, da mesma familia da tag `latest` perdida.
    """

    def test_o_nome_da_variavel_e_uma_entrada(self):
        assert "image-ref-var" in entradas(carregar(DEPLOY)), (
            "o nome da variavel do compose tem que vir de quem chama"
        )

    def test_nao_ha_nome_de_variavel_fixo(self):
        assert "export IMAGE_REF=" not in texto(DEPLOY), (
            "IMAGE_REF fixo: o compose de producao le API_IMAGE_REF/FS_IMAGE_REF"
        )

    def test_a_troca_e_o_rollback_usam_a_mesma_entrada(self):
        """Rollback exportando outra variavel deixaria producao na imagem que
        acabou de reprovar no health."""
        corpo = texto(DEPLOY)
        assert corpo.count("export ${VARIAVEL}=") == 2, (
            "troca e rollback devem exportar a variavel parametrizada"
        )

    def test_o_nome_da_variavel_e_validado(self):
        """O nome entra numa string de shell montada aqui: sem validacao, um
        valor como `X; curl evil` viraria comando na VM."""
        assert "A-Za-z_" in texto(DEPLOY), (
            "falta validar o formato do nome da variavel antes de interpolar"
        )


class TestTodosOsWorkflowsSaoValidos:
    """Rede de seguranca: um YAML quebrado aqui derruba todos os chamadores."""

    @pytest.mark.parametrize(
        "caminho", sorted(DIR_WORKFLOWS.glob("*.yml")), ids=lambda p: p.name
    )
    def test_yaml_parseia(self, caminho):
        wf = yaml.safe_load(caminho.read_text(encoding="utf-8"))
        assert isinstance(wf, dict)
        assert gatilhos(wf), f"{caminho.name} nao declara gatilhos"
