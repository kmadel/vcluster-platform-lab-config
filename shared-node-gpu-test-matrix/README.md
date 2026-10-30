# Shared Nodes GPU test matrix

Tenant clusters on Shared Nodes that each get their own dedicated GPU node, created on demand, and
run their own NVIDIA GPU driver version and NVIDIA GPU Operator version on it. Use it to qualify
a GPU stack upgrade next to the current version on identical hardware or even to test different GPU drivers, without
building a physical cluster for each combination.

The tenant clusters keep what Shared Nodes are good at for trusted dev, CI, and test
teams: control planes as pods, sleep mode, and one platform stack. The GPU node(s)
under each one is dedicated to it via a node selector and is reprovisioned clean when it's
returned, so no driver or runtime state carries over to the next cluster.

This is a pattern built from today's pieces, not a product feature.

## How it works

```text
spark-cluster (control plane cluster)
└─ elastic-cp-parent        Private Nodes + KubeVirt Auto Nodes (could be any node provider), project `platform` (private Platform project for tenant clusters on Private Nodes also used as control plane clusters),
   │                        also connected to Platform as a control plane cluster
   ├─ system pool           kvm-medium VMs: parent system pods, cluster control planes
   └─ tenant pool           kvm-gb300-1gpu-tenant VMs, tainted, one per cluster
      ├─ gpu-baseline       project gpu-test-matrix: driver 580.65.06, GPU Operator v26.3.2
      └─ gpu-candidate      project gpu-test-matrix: driver 580.82.07, GPU Operator v26.3.3
```

Each tenant cluster's identity reaches the parent control plane cluster on its pods, so the parent's config
never changes when a tenant cluster is added:

1. The `shared-node-gpu-tenant` cluster template sets
   `sync.fromHost.nodes.selector.labels: {demo.vcluster.com/tenant: <project>-<name>}`.
   vCluster adds that nodeSelector to every pod it syncs to the parent, and syncs
   only nodes with that label back into the tenant cluster.
2. The parent's auto nodes dynamic `tenant` pool selects `demo.vcluster.com/tenant Exists`.
3. The KubeVirt node provider (could be any node provider) `kvm-gb300-1gpu-tenant` NodeType sets the property
   `demo.vcluster.com/tenant: "*"`, which Platform turns into an `Exists`
   requirement, so the pool can use it for any child Shared Node tenant cluster.
4. Taking the tenant cluster `gpu-baseline` as the example: the first pod it syncs to
   `elastic-cp-parent` waits there for a node. Karpenter, running in the 
   `elastic-cp-parent` control plane cluster, creates a NodeClaim that requires that one tenant value
   (`demo.vcluster.com/tenant In [gpu-test-matrix-gpu-baseline]`). When the VM
   joins `elastic-cp-parent` satisfying the original Karpent NodeClaim, Karpenter copies that label to the new Node. Since
   `gpu-baseline` syncs only nodes with its own tenant label, the node shows up in
   `gpu-baseline` and in no other child Shared Nodes tenant cluster.
