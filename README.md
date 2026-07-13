# NeuroScope MRI

NeuroScope MRI is a Flask web app for brain MRI review. It supports:

- 3D multimodal MRI tumor segmentation with the MONAI `brats_mri_segmentation` bundle.
- Image, video, and webcam frame overlays for quick visual review.
- A polished white clinical UI with animated MRI visualization.
- One-command Azure deployment through Terraform, AKS, ACR, Azure Files, Log Analytics, Azure Monitor, GitHub Actions, and Jenkins.

This is a research and engineering project, not a medical device. Do not use it for diagnosis without clinical validation and qualified medical review.

## Model

The production MRI path is wired for MONAI Model Zoo's `brats_mri_segmentation` bundle. That bundle is designed for four aligned MRI volumes: T1c, T1, T2, and FLAIR.

Single image, video, and live-camera modes use a lightweight visual triage overlay because a random 2D screenshot or webcam frame is not enough for diagnostic-grade tumor segmentation.

## Run Locally

If your machine is low on disk space, skip local dependency installation, model download, Docker builds, and Terraform init. The repository itself is tiny; the heavy parts are designed to run in GitHub Actions, Jenkins, and Azure.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5000`.

To download the MONAI model bundle locally:

```bash
python scripts/download_model.py
```

## Deploy With Only GitHub Secrets

The default Terraform settings are sized for an Azure free/trial demo: one `Standard_D2s_v7` AKS node, one app replica, and a 20 GB Azure Files share. It is not a true zero-cost production setup because AKS worker nodes, storage, and monitoring can consume credits. For heavier 3D MRI inference, increase `node_vm_size`, `node_count`, and Kubernetes memory limits after the demo works.

If Azure rejects the VM size in your region, copy the smallest allowed size from the workflow error and set `AKS_NODE_VM_SIZE` in `.github/workflows/deploy-azure.yml`.

Create one GitHub secret named `AZURE_CREDENTIALS` with Azure service principal JSON:

```json
{
  "clientId": "00000000-0000-0000-0000-000000000000",
  "clientSecret": "your-secret",
  "subscriptionId": "00000000-0000-0000-0000-000000000000",
  "tenantId": "00000000-0000-0000-0000-000000000000"
}
```

Then run the `Deploy NeuroScope MRI to Azure` workflow, or push to `main`.

The workflow automatically:

1. Creates Terraform remote state storage.
2. Provisions Azure resources with Terraform.
3. Builds and pushes the Docker image to Azure Container Registry.
4. Deploys the app to AKS.
5. Mounts Azure Files for uploads, results, and model cache.
6. Enables Azure Monitor Container Insights through Log Analytics.

## Jenkins

The included `Jenkinsfile` runs the same deployment script. Jenkins cannot read GitHub Actions secrets directly, so add one Jenkins secret text credential with ID `azure-credentials-json` containing the same Azure JSON if you want Jenkins to deploy it.

## Azure Services Created

- Azure Kubernetes Service for the running app.
- Azure Container Registry for container images.
- Azure Files for persistent uploads, generated overlays, and model cache.
- Log Analytics Workspace for Azure Monitor Container Insights.
- Application Insights workspace resource for web monitoring expansion.
- Terraform backend storage account and container.

Azure Container Service (ACS) is retired; this project uses AKS, the supported Azure Kubernetes platform.

## Monitoring

The app exposes:

- `/healthz` for health probes.
- `/metrics` for Prometheus-style application counters.

In Azure Portal, open the AKS resource and use **Insights** to see CPU, memory, pod health, restarts, and logs. The Terraform deployment connects AKS to the Log Analytics workspace automatically.
