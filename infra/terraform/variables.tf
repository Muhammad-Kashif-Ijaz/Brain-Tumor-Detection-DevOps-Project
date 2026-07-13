variable "project_name" {
  description = "Short project name used in Azure resource names."
  type        = string
  default     = "neuroscope"
}

variable "environment" {
  description = "Deployment environment name."
  type        = string
  default     = "prod"
}

variable "location" {
  description = "Azure region where application infrastructure is created."
  type        = string
  default     = "eastus"
}

variable "node_count" {
  description = "Initial AKS node count."
  type        = number
  default     = 1
}

variable "node_vm_size" {
  description = "AKS node VM size. B2ms is a low-cost demo default; increase this for heavier 3D inference workloads."
  type        = string
  default     = "Standard_B2ms"
}

variable "file_share_name" {
  description = "Azure Files share mounted by the application."
  type        = string
  default     = "neuroscope-data"
}

variable "file_share_quota_gb" {
  description = "Azure Files quota in GB for uploads, overlays, and model cache."
  type        = number
  default     = 20
}
