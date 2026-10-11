# From the lab's RTX 2000 Ada to HGX B200 and B300

[gpu-passthrough.md](gpu-passthrough.md) passes one workstation GPU through two
layers of VMs, because the lab has one physical host. This document covers what
changes when the same Platform objects (KubeVirtProviderCluster, HostDevice,
NodeTypes, the GPU tenant template) run on real HGX B200 or B300 servers.

Scope: HGX B200 and HGX B300, the 8-GPU NVSwitch baseboards. GB200 and GB300
NVL72 are a different design (rack-scale NVLink domains, IMEX, ComputeDomains)
and are not covered here.

Confidence: the vCluster Platform and device operator behavior below comes from
reading their code. The hardware statements about B200 and B300 come from
NVIDIA's public material; anything marked **verify** has not been checked
against a real board.

## Topology side by side

```text
LAB (nested, one host)                     HGX B200 / B300 (one level)

vmetal  AMD, one RTX 2000 Ada              HGX server  8 GPUs + NVSwitch + NICs
 │ libvirt hostdev, emulated vIOMMU         │ Metal3 provisions it as a node of the
 ▼                                          │ KubeVirt provider cluster (bare metal)
L1 VM   provider cluster node               │ device operator: IOMMU, hugepages,
 │ device operator binds vfio-pci           │ NUMA, vfio-pci, NVLink partitions
 ▼                                          ▼
L2 VM   tenant private node                VM      tenant private node
        GPU Operator installs driver               1, 2, 4 or 8 GPUs (one partition)
                                                   GPU Operator installs driver
```

The lab's middle layer exists only because there is no second machine. On HGX
the provider cluster node **is** the server, so there is one VM boundary, a
real IOMMU, and no nested virtualization.

## At a glance

| | Lab | HGX B200 / B300 |
| --- | --- | --- |
| GPUs per host | 1, PCIe add-in card over OCuLink | 8 SXM modules on one baseboard |
| GPU interconnect | None | NVLink5 through NVSwitch: every GPU reaches every other GPU's memory unless the fabric is partitioned |
| IOMMU | Emulated Intel vIOMMU in an AMD guest | The real one (VT-d or AMD-Vi) |
| `spec.iommu` on the HostDevice | Must stay unset (the agent would write the AMD flag for an Intel vIOMMU) | Usable: the CPU vendor matches the IOMMU |
| PCIe topology work | Hand-placed root port so the GPU gets its own group | Real PCIe switches on the board, so ACS on the switch ports decides the groups |
| Companion functions | HDMI audio function, kept off the domain | No audio function. NVSwitches, NICs and NVMe share the PCIe switches instead |
| Host NVIDIA software | None: the L1 node must not load a driver | NVIDIA driver plus Fabric Manager for the NVSwitches, with the GPUs themselves on vfio-pci |
| KubeVirt device plugin | KubeVirt's own (`externalResourceProvider: false`) | The device operator's own, which hands out whole NVLink partitions (`externalResourceProvider: true`) |
| VM shapes | One: 1 GPU | One per partition size: 1, 2, 4 or 8 GPUs |
| VM memory | 10 GiB | Hundreds of GiB to TiB, which needs hugepages and NUMA placement |
| Tenant networking | Provider pod network, bridge binding (masquerade gave every VM the same node IP) | RDMA NICs (ConnectX-7 on B200, ConnectX-8 on B300) as SR-IOV VFs or whole PFs |
| Between tenants | Nothing beyond the vfio reset | vfio reset, partition reactivation, scrub, health check, quarantine |
| Inventory | sushy-tools, which cannot see the passed-through card | Real Redfish, which reports PCIe devices |

## What disappears

Everything the lab needed only because it is nested:

- the vIOMMU, `caching_mode`, and `ioapic driver='qemu'` on a libvirt domain;
- the hand-placed single-function root port;
- detaching the audio function;
- the "zero IOMMU groups, add `intel_iommu=on`" fallback;
- the 16 GiB memory ceiling of the L1 node, and pinned L1 memory on L0;
- the OCuLink dock power-on ordering.

