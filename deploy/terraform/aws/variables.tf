variable "region" {
  description = "AWS region. London keeps the data close to the people reading it."
  type        = string
  default     = "eu-west-2"
}

variable "name" {
  description = "Name prefix for every resource."
  type        = string
  default     = "availabilityiq"
}

variable "environment" {
  description = "Environment label: prod, staging."
  type        = string
  default     = "prod"
}

variable "vpc_cidr" {
  description = "CIDR for the VPC. Three private and three public subnets are carved from it."
  type        = string
  default     = "10.0.0.0/16"
}

variable "kubernetes_version" {
  description = "EKS Kubernetes version. Check the supported list before changing; EKS trails upstream."
  type        = string
  default     = "1.34"
}

variable "node_instance_types" {
  description = "Instance types for the managed node group. The API is small; the weekly job wants memory."
  type        = list(string)
  default     = ["t3.large"]
}

variable "node_min_size" {
  type    = number
  default = 2
}

variable "node_max_size" {
  type    = number
  default = 4
}

variable "node_desired_size" {
  type    = number
  default = 2
}

variable "namespace" {
  description = "Kubernetes namespace the chart is installed into."
  type        = string
  default     = "aiq"
}

variable "service_account" {
  description = "Service account the chart creates; the scoring job and the API run as it."
  type        = string
  default     = "aiq-availabilityiq"
}

variable "tags" {
  description = "Extra tags on every resource."
  type        = map(string)
  default     = {}
}
