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
        # Conta so nas linhas que MONTAM roteiro para a VM. A mensagem de erro
        # do rollback tambem cita `export ${VARIAVEL}=`, ensinando o comando de
        # recuperacao manual - util para quem opera, e nao e um terceiro uso.
        roteiros = [
            l for l in texto(DEPLOY).splitlines()
            if l.lstrip().startswith("roteiro=")
        ]
        com_variavel = [l for l in roteiros if "export ${VARIAVEL}=" in l]
        assert len(com_variavel) == 2, (
            "troca e rollback devem exportar a variavel parametrizada "
            f"(encontrados {len(com_variavel)} roteiros)"
        )
    def test_o_nome_da_variavel_e_validado(self):
        """O nome entra numa string de shell montada aqui: sem validacao, um
        valor como `X; curl evil` viraria comando na VM."""
        assert "A-Za-z_" in texto(DEPLOY), (
            "falta validar o formato do nome da variavel antes de interpolar"
        )


class TestAVmSeAutenticaNoAcr:
    """BDD: o deploy autentica o docker da VM no ACR antes de puxar.

    Descoberto exercitando o deploy de verdade em 10/09/2026: a run ficou
    VERDE e o compose imprimiu `app2 Pulling / Pulled`, mas a VM nao estava
    autenticada. Funcionou so porque aquela imagem ja estava em cache local,
    puxada a mao numa verificacao anterior.

    Um `docker run` no mesmo ACR, num digest nao cacheado, devolvia
    `unauthorized`. O primeiro deploy de uma imagem realmente nova - um
    hotfix, tipicamente - falharia no pull.

    A VM tem AcrPull por managed identity, mas o daemon do docker nao usa
    RBAC: precisa de `docker login`. O token sai do IMDS e e trocado no
    endpoint oauth2/exchange do proprio ACR, entao nao ha segredo em lugar
    nenhum - nem no workflow, nem no script que fica gravado na VM.
    """

    def test_existe_passo_de_login_no_acr(self):
        assert "oauth2/exchange" in texto(DEPLOY), (
            "a VM nunca se autentica no ACR: o pull so funciona por cache"
        )

    def test_o_login_usa_a_identidade_gerenciada(self):
        corpo = texto(DEPLOY)
        assert "169.254.169.254" in corpo, (
            "o token tem que vir do IMDS, nao de segredo configurado"
        )

    def test_a_sessao_e_encerrada_ao_final(self):
        """`docker login` grava credencial em /root/.docker/config.json. Ela
        precisa sair quando o deploy termina, inclusive se ele falhar."""
        corpo = texto(DEPLOY)
        assert "docker logout" in corpo, "a sessao do ACR fica aberta na VM"
        assert "if: always()" in corpo, (
            "o logout precisa rodar mesmo quando o deploy falha"
        )

    def test_o_login_vem_antes_da_troca(self):
        # Sem os comentarios: eles CITAM `docker compose pull` ao explicar por
        # que o login existe, e a citacao vem antes do proprio login.
        util = "\n".join(
            l for l in texto(DEPLOY).splitlines()
            if not l.lstrip().startswith("#")
        )
        assert util.index("oauth2/exchange") < util.index("docker compose pull"), (
            "autenticar depois do pull nao serve para nada"
        )

class TestORollbackRestauraDeVerdade:
    """BDD: o rollback tem que restaurar a imagem, e provar que restaurou.

    Descoberto no ensaio de rollback de 10/09/2026. O passo relatou SUCESSO,
    o log disse "Voltando para sha256:d79b27ad..." e "Imagem revertida", e a
    producao continuou na imagem que acabara de reprovar no health.

    Duas causas somadas:

    1. A captura reduzia `RepoDigests[0]` a um digest cru. O que a VM tem em
       RepoDigests e `host/repo@sha256:...`; o grep jogava fora o host e o
       repo. O rollback exportava `FS_IMAGE_REF=sha256:...`, que nao e
       referencia de imagem, e o `docker compose up` falhava dentro da VM.

    2. O passo nao conferia nada. `az vm run-command invoke` devolve exito
       quando a INVOCACAO funciona, mesmo que o script dentro da VM falhe -
       por isso todo passo daqui procura uma sentinela na saida. O rollback
       era o unico que nao procurava, e imprimia "Imagem revertida" sempre.

    Um rollback que mente e pior que nao ter rollback: quem le o log acredita
    que producao voltou.
    """

    def test_a_captura_preserva_a_referencia_completa(self):
        corpo = texto(DEPLOY)
        assert "@sha256:" in corpo, (
            "a captura precisa manter host/repo@digest, nao so o digest"
        )
        assert "grep -oE 'sha256:[0-9a-f]{64}'" not in corpo, (
            "este padrao descarta host e repo: o rollback fica sem referencia"
        )

    def test_o_rollback_confere_o_resultado(self):
        assert "ROLLBACK_OK" in texto(DEPLOY), (
            "o rollback nao verifica se a imagem voltou"
        )

    def test_toda_sentinela_e_procurada_na_saida(self):
        """`az vm run-command invoke` devolve exito mesmo quando o script
        dentro da VM falha. Sem procurar sentinela, o passo mente."""
        corpo = texto(DEPLOY)
        for sentinela in ("LOGIN_ACR_OK", "MIGRACAO_OK", "HEALTH_OK", "ROLLBACK_OK"):
            assert corpo.count(sentinela) >= 2, (
                f"{sentinela} e emitida mas nunca procurada na saida"
            )

    def test_nao_afirma_reversao_sem_confirmar(self):
        util = "\n".join(
            l for l in texto(DEPLOY).splitlines()
            if not l.lstrip().startswith("#")
        )
        assert "::error::Imagem revertida. O SCHEMA NAO foi revertido." not in util, (
            "afirmacao incondicional de que a imagem voltou"
        )


