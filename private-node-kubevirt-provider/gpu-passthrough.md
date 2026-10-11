# RTX 2000 Ada through the KubeVirt provider

How the lab's one NVIDIA GPU reaches a tenant cluster as a KubeVirt VM, through
the `kubevirt-provider-cluster` KubeVirtProviderCluster and its embedded KubeVirt
NodeProvider (`kubevirt-us-va-blacksburg-dc1`).

Status: **working end to end, 2026-10-10.** Tenant cluster `gpu-vm-tenant`
(from `private-nodes-kubevirt-static-gpu-stack`) ran the whole `demo-gpu`
Stack to Healthy: cert-manager, GPU Operator with the containerized driver,
the `gpuready` and `dcgmready` gates, the CUDA smoke test, and NVSentinel.
The first attempt needed two fixes, both now in this repo: keep CPU VMs off
the GPU host (affinity), and bridge binding instead of masquerade (unique node
IPs). Both are described below.

For what changes on real HGX B200 or B300 servers, see
[gpu-passthrough-hgx-b200-b300.md](gpu-passthrough-hgx-b200-b300.md).

## The chain

The card is passed through twice, so every layer has to support device
assignment:

```text
vmetal (L0, physical, AMD)       RTX 2000 Ada on vfio-pci (10de:28b0, group 24)
 │  libvirt hostdev, behind an emulated Intel vIOMMU
 ▼
rack-c-u12-xlarge-gpu (L1)       Metal3 BMH, node of kubevirt-provider-cluster
 │  vCluster device operator binds it to vfio-pci (HostDevice rtx-2000-ada)
 │  KubeVirt advertises it as nvidia.com/RTX_2000_Ada
 ▼
KubeVirt VM (L2)                 private node of the GPU tenant cluster
                                 GPU Operator installs the driver here
```

One card means one GPU VM, which means one tenant cluster at a time. The same
BareMetalHost also backs `private-nodes-metal3-static-gpu-stack`, which hands the
card straight to a Metal3 tenant. While `kubevirt-provider-cluster` holds the
host, that template's GPU pool cannot be satisfied, and the reverse.

## What is in this repo

| File | What it does |
| --- | --- |
| `private-node-provider-dynamic-template.yaml` | Static `gpu-host` pool: claims the one `xlarge-gpu` machine into `kubevirt-provider-cluster`, on `ubuntu-noble-bootstrap`, labeled `demo.vcluster.com/gpu-host=true` |
| `provider-cluster/hostdevice-rtx-2000-ada.yaml` | HostDevice: bind `10de:28b0` to vfio-pci on that node, advertise it to KubeVirt as `nvidia.com/RTX_2000_Ada` |
| `provider-cluster-devices-app.yaml` | Argo CD Application delivering `provider-cluster/` into `kubevirt-provider-cluster` |
| `kubevirt-provider.yaml` | NodeTypes `kvm-gpu-rtx-2000-ada` (6 CPU, 10 GiB, the GPU, UEFI, 60 GiB root) and `kvm-medium` (2 CPU, 4 GiB) |
| `../tenant-management/cluster-templates/private-kubevirt-gpu-stack.yaml` | Tenant template `private-nodes-kubevirt-static-gpu-stack`: one GPU VM, one CPU service VM, and the `demo-gpu` Stack (GPU Operator, NVSentinel, `gpuready` gate) |

### Why the HostDevice goes through Argo CD

The HostDevice CRD only exists once vCluster Platform has installed the device
operator, which happens after the NodeProvider is set up, well after the tenant
cluster is created. The alternatives were weighed and rejected:

- **`experimental.deploy` manifests in the provider template.** Any edit changes
  the vCluster Helm values, which restarts the provider cluster's control plane.
- **`spec.template.objects` on the provider template.** Works, since Platform
  retries an apply that fails on a missing CRD, but the instance shows
  `AppsAndObjectsSynced=False` until then. Objects apply from the template
  snapshot on the instance, so every edit also needs a template sync. It would
  also put a one-host lab detail into a generic template.
