#!/usr/bin/env bash
set -euo pipefail

PROJECT_NAME="${PROJECT_NAME:-neuroscope}"
ENVIRONMENT="${ENVIRONMENT:-demo}"
AZURE_LOCATION="${AZURE_LOCATION:-eastus}"
AUTO_SELECT_AZURE_LOCATION="${AUTO_SELECT_AZURE_LOCATION:-true}"
AZURE_LOCATION_CANDIDATES="${AZURE_LOCATION_CANDIDATES:-eastus eastus2 westus2 westus3 centralus}"
AZURE_LOCATION_MAX_CANDIDATES="${AZURE_LOCATION_MAX_CANDIDATES:-3}"
AZURE_CLI_TIMEOUT_SECONDS="${AZURE_CLI_TIMEOUT_SECONDS:-15}"
AKS_NODE_VM_SIZE="${AKS_NODE_VM_SIZE:-Standard_D2s_v7}"
AKS_NODE_COUNT="${AKS_NODE_COUNT:-1}"
AKS_REQUIRED_VCPUS="${AKS_REQUIRED_VCPUS:-2}"
TERRAFORM_PARALLELISM="${TERRAFORM_PARALLELISM:-1}"
TF_LOCK_TIMEOUT="${TF_LOCK_TIMEOUT:-10m}"
IMAGE_NAME="${IMAGE_NAME:-neuroscope-mri}"
IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)}"
NAMESPACE="${K8S_NAMESPACE:-neuroscope}"
TERRAFORM_DIR="${TERRAFORM_DIR:-infra/terraform}"
STATE_CONTAINER="${STATE_CONTAINER:-tfstate}"
K8S_ROLLOUT_TIMEOUT="${K8S_ROLLOUT_TIMEOUT:-20m}"
PUBLIC_ENDPOINT_TIMEOUT_SECONDS="${PUBLIC_ENDPOINT_TIMEOUT_SECONDS:-600}"

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
require_tool curl

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN="python"
fi
require_tool "$PYTHON_BIN"

credential_field() {
  "$PYTHON_BIN" -c 'import json,os,sys; print(json.loads(os.environ["AZURE_CREDENTIALS"]).get(sys.argv[1], ""))' "$1"
}

if [ -n "${AZURE_CREDENTIALS:-}" ]; then
  export AZURE_CLIENT_ID="${AZURE_CLIENT_ID:-$(credential_field clientId)}"
  export AZURE_CLIENT_SECRET="${AZURE_CLIENT_SECRET:-$(credential_field clientSecret)}"
  export AZURE_TENANT_ID="${AZURE_TENANT_ID:-$(credential_field tenantId)}"
  export AZURE_SUBSCRIPTION_ID="${AZURE_SUBSCRIPTION_ID:-$(credential_field subscriptionId)}"
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

export AZURE_CORE_ONLY_SHOW_ERRORS=1
export ARM_CLIENT_ID="${ARM_CLIENT_ID:-${AZURE_CLIENT_ID:-}}"
export ARM_CLIENT_SECRET="${ARM_CLIENT_SECRET:-${AZURE_CLIENT_SECRET:-}}"
export ARM_TENANT_ID="${ARM_TENANT_ID:-${AZURE_TENANT_ID:-}}"
export ARM_SUBSCRIPTION_ID="${ARM_SUBSCRIPTION_ID:-${AZURE_SUBSCRIPTION_ID:-}}"

with_azure_timeout() {
  if command -v timeout >/dev/null 2>&1; then
    timeout "${AZURE_CLI_TIMEOUT_SECONDS}s" "$@"
  else
    "$@"
  fi
}

remaining_vcpus() {
  local region="$1"
  local usage_json
  if ! usage_json="$(with_azure_timeout az vm list-usage --location "$region" --query "[?name.value=='cores'].{limit:limit,current:currentValue}" -o json 2>/dev/null)"; then
    printf '%s' "unknown"
    return
  fi

  printf '%s' "$usage_json" | "$PYTHON_BIN" -c '
import json
import sys

try:
    usage = json.load(sys.stdin)
except Exception:
    print("unknown")
    raise SystemExit

if not usage:
    print("unknown")
else:
    print(max(0, int(usage[0].get("limit", 0)) - int(usage[0].get("current", 0))))
'
}

