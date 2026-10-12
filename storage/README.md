# Lab storage: Longhorn per cluster, NFS on vmetal

How the lab adds storage that outlives a single node: replicated block storage
(Longhorn) inside the loft and spark clusters, and an NFS server on vmetal for
everything that has to cross clusters. It is the shape real data centers use:
replicated storage inside each cluster, plus an off-cluster target for backups.

Status, 2026-10-11: step 1 done (NFS on vmetal, verified with a root write
from `kpi-c01`). Steps 2 to 4 not yet applied.

## Layout

```text
vmetal (192.168.50.61), dedicated Samsung 990 PRO 2 TB (nvme0n1) at /srv/nfs
 └ NFS exports
     ├─ /srv/nfs/longhorn-backups/loft   ← Longhorn backups, loft cluster
     ├─ /srv/nfs/longhorn-backups/spark  ← Longhorn backups, spark cluster
     └─ /srv/nfs/kubevirt-disks          ← VM root disks, kubevirt-provider-cluster

loft cluster (Pis)           Longhorn, 2 replicas on the Pis' NVMe
spark cluster                Longhorn, 2 replicas on vmetal + vmetal-w02
kubevirt-provider-cluster    csi-driver-nfs, StorageClass nfs-vm-disks
```

| Cluster | Everyday storage | Backups | Git |
| --- | --- | --- | --- |
| loft (Pis) | Longhorn, opt in with `storageClassName: longhorn`; `local-path` stays the default | NFS, `.../loft` | `baseline/longhorn.yaml`, profile and annotation in `clusters/loft-cluster.yaml` |
| spark | Longhorn the same way | NFS, `.../spark` | `baseline/longhorn.yaml`, profile and annotation in `clusters/spark-cluster.yaml` |
| kubevirt-provider-cluster | `nfs-vm-disks` for every new VM root disk | Snapshots on vmetal, if wanted | `private-node-kubevirt-provider/provider-cluster-storage-app.yaml`, the `image-datavolume-template` property in `kubevirt-provider.yaml` |

### How Longhorn is delivered

One `ArgoCDApplicationTemplate` (`baseline/longhorn.yaml`) serves every cluster,
through the fleet: the `storage-longhorn` FleetProfile in
`bootstrap/fleet-bindings.yaml` binds it to any Cluster whose
`fleet.lab.kurtmadel.com/profiles` annotation lists that profile. Each cluster's
backup target comes from its own `fleet.lab.kurtmadel.com/longhorn-backup-target`
annotation, the same way `metallb-config` reads each cluster's pool.

**Adding `storage-longhorn` to a cluster's profiles is the install step.** The
template syncs automatically, so add the profile only once that cluster's nodes
are prepared. Both annotations are already in place; neither cluster lists the
profile yet.

### Why vmetal's spare drive for NFS

vmetal has three NVMe drives: the Kingston 1 TB holds the OS (LVM root, with
about 350 GB still unallocated in `ubuntu-vg`), one 990 PRO is mounted at
`/mnt/vm-images`, and the other 990 PRO (`nvme0n1`) was unused by vmetal. It
held an old Raspberry Pi OS image from the retired worker `kpi-w51` (MBR,
`bootfs` and `rootfs` partitions) that an x86 host cannot boot. Putting NFS on
that drive:

- **adds no failure domain.** The provider cluster's node VMs already run on
  vmetal, so VM disks served from vmetal fail together with the VMs that use
  them, and with nothing else. On vmetal-w02 they would depend on two hosts.
- **avoids contention.** It is its own device, separate from the OS, the VM
  images, the OS image server and the registry cache.
- **keeps VM disk traffic on the host.** The provider node VMs reach
  `192.168.50.61` over vmetal's own `br-lan` bridge, not the physical network.

vmetal-w02 has a single 2 TB drive that also holds its OS; it stays the
fallback.

### Why not one Longhorn for everything

Longhorn serves only the cluster it runs in, so one installation cannot back
several clusters. NFS is the piece that crosses clusters.

That holds even though the KubeVirt VMs run on spark-cluster hardware.
`kubevirt-provider-cluster` is split across clusters:

| Part | Runs on | So its storage comes from |
| --- | --- | --- |
| Control plane (API server, embedded etcd, PVC `data-kubevirt-provider-cluster-0`) | the loft cluster (`virtualCluster.clusterRef.cluster: loft-cluster`) | the loft cluster: Longhorn, once that PVC moves |
| Worker nodes and their KubeVirt VMs | Metal3 machines, libvirt VMs on vmetal | its own cluster: `nfs-vm-disks` |