## What is new

### NVLink partitioning is the isolation boundary

On a single PCIe card, vfio and the IOMMU are the whole isolation story. On HGX
they are not. Every GPU reaches every other GPU's memory over NVLink, whatever
VM each one is assigned to. Two tenants given one GPU each from the same board
can read each other's memory unless the NVSwitch fabric is partitioned.

The device operator handles this with `spec.nvlink`:

- Fabric Manager must run in **shared NVSwitch mode**. Set
  `NVFM_CONFIG_FABRIC_MODE=1` on an `NVIDIADriver` scoped to these nodes when the
  GPU Operator runs Fabric Manager. Use `setFabricMode: true` when the driver is
  installed on the host.
- `partitionSize` (1, 2, 4 or 8) activates every partition of that size. The
  operator's device plugin then allocates a partition as an all-or-nothing unit,
  so a VM must request exactly `partitionSize` GPUs.
- After a VM releases a partition, the operator deactivates and reactivates it
  before offering it again.
- `nvlink` requires `spec.kubevirt`. The `nvidiaGPUOperator` path cannot
  partition.

**Verify on B200/B300:**

- The operator was built against HGX H200 (NVLink4); its only test fixture is an
  H200 partition table. Its code notes that NVLink5 differs: the NVIDIA driver
  container resets the GPUs across a Fabric Manager restart, which NVLink4 does
  not. The extra deactivation it does for NVLink4 should be harmless on NVLink5,
  but nobody has run it there.
- Blackwell HGX brings the NVLink Subnet Manager (NVLSM) into the NVSwitch
  management stack alongside Fabric Manager. The operator detects and drives
  Fabric Manager only. It has no NVLSM handling.
- Confirm with NVIDIA's Fabric Manager guide, for the driver branch you plan to
  run, that shared NVSwitch mode is supported on the exact board.

### Protected PCIe

Protected PCIe is NVIDIA's multi-GPU confidential-computing mode: the whole
board goes to one confidential VM. Fabric Manager refuses shared NVSwitch mode
while it is on, and boards ship with it half applied (on for the NVSwitches, off
for the GPUs), a state `nvidia-smi` does not show. `clearProtectedPCIe: true`
lets the operator turn it off. That writes device firmware, so it needs the
NVIDIA modules unloaded and takes the fleet's single maintenance slot.

The consequence for offerings: **multi-GPU confidential VMs and per-partition
tenancy on one board are mutually exclusive.** Pick one per host.

### The host runs NVIDIA software, the guest still owns the GPU

In the lab, the L1 node must not load any NVIDIA driver. On HGX, the host needs
the driver and Fabric Manager for the NVSwitches, while the GPUs are bound to
vfio-pci and the guest's GPU Operator installs its own driver. The operator
works out where the host driver comes from (`status.nodes[].nvlink.driverProvider`):
the GPU Operator's driver container, or systemd on the host.

