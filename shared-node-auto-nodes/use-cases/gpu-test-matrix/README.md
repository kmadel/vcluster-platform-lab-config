# GPU test matrix

A use case of [Auto Nodes for Shared Nodes](../../README.md): tenant clusters on
Shared Nodes that each get their own dedicated GPU node, created on demand, and run
their own NVIDIA GPU driver version and NVIDIA GPU Operator version on it. Use it
to qualify a GPU stack upgrade next to the current version on identical hardware,
or to test different GPU drivers, without building a physical cluster for each
combination.

How the dedicated nodes, isolation, scaling in, and the parent work is in the
[top-level README](../../README.md). This one covers what's specific to GPUs.

```text
elastic-cp-parent
└─ tenant pool           kvm-gb300-1gpu-tenant VMs, one GPU slot of the GB300 tray each
   ├─ gpu-baseline       project gpu-test-matrix: driver 580.65.06, GPU Operator v26.3.2
   └─ gpu-candidate      project gpu-test-matrix: driver 580.82.07, GPU Operator v26.3.3
```

## The GPU stack (simulated GPUs)

Each tenant cluster installs its own GPU stack with the `shared-node-gpu-operator`
Stack, in order:

1. **nvml-mock** (`demo-nvml-mock`), the simulated driver, reporting the driver
   version the tenant cluster picked (`driverVersion`).
2. **The GPU Operator** (`demo-gpu-operator-mock`), the version the tenant cluster
   picked (`gpuOperatorVersion`), with the driver and toolkit operands off.

The GPUs are simulated: the GB300 tray (`rack-a-u20-gb300-tray`) is carved into
one-GPU-slot VMs, and nvml-mock makes each VM report one GB300. With real GPUs,
the Stack would be the GPU Operator alone, installing the driver version the
tenant cluster asks for (`driver.enabled`, `driver.version`). That's safe because
the node belongs to one tenant cluster on Shared Nodes and is reprovisioned when
it's returned.

## Files

| Path | What it is |
|---|---|
| `project.yaml` | Project `gpu-test-matrix`, allowed to use only `elastic-cp-parent` |
| `shared-node-gpu-tenant-template.yaml` | The cluster template, with `gpuOperatorVersion` and `driverVersion` parameters |
| `shared-node-gpu-operator-stack.yaml` | Stack that installs nvml-mock, then the GPU Operator |
| `gpu-baseline.yaml`, `gpu-candidate.yaml` | The two tenant clusters |
| `parent-dcgm-collector-template.yaml` | Collector that scrapes every tenant cluster's DCGM exporter on the parent |
| `parent-dcgm-collector.yaml` | Binds that collector to `elastic-cp-parent` |

Defined outside this folder:

