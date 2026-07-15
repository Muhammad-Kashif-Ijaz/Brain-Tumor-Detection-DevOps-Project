#!/usr/bin/env bash
set -euo pipefail

PROJECT_NAME="${PROJECT_NAME:-neuroscope}"
ENVIRONMENT="${ENVIRONMENT:-prod}"
AZURE_LOCATION="${AZURE_LOCATION:-eastus}"
AKS_NODE_VM_SIZE="${AKS_NODE_VM_SIZE:-Standard_D2s_v7}"
AKS_NODE_COUNT="${AKS_NODE_COUNT:-1}"
AKS_REQUIRED_VCPUS="${AKS_REQUIRED_VCPUS:-2}"
AUTO_SELECT_AZURE_LOCATION="${AUTO_SELECT_AZURE_LOCATION:-true}"
CHECK_AKS_SKU_AVAILABILITY="${CHECK_AKS_SKU_AVAILABILITY:-false}"
AZURE_CLI_TIMEOUT_SECONDS="${AZURE_CLI_TIMEOUT_SECONDS:-30}"
TERRAFORM_APPLY_INFRA="${TERRAFORM_APPLY_INFRA:-false}"
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
  if command -v timeout >/dev/null 2>&1; then
    usage_json="$(timeout "$AZURE_CLI_TIMEOUT_SECONDS" az vm list-usage --location "$region" -o json 2>/dev/null || echo '[]')"
  else
    usage_json="$(az vm list-usage --location "$region" -o json 2>/dev/null || echo '[]')"
  fi
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
  if command -v timeout >/dev/null 2>&1; then
    sku_json="$(timeout "$AZURE_CLI_TIMEOUT_SECONDS" az vm list-skus --location "$region" --resource-type virtualMachines --size "$AKS_NODE_VM_SIZE" --all -o json 2>/dev/null || echo '[]')"
  else
    sku_json="$(az vm list-skus --location "$region" --resource-type virtualMachines --size "$AKS_NODE_VM_SIZE" --all -o json 2>/dev/null || echo '[]')"
  fi
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
  if [ "$CHECK_AKS_SKU_AVAILABILITY" != "true" ]; then
    echo "Skipping slow VM SKU availability scan. Terraform/AKS will validate the selected VM size."
  fi
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
    if [ "$CHECK_AKS_SKU_AVAILABILITY" = "true" ]; then
      local available
      available="$(sku_available "$region")"
      if [ "$available" != "yes" ]; then
        echo "  ${region}: ${AKS_NODE_VM_SIZE} unavailable"
        continue
      fi
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
FILE_SHARE_QUOTA_GB="${FILE_SHARE_QUOTA_GB:-20}"

COMPACT_PROJECT="${NORMALIZED_PROJECT//-/}"
RESOURCE_NAME_PREFIX="${COMPACT_PROJECT}${ENVIRONMENT}"
DEFAULT_NAME_SUFFIX="${STATE_HASH:0:8}"

first_matching_resource_name() {
  local resource_type="$1"
  local name_prefix="$2"

  az resource list \
    --resource-group "$APP_RESOURCE_GROUP" \
    --resource-type "$resource_type" \
    --query "[?starts_with(name, '${name_prefix}')].name | [0]" \
    -o tsv 2>/dev/null || true
}

resource_suffix_from_name() {
  local resource_name="$1"
  if [[ "$resource_name" == "$RESOURCE_NAME_PREFIX"* ]]; then
    printf '%s' "${resource_name#"$RESOURCE_NAME_PREFIX"}"
  fi
}

EXISTING_ACR_NAME="$(first_matching_resource_name "Microsoft.ContainerRegistry/registries" "$RESOURCE_NAME_PREFIX")"
EXISTING_STORAGE_ACCOUNT_NAME="$(first_matching_resource_name "Microsoft.Storage/storageAccounts" "$RESOURCE_NAME_PREFIX")"
NAME_SUFFIX="${NAME_SUFFIX:-$(resource_suffix_from_name "${EXISTING_ACR_NAME:-}")}"
NAME_SUFFIX="${NAME_SUFFIX:-$(resource_suffix_from_name "${EXISTING_STORAGE_ACCOUNT_NAME:-}")}"
NAME_SUFFIX="${NAME_SUFFIX:-$DEFAULT_NAME_SUFFIX}"

