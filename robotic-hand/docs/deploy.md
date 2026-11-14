# Phase 5a — deploying `real_server` for a live cloud test

This guide stands up one or two small **CPU** VMs that run the real RT-DETR
inference container, then points `live_run.py` at them. It covers Google Cloud
and Azure, firewall restricted to your own IP, budget alerts, and full teardown.

> ## ⚠️ Plain HTTP only
>
> Phase 5a has **no TLS**. The `X-API-Token` header and every test image travel
> over the public Internet unencrypted. Anyone who can observe the path can
> read the token and the images.
>
> - Use **non-sensitive test images only** — never factory footage, faces,
>   customer parts, or anything you would not publish.
> - Treat the token as disposable, rotate it after the test, and never commit
>   it.
> - Keep the firewall limited to your current public IP (`/32`).
> - Do not reuse this setup for production. TLS, a reverse proxy and secret
>   management are later work.

## What you will end up with

```
edge laptop                         provider VMs (GCP/Azure, CPU)
┌─────────────────────────┐         ┌──────────────────────────┐
│ live_run.py             │  HTTP   │ real_server container A  │
│  camera -> outbox ->    │────────▶│  :8001 /predict          │
│  MQTT -> worker ->      │         └──────────────────────────┘
│  router -> providers    │  HTTP   ┌──────────────────────────┐
│                         │────────▶│ real_server container B  │
│ Mosquitto on localhost  │         └──────────────────────────┘
└─────────────────────────┘
```

The broker and worker stay on the edge/laptop. Only the inference endpoints are
on the VMs, so only port `8001` is exposed. Never expose Mosquitto (`1883`) to
the Internet.

## 0. Before you start

- A Google Cloud project with billing enabled, **or** an Azure subscription.
- `gcloud` CLI (`gcloud init`) **or** `az` CLI (`az login`).
- Docker on the edge machine (for Mosquitto): `docker compose up -d` in
  `robotic-hand/`.
- Python 3.10+ on the edge with `pip install -r requirements.txt`.
- A token generated on the edge:

  ```bash
  export API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
  printf 'API_TOKEN=%s\nMODEL_ID=PekingU/rtdetr_r18vd\nDETECTION_THRESHOLD=0.5\n' \
    "$API_TOKEN" > /tmp/robotic-hand.env
  chmod 600 /tmp/robotic-hand.env
  ```

  Keep `API_TOKEN` in your local `.env` too (placeholder only in
  `.env.example`). It is never written to the reports or container logs.

- Know your public IP; every firewall rule below derives it:

  ```bash
  MY_IP="$(curl -fsS https://ifconfig.me)"
  echo "$MY_IP"
  ```

> VM prices change. Use the cloud pricing calculator and set the budget alert
> (step 5) **before** creating instances.

---

## Google Cloud

### 1. Project and APIs

```bash
gcloud config set project YOUR_PROJECT_ID
gcloud services enable compute.googleapis.com
```

### 2. Budget alert

Console: **Billing → Budgets & alerts → Create budget**

1. Scope: the project you are using.
2. Amount: a small test budget (for example `$10`).
3. Actions: email alerts at **50%**, **90%** and **100%**.
4. Save.

The budget is an alert, not a hard cap; it does not stop the VMs. Teardown in
step 8 is what stops billing.

### 3. Create the VM

```bash
gcloud compute instances create real-infer-a \
  --zone=us-central1-a \
  --machine-type=e2-standard-2 \
  --image-family=ubuntu-2404-lts-amd64 \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=30GB \
  --tags=real-infer

# Optional second provider in a different region for failover tests
gcloud compute instances create real-infer-b \
  --zone=europe-west1-b \
  --machine-type=e2-standard-2 \
  --image-family=ubuntu-2404-lts-amd64 \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=30GB \
  --tags=real-infer
```

`e2-standard-2` (2 vCPU, 8 GB) is the minimum this guide assumes; CPU RT-DETR
is slow, so latency here is a routing/failover measurement, not a production
number. Larger (4 vCPU+) VMs reduce it.

### 4. Firewall: only your IP, only 8001

```bash
gcloud compute firewall-rules create allow-real-8001 \
  --allow=tcp:8001 \
  --source-ranges="${MY_IP}/32" \
  --target-tags=real-infer
```

Do **not** use `0.0.0.0/0`. GCP's default network still allows SSH from
anywhere (`default-allow-ssh`); restrict or remove it if that matters to you.
No rule is created for `1883`.

### 5. Copy the code and build the image

On the VM, either clone the repository or copy a tarball from the edge:

```bash
# From the edge project directory, excluding local state:
tar --exclude=.venv --exclude=logs --exclude=__pycache__ \
    --exclude='*.db' -czf /tmp/robotic-hand.tgz .
gcloud compute scp /tmp/robotic-hand.tgz real-infer-a:/tmp/ --zone=us-central1-a

gcloud compute ssh real-infer-a --zone=us-central1-a
```

On the VM:

