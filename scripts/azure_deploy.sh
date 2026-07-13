#!/usr/bin/env bash
set -euo pipefail

PROJECT_NAME="${PROJECT_NAME:-neuroscope}"
ENVIRONMENT="${ENVIRONMENT:-prod}"
AZURE_LOCATION="${AZURE_LOCATION:-eastus}"
IMAGE_NAME="${IMAGE_NAME:-neuroscope-mri}"
IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)}"
NAMESPACE="${K8S_NAMESPACE:-neuroscope}"
TERRAFORM_DIR="${TERRAFORM_DIR:-infra/terraform}"
STATE_CONTAINER="${STATE_CONTAINER:-tfstate}"

require_tool() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required tool: $1" >&2
    exit 1
  fi
}

require_tool az
require_tool docker
require_tool kubectl
require_tool terraform
PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN="python"
fi
require_tool "$PYTHON_BIN"

if [ -n "${AZURE_CREDENTIALS:-}" ]; then
  export AZURE_CLIENT_ID="${AZURE_CLIENT_ID:-$("$PYTHON_BIN" -c "import json,os; print(json.loads(os.environ['AZURE_CREDENTIALS']).get('clientId',''))")}"
  export AZURE_CLIENT_SECRET="${AZURE_CLIENT_SECRET:-$("$PYTHON_BIN" -c "import json,os; print(json.loads(os.environ['AZURE_CREDENTIALS']).get('clientSecret',''))")}"
  export AZURE_TENANT_ID="${AZURE_TENANT_ID:-$("$PYTHON_BIN" -c "import json,os; print(json.loads(os.environ['AZURE_CREDENTIALS']).get('tenantId',''))")}"
  export AZURE_SUBSCRIPTION_ID="${AZURE_SUBSCRIPTION_ID:-$("$PYTHON_BIN" -c "import json,os; print(json.loads(os.environ['AZURE_CREDENTIALS']).get('subscriptionId',''))")}"
fi

if ! az account show >/dev/null 2>&1; then
  if [ -z "${AZURE_CLIENT_ID:-}" ] || [ -z "${AZURE_CLIENT_SECRET:-}" ] || [ -z "${AZURE_TENANT_ID:-}" ]; then
    echo "Azure login is required. Provide AZURE_CREDENTIALS JSON or Azure service principal environment variables." >&2
    exit 1
  fi
  az login --service-principal \
    --username "$AZURE_CLIENT_ID" \
    --password "$AZURE_CLIENT_SECRET" \
    --tenant "$AZURE_TENANT_ID" >/dev/null
fi

if [ -n "${AZURE_SUBSCRIPTION_ID:-}" ]; then
  az account set --subscription "$AZURE_SUBSCRIPTION_ID"
fi

SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
HASH_INPUT="${SUBSCRIPTION_ID}-${PROJECT_NAME}-${ENVIRONMENT}"
STATE_HASH="$("$PYTHON_BIN" -c "import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:12])" "$HASH_INPUT")"
STATE_RG="${STATE_RG:-${PROJECT_NAME}-${ENVIRONMENT}-tfstate-rg}"
STATE_SA="${STATE_SA:-tfst${STATE_HASH}}"
STATE_KEY="${STATE_KEY:-${PROJECT_NAME}-${ENVIRONMENT}.tfstate}"

echo "Preparing Terraform state storage..."
az group create --name "$STATE_RG" --location "$AZURE_LOCATION" >/dev/null

if ! az storage account show --name "$STATE_SA" --resource-group "$STATE_RG" >/dev/null 2>&1; then
  az storage account create \
    --name "$STATE_SA" \
    --resource-group "$STATE_RG" \
    --location "$AZURE_LOCATION" \
    --sku Standard_LRS \
    --kind StorageV2 \
    --min-tls-version TLS1_2 \
    --allow-blob-public-access false >/dev/null
fi

STATE_KEY_VALUE="$(az storage account keys list --resource-group "$STATE_RG" --account-name "$STATE_SA" --query '[0].value' -o tsv)"
az storage container create \
  --name "$STATE_CONTAINER" \
  --account-name "$STATE_SA" \
  --account-key "$STATE_KEY_VALUE" >/dev/null

pushd "$TERRAFORM_DIR" >/dev/null
terraform init -input=false -reconfigure \
  -backend-config="resource_group_name=${STATE_RG}" \
  -backend-config="storage_account_name=${STATE_SA}" \
  -backend-config="container_name=${STATE_CONTAINER}" \
  -backend-config="key=${STATE_KEY}"

terraform apply -input=false -auto-approve \
  -var="project_name=${PROJECT_NAME}" \
  -var="environment=${ENVIRONMENT}" \
  -var="location=${AZURE_LOCATION}"

ACR_NAME="$(terraform output -raw acr_name)"
ACR_LOGIN_SERVER="$(terraform output -raw acr_login_server)"
AKS_NAME="$(terraform output -raw aks_name)"
RESOURCE_GROUP="$(terraform output -raw resource_group_name)"
STORAGE_ACCOUNT_NAME="$(terraform output -raw storage_account_name)"
STORAGE_ACCOUNT_KEY="$(terraform output -raw storage_account_key)"
FILE_SHARE_NAME="$(terraform output -raw file_share_name)"
LOG_ANALYTICS_NAME="$(terraform output -raw log_analytics_workspace_name)"
popd >/dev/null

IMAGE_URI="${ACR_LOGIN_SERVER}/${IMAGE_NAME}:${IMAGE_TAG}"
LATEST_URI="${ACR_LOGIN_SERVER}/${IMAGE_NAME}:latest"

echo "Building and publishing container image..."
az acr login --name "$ACR_NAME"
docker build -t "$IMAGE_URI" -t "$LATEST_URI" .
docker push "$IMAGE_URI"
docker push "$LATEST_URI"

echo "Deploying to AKS..."
az aks get-credentials --resource-group "$RESOURCE_GROUP" --name "$AKS_NAME" --overwrite-existing >/dev/null

kubectl apply -f k8s/namespace.yaml
kubectl -n "$NAMESPACE" create secret generic azure-storage-secret \
  --from-literal=azurestorageaccountname="$STORAGE_ACCOUNT_NAME" \
  --from-literal=azurestorageaccountkey="$STORAGE_ACCOUNT_KEY" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f k8s/azure-monitor-agentconfig.yaml
"$PYTHON_BIN" - <<PY | kubectl apply -f -
from pathlib import Path

content = Path("k8s/deployment.yaml").read_text(encoding="utf-8")
content = content.replace("__FILE_SHARE_NAME__", "${FILE_SHARE_NAME}")
print(content)
PY
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/hpa.yaml
kubectl -n "$NAMESPACE" set image deployment/neuroscope-mri neuroscope-mri="$IMAGE_URI"
kubectl -n "$NAMESPACE" rollout status deployment/neuroscope-mri --timeout=12m

echo "Waiting for public endpoint..."
PUBLIC_IP=""
for _ in {1..60}; do
  PUBLIC_IP="$(kubectl -n "$NAMESPACE" get service neuroscope-mri -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)"
  if [ -n "$PUBLIC_IP" ]; then
    break
  fi
  sleep 10
done

echo "Deployment complete."
echo "Application URL: http://${PUBLIC_IP:-pending}"
echo "Azure resource group: $RESOURCE_GROUP"
echo "Azure Monitor workspace: $LOG_ANALYTICS_NAME"
echo "Azure Files share: ${STORAGE_ACCOUNT_NAME}/${FILE_SHARE_NAME}"