ACR_NAME="${RESOURCE_NAME_PREFIX}${NAME_SUFFIX}"
ACR_NAME="${ACR_NAME:0:50}"
STORAGE_ACCOUNT_NAME="${RESOURCE_NAME_PREFIX}${NAME_SUFFIX}"
STORAGE_ACCOUNT_NAME="${STORAGE_ACCOUNT_NAME:0:24}"
LOG_ANALYTICS_NAME="${APP_PREFIX}-logs"
APP_INSIGHTS_NAME="${APP_PREFIX}-appi"
AKS_NAME="${APP_PREFIX}-aks"
AKS_DNS_PREFIX="${COMPACT_PROJECT}-${ENVIRONMENT}"

ensure_core_azure_resources() {
  echo "Ensuring Azure resources with Azure CLI before Terraform state import..."
  az group create \
    --name "$APP_RESOURCE_GROUP" \
    --location "$AZURE_LOCATION" \
    --tags application="NeuroScope MRI" environment="$ENVIRONMENT" managed_by="Terraform" >/dev/null

  if ! az monitor log-analytics workspace show --resource-group "$APP_RESOURCE_GROUP" --workspace-name "$LOG_ANALYTICS_NAME" >/dev/null 2>&1; then
    echo "Creating Log Analytics workspace ${LOG_ANALYTICS_NAME}..."
    az monitor log-analytics workspace create \
      --resource-group "$APP_RESOURCE_GROUP" \
      --workspace-name "$LOG_ANALYTICS_NAME" \
      --location "$AZURE_LOCATION" \
      --sku PerGB2018 \
      --retention-time 30 \
      --tags application="NeuroScope MRI" environment="$ENVIRONMENT" managed_by="Terraform" >/dev/null
  fi
  LOG_ANALYTICS_ID="$(az monitor log-analytics workspace show --resource-group "$APP_RESOURCE_GROUP" --workspace-name "$LOG_ANALYTICS_NAME" --query id -o tsv)"

  if az monitor app-insights component -h >/dev/null 2>&1; then
    if ! az monitor app-insights component show --app "$APP_INSIGHTS_NAME" --resource-group "$APP_RESOURCE_GROUP" >/dev/null 2>&1; then
      echo "Creating Application Insights component ${APP_INSIGHTS_NAME}..."
      az monitor app-insights component create \
        --app "$APP_INSIGHTS_NAME" \
        --location "$AZURE_LOCATION" \
        --resource-group "$APP_RESOURCE_GROUP" \
        --application-type web \
        --workspace "$LOG_ANALYTICS_ID" \
        --tags application="NeuroScope MRI" environment="$ENVIRONMENT" managed_by="Terraform" >/dev/null || \
        echo "Application Insights CLI create was not available; Terraform state reconciliation will handle it if needed."
    fi
  fi

  if ! az acr show --name "$ACR_NAME" --resource-group "$APP_RESOURCE_GROUP" >/dev/null 2>&1; then
    echo "Creating Azure Container Registry ${ACR_NAME}..."
    az acr create \
      --name "$ACR_NAME" \
      --resource-group "$APP_RESOURCE_GROUP" \
      --location "$AZURE_LOCATION" \
      --sku Basic \
      --admin-enabled false \
      --tags application="NeuroScope MRI" environment="$ENVIRONMENT" managed_by="Terraform" >/dev/null
  fi

  if ! az storage account show --name "$STORAGE_ACCOUNT_NAME" --resource-group "$APP_RESOURCE_GROUP" >/dev/null 2>&1; then
    echo "Creating Storage Account ${STORAGE_ACCOUNT_NAME}..."
    az storage account create \
      --name "$STORAGE_ACCOUNT_NAME" \
      --resource-group "$APP_RESOURCE_GROUP" \
      --location "$AZURE_LOCATION" \
      --sku Standard_LRS \
      --kind StorageV2 \
      --min-tls-version TLS1_2 \
      --allow-blob-public-access false \
      --https-only true \
      --tags application="NeuroScope MRI" environment="$ENVIRONMENT" managed_by="Terraform" >/dev/null
  fi
  STORAGE_ACCOUNT_KEY_FOR_SHARE="$(az storage account keys list --resource-group "$APP_RESOURCE_GROUP" --account-name "$STORAGE_ACCOUNT_NAME" --query '[0].value' -o tsv)"
  az storage share create \
    --name "$FILE_SHARE_NAME" \
    --account-name "$STORAGE_ACCOUNT_NAME" \
    --account-key "$STORAGE_ACCOUNT_KEY_FOR_SHARE" \
    --quota "$FILE_SHARE_QUOTA_GB" >/dev/null

  if ! az aks show --resource-group "$APP_RESOURCE_GROUP" --name "$AKS_NAME" >/dev/null 2>&1; then
    echo "Creating AKS cluster ${AKS_NAME}. This can take 10-20 minutes on Azure..."
    az aks create \
      --resource-group "$APP_RESOURCE_GROUP" \
      --name "$AKS_NAME" \
      --location "$AZURE_LOCATION" \
      --dns-name-prefix "$AKS_DNS_PREFIX" \
      --node-count "$AKS_NODE_COUNT" \
      --node-vm-size "$AKS_NODE_VM_SIZE" \
      --nodepool-name system \
      --node-osdisk-size 128 \
      --enable-managed-identity \
      --enable-rbac \
      --network-plugin azure \
      --load-balancer-sku standard \
      --enable-oidc-issuer \
      --enable-workload-identity \
      --enable-addons monitoring,azure-policy \
      --workspace-resource-id "$LOG_ANALYTICS_ID" \
      --generate-ssh-keys \
      --tags application="NeuroScope MRI" environment="$ENVIRONMENT" managed_by="Terraform" >/dev/null
  fi

  echo "Waiting for AKS cluster ${AKS_NAME} to be ready..."
  az aks wait --resource-group "$APP_RESOURCE_GROUP" --name "$AKS_NAME" --created --timeout 1800
  az aks update --resource-group "$APP_RESOURCE_GROUP" --name "$AKS_NAME" --attach-acr "$ACR_NAME" >/dev/null
}

