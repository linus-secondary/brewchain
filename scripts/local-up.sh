#!/usr/bin/env bash
# Run BrewChain on your laptop with minikube (sized for 2 CPU cores).
# Needs: Docker Desktop (running), minikube, kubectl.   Run from the project folder:  bash scripts/local-up.sh
set -euo pipefail

for tool in docker minikube kubectl; do
  command -v $tool >/dev/null 2>&1 || { echo "ERROR: '$tool' not found. Install it, then open a NEW terminal. See README step 'Install the tools'."; exit 1; }
done
docker info >/dev/null 2>&1 || { echo "ERROR: Docker Desktop is not running. Start it and wait for the whale icon to stop animating."; exit 1; }

echo "==> 1/5 Starting minikube (2 CPUs, 4 GB RAM)"
minikube status >/dev/null 2>&1 || minikube start --driver=docker --cpus 2 --memory 4g

echo "==> 2/5 Turning on metrics-server (needed for autoscaling)"
minikube addons enable metrics-server

echo "==> 3/5 Building the app image inside minikube"
minikube image build -t brewchain:1.0 app

echo "==> 4/5 Deploying database + 4 services + autoscalers"
kubectl apply -k k8s/local
kubectl rollout status statefulset/postgres -n brewchain --timeout=300s
for d in menu-inventory orders loyalty gateway; do
  kubectl rollout status deployment/$d -n brewchain --timeout=300s
done

echo "==> 5/5 Done"
kubectl get pods,hpa -n brewchain
echo
echo "Open the app (keep this running in its own terminal):"
echo "   kubectl port-forward --address 0.0.0.0 -n brewchain svc/gateway 8080:80"
echo "Then visit http://localhost:8080  (friends on the same Wi-Fi: http://<your-laptop-IP>:8080)"
