# Auto Nodes for Shared Nodes

Tenant clusters on Shared Nodes that each get their own dedicated nodes, created
on demand by a Private Nodes parent with Auto Nodes, and returned when the tenant
cluster no longer needs them. Each tenant cluster owns the node-level software on
its nodes: drivers, operators, container runtimes. So different tenant clusters
can test different versions of that software side by side, without building a
physical cluster for each combination.

The tenant clusters keep what Shared Nodes are good at for trusted dev, CI, and
test teams: control planes as pods, sleep mode, and one platform stack. The nodes
under each one are dedicated to it through a node selector and are reprovisioned
clean when they're returned, so no driver or runtime state carries over to the
next tenant cluster.

This is a pattern built from today's pieces, not a product feature.

## Use cases

| Use case | Status | What each tenant cluster owns |
|---|---|---|
| [GPU test matrix](use-cases/gpu-test-matrix/) | Running | NVIDIA GPU driver (simulated with nvml-mock) and GPU Operator versions |
| vNode versions | Candidate | The vNode container runtime version on its nodes. A strong fit: a runtime is node-level software that needs a dedicated node. |
| DRA driver versions (for example with Dynamo) | Needs validation | A DRA driver. Inside a Shared Nodes tenant cluster, the driver publishes ResourceSlices to the tenant cluster's API, but the parent's scheduler allocates devices from the parent's. vCluster's DRA sync is documented as host to tenant cluster only, so this may need Private Nodes instead. |

