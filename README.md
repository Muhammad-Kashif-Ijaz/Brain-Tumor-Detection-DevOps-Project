# CerebraVue MRI

CerebraVue MRI is a Flask research workspace for brain MRI review. It supports:

- 3D multimodal MRI tumor segmentation with the MONAI `brats_mri_segmentation` bundle.
- SegFormer-B2 slice segmentation for MRI images, clearly separated multi-view image sheets, sampled video frames, and camera captures.
- A full-screen transparent imaging workstation with source/result comparison, collapsible study controls, export, and responsive review states.
- One-command Azure deployment through Terraform, AKS, ACR, Azure Files, Log Analytics, Azure Monitor, GitHub Actions, and Jenkins.

This is a research and engineering project, not a medical device. Do not use it for diagnosis without clinical validation and qualified medical review.

## Model

The volumetric path uses MONAI Model Zoo's `brats_mri_segmentation` bundle version `0.5.4`. It is designed for four aligned MRI volumes: T1c, T1, T2, and FLAIR, and returns whole-tumor, tumor-core, and enhancing-tumor segmentation. This is the appropriate route when coverage across the whole scan and all anatomical planes matters. The bundled review image contains sagittal, coronal, axial, and all-detected-regions overview panels; the overview preserves every voxel region segmented by the model in the submitted volume.

The MONAI bundle documentation reports validation Dice values of 0.8559 for tumor core, 0.9026 for whole tumor, and 0.7905 for enhancing tumor on its BraTS 2018 validation data. Those are dataset-specific research measurements, not a guarantee for a different scanner, population, tumor type, or clinical workflow.

Image, video, and camera modes use the MIT-licensed [brain MRI SegFormer-B2 checkpoint](https://huggingface.co/kiselyovd/brain-mri-segmentation). It was trained for binary tumor segmentation on lower-grade glioma MRI slices with a patient-level test split. Inference averages original and horizontally mirrored predictions before mask cleanup. These modes are slice-level research review only; scanner protocol differences, screenshots, compression, and camera capture can materially reduce performance.

When a single uploaded image contains up to four clearly separated MRI views, the application detects the panel gutters, reviews each panel independently, and returns one labeled thermal contact sheet. This is a convenience feature for MRI image sheets; it does not turn a screenshot into a full volumetric examination. Brain X-rays are not a supported tumor-segmentation input because they do not provide the soft-tissue information required for reliable tumor localization.

Both checkpoints are downloaded while the container image is built. They are stored under `/app/models`, outside the Azure Files mount, so every deployed pod starts with the same model artifacts and does not download them during a request.

## Run Locally

If your machine is low on disk space, skip local dependency installation, model download, Docker builds, and Terraform init. The repository itself is tiny; the heavy parts are designed to run in GitHub Actions, Jenkins, and Azure.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5000`.

To preview only the interface without installing Python or the model packages:

```bash
node scripts/ui_preview_server.mjs
```

Open `http://127.0.0.1:5080`. Analysis endpoints are intentionally unavailable in this lightweight preview.

To download both trained model packages locally:

```bash
python scripts/download_model.py
```

## Deploy With Only GitHub Secrets

The default Terraform settings are sized for an Azure free/trial demo: one `Standard_D2s_v7` AKS node, one app replica, and a 20 GB Azure Files share. The GitHub workflow uses `ENVIRONMENT=demo` so failed production experiments do not pollute the demo deployment state. It is not a true zero-cost production setup because AKS worker nodes, storage, and monitoring can consume credits. For heavier 3D MRI inference, increase `node_vm_size`, `node_count`, and Kubernetes memory limits after the demo works.

If Azure rejects the VM size in your region, copy the smallest allowed size from the workflow error and set `AKS_NODE_VM_SIZE` in `.github/workflows/deploy-azure.yml`.

The deployment script checks candidate Azure regions before running Terraform. If the default region has 0 vCPU quota, it tries the next region listed in `AZURE_LOCATION_CANDIDATES`. If every checked region has 0 quota, request a quota increase in Azure Portal or change `AZURE_LOCATION_CANDIDATES` to include a region where your subscription has quota.

Terraform state storage may stay in the original region even when the app region changes. The deployment script reuses the existing state resource group location and deploys the AKS app resources in the selected quota-friendly region.

The Kubernetes deployment is applied cleanly each run: the workflow renders the real container image directly into the manifest, replaces the previous demo deployment, waits up to 20 minutes, and prints pod events/logs automatically if rollout fails.

Create one GitHub secret named `AZURE_CREDENTIALS` with Azure service principal JSON:

```json
{
  "clientId": "00000000-0000-0000-0000-000000000000",
  "clientSecret": "your-secret",
  "subscriptionId": "00000000-0000-0000-0000-000000000000",
  "tenantId": "00000000-0000-0000-0000-000000000000"
}
```

Then push to `main`. The model container smoke test builds the image, requires the trained slice checkpoint to load, and exercises both single-view and multi-view MRI uploads. Azure deployment starts only after that check succeeds. You can still use the manual deployment workflow when needed.

The workflow automatically:

1. Creates Terraform remote state storage.
2. Provisions Azure resources with Terraform.
3. Builds and pushes the Docker image to Azure Container Registry.
4. Deploys the app to AKS.
5. Mounts Azure Files for temporary uploads and generated review images.
6. Enables Azure Monitor Container Insights through Log Analytics.

## Jenkins

The included `Jenkinsfile` runs the same deployment script. Jenkins cannot read GitHub Actions secrets directly, so add one Jenkins secret text credential with ID `azure-credentials-json` containing the same Azure JSON if you want Jenkins to deploy it.

## Azure Services Created

- Azure Kubernetes Service for the running app.
- Azure Container Registry for container images.
- Azure Files for generated review images and temporary application storage. Model checkpoints are baked into the container image.
- Log Analytics Workspace for Azure Monitor Container Insights.
- Application Insights workspace resource for web monitoring expansion.
- Terraform backend storage account and container.

Azure Container Service (ACS) is retired; this project uses AKS, the supported Azure Kubernetes platform.

## Hospital Pilot Boundary

This repository is not a cleared medical device and must not be used for autonomous diagnosis. Before any hospital pilot, validate it on local scanner protocols and patient populations, perform radiologist acceptance testing, complete privacy and security review, define retention and audit policies, and obtain the regulatory approval required in the deployment jurisdiction. A browser camera is a live-frame review source, not a direct MRI scanner connection. Direct scanner or PACS ingestion requires a separately configured DICOM/DICOMweb gateway and hospital network integration.

## Monitoring

The app exposes:

- `/healthz` for health probes.
- `/metrics` for Prometheus-style application counters.

In Azure Portal, open the AKS resource and use **Insights** to see CPU, memory, pod health, restarts, and logs. The Terraform deployment connects AKS to the Log Analytics workspace automatically.