The node image also needs `python3` (NVIDIA's gpu-admin-tools) and `nsenter`.
For a host driver, it needs `systemctl` and `modprobe` as well.

### IOMMU groups follow the board's PCIe switches

There is no vIOMMU to arrange. Each GPU sits behind a PCIe switch on the
baseboard, shared with NICs and NVMe. ACS on the switch's downstream ports
decides whether a GPU gets its own IOMMU group. Never use the ACS override to
manufacture isolation: that hides the problem rather than fixing it.

**B300 specifically (verify):** ConnectX-8 SuperNICs sit on the baseboard, and
ConnectX-8 includes its own PCIe switch that the GPU attaches through. Expect
the GPU and its NIC to share more topology than on B200. Read the real layout
with `lspci -tv` and the group listing from `gpu-passthrough.md` before
designing shapes. The device operator's discovery only covers display-class
devices, so a NIC that must travel with its GPU is out of its scope.

There is also a tension to settle per offering. Isolation wants ACS on.
GPUDirect RDMA between a GPU and a NIC under the same switch wants peer traffic
to stay inside the switch. Qualify RDMA performance inside a VM before promising
it.

### NUMA, hugepages, and VM size

HGX servers are typically dual-socket, with the GPUs split across the two NUMA
nodes. The operator's `spec.numa` configures the kubelet with a static CPU
manager and the **single-numa-node** topology manager, and `spec.hugePages`
reserves 1 GiB pages at boot.

- Partitions of 1, 2 or 4 GPUs can each be NUMA-local. Check that the partition
  table's GPU sets line up with the sockets. The operator notes that Fabric
  Manager's physical GPU ids are not PCI enumeration order.
- **An 8-GPU VM spans both sockets, and single-numa-node admission will reject
  it.** Run 8-GPU shapes on hosts without `spec.numa`, or offer them as
  whole-server Metal3 allocations instead (below).
- VFIO pins all guest memory when the VM starts. For guests of hundreds of GiB
  that takes long enough to notice, and 1 GiB hugepages cut it down. In the VM
  spec: `domain.memory.hugepages.pageSize: 1Gi`,
  `domain.cpu.dedicatedCpuPlacement: true`, and
  `domain.cpu.numa.guestMappingPassthrough: {}`. With `spec.numa`, the operator
  enables the `CPUManager`, `NUMA` and `PCINUMAAwareTopology` gates.

### Large BARs

Datacenter GPUs expose a BAR1 sized to their HBM, which is hundreds of GiB per
GPU, multiplied by up to eight GPUs per VM. The VM's UEFI firmware has to place
all of it in its 64-bit MMIO window. **Verify** inside a multi-GPU guest that
`nvidia-smi -q -d MEMORY` reports the full BAR1 size for every GPU, rather than
trusting that the VM booted.

### Tenant networking

The lab's VMs use the pod network. Real tenants on B200/B300 need the RDMA
fabric, as SR-IOV VFs bound to vfio-pci or as whole PFs, with VLAN or segment
isolation, anti-spoofing, and NUMA locality to the GPUs. That is the SR-IOV
Network Operator's job, not the device operator's: SR-IOV was removed from the
operator, which keeps only the `Bridge` CRD for L2 attachments.

### Between one tenant and the next

The lab tolerates "vfio reset and reuse". A multi-tenant B200/B300 fleet cannot.
The operator covers the vfio reset and NVLink partition reactivation. It does
**not** scrub HBM, run health checks such as DCGM diagnostics, or quarantine a
failed device. Those are gates before calling the boundary safe for mutually
untrusted tenants.

### Capacity accounting in Platform

The KubeVirt provider's NodeType capacity is `maxCapacity` minus existing claims.
It never looks at the hosts. In the lab `maxCapacity: 1` is exact. On a fleet:

- set each shape's `maxCapacity` to the number of partitions of that size across
  the qualified hosts;
- remember that every shape on one board draws from the same 8 GPUs. Offering
  2-GPU and 4-GPU shapes on the same host means `partitionSize` has to change,
  which resets that host's GPUs;
- a claim the hosts cannot satisfy shows up as an unschedulable VM, then
  `InsufficientCapacity` after the provider's timeout.

Simplest: one partition size per host pool, with a NodeType per pool.

## What carries over unchanged

- **The Platform object chain.** A KubeVirtProviderCluster, with
  `deploy.vClusterDeviceOperator`, a HostDevice delivered by Argo CD, NodeTypes
  carrying `domain.devices.gpus`, and a tenant template with the `demo-gpu` Stack.
- **The resource name comes from the hardware.** It must be the same string in
  the HostDevice, the KubeVirt CR and the VM's `deviceName`. Discover the PCI
  ID: B200 variants already differ (`2901` documented for DGX B200, others seen),
  and B300's should be read from the board.
- **NodeType merges.** Use the NodeType's `mergeVirtualMachineTemplate`, not the
  per-claim merge property (ENGCP-1412).
- **Ordering.** Create GPU tenants only after the HostDevice reports Ready.
- **Ownership of the KubeVirt CR.** The operator owns `permittedHostDevices` and
  the feature gates; keep them out of the KubeVirt Helm values.
- **Driver in the guest.** GPU Operator inside the VM, exactly as in the lab.

## The HostDevice, side by side

Lab:

```yaml
spec:
  nodeSelector: { demo.vcluster.com/gpu-host: "true" }
  deviceSelector: { vendor: "10de", deviceIDs: ["28b0"] }
  kubevirt:
    resourceName: nvidia.com/RTX_2000_Ada
```

HGX B200 or B300, sketch only. Every value marked `<...>` comes from the host:

```yaml
spec:
  nodeSelector: { <qualified-hgx-label>: "true" }
  iommu:
    enabled: true              # correct on bare metal
  hugePages:
    pageSize: 1Gi
    count: <pages per node>
  numa:                        # omit on hosts that serve 8-GPU VMs
    reservedCPUs: "<cpuset>"
    memoryManager: true
  deviceSelector: { vendor: "10de", deviceIDs: ["<observed>"] }
  kubevirt:
    resourceName: nvidia.com/<observed>
  nvlink:
    partitionSize: 2           # one size per host pool
    clearProtectedPCIe: true   # only if multi-GPU CC is not offered on this host
```

And the matching NodeType merge for a 2-GPU shape:

```yaml
mergeVirtualMachineTemplate:
  spec:
    template:
      spec:
        domain:
          cpu:
            dedicatedCpuPlacement: true
            numa: { guestMappingPassthrough: {} }
          memory:
            hugepages: { pageSize: 1Gi }
          devices:
            gpus:              # exactly partitionSize entries
              - { name: gpu1, deviceName: nvidia.com/<observed> }
              - { name: gpu2, deviceName: nvidia.com/<observed> }
```

## The other option: whole-server allocation

The lab already runs the alternative: `private-nodes-metal3-static-gpu-stack`
gives the whole machine to one Metal3 tenant. On HGX that means:

- no KubeVirt, vfio, partitioning, or hugepage pinning;
- Fabric Manager in its default bare-metal mode, with the full 8-GPU NVLink
  domain;
- native performance, including GPU-to-NIC RDMA;
- reclaim by reimaging the server, plus firmware and health checks, through the
  vMetal machine lifecycle;
- multi-GPU confidential computing remains available.

The cost is granularity: the smallest tenant is eight GPUs.

| Tenant wants | Use |
| --- | --- |
| 8 GPUs, full NVLink, RDMA at native speed | Whole server via Metal3 |
| 1, 2 or 4 GPUs with NVLink inside the slice | KubeVirt VM, one NVLink partition per VM |
| Multi-GPU confidential computing | Whole server, Protected PCIe left on |
| Sharing inside one GPU | Neither: MIG inside a VM that owns a whole GPU, or vGPU (licensed, out of scope here) |

The rule of thumb: if a board cannot show clean IOMMU groups, deterministic
reset, and a supported partition model, offer it as a whole server.

## Before claiming per-GPU tenancy on B200 or B300

- [ ] PCI IDs and IOMMU groups read from the real board, and no ACS override
- [ ] Fabric Manager shared NVSwitch mode supported for this board and driver
      branch, and NVLSM's role understood
- [ ] Device operator partitioning run on NVLink5, including a VM release and
      partition reactivation
- [ ] Protected PCIe state read on GPUs and NVSwitches separately, and a
      decision made about confidential computing on this host
- [ ] Partition GPU sets checked against the NUMA layout, and 8-GPU shapes
      placed off single-numa-node hosts
- [ ] Full BAR1 visible to every GPU inside a multi-GPU VM
- [ ] RDMA NIC attachment and GPUDirect RDMA qualified inside a VM
      (B300: GPU and ConnectX-8 topology mapped first)
- [ ] Reset, scrub, health check and quarantine procedure run repeatedly
      between tenants
- [ ] NodeType `maxCapacity` matched to real partition counts per host pool
