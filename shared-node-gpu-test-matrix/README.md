# Shared-node GPU test matrix

Shared-node clusters that each get their own GPU node, created on demand, and
run their own driver version and GPU Operator version on it. Use it to qualify
a GPU stack upgrade next to the current version on identical hardware, without
building a physical cluster for each combination.

The clusters keep what shared nodes are good at for trusted dev, CI, and test
teams: control planes as pods, sleep mode, and one platform stack. The GPU node
under each one is dedicated to it and is reprovisioned clean when it's
returned, so no driver or runtime state carries over to the next cluster.

This is a pattern built from today's pieces, not a product feature.

## How it works

```text
spark-cluster (control plane cluster)
└─ elastic-cp-parent        Private Nodes + KubeVirt Auto Nodes (could be any node provider), project `platform` (private Platform project for Private Node tenant clusters also used as control plane clusters),
   │                        also connected to Platform as a control plane cluster
   ├─ system pool           kvm-medium VMs: parent system pods, cluster control planes
   └─ tenant pool           kvm-gb300-1gpu-tenant VMs, tainted, one per cluster
      ├─ gpu-baseline       project gpu-test-matrix: driver 580.65.06, GPU Operator v26.3.2
      └─ gpu-candidate      project gpu-test-matrix: driver 580.82.07, GPU Operator v26.3.3
```

Each cluster's identity reaches the parent on its pods, so the parent's config
never changes when a cluster is added:

1. The `shared-node-gpu-tenant` cluster template sets
   `sync.fromHost.nodes.selector.labels: {demo.vcluster.com/tenant: <project>-<name>}`.
   vCluster adds that nodeSelector to every pod it syncs to the parent, and syncs
   only nodes with that label back into the cluster.
2. The parent's `tenant` pool selects `demo.vcluster.com/tenant Exists`.
3. The `kvm-gb300-1gpu-tenant` NodeType sets the property
   `demo.vcluster.com/tenant: "*"`, which Platform turns into an `Exists`
   requirement, so the pool can use it for any cluster.
4. Taking `gpu-baseline` as the example: the first pod it syncs to
   `elastic-cp-parent` waits there for a node. Karpenter, running in
   `elastic-cp-parent`, creates a NodeClaim that requires that one tenant value
   (`demo.vcluster.com/tenant In [gpu-test-matrix-gpu-baseline]`). When the VM
   joins `elastic-cp-parent`, Karpenter copies the label to the new Node. Since
   `gpu-baseline` syncs only nodes with its own tenant label, the node shows up in
   `gpu-baseline` and in no other shared-node cluster.
5. When `gpu-baseline` sleeps or is deleted, its node empties and is consolidated
   after 2 minutes. The VM goes back to the GB300 tray.

Two keys, two jobs: `demo.vcluster.com/tenant` is the per-cluster **label**, and
`demo.vcluster.com/tenant-pool=true:NoSchedule` is the pool's **taint**, the same
on every tenant node.

To see the tenant value, look at the Karpenter `NodeClaims` inside the parent, not
the Platform `NodeClaims` in `p-platform`. The Platform `NodeClaim` only asks the
KubeVirt provider for a VM of a type, and carries no tenant value:

```bash
vcluster platform connect vcluster elastic-cp-parent --project platform
kubectl get nodeclaims.karpenter.sh -L demo.vcluster.com/tenant
```

### The GPU stack

Each cluster installs its own GPU stack with the `shared-node-gpu-operator`
Stack, in order:

1. **nvml-mock** (`demo-nvml-mock`), the simulated driver, reporting the driver
   version the cluster picked (`driverVersion`).
2. **The GPU Operator** (`demo-gpu-operator-mock`), the version the cluster
   picked (`gpuOperatorVersion`), with the driver and toolkit operands off.

The GPUs are simulated: the GB300 tray (`rack-a-u20-xlarge`) is carved into
one-GPU-slot VMs, and nvml-mock makes each VM report one GB300. With real GPUs,
the Stack would be the GPU Operator alone, installing the driver version the
cluster asks for (`driver.enabled`, `driver.version`). That's safe because the
node belongs to one shared-node tenant cluster and is reprovisioned when it's returned.

## Files

| Path | What it is |
|---|---|
| `parent/elastic-cp-parent.yaml` | The parent cluster: `system` and `tenant` pools, embedded etcd, Argo CD integration, the `nvidia` RuntimeClass |
| `parent/parent-dcgm-collector-template.yaml` | Collector that scrapes every cluster's DCGM exporter on the parent |
| `parent/parent-dcgm-collector.yaml` | Binds that collector to `elastic-cp-parent` |
| `tenants/project.yaml` | Project `gpu-test-matrix`, allowed to use only `elastic-cp-parent` |
| `tenants/shared-node-gpu-tenant-template.yaml` | The cluster template, with `gpuOperatorVersion` and `driverVersion` parameters |
| `tenants/shared-node-gpu-operator-stack.yaml` | Stack that installs nvml-mock, then the GPU Operator |
| `tenants/gpu-baseline.yaml`, `tenants/gpu-candidate.yaml` | The two clusters |

