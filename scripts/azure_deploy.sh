#!/usr/bin/env bash
set -euo pipefail

PROJECT_NAME="${PROJECT_NAME:-neuroscope}"
ENVIRONMENT="${ENVIRONMENT:-prod}"
AZURE_LOCATION="${AZURE_LOCATION:-eastus}"
AKS_NODE_VM_SIZE="${AKS_NODE_VM_SIZE:-Standard_D2s_v7}"
AKS_REQUIRED_VCPUS="${AKS_REQUIRED_VCPUS:-2}"
AUTO_SELECT_AZURE_LOCATION="${AUTO_SELECT_AZURE_LOCATION:-true}"
AZURE_LOCATION_CANDIDATES="${AZURE_LOCATION_CANDIDATES:-eastus eastus2 westus2 westus3 centralus southcentralus northeurope westeurope uksouth canadacentral southeastasia centralindia uaenorth}"
IMAGE_NAME="${IMAGE_NAME:-neuroscope-mri}"
IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)}"
NAMESPACE="${K8S_NAMESPACE:-neuroscope}"
TERRAFORM_DIR="${TERRAFORM_DIR:-infra/terraform}"
STATE_CONTAINER="${STATE_CONTAINER:-tfstate}"
K8S_ROLLOUT_TIMEOUT="${K8S_ROLLOUT_TIMEOUT:-20m}"

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

remaining_vcpus() {
  local region="$1"
  local usage_json
  usage_json="$(az vm list-usage --location "$region" -o json 2>/dev/null || echo '[]')"
  printf '%s' "$usage_json" | "$PYTHON_BIN" -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    print(0)
    raise SystemExit
for item in data:
    if item.get("name", {}).get("value") == "cores":
        print(max(0, int(item.get("limit", 0)) - int(item.get("currentValue", 0))))
        break
else:
    print(0)
'
}

sku_available() {
  local region="$1"
  local sku_json
  sku_json="$(az vm list-skus --location "$region" --resource-type virtualMachines --size "$AKS_NODE_VM_SIZE" --all -o json 2>/dev/null || echo '[]')"
  printf '%s' "$sku_json" | "$PYTHON_BIN" -c '
import json
import sys

sku = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    print("no")
    raise SystemExit

for item in data:
    if item.get("name") == sku and not item.get("restrictions"):
        print("yes")
        break
else:
    print("no")
' "$AKS_NODE_VM_SIZE"
}

select_azure_location() {
  if [ "$AUTO_SELECT_AZURE_LOCATION" != "true" ]; then
    return
  fi

  echo "Checking Azure region quota for AKS node size ${AKS_NODE_VM_SIZE}..."
  local seen=" "
  local ordered_regions=""
  for region in $AZURE_LOCATION $AZURE_LOCATION_CANDIDATES; do
    if [[ "$seen" != *" $region "* ]]; then
      seen="${seen}${region} "
      ordered_regions="${ordered_regions}${region} "
    fi
  done

  local best_quota=0
  local best_region=""
  for region in $ordered_regions; do
    local available
    available="$(sku_available "$region")"
    if [ "$available" != "yes" ]; then
      echo "  ${region}: ${AKS_NODE_VM_SIZE} unavailable"
      continue
    fi

    local left
    left="$(remaining_vcpus "$region")"
    echo "  ${region}: ${left} regional vCPU quota remaining"
    if [ "$left" -ge "$AKS_REQUIRED_VCPUS" ]; then
      AZURE_LOCATION="$region"
      echo "Using Azure region: ${AZURE_LOCATION}"
      return
    fi
    if [ "$left" -gt "$best_quota" ]; then
      best_quota="$left"
      best_region="$region"
    fi
  done

  echo "No checked Azure region has ${AKS_REQUIRED_VCPUS}+ vCPU quota for ${AKS_NODE_VM_SIZE}." >&2
  if [ -n "$best_region" ]; then
    echo "Best checked region was ${best_region} with ${best_quota} vCPU quota remaining." >&2
  fi
  echo "Request quota in Azure Portal, set AZURE_LOCATION to a region with quota, or set AKS_NODE_VM_SIZE to another allowed VM size." >&2
  exit 1
}

select_azure_location

SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
NORMALIZED_PROJECT="$("$PYTHON_BIN" -c 'import re,sys; print(re.sub(r"[^a-z0-9-]", "-", sys.argv[1].lower()))' "$PROJECT_NAME")"
APP_PREFIX="${NORMALIZED_PROJECT}-${ENVIRONMENT}"
APP_RESOURCE_GROUP="${APP_PREFIX}-rg"
HASH_INPUT="${SUBSCRIPTION_ID}-${PROJECT_NAME}-${ENVIRONMENT}"
STATE_HASH="$("$PYTHON_BIN" -c "import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:12])" "$HASH_INPUT")"
STATE_RG="${STATE_RG:-${PROJECT_NAME}-${ENVIRONMENT}-tfstate-rg}"
STATE_SA="${STATE_SA:-tfst${STATE_HASH}}"
STATE_KEY="${STATE_KEY:-${PROJECT_NAME}-${ENVIRONMENT}.tfstate}"
STATE_LOCATION="${STATE_LOCATION:-}"
FILE_SHARE_NAME="${FILE_SHARE_NAME:-neuroscope-data}"

resource_group_location() {
  local group_name="$1"
  az group show --name "$group_name" --query location -o tsv 2>/dev/null || true
}

existing_state_location="$(resource_group_location "$STATE_RG")"
if [ -n "$existing_state_location" ]; then
  STATE_LOCATION="$existing_state_location"
else
  STATE_LOCATION="${STATE_LOCATION:-$AZURE_LOCATION}"
fi

echo "Preparing Terraform state storage in ${STATE_LOCATION}..."
az group create --name "$STATE_RG" --location "$STATE_LOCATION" >/dev/null

if ! az storage account show --name "$STATE_SA" --resource-group "$STATE_RG" >/dev/null 2>&1; then
  az storage account create \
    --name "$STATE_SA" \
    --resource-group "$STATE_RG" \
    --location "$STATE_LOCATION" \
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

export TF_VAR_project_name="$PROJECT_NAME"
export TF_VAR_environment="$ENVIRONMENT"
export TF_VAR_location="$AZURE_LOCATION"
export TF_VAR_node_vm_size="$AKS_NODE_VM_SIZE"
export TF_VAR_file_share_name="$FILE_SHARE_NAME"

state_has() {
  terraform state show "$1" >/dev/null 2>&1
}

import_if_exists() {
  local address="$1"
  local id="$2"
  local label="$3"

  if state_has "$address"; then
    echo "Terraform state already tracks ${label}."
    return
  fi

  if az resource show --ids "$id" >/dev/null 2>&1; then
    echo "Importing existing ${label} into Terraform state..."
    terraform import -input=false "$address" "$id"
  fi
}

import_resource_group_if_exists() {
  local address="$1"
  local name="$2"
  local id="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${name}"

  if state_has "$address"; then
    echo "Terraform state already tracks resource group ${name}."
    return
  fi

  if az group show --name "$name" >/dev/null 2>&1; then
    echo "Importing existing resource group ${name} into Terraform state..."
    terraform import -input=false "$address" "$id"
  fi
}

first_matching_resource_name() {
  local resource_type="$1"
  local name_prefix="$2"

  az resource list \
    --resource-group "$APP_RESOURCE_GROUP" \
    --resource-type "$resource_type" \
    --query "[?starts_with(name, '${name_prefix}')].name | [0]" \
    -o tsv 2>/dev/null || true
}

echo "Reconciling Terraform state with any resources from earlier failed runs..."
import_resource_group_if_exists \
  "azurerm_resource_group.main" \
  "$APP_RESOURCE_GROUP"
import_if_exists \
  "azurerm_log_analytics_workspace.main" \
  "/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${APP_RESOURCE_GROUP}/providers/Microsoft.OperationalInsights/workspaces/${APP_PREFIX}-logs" \
  "Log Analytics workspace ${APP_PREFIX}-logs"
import_if_exists \
  "azurerm_application_insights.main" \
  "/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${APP_RESOURCE_GROUP}/providers/Microsoft.Insights/components/${APP_PREFIX}-appi" \
  "Application Insights component ${APP_PREFIX}-appi"
import_if_exists \
  "azurerm_kubernetes_cluster.main" \
  "/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${APP_RESOURCE_GROUP}/providers/Microsoft.ContainerService/managedClusters/${APP_PREFIX}-aks" \
  "AKS cluster ${APP_PREFIX}-aks"