- **The device operator's chart values.** The chart has no HostDevice values, so
  `deploy.vClusterDeviceOperator.helmValues` cannot create one.

The Application uses `SkipDryRunOnMissingResource` and unlimited retries, so its
first sync simply waits for the CRD. It targets the provider cluster by server
URL because Platform truncates and hashes the Argo CD cluster name.

`private-node-kubevirt-provider-config` (the parent Application) uses
`recurse: false`. That is what stops it from also applying `provider-cluster/`
to the management cluster, so keep it that way.

## Host-side prerequisites (not in Git)

These live in the libvirt domain on `vmetal`, so Git cannot carry them. Make
domain changes only while the BMH is `available` and powered off; do not
`virsh start` it by hand, because Ironic's power-state sync powers it back off.

### Done 2026-10-10

1. **vIOMMU.** `<iommu model='intel'><driver intremap='on' caching_mode='on'/></iommu>`
   and `<ioapic driver='qemu'/>`. QEMU's Intel model is emulated, so it works on
   the AMD host. `caching_mode` is what lets VFIO run behind it.
2. **Dedicated root port.** The GPU now sits on its own single-function root
   port: index 15, `00:10.0`, bus `0x0f`. This was done in case libvirt's
   default, which packs eight root ports into one multifunction slot
   (`00:01.x`), put the GPU in one IOMMU group with the boot disk. Observed
   afterwards: QEMU's root ports do isolate each other (each `00:01.x` port and
   the virtio disk have their own groups), so the default layout would likely
   have worked too. The dedicated port is harmless.

3. **Audio function detached.** The device operator only binds display-class
   devices, so the audio function (`c6:00.1`, `10de:22be`) would have stayed
   with the guest kernel. It would share the GPU's IOMMU group in L1, and if
   `snd_hda_intel` claimed it, the group would not be viable and the VM could
   not start.

On L0 both functions must still be bound to vfio-pci, because they share IOMMU
group 24 there. The `options vfio-pci ids=10de:28b0,10de:22be` binding from
Phase 0 covers that. If the host's `snd_hda_intel` ever claims `c6:00.1`, the
domain fails to start because group 24 is not viable. Only the GPU function is
passed to the guest. This is how it was detached, for a rebuilt domain:

```bash
cat > /tmp/gpu-audio.xml <<'EOF'
<hostdev mode='subsystem' type='pci' managed='yes'>
  <source>
    <address domain='0x0000' bus='0xc6' slot='0x00' function='0x1'/>
  </source>
</hostdev>
EOF
sudo virsh detach-device vmetal-xlarge-gpu-1 /tmp/gpu-audio.xml --config
sudo virsh dumpxml vmetal-xlarge-gpu-1 | grep -c '<hostdev'   # expect 1
```

The `multifunction='on'` left on the GPU's guest address is harmless.

### Always

L0's vfio-pci binding must survive reboots, and the DEG2 dock must be powered
on before the host boots. Both are covered in Phase 0 of
`vmetal-sushy-demo/docs/gpu-passthrough-plan.md`.

## Rollout

The parent Application has `automated` commented out, so a push changes nothing
in the management cluster until it is synced. The GPU tenant template is under
`tenant-management/` and applies on push, but creating a tenant from it is the
last step.

### 1. Sync `private-node-kubevirt-provider-config`

This applies three things at once: the static pool, the two NodeTypes, and the
devices Application.

The template change re-renders the provider cluster's vCluster Helm values,
which **restarts its control plane**. VMs keep running; their NodeClaims and
the KubeVirt API pause briefly. It is a non-parameter change, so it reaches the
existing instance without a template sync.

### 2. The GPU host joins the provider cluster

```bash
# management cluster
kubectl get nodeclaims -A | grep -i gpu
# spark-cluster
kubectl -n metal3-system get bmh rack-c-u12-xlarge-gpu   # provisioning -> provisioned

# provider cluster
vcluster platform connect vcluster kubevirt-provider-cluster --project platform
kubectl get nodes -l demo.vcluster.com/gpu-host=true
```

