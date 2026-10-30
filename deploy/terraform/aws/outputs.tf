output "cluster_name" {
  value = module.eks.cluster_name
}

output "cluster_endpoint" {
  value = module.eks.cluster_endpoint
}

output "region" {
  value = var.region
}

output "kubeconfig_command" {
  description = "Run this once the cluster exists."
  value       = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name}"
}

output "artifact_bucket" {
  value = aws_s3_bucket.artifacts.bucket
}

output "efs_file_system_id" {
  description = "Put this in the efs-sc StorageClass (see README.md)."
  value       = aws_efs_file_system.artifacts.id
}

output "scoring_role_arn" {
  description = "Associated with the chart's service account through Pod Identity; nothing to annotate."
  value       = aws_iam_role.scoring.arn
}

output "private_subnets" {
  value = module.vpc.private_subnets
}
