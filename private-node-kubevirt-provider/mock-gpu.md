# Simulated GPU VMs (nvml-mock)

A mock "4-GPU server" carved into four 1-GPU KubeVirt VMs, each a node of one tenant
cluster. No physical GPU is involved: NVIDIA's nvml-mock simulates one GPU per node, so the
device plugin, `nvidia-smi`, GFD and dcgm-exporter all report a GPU, and the Fleet GPU
dashboards populate. Nothing CUDA runs. Present the GPU model as simulated.

**Status: verified 2026-10-17** with `mock-gpu-test` in `vm-tenants` (4.13.0-alpha.19,
`b200`, 4 VMs). Mock host Ready about 5 minutes after its claim took a host; VMs joined about
3 minutes after their disk imports; the Stack was Healthy about 4 minutes after nvml-mock
started. Every node advertised `nvidia.com/gpu: 1` as an NVIDIA B200 (184,320 MiB, driver
580.65.06), and 24 DCGM series reached the Fleet Prometheus with the tenant's
`vcluster_platform_project` and `vcluster_platform_instance` labels. Dashboard:
**Tenant Cluster GPUs** (`/grafana/d/vcluster-tenant-gpus`, `observability/grafana/`).

## Don't assign the mock host to a tenant

The static pool selects any xlarge. If the xlarge it picks is assigned to a tenant
(`tenant.vcluster.com/exclusive-to`), the provider cluster's claim can't see it and retries
"no available BareMetalHost" forever, because NodeType capacity ignores the assignment. On
2026-10-17 it picked `rack-a-u20-xlarge`, then assigned to acme; unassigning it fixed it.

## Pieces

| File | What |
| --- | --- |
| `private-node-provider-dynamic-template.yaml` | Static pool `mock-gpu-host`: one xlarge (8 CPU, 16 GiB), labeled `demo.vcluster.com/mock-gpu-host=true` |
| `kubevirt-provider.yaml` | NodeType `kvm-mock-gpu` (2 CPU, 1 CPU request, 3 GiB, 30 GiB disk, max 4), pinned to the mock host; the CPU types now avoid it |
| `tenant-management/argocd-templates/nvml-mock-application-template.yaml` | `demo-nvml-mock` |
| `tenant-management/argocd-templates/gpu-operator-mock-application-template.yaml` | `demo-gpu-operator-mock`: driver, toolkit, DCGM off; device plugin, GFD, CDI, dcgm-exporter on (NVIDIA's k8s-test-infra recipe) |
| `tenant-management/stacks-templates/mock-gpu-stack.yaml` | `demo-mock-gpu`: nvml-mock, then the GPU Operator, then the capacity gate |
| `tenant-management/cluster-templates/private-kubevirt-mock-gpu-stack.yaml` | `private-nodes-kubevirt-mock-gpu-stack`, allowed in `vm-tenants` |

No CPU service VM, unlike the real GPU template: four mock VMs fill the xlarge. The mock
nodes carry the `workload.example.com/pool=gpu-compute` label but not the `nvidia.com/gpu`
taint, so CoreDNS and the GPU Operator controllers run on them.

## Roll out

1. Push, then **sync `private-node-kubevirt-provider-config` by hand** (its automated sync
   is off). That updates the provider cluster's template, which claims one xlarge from
   Metal3 (`rack-a-xlarge-pool` or `rack-b-xlarge-pool`): a full Metal3 boot, so budget
   15 to 20 minutes before the node is Ready with `demo.vcluster.com/mock-gpu-host=true`.
2. `tenant-management` syncs on its own: the Argo templates, Stack, cluster template and
   project change.
3. Create a tenant cluster in `vm-tenants` from **Private Nodes - KubeVirt VMs with Simulated
   GPUs**, `gpuVMCount: 4`. Four VM boots, then the Stack.

## Verify

In the tenant cluster:

```sh
kubectl get nodes -o custom-columns=NAME:.metadata.name,GPU:.status.allocatable.nvidia\\.com/gpu,PRODUCT:.metadata.labels.nvidia\\.com/gpu\\.product
kubectl -n mokka get ds nvml-mock
kubectl -n gpu-operator get pods
kubectl -n gpu-operator exec ds/nvidia-device-plugin-daemonset -- nvidia-smi -L
```

Expect `nvidia.com/gpu: 1` on every node, the Stack Ready once all four are advertised,
and DCGM series (`DCGM_FI_DEV_GPU_UTIL` between 15 and 75%) on the Fleet GPU dashboards.

## What the first run showed

- **containerd restart: fine.** The node agent wrote `/etc/containerd/conf.d/50-mokka.toml`,
  restarted containerd through systemd and set up the `nvidia` handler; NRI connected
  (containerd 2.2.3) and every node stayed Ready.
- **DCGM fields.** dcgm-exporter skips the fields the mock doesn't implement (vGPU license,
  NVLink CRC) without crashing. Of the 24 it serves, these move with time: GPU utilization,
  memory copy utilization, temperature, power, graphics engine, tensor and DRAM activity,
  PCIe rx/tx. Static or zero on the mock: framebuffer used, memory temperature, SM and memory
  clocks, total energy, encoder/decoder, NVLink bandwidth. The Tenant Cluster GPUs dashboard
  uses only the first group plus framebuffer capacity and the error counters.
