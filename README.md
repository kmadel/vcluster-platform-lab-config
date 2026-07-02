# platform-config

GitOps configuration for the vCluster Platform management cluster and its
connected control-plane clusters.

Argo CD reconciles the cluster parameter sheets and reusable application
templates in this repository. The Fleet Binding Controller then reads those
cluster parameters and creates the corresponding vCluster Platform
`ArgoCDApplication` bindings.

## Repository layout

```text
bootstrap/  Argo CD app-of-apps and child Applications
baseline/   Shared ArgoCDApplicationTemplates and their local chart sources
clusters/   Cluster labels and annotations used as fleet parameters
```

The `root` Application recursively reconciles `bootstrap/`. Its child
Applications apply `baseline/` and `clusters/` to the vCluster Platform
management cluster.

## Prerequisites

- vCluster Platform and its CRDs are installed.
- Argo CD is installed in the `argocd` namespace.
- The `vcluster-platform` namespace and the Platform project namespace
  (`p-platform` by default) exist.
- Argo CD can read
  `https://forgejo.apps.lab.kurtmadel.com/vcluster-demos/platform-config.git`.

Before bootstrapping, verify the cluster name and fleet annotations in
[`clusters/vcp.yaml`](clusters/vcp.yaml) and
[`clusters/cp-blacksburg-dc1.yaml`](clusters/cp-blacksburg-dc1.yaml).

## Bootstrap

Push the initial configuration:

```sh
git push -u origin main
```

If the Argo CD Helm values already seed `Application/root`, no additional
bootstrap command is required. Otherwise, create it once:

```sh
kubectl apply -f bootstrap/root.yaml
```

Watch the Layer 1 Applications converge:

```sh
kubectl -n argocd get applications
```

Install the Fleet Binding Controller from the published OCI Helm chart:

```sh
helm upgrade --install fleet-bindings \
  oci://ghcr.io/loft-demos/vcluster-fleet-gitops/fleet-bindings \
  --version 0.1.0 \
  --namespace vcluster-platform \
  --create-namespace \
  --set projectNamespace=p-platform
```

Change `projectNamespace` if your vCluster Platform project uses a different
`p-<project>` namespace.

## Verify

```sh
kubectl -n argocd get applications
kubectl -n vcluster-platform get pods \
  -l app.kubernetes.io/name=fleet-binding-controller
kubectl -n p-platform get fleetprofiles.fleet.lab.kurtmadel.com
kubectl -n p-platform get argocdapplications.management.loft.sh
```

## Making changes

Edit the manifests, commit, and push to `main`. Argo CD automatically
reconciles bootstrap and baseline changes. The `fleet-clusters` Application
intentionally has pruning and self-healing disabled, so review and sync cluster
resource changes deliberately.

`bootstrap/snooze-platform-config.yaml` depends on the separate
`vcluster-demos/vcluster-snooze` Forgejo repository. Remove that Application or
recreate the repository if the snooze configuration is not currently available.