The provider nodes are nodes of `kubevirt-provider-cluster`, not of the spark
cluster, so the spark cluster's Longhorn cannot serve the VM disks even though
it shares the hardware.

### Why NFS, not Longhorn, for the KubeVirt VM disks

Longhorn inside the provider cluster would replicate across node VMs whose
disks all sit on vmetal, which protects against a node VM being re-imaged but
not against the hardware, and suggests more redundancy than exists. NFS on a
dedicated drive is plainly what it is, and it fixes the failure that motivated
this: on 2026-10-11 a reclaimed and re-imaged provider node brought a VM back
on a blank `local-path` disk (see
`private-node-kubevirt-provider/gpu-passthrough.md`). With the disk on NFS, the
VM restarts on any provider node with its disk intact.

## Step 1: NFS server on vmetal

Confirm `nvme0n1` is empty before formatting it. A drive can be in use with
nothing mounted, as an LVM physical volume or a ZFS member:

```bash
lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINT /dev/nvme0n1
sudo blkid /dev/nvme0n1*
sudo pvs
```

Checked 2026-10-11: it held only the `kpi-w51` Pi image (24 KB in `/home`,
6.1 GB of disposable k3s worker data), no LVM, and no `fstab` entry. Partition,
format, and mount it by UUID. `nofail` keeps vmetal booting if the drive ever
goes missing. Make sure the device is `nvme0n1`, not `nvme2n1`, the other 990
PRO behind `/mnt/vm-images`:

```bash
sudo parted -s /dev/nvme0n1 mklabel gpt mkpart nfs ext4 1MiB 100%
sudo mkfs.ext4 -L nfs /dev/nvme0n1p1
sudo mkdir -p /srv/nfs
echo "UUID=$(sudo blkid -s UUID -o value /dev/nvme0n1p1) /srv/nfs ext4 defaults,nofail 0 2" | sudo tee -a /etc/fstab
sudo systemctl daemon-reload
sudo mount /srv/nfs
df -h /srv/nfs
grep -c '/srv/nfs' /etc/fstab
```

`mkfs` discards the whole 2 TB first, which can take minutes; while it runs,
`parted` and `blkid` may print `Failed to wait for daemon to reply: Connection
timed out` (udev waiting on the busy device). That is harmless if `df` then
shows about 1.8 TB at `/srv/nfs` and `grep` prints `1`, a single `fstab` line
with a non-empty UUID. Done 2026-10-11 (UUID `fb3373e2-...`).

Export it:

```bash
sudo apt-get install -y nfs-kernel-server
sudo mkdir -p /srv/nfs/kubevirt-disks /srv/nfs/longhorn-backups/loft /srv/nfs/longhorn-backups/spark

# Ubuntu does not create /etc/exports.d; without it tee fails and nothing is
# exported (exportfs -v prints nothing).
sudo mkdir -p /etc/exports.d
sudo tee /etc/exports.d/lab.exports <<'EOF'
/srv/nfs/kubevirt-disks   192.168.50.0/24(rw,sync,no_subtree_check,no_root_squash)
/srv/nfs/longhorn-backups 192.168.50.0/24(rw,sync,no_subtree_check,no_root_squash)
EOF
sudo exportfs -ra
sudo exportfs -v
```

`no_root_squash` is required by both clients: csi-driver-nfs creates each
volume's directory as root, and Longhorn writes backups as root. That is why
the exports are limited to the lab LAN. If vmetal runs a firewall, allow TCP
2049 from `192.168.50.0/24`.

Check from any Pi before going further:

```bash
sudo apt-get install -y nfs-common
sudo mount -t nfs -o nfsvers=4.1 192.168.50.61:/srv/nfs/longhorn-backups /mnt
sudo touch /mnt/write-test && sudo rm /mnt/write-test && sudo umount /mnt && echo OK
```

Write as root (`sudo touch`): the export's directories belong to root, so a
plain user's write is refused with `Permission denied`, which is correct.
Longhorn and csi-driver-nfs write as root, which is what `no_root_squash` allows.

## Step 2: Longhorn on the loft cluster

### Prepare the Pis

Longhorn's v1 engine attaches volumes over iSCSI, and its NFS backup target
needs an NFS client. On **every** loft node, including any added later:

```bash
sudo apt-get install -y open-iscsi nfs-common
sudo systemctl enable --now iscsid
```

Label the nodes that hold replicas. Leave out `kpi4-w01`, the 256 GB node:

