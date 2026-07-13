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
  default     = 2
}

variable "node_vm_size" {
  description = "AKS node VM size. Increase this for heavier 3D inference workloads."
  type        = string
  default     = "Standard_D4s_v5"
}

variable "file_share_name" {
  description = "Azure Files share mounted by the application."
  type        = string
  default     = "neuroscope-data"
}
