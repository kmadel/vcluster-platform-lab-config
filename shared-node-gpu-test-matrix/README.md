# Shared-node GPU test matrix

Shared-node clusters that each get their own GPU node, created on demand, and
run their own GPU Operator version on it. Use it to qualify a GPU Operator
upgrade (or, with real GPUs, a driver) next to the current version on identical
hardware, without building a physical cluster for each combination.

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
      ├─ gpu-baseline       shared-node cluster, project gpu-test-matrix, GPU Operator v26.3.2
      └─ gpu-candidate      shared-node cluster, project gpu-test-matrix, GPU Operator v26.3.3
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
4. The first pending pod (the cluster's GPU Operator) makes Karpenter create a
   NodeClaim that requires that one tenant value, and the new VM is labeled with
   it. The node appears only in its own cluster.
5. When the cluster sleeps or is deleted, its node empties and is consolidated
   after 2 minutes. The VM goes back to the GB300 tray.

The GPUs are simulated: the GB300 tray (`rack-a-u20-xlarge`) runs nvml-mock and
is carved into one-GPU VMs, and each VM gets nvml-mock from its cloud-init.

## Files

| Path | What it is |
|---|---|
| `parent/elastic-cp-parent.yaml` | The parent cluster: `system` and `tenant` pools, embedded etcd, Argo CD integration, the `nvidia` RuntimeClass |
| `parent/parent-dcgm-collector-template.yaml` | Collector that scrapes every cluster's DCGM exporter on the parent |
| `parent/parent-dcgm-collector.yaml` | Binds that collector to `elastic-cp-parent` |
| `tenants/project.yaml` | Project `gpu-test-matrix`, allowed to use only `elastic-cp-parent` |
| `tenants/shared-node-gpu-tenant-template.yaml` | The cluster template, with a `gpuOperatorVersion` parameter |
| `tenants/shared-node-gpu-operator-stack.yaml` | Stack that installs the GPU Operator through `demo-gpu-operator-mock` |
| `tenants/gpu-baseline.yaml`, `tenants/gpu-candidate.yaml` | The two clusters |

Defined outside this folder:

- **`kvm-gb300-1gpu-tenant`** NodeType, with the other KubeVirt NodeTypes in
  `../private-node-kubevirt-provider/kubevirt-provider.yaml`.
- **`gpu-tray-host`** static pool (`quantity: 1`), which claims the GB300 tray, in
  `../private-node-kubevirt-provider/private-node-provider-dynamic-template.yaml`.
- **`bootstrap/shared-node-gpu-test-matrix.yaml`**, the Argo CD Application for
  this folder.

## Settings that matter

- **The parent's `system` pool needs `demo.vcluster.com/tenant DoesNotExist`.**
  The Auto Nodes cloud provider registers every NodeType property key as a
  well-known label, and Karpenter lets a pool serve a pod that asks for a
  well-known label the pool doesn't mention. Without this selector, a pending
  cluster pod can land on the untainted system pool and stamp its tenant label on
  a plain CPU node.
- **The `tenant` pool is tainted**, and the cluster template adds the toleration
  with `sync.toHost.pods.enforceTolerations`.
- **`syncBackChanges: true`** lets the GPU Operator's `nvidia.com/gpu.deploy.*`
  labels, which it writes on the cluster's node, reach the parent's node, where
  its DaemonSet pods actually run. That's only reasonable because each node
  belongs to one cluster.
- **`nodetype.loft.sh/allow-unneeded-special-resources: "true"`** on the NodeType.
  A cluster's first pods request no GPU, and dynamic claims otherwise skip a
  NodeType with an extended resource the claim doesn't request.
- **Embedded CoreDNS** in each cluster, so no CoreDNS pod sits on (and holds) the
  GPU node.
- **The parent provides the `nvidia` RuntimeClass** through
  `experimental.deploy.vcluster.manifests`. Operand pods ask for it, and
  RuntimeClasses aren't synced down from the clusters.

## Bring it up from scratch

1. Push. Argo CD syncs the provider change, then this folder.
2. Sync `kubevirt-provider-cluster`'s template change in the Platform UI if the
   instance shows `TemplateChangesAvailable`. That brings up the GB300 tray.
3. Once `elastic-cp-parent` is Ready, connect it as a control plane cluster. This
   is the one manual step:

   ```bash
   vcluster platform connect vcluster elastic-cp-parent --project platform
   vcluster platform add cluster elastic-cp-parent \
     --display-name "elastic-cp-parent" \
     --helm-chart-version <the Platform version> --wait
   ```

4. The clusters in `gpu-test-matrix` then come up. Each one's GPU Operator
   starts its GPU VM. Expect a few minutes for the VM to boot and join.

## Show it

From the Platform UI: the Clusters list shows `1 GPU` for each cluster, and each
cluster's Nodes view shows one node with its own name.

From a terminal:

```bash
vcluster platform connect vcluster gpu-baseline --project gpu-test-matrix
kubectl get nodes -o custom-columns='NODE:.metadata.name,GPUS:.status.allocatable.nvidia\.com/gpu,PRODUCT:.metadata.labels.nvidia\.com/gpu\.product'
kubectl exec -n gpu-operator ds/nvidia-device-plugin-daemonset -c nvidia-device-plugin -- nvidia-smi -L
kubectl get deploy gpu-operator -n gpu-operator -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
```

Repeat with `gpu-candidate`. Point to the node names, not the GPU UUIDs (see
below).

To add a version to the matrix, copy `tenants/gpu-candidate.yaml` with a new name
and `gpuOperatorVersion`. Nothing on the parent changes.

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

## Limits

- **Simulated GPUs.** nvml-mock simulates NVML, not the driver. Operator versions
  really differ per cluster, but the driver version is the one the mock reports
  (`580.65.06`). With real GPUs, per-cluster drivers are real: in passthrough VMs
  the driver lives in the guest, and bare-metal nodes are reprovisioned clean.
- **GPU Operator v26.7.x and later don't run on nvml-mock.** The device plugin's
  init container waits for an `nvidia` kernel module. Stay on v26.3.x.
- **Every GPU has the same UUID** (`GPU-b300b300-0000-…`); nvml-mock hard-codes
  them per profile. Shown side by side, it looks like one shared GPU.
- **Four clusters at most** with GPUs at a time: the tray has 4 simulated GPUs.
- **The parent's connection is manual**, and so is its writer key's scope: GPU
  metrics are written under the parent, not under each cluster.