Defined outside this folder:

- **`kvm-gb300-1gpu-tenant`** NodeType, with the other KubeVirt NodeTypes in
  `../private-node-kubevirt-provider/kubevirt-provider.yaml`.
- **`gpu-tray-host`** static pool (`quantity: 1`), which claims the GB300 tray, in
  `../private-node-kubevirt-provider/private-node-provider-dynamic-template.yaml`.
- **`demo-nvml-mock`** Argo CD template, which has the optional `driverVersion`
  parameter, in `../tenant-management/argocd-templates/`.
- **`bootstrap/shared-node-gpu-test-matrix.yaml`**, the Argo CD Application for
  this folder.

## Settings that matter

- **No `syncBackChanges` in the `shared-node-gpu-tenant` template.** Combined with
  `enforceTolerations`, it removes the `tenant` pool's taint from the GPU node:
  1. `enforceTolerations` makes every `gpu-baseline` pod tolerate
     `demo.vcluster.com/tenant-pool`. vCluster therefore leaves that taint out of
     `gpu-baseline`'s copy of its node, since none of its pods are affected by it.
  2. The GPU Operator and NFD, running in `gpu-baseline`, update that copy of the
     node to add their labels.
  3. With `syncBackChanges: true`, vCluster writes the copy back to the real node
     in `elastic-cp-parent`, including its taint list, which no longer has the
     taint.

  Once the taint is gone, `elastic-cp-parent`'s own pods and the other
  clusters' control plane pods can be scheduled on `gpu-baseline`'s GPU node.
  That mixes clusters on one node, and the node never empties, so it's never
  consolidated.
- **The GPU Operator's node labels come from the NodeType.** Its operand
  DaemonSets select `nvidia.com/gpu.deploy.<component>=true`, and the kubelet
  checks those labels on the real node. Without sync-back, the operator can't put
  them there, so `kvm-gb300-1gpu-tenant` sets them as properties, which become
  node labels at boot.
- **The parent's `system` pool needs `demo.vcluster.com/tenant DoesNotExist`.**
  The Auto Nodes cloud provider registers every NodeType property key as a
  well-known label, and Karpenter lets a pool serve a pod that asks for a
  well-known label the pool doesn't mention. Without this selector, a pending
  cluster pod can land on the untainted system pool and stamp its tenant label on
  a plain CPU node.
- **`nodetype.loft.sh/allow-unneeded-special-resources: "true"`** on the NodeType.
  A cluster's first pods request no GPU, and dynamic claims otherwise skip a
  NodeType with an extended resource the claim doesn't request.
- **Embedded CoreDNS** in each cluster, so no CoreDNS pod sits on (and holds) the
  GPU node.
- **`elastic-cp-parent` has its own `nvidia` RuntimeClass.** A shared-node
  cluster's pods run in `elastic-cp-parent`, so anything Kubernetes needs to
  start them has to exist there, not only in the cluster that created them:
  1. The GPU Operator's device plugin, GFD, DCGM exporter, and validator pods set
     `runtimeClassName: nvidia`, which selects the containerd runtime that gives a
     container the GPU devices and driver libraries.
  2. The GPU Operator in `gpu-baseline` creates a RuntimeClass named `nvidia`, but
     only in `gpu-baseline`. vCluster can sync RuntimeClasses from a host into a
     cluster (`sync.fromHost.runtimeClasses`), but not the other way.
  3. vCluster syncs those pods to `elastic-cp-parent` with
     `runtimeClassName: nvidia` unchanged. Without a RuntimeClass by that name in
     `elastic-cp-parent`, the API server rejects them.

  `elastic-cp-parent`'s config creates the RuntimeClass inside itself with
  `experimental.deploy.vcluster.manifests`, so it's part of the parent's
  definition in Git, and one copy serves every shared-node cluster on it. Its
  `handler: nvidia` names the runtime that nvml-mock's node agent adds to
  containerd on each GPU node. With real GPUs, the GPU Operator's container
  toolkit adds it instead.

Check that every tenant node still has its taint:

```bash
kubectl get nodes -L demo.vcluster.com/tenant \
  -o custom-columns='NODE:.metadata.name,TENANT:.metadata.labels.demo\.vcluster\.com/tenant,TAINTS:.spec.taints[*].key'
```

