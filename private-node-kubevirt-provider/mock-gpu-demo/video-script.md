# Demo video: GPU tenant clusters on simulated GPUs, with failed GPUs replaced automatically

**Audience:** vCluster Labs colleagues (customer engineering, sales engineering, product).
**Length:** about 7 minutes finished. Record each scene as its own take and speed up the waits.
**Recorded from:** the vcp.lab.kurtmadel.com lab, project **VM Tenants**:

| Tenant cluster | Template | GPUs |
| --- | --- | --- |
| `tenant-a-gpu` | Private Nodes - KubeVirt VMs with Simulated GPUs, NVSentinel and Dynamic GPU Nodes (`gpuVMCount: 2`, `spareGPUs: 1`) | 2, launched on demand by its workload, plus 1 replacement when needed |
| `tenant-b-gpu` | Private Nodes - KubeVirt VMs with Simulated GPUs (`gpuVMCount: 1`) | 1 |

The mock "four-GPU server" holds tenant A's two GPU VMs, tenant B's one, and one slot in reserve.

**The one-sentence story:** a GPU server shared by two tenants as one-GPU VMs, launched on demand
for each tenant's workload, with the full NVIDIA software stack and per-tenant GPU dashboards;
when a GPU fails, its node is quarantined, a replacement GPU VM comes out of reserve capacity,
the workload moves onto it, and the failed node is retired, all without anyone touching it.
None of it needs a real GPU.

**Say it once, early, and mean it:** the GPUs are simulated by NVIDIA's own nvml-mock. Everything
around them is the real thing: vCluster Platform, KubeVirt, Auto Nodes, the NVIDIA GPU Operator,
DCGM, NVSentinel. Nothing runs CUDA. Never let a viewer think B200s are in the lab.

---

## Before you record

Run through this once, right before the first take.

1. **Both clusters healthy.** In the Platform UI both Stacks are **Healthy**. On the VMs page,
   three `kvm-mock-gpu` VMs on `rack-a-u20-xlarge` (two tenant A, one tenant B), so one slot of
   four is free.
2. **Contexts.**
   `vcluster platform connect vcluster tenant-a-gpu --project vm-tenants` and the same for
   `tenant-b-gpu`. `demo.sh` defaults to tenant A's context name; if yours differs,
   `export CTX=<tenant A context>`.
3. **Tenant A is ready.** `./demo.sh status` shows two `gpu-compute` nodes and one
   `cpu-services` node, nothing cordoned, `ThermalMargin=False Nvlink=False` on both GPU nodes,
   and two `gpu-workload` pods Running, one per GPU node. The workload belongs to the cluster's
   Stack, so leave it running: deleting it scales the GPU nodes to zero, and they take about
   5 minutes to come back.
4. **Browser tabs, in order, zoom 110%, light theme:**
   1. Platform UI, project **VM Tenants**, **Clusters** list
   2. Platform UI, **Bare Metal > Machines** (Metal3 tab)
   3. Platform UI, **Bare Metal > VMs**
   4. Platform UI, `tenant-a-gpu` cluster page, **Stacks** tab
   5. Grafana **Tenant Cluster GPUs**: `https://vcp.lab.kurtmadel.com/grafana/d/vcluster-tenant-gpus?var-project=vm-tenants&from=now-15m&to=now&refresh=10s`
5. **Two terminals**, large font (18pt+), dark on light, in this folder:
   - left: commands
   - right: `./demo.sh watch <node>` during scene 6 (after `./demo.sh pick`)
6. **Turn off notifications.** Hide the bookmarks bar and any customer tabs.

---

## Scene by scene

Timings are for the finished cut. **⏩ SPEED UP** marks every wait longer than about 30
seconds: run it 4x to 10x (or cut it) with a small on-screen timer or a "sped up" caption.
The full list is in the speed-up map under Editing notes.

### 0. Cold open (0:00 to 0:20)

**Screen:** the scene 6 payoff, cut in from the end: the watch terminal showing the failed node
gone, a new GPU node in its place, and the workload pod running on it.

> "That GPU just overheated. Nobody touched anything: the node was pulled out of service, a
> replacement GPU came out of reserve capacity, the workload moved onto it, and the failed node
> was retired. Let me show you how it's built, and why there isn't a real GPU anywhere in it."

### 1. Why simulated GPUs (0:20 to 0:50)

**Screen:** Platform UI, Clusters list, `tenant-a-gpu` and `tenant-b-gpu` visible.

> "Customers want to see GPU tenant clusters: carving a GPU server up between tenants, watching
> utilization, and handling failures. Demo hardware is scarce, and B300s definitely aren't
> sitting in a lab. So the GPUs here are simulated with NVIDIA's own nvml-mock. Everything else
> is real: vCluster Platform, KubeVirt, the NVIDIA GPU Operator, DCGM and NVSentinel. Nothing
> here runs CUDA, it's for showing the platform."