- **Same UUID on every node.** The profile hard-codes GPU UUIDs, so with one GPU per node
  every node reports `GPU-b2000000-0000-0000-0000-000000000000`. Key GPUs by `Hostname`,
  never `UUID`.
- **Counters gain `_total`** on the way into Prometheus (`DCGM_EXP_XID_ERRORS_COUNT_total`).
- **Capacity: fine.** All four VMs ran on the xlarge at 1 CPU and 3 GiB requested each.
- **Control plane cluster views show nothing for this tenant on the KubeVirt provider.** Its
  control plane runs on spark-cluster (`vcluster_platform_hosted_on`); only its VMs run on
  the provider cluster. Use the fleet view, or spark-cluster's.

## Variant: NVSentinel fault injection

**Status: written 2026-10-17, not yet run.** Templates render and validate (sprig, vCluster
0.37.3 schema, NVSentinel v1.21.0 and GPU Operator v26.3.3 `helm template`).

Template **Private Nodes - KubeVirt VMs with Simulated GPUs and NVSentinel**
(`private-nodes-kubevirt-mock-gpu-nvsentinel-stack`, Stack `demo-mock-gpu-nvsentinel`): the
four mock-GPU VMs plus one `kvm-medium` CPU service VM, with standalone DCGM
(`nvidia-dcgm` on 5555) and NVSentinel's full pipeline (MongoDB via Percona,
fault-quarantine, node-drainer), following NVIDIA's k8s-test-infra NVSentinel guide.
`demo-nvsentinel` (the real-GPU stack's) stays detection-only.

- **Shares the mock host** with the plain template: delete one cluster before creating the other.
- **The CPU service VM** lands on the provider's large hosts. `rack-a-u18-large` lacks the
  4 GiB, so expect the provider cluster to add `rack-b-u16-large` (one Metal3 boot). Keep that
  host unassigned (see "Don't assign the mock host to a tenant").
- **Placement:** health monitor, metadata collector and platform connectors on the GPU nodes;
  everything else on the CPU service VM, so a GPU drain never evicts the pipeline.

### The demo beat

In the tenant cluster, put something on the GPUs to watch it move:

```sh
kubectl create deployment gpu-workload --image=busybox:1.36 --replicas=3 -- sleep infinity
kubectl set resources deployment gpu-workload --limits=nvidia.com/gpu=1
kubectl get pods -l app=gpu-workload -o wide
```

Heat one GPU past its slowdown threshold (B200's T.Limit; 142 °C is the guide's value):

```sh
NODE=<a node running a gpu-workload pod>
MOCK=$(kubectl -n mokka get pod -o name --field-selector spec.nodeName=$NODE)
kubectl -n mokka exec $MOCK -c nvml-mock-nri -- nvml-mock-ctl temp --gpu 0 142
kubectl get node $NODE -o jsonpath='{.spec.unschedulable}{"\n"}{range .status.conditions[?(@.type=="GpuThermalMarginWatch")]}{.status} {.message}{end}{"\n"}'
kubectl get pods -l app=gpu-workload -o wide -w     # evicted, rescheduled elsewhere
```

Expect `GpuThermalMarginWatch=True`, the node cordoned and drained, and its pod rescheduled.
Then cool it, and NVSentinel uncordons the node on its own:

```sh
kubectl -n mokka exec $MOCK -c nvml-mock-nri -- nvml-mock-ctl reset --gpu 0
```

The Tenant Cluster GPUs dashboard shows the temperature spike on that GPU while it happens.
