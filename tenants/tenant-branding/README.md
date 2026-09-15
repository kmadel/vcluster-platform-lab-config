# Declarative tenant branding under Argo CD

Manages vCluster Platform per-tenant UI branding from git, without writing the
backing Secret directly and without giving up validation, RBAC, or drift correction.

## Why it is shaped this way

`TenantConfig` is a subresource-request type on `Tenant`, not a CRD:

```
tenants/config | kind=TenantConfig | verbs= get,create
```

No collection, no `update`, no `patch`, no `list`, no `watch`. Argo CD reconciles by
diffing a declared manifest against `GET /apis/<group>/<version>/<resource>/<name>`,
so there is nothing for it to diff or apply here. A write is a subresource POST that
**replaces the entire TenantConfig**, so it also has to be read-merge-post.

The split below keeps everything Argo *can* do:

| Piece | Managed by | Diffable in git |
|---|---|---|
| Desired branding (`ConfigMap/tenant-branding`) | Argo CD, with `selfHeal` | yes |
| Applied `TenantConfig` | the reconciler Job/CronJob | no (API has no diff surface) |

You review branding changes as a ConfigMap diff in a PR. The Job projects that
ConfigMap onto the subresource. The CronJob closes the drift window that Argo
cannot see into.

## Files

| File | What it is |
|---|---|
| `00-rbac.yaml` | ServiceAccount + ClusterRole/Binding granting `get`+`create` on `tenants/config`, scoped by `resourceNames` |
| `10-branding.yaml` | **The file you edit.** One `<tenant>.json` (uiSettings) and optional `<tenant>.svg` per tenant |
| `20-reconciler.yaml` | The read-merge-post reconciler, stdlib Python, as a ConfigMap |
| `30-sync-job.yaml` | PostSync hook: applies branding on every sync, fails the sync if a tenant errors |
| `40-drift-cronjob.yaml` | Every 15 min: re-reconciles, correcting drift made outside git |
| `argocd-application.yaml` | Application wiring, pointed at the control plane cluster |
| `reconcile.py` | Source of truth for the script embedded in `20-reconciler.yaml` |

## How the reconciler reaches the API

`v1.management.loft.sh` is an aggregated APIService in the control plane cluster
(`vcluster-platform/loft-apiservice`), so the subresource is reachable at the normal
in-cluster endpoint with the pod's ServiceAccount token. No platform access key, no
`vcluster` CLI, no kubeconfig Secret to manage.

The platform's authorizer resolves ordinary `rbac.authorization.k8s.io` ClusterRoles
and ClusterRoleBindings from that cluster and matches ServiceAccount subjects, which
is why `00-rbac.yaml` is plain Kubernetes RBAC rather than a platform User or Team.

## What it deliberately does not touch

Only `.spec.uiSettings` is written. `.spec.hostnames` and `.spec.authentication` are
read from the live object and posted back unchanged. That is not just politeness:

- A write replaces the whole config, so posting a bare `uiSettings` would wipe both.
- Changing `hostnames` requires `update` on `tenants` on top of `create` on
  `tenants/config`, because a hostname is an exclusive platform-wide routing claim.
  Resubmitting them unchanged is a no-op, so this reconciler stays inside the
  narrower grant.

If you ever want hostnames in git too, that is a separate decision with a wider RBAC
grant and real collision semantics behind it.

## Adapting it

1. Point `argocd-application.yaml` at your repo and path.
2. Confirm the platform namespace. These manifests assume `vcluster-platform`.
3. Add a tenant: add `<tenant>.json` (+ optional `<tenant>.svg`) to `10-branding.yaml`
   **and** add the name to `resourceNames` in `00-rbac.yaml`. Forgetting the second
   gives a `403` naming the tenant in the Job log.
4. Air-gapped: replace `python:3.12-alpine` in both `30-` and `40-` with a mirrored
   tag. The script needs nothing but the standard library.

## Operating it

```sh
# what the last sync did
kubectl -n vcluster-platform logs job/tenant-branding-sync

# what drift correction has been doing
kubectl -n vcluster-platform logs -l job-name --tail=-1 --prefix

# what the platform actually holds for a tenant
kubectl get --raw /apis/management.loft.sh/v1/tenants/acme/config | jq .spec.uiSettings
```

Per-tenant log lines are `unchanged` or `updated`. A steady state of all-`unchanged`
is the closest thing this API allows to an Argo `Synced` badge.

Note on that last command: `kubectl get --raw` ignores the path prefix in the
kubeconfig server URL. Against a platform kubeconfig
(`https://<host>/kubernetes/management`) you must prepend it:
`/kubernetes/management/apis/...`. From inside the control plane cluster, the
unprefixed path is correct, which is what the reconciler uses.

## Verified

- All six manifests pass `kubectl apply --dry-run=server` against a live control
  plane cluster.
- The embedded script round-trips out of the ConfigMap and compiles.
- The merge was tested against a live object carrying `hostnames` and
  `authentication`: both survive, the logo is injected, and a converged tenant
  produces no write.
- Not tested end to end against a real tenant: the lab platform has no Tenant
  objects. The first real run is the remaining unknown.
