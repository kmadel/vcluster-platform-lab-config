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
└─ elastic-cp-parent        Private Nodes + KubeVirt Auto Nodes, project platform,
   │                        also connected to Platform as a control plane cluster
   ├─ system pool           kvm-medium VMs: parent system pods, cluster control planes
   └─ tenant pool           kvm-gb300-1gpu-tenant VMs, tainted, one per cluster
      ├─ gpu-baseline       project gpu-test-matrix: driver 580.65.06, GPU Operator v26.3.2
      └─ gpu-candidate      project gpu-test-matrix: driver 580.82.07, GPU Operator v26.3.3
```

Each cluster's identity reaches the parent on its pods, so the parent's config
never changes when a cluster is added:

1. The cluster template sets
   `sync.fromHost.nodes.selector.labels: {demo.vcluster.com/tenant: <project>-<name>}`.
   vCluster adds that nodeSelector to every pod it syncs to the parent, and syncs
   only nodes with that label back into the cluster.
2. The parent's `tenant` pool selects `demo.vcluster.com/tenant Exists`.
3. The `kvm-gb300-1gpu-tenant` NodeType sets the property
   `demo.vcluster.com/tenant: "*"`, which Platform turns into an `Exists`
   requirement, so the pool can use it for any cluster.
4. The cluster's first pending pod makes Karpenter, inside the parent, create a
   NodeClaim that requires that one tenant value
   (`demo.vcluster.com/tenant In [gpu-test-matrix-gpu-baseline]`). When the VM
   joins, Karpenter copies the label to the Node, and the node appears only in its
   own cluster.
5. When the cluster sleeps or is deleted, its node empties and is consolidated
   after 2 minutes. The VM goes back to the GB300 tray.

Two keys, two jobs: `demo.vcluster.com/tenant` is the per-cluster **label**, and
`demo.vcluster.com/tenant-pool=true:NoSchedule` is the pool's **taint**, the same
on every tenant node.

To see the tenant value, look at the Karpenter NodeClaims inside the parent, not
the Platform NodeClaims in `p-platform`. The Platform NodeClaim only asks the
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
node belongs to one cluster and is reprovisioned when it's returned.

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

- **No `syncBackChanges` on the clusters.** vCluster hides the taints a cluster's
  pods tolerate (here, through `enforceTolerations`) from the cluster's view of
  its node. With `syncBackChanges: true`, any node update from inside the cluster
  (the GPU Operator and NFD label nodes) writes that view back to the real node
  and erases the pool's taint. Without the taint, parent pods and other clusters'
  control planes land on tenant GPU nodes, which then never consolidate.
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
- **The parent provides the `nvidia` RuntimeClass** through
  `experimental.deploy.vcluster.manifests`. Operand pods ask for it, and
  RuntimeClasses aren't synced down from the clusters.

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
