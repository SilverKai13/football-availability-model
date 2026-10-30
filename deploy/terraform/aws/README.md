# AWS footprint

One environment: a VPC, an EKS cluster, an EFS file system for the artefact
volume, an S3 bucket that keeps every published predictions file, and the IAM
roles the pods use through EKS Pod Identity.

**This has been validated, linted and scanned in CI. It has not been applied.**
Applying it creates resources that cost money every hour they exist, and the
project was built without spending any. The estimate below is why the
decision was deliberate rather than an oversight; the configuration is
written so that the first apply is a routine one.

## What it creates

| Resource | Why | Notes |
|---|---|---|
| VPC, 3 private + 3 public subnets, one NAT gateway | Nodes in private subnets, load balancers in public | One NAT, not three: the only outbound traffic is the weekly export and image pulls |
| EKS cluster, Kubernetes `var.kubernetes_version` | Runs the chart | Public endpoint for `kubectl`; restrict it to known CIDRs once there are any |
| Managed node group, 2 to 4 × `t3.large` | The API, the page, the weekly job, the monitoring stack | AL2023 AMI; the job's 2Gi request is what sizes the nodes |
| Add-ons: CoreDNS, kube-proxy, VPC CNI, Pod Identity agent, EBS CSI, EFS CSI | The usual, plus the two storage drivers | CSI drivers get their AWS-managed policies through Pod Identity |
| EFS file system + mount targets | The artefact volume, `ReadWriteMany` | So the job and every API replica can mount it wherever they land |
| S3 bucket, versioned, encrypted, private | Archive of every published predictions file | Non-current versions expire after 90 days |
| IAM role `*-scoring` with Pod Identity association | Lets the chart's service account read and write the bucket | Nothing to annotate in the chart; the association is by namespace and name |

## Cost, roughly, in eu-west-2

| Item | Per month |
|---|---|
| EKS control plane | about $73 |
| 2 × t3.large on demand | about $135 |
| NAT gateway | about $37 plus data |
| EFS, S3 at this size | under $5 |
| **Total** | **about $250** |

Spot nodes and a smaller instance type would roughly halve it. It is still
not zero, which is the whole point.

## Applying it

```bash
cd deploy/terraform/aws
cp terraform.tfvars.example terraform.tfvars      # edit
terraform init                                     # after filling in the s3 backend in versions.tf
terraform plan -out=plan.tfplan
terraform apply plan.tfplan
$(terraform output -raw kubeconfig_command)
```

Then the two pieces Terraform does not manage, because they live inside the
cluster:

```bash
# A StorageClass for the artefact volume, using the file system Terraform made.
cat <<EOF | kubectl apply -f -
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: efs-sc
provisioner: efs.csi.aws.com
parameters:
  provisioningMode: efs-ap
  fileSystemId: $(terraform output -raw efs_file_system_id)
  directoryPerms: "700"
  uid: "10001"
  gid: "10001"
EOF

# The monitoring stack, then the chart with the production values.
helm install kube-prometheus-stack prometheus-community/kube-prometheus-stack -n monitoring --create-namespace
helm install aiq ../../chart/availabilityiq -n aiq --create-namespace -f ../../chart/availabilityiq/values-prod.example.yaml
```

## What CI checks

`terraform fmt`, `terraform validate` against the pinned module and provider
versions (the lock file is committed), `tflint` with the AWS ruleset, and a
Trivy misconfiguration scan reported in the log. The scan is not gating: its
HIGH findings on this configuration are the kind that need a judgement call
(customer-managed KMS keys for S3, a public cluster endpoint), and those
judgements are recorded in `deploy/DECISIONS.md` rather than silenced in a
config file.

## Tearing it down

`terraform destroy`. The S3 bucket is versioned, so empty it first or the
destroy stops there, which is the right default for a bucket that holds
every prediction ever published.
