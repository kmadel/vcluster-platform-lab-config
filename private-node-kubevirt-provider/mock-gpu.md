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

**Status: verified 2026-10-17** with `mock-gpu-nvs` in `vm-tenants`. Stack Healthy about 14
minutes after create (including one provider scale-out for the CPU service VM). A thermal
fault cordoned its node in **12 seconds** and the drained pod was running elsewhere in under
a minute; cooling the GPU uncordoned the node in **21 seconds**, with no manual step.

Template **Private Nodes - KubeVirt VMs with Simulated GPUs and NVSentinel**
(`private-nodes-kubevirt-mock-gpu-nvsentinel-stack`, Stack `demo-mock-gpu-nvsentinel`): the
four mock-GPU VMs plus one `kvm-medium` CPU service VM, with standalone DCGM
(`nvidia-dcgm` on 5555) and NVSentinel v1.21.0's full pipeline (MongoDB via Percona,
fault-quarantine, node-drainer), following NVIDIA's k8s-test-infra NVSentinel guide.
`demo-nvsentinel` (the real-GPU stack's) stays detection-only.

- **Shares the mock host** with the plain template: delete one cluster before creating the other.
- **The CPU service VM** lands on the provider's large hosts. `rack-a-u18-large` lacks the
  4 GiB, so the provider cluster adds `rack-b-u16-large` (one Metal3 boot, about 2 minutes on
  2026-10-17). Keep that host unassigned (see "Don't assign the mock host to a tenant").
- **Placement:** health monitor, metadata collector and platform connectors on the GPU nodes;
  MongoDB, its operator, fault-quarantine, node-drainer and the labeler on the CPU service VM,
  so a GPU drain never evicts the pipeline.

### The demo beat

In the tenant cluster, put something on the GPUs to watch it move. Leave one GPU free so the
evicted pod has somewhere to go (three replicas on four GPU nodes):

```sh
kubectl create deployment gpu-workload --image=busybox:1.36 --replicas=3 -- sleep infinity
kubectl set resources deployment gpu-workload --limits=nvidia.com/gpu=1
kubectl get pods -l app=gpu-workload -o wide
```

Heat one GPU past its slowdown threshold (142 °C is the guide's value):

```sh
NODE=<a node running a gpu-workload pod>
MOCK=$(kubectl -n mokka get pod -o name --field-selector spec.nodeName=$NODE)
kubectl -n mokka exec $MOCK -c node-agent -- nvml-mock-ctl temp --gpu 0 142
kubectl get node $NODE -o jsonpath='{.spec.unschedulable}{"\n"}{range .status.conditions[?(@.type=="GpuThermalMarginWatch")]}{.status} {.message}{end}{"\n"}'
kubectl get pods -l app=gpu-workload -o wide -w     # evicted, rescheduled elsewhere
```

What happened on 2026-10-17: `GpuThermalMarginWatch=True`,
`GPU_TEMP_HW_SLOWDOWN_VIOLATION ... thermal margin -5°C below HW slowdown`, the node cordoned
(`node.kubernetes.io/unschedulable`), and the pod on it evicted and running on the free GPU
node. The evicted pod took its full 30 s grace period (`sleep` ignores SIGTERM).

Cool it, and NVSentinel uncordons the node on its own:

```sh
kubectl -n mokka exec $MOCK -c node-agent -- nvml-mock-ctl reset --gpu 0
```

On the Tenant Cluster GPUs dashboard the GPU's temperature jumps from about 60 to **95 °C**
(DCGM's reported temperature tops out there; NVSentinel acts on the thermal-margin field,
which the 142 °C pin drives negative) and drops back after the reset, with the usual scrape
delay of up to a minute.

`nvml-mock-ctl` has more faults for other beats: `fail` (lost device, fallen off the bus,
ECC, Xid; `--mode healthy` clears it), and pins for `util`, `power`, `clocks`, `throttle` and
`pstate`. Only the thermal path is verified here.

### What the first run showed

- **Stack tasks can show Failed while they are still starting.** cert-manager (waiting for
  its node) and NVSentinel (Percona CRDs without status conditions yet) both went Failed with
  reason Degraded while their health was Progressing. On 2026-10-18 the NVSentinel task went
  back to Healthy on its own once its pods were up, so wait first; retry the task (UI, or the
  `platform.vcluster.com/stack-retry` annotation) only if it stays Failed after its pods are
  Running.
- **NVSentinel's first few minutes are noisy and fine.** platform-connectors,
  fault-quarantine and node-drainer crash-loop with `ENOTFOUND mongodb-rs0...` until MongoDB
  is Ready: a headless Service has no DNS record before its pod is ready. They recover on
  their next restart once `create-mongodb-database` completes.
- **Every watch must be healthy before you inject.** `kubectl get node <gpu node> -o
  jsonpath='{range .status.conditions[*]}{.type}={.status}{"\n"}{end}'` should list
  `GpuThermalMarginWatch=False` and `GpuNvlinkWatch=False`. A True NVLink watch at idle means
  the BER suppression didn't apply, and that node will never uncordon after a fault.

## Variant: replace a failed GPU from spare capacity

**Status: verified 2026-10-18**, end to end with no manual step.

Two tenant clusters share the mock "4-GPU server", with one slot held in reserve:

| Slot | Holder |
| --- | --- |
| 1, 2 | `tenant-a-gpu`: NVSentinel template, `gpuVMCount: 2`, `spareGPUs: 1` (plus its CPU service VM on a large host) |
| 3 | `tenant-b-gpu`: plain mock template, `gpuVMCount: 1` |
| 4 | reserve |

Run `./demo.sh workload` against tenant A (two replicas, one per static GPU, so a drained
pod has nowhere to go but a spare), then heat one of its GPUs. What happened:

| t | |
| --- | --- |
| 0 s | `nvml-mock-ctl temp --gpu 0 142` on a static GPU node |
| +13 s | NVSentinel cordons and drains it; the pod goes Pending; **Auto Nodes claims the reserve slot** (`gpu-spare` pool) |
| +111 s | Spare VM joins tenant A |
| +306 s | Spare advertises its GPU; **the workload runs on it** |
| +306 s | `nvml-mock-ctl reset --gpu 0` |
| +328 s | NVSentinel uncordons the healed node |
| +393 s | **Consolidation moves the workload back** to the healed node |
| +437 s | **Spare deleted; slot returned to the reserve** |

How it works, in the NVSentinel template (`spareGPUs` > 0 adds both pools):

- `kvm-mock-gpu` **declares** `nvidia.com/gpu: "1"` in its `resources`, so vCluster copies the
  GPU into the Karpenter instance type and a Pending GPU pod can launch it.
- **`gpu-spare`** is a dynamic pool, separate from the static GPU pool so a cordoned static
  node doesn't use up its `limits: {nvidia.com/gpu: N}`. Tainted `nvidia.com/gpu:NoSchedule`
  so only GPU pods can trigger it; the workload, nvml-mock, the GPU Operator and NVSentinel's
  GPU-side pods tolerate it. `consolidationPolicy: WhenEmptyOrUnderutilized`,
  `consolidateAfter: 1m` gives the slot back after recovery.
- **`scheduler-headroom`** is a dynamic pool on `kvm-small` with `limits: {cpu: "0"}`. It
  never launches anything. It exists because when the spare takes the mock host's last slot,
  `kvm-mock-gpu` goes Pending, no dynamic pool has a launchable instance type, and Karpenter
  refuses to build its scheduler ("filtered out all instance types for all node pools"), so
  consolidation can't even simulate deleting the spare. A pool with an always-launchable type
  keeps the simulation possible; the zero limit drops that type before any launch. A real
  fleet with free capacity in some pool doesn't need this.
- **`gpu-spare` selects the full NodeType name** (`kubevirt-us-va-blacksburg-dc1.kvm-mock-gpu`).
  The short alias works for launching but breaks consolidation: vCluster's init-node
  controller copies the selected `vcluster.com/node-type` value into the node's
  `node.kubernetes.io/instance-type`, Karpenter names the instance type with the full name,
  and the spare is "Unconsolidatable: Instance Type "kvm-mock-gpu" not found". That is a
  vCluster bug (`vcluster-pro` `pkg/privatenodes/autonodes/controllers/initnode.go:80-99`;
  the controller should use the NodeType's name), worked around here.

To watch it: `./demo.sh watch <node>` in one terminal (it shows which GPU node is a spare),
`./demo.sh heat <node>`, and later `./demo.sh cool <node>`. If the spare never goes away,
read Karpenter's view in the tenant's control plane pod, not the events (Karpenter
de-duplicates them):
`kubectl --context <control plane cluster> -n <tenant namespace> logs <tenant>-0 | grep -iE "disruption|consolidat"`.

## Run it in your own environment

The lab build uses Metal3 hosts, KubeVirt VMs and a GitOps repo, but the simulated-GPU part
is independent of all three: **any vCluster private node running containerd 2.x can carry
nvml-mock.** Pick the shape that fits.

### What you need

| | Plain mock | With NVSentinel |
| --- | --- | --- |
| vCluster Platform | 4.12+ with private nodes and the Argo CD integration (Stacks). 4.13 for Tenants. | same |
| Node runtime | containerd 2.x under systemd, as vCluster's join installs it | same |
| GPU nodes | 1 CPU, 3 GiB, 30 GiB disk each (one simulated GPU per node) | same, plus ~770 MiB of NVSentinel requests per node |
| CPU service node | none | 2 CPU, 4 GiB, plus a StorageClass for a 2 GiB MongoDB volume (`local-path` in a private-node tenant cluster) |
| Images and charts | `ghcr.io/nvidia/nvml-mock`, `nvcr.io` (GPU Operator, dcgm-exporter), `helm.ngc.nvidia.com/nvidia` | plus `ghcr.io/nvidia/nvsentinel`, `docker.io/percona/*`, `quay.io/jetstack` (cert-manager) |
| Optional | Fleet Observability, for the Tenant Cluster GPUs dashboard | same |

No real GPU, IOMMU, vfio or device operator anywhere.

### Fastest path: any existing tenant cluster

If you already have a private-node tenant cluster with Argo CD integration, skip the
infrastructure: apply the Argo CD templates and a Stack, and attach the Stack to that cluster.

1. Apply `tenant-management/argocd-templates/nvml-mock-application-template.yaml` and
   `gpu-operator-mock-application-template.yaml` (plus `nvsentinel-mock-application-template.yaml`,
   `cert-manager-application-template.yaml` and `stack-gate-application-template.yaml` for
   the NVSentinel variant; the plain Stack also needs the stack gate).
2. Apply `tenant-management/stacks-templates/mock-gpu-stack.yaml` (or
   `mock-gpu-nvsentinel-stack.yaml`).
3. Label the nodes that should get a simulated GPU `workload.example.com/pool=gpu-compute`
   (and, for NVSentinel, one CPU node `workload.example.com/pool=cpu-services`).
4. Add the Stack to the cluster (cluster template `deploy.stacks`, or the UI) with `minGPUs`
   set to the number of GPU nodes.

Each labelled node then reports `nvidia.com/gpu: 1` within a few minutes of nvml-mock starting.

### Full path: KubeVirt VMs as in the lab

Copy these, then change the lab-specific values below.

| File | What it is |
| --- | --- |
| `private-node-kubevirt-provider/private-node-provider-dynamic-template.yaml` | KubeVirt provider cluster template; adds the `mock-gpu-host` static pool |
| `private-node-kubevirt-provider/kubevirt-provider.yaml` | `KubeVirtProviderCluster` with the `kvm-mock-gpu` NodeType (other types avoid the mock host) |
| `tenant-management/cluster-templates/private-kubevirt-mock-gpu-stack.yaml` | Tenant template, 4 GPU VMs |
| `tenant-management/cluster-templates/private-kubevirt-mock-gpu-nvsentinel-stack.yaml` | Tenant template, 4 GPU VMs + CPU service VM |
| `tenant-management/argocd-templates/{nvml-mock,gpu-operator-mock,nvsentinel-mock}-application-template.yaml` | The three mock-specific Argo CD templates |
| `tenant-management/stacks-templates/mock-gpu{,-nvsentinel}-stack.yaml` | The two Stacks |
| `observability/grafana/gen_tenant_gpus.py` | Generator for the Tenant Cluster GPUs dashboard |

Lab-specific values to replace:

| Where | Lab value | Replace with |
| --- | --- | --- |
| Tenant templates, `autoNodes[].provider` | `kubevirt-us-va-blacksburg-dc1` | your KubeVirt NodeProvider |
| Tenant templates, `joinNode.containerd.registry.mirrors` | `192.168.50.61:500x` pull-through caches | your mirrors, or delete the block |
| Tenant templates, `controlPlane.statefulSet.scheduling.nodeSelector` | `kubernetes.io/hostname: vmetal-w02` | where your tenant control planes run, or delete |
| Tenant templates, `integrations.argoCD.connector` | `demo-lab` / `demo-lab-<tenant>` | your Argo CD connector |
| Tenant templates, `networking` | pod `10.244.0.0/16`, service `10.96.0.0/16` | anything off your provider's pod and service CIDRs (KubeVirt bridge binding puts node IPs on the provider pod network) |
| Provider template, `mock-gpu-host` | `vcluster.com/profile In [xlarge]` | a selector for one 8 CPU / 16 GiB host, or drop the pool and the `kvm-mock-gpu` affinity to use your existing KubeVirt hosts |
| `kubevirt-provider.yaml` NodeType labels | `topology.kubernetes.io/region: us-va-blacksburg-dc1`, `tenants.demo.vcluster.com/*` | your region and tenant grant labels |
| Project | `vm-tenants` `allowedTemplates` | your project |
| `gen_tenant_gpus.py`, `FLEET_DS` | `e8b3387a-...` | your fleet datasource UID (the observability connector Secret's UID) |
| `stack-gate` repoURL | `github.com/loft-demos/ce-demo-nvidia-gpu-operator-stack` | keep (public) or your fork |

Order: provider cluster template and `KubeVirtProviderCluster` (then **sync the provider
cluster's template** in the UI, or its new pool never renders), Argo CD templates, Stacks,
tenant templates, project. Then create a tenant cluster from the template and run the
Verify commands above.

### Gotchas that cost time here

- **Template changes don't reach a running instance** until it syncs its template (UI
  **Sync Template**, or `spec.templateRef.syncOnce: true`).
- **A static pool's host must be visible to the claiming cluster.** NodeType capacity ignores
  tenant assignment, so a selector that spans a host lent to a tenant can stall the claim
  forever with `no available BareMetalHost`. Narrow the selector or unassign the host.
- **Argo CD OCI charts:** `repoURL` must include the chart name
  (`oci://ghcr.io/nvidia/k8s-test-infra/chart/nvml-mock`); without it ghcr answers 403.
- **nvml-mock's `gpu.count` must be an integer**, not a string; the chart schema rejects `"1"`.
- **Key GPU dashboards by `Hostname`.** Every node reports the same GPU UUID.
- **NVSentinel needs a non-GPU node for its pipeline**, or the drainer can evict itself; and
  extend `node-drainer.systemNamespaces` to whatever must survive a drain (the mock, your
  collector, cert-manager).
