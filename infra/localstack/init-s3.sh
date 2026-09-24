#!/bin/bash
# LocalStack ready hook: create the POC bucket and its lifecycle rules (design.md §8.5a, R10.2).
set -euo pipefail

BUCKET=clinsync-poc

awslocal s3 mb "s3://${BUCKET}"
awslocal s3api put-bucket-lifecycle-configuration \
  --bucket "${BUCKET}" \
  --lifecycle-configuration '{
    "Rules": [
      {
        "ID": "expire-incoming",
        "Filter": {"Prefix": "ClinSync/incoming/"},
        "Status": "Enabled",
        "Expiration": {"Days": 1}
      },
      {
        "ID": "expire-staging",
        "Filter": {"Prefix": "ClinSync/staging/"},
        "Status": "Enabled",
        "Expiration": {"Days": 7}
      }
    ]
  }'