COMPACT_PROJECT="${NORMALIZED_PROJECT//-/}"
RANDOM_NAME_PREFIX="${COMPACT_PROJECT}${ENVIRONMENT}"
EXISTING_ACR_NAME="$(first_matching_resource_name "Microsoft.ContainerRegistry/registries" "$RANDOM_NAME_PREFIX")"
if [ -n "$EXISTING_ACR_NAME" ]; then
  import_if_exists \
    "azurerm_container_registry.main" \
    "/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${APP_RESOURCE_GROUP}/providers/Microsoft.ContainerRegistry/registries/${EXISTING_ACR_NAME}" \
    "Container Registry ${EXISTING_ACR_NAME}"
fi

EXISTING_STORAGE_ACCOUNT_NAME="$(first_matching_resource_name "Microsoft.Storage/storageAccounts" "$RANDOM_NAME_PREFIX")"
if [ -n "$EXISTING_STORAGE_ACCOUNT_NAME" ]; then
  import_if_exists \
    "azurerm_storage_account.main" \
    "/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${APP_RESOURCE_GROUP}/providers/Microsoft.Storage/storageAccounts/${EXISTING_STORAGE_ACCOUNT_NAME}" \
    "storage account ${EXISTING_STORAGE_ACCOUNT_NAME}"
  import_if_exists \
    "azurerm_storage_share.app" \
    "/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${APP_RESOURCE_GROUP}/providers/Microsoft.Storage/storageAccounts/${EXISTING_STORAGE_ACCOUNT_NAME}/fileServices/default/shares/${FILE_SHARE_NAME}" \
    "Azure Files share ${EXISTING_STORAGE_ACCOUNT_NAME}/${FILE_SHARE_NAME}"
fi

terraform_apply() {
  terraform apply -input=false -auto-approve \
    -var="project_name=${PROJECT_NAME}" \
    -var="environment=${ENVIRONMENT}" \
    -var="location=${AZURE_LOCATION}" \
    -var="node_vm_size=${AKS_NODE_VM_SIZE}"
}

apply_log="$(mktemp)"
if ! terraform_apply 2>&1 | tee "$apply_log"; then
  if grep -Eq "Provider produced inconsistent result after apply|Root object was present, but now absent" "$apply_log"; then
    echo "Terraform provider returned a transient Azure consistency error. Waiting 90 seconds, then retrying once..."
    sleep 90
    terraform_apply
  else
    exit 1
  fi
fi

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

dump_k8s_diagnostics() {
  echo "Deployment did not become ready. Kubernetes diagnostics:"
  kubectl -n "$NAMESPACE" get pods -o wide || true
  kubectl -n "$NAMESPACE" get events --sort-by=.lastTimestamp | tail -80 || true
  kubectl -n "$NAMESPACE" describe deployment neuroscope-mri || true
  kubectl -n "$NAMESPACE" describe pods -l app=neuroscope-mri || true
  kubectl -n "$NAMESPACE" logs -l app=neuroscope-mri --all-containers --tail=200 --prefix || true
}

render_deployment() {
  "$PYTHON_BIN" - <<PY
from pathlib import Path

content = Path("k8s/deployment.yaml").read_text(encoding="utf-8")
content = content.replace("__FILE_SHARE_NAME__", "${FILE_SHARE_NAME}")
content = content.replace("__IMAGE_URI__", "${IMAGE_URI}")
print(content)
PY
}

kubectl apply -f k8s/namespace.yaml
kubectl -n "$NAMESPACE" create secret generic azure-storage-secret \
  --from-literal=azurestorageaccountname="$STORAGE_ACCOUNT_NAME" \
  --from-literal=azurestorageaccountkey="$STORAGE_ACCOUNT_KEY" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f k8s/azure-monitor-agentconfig.yaml
kubectl apply -f k8s/service.yaml

kubectl -n "$NAMESPACE" delete hpa neuroscope-mri --ignore-not-found=true
kubectl -n "$NAMESPACE" delete deployment neuroscope-mri --ignore-not-found=true
render_deployment | kubectl apply -f -
kubectl apply -f k8s/hpa.yaml
if ! kubectl -n "$NAMESPACE" rollout status deployment/neuroscope-mri --timeout="$K8S_ROLLOUT_TIMEOUT"; then
  dump_k8s_diagnostics
  exit 1
fi

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