class TestODeployToleraConcorrencia:
    """BDD: duas execucoes na mesma VM nao podem derrubar uma a outra.

    Descoberto em 11/09/2026, no primeiro deploy do Veiculando.Web. A extensao
    run-command aceita UMA execucao por vez por VM; uma consulta paralela
    segurava a extensao, e o passo "Trocar o container e conferir saude"
    recebeu `(Conflict) Run command extension execution is in progress`.

    O rollback funcionou e producao ficou intacta - mas a run acusou falha de
    HEALTH. A aplicacao nunca chegou a ser consultada: o roteiro foi recusado
    antes de rodar. O diagnostico manda quem investiga procurar defeito na
    imagem, que e o lugar errado, e isso custa mais caro que a falha.

    Nao e hipotese remota: com os cinco front-ends entrando no pipeline, dois
    merges proximos na mesma VM ja bastam.
    """

    def test_o_job_serializa_por_vm(self):
        job = carregar(DEPLOY)["jobs"]["deploy"]
        grupo = (job.get("concurrency") or {}).get("group", "")
        assert "inputs.vm-name" in grupo and "inputs.resource-group" in grupo, (
            "sem concurrency por VM, dois deploys na mesma maquina se atropelam"
        )

    def test_deploys_enfileiram_em_vez_de_cancelar(self):
        job = carregar(DEPLOY)["jobs"]["deploy"]
        assert (job.get("concurrency") or {}).get("cancel-in-progress") is False, (
            "cancelar um deploy no meio da troca deixa o container em estado "
            "indefinido: ele precisa enfileirar, nao ser interrompido"
        )

    def test_nenhuma_invocacao_escapa_do_invocador(self):
        """A unica chamada direta legitima e a que vive DENTRO do invocador.

        Uma invocacao que escape nao tem retry, e derruba o deploy inteiro no
        primeiro conflito - com a mensagem do passo em que ela estiver.
        """
        util = [
            l for l in texto(DEPLOY).splitlines()
            if not l.lstrip().startswith("#")
        ]
        diretas = [l for l in util if "az vm run-command invoke" in l]
        assert len(diretas) == 1, (
            f"{len(diretas)} invocacoes diretas de run-command; esperado 1 "
            "(a de dentro do invocador). As demais nao tem retry."
        )

    def test_o_invocador_e_escrito_antes_do_primeiro_uso(self):
        passos = carregar(DEPLOY)["jobs"]["deploy"]["steps"]
        preparo = next(
            i for i, p in enumerate(passos)
            if p.get("name", "").startswith("Preparar o invocador")
        )
        uso = next(
            i for i, p in enumerate(passos)
            if 'bash "$INVOCADOR"' in (p.get("run") or "")
        )
        assert preparo < uso, "o invocador e usado antes de existir"

    def test_so_o_conflito_e_retentado(self):
        """Retentar um erro de permissao atrasa a falha e esconde a causa.

        Foi um `AuthorizationFailed` que travou o primeiro deploy da API: com
        retry cego, aquilo teria levado minutos para aparecer, e apareceria
        como timeout em vez de como falta de papel.
        """
        corpo = texto(DEPLOY)
        assert "execution is in progress" in corpo, (
            "o invocador precisa reconhecer o conflito da extensao"
        )
        assert re.search(r"grep -qE 'Conflict\|execution is in progress'", corpo), (
            "sem o guarda, qualquer erro entra no laco de retry"
        )

    def test_o_retry_tem_teto(self):
        corpo = texto(DEPLOY)
        assert "INVOCAR_TENTATIVAS" in corpo, "o numero de tentativas e fixo"
        assert "seguiu ocupada apos" in corpo, (
            "um laco sem teto prende o runner ate o timeout do job"
        )


