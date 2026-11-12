# BrewChain - a coffee shop chain on Kubernetes

Four coffee shops, one brand. Customers preorder from their phone, staff work through a live queue,
stock is tracked per shop, members earn points and move up tiers, and Kubernetes adds pods (and on AWS,
servers) when a rush of customers arrives.

```
Phones / laptops
      |
  gateway (web pages + /api router)          <- public entry, autoscaled
      |-------------------|-----------------|
  menu-inventory       orders            loyalty      <- each autoscaled (HPA)
      |-------------------|-----------------|
                    PostgreSQL               <- pod locally, Amazon RDS on AWS
```

| Folder | What is inside |
| --- | --- |
| `app/` | One Python (FastAPI) image that runs as any of the 4 services (`SERVICE=menu/orders/loyalty/gateway`), the database schema, and the 3 web pages |
| `k8s/base/` | Deployments, Services, ConfigMap and HorizontalPodAutoscalers |
| `k8s/local/` | Adds PostgreSQL as a StatefulSet for minikube |
| `k8s/aws/` | Patches for EKS: public load balancer, node pool, bigger pods |
| `k8s/loadtest/` | Locust: hundreds of simulated customers for the rush demo |
| `scripts/` | `local-up.sh`, `aws-up.sh`, `aws-down.sh`, `smoke_test.py` |

## Web pages

| Page | Who | URL |
| --- | --- | --- |
| Order ahead | Customers on phones | `/` |
| Staff | Barista at one shop: queue + inventory | `/staff.html` |
| Chain dashboard | Owner: all shops, pods, loyalty | `/admin.html` |

## Tools to install (once)

- Docker Desktop - https://www.docker.com/products/docker-desktop
- minikube - https://minikube.sigs.k8s.io/docs/start/
- kubectl - https://kubernetes.io/docs/tasks/tools/
- AWS CLI v2 - https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html
- eksctl 0.195 or newer - https://eksctl.io/installation/
- Python 3 (for the test script) and, on Windows, Git for Windows so you can run the `.sh` scripts in **Git Bash**

Check: `docker version`, `minikube version`, `kubectl version --client`, `aws --version`, `eksctl version`.

**Windows quick install** (PowerShell, then close it and open a NEW Git Bash window so the commands are found):

```powershell
winget install Kubernetes.minikube
winget install Kubernetes.kubectl
winget install Amazon.AWSCLI
winget install eksctl.eksctl      # or: choco install eksctl
```

Docker Desktop must be **running** before `local-up.sh`. A 2-core laptop with 16 GB RAM is enough: the local setup
uses 2 CPUs and 4 GB, with small pods (max 4 per service). The big scaling show is on AWS, which doesn't use your laptop.

## 0. Quick check without Kubernetes (optional, 2 minutes)

```bash
docker compose up --build
# open http://localhost:8080      stop with Ctrl+C, then: docker compose down
```

## 1. Run on your laptop (minikube)

```bash
bash scripts/local-up.sh
kubectl port-forward --address 0.0.0.0 -n brewchain svc/gateway 8080:80     # leave running
```

Open http://localhost:8080. Friends on the same Wi-Fi open `http://<your-laptop-IP>:8080`
(find the IP with `ipconfig` on Windows or `ipconfig getifaddr en0` on Mac; allow the firewall prompt).

Test it: `python scripts/smoke_test.py http://localhost:8080` - should print `All 16 checks passed`.

## 2. Watch autoscaling (local)

Terminal A: `kubectl get hpa,pods -n brewchain -w`

Terminal B:
```bash
kubectl apply -k k8s/loadtest
kubectl port-forward -n brewchain svc/locust 8089:8089
```
Open http://localhost:8089, set **100 users, spawn rate 10** (laptop) or **300 users, spawn rate 20** (AWS), Start. Within 1-2 minutes the HPAs
show CPU above 50% and new pods appear. The admin page lists the new pod names.
Stop the test and pods shrink back after about a minute. Remove Locust: `kubectl delete -k k8s/loadtest`.

## 3. Deploy to AWS

```bash
aws configure                       # access key, secret, region ap-south-1
export DB_PASSWORD=$(openssl rand -hex 12)   # database password; keep it for re-runs, never commit it
bash scripts/aws-up.sh              # about 30 minutes, prints the public URL at the end
python scripts/smoke_test.py http://<the-url>
```

What it creates: EKS cluster in **Auto Mode** (AWS manages the servers), a container registry (ECR),
a PostgreSQL database (RDS `db.t4g.micro`, not reachable from the internet), a public Network Load Balancer,
and a node pool of small 2-vCPU servers capped at 6.

Rush demo on AWS: same Locust steps as above, plus `kubectl get nodes -w` to watch new EC2 servers join
when pods no longer fit (takes 1-2 minutes).

**Delete it all when finished - it costs money every hour it runs:**
```bash
bash scripts/aws-down.sh
```

If `eksctl create cluster` is refused on a brand-new AWS account, your account may be on the Free plan;
upgrade to the Paid plan in the Billing console (your sign-up credits still apply).

## Useful commands

```bash
kubectl get all -n brewchain                     # everything
kubectl logs -n brewchain deploy/orders          # logs of one service
kubectl top pods -n brewchain                    # CPU / memory per pod
kubectl delete pod -n brewchain -l app=orders    # kill orders pods: watch Kubernetes recreate them
kubectl exec -it -n brewchain statefulset/postgres -- psql -U brew brewchain     # SQL shell (local)
```

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `minikube: command not found` | Not installed, or the terminal was opened before installing. Install it and open a NEW terminal |
| Pods `Pending` on minikube | Not enough CPU. Close other apps, then `minikube delete` and run `bash scripts/local-up.sh` again |
| Pods `ImagePullBackOff` on minikube | Image not built inside minikube: `minikube image build -t brewchain:1.0 app` |
| HPA shows `<unknown>` | metrics-server still starting; wait 1-2 minutes |
| Pods `CrashLoopBackOff`, log says "waiting for database" | Database not ready or wrong DATABASE_URL secret |
| AWS URL does not open | NLB DNS takes 2-3 minutes after it appears |
| Phone cannot reach laptop | Same Wi-Fi? Firewall allowed? Used `--address 0.0.0.0`? |

## Contributors

BrewChain was built as a team project for the MACSE512 Operating Systems course at VIT Chennai (SCOPE) by:

- Linus Edric
- Karthik Raktate
- Naveen M