### 2. One GPU server, two tenants, one GPU in reserve (0:50 to 2:00)

**Screen:** Machines tab: `rack-a-u20-xlarge` (Provisioned, the mock "GPU server"). Then the
VMs page: three 1 CPU / 3 GiB GPU VMs on that host, two `tenant-a-gpu-*` and one
`tenant-b-gpu-*`, and tenant A's 2 CPU / 4 GiB service VM on a different host.

> "This bare metal host was provisioned by Metal3 and joined a KubeVirt cluster that vCluster
> Platform manages. Think of it as a four-GPU server. Two tenants share it: tenant A has two GPU
> VMs, tenant B has one, and each VM is a node in that tenant's own cluster. Tenant A's GPU VMs
> weren't requested by an admin: its workload asked for two GPUs, and vCluster's Auto Nodes
> launched a GPU VM for each. The fourth GPU isn't anyone's. It's reserve capacity, and you'll
> see what it's for in a minute."

**Optional insert:** creating a cluster from the template in the UI. Show the parameters:
**GPU VMs: 2**, **Spare GPU VMs: 1**, **Simulated GPU model: b200**, click Create, then
**⏩ CUT**: bring-up takes about 12 minutes. If you want to show it coming up, a few seconds of
the cluster page at 30x or more, ending on the Stack going green.

### 3. What each tenant sees (2:00 to 3:00)

**Screen:** left terminal.

```sh
./demo.sh status
./demo.sh smi
CTX=<tenant B context> ./demo.sh status
```

> "From inside tenant A's cluster these are ordinary Kubernetes nodes, each with a GPU: one
> `nvidia.com/gpu` per node, labelled B200 by NVIDIA's feature discovery, and the tenant's
> workload running on both. Here's `nvidia-smi` on one of them: driver 580, 180 gigabytes of
> memory, power and utilization moving. That's the real NVIDIA device plugin and nvidia-smi,
> reading a mock driver. And tenant B, on the same physical server, sees exactly one GPU node,
> its own, and nothing of tenant A."

### 4. Delivered by a Stack (3:00 to 3:40)

**Screen:** `tenant-a-gpu`'s **Stacks** tab: the task graph, all green.

> "None of that was installed by hand. The cluster template attaches a Stack. Its first task is
> the workload itself, and its GPU requests are what made Auto Nodes launch the GPU VMs. Then
> nvml-mock, the GPU Operator with standalone DCGM, two readiness gates, cert-manager and
> NVSentinel, in dependency order. Every tenant cluster from this template gets the same thing."

### 5. GPU observability per tenant cluster (3:40 to 4:30)

**Screen:** Grafana **Tenant Cluster GPUs**. Show the inventory table with both tenants, then
switch the Tenant Cluster selector to `tenant-a-gpu`, then `tenant-b-gpu`.

> "Fleet Observability picks up DCGM from every tenant cluster, labelled by project and tenant
> cluster. Across both tenants there are three GPUs. Pick tenant A and you see its two:
> utilization, tensor activity, power, temperature, health counters, per GPU. Pick tenant B and
> you see its one. It only uses metrics that behave on both real hardware and the mock, so it
> works the same on a real B200."

Set the selector back to `tenant-a-gpu` and leave the tab where you can get to it in one click.

### 6. A GPU fails and is replaced (4:30 to 6:30)

**Screen:** both terminals. Right: `./demo.sh watch <node>`.

```sh
./demo.sh pick              # a node running a workload pod
./demo.sh watch <node>      # right terminal
./demo.sh heat <node>       # left terminal
```

> "Tenant A's workload is using both its GPUs, so neither is free. Now I'll make one of them
> overheat: nvml-mock lets you inject faults, and I'm pushing GPU zero past its thermal slowdown
> limit."

**Wait for it (about 14 seconds, real speed):** `GpuThermalMarginWatch=True`, `cordoned=true`,
a workload pod Pending.

> "NVSentinel caught it: the thermal margin went negative. It quarantined the node and drained
> it. The workload's pod has nowhere to go: the tenant's other GPU is busy, and tenant B's GPU
> isn't tenant A's to take."

**Wait (about 5 minutes in total).** Keep the watch terminal on screen and pace it in three
stretches (times from the heat command, 2026-10-18 run):