To add one, see [Adding a use case](#adding-a-use-case).

## How it works

```text
spark-cluster (control plane cluster)
└─ elastic-cp-parent        Private Nodes + Auto Nodes (KubeVirt here; could be any node provider),
   │                        project `platform`, also connected to Platform as a control plane cluster
   ├─ system pool           kvm-medium VMs: parent system pods, tenant cluster control planes
   └─ tenant pool           tenant NodeTypes, tainted, nodes dedicated to one tenant cluster each
      ├─ gpu-baseline       project gpu-test-matrix (GPU test matrix use case)
      └─ gpu-candidate      project gpu-test-matrix (GPU test matrix use case)
```

Each tenant cluster's identity reaches the parent control plane cluster on its
pods, so the parent's config never changes when a tenant cluster is added:

1. The tenant cluster's template sets
   `sync.fromHost.nodes.selector.labels: {demo.vcluster.com/tenant: <project>-<name>}`.
   vCluster adds that nodeSelector to every pod it syncs to the parent, and syncs
   only nodes with that label back into the tenant cluster.
2. The parent's Auto Nodes dynamic `tenant` pool selects
   `demo.vcluster.com/tenant Exists`.
3. Each tenant NodeType (for the GPU test matrix, the KubeVirt node provider's
   `kvm-gb300-1gpu-tenant`) sets the property `demo.vcluster.com/tenant: "*"`,
   which Platform turns into an `Exists` requirement, so the pool can use it for
   any child Shared Nodes tenant cluster.
4. Taking the tenant cluster `gpu-baseline` as the example: the first pod it syncs
   to `elastic-cp-parent` waits there for a node. Karpenter, running in the
   `elastic-cp-parent` control plane cluster, creates a NodeClaim that requires that
   one tenant value (`demo.vcluster.com/tenant In [gpu-test-matrix-gpu-baseline]`).
   When the VM joins `elastic-cp-parent`, satisfying that Karpenter NodeClaim,
   Karpenter copies that label to the new Node. Since `gpu-baseline` syncs only
   nodes with its own tenant label, the node shows up in `gpu-baseline` and in no
   other child Shared Nodes tenant cluster.
5. When `gpu-baseline` sleeps or is deleted, its nodes empty and are consolidated
   after 2 minutes, and the VMs go back to the node provider. Scaling a workload
   down inside `gpu-baseline` releases an extra node only through the
   `tenant-node-reaper` CronJob, not through Karpenter; see
   [Scaling in](#scaling-in).

Two keys, two jobs:

- `demo.vcluster.com/tenant` is the per-cluster **label**. It keeps tenant clusters
  off each other's nodes: vCluster adds it as a `nodeSelector` to every pod it
  syncs, so a `gpu-candidate` pod can only schedule on a node labeled for
  `gpu-candidate`. Seeing a node isn't what stops it.
- `demo.vcluster.com/tenant-pool=true:NoSchedule` is the pool's **taint**, the same
  on every tenant node. It keeps off pods that have no tenant selector, such as the
  tenant clusters' control planes and the parent's own pods.

The taint alone is a soft barrier: any pod can tolerate it, by key or with a
wildcard (`operator: Exists`). A tenant cluster on `elastic-cp-parent` created
without the tenant selector could then put its pods on any tenant cluster's node.
The `tenant-pool-isolation` admission policy closes that; see
[Settings that matter](#settings-that-matter).

To see the tenant value, look at the Karpenter `NodeClaims` inside the parent, not
the Platform `NodeClaims` in `p-platform`. The Platform `NodeClaim` only asks the
node provider for a machine of a type, and carries no tenant value:

```bash
vcluster platform connect vcluster elastic-cp-parent --project platform
kubectl get nodeclaims.karpenter.sh -L demo.vcluster.com/tenant
```

## Files

| Path | What it is |
|---|---|
| `parent/elastic-cp-parent.yaml` | The parent cluster: `system` and `tenant` pools, embedded etcd, Argo CD integration, and what it provides every tenant cluster: the `tenant-pool-isolation` admission policy, the `tenant-node-reaper` CronJob, and use case pieces that must live on the parent (the GPU test matrix's `nvidia` RuntimeClass) |
| `use-cases/<name>/` | One folder per use case: its project, cluster template, Stack, tenant clusters, and README |

Defined outside this folder:

- **`../clusters/elastic-cp-parent.yaml`**, a metadata-only overlay for the parent's
  Cluster resource, with its `demo.vcluster.com/sleep-timezone` annotation. It lives
  under `clusters/` because that Application doesn't prune.
- **Tenant NodeTypes**, with the other KubeVirt NodeTypes in
  `../private-node-kubevirt-provider/kubevirt-provider.yaml`.
- **`../bootstrap/shared-node-auto-nodes.yaml`**, the Argo CD Application for this
  folder. It keeps its original name, `shared-node-gpu-test-matrix`, so that moving
  the folder didn't prune anything.

## Settings that matter

- **No `syncBackChanges` in a tenant cluster template.** Combined with
  `enforceTolerations`, it removes the `tenant` pool's taint from the tenant node:
  1. `enforceTolerations` makes every `gpu-baseline` pod tolerate
     `demo.vcluster.com/tenant-pool`. vCluster therefore leaves that taint out of
     `gpu-baseline`'s copy of its node, since none of its pods are affected by it.
  2. Any controller in `gpu-baseline` that labels nodes (the GPU Operator and NFD
     in the GPU test matrix) updates that copy of the node.
  3. With `syncBackChanges: true`, vCluster writes the copy back to the real node
     in `elastic-cp-parent`, including its taint list, which no longer has the
     taint.

  Once the taint is gone, `elastic-cp-parent`'s own pods and the other tenant
  clusters' control plane pods can be scheduled on `gpu-baseline`'s node. That
  mixes tenant clusters on one node, and the node never empties, so it's never
  consolidated.
- **Node labels a tenant cluster's node agents need come from the NodeType.**
  The kubelet checks a pod's node selector against the real node. Without
  sync-back, labels written inside a tenant cluster never reach it, so set them as
  NodeType properties, which become node labels at boot. (The GPU test matrix does
  this for the GPU Operator's `nvidia.com/gpu.deploy.*` labels.)
- **`tenant-pool-isolation` keeps every tenant cluster on its own nodes.** A built-in
  Kubernetes `ValidatingAdmissionPolicy` on `elastic-cp-parent`, no Kyverno needed.
  It applies to pods synced from a tenant cluster (they carry
  `vcluster.loft.sh/managed-by`) that tolerate the tenant pool, by key or with a
  wildcard. Such a pod must have a `demo.vcluster.com/tenant` selector equal to
  `<project>-<name>`, computed from its host namespace (`p-<project>-v-<name>`).
  Otherwise it's rejected, including any pod from a namespace that doesn't follow
  that pattern. The parent's own pods (flannel, kube-proxy) also tolerate
  everything and aren't affected. Without it, the separation depends on every
  tenant cluster on the parent being created from a template with the tenant
  selector; a project's allowed templates enforce that for its own users, but
  admins of other projects that can use the parent aren't held to it.
- **The parent's `system` pool needs `demo.vcluster.com/tenant DoesNotExist`.**
  The Auto Nodes cloud provider registers every NodeType property key as a
  well-known label, and Karpenter lets a pool serve a pod that asks for a
  well-known label the pool doesn't mention. Without this selector, a pending
  tenant cluster pod can land on the untainted system pool and stamp its tenant
  label on a plain CPU node.
- **`nodetype.loft.sh/allow-unneeded-special-resources: "true"`** on any tenant
  NodeType that advertises an extended resource, such as `nvidia.com/gpu`. A tenant
  cluster's first pods often don't request it, and dynamic claims otherwise skip a
  NodeType with an extended resource the claim doesn't request.
- **Embedded CoreDNS** in each tenant cluster, so no CoreDNS pod sits on (and
  holds) a tenant node.
- **Cluster-scoped objects a tenant cluster's pods reference must exist on the
  parent.** A tenant cluster's pods run in `elastic-cp-parent`, so anything
  Kubernetes needs to start them has to exist there, not only in the tenant cluster
  that created them, and vCluster doesn't sync objects like RuntimeClasses down.
  `elastic-cp-parent` creates them inside itself with
  `experimental.deploy.vcluster.manifests`, so they're part of the parent's
  definition in Git, and one copy serves every tenant cluster on it. The GPU test
  matrix's `nvidia` RuntimeClass is the example; see its README.

Check that every tenant node still has its taint:

```bash
kubectl get nodes -L demo.vcluster.com/tenant \
  -o custom-columns='NODE:.metadata.name,TENANT:.metadata.labels.demo\.vcluster\.com/tenant,TAINTS:.spec.taints[*].key'
```

## Bring it up from scratch

1. Push. Argo CD syncs this folder.
2. Sync `private-node-kubevirt-provider-config` by hand (its auto-sync is off).
   That creates the tenant NodeTypes. Each use case's README lists anything else
   its NodeTypes need, such as the GPU test matrix's GB300 tray.
3. Once `elastic-cp-parent` is Ready, connect it as a control plane cluster. This
   is the one manual step:

   ```bash
   vcluster platform connect vcluster elastic-cp-parent --project platform
   vcluster platform add cluster elastic-cp-parent \
     --display-name "elastic-cp-parent" \
     --helm-chart-version <the Platform version> --wait
   ```

4. The tenant clusters then come up, and each one's first pods bring up its
   nodes. Expect a few minutes for a VM to boot and join.

Platform doesn't roll template changes onto existing instances. After changing a
tenant cluster template, delete its tenant clusters and let Argo CD recreate them,
or apply the update to each instance in the UI.

## Adding a use case

1. **A tenant NodeType** in the node provider, with `demo.vcluster.com/tenant: "*"`,
   any labels the tenant cluster's node agents need as properties, and
   `nodetype.loft.sh/allow-unneeded-special-resources: "true"` if it advertises an
   extended resource.
2. **A folder under `use-cases/`** with a project (allowed to use only
   `elastic-cp-parent`, with a `p-{{.Values.loft.project}}-v-{{.Values.loft.name}}`
   namespace pattern, which `tenant-pool-isolation` relies on), a tenant cluster
   template, a Stack for the node-level software, the tenant clusters, and a
   README.
3. **The template must keep** the tenant `nodeSelector` labels, the
   `enforceTolerations` entry for the pool's taint, embedded etcd and CoreDNS, the
   nightly sleep, and no `syncBackChanges`. Copy the GPU test matrix's
   `shared-node-gpu-tenant` template as a starting point.
4. **Anything cluster-scoped its pods reference** (for example a RuntimeClass)
   goes into the parent's `experimental.deploy.vcluster.manifests`.

## Scaling in

A tenant cluster can scale out: give `gpu-baseline` a workload that needs more
than its nodes have, and its pending pod gets it another node, labeled for
`gpu-baseline`, within a few minutes. Scaling that workload back down does **not**
make Karpenter release the extra node; the `tenant-node-reaper` CronJob below
does.

Karpenter, running in `elastic-cp-parent`, consolidates a `WhenEmpty` node only
when nothing but DaemonSet pods is left on it, and it recognizes a DaemonSet pod
only by an owner reference with `apiVersion: apps/v1` and `kind: DaemonSet`
(`IsReschedulable` in Karpenter's `pkg/utils/pod/scheduling.go`). The DaemonSet
pods from `gpu-baseline` (nvml-mock and the GPU Operator's operands in the GPU test
matrix) run in `elastic-cp-parent` as copies that vCluster owns by
`Service/gpu-baseline`, because the DaemonSets themselves exist only in
`gpu-baseline`. So after a scale-down, Karpenter sees the extra node still running
ordinary looking pods and leaves it. `WhenEmptyOrUnderutilized` doesn't help
either: the copies are pinned to their node by name, so Karpenter can't move them.

What releases a node:

- **Sleep or delete the tenant cluster.** All of its pods go away, DaemonSet copies
  included, and its nodes are consolidated after 2 minutes. This is the scale-in
  to use in a demo. Sleep scales Deployments to zero and gives each DaemonSet a
  `nodeSelector` that no node matches, so their pods are removed too. The tenant
  cluster templates put every tenant cluster to sleep at midnight
  (`sleep.auto.schedule: "0 0 * * *"`), so extra nodes are released every night
  without anyone doing it. A tenant cluster wakes on its next access. Midnight is
  in the timezone from the `demo.vcluster.com/sleep-timezone` annotation on
  `elastic-cp-parent`'s Cluster resource (`../clusters/elastic-cp-parent.yaml`),
  or UTC without it.
- **The `tenant-node-reaper` CronJob does it in about 4 minutes.**
  `elastic-cp-parent` runs it every minute (defined in
  `parent/elastic-cp-parent.yaml`). For each tenant node it checks whether every
  running pod came from a DaemonSet: the parent's own DaemonSet pods are owned by a
  DaemonSet, and vCluster marks synced copies of a tenant cluster's DaemonSet pods
  with `vcluster.loft.sh/owner-set-kind: DaemonSet`. A node that is idle on two
  runs at least 2 minutes apart is deleted.

  A new node also starts out with only DaemonSet pods, before the pod that asked
  for it schedules, so the job keeps a node that:
  - Karpenter hasn't initialized yet, or initialized less than 2 minutes ago.
    Initialized means the node's extended resources (for example its GPU) are
    registered, which is when a waiting pod that requests them can bind.
  - A Pending pod is waiting for, by the node's tenant label.

  Measured on `gpu-candidate`: after scaling `gpu-demo` from 2 to 1, its extra node
  was gone 4.4 minutes later (75 seconds of that was the pod terminating). Scaling
  back to 2, the new node was kept throughout its 4-minute start-up until the
  replica was running on it.

  Deleting the Node object is enough. Karpenter's `karpenter.sh/termination`
  finalizer drains it, deletes its NodeClaim, and the VM goes back to the node
  provider, in about a minute. The job needs only plain RBAC in
  `elastic-cp-parent` (list pods and nodes, read Karpenter NodeClaims, patch and
  delete nodes), no Platform credential. A node with the
  `karpenter.sh/do-not-disrupt: "true"` annotation is skipped.

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
  DaemonSet in the tenant cluster's host namespace. The API server assigns UIDs, so
  it can't be set in advance, and the garbage collector strips an owner reference
  whose UID doesn't match while another owner is valid.
- **An autoscaler inside the Shared Nodes tenant cluster**, which sees the real
  DaemonSet owners, needs a provider that can create nodes for the parent. The
  Auto Nodes Karpenter provider is built into Private Nodes and has no
  standalone form. With EKS and AWS Karpenter that's possible in principle, but
  the guidance from a customer case was not to run an autoscaler inside a
  vCluster, and to run such DaemonSets on the host instead. That doesn't fit
  here, where each tenant cluster owns its node-level software.

The same root cause hits host-level Karpenter on EKS with DaemonSets inside a
vCluster, so it's worth raising with the Auto Nodes team: vCluster already marks
the copies with `vcluster.loft.sh/owner-set-kind: DaemonSet`, which Karpenter
could honor.

## Limits

- **Scaling a workload down releases its node only after about 4 minutes,**
  through `tenant-node-reaper`, not through Karpenter. See
  [Scaling in](#scaling-in).
- **Control plane storage.** Each tenant cluster's embedded etcd is a `local-path`
  volume on whichever `system` node it lands on. If that node is replaced, the
  tenant cluster's state is lost (it is all re-created from Git, but the tenant
  cluster restarts empty).
- **The parent's connection is manual**, and so is its writer key's scope: metrics
  collected on the parent are written under the parent, not under each tenant
  cluster.
- **Argo CD on 4.13 alphas.** Enabling the parent's Argo CD integration together
  with a change that restarts its control plane left its first syncs hung until
  the application controller was restarted. Platform 5.0.0-alpha.1 and 4.12.3
  include a fix that waits for the cluster to be Ready before registering it
  (loft-enterprise #8403).