```bash
kubectl --context vcluster-platform_loft-cluster label node \
  kpi-c01 kpi-c03 kpi4-c02 node.longhorn.io/create-default-disk=true
```

Add the new Pi 5 to that list once it has joined. Boot it from its NVMe drive:
Longhorn keeps data under `/var/lib/longhorn` on the root filesystem, and that
should not be an SD card.

### Install

In `clusters/loft-cluster.yaml`, change the profiles annotation to
`"fleet-observability-platform,storage-longhorn"` and push. The fleet binds the
template, and the Application syncs on its own.

```bash
L=vcluster-platform_loft-cluster
kubectl --context $L -n longhorn-system get pods
kubectl --context $L -n longhorn-system get nodes.longhorn.io
kubectl --context $L -n longhorn-system get backuptargets.longhorn.io
```

Every pod Running, the labeled nodes showing a disk, and the `default` backup
target `Available`.

### UI

`infrastructure/longhorn-ui/longhorn-ui.yaml` publishes the loft cluster's UI
at `https://longhorn.apps.lab.kurtmadel.com` on the `lab` Gateway's
`apps-https` listener, behind Envoy Gateway basic auth. The Longhorn UI has no
login of its own and can delete volumes and backups, so create the
`longhorn-ui-basic-auth` Secret first (the file's header has the commands; it
stays out of Git). Platform-as-OIDC-provider single sign-on can replace basic
auth once it is enabled in the Platform config.

### What to move

Moving a volume is a separate decision per workload. A StatefulSet's volume
claim template cannot change in place, so each move is a backup and restore
into a new PVC.

| Volume | Move? |
| --- | --- |
| `data-kubevirt-provider-cluster-0` (provider control plane etcd) | **Yes, first.** Losing it loses the provider cluster's state: the VirtualMachine objects behind every tenant cluster's VM nodes, and the KubeVirt and HostDevice configuration. Running VMs keep running, but nothing can manage them |
| Forgejo `gitea-shared-storage` | Yes. It holds this repository |
| Registry caches, Prometheus | No: they refill on their own |

## Step 3: VM disks on NFS for the provider cluster

The provider nodes need no NFS packages: the csi-driver-nfs node plugin image
installs `nfs-common` itself and mounts from inside its container. That matters
because Metal3 re-images those nodes, so anything installed on them by hand
would not survive.

Only after step 1 checks out. Sync `private-node-kubevirt-provider-config`:
that creates the `kubevirt-provider-cluster-storage` Application and updates the
`KubeVirtProviderCluster` with the new disk property.

```bash
P=vcluster-platform_kubevirt-provider-cluster_platform_vcluster-platform_loft-cluster
kubectl --context $P get storageclass nfs-vm-disks
kubectl --context $P -n kube-system get pods | grep csi-nfs
```

Prove it before any VM depends on it, with a throwaway claim:

```bash
kubectl --context $P -n default apply -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: nfs-smoke}
spec:
  storageClassName: nfs-vm-disks
  accessModes: [ReadWriteMany]
  resources: {requests: {storage: 1Gi}}
EOF
kubectl --context $P -n default get pvc nfs-smoke
kubectl --context $P -n default delete pvc nfs-smoke
```

The claim should show `Bound`, and deleting it removes its directory on vmetal.

New VMs then get their root disk on `nfs-vm-disks`. Existing VMs keep
`local-path` until their claim is recreated: delete the VM's platform NodeClaim
when convenient and the pool re-creates it on NFS. The GPU VM's 60 GiB import
takes longer over NFS than it did locally.

## Step 4: Longhorn on the spark cluster

The spark cluster holds most of the lab's important state, and nine of its
twelve volumes sit on vmetal (2026-10-11). Replicas go on the two large x86
hosts, so each backs up the other.

### Prepare the nodes

On **every** spark node (vmetal, vmetal-w02, spark-w01, and the three Pis),
because the Longhorn manager runs everywhere and a pod on any node may attach a
volume:

```bash
sudo apt-get install -y open-iscsi nfs-common
sudo systemctl enable --now iscsid
```