select_azure_location() {
  if [ "$AUTO_SELECT_AZURE_LOCATION" != "true" ]; then
    return
  fi

  echo "Checking up to ${AZURE_LOCATION_MAX_CANDIDATES} Azure regions for ${AKS_REQUIRED_VCPUS} available vCPUs..."
  local seen=" "
  local checked=0
  local region
  for region in $AZURE_LOCATION $AZURE_LOCATION_CANDIDATES; do
    if [[ "$seen" == *" $region "* ]]; then
      continue
    fi
    seen="${seen}${region} "
    checked=$((checked + 1))
    if [ "$checked" -gt "$AZURE_LOCATION_MAX_CANDIDATES" ]; then
      break
    fi

    local available
    available="$(remaining_vcpus "$region")"
    if [[ "$available" =~ ^[0-9]+$ ]]; then
      echo "  ${region}: ${available} regional vCPUs available"
      if [ "$available" -ge "$AKS_REQUIRED_VCPUS" ]; then
        AZURE_LOCATION="$region"
        echo "Using Azure region: ${AZURE_LOCATION}"
        return
      fi
    else
      echo "  ${region}: quota lookup unavailable"
    fi
  done

  echo "No checked region has the ${AKS_REQUIRED_VCPUS} vCPUs required for ${AKS_NODE_VM_SIZE}." >&2
  echo "Request regional vCPU quota in Azure, then rerun the same workflow. Existing resource groups are never moved automatically." >&2
  exit 1
}

resource_group_location() {
  az group show --name "$1" --query location -o tsv 2>/dev/null || true
}

SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
NORMALIZED_PROJECT="$($PYTHON_BIN -c 'import re,sys; print(re.sub(r"[^a-z0-9-]", "-", sys.argv[1].lower()))' "$PROJECT_NAME")"
COMPACT_PROJECT="${NORMALIZED_PROJECT//-/}"
APP_PREFIX="${NORMALIZED_PROJECT}-${ENVIRONMENT}"
APP_RESOURCE_GROUP="${APP_PREFIX}-rg"
HASH_INPUT="${SUBSCRIPTION_ID}-${PROJECT_NAME}-${ENVIRONMENT}"
STATE_HASH="$($PYTHON_BIN -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:12])' "$HASH_INPUT")"
STATE_RG="${STATE_RG:-${PROJECT_NAME}-${ENVIRONMENT}-tfstate-rg}"
STATE_SA="${STATE_SA:-tfst${STATE_HASH}}"
STATE_KEY="${STATE_KEY:-${PROJECT_NAME}-${ENVIRONMENT}.tfstate}"
FILE_SHARE_NAME="${FILE_SHARE_NAME:-neuroscope-data}"
FILE_SHARE_QUOTA_GB="${FILE_SHARE_QUOTA_GB:-20}"
NAME_SUFFIX="${NAME_SUFFIX:-${STATE_HASH:0:8}}"

ACR_NAME="${COMPACT_PROJECT}${ENVIRONMENT}${NAME_SUFFIX}"
ACR_NAME="${ACR_NAME:0:50}"
STORAGE_ACCOUNT_NAME="${COMPACT_PROJECT}${ENVIRONMENT}${NAME_SUFFIX}"
STORAGE_ACCOUNT_NAME="${STORAGE_ACCOUNT_NAME:0:24}"
LOG_ANALYTICS_NAME="${APP_PREFIX}-logs"
APP_INSIGHTS_NAME="${APP_PREFIX}-appi"
AKS_NAME="${APP_PREFIX}-aks"

existing_app_location="$(resource_group_location "$APP_RESOURCE_GROUP")"
if [ -n "$existing_app_location" ]; then
  AZURE_LOCATION="$existing_app_location"
  echo "Using existing application resource group region: ${AZURE_LOCATION}"
  if ! az aks show --resource-group "$APP_RESOURCE_GROUP" --name "$AKS_NAME" >/dev/null 2>&1; then
    existing_region_vcpus="$(remaining_vcpus "$AZURE_LOCATION")"
    if [[ "$existing_region_vcpus" =~ ^[0-9]+$ ]] && [ "$existing_region_vcpus" -lt "$AKS_REQUIRED_VCPUS" ]; then
      echo "${AZURE_LOCATION} has only ${existing_region_vcpus} regional vCPUs available, but AKS requires ${AKS_REQUIRED_VCPUS}." >&2
      echo "Request quota in ${AZURE_LOCATION}; the existing resource group cannot be moved to another region." >&2
      exit 1
    fi
  fi
else
  select_azure_location
fi

existing_state_location="$(resource_group_location "$STATE_RG")"
STATE_LOCATION="${STATE_LOCATION:-$AZURE_LOCATION}"
if [ -n "$existing_state_location" ]; then
  STATE_LOCATION="$existing_state_location"
fi