### 3. Check the IOMMU inside L1, before anything else

Run it detached and read the logs: with `-it`, a command this short finishes
before kubectl attaches and the output is lost.

```bash
NODE=$(kubectl get nodes -l demo.vcluster.com/gpu-host=true -o name | head -1)
kubectl debug "$NODE" --image=ubuntu:24.04 --profile=sysadmin -- chroot /host bash -c '
  echo "groups: $(ls /sys/kernel/iommu_groups | wc -l)"
  dmesg | grep -iE "DMAR|iommu" | head -5
  lspci -nnk -d 10de:
  for g in /sys/kernel/iommu_groups/*; do
    for d in "$g"/devices/*; do echo "group ${g##*/}: $(lspci -nns "${d##*/}")"; done
  done | grep -B2 -A2 10de'
sleep 15
kubectl logs "$(kubectl get pods --sort-by=.metadata.creationTimestamp -o name | grep node-debugger | tail -1)"
```

Observed 2026-10-10: 25 groups and a `DMAR` table with no `intel_iommu=on`
on the command line (the noble kernel enables the Intel IOMMU by default), and
`0f:00.0` alone in group 24, already on `vfio-pci`.

- **Zero groups:** the guest kernel did not enable the Intel vIOMMU. Add
  `intel_iommu=on iommu=pt` to the node's OS image, either
  `ubuntu-noble-bootstrap` itself (harmless on nodes without an IOMMU) or a
  derived image pinned in the `gpu-host` pool. Do **not** set `spec.iommu` on
  the HostDevice instead. The agent picks the flag from the CPU vendor in
  `/proc/cpuinfo`, and this guest reports `AuthenticAMD`, so it writes
  `amd_iommu=on`, reboots once, and still has no IOMMU.
- **The GPU's group** should hold only its root port `00:10.0` and `0f:00.0`
  (`10de:28b0`). Anything else in it, such as a virtio disk, blocks vfio.

### 4. The HostDevice reports Ready

```bash
kubectl get hostdevice rtx-2000-ada                 # Selected 1, Ready 1
kubectl get hostdevice rtx-2000-ada -o jsonpath='{.status.nodes[0].phase}{"\n"}{.status.nodes[0].message}{"\n"}'
kubectl get kubevirt -A -o jsonpath='{.items[0].spec.configuration.permittedHostDevices}{"\n"}'
kubectl get "$NODE" -o jsonpath='{.status.allocatable.nvidia\.com/RTX_2000_Ada}{"\n"}'   # expect 1
```

If the Argo CD Application stays silent on a freshly rebuilt provider cluster,
check for the `vcluster-gitops-watcher` pause
(`argocd.argoproj.io/skip-reconcile` on the cluster secret) before anything
else.

### 5. KubeVirt feature gates

Depending on the KubeVirt version Platform deploys, `domain.devices.gpus` may
still need the `GPU` and `HostDevices` feature gates. Check the version:

```bash
kubectl get kubevirt -A -o jsonpath='{.items[0].status.observedKubeVirtVersion}{"\n"}'
```

If VM creation in step 6 is rejected for a disabled feature gate, add them to the
HostDevice rather than to the KubeVirt Helm values (the operator owns those
fields, and Helm and the operator would overwrite each other):

```yaml
  kubevirt:
    resourceName: nvidia.com/RTX_2000_Ada
    featureGates: ["GPU", "HostDevices"]
```

### 6. Create the GPU tenant cluster

Only now, from `private-nodes-kubevirt-static-gpu-stack`. If the GPU is not yet
advertised, the VM cannot schedule and the NodeClaim eventually fails as
`InsufficientCapacity`.

```bash
# provider cluster: the VM lands on the gpu-host node and starts
kubectl get vmi -A -o wide
# tenant cluster
kubectl get nodes -L workload.example.com/pool
```