resource_group_location() {
  local group_name="$1"
  az group show --name "$group_name" --query location -o tsv 2>/dev/null || true
}

existing_app_location="$(resource_group_location "$APP_RESOURCE_GROUP")"
if [ -n "$existing_app_location" ]; then
  AZURE_LOCATION="$existing_app_location"
  echo "Using existing application resource group region: ${AZURE_LOCATION}"
fi

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

ensure_core_azure_resources

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
export TF_VAR_node_count="$AKS_NODE_COUNT"
export TF_VAR_file_share_name="$FILE_SHARE_NAME"
export TF_VAR_file_share_quota_gb="$FILE_SHARE_QUOTA_GB"
export TF_VAR_name_suffix="$NAME_SUFFIX"

state_has() {
  terraform state show "$1" >/dev/null 2>&1
}

resource_exists() {
  az resource show --ids "$1" >/dev/null 2>&1
}

remove_stale_state_if_missing() {
  local address="$1"
  local id="$2"
  local label="$3"

  if state_has "$address" && ! resource_exists "$id"; then
    echo "Terraform state tracks ${label}, but Azure no longer has it. Removing stale Terraform state..."
    terraform state rm "$address" >/dev/null
  fi
}

import_if_exists() {
  local address="$1"
  local id="$2"
  local label="$3"

  if state_has "$address"; then
    echo "Terraform state already tracks ${label}."
    return
  fi

  if resource_exists "$id"; then
    echo "Importing existing ${label} into Terraform state..."
    terraform import -input=false "$address" "$id"
  fi
}