echo "Preparing the Terraform state backend in ${STATE_LOCATION}..."
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

STATE_STORAGE_KEY="$(az storage account keys list --resource-group "$STATE_RG" --account-name "$STATE_SA" --query '[0].value' -o tsv)"
az storage container create \
  --name "$STATE_CONTAINER" \
  --account-name "$STATE_SA" \
  --account-key "$STATE_STORAGE_KEY" >/dev/null

RESOURCE_GROUP_ID="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${APP_RESOURCE_GROUP}"
LOG_ANALYTICS_ID="${RESOURCE_GROUP_ID}/providers/Microsoft.OperationalInsights/workspaces/${LOG_ANALYTICS_NAME}"
APP_INSIGHTS_ID="${RESOURCE_GROUP_ID}/providers/Microsoft.Insights/components/${APP_INSIGHTS_NAME}"
ACR_ID="${RESOURCE_GROUP_ID}/providers/Microsoft.ContainerRegistry/registries/${ACR_NAME}"
STORAGE_ACCOUNT_ID="${RESOURCE_GROUP_ID}/providers/Microsoft.Storage/storageAccounts/${STORAGE_ACCOUNT_NAME}"
AKS_ID="${RESOURCE_GROUP_ID}/providers/Microsoft.ContainerService/managedClusters/${AKS_NAME}"
FILE_SHARE_ID="${STORAGE_ACCOUNT_ID}/fileServices/default/shares/${FILE_SHARE_NAME}"

pushd "$TERRAFORM_DIR" >/dev/null
export ARM_ACCESS_KEY="$STATE_STORAGE_KEY"

terraform init -input=false -reconfigure \
  -backend-config="resource_group_name=${STATE_RG}" \
  -backend-config="storage_account_name=${STATE_SA}" \
  -backend-config="container_name=${STATE_CONTAINER}" \
  -backend-config="key=${STATE_KEY}"
terraform validate

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

remove_state() {
  terraform state rm -lock-timeout="$TF_LOCK_TIMEOUT" "$1" >/dev/null
}

resource_exists() {
  az resource show --ids "$1" >/dev/null 2>&1
}

terraform_import() {
  terraform import -input=false -lock-timeout="$TF_LOCK_TIMEOUT" "$1" "$2"
}

state_resource_name() {
  terraform state show -no-color "$1" 2>/dev/null | awk -F' = ' '/^[[:space:]]*name[[:space:]]*=/{gsub(/"/, "", $2); print $2; exit}'
}

untrack_name_mismatch() {
  local address="$1"
  local expected_name="$2"
  if ! state_has "$address"; then
    return 0
  fi

  local tracked_name
  tracked_name="$(state_resource_name "$address")"
  if [ -n "$tracked_name" ] && [ "$tracked_name" != "$expected_name" ]; then
    echo "Terraform state tracks ${tracked_name}, not the deterministic ${expected_name}. Leaving the old Azure resource untouched."
    remove_state "$address"
    return 1
  fi
  return 0
}

reconcile_resource_group() {
  local address="azurerm_resource_group.main"
  if state_has "$address" && ! az group show --name "$APP_RESOURCE_GROUP" >/dev/null 2>&1; then
    echo "Removing stale Terraform state for resource group ${APP_RESOURCE_GROUP}."
    remove_state "$address"
  fi
  if ! state_has "$address" && az group show --name "$APP_RESOURCE_GROUP" >/dev/null 2>&1; then
    echo "Importing existing resource group ${APP_RESOURCE_GROUP}."
    terraform_import "$address" "$RESOURCE_GROUP_ID" || echo "Resource group import will be retried after Azure settles."
  fi
}

reconcile_resource() {
  local address="$1"
  local resource_id="$2"
  local label="$3"

  if state_has "$address" && ! resource_exists "$resource_id"; then
    echo "Removing stale Terraform state for ${label}."
    remove_state "$address"
  fi
  if ! state_has "$address" && resource_exists "$resource_id"; then
    echo "Importing existing ${label}."
    terraform_import "$address" "$resource_id" || echo "${label} import will be retried after Azure settles."
  fi
}

file_share_exists() {
  if ! az storage account show --name "$STORAGE_ACCOUNT_NAME" --resource-group "$APP_RESOURCE_GROUP" >/dev/null 2>&1; then
    return 1
  fi
  local account_key
  account_key="$(az storage account keys list --resource-group "$APP_RESOURCE_GROUP" --account-name "$STORAGE_ACCOUNT_NAME" --query '[0].value' -o tsv 2>/dev/null || true)"
  [ -n "$account_key" ] && az storage share exists \
    --name "$FILE_SHARE_NAME" \
    --account-name "$STORAGE_ACCOUNT_NAME" \
    --account-key "$account_key" \
    --query exists -o tsv 2>/dev/null | grep -qi '^true$'
}