```bash
sudo apt-get update
sudo apt-get install -y docker.io
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"   # re-login for this to take effect

mkdir -p ~/robotic-hand && cd ~/robotic-hand
tar -xzf /tmp/robotic-hand.tgz

# PRELOAD_MODEL=1 bakes the weights into the image (slower build, offline start).
# Use --build-arg PRELOAD_MODEL=0 to download on first start instead.
sudo docker build -f real_server/Dockerfile \
  -t robotic-hand-real-server:5a \
  --build-arg MODEL_ID=PekingU/rtdetr_r18vd \
  --build-arg PRELOAD_MODEL=1 .
```

The build installs CPU torch, so expect several minutes and ~2–4 GB of disk.

### 6. Run the container

Copy the env file and keep it root-readable only:

```bash
gcloud compute scp /tmp/robotic-hand.env real-infer-a:/tmp/robotic-hand.env --zone=us-central1-a
gcloud compute ssh real-infer-a --zone=us-central1-a --command \
  'sudo mv /tmp/robotic-hand.env /etc/robotic-hand.env && \
   sudo chown root:root /etc/robotic-hand.env && sudo chmod 600 /etc/robotic-hand.env'

gcloud compute ssh real-infer-a --zone=us-central1-a --command \
  'sudo docker run -d --name real-infer-a --restart unless-stopped \
     -p 8001:8001 --env-file /etc/robotic-hand.env \
     robotic-hand-real-server:5a'
```

`API_TOKEN` comes from the env file; it is not in the image or the command
line. The image's healthcheck probes `/health` every 30 s after a 180 s start
period.

### 7. Verify from the edge

```bash
VM_IP="$(gcloud compute instances describe real-infer-a \
  --zone=us-central1-a --format='get(networkInterfaces[0].accessConfigs[0].natIP)')"
echo "$VM_IP"

# Health: no token, reports only model/device
curl -s "http://${VM_IP}:8001/health"

IMG="$(ls images/*.jpg | head -1)"

# Missing token -> 401
curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST --data-binary "@${IMG}" \
  -H 'Content-Type: image/jpeg' \
  "http://${VM_IP}:8001/predict"

# Correct token -> 200
curl -s -X POST --data-binary "@${IMG}" \
  -H 'Content-Type: image/jpeg' \
  -H "X-API-Token: ${API_TOKEN}" \
  "http://${VM_IP}:8001/predict"
```

Container checks on the VM:

```bash
sudo docker ps
sudo docker inspect --format '{{.State.Health.Status}}' real-infer-a
sudo docker logs --tail 50 real-infer-a
```

With `PRELOAD_MODEL=1`, health should pass shortly after start; with
`PRELOAD_MODEL=0`, the first start downloads the model and may take longer than
the health start period.

### 8. Stop and delete (do this when finished)

```bash
# Stop compute billing but keep the disk (disk charges continue):
gcloud compute instances stop real-infer-a --zone=us-central1-a
gcloud compute instances stop real-infer-b --zone=europe-west1-b

# Delete everything:
gcloud compute instances delete real-infer-a --zone=us-central1-a --delete-disks=all
gcloud compute instances delete real-infer-b --zone=europe-west1-b --delete-disks=all
gcloud compute firewall-rules delete allow-real-8001
rm -f /tmp/robotic-hand.tgz /tmp/robotic-hand.env
```

Confirm in **Compute Engine → VM instances** that nothing is left running, and
in **Billing → Reports** that spend has stopped.

---

## Azure

### 1. Resource group and budget

```bash
az group create --name rg-robotic-hand --location eastus
```

Console: **Cost Management + Billing → Budgets → Add**

1. Scope: resource group `rg-robotic-hand`.
2. Amount: a small test budget (for example `$10`).
3. Alert thresholds: 50%, 90%, 100%, email to yourself.
4. Create.

### 2. Create the VM

```bash
az vm create \
  --resource-group rg-robotic-hand \
  --name real-infer-a \
  --image Ubuntu2404 \
  --size Standard_B2s \
  --admin-username azureuser \
  --generate-ssh-keys \
  --public-ip-sku Standard

# Optional second provider in another region
az group create --name rg-robotic-hand-b --location westeurope
az vm create \
  --resource-group rg-robotic-hand-b \
  --name real-infer-b \
  --image Ubuntu2404 \
  --size Standard_B2s \
  --admin-username azureuser \
  --generate-ssh-keys \
  --public-ip-sku Standard
```

`Standard_B2s` is 2 vCPU / 4 GB. If the model is killed for memory, use
`Standard_B4ms` (4 vCPU / 16 GB). CPU inference will still be slow.

`az vm create` creates an NSG with SSH open. Restrict SSH to your IP as well if
you want:

```bash
az network nsg rule create \
  --resource-group rg-robotic-hand --nsg-name real-infer-aNSG \
  --name allow-my-ip-ssh --priority 1000 \
  --access Allow --protocol Tcp --direction Inbound \
  --source-address-prefixes "${MY_IP}/32" \
  --destination-port-ranges 22
```