try_import_if_untracked() {
  local address="$1"
  local id="$2"
  local label="$3"

  if state_has "$address"; then
    echo "Terraform state already tracks ${label}."
    return
  fi

  echo "Checking whether ${label} already exists and can be imported..."
  if terraform import -input=false "$address" "$id" >/dev/null 2>&1; then
    echo "Imported existing ${label} into Terraform state."
  else
    echo "No importable existing ${label} found. Terraform will create it if needed."
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

reconcile_terraform_state() {
  echo "Reconciling Terraform state with any resources from earlier failed runs..."
  import_resource_group_if_exists \
    "azurerm_resource_group.main" \
    "$APP_RESOURCE_GROUP"

  local logs_id="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${APP_RESOURCE_GROUP}/providers/Microsoft.OperationalInsights/workspaces/${APP_PREFIX}-logs"
  local appi_id="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${APP_RESOURCE_GROUP}/providers/Microsoft.Insights/components/${APP_PREFIX}-appi"
  local aks_id="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${APP_RESOURCE_GROUP}/providers/Microsoft.ContainerService/managedClusters/${APP_PREFIX}-aks"

  remove_stale_state_if_missing \
    "azurerm_log_analytics_workspace.main" \
    "$logs_id" \
    "Log Analytics workspace ${APP_PREFIX}-logs"
  try_import_if_untracked \
    "azurerm_log_analytics_workspace.main" \
    "$logs_id" \
    "Log Analytics workspace ${APP_PREFIX}-logs"

  remove_stale_state_if_missing \
    "azurerm_application_insights.main" \
    "$appi_id" \
    "Application Insights component ${APP_PREFIX}-appi"
  try_import_if_untracked \
    "azurerm_application_insights.main" \
    "$appi_id" \
    "Application Insights component ${APP_PREFIX}-appi"

  remove_stale_state_if_missing \
    "azurerm_kubernetes_cluster.main" \
    "$aks_id" \
    "AKS cluster ${APP_PREFIX}-aks"
  try_import_if_untracked \
    "azurerm_kubernetes_cluster.main" \
    "$aks_id" \
    "AKS cluster ${APP_PREFIX}-aks"

  import_if_exists \
    "azurerm_container_registry.main" \
    "/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${APP_RESOURCE_GROUP}/providers/Microsoft.ContainerRegistry/registries/${ACR_NAME}" \
    "Container Registry ${ACR_NAME}"

  import_if_exists \
    "azurerm_storage_account.main" \
    "/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${APP_RESOURCE_GROUP}/providers/Microsoft.Storage/storageAccounts/${STORAGE_ACCOUNT_NAME}" \
    "storage account ${STORAGE_ACCOUNT_NAME}"
  import_if_exists \
    "azurerm_storage_share.app" \
    "/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${APP_RESOURCE_GROUP}/providers/Microsoft.Storage/storageAccounts/${STORAGE_ACCOUNT_NAME}/fileServices/default/shares/${FILE_SHARE_NAME}" \
    "Azure Files share ${STORAGE_ACCOUNT_NAME}/${FILE_SHARE_NAME}"
}

terraform_apply() {
  terraform apply -input=false -auto-approve \
    -var="project_name=${PROJECT_NAME}" \
    -var="environment=${ENVIRONMENT}" \
    -var="location=${AZURE_LOCATION}" \
    -var="node_vm_size=${AKS_NODE_VM_SIZE}" \
    -var="node_count=${AKS_NODE_COUNT}" \
    -var="file_share_name=${FILE_SHARE_NAME}" \
    -var="file_share_quota_gb=${FILE_SHARE_QUOTA_GB}" \
    -var="name_suffix=${NAME_SUFFIX}"
}

terraform_apply_with_recovery() {
  local attempt=1
  local max_attempts=3
  local apply_log

  while [ "$attempt" -le "$max_attempts" ]; do
    reconcile_terraform_state
    apply_log="$(mktemp)"

    if terraform_apply 2>&1 | tee "$apply_log"; then
      return
    fi

    if grep -Eq "Provider produced inconsistent result after apply|Root object was present, but now absent|ResourceNotFound|was not found|already exists - to be managed via Terraform" "$apply_log"; then
      echo "Terraform and Azure are temporarily out of sync after attempt ${attempt}/${max_attempts}."
      echo "Waiting 90 seconds, reconciling state again, then retrying..."
      sleep 90
      attempt=$((attempt + 1))
      continue
    fi

    exit 1
  done

  echo "Terraform apply failed after ${max_attempts} recovery attempts." >&2
  exit 1
}

if [ "$TERRAFORM_APPLY_INFRA" = "true" ]; then
  terraform_apply_with_recovery
  ACR_NAME="$(terraform output -raw acr_name)"
  ACR_LOGIN_SERVER="$(terraform output -raw acr_login_server)"
  AKS_NAME="$(terraform output -raw aks_name)"
  RESOURCE_GROUP="$(terraform output -raw resource_group_name)"
  STORAGE_ACCOUNT_NAME="$(terraform output -raw storage_account_name)"
  STORAGE_ACCOUNT_KEY="$(terraform output -raw storage_account_key)"
  FILE_SHARE_NAME="$(terraform output -raw file_share_name)"
  LOG_ANALYTICS_NAME="$(terraform output -raw log_analytics_workspace_name)"
else
  reconcile_terraform_state
  echo "Skipping Terraform apply because TERRAFORM_APPLY_INFRA=false. Azure resources were ensured with Azure CLI and imported into Terraform state."
  ACR_LOGIN_SERVER="$(az acr show --name "$ACR_NAME" --query loginServer -o tsv)"
  RESOURCE_GROUP="$APP_RESOURCE_GROUP"
  STORAGE_ACCOUNT_KEY="$(az storage account keys list --resource-group "$APP_RESOURCE_GROUP" --account-name "$STORAGE_ACCOUNT_NAME" --query '[0].value' -o tsv)"
fi
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
