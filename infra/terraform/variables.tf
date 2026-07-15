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

variable "name_suffix" {
  description = "Stable lowercase suffix used for globally unique Azure resource names."
  type        = string
}

variable "node_count" {
  description = "Initial AKS node count."
  type        = number
  default     = 1
}

variable "node_vm_size" {
  description = "AKS node VM size. Standard_D2s_v7 is a small size from the current allowed eastus list for this subscription."
  type        = string
  default     = "Standard_D2s_v7"
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