The `demo-gpu` Stack's `gpuready` task passes once the scheduler advertises
`nvidia.com/gpu`. For the final check, the GPU Operator's validator logs, or
any pod running `nvidia-smi`, should report `NVIDIA RTX 2000 Ada Generation`.

## Sizing

| Layer | Memory | Note |
| --- | --- | --- |
| L1 `rack-c-u12-xlarge-gpu` | 16 GiB, 8 CPU | The BMH reports `ramMebibytes: 16384`; the 24 GiB in the plan doc's budget is stale. All of it is pinned on L0 while it runs. |
| L2 GPU VM | 10 GiB, 6 CPU | Leaves room for L1's kubelet, KubeVirt, the agent, and VFIO overhead |
| L2 CPU service VM | 4 GiB, 2 CPU | Runs on the provider's `large` hosts (4 CPU, 8 GiB). Raise it if NVSentinel does not fit. |

Every non-GPU NodeType (`kvm-small`, `kvm-small-reserved`, `kvm-medium`)
requires `demo.vcluster.com/gpu-host` to be absent, and the GPU NodeType
requires it. Without that, the first run on 2026-10-10 left the GPU VM
`ErrorUnschedulable` with `Insufficient cpu` and `Insufficient memory` on the
GPU host itself. The likely cause: the GPU tenant's own `kvm-medium` VM was
created at the same moment and landed on the emptiest node, the GPU host.
Platform keeps
affinity terms from the VM template and appends its placement expressions to
each, so the rule survives. It is affinity rather than a taint because a taint
would also need tolerations on KubeVirt's `virt-handler`.

Budget the GPU VM's memory request plus KubeVirt's overhead, which includes an
extra 1 GiB whenever a VFIO device is attached.

## Measured inside the L2 VM

Run 2026-10-10 on tenant cluster `gpu-vm-tenant`, from inside the VM that holds
the card (two layers of passthrough below it).

| What | Result | How |
| --- | --- | --- |
| Card and driver | RTX 2000 Ada Generation, driver 580.126.20, CUDA 13.0 | `nvidia-smi` in the driver container |
| VRAM | 16,380 MiB | `nvidia-smi` |
| BAR1 | 16,384 MiB, the full VRAM | `nvidia-smi -q`. Resizable BAR survives both layers, which is what the UEFI firmware on the NodeType is for |
| PCIe link | x4 of the card's x8, the OCuLink dock's width. Reports Gen 1 at idle (`P8` downshifts the link) | `nvidia-smi -q` |
| Host to GPU | 6.69 GB/s | `dcgmi diag -r pcie` |
| GPU to host | 6.59 GB/s | same |
| Bidirectional | 11.27 GB/s | same |
| Latency | 1.71 µs host to GPU, 1.64 µs GPU to host | same |
| FP32 compute | 6,926 GFLOP/s, about 58% of the ~12 TFLOPS peak | CUDA n-body sample, `-benchmark -numbodies=262144` |
| Stress | `targeted_stress` Pass | `dcgmi diag -r targeted_stress` |

PCIe Gen4 x4 is about 7.9 GB/s raw, so 6.6 to 6.7 GB/s each way is what such a
link delivers in practice. The two passthrough layers add no measurable
bandwidth cost. n-body is not a peak-FLOPS test (its inner loop mixes square
roots and divides with the multiply-adds), so about 58% of peak is a normal
ratio rather than a passthrough tax. NVIDIA also notes that its samples are not
calibrated benchmarks.

Reproduce, against the tenant cluster:

```bash
kubectl -n gpu-operator exec ds/nvidia-driver-daemonset -c nvidia-driver-ctr -- nvidia-smi
kubectl -n gpu-operator exec ds/nvidia-dcgm -c nvidia-dcgm-ctr -- dcgmi diag -r pcie -v
```