On the Ubuntu hosts, check for `multipathd`. If it runs, stop it from claiming
the iSCSI disks Longhorn creates (Longhorn's documented fix), then restart it:

```bash
systemctl is-active multipathd
sudo tee -a /etc/multipath.conf <<'EOF'
blacklist {
    devnode "^sd[a-z0-9]+"
}
EOF
sudo systemctl restart multipathd
```

On vmetal, give Longhorn its own logical volume from the free space in
`ubuntu-vg`, so replicas neither fill the root filesystem nor touch the NFS
drive. It leaves about 50 GB free in the volume group:

```bash
sudo lvcreate -L 300G -n longhorn ubuntu-vg
sudo mkfs.ext4 -L longhorn /dev/ubuntu-vg/longhorn
sudo mkdir -p /var/lib/longhorn
echo '/dev/ubuntu-vg/longhorn /var/lib/longhorn ext4 defaults,nofail 0 2' | sudo tee -a /etc/fstab
sudo mount /var/lib/longhorn
```

vmetal-w02 uses its root filesystem (about 1.8 TB free). Label the two hosts:

```bash
kubectl --context vcluster-platform_spark-cluster label node \
  vmetal vmetal-w02 node.longhorn.io/create-default-disk=true
```

Leave out spark-w01 (the DGX Spark: GPU work, and `model-cache` already uses
600 GiB there) and kpi4-spark-c03 (220 GB). The 16 GB Pi 5s `kpi5-spark-c01`
and `kpi5-spark-c02` can be labeled later for a third replica location.

### Install on spark

In `clusters/spark-cluster.yaml`, change the profiles annotation to
`fleet-observability-collector,storage-longhorn` and push, then run the same
checks as step 2 with `--context vcluster-platform_spark-cluster`.

### What to move on spark

| Volume (node, 2026-10-11) | Move? |
| --- | --- |
| NetBox: `data-netbox-postgresql-0`, `netbox-media`, `valkey-data-...` (vmetal) | **Yes.** NetBox is the inventory Metal3 imports from. Check how NetBox is deployed and set its chart's storage class |
| `data-gpu-vm-tenant-0`, tenant control plane etcd (vmetal-w02) | **Yes**, through the template: `controlPlane.statefulSet.persistence.volumeClaim.storageClass: longhorn`. Replication only buys failover if the template also stops pinning the control plane to `kubernetes.io/hostname: vmetal-w02`; allow both vmetal and vmetal-w02 |
| `data-local-kvm-provider-0` (kpi5-spark-c01) | If that tenant cluster still matters |
| `open-webui` (vmetal) | Yes, small user data |
| Metal3 `data-mariadb-0`, `ironic-db-metal3-0` (vmetal) | Data protection only: Ironic must run on vmetal (it needs `br-provision`). These are deployed by the Metal3 node provider, and `data-mariadb-0` is a static PV (`mariadb-metal3-vmetal`, see the sushy demo's `mariadb-pv-migration.md`). Check what the provider lets you set before moving them |
| qdrant `data-qdrant-0` 200 Gi, `rag-repos` (vmetal) | Optional: the indexer can rebuild the index from the repos |
| `model-cache` 600 Gi (spark-w01), `qwen3-embed-cache` (vmetal) | No: re-downloadable, and too large to replicate for nothing |

## Caveats

- **vmetal is a single point of failure for VM disks**, as it already was for
  every provider node. While the NFS server is down, VM disk I/O blocks until
  it returns (NFS mounts are hard by default).
- **The spark cluster's backups live on vmetal**, next to one of its replicas.
  They cover deleted or corrupted volumes; losing vmetal itself is what the
  vmetal-w02 replica is for.
- **Live migration is still unavailable.** RWX storage is one requirement;
  the other is a migratable network binding. The VMs use bridge binding on the
  pod network, which KubeVirt reports as not live-migratable
  (`InterfaceNotLiveMigratable`). That changes only with the network plan
  (Kube-OVN), and is unverified there.
- **Throughput:** NFS is slower than a local NVMe path. Fine for VM root disks
  in this lab; not for etcd-heavy work inside the VMs.
- **`reclaimPolicy: Delete`:** a VM's disk goes with its NodeClaim. That is
  intended, since tenant nodes are disposable.
- **vmetal's load:** kernel NFS and a Longhorn replica set both add to the
  busiest host (pinned GPU guest memory, the Metal3 stack, spark workloads).

## Rollback

- Provider cluster: remove the `image-datavolume-template` property and sync.
  New VMs return to `local-path`; VMs already on NFS keep their disks until
  their claims are recreated. Remove the storage Application last, after no
  PVC uses `nfs-vm-disks`.
- Longhorn on either cluster: move its volumes off first, set Longhorn's
  `deleting-confirmation-flag` setting to `true` (its uninstall guard), then
  remove `storage-longhorn` from that cluster's profiles annotation.