5. When `gpu-baseline` sleeps or is deleted, its node empties and is consolidated
   after 2 minutes. The VM goes back to the GB300 tray. Scaling a workload down
   inside `gpu-baseline` releases an extra node only through the
   `tenant-node-reaper` CronJob, not through Karpenter; see
   [Scaling in](#scaling-in).

Two keys, two jobs:

- `demo.vcluster.com/tenant` is the per-cluster **label**. It keeps tenant clusters off
  each other's nodes: vCluster adds it as a `nodeSelector` to every pod it syncs,
  so a `gpu-candidate` pod can only schedule on a node labeled for
  `gpu-candidate`. Seeing a node isn't what stops it.
- `demo.vcluster.com/tenant-pool=true:NoSchedule` is the pool's **taint**, the same
  on every tenant node. It keeps off pods that have no tenant selector, such as the tenant
  clusters' control planes and the parent's own pods.

The taint alone is a soft barrier: any pod can tolerate it, by key or with a
wildcard (`operator: Exists`). A cluster on `elastic-cp-parent` created without
the tenant selector could then put its pods on any tenant cluster's GPU node. The
`tenant-pool-isolation` admission policy closes that; see
[Settings that matter](#settings-that-matter).

To see the tenant value, look at the Karpenter `NodeClaims` inside the parent, not
the Platform `NodeClaims` in `p-platform`. The Platform `NodeClaim` only asks the
KubeVirt provider for a VM of a type, and carries no tenant value:

```bash
vcluster platform connect vcluster elastic-cp-parent --project platform
kubectl get nodeclaims.karpenter.sh -L demo.vcluster.com/tenant
```

### The GPU stack (specifically for mocked GPUs)

Each tenant cluster installs its own GPU stack with the `shared-node-gpu-operator`
Stack, in order:

1. **nvml-mock** (`demo-nvml-mock`), the simulated driver, reporting the driver
   version the cluster picked (`driverVersion`).
2. **The GPU Operator** (`demo-gpu-operator-mock`), the version the cluster
   picked (`gpuOperatorVersion`), with the driver and toolkit operands off.

The GPUs are simulated: the GB300 tray (`rack-a-u20-xlarge`) is carved into
one-GPU-slot VMs, and nvml-mock makes each VM report one GB300. With real GPUs,
the Stack would be the GPU Operator alone, installing the driver version the
cluster asks for (`driver.enabled`, `driver.version`). That's safe because the
node belongs to one tenant cluster on Shared Nodes and is reprovisioned when it's returned.

## Files

| Path | What it is |
|---|---|
| `parent/elastic-cp-parent.yaml` | The parent cluster: `system` and `tenant` pools, embedded etcd, Argo CD integration, and what it provides every cluster: the `nvidia` RuntimeClass, the `tenant-pool-isolation` admission policy, and the `tenant-node-reaper` CronJob |
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
- **`tenant-pool-isolation` keeps every cluster on its own nodes.** A built-in
  Kubernetes `ValidatingAdmissionPolicy` on `elastic-cp-parent`, no Kyverno needed.
  It applies to pods synced from a cluster (they carry
  `vcluster.loft.sh/managed-by`) that tolerate the tenant pool, by key or with a
  wildcard. Such a pod must have a `demo.vcluster.com/tenant` selector equal to
  `<project>-<name>`, computed from its host namespace (`p-<project>-v-<name>`).
  Otherwise it's rejected, including any pod from a namespace that doesn't follow
  that pattern. The parent's own pods (flannel, kube-proxy) also tolerate
  everything and aren't affected. Without it, the separation depends on every
  cluster on the parent being created from `shared-node-gpu-tenant`; the
  `gpu-test-matrix` project enforces that for its own users, but admins of other
  projects that can use the parent aren't held to it.
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
- **`elastic-cp-parent` has its own `nvidia` RuntimeClass.** A Shared Nodes
  tenant cluster's pods run in `elastic-cp-parent`, so anything Kubernetes needs to
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
  definition in Git, and one copy serves every Shared Nodes tenant cluster on it. Its
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
tenant cluster's Nodes view shows one node with its own name.

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
  `cluster-collector-with-embedded-etcd`, like every Private Nodes cluster.
  `parent-dcgm-collector` adds the DCGM exporters of every cluster on it, with
  `tenant_cluster` and `tenant_project` labels, written with the parent's scope.
  In Grafana, use the `elastic-cp-parent` datasource, for example
  `DCGM_FI_DEV_GPU_UTIL{tenant_cluster="gpu-candidate"}`.
- **Tenant Clusters:** the bundled `shared-node-tenant-collector` scrapes only each
  cluster's API server and etcd, so the cluster-level Observability tab shows no
  GPU data. GPU metrics for these clusters live on the parent.
- **License page:** on an online license, "Attached GPUs" stays at 0. The GPU
  meter needs usage snapshots, which need the Platform database, which needs an
  offline license key. The clusters' `nvidia.com/gpu` is the live number.

## Scaling in

A cluster can scale out: give `gpu-baseline` a workload that needs more GPUs than
its nodes have, and its pending pod gets it another GPU VM, labeled for
`gpu-baseline`, within a few minutes. Scaling that workload back down does **not**
make Karpenter release the extra node; the `tenant-node-reaper` CronJob below
does.

Karpenter, running in `elastic-cp-parent`, consolidates a `WhenEmpty` node only
when nothing but DaemonSet pods is left on it, and it recognizes a DaemonSet pod
only by an owner reference with `apiVersion: apps/v1` and `kind: DaemonSet`
(`IsReschedulable` in Karpenter's `pkg/utils/pod/scheduling.go`). The DaemonSet
pods from `gpu-baseline` (nvml-mock, the GPU Operator's operands) run in
`elastic-cp-parent` as copies that vCluster owns by `Service/gpu-baseline`, because
the DaemonSets themselves exist only in `gpu-baseline`. So after a scale-down,
Karpenter sees the extra node still running ordinary looking pods and leaves it.
`WhenEmptyOrUnderutilized` doesn't help either: the copies are pinned to their
node by name, so Karpenter can't move them.

What releases a node:

- **Sleep or delete the cluster.** All of its pods go away, DaemonSet copies
  included, and its nodes are consolidated after 2 minutes. This is the scale-in
  to use in a demo. Sleep scales Deployments to zero and gives each DaemonSet a
  `nodeSelector` that no node matches, so their pods are removed too. The
  `shared-node-gpu-tenant` template puts every tenant cluster to sleep at midnight
  (`sleep.auto.schedule: "0 0 * * *"`), so extra GPU nodes are released every
  night without anyone doing it. A cluster wakes on its next access. Midnight is
  in the timezone from the `demo.vcluster.com/sleep-timezone` annotation on
  `elastic-cp-parent`'s Cluster resource (`../clusters/elastic-cp-parent.yaml`),
  or UTC without it.
- **The `tenant-node-reaper` CronJob does it in about 4 minutes.**
  `elastic-cp-parent` runs it every minute (defined with the RuntimeClass in
  `parent/elastic-cp-parent.yaml`). For each tenant node it checks whether every
  running pod came from a DaemonSet: the parent's own DaemonSet pods are owned by a
  DaemonSet, and vCluster marks synced copies of a cluster's DaemonSet pods with
  `vcluster.loft.sh/owner-set-kind: DaemonSet`. A node that is idle on two runs
  at least 2 minutes apart is deleted.

  A new node also starts out with only DaemonSet pods, before the pod that asked
  for it schedules, so the job keeps a node that:
  - Karpenter hasn't initialized yet, or initialized less than 2 minutes ago.
    Initialized means the node's extended resources (its GPU) are registered,
    which is when a waiting GPU pod can bind.
  - A Pending pod is waiting for, by the node's tenant label.

  Measured on `gpu-candidate`: after scaling `gpu-demo` from 2 to 1, its extra node
  was gone 4.4 minutes later (75 seconds of that was the pod terminating). Scaling
  back to 2, the new node was kept throughout its 4-minute start-up until the
  replica was running on it.

  Deleting the Node object is enough. Karpenter's `karpenter.sh/termination`
  finalizer drains it, deletes its NodeClaim, and the VM goes back to the tray,
  in about a minute. The job needs only plain RBAC in `elastic-cp-parent` (list
  pods and nodes, read Karpenter NodeClaims, patch and delete nodes), no Platform
  credential. A node with
  the `karpenter.sh/do-not-disrupt: "true"` annotation is skipped.

  To check a node by hand, or release it right away:

  ```bash
  # In elastic-cp-parent: pods on the node that did not come from a DaemonSet.
  kubectl get pods -A --field-selector spec.nodeName=<node> -o json | jq -r '
    .items[] | select((.metadata.annotations["vcluster.loft.sh/owner-set-kind"] // "") != "DaemonSet")
    | select([.metadata.ownerReferences[]? | select(.kind == "DaemonSet")] | length == 0)
    | "\(.metadata.namespace)/\(.metadata.name)"'
  # Nothing printed: release it.
  kubectl delete node <node>
  ```

Alternatives that don't work here:

- **`syncSettings.setOwner: false`** only removes the `Service` owner reference,
  which leaves the copies with no owner at all.
- **A pod patch that adds a `DaemonSet` owner reference** needs the UID of a real
  DaemonSet in the cluster's host namespace. The API server assigns UIDs, so it
  can't be set in advance, and the garbage collector strips an owner reference
  whose UID doesn't match while another owner is valid.
- **An autoscaler inside the Shared Nodes cluster**, which sees the real
  DaemonSet owners, needs a provider that can create nodes for the parent. The
  Auto Nodes Karpenter provider is built into Private Nodes and has no
  standalone form. With EKS and AWS Karpenter that's possible in principle, but
  the guidance from a customer case was not to run an autoscaler inside a
  vCluster, and to run such DaemonSets on the host instead. That doesn't fit
  here, where each cluster owns its GPU Operator and nvml-mock.

The same root cause hits host-level Karpenter on EKS with DaemonSets inside a
vCluster, so it's worth raising with the Auto Nodes team: vCluster already marks
the copies with `vcluster.loft.sh/owner-set-kind: DaemonSet`, which Karpenter
could honor.

## Limits

- **Scaling a workload down releases its node only after about 4 minutes,**
  through `tenant-node-reaper`, not through Karpenter. See
  [Scaling in](#scaling-in).
- **Simulated GPUs.** The driver version per cluster is what nvml-mock reports,
  not an installed driver.
- **`driverVersion` needs nvml-mock 0.6.0 or later.** In chart 0.5.0 and 0.5.1 it
  only sets the node agent's `DRIVER_VERSION` environment variable; NVML, and so
  `nvidia-smi`, GFD, and DCGM, still report the profile's `580.65.06`. The chart on
  `main` (0.6.0-dev) writes it into the profile's `system.driver_version`. Once
  0.6.0 is released, raise `nvmlMockVersion` in the cluster template.
- **Control plane storage.** Each tenant cluster's embedded etcd is a `local-path` volume
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
