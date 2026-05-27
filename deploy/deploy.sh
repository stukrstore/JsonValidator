#!/usr/bin/env bash
# Deploy JsonValidator to Azure Container Apps inside AZ-KR-INFRA-VNET / ACA-Subnet.
# Idempotent: re-running updates the existing container app revision.
set -euo pipefail

# -------- Configuration --------
LOCATION="koreacentral"
RG="AZ-JSONVALIDATOR-RG"
ACR_NAME="mskraksclustercr"
ACR_LOGIN_SERVER="${ACR_NAME}.azurecr.io"
IMAGE_REPO="jsonvalidator"
IMAGE_TAG="${IMAGE_TAG:-latest}"
IMAGE="${ACR_LOGIN_SERVER}/${IMAGE_REPO}:${IMAGE_TAG}"

VNET_RG="az-managemet-rg"
VNET_NAME="AZ-KR-INFRA-VNET"
ACA_SUBNET_NAME="JsonValidator-ACA-Subnet"
ACA_SUBNET_PREFIX="10.1.34.0/23"

CAE_NAME="jsonvalidator-cae"
APP_NAME="jsonvalidator-app"
TARGET_PORT=8000

# -------- Load .env --------
if [[ ! -f .env ]]; then
  echo "ERROR: .env not found. Copy .env.sample and fill in real values." >&2
  exit 1
fi
set -a
# shellcheck disable=SC1091
source .env
set +a

: "${MONGODB_URI:?MONGODB_URI must be set in .env}"

# -------- Resource group --------
az group create -n "$RG" -l "$LOCATION" -o none

# -------- ACR build & push --------
echo "==> Building image in ACR ($ACR_LOGIN_SERVER/$IMAGE_REPO:$IMAGE_TAG)"
az acr build \
  --registry "$ACR_NAME" \
  --image "${IMAGE_REPO}:${IMAGE_TAG}" \
  --file Dockerfile \
  .

# -------- ACA environment --------
SUBNET_ID="$(az network vnet subnet show -g "$VNET_RG" --vnet-name "$VNET_NAME" -n "$ACA_SUBNET_NAME" --query id -o tsv 2>/dev/null || true)"
if [[ -z "$SUBNET_ID" ]]; then
  echo "==> Creating subnet $ACA_SUBNET_NAME ($ACA_SUBNET_PREFIX) with delegation Microsoft.App/environments"
  az network vnet subnet create \
    -g "$VNET_RG" --vnet-name "$VNET_NAME" \
    -n "$ACA_SUBNET_NAME" \
    --address-prefixes "$ACA_SUBNET_PREFIX" \
    --delegations Microsoft.App/environments \
    -o none
  SUBNET_ID="$(az network vnet subnet show -g "$VNET_RG" --vnet-name "$VNET_NAME" -n "$ACA_SUBNET_NAME" --query id -o tsv)"
fi
echo "==> ACA subnet: $SUBNET_ID"

if ! az containerapp env show -n "$CAE_NAME" -g "$RG" -o none 2>/dev/null; then
  echo "==> Creating Container Apps environment $CAE_NAME"
  az containerapp env create \
    -n "$CAE_NAME" \
    -g "$RG" \
    -l "$LOCATION" \
    --infrastructure-subnet-resource-id "$SUBNET_ID" \
    --internal-only false \
    -o none
else
  echo "==> Container Apps environment $CAE_NAME already exists"
fi

# -------- ACR credentials for ACA --------
ACR_ID="$(az acr show -n "$ACR_NAME" --query id -o tsv)"

# Enable admin user for simple username/password pull (demo). For prod, use managed identity.
az acr update -n "$ACR_NAME" --admin-enabled true -o none
ACR_USER="$(az acr credential show -n "$ACR_NAME" --query username -o tsv)"
ACR_PASS="$(az acr credential show -n "$ACR_NAME" --query 'passwords[0].value' -o tsv)"

# -------- Container app --------
if ! az containerapp show -n "$APP_NAME" -g "$RG" -o none 2>/dev/null; then
  echo "==> Creating container app $APP_NAME"
  az containerapp create \
    -n "$APP_NAME" \
    -g "$RG" \
    --environment "$CAE_NAME" \
    --image "$IMAGE" \
    --target-port "$TARGET_PORT" \
    --ingress external \
    --registry-server "$ACR_LOGIN_SERVER" \
    --registry-username "$ACR_USER" \
    --registry-password "$ACR_PASS" \
    --secrets "mongodb-uri=$MONGODB_URI" "flask-secret=${FLASK_SECRET:-change-me}" \
    --env-vars \
      "MONGODB_URI=secretref:mongodb-uri" \
      "FLASK_SECRET=secretref:flask-secret" \
      "MONGODB_DB=${MONGODB_DB:-jsonvalidator}" \
      "MONGODB_COLLECTION=${MONGODB_COLLECTION:-requests}" \
      "AOAI_ENDPOINT=${AOAI_ENDPOINT:-https://mskr-aoai-eastus.openai.azure.com/}" \
      "AOAI_DEPLOYMENT=${AOAI_DEPLOYMENT:-gpt-5.3-codex}" \
      "AOAI_API_VERSION=${AOAI_API_VERSION:-2025-04-01-preview}" \
      "PORT=$TARGET_PORT" \
    --min-replicas 1 \
    --max-replicas 2 \
    --cpu 0.5 --memory 1.0Gi \
    -o none
else
  echo "==> Updating container app $APP_NAME"
  az containerapp secret set -n "$APP_NAME" -g "$RG" \
    --secrets "mongodb-uri=$MONGODB_URI" "flask-secret=${FLASK_SECRET:-change-me}" -o none
  az containerapp registry set -n "$APP_NAME" -g "$RG" \
    --server "$ACR_LOGIN_SERVER" --username "$ACR_USER" --password "$ACR_PASS" -o none
  az containerapp update \
    -n "$APP_NAME" -g "$RG" \
    --image "$IMAGE" \
    --set-env-vars \
      "MONGODB_URI=secretref:mongodb-uri" \
      "FLASK_SECRET=secretref:flask-secret" \
      "MONGODB_DB=${MONGODB_DB:-jsonvalidator}" \
      "MONGODB_COLLECTION=${MONGODB_COLLECTION:-requests}" \
      "AOAI_ENDPOINT=${AOAI_ENDPOINT:-https://mskr-aoai-eastus.openai.azure.com/}" \
      "AOAI_DEPLOYMENT=${AOAI_DEPLOYMENT:-gpt-5.3-codex}" \
      "AOAI_API_VERSION=${AOAI_API_VERSION:-2025-04-01-preview}" \
      "PORT=$TARGET_PORT" \
    -o none
fi

FQDN="$(az containerapp show -n "$APP_NAME" -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)"
echo ""
echo "✅ Deployed."
echo "URL: https://$FQDN"
