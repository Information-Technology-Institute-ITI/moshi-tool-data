output "web_elastic_ip" {
  description = "Create the domain A record at this address before enabling Nginx."
  value       = aws_eip.web.public_ip
}

output "web_private_url" {
  description = "Private worker/API address; port 8765 accepts traffic only from the processing SG."
  value       = "http://${local.web_private_ip}:8765"
}

output "web_instance_id" {
  value = aws_instance.web.id
}

output "processing_instance_id" {
  value = aws_instance.processing.id
}

output "web_repository_url" {
  value = aws_ecr_repository.web.repository_url
}

output "processing_repository_url" {
  value = aws_ecr_repository.processing.repository_url
}

output "deployed_images" {
  value = {
    web        = local.web_image
    processing = local.processing_image
  }
}

output "workspace_volume_id" {
  value = aws_ebs_volume.workspace.id
}

output "processing_cache_volume_id" {
  value = aws_ebs_volume.processing_cache.id
}