class TestOBuildPodeDependerDoCore:
    """BDD: um repositorio cujo Dockerfile referencia o core consegue publicar.

    O BFF (Veiculando.WhiteLabel.Api) referencia cinco projetos do core por
    caminho relativo, e o Dockerfile espera `Veiculando/` como diretorio IRMAO.
    Era o unico dos tres repositorios WhiteLabel sem imagem publicada, e o
    efeito aparecia no preview: os fronts ganharam imagem rastreavel ao commit
    em 14/09/2026 e o BFF continuou num artefato de 27/08 publicado a mao, 13
    commits atras.

    Atualizar so os fronts poria codigo novo contra um BFF velho logo depois de
    uma sprint que mexeu em multi-tenant - quebraria parecendo defeito da
    sprint, e nao da montagem do ambiente.

    O contrato copia o do `_dotnet-ci.yml`, que ja resolvia isto para a
    compilacao. Dois contratos diferentes para a mesma necessidade seria pior
    que o problema.
    """

    def test_o_contexto_muda_quando_o_core_entra(self):
        """Com o core ao lado, o contexto tem que ser a raiz do workspace.

        Restrito a `source-dir`, o docker nao enxerga o diretorio irmao e o
        build morre no COPY com "file not found" - erro que nao diz nada sobre
        a causa e manda procurar defeito no Dockerfile.
        """
        corpo = texto(BUILD)
        linha = [l for l in corpo.splitlines() if l.strip().startswith("context:")]
        assert linha, "o passo de build nao declara contexto"
        assert "needs-core" in linha[0], (
            "o contexto e fixo em source-dir: com needs-core o COPY do core falha"
        )

    def test_o_pin_do_core_e_validado(self):
        """Nome de branch ou SHA abreviado nao garante build reproduzivel."""
        corpo = texto(BUILD)
        assert re.search(r"\[0-9a-f\]\{40\}", corpo), (
            "o pin do core nao e validado como SHA completo de 40 hex"
        )

    def test_o_core_e_fixado_pelo_arquivo_e_nao_por_branch(self):
        wf = carregar(BUILD)
        passos = wf["jobs"]["build-push"]["steps"]
        ck = [p for p in passos if "core na ref fixada" in (p.get("name") or "")]
        assert ck, "falta o checkout do core"
        assert "steps.coreref.outputs.sha" in ck[0]["with"]["ref"], (
            "o core precisa vir da ref resolvida do arquivo, nao de um branch"
        )

    def test_a_falta_do_token_falha_cedo_e_explicada(self):
        """Sem o guard, a falha aparece la na frente como COPY sem arquivo."""
        corpo = texto(BUILD)
        assert "Secret 'core-repo-token' ausente" in corpo, (
            "falta o guard de token; a falha apareceria no docker, sem causa"
        )

    def test_o_token_do_core_e_opcional(self):
        """Repositorio autocontido nao pode ser obrigado a informar o token."""
        wf = carregar(BUILD)
        segredos = gatilhos(wf)["workflow_call"]["secrets"]
        assert segredos["core-repo-token"].get("required") is not True, (
            "core-repo-token obrigatorio quebraria quem nao depende do core"
        )

    def test_o_core_nao_entra_na_promocao(self):
        """Promocao nao constroi nada: re-etiqueta um digest ja publicado."""
        wf = carregar(BUILD)
        passos = wf["jobs"]["build-push"]["steps"]
        for p in passos:
            if "core" in (p.get("name") or "").lower():
                assert "promote-from-digest == ''" in p.get("if", ""), (
                    f"'{p['name']}' roda na promocao, que nao faz checkout"
                )


class TestASondaAlcancaOServico:
    """BDD: servico sem porta publicada tambem precisa ser verificado.

    O preview roda `core-api` e `fs` sem publicar porta - o BFF os alcanca por
    DNS interno, e nada mais precisa. Uma sonda partindo do host falharia por
    REDE, nao por saude: o deploy correto reprovaria e o rollback desfaria uma
    troca que estava boa.

    Publicar porta so para o health seria pior: o preview ja esta alcancavel em
    HTTP puro por fora do Cloudflare (VEI-SUP-15), e a resposta nao e abrir
    mais uma.
    """

    def test_a_sonda_pode_rodar_dentro_do_container(self):
        corpo = texto(DEPLOY)
        assert "docker exec" in corpo and "health-container" in corpo, (
            "nao ha como verificar servico sem porta publicada"
        )

    def test_o_roteiro_usa_a_sonda_resolvida(self):
        """Curl fixo no roteiro ignoraria health-container em silencio."""
        corpo = texto(DEPLOY)
        assert "if ${sonda}; then echo HEALTH_OK" in corpo, (
            "o roteiro nao usa a sonda resolvida: health-container seria inerte"
        )

    def test_o_nome_do_container_da_sonda_e_validado(self):
        """O nome entra numa string de shell montada e rodaria como root."""
        corpo = texto(DEPLOY)
        assert re.search(r"health-container invalido", corpo), (
            "sem validacao, um valor como 'x; curl evil' vira comando na VM"
        )

    def test_quem_publica_porta_nao_e_afetado(self):
        wf = carregar(DEPLOY)
        assert entradas(wf)["health-container"]["default"] == "", (
            "o default precisa ser vazio: api, fs e web sondam do host"
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