### 3. Firewall: only your IP, only 8001

```bash
az network nsg rule create \
  --resource-group rg-robotic-hand --nsg-name real-infer-aNSG \
  --name allow-my-ip-8001 --priority 1001 \
  --access Allow --protocol Tcp --direction Inbound \
  --source-address-prefixes "${MY_IP}/32" \
  --destination-port-ranges 8001
```

Repeat for `real-infer-bNSG` in `rg-robotic-hand-b`. Do not add a rule for
`1883`.

### 4. Install Docker, copy and build

```bash
VM_IP="$(az vm show -d -g rg-robotic-hand -n real-infer-a --query publicIps -o tsv)"
scp /tmp/robotic-hand.tgz azureuser@"${VM_IP}":/tmp/
ssh azureuser@"${VM_IP}"

sudo apt-get update
sudo apt-get install -y docker.io
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"   # re-login

mkdir -p ~/robotic-hand && cd ~/robotic-hand
tar -xzf /tmp/robotic-hand.tgz
sudo docker build -f real_server/Dockerfile \
  -t robotic-hand-real-server:5a \
  --build-arg MODEL_ID=PekingU/rtdetr_r18vd \
  --build-arg PRELOAD_MODEL=1 .
```

### 5. Run and verify

```bash
scp /tmp/robotic-hand.env azureuser@"${VM_IP}":/tmp/robotic-hand.env
ssh azureuser@"${VM_IP}" \
  'sudo mv /tmp/robotic-hand.env /etc/robotic-hand.env && \
   sudo chown root:root /etc/robotic-hand.env && sudo chmod 600 /etc/robotic-hand.env && \
   sudo docker run -d --name real-infer-a --restart unless-stopped \
     -p 8001:8001 --env-file /etc/robotic-hand.env \
     robotic-hand-real-server:5a'
```

Then repeat the curl checks from the GCP section against `${VM_IP}`.

### 6. Stop and delete

```bash
# Deallocate stops compute billing; the disk remains and still bills.
az vm deallocate -g rg-robotic-hand -n real-infer-a
az vm deallocate -g rg-robotic-hand-b -n real-infer-b

# Delete everything:
az group delete --name rg-robotic-hand --yes --no-wait
az group delete --name rg-robotic-hand-b --yes --no-wait
rm -f /tmp/robotic-hand.tgz /tmp/robotic-hand.env
```

Check **Cost Management → Cost analysis** afterwards to confirm the spend line
has ended.

---

## 7. Run the live test from the edge

With the provider IPs known:

```bash
cd robotic-hand
docker compose up -d                 # local Mosquitto only

export API_TOKEN='the-same-token-you-put-on-the-VMs'
export ROUTER_ENDPOINTS="http://${VM_A_IP}:8001/predict,http://${VM_B_IP}:8001/predict"

python live_run.py \
  --duration 120 \
  --fps 2 \
  --deadline-ms 8000 \
  --soak \
  --network "home fibre, Wi-Fi 5" \
  --vm-type "e2-standard-2 / e2-standard-2" \
  --region "us-central1 / europe-west1" \
  --device "laptop webcam"
```

At the end:

- `docs/live_results.md` — summary, edge-clock percentiles, failover events,
  environment.
- `logs/live/<run-id>/latency.csv` — one row per result, with epoch timestamps.
- `logs/live/<run-id>/soak.csv` — memory and outbox depth every 10 s.

For the fault sequence, run in a second terminal:

```bash
python fault_drill.py
```

Its CSV (`logs/fault_drill_<UTC>.csv`) uses the same epoch clock as
`latency.csv`, so events line up by timestamp.

## 8. Cleanup checklist

- [ ] `docker compose down` on the edge (stops the local broker).
- [ ] Stop/delete every VM and confirm in the console.
- [ ] Delete the firewall rule / NSG rule.
- [ ] Delete local `/tmp/robotic-hand.env` and `/tmp/robotic-hand.tgz`.
- [ ] Rotate `API_TOKEN` in `.env` after the test.
- [ ] Confirm budget/spend has stopped.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `curl /health` times out | Firewall not limited to your current IP, VM still booting, or container not started |
| `/health` returns 503/timeout | Model still loading; check `sudo docker logs real-infer-a` |
| `/predict` returns 401 | Header missing or token differs from the VM's `/etc/robotic-hand.env` |
| `/predict` returns 503 | `API_TOKEN` was not passed to the container |
| Container exits with OOM | VM too small; use 8–16 GB RAM |
| First request very slow | Cold model; `PRELOAD_MODEL=1` and the warm-up reduce this |
| `live_run.py` cannot reach broker | `docker compose up -d` was not run, or `MQTT_BROKER_HOST` is wrong |

## What this setup does not prove

- It does not measure production latency: CPU-only inference on a small VM is
  much slower than a GPU.
- It does not secure traffic: HTTP and the token header are unencrypted.
- It does not test the physical hand, camera calibration or actuator safety.
- Budget alerts notify; they do not stop resources. Teardown is manual.
