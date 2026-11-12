#!/usr/bin/env bash
# Deploy BrewChain to AWS: EKS Auto Mode + Amazon RDS PostgreSQL + Amazon ECR.
# Needs: AWS CLI (logged in with `aws configure`), eksctl >= 0.195, kubectl, Docker.
# Run from the project folder:  bash scripts/aws-up.sh
# Takes about 30 minutes. COSTS MONEY while it runs - delete with scripts/aws-down.sh
set -euo pipefail

CLUSTER=brewchain
REGION=${AWS_REGION:-ap-south-1}                 # Mumbai. Change if you like.
: "${DB_PASSWORD:?Set DB_PASSWORD first (letters and digits only, 12+ characters), e.g.  export DB_PASSWORD=$(openssl rand -hex 12)}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGISTRY=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com
IMAGE=$REGISTRY/brewchain

echo "==> 1/7 EKS cluster with Auto Mode (15-20 min the first time)"
if ! eksctl get cluster --name $CLUSTER --region $REGION >/dev/null 2>&1; then
  eksctl create cluster --name $CLUSTER --region $REGION --enable-auto-mode
fi
aws eks update-kubeconfig --name $CLUSTER --region $REGION

echo "==> 2/7 Build the image and push it to Amazon ECR"
aws ecr describe-repositories --repository-names brewchain --region $REGION >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name brewchain --region $REGION >/dev/null
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $REGISTRY
docker build --platform linux/amd64 -t $IMAGE:1.0 app
docker push $IMAGE:1.0

echo "==> 3/7 Network for the database"
VPC_ID=$(aws eks describe-cluster --name $CLUSTER --region $REGION --query cluster.resourcesVpcConfig.vpcId --output text)
VPC_CIDR=$(aws ec2 describe-vpcs --vpc-ids $VPC_ID --region $REGION --query 'Vpcs[0].CidrBlock' --output text)
SUBNETS=$(aws eks describe-cluster --name $CLUSTER --region $REGION --query 'cluster.resourcesVpcConfig.subnetIds' --output text)
aws rds describe-db-subnet-groups --db-subnet-group-name brewchain-db --region $REGION >/dev/null 2>&1 \
  || aws rds create-db-subnet-group --db-subnet-group-name brewchain-db \
       --db-subnet-group-description "BrewChain database subnets" --subnet-ids $SUBNETS --region $REGION >/dev/null
SG=$(aws ec2 describe-security-groups --region $REGION \
       --filters Name=group-name,Values=brewchain-db Name=vpc-id,Values=$VPC_ID \
       --query 'SecurityGroups[0].GroupId' --output text)
if [ "$SG" = "None" ]; then
  SG=$(aws ec2 create-security-group --group-name brewchain-db --description "BrewChain RDS - Postgres from the cluster VPC only" \
         --vpc-id $VPC_ID --region $REGION --query GroupId --output text)
  aws ec2 authorize-security-group-ingress --group-id $SG --protocol tcp --port 5432 --cidr $VPC_CIDR --region $REGION >/dev/null
fi

echo "==> 4/7 Amazon RDS PostgreSQL (about 10 min)"
if ! aws rds describe-db-instances --db-instance-identifier brewchain-db --region $REGION >/dev/null 2>&1; then
  aws rds create-db-instance --db-instance-identifier brewchain-db --region $REGION \
    --engine postgres --db-instance-class db.t4g.micro --allocated-storage 20 \
    --master-username brewadmin --master-user-password "$DB_PASSWORD" --db-name brewchain \
    --db-subnet-group-name brewchain-db --vpc-security-group-ids $SG \
    --no-publicly-accessible --backup-retention-period 0 >/dev/null
fi
aws rds wait db-instance-available --db-instance-identifier brewchain-db --region $REGION
DB_HOST=$(aws rds describe-db-instances --db-instance-identifier brewchain-db --region $REGION \
            --query 'DBInstances[0].Endpoint.Address' --output text)

echo "==> 5/7 metrics-server (needed for autoscaling)"
aws eks create-addon --cluster-name $CLUSTER --addon-name metrics-server --region $REGION >/dev/null 2>&1 \
  || kubectl get deployment metrics-server -n kube-system >/dev/null 2>&1 \
  || kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml

echo "==> 6/7 Deploy BrewChain"
kubectl apply -f k8s/aws/nodepool.yaml
kubectl apply -f k8s/base/namespace.yaml
kubectl create secret generic db-credentials -n brewchain \
  --from-literal=DATABASE_URL="postgresql://brewadmin:$DB_PASSWORD@$DB_HOST:5432/brewchain?sslmode=require" \
  --dry-run=client -o yaml | kubectl apply -f -
sed -i.bak "s|newName: .*|newName: $IMAGE|" k8s/aws/kustomization.yaml && rm -f k8s/aws/kustomization.yaml.bak
kubectl apply -k k8s/aws
# put the placeholder back so your AWS account id is never committed by accident
sed -i.bak "s|newName: .*|newName: ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/brewchain|" k8s/aws/kustomization.yaml && rm -f k8s/aws/kustomization.yaml.bak
for d in menu-inventory orders loyalty gateway; do
  kubectl rollout status deployment/$d -n brewchain --timeout=600s
done

echo "==> 7/7 Waiting for the public load balancer"
for i in $(seq 1 60); do
  HOST=$(kubectl get svc gateway -n brewchain -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)
  [ -n "$HOST" ] && break
  sleep 10
done
kubectl get nodes,pods,hpa -n brewchain
echo
echo "BrewChain is live at:  http://$HOST"
echo "(The address can take 2-3 more minutes to start working. Share it with your friends.)"
echo "When you are done:     bash scripts/aws-down.sh"
