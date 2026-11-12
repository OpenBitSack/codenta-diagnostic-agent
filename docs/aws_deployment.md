# AWS deployment notes (design only — not deployed)

Nothing in this repository has been deployed to AWS. Local development needs no AWS credentials.

| Concern | Local prototype | AWS mapping |
|---|---|---|
| API | uvicorn / FastAPI container | ECS Fargate service behind an ALB (private subnets), or API Gateway → ECS |
| Workers | `python -m codenta_diag.worker` (thread pool) | Separate ECS service; scale on queue depth / oldest-message age |
| Queue | DB-backed `jobs` table (atomic claims, leases, retries, dead-letter) | SQS standard queue + DLQ (`maxReceiveCount` = `job_max_attempts`); visibility timeout = lease; or keep the Postgres queue at moderate volume |
| Database | SQLite (dev) / PostgreSQL (compose; suite verified on PostgreSQL 16) | RDS / Aurora PostgreSQL, Multi-AZ, encryption at rest (KMS), IAM auth |
| Similarity search | Python cosine over hashed embeddings | `pgvector` extension on Aurora PostgreSQL with HNSW index |
| Cache | `cache_entries` table + in-process map | Same table, or ElastiCache Redis for hot keys |
| Config registry | YAML in the image | S3 (versioned bucket) or AppConfig; still versioned and fingerprinted; changes go through code review |
| LLM | Anthropic API (optional) | Amazon Bedrock (Claude) for data-residency / VPC endpoints; API key or IAM in Secrets Manager |
| Auth | API keys | Cognito or corporate OIDC IdP; JWT validation at ALB or in-app |
| Secrets | env vars | Secrets Manager / SSM Parameter Store |
| Logs / metrics | stdout + `/v1/metrics` | CloudWatch Logs (no note text), CloudWatch metrics / Prometheus scrape of `/v1/metrics`, alarms on DLQ depth and queue age |
| Audit | `audit_log` table | Same table + periodic export to S3 (Object Lock) for retention |
| Network | localhost | VPC, private subnets, VPC endpoints for SQS/S3/Bedrock/Secrets Manager, no public DB |
| Read-only production traces | separate table, read-only store class | Separate read-only DB user / replica, or S3 trace bucket with read-only IAM policy for the diagnostic service |
| Release of approved fixes | out of scope (human decision recorded only) | Existing CI/CD with manual approval; the diagnostic service has **no** IAM permission to modify production config |
