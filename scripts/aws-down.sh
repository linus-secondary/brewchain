#!/usr/bin/env bash
# Delete EVERYTHING BrewChain created on AWS so the bill stops.
set -uo pipefail
CLUSTER=brewchain
REGION=${AWS_REGION:-ap-south-1}

echo "==> Removing the app and its load balancer"
kubectl delete -k k8s/loadtest --ignore-not-found 2>/dev/null
kubectl delete namespace brewchain --ignore-not-found --wait=true
kubectl delete -f k8s/aws/nodepool.yaml --ignore-not-found

echo "==> Deleting the RDS database (about 5-10 min)"
aws rds delete-db-instance --db-instance-identifier brewchain-db --skip-final-snapshot \
  --delete-automated-backups --region $REGION >/dev/null 2>&1
aws rds wait db-instance-deleted --db-instance-identifier brewchain-db --region $REGION
aws rds delete-db-subnet-group --db-subnet-group-name brewchain-db --region $REGION 2>/dev/null
VPC_ID=$(aws eks describe-cluster --name $CLUSTER --region $REGION --query cluster.resourcesVpcConfig.vpcId --output text 2>/dev/null)
SG=$(aws ec2 describe-security-groups --region $REGION --filters Name=group-name,Values=brewchain-db Name=vpc-id,Values=$VPC_ID \
       --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null)
[ -n "$SG" ] && [ "$SG" != "None" ] && aws ec2 delete-security-group --group-id $SG --region $REGION

echo "==> Deleting the image repository"
aws ecr delete-repository --repository-name brewchain --force --region $REGION >/dev/null 2>&1

echo "==> Deleting the EKS cluster and its network (10-15 min)"
eksctl delete cluster --name $CLUSTER --region $REGION --wait

echo "Done. Check the AWS console (EC2, RDS, EKS, Load Balancers) shows nothing left."
