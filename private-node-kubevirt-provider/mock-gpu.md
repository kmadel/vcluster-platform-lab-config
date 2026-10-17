# Simulated GPU VMs (nvml-mock)

A mock "4-GPU server" carved into four 1-GPU KubeVirt VMs, each a node of one tenant
cluster. No physical GPU is involved: NVIDIA's nvml-mock simulates one GPU per node, so the
device plugin, `nvidia-smi`, GFD and dcgm-exporter all report a GPU, and the Fleet GPU
dashboards populate. Nothing CUDA runs. Present the GPU model as simulated.

**Status: written 2026-10-17, not yet run.** Every template renders and validates
(Go templates with sprig, vCluster 0.37.2/0.37.3 schema, nvml-mock 0.5.0 and GPU Operator
v26.3.3 `helm template`), but nothing has booted.

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

## Risks to watch on the first run

- **containerd restart.** nvml-mock's node agent makes the `nvidia` runtime containerd's
  default and restarts containerd through systemd. The vCluster join writes
  `/etc/containerd/config.toml` with no `imports`; nvidia-ctk is expected to add the
  `conf.d` import. If pods fail with an unknown runtime, check that file first.
- **Unmocked DCGM fields.** The exporter field list is the real stack's. Fields the mock
  doesn't implement (encoder, NVLink, vGPU license) should be skipped; if dcgm-exporter
  crash-loops instead, trim the list.
- **GFD machine type.** `GFD_MACHINE_TYPE_FILE` points at `/etc/nvml-mock/machine-type`
  as in NVIDIA's guide; if GFD labels look wrong, drop that env.
- **Capacity.** Four VMs plus the host's kubelet, KubeVirt and virt-launcher overhead come
  close to 16 GiB. If the fourth VM stays Pending, lower `kvm-mock-gpu` memory to 2.5Gi.