- **The `nvidia` RuntimeClass**, in `../../parent/elastic-cp-parent.yaml` (see
  [below](#the-nvidia-runtimeclass-on-the-parent)).
- **`kvm-gb300-1gpu-tenant`** NodeType, with the other KubeVirt NodeTypes in
  `../../../private-node-kubevirt-provider/kubevirt-provider.yaml`.
- **`gpu-tray-host`** static pool (`quantity: 1`), which claims the GB300 tray, in
  `../../../private-node-kubevirt-provider/private-node-provider-dynamic-template.yaml`.
- **`demo-nvml-mock`** Argo CD template, which has the optional `driverVersion`,
  `gpuUtilMin`, and `gpuUtilMax` parameters, in
  `../../../tenant-management/argocd-templates/`.

## GPU-specific settings

- **The GPU Operator's node labels come from the NodeType.** Its operand
  DaemonSets select `nvidia.com/gpu.deploy.<component>=true`, and the kubelet
  checks those labels on the real node. Without sync-back, the operator can't put
  them there, so `kvm-gb300-1gpu-tenant` sets them as properties, which become
  node labels at boot.
- **`allow-unneeded-special-resources`** on `kvm-gb300-1gpu-tenant`. A tenant
  cluster's first pods (its GPU Operator) request no GPU.
- **Busy-looking GPUs.** The Stack passes `gpuUtilMin: 65` and `gpuUtilMax: 85`
  to nvml-mock, so dashboards average about 75% utilization.

### The `nvidia` RuntimeClass on the parent

A Shared Nodes tenant cluster's pods run in `elastic-cp-parent`, so anything
Kubernetes needs to start them has to exist there, not only in the tenant cluster
that created them:

1. The GPU Operator's device plugin, GFD, DCGM exporter, and validator pods set
   `runtimeClassName: nvidia`, which selects the containerd runtime that gives a
   container the GPU devices and driver libraries.
2. The GPU Operator in `gpu-baseline` creates a RuntimeClass named `nvidia`, but
   only in `gpu-baseline`. vCluster can sync RuntimeClasses from a host into a
   tenant cluster (`sync.fromHost.runtimeClasses`), but not the other way.
3. vCluster syncs those pods to `elastic-cp-parent` with
   `runtimeClassName: nvidia` unchanged. Without a RuntimeClass by that name in
   `elastic-cp-parent`, the API server rejects them.

`elastic-cp-parent`'s config creates the RuntimeClass inside itself with
`experimental.deploy.vcluster.manifests`, so it's part of the parent's definition
in Git, and one copy serves every Shared Nodes tenant cluster on it. Its
`handler: nvidia` names the runtime that nvml-mock's node agent adds to
containerd on each GPU node. With real GPUs, the GPU Operator's container toolkit
adds it instead.

## Bring it up

On top of the top-level [Bring it up from scratch](../../README.md#bring-it-up-from-scratch):
after syncing `private-node-kubevirt-provider-config`, if the
`kubevirt-provider-cluster` instance shows `TemplateChangesAvailable`, apply it in
the Platform UI to bring up the GB300 tray. Each tenant cluster's Stack then
starts its GPU VM.

A fresh GPU node takes a minute or two to settle: its GPU Operator pods start
before nvml-mock has added the `nvidia` runtime, and nvml-mock restarts containerd
once it's ready, so those pods restart once. Scale up a minute or two before
showing it.

## Show it

From the Platform UI: the Clusters list shows `1 GPU` for each tenant cluster, and
each tenant cluster's Nodes view shows one node with its own name.

From a terminal:

```bash
vcluster platform connect vcluster gpu-baseline --project gpu-test-matrix
kubectl get nodes -o custom-columns='NODE:.metadata.name,GPUS:.status.allocatable.nvidia\.com/gpu,PRODUCT:.metadata.labels.nvidia\.com/gpu\.product,DRIVER:.metadata.labels.nvidia\.com/cuda\.driver-version\.full'
kubectl exec -n gpu-operator ds/nvidia-device-plugin-daemonset -c nvidia-device-plugin -- nvidia-smi
kubectl get deploy gpu-operator -n gpu-operator -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
```

Repeat with `gpu-candidate`. Point to the node names, not the GPU UUIDs (see
[Limits](#limits)).

To add a combination to the matrix, copy `gpu-candidate.yaml` with a new name,
`gpuOperatorVersion`, and `driverVersion`. Nothing on the parent changes.

To show a tenant cluster scaling out, give it a workload that requests more GPUs
than it has, for example a Deployment with 2 replicas each requesting
`nvidia.com/gpu: 1`. It gets a second GPU node within a few minutes. Scaling back
down releases it through `tenant-node-reaper`; see the top-level
[Scaling in](../../README.md#scaling-in).

## Observability

- **Parent:** fleet-bindings gives `elastic-cp-parent` the standard
  `cluster-collector-with-embedded-etcd`, like every Private Nodes cluster.
  `parent-dcgm-collector` adds the DCGM exporters of every tenant cluster on it,
  with `tenant_cluster` and `tenant_project` labels, written with the parent's
  scope. In Grafana, use the `elastic-cp-parent` datasource, for example
  `DCGM_FI_DEV_GPU_UTIL{tenant_cluster="gpu-candidate"}`. The Tenant Cluster GPUs
  dashboard (`observability/grafana/gen_tenant_gpus.py`) lists these GPUs under
  their owning tenant cluster.
- **Tenant clusters:** the bundled `shared-node-tenant-collector` scrapes only each
  tenant cluster's API server and etcd, so the tenant cluster-level Observability
  tab shows no GPU data. GPU metrics for these tenant clusters live on the parent.
- **License page:** on an online license, "Attached GPUs" stays at 0. The GPU
  meter needs usage snapshots, which need the Platform database, which needs an
  offline license key. The tenant clusters' `nvidia.com/gpu` is the live number.

## Limits

- **Simulated GPUs.** The driver version per tenant cluster is what nvml-mock
  reports, not an installed driver.
- **`driverVersion` needs nvml-mock 0.6.0 or later.** In chart 0.5.0 and 0.5.1 it
  only sets the node agent's `DRIVER_VERSION` environment variable; NVML, and so
  `nvidia-smi`, GFD, and DCGM, still report the profile's `580.65.06`. The chart on
  `main` (0.6.0-dev) writes it into the profile's `system.driver_version`. Once
  0.6.0 is released, raise `nvmlMockVersion` in the cluster template.
- **GPU Operator v26.7.x and later don't run on nvml-mock.** The device plugin's
  init container waits for an `nvidia` kernel module. Stay on v26.3.x.
- **Every GPU has the same UUID** (`GPU-b300b300-0000-…`); nvml-mock hard-codes
  them per profile. Shown side by side, it looks like one shared GPU.
- **Four GPU nodes at most** at a time, across all tenant clusters: the tray has 4
  GPU slots.
- **GPU metrics are written under the parent**, not under each tenant cluster.