For n-body, run `nvcr.io/nvidia/k8s/cuda-sample:nbody-cuda11.7.1` with args
`nbody -gpu -benchmark -numbodies=262144`, requesting `nvidia.com/gpu: 1`, with
`runtimeClassName: nvidia`, the `nvidia.com/gpu` toleration, and a
`workload.example.com/pool: gpu-compute` node selector. There is no CUDA 12
n-body tag in that repository, but the 11.7 build runs on this card (compute
capability 8.9). It only mislabels the architecture as "Ampere" in its output.

## Networking: bridge binding, not masquerade

The first run on 2026-10-10 registered both VMs with INTERNAL-IP `10.0.2.2`.
That is KubeVirt's masquerade guest address, and every masquerade VM gets it.
With two nodes on the same IP:

- konnectivity (`kubectl logs`, `exec`) dials `10.0.2.2` from the agent's VM
  and reaches that VM's own kubelet, never the other one;
- flannel addresses nodes by IP, so pod traffic between the VMs fails, which
  cuts GPU-node pods off from CoreDNS (the driver container's downloads) and
  NVSentinel off from DCGM;
- the node-to-node VPN does not help: it advertises each node's own IP as a
  `/32` route, and both are `10.0.2.2`.

So the provider's base VM template uses `bridge: {}` on the pod network, and
every NodeType inherits it. The guest takes its virt-launcher pod's IP from
KubeVirt's DHCP, unique and routable across the provider cluster. A NodeType
that sets its own `interfaces` list replaces the base one (lists are replaced,
not merged), so a new NodeType should leave `interfaces` alone. The trade-off:
bridge binding on the pod network rules out live migration, which the GPU VM
could not do anyway, and which the CPU VMs' `local-path` root disks already
prevented.

That puts tenant nodes on the provider's pod network, so every tenant template
using this provider must keep its CIDRs off the provider's (`10.64.0.0/16`
pods, `10.128.0.0/16` services). Both `private-nodes-kubevirt-static-gpu-stack`
and `private-nodes-kubevirt-dynamic` use `10.244.0.0/16` and `10.96.0.0/16`.
CIDRs cannot change on a running tenant cluster, so an existing tenant takes
the change by being recreated. On 2026-10-10 no tenant used the dynamic
template, so nothing needed recreating.

Verified 2026-10-10 on tenant cluster `gpu-vm-tenant`: the GPU VM registered
`10.64.1.23` and the CPU VM `10.64.4.10`, both `Ready`.

## Known gaps

- **KubeVirt feature gates.** None were needed on KubeVirt 1.8.2: the GPU VM
  was admitted with an empty gate list. Step 5 still applies to older
  KubeVirt versions.
- **No bare-metal baseline.** The measurements below have nothing on this card
  without the two passthrough layers to compare against. That would take
  provisioning `rack-c-u12-xlarge-gpu` directly to a Metal3 GPU tenant and
  rerunning them.
- **Per-claim template merge.** Use the NodeType's `mergeVirtualMachineTemplate`.
  The `kubevirt.vcluster.com/merge-virtual-machine-template` property path
  still reads the wrong property key (ENGCP-1412).
- **A better home for the HostDevice.** `kubevirt-provider-controller` already
  sequences the tenant cluster, the NodeProvider, and `Available`, so it could
  own a `spec.hostDevices` field and apply HostDevices once the CRD exists.

## Rollback

1. Delete the GPU tenant cluster.
2. Delete `provider-cluster/hostdevice-rtx-2000-ada.yaml` and push. The devices
   Application prunes the HostDevice, and its finalizer removes the KubeVirt CR
   entries. Do this first: the Application carries no resources finalizer, so
   deleting it would leave the HostDevice behind.
3. Remove the `gpu-host` pool, the two NodeTypes, and the devices Application,
   then sync the parent Application with prune.
4. The BMH deprovisions back to `available` and powered off. The Metal3 GPU
   template can claim it again.

The vIOMMU and the dedicated root port can stay on the domain. The Metal3
path does not need them, and they have not been tested with it.