reconcile_file_share() {
  local address="azurerm_storage_share.app"
  if state_has "$address" && ! file_share_exists; then
    echo "Removing stale Terraform state for Azure Files share ${FILE_SHARE_NAME}."
    remove_state "$address"
  fi
  if ! state_has "$address" && file_share_exists; then
    echo "Importing existing Azure Files share ${FILE_SHARE_NAME}."
    terraform_import "$address" "$FILE_SHARE_ID" || echo "Azure Files share import will be retried after Azure settles."
  fi
}

reconcile_acr_pull_assignment() {
  local address="azurerm_role_assignment.acr_pull"
  if state_has "$address" || ! resource_exists "$ACR_ID" || ! resource_exists "$AKS_ID"; then
    return
  fi

  local kubelet_object_id
  kubelet_object_id="$(az aks show --resource-group "$APP_RESOURCE_GROUP" --name "$AKS_NAME" --query 'identityProfile.kubeletidentity.objectId' -o tsv 2>/dev/null || true)"
  if [ -z "$kubelet_object_id" ]; then
    return
  fi

  local assignment_id
  assignment_id="$(az role assignment list \
    --scope "$ACR_ID" \
    --all \
    --query "[?principalId=='${kubelet_object_id}' && roleDefinitionName=='AcrPull'].id | [0]" -o tsv 2>/dev/null || true)"
  if [ -n "$assignment_id" ]; then
    echo "Importing the existing AKS AcrPull role assignment."
    terraform_import "$address" "$assignment_id" || echo "AcrPull role assignment import will be retried after Azure settles."
  fi
}

reconcile_terraform_state() {
  echo "Reconciling Terraform state with Azure before apply..."
  reconcile_resource_group
  reconcile_resource "azurerm_log_analytics_workspace.main" "$LOG_ANALYTICS_ID" "Log Analytics workspace"
  reconcile_resource "azurerm_application_insights.main" "$APP_INSIGHTS_ID" "Application Insights"

  if ! untrack_name_mismatch "azurerm_container_registry.main" "$ACR_NAME"; then
    :
  fi
  if ! untrack_name_mismatch "azurerm_storage_account.main" "$STORAGE_ACCOUNT_NAME"; then
    if state_has "azurerm_storage_share.app"; then
      remove_state "azurerm_storage_share.app"
    fi
  fi

  reconcile_resource "azurerm_container_registry.main" "$ACR_ID" "Container Registry"
  reconcile_resource "azurerm_storage_account.main" "$STORAGE_ACCOUNT_ID" "storage account"
  reconcile_file_share
  reconcile_resource "azurerm_kubernetes_cluster.main" "$AKS_ID" "AKS cluster"
  reconcile_acr_pull_assignment
}

terraform_apply() {
  terraform apply -input=false -auto-approve \
    -lock-timeout="$TF_LOCK_TIMEOUT" \
    -parallelism="$TERRAFORM_PARALLELISM" \
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
      rm -f "$apply_log"
      return
    fi

    if grep -Eq "Provider produced inconsistent result after apply|Root object was present, but now absent|ResourceNotFound|already exists - to be managed via Terraform|RoleAssignmentExists|Another operation.*in progress" "$apply_log"; then
      rm -f "$apply_log"
      if [ "$attempt" -lt "$max_attempts" ]; then
        echo "Azure is still reconciling a previous resource operation. Waiting 45 seconds before retry ${attempt}/${max_attempts}."
        sleep 45
        attempt=$((attempt + 1))
        continue
      fi
    fi

    rm -f "$apply_log"
    exit 1
  done

  echo "Terraform could not reconcile the Azure state after ${max_attempts} attempts." >&2
  exit 1
}

terraform_apply_with_recovery

ACR_NAME="$(terraform output -raw acr_name)"
ACR_LOGIN_SERVER="$(terraform output -raw acr_login_server)"
AKS_NAME="$(terraform output -raw aks_name)"
RESOURCE_GROUP="$(terraform output -raw resource_group_name)"
STORAGE_ACCOUNT_NAME="$(terraform output -raw storage_account_name)"
STORAGE_ACCOUNT_KEY="$(terraform output -raw storage_account_key)"
FILE_SHARE_NAME="$(terraform output -raw file_share_name)"
LOG_ANALYTICS_NAME="$(terraform output -raw log_analytics_workspace_name)"
APP_INSIGHTS_NAME="$(terraform output -raw application_insights_name)"
APP_INSIGHTS_CONNECTION_STRING="$(terraform output -raw application_insights_connection_string)"

