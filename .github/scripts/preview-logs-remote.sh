# Script FIXO executado na VM de preview pelo workflow preview-logs.yml
# (VEI-RD-102). Somente leitura: le logs de um container do stack
# `veiculando-preview` e devolve a saida mascarada.
#
# O que este script NAO faz, de proposito:
# - nao le nem altera o compose nem o `.env`/stack.env do stack: o container e
#   achado pelos labels que o proprio compose grava, entao nenhum arquivo de
#   configuracao (e nenhum segredo nele) e aberto;
# - nao aceita comando: os quatro parametros sao dados, revalidados aqui mesmo
#   que o workflow ja os tenha validado, e nunca sao avaliados pelo shell.
#
# Parametros posicionais (formato do `az vm run-command --parameters`):
#   SERVICO=<bff|exibidora|app|edge|core> SINCE=<n[smh]> TAIL=<1..5000> TRACE=<[A-Za-z0-9-]*>

# Run Command usa /bin/sh; o resto do script precisa de bash.
if [ -z "${BASH_VERSION:-}" ]; then exec /usr/bin/env bash "$0" "$@"; fi
set -euo pipefail

# Mascara Bearer, JWT, senhas, chaves e connection strings. Aplicada antes de a
# saida deixar a VM: o que o Run Command devolve fica gravado no Azure.
mascarar() {
  sed -E \
    -e 's/(Bearer[[:space:]]+)[^[:space:]"'"'"',;]+/\1***/gI' \
    -e 's/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]*)?/***JWT***/g' \
    -e 's/((Password|Pwd|AccountKey|SharedAccessKey|SharedAccessSignature|Secret|ApiKey|access_token|refresh_token|client_secret)[[:space:]]*"?[=:][[:space:]]*"?)[^;"'"'"'&[:space:]]+/\1***/gI' \
    -e 's/(Authorization[[:space:]]*[:=][[:space:]]*)(Basic|Digest)[[:space:]]+[^[:space:]"]+/\1\2 ***/gI'
}

# Os testes de contrato carregam so a funcao acima.
if [[ "${PREVIEW_LOGS_SOURCE_ONLY:-}" == 1 ]]; then return 0 2>/dev/null || exit 0; fi

servico="${1#SERVICO=}"
since="${2#SINCE=}"
tail_n="${3#TAIL=}"
trace="${4#TRACE=}"

case "$servico" in
  bff|exibidora|app|edge|core) ;;
  *) echo "PREVIEW_LOGS=invalid-input servico"; exit 64 ;;
esac
[[ "$since" =~ ^[0-9]+[smh]$ ]] || { echo "PREVIEW_LOGS=invalid-input since"; exit 64; }
[[ "$tail_n" =~ ^[0-9]{1,4}$ ]] && (( tail_n >= 1 && tail_n <= 5000 )) || { echo "PREVIEW_LOGS=invalid-input tail"; exit 64; }
[[ -z "$trace" || "$trace" =~ ^[A-Za-z0-9-]{1,128}$ ]] || { echo "PREVIEW_LOGS=invalid-input trace"; exit 64; }

container="$(docker ps -a \
  --filter label=com.docker.compose.project=veiculando-preview \
  --filter "label=com.docker.compose.service=$servico" \
  --format '{{.Names}}' | head -n 1)"
[[ -n "$container" ]] || { echo "PREVIEW_LOGS=no-container $servico"; exit 65; }

filtrar() { if [[ -n "$trace" ]]; then grep -F -- "$trace" || true; else cat; fi; }

saida="$(docker logs --timestamps --since "$since" --tail "$tail_n" "$container" 2>&1 | filtrar | mascarar)"
echo "PREVIEW_LOGS_CONTAINER=$container"
printf '%s\n' "$saida"
# O Run Command devolve so os ULTIMOS ~4 KB do stdout. A contagem e o marcador
# vem no fim para sobreviver ao corte: quem le compara a contagem com o que
# recebeu e, se faltar linha, refina com `trace` ou reduz `since`/`tail`.
echo "PREVIEW_LOGS_LINES=$(printf '%s' "$saida" | grep -c '' || true) PREVIEW_LOGS_BYTES=${#saida}"
echo "PREVIEW_LOGS=ok"
