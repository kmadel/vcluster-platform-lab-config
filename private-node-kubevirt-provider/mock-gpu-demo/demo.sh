#!/usr/bin/env bash
# Helper for recording the simulated-GPU + NVSentinel demo (see video-script.md).
# Every command runs against the tenant cluster in $CTX.
#
#   ./demo.sh status           nodes, GPUs, cordons, NVSentinel watches
#   ./demo.sh workload [N]     GPU workload, N replicas (default: one per static GPU node,
#                              so a drained pod needs a spare GPU node)
#   ./demo.sh smi [NODE]       nvidia-smi on a node (default: first GPU node)
#   ./demo.sh pick             a GPU node that is running a workload pod
#   ./demo.sh heat NODE        pin GPU 0 on NODE to 142 C (thermal fault)
#   ./demo.sh watch NODE       follow the cordon, condition and pod moves (Ctrl-C to stop)
#   ./demo.sh cool NODE        clear the fault; NVSentinel uncordons on its own
#   ./demo.sh reset            cool every GPU and delete the workload (pre-take cleanup)
set -euo pipefail

CTX="${CTX:-vcluster-platform_tenant-a-gpu_vm-tenants_vcluster-platform_spark-cluster}"
HOT_C="${HOT_C:-142}"
K=(kubectl --context "$CTX")

gpu_nodes() { "${K[@]}" get nodes -l workload.example.com/pool=gpu-compute -o name | sed 's|node/||'; }
mock_pod() { "${K[@]}" -n mokka get pod -o name --field-selector "spec.nodeName=$1"; }
# True when the workload is the Stack's (dynamic-GPU template): Argo CD owns it then.
argo_owned() { "${K[@]}" get deployment gpu-workload -n default -o jsonpath='{.metadata.annotations.argocd\.argoproj\.io/tracking-id}{.metadata.labels.app\.kubernetes\.io/instance}' 2>/dev/null | grep -q .; }
need_node() { [[ -n "${1:-}" ]] || { echo "usage: $0 $2 NODE   (try: $0 pick)" >&2; exit 1; }; }

case "${1:-status}" in
  status)
    "${K[@]}" get nodes -o custom-columns='NODE:.metadata.name,POOL:.metadata.labels.workload\.example\.com/pool,SPARE:.metadata.labels.demo\.vcluster\.com/spare-gpu,GPU:.status.allocatable.nvidia\.com/gpu,MODEL:.metadata.labels.nvidia\.com/gpu\.product,CORDONED:.spec.unschedulable'
    echo
    for n in $(gpu_nodes); do
      printf '%-24s ' "$n"
      "${K[@]}" get node "$n" -o jsonpath='{range .status.conditions[?(@.type=="GpuThermalMarginWatch")]}ThermalMargin={.status} {end}{range .status.conditions[?(@.type=="GpuNvlinkWatch")]}Nvlink={.status}{end}{"\n"}'
    done
    echo
    "${K[@]}" get pods -n default -l app=gpu-workload -o wide 2>/dev/null || true
    ;;
  workload)
    if argo_owned; then echo "gpu-workload is managed by the cluster's Stack (Argo CD); leaving it as is."; "${K[@]}" get pods -n default -l app=gpu-workload -o wide; exit 0; fi
    replicas="${2:-$("${K[@]}" get nodes -l 'workload.example.com/pool=gpu-compute,!demo.vcluster.com/spare-gpu' -o name | wc -l | tr -d ' ')}"
    # Tolerates the nvidia.com/gpu taint on spare GPU nodes, so a drained pod can land on one.
    "${K[@]}" apply -n default -f - <<YAML
apiVersion: apps/v1
kind: Deployment
metadata:
  name: gpu-workload
spec:
  replicas: ${replicas}
  selector:
    matchLabels: {app: gpu-workload}
  template:
    metadata:
      labels: {app: gpu-workload}
    spec:
      terminationGracePeriodSeconds: 5
      tolerations:
        - {key: nvidia.com/gpu, operator: Exists, effect: NoSchedule}
      containers:
        - name: work
          image: busybox:1.36
          command: [sleep, infinity]
          resources:
            limits: {nvidia.com/gpu: 1}
YAML
    "${K[@]}" rollout status deployment gpu-workload -n default --timeout=180s
    "${K[@]}" get pods -n default -l app=gpu-workload -o wide
    ;;
  smi)
    node="${2:-$(gpu_nodes | head -1)}"
    dp=$("${K[@]}" get pods -n gpu-operator -l app=nvidia-device-plugin-daemonset -o name --field-selector "spec.nodeName=$node")
    "${K[@]}" exec -n gpu-operator "$dp" -c nvidia-device-plugin -- nvidia-smi
    ;;
  pick)
    "${K[@]}" get pods -n default -l app=gpu-workload --field-selector status.phase=Running -o jsonpath='{.items[0].spec.nodeName}{"\n"}'
    ;;
  heat)
    need_node "${2:-}" heat
    "${K[@]}" -n mokka exec "$(mock_pod "$2")" -c node-agent -- nvml-mock-ctl temp --gpu 0 "$HOT_C"
    ;;
  watch)
    need_node "${2:-}" watch
    while true; do
      clear
      date +%H:%M:%S
      "${K[@]}" get node "$2" -o jsonpath='Node {.metadata.name}  cordoned={.spec.unschedulable}{"\n"}{range .status.conditions[?(@.type=="GpuThermalMarginWatch")]}GpuThermalMarginWatch={.status}  {.message}{end}{"\n"}' | cut -c1-160
      echo
      "${K[@]}" get pods -n default -l app=gpu-workload -o custom-columns='POD:.metadata.name,STATUS:.status.phase,NODE:.spec.nodeName'
      echo
      "${K[@]}" get nodes -l workload.example.com/pool=gpu-compute -o custom-columns='GPU NODE:.metadata.name,SPARE:.metadata.labels.demo\.vcluster\.com/spare-gpu,READY:.status.conditions[?(@.type=="Ready")].status,GPU:.status.allocatable.nvidia\.com/gpu,CORDONED:.spec.unschedulable'
      sleep 2
    done
    ;;
  cool)
    need_node "${2:-}" cool
    "${K[@]}" -n mokka exec "$(mock_pod "$2")" -c node-agent -- nvml-mock-ctl reset --gpu 0
    ;;
  reset)
    for n in $(gpu_nodes); do
      "${K[@]}" -n mokka exec "$(mock_pod "$n")" -c node-agent -- nvml-mock-ctl reset --gpu 0 >/dev/null && echo "cooled $n"
    done
    if argo_owned; then echo "gpu-workload is managed by the cluster's Stack; kept"; else
      "${K[@]}" delete deployment gpu-workload -n default --ignore-not-found
    fi
    ;;
  *)
    sed -n '2,14p' "$0"; exit 1
    ;;
esac