popd >/dev/null
unset ARM_ACCESS_KEY

if [ -z "$APP_INSIGHTS_CONNECTION_STRING" ]; then
  echo "Application Insights was provisioned without a connection string." >&2
  exit 1
fi

IMAGE_URI="${ACR_LOGIN_SERVER}/${IMAGE_NAME}:${IMAGE_TAG}"
LATEST_URI="${ACR_LOGIN_SERVER}/${IMAGE_NAME}:latest"

echo "Building and publishing the application image..."
az acr login --name "$ACR_NAME"
docker build -t "$IMAGE_URI" -t "$LATEST_URI" .
docker push "$IMAGE_URI"
docker push "$LATEST_URI"

echo "Deploying to AKS..."
az aks get-credentials --resource-group "$RESOURCE_GROUP" --name "$AKS_NAME" --overwrite-existing >/dev/null

dump_k8s_diagnostics() {
  echo "Kubernetes diagnostics:"
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
content = content.replace("__ASSET_VERSION__", "${IMAGE_TAG}")
print(content)
PY
}

kubectl apply -f k8s/namespace.yaml
kubectl -n "$NAMESPACE" create secret generic azure-storage-secret \
  --from-literal=azurestorageaccountname="$STORAGE_ACCOUNT_NAME" \
  --from-literal=azurestorageaccountkey="$STORAGE_ACCOUNT_KEY" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NAMESPACE" create secret generic application-insights-secret \
  --from-literal=connection-string="$APP_INSIGHTS_CONNECTION_STRING" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f k8s/azure-monitor-agentconfig.yaml
kubectl apply -f k8s/service.yaml
render_deployment | kubectl apply -f -
kubectl apply -f k8s/hpa.yaml

if ! kubectl -n "$NAMESPACE" rollout status deployment/neuroscope-mri --timeout="$K8S_ROLLOUT_TIMEOUT"; then
  dump_k8s_diagnostics
  exit 1
fi

echo "Waiting for the public endpoint..."
public_attempts=$((PUBLIC_ENDPOINT_TIMEOUT_SECONDS / 10))
PUBLIC_HOST=""
for _ in $(seq 1 "$public_attempts"); do
  PUBLIC_HOST="$(kubectl -n "$NAMESPACE" get service neuroscope-mri -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)"
  if [ -z "$PUBLIC_HOST" ]; then
    PUBLIC_HOST="$(kubectl -n "$NAMESPACE" get service neuroscope-mri -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
  fi
  if [ -n "$PUBLIC_HOST" ]; then
    break
  fi
  sleep 10
done

if [ -z "$PUBLIC_HOST" ]; then
  echo "AKS did not assign a public LoadBalancer endpoint within ${PUBLIC_ENDPOINT_TIMEOUT_SECONDS} seconds." >&2
  dump_k8s_diagnostics
  exit 1
fi

APPLICATION_URL="http://${PUBLIC_HOST}"
endpoint_ready=false
for _ in $(seq 1 30); do
  if curl --fail --silent --show-error --max-time 15 "${APPLICATION_URL}/healthz" >/dev/null; then
    endpoint_ready=true
    break
  fi
  sleep 5
done

if [ "$endpoint_ready" != "true" ]; then
  echo "The LoadBalancer address was assigned, but the application health endpoint did not respond." >&2
  dump_k8s_diagnostics
  exit 1
fi

echo "Deployment complete."
echo "Application URL: ${APPLICATION_URL}"
echo "Azure resource group: ${RESOURCE_GROUP}"
echo "Azure Monitor workspace: ${LOG_ANALYTICS_NAME}"
echo "Application Insights: ${APP_INSIGHTS_NAME}"
echo "Azure Files share: ${STORAGE_ACCOUNT_NAME}/${FILE_SHARE_NAME}"

# Make the public address easy to find after a successful GitHub Actions run.
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "## NeuroScope deployment is live"
    echo
    echo "- **Application:** [Open NeuroScope MRI](${APPLICATION_URL})"
    echo "- **Resource group:** ${RESOURCE_GROUP}"
    echo "- **Azure Monitor workspace:** ${LOG_ANALYTICS_NAME}"
    echo "- **Application Insights:** ${APP_INSIGHTS_NAME}"
  } >> "$GITHUB_STEP_SUMMARY"
fi