- **0:14 to 2:00, ⏩ SPEED UP 10x.** The drain finishes (the failed node's pod disappears) and the
  replacement VM boots. A new GPU node appears in the watch at about 2:00 with no GPU yet. Slow
  to real speed for a moment there.
- **2:00 to 3:40, ⏩ SPEED UP 4x.** The interesting stretch: the failed node gets the
  `karpenter.sh/disrupted` taint (about 2:45) and disappears (about 3:40). Cut to the VMs page
  partway: briefly four `kvm-mock-gpu` VMs, then three again, with the failed one gone.
- **3:40 to 5:15, ⏩ SPEED UP 10x.** The replacement's GPU stack starts; its GPU shows `1`
  (about 4:15) and the workload pod goes Running (about 5:15). Slow to real speed for the moment
  it turns Running.

> "So Auto Nodes launched a replacement: a new GPU VM out of the reserve slot, joined to tenant
> A's cluster with its GPU stack. And the failed node, empty and quarantined, was retired: its
> VM deleted and its slot handed back. The workload is running on the replacement. Detect,
> quarantine, replace, retire, and nobody touched it. On the dashboard, there's the
> temperature spike on the GPU that failed, and the new GPU that took over."

**Screen:** cut to the Grafana tab: inventory table and temperature panel. The dashboard lags
up to a minute behind the node, so record this shot at the end of the take, or a minute after
the heat command, rather than waiting on camera. The spike stays on the graph, so a later shot
shows it fine.

### 7. How to get it (6:30 to 7:00)

**Screen:** the repo page or `mock-gpu.md` open at "Run it in your own environment".

> "Everything is in the lab's GitOps repo, including a section on running it in your own
> environment. The fastest path doesn't need KubeVirt at all: nvml-mock runs on any vCluster
> private node, so you can add the Stack to a tenant cluster you already have."

### 8. Close (7:00 to 7:20)

**Screen:** back to the Fleet Observability page.

> "So: a GPU server shared between tenants as GPU VMs, launched on demand, the full NVIDIA
> stack, per-tenant GPU observability, and failed GPUs replaced end to end, all demonstrable
> without a single GPU in the lab. Remember to call the GPUs simulated when you show it."

---

## Editing notes

- **Speed-up map** (everything over about 30 seconds):

  | Where | Real duration | Treatment |
  | --- | --- | --- |
  | Scene 2 optional insert: cluster bring-up | about 12 min | ⏩ CUT, or 30x+ for a few seconds |
  | Scene 6: drain and replacement VM boot (0:14 to 2:00) | about 1 min 45 s | ⏩ 10x |
  | Scene 6: failed node retired (2:00 to 3:40) | about 1 min 40 s | ⏩ 4x, it's the point being shown |
  | Scene 6: replacement GPU stack until the pod runs (3:40 to 5:15) | about 1 min 35 s | ⏩ 10x |
  | Scene 6: dashboard catching up to the spike | up to 1 min | don't wait on camera; shoot the panel later |

  Keep at real speed: the fault detection (about 14 s), and the moments the replacement node
  appears, the failed node disappears, and the pod turns Running. How fast it reacts is the point.
- **Captions** worth adding: "Simulated GPUs (NVIDIA nvml-mock)" on every scene with GPU names
  on screen, "sped up" wherever you speed up, and a timer during scene 6.
- **Reference timings** (2026-10-18 run): cordon and drain at 14 s; replacement claimed at 25 s;
  replacement joined at 2:02; failed node marked for retirement at 2:46; its slot released at
  3:29 and the node gone at 3:40; workload Running on the replacement at 5:18.
- **Blur** nothing should need it, but check for customer names in browser tabs and the
  cluster list before export.
- The Grafana temperature tops out at **95 °C** even though the fault pins 142 °C: DCGM caps the
  reported value, and NVSentinel acts on the thermal margin. If asked, that's why.
- **If someone asks "isn't the failed GPU back in the pool?":** with nvml-mock it was never real.
  With real passthrough, keeping a bad GPU out of rotation is a device-health concern below
  Auto Nodes.

## If a take goes wrong

| Symptom | Fix |
| --- | --- |
| No replacement appears | The reserve slot must be free: the VMs page should show three `kvm-mock-gpu` VMs before the take, not four. |
| Pod reschedules without a new node | A GPU was free: both workload pods must be Running, one per GPU node, before heating. |
| Failed node isn't retired | It takes about 1.5 minutes after the drain. Longer: check Karpenter's decisions in the tenant control plane pod's logs (see `mock-gpu.md`). |
| `heat` errors on the pod name | The node has no nvml-mock pod; you picked the CPU node. Use `./demo.sh pick`. |
| `heat` says `read-only file system` | An old copy of `demo.sh`: it must exec into the `node-agent` container. |
| Stack shows a task Failed before recording | Usually a task still starting; it recovers on its own once its pods run. Retry it only if it stays Failed. |

**Between takes:** nothing to reset. After a take, tenant A again has two GPU nodes (one of them
the replacement) and the reserve slot is free again, so you can re-run scene 6 straight away,
picking a node from `./demo.sh pick`. The previous replacement's GPU is already at normal
temperature; `./demo.sh reset` cools any GPU you heated and left.