## Bring it up from scratch

1. Push. Argo CD syncs this folder.
2. Sync `private-node-kubevirt-provider-config` by hand (its auto-sync is off).
   That creates `kvm-gb300-1gpu-tenant`. If the `kubevirt-provider-cluster`
   instance shows `TemplateChangesAvailable`, apply it in the Platform UI to bring
   up the GB300 tray.
3. Once `elastic-cp-parent` is Ready, connect it as a control plane cluster. This
   is the one manual step:

   ```bash
   vcluster platform connect vcluster elastic-cp-parent --project platform
   vcluster platform add cluster elastic-cp-parent \
     --display-name "elastic-cp-parent" \
     --helm-chart-version <the Platform version> --wait
   ```

4. The clusters in `gpu-test-matrix` then come up. Each one's Stack starts its GPU
   VM. Expect a few minutes for the VM to boot and join.

Platform doesn't roll template changes onto existing instances. After changing
`shared-node-gpu-tenant`, delete the clusters and let Argo CD recreate them, or
apply the update to each instance in the UI.

## Show it

From the Platform UI: the Clusters list shows `1 GPU` for each cluster, and each
cluster's Nodes view shows one node with its own name.

From a terminal:

```bash
vcluster platform connect vcluster gpu-baseline --project gpu-test-matrix
kubectl get nodes -o custom-columns='NODE:.metadata.name,GPUS:.status.allocatable.nvidia\.com/gpu,PRODUCT:.metadata.labels.nvidia\.com/gpu\.product,DRIVER:.metadata.labels.nvidia\.com/cuda\.driver-version\.full'
kubectl exec -n gpu-operator ds/nvidia-device-plugin-daemonset -c nvidia-device-plugin -- nvidia-smi
kubectl get deploy gpu-operator -n gpu-operator -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
```

Repeat with `gpu-candidate`. Point to the node names, not the GPU UUIDs (see
below).

To add a combination to the matrix, copy `tenants/gpu-candidate.yaml` with a new
name, `gpuOperatorVersion`, and `driverVersion`. Nothing on the parent changes.

## Observability

- **Parent:** fleet-bindings gives `elastic-cp-parent` the standard
  `cluster-collector-with-embedded-etcd`, like every private-node cluster.
  `parent-dcgm-collector` adds the DCGM exporters of every cluster on it, with
  `tenant_cluster` and `tenant_project` labels, written with the parent's scope.
  In Grafana, use the `elastic-cp-parent` datasource, for example
  `DCGM_FI_DEV_GPU_UTIL{tenant_cluster="gpu-candidate"}`.
- **Clusters:** the bundled `shared-node-tenant-collector` scrapes only each
  cluster's API server and etcd, so the cluster-level Observability tab shows no
  GPU data. GPU metrics for these clusters live on the parent.
- **License page:** on an online license, "Attached GPUs" stays at 0. The GPU
  meter needs usage snapshots, which need the Platform database, which needs an
  offline license key. The clusters' `nvidia.com/gpu` is the live number.

## Limits

- **Simulated GPUs.** The driver version per cluster is what nvml-mock reports,
  not an installed driver.
- **`driverVersion` needs nvml-mock 0.6.0 or later.** In chart 0.5.0 and 0.5.1 it
  only sets the node agent's `DRIVER_VERSION` environment variable; NVML, and so
  `nvidia-smi`, GFD, and DCGM, still report the profile's `580.65.06`. The chart on
  `main` (0.6.0-dev) writes it into the profile's `system.driver_version`. Once
  0.6.0 is released, raise `nvmlMockVersion` in the cluster template.
- **Control plane storage.** Each cluster's embedded etcd is a `local-path` volume
  on whichever `system` node it lands on. If that node is replaced, the cluster's
  state is lost (it is all re-created from Git, but the cluster restarts empty).
- **GPU Operator v26.7.x and later don't run on nvml-mock.** The device plugin's
  init container waits for an `nvidia` kernel module. Stay on v26.3.x.
- **Every GPU has the same UUID** (`GPU-b300b300-0000-…`); nvml-mock hard-codes
  them per profile. Shown side by side, it looks like one shared GPU.
- **Four clusters at most** with GPUs at a time: the tray has 4 GPU slots.
- **The parent's connection is manual**, and so is its writer key's scope: GPU
  metrics are written under the parent, not under each cluster.
- **Argo CD on 4.13 alphas.** Enabling the parent's Argo CD integration together
  with a change that restarts its control plane left its first syncs hung until
  the application controller was restarted. Platform 5.0.0-alpha.1 and 4.12.3
  include a fix that waits for the cluster to be Ready before registering it
  (loft-enterprise #8403).
