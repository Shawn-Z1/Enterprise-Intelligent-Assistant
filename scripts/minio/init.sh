#!/bin/sh
# 初始化独立存储身份与 Bucket；凭证不回显，任一步失败即中止。
set -eu
mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
mc mb --ignore-existing local/milvus-data >/dev/null
mc mb --ignore-existing "local/$APP_MINIO_BUCKET" >/dev/null

# 每个身份只能访问自己的 Bucket，阻止应用触碰 Milvus 数据。
create_identity() {
  # 参数依次为身份名、密钥、Bucket 和策略名称；无正文返回。
  identity="$1"
  secret="$2"
  bucket="$3"
  policy="$4"
  cat > "/tmp/$policy.json" <<EOF
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["s3:ListBucket","s3:GetBucketLocation"],"Resource":["arn:aws:s3:::$bucket"]},{"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:DeleteObject","s3:AbortMultipartUpload","s3:ListMultipartUploadParts"],"Resource":["arn:aws:s3:::$bucket/*"]}]}
EOF
  mc admin user add local "$identity" "$secret" >/dev/null
  mc admin policy create local "$policy" "/tmp/$policy.json" >/dev/null
  mc admin policy attach local "$policy" --user "$identity" >/dev/null
}
create_identity "$MILVUS_ACCESS_KEY" "$MILVUS_SECRET_KEY" milvus-data milvus-storage
create_identity "$APP_MINIO_ACCESS_KEY" "$APP_MINIO_SECRET_KEY" "$APP_MINIO_BUCKET" application-storage
echo 'MinIO 独立 Bucket 与身份初始化完成'
