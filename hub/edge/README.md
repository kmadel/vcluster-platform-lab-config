# hub/edge

The shared Envoy Gateway edge on the hub: `loft-cluster`, the control plane
cluster that runs vCluster Platform and Argo CD. The
[`hub-edge`](../../bootstrap/hub-edge.yaml) Application owns this directory and
[`hub-envoy-gateway`](../../bootstrap/hub-envoy-gateway.yaml) owns the controller
chart. Argo CD only reads `*.yaml` here, so this README is never applied.

This doc covers:

1. What runs today and why.
2. Option A: serving vCluster Platform from Envoy directly, and the commits that
   get there.
3. What any vCluster Platform install needs from its edge, and how that maps to
   other ingress setups.

## 1. Current setup

| File | What it is |
| --- | --- |
| `gatewayclass.yaml` | `eg` GatewayClass |
| `envoyproxy.yaml` | Envoy data plane: 2 replicas on control-plane nodes, MetalLB VIP `192.168.50.10` |
| `gateway.yaml` | `lab` Gateway: `*.lab`, `*.apps.lab`, and `*.tenant.lab` listeners, plus `vcp-tls` passthrough |
| `clienttrafficpolicy.yaml` | Keeps `%2F` in paths (Argo CD) |
| `certificates.yaml` | Let's Encrypt via DNS-01 for each wildcard, plus `vcp.lab` |
| `platform-routes.yaml` | `vcp.lab` port 80 redirect, plus the TLSRoute to nginx |
| `platform-tls-proxy.yaml` | nginx: terminates TLS for `vcp.lab`, forwards to `loft:80`, adds CORS |
| `fleet-observability-otlp-route.yaml` | `otel.lab` `POST /v1/metrics` to the observability connector |

```text
vcp.lab   :443 --SNI--> vcp-tls (Passthrough) --> TLSRoute --> nginx (TLS, CORS) --> loft:80
*.lab     :443 --SNI--> https (Terminate)     --> HTTPRoutes (otel.lab)
*.apps    :443 --SNI--> apps-https            --> Argo CD, Forgejo
*.tenant  :443 --SNI--> tenant-https          --> tenant cluster routes
```

### Why vCluster Platform goes through nginx today

Private nodes join tenant clusters through the Tailscale coordinator that
vCluster Platform embeds. It is served on the same `loftHost` as the UI and API.
To register, a node sends:

```http
POST /coordinator/ts2021 HTTP/1.1
Upgrade: tailscale-control-protocol
Connection: upgrade
```

The node expects `101 Switching Protocols` back. Envoy only accepts upgrade
types that are on its allowlist, and Envoy Gateway's default allowlist is just
`websocket`. Any other upgrade gets a local `403` with response details
`upgrade_failed`. nginx forwards any `Upgrade` header, so the setup sends
`vcp.lab` past Envoy's HTTP layer (TLS passthrough) and lets nginx do the HTTP
work.

That workaround costs:

- A TLSRoute (an experimental-channel API).
- An nginx Deployment.
- A second certificate.
- CORS configured in nginx.
- A cert that never reloads. nginx reads it only at startup, so every
  cert-manager renewal needs a pod restart. On 2026-10-07 nginx was 18 days
  from serving an expired cert.
- A 1 MiB request body limit, nginx's default.

## 2. Option A: Envoy-native

Envoy Gateway can add upgrade types itself through
`BackendTrafficPolicy.spec.httpUpgrade`. The hub runs v1.8.0, which has this
field (checked with `kubectl explain` on 2026-10-07). With it, `vcp.lab` is an
ordinary HTTPRoute on the `https` listener. The `*.lab` wildcard cert already
covers `vcp.lab`.

```text
vcp.lab :443 --SNI--> https (Terminate) --> HTTPRoute vcluster-platform-https --> loft:80
                                            + BackendTrafficPolicy (upgrade types)
                                            + SecurityPolicy (CORS)
```

### Target files

`platform-routes.yaml` keeps the port 80 redirect route. Add the HTTPS route and
remove the TLSRoute:

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: vcluster-platform-https
  namespace: vcluster-platform
spec:
  parentRefs:
    - name: lab
      namespace: envoy-gateway-system
      sectionName: https
  hostnames:
    - vcp.lab.kurtmadel.com
  rules:
    - backendRefs:
        - name: loft
          port: 80
      timeouts:
        # Envoy's default route timeout is 15s. Watches, log follows, and the
        # Tailscale map poll are long-lived streams.
        request: 0s
```

New `platform-upgrades.yaml`:

```yaml
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: BackendTrafficPolicy
metadata:
  name: vcluster-platform-upgrades
  namespace: vcluster-platform
spec:
  targetRefs:
    - group: gateway.networking.k8s.io
      kind: HTTPRoute
      name: vcluster-platform-https
  # Replaces the default list, so websocket must be listed too.
  # Do not add requestBuffer to this policy: when it is set, Envoy Gateway
  # ignores the upgrade configuration.
  httpUpgrade:
    - type: websocket                    # UI, newer kubectl exec/attach
    - type: spdy/3.1                     # older kubectl exec/port-forward
    - type: tailscale-control-protocol   # private nodes
```

New `platform-cors.yaml`. It's needed because the snooze/wake page on
`*.tenant.lab` calls the Platform API from the browser with an `Authorization`
header. vCluster Platform sets no CORS headers and strips any it gets from
backends, so the edge is the only place they can come from:

```yaml
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: SecurityPolicy
metadata:
  name: platform-cors
  namespace: vcluster-platform
spec:
  targetRefs:
    - group: gateway.networking.k8s.io
      kind: HTTPRoute
      name: vcluster-platform-https
  cors:
    allowOrigins:
      - "https://*.tenant.lab.kurtmadel.com"   # strings, one leading wildcard label
    allowMethods: [GET, OPTIONS]               # add POST if the page ever wakes
    allowHeaders: [Authorization, Content-Type]
    maxAge: 86400s
```

Envoy answers preflights from allowed origins itself, and they never reach
vCluster Platform. Don't set `allowCredentials`: the page sends a bearer token in
a header, not cookies. Attach the policy to this route only, not the Gateway.
A route-level SecurityPolicy replaces a Gateway-level one, and at Gateway level
it would also cover Argo CD and Forgejo.

### Commit sequence

Each step is its own commit. `hub-edge` syncs it automatically.

**A1. Test hostname (additive, no impact on `vcp.lab`).** Add a copy of the
HTTPRoute and the BackendTrafficPolicy for `vcp-test.lab.kurtmadel.com`. It's
covered by the wildcard, so no DNS or cert changes are needed. Then:

```sh
curl -sv --http1.1 -X POST \
  -H 'Upgrade: tailscale-control-protocol' -H 'Connection: upgrade' \
  https://vcp-test.lab.kurtmadel.com/coordinator/ts2021
```

If you get `403` and the Envoy access log shows `upgrade_failed`, Envoy is
still blocking the upgrade: stop here. Any response from vCluster Platform
(usually a 4xx about the missing handshake header) means the upgrade passed.

**A2. Cutover.** In one commit:

- Add `vcluster-platform-https`, `platform-upgrades.yaml`, and `platform-cors.yaml`.
- Remove the TLSRoute from `platform-routes.yaml`.
- Remove the `vcp-tls` listener from `gateway.yaml`.

While `vcp-tls` exists, its exact SNI match wins over the `*.lab` wildcard, so
removing the listener is the switch. That makes the cutover all-or-nothing, and
`git revert` undoes it. Argo CD and Forgejo use `apps-https`, which this commit
doesn't touch, so GitOps keeps working if the cutover fails.

Check after the sync:

- `kubectl -n vcluster-platform get httproute,backendtrafficpolicy,securitypolicy`:
  every policy is `Accepted=True`.
- Private nodes leave `NeedsLogin` and get a Tailscale IP.
- No `upgrade_failed` in the Envoy logs for `/coordinator/ts2021`.
- `kubectl exec` and `kubectl get -w` through vCluster Platform stay up past 5
  minutes. If idle streams drop, add a ClientTrafficPolicy with
  `timeout.http.idleTimeout` on the `https` listener. The existing
  `lab-https-paths` policy already targets that listener, so add it there.
- CORS preflight: run a `curl -X OPTIONS` with `Origin:
  https://<x>.tenant.lab.kurtmadel.com` and `Access-Control-Request-Method: GET`.
  The response should echo the origin. A non-tenant origin gets no
  `Access-Control-Allow-Origin` header.
- The snooze page loads with no CORS errors in the browser console.

**A3. Cleanup, after a few stable days.**

- Remove the test route, `platform-tls-proxy.yaml`, and the `vcp-lab-kurtmadel-com`
  Certificate. Argo CD prunes the nginx ConfigMap, Deployment, and Service.
- The Certificate has `Delete=false`, so delete it and its Secret by hand:
  `kubectl -n vcluster-platform delete certificate vcp-lab-kurtmadel-com` and
  `kubectl -n vcluster-platform delete secret vcp-lab-kurtmadel-com-tls`.

### What changes as a result

| | Today | Option A |
| --- | --- | --- |
| Hops for `vcp.lab` | Envoy (L4), nginx, loft | Envoy (L7), loft |
| Certificates | `*.lab` + `vcp.lab` | `*.lab` only |
| Cert renewal | Needs an nginx restart | Envoy hot-reloads Secrets |
| CORS | nginx `map` | SecurityPolicy |
| Body size limit | 1 MiB (nginx default) | None by default |
| HTTP/2 coalescing on `*.lab` | Can misroute `vcp.lab` | Not an issue, same listener |
| Client IP at vCluster Platform | Envoy pod IP | `X-Forwarded-For` from Envoy (node IP, due to `externalTrafficPolicy: Cluster`) |
| Experimental APIs | TLSRoute | None |

## 3. Applying this to other vCluster Platform installs

Most of what's above isn't specific to this lab. The DNS-01 certs, MetalLB, and
public DNS pointing at a private IP are. The edge requirements come from
vCluster Platform itself.

### What vCluster Platform needs from any edge

vCluster Platform serves the UI, the API, the Kubernetes proxy to tenant
clusters, the Tailscale coordinator, and DERP on **one hostname**
(`config.loftHost`). The edge in front of it must:

1. **Pass HTTP/1.1 upgrades.** It needs `websocket` (UI, newer kubectl
   exec/attach), `spdy/3.1` (older kubectl clients), and, **if private nodes are
   used**, `tailscale-control-protocol` on `POST /coordinator/ts2021`. The
   Tailscale client forces HTTP/1.1 for this request.
2. **Allow long-lived streams.** No short request timeout, and no response
   buffering on watches, log follows, the map poll, or DERP.
3. **Forward host and scheme.** Set `Host` and `X-Forwarded-Proto: https`.
   vCluster Platform builds redirects and OIDC callbacks from them.
4. **Not cap request bodies** at something small. Applies through the
   Kubernetes proxy can be large.
5. **Add CORS only if a browser page on another origin calls the API.**
   vCluster Platform adds no CORS headers itself.

Without private nodes, only the first upgrade types (`websocket`, `spdy/3.1`)
matter. Envoy Gateway's default plus `spdy/3.1` covers them, and the Tailscale
problem doesn't arise.

### How common edges handle this

| Edge | Upgrades for private nodes | Notes |
| --- | --- | --- |
| ingress-nginx (the chart's `ingress.enabled: true` default) | Works as is: nginx forwards any `Upgrade` | Raise `proxy-read-timeout`, `proxy-send-timeout`, and `proxy-body-size` via annotations. |
| Envoy Gateway | Needs `BackendTrafficPolicy.httpUpgrade` (Option A), or an `EnvoyPatchPolicy` on the HCM's `upgrade_configs` on versions without the field | Check first with `kubectl explain backendtrafficpolicy.spec.httpUpgrade`. Set `timeouts.request: 0s` on the route. |
| Other Envoy-based Gateway API implementations (kgateway, Istio, Cilium's Envoy) | Assume the same websocket-only default until tested | Find the implementation's upgrade-type setting. Run the A1 curl test before cutover. |
| L4 load balancer or TLS passthrough straight to vCluster Platform | Works: vCluster Platform is the coordinator and handles every upgrade | vCluster Platform serves a custom cert from the `PROXY_TLS_CERT` and `PROXY_TLS_KEY` env vars (chart `envValueFrom` with a `secretKeyRef`). It reads them only at startup, so restart on renewal. There's nowhere to add CORS. |
| Passthrough to a TLS-terminating proxy (this lab's old setup) | Works | Full control, but you run and patch an extra proxy and handle cert reloads. |

The A1 curl test works on any of these. Point it at a test hostname on the
candidate edge before switching the real `loftHost`.

### Certificates

- **Edge on a public IP:** HTTP-01 through the Gateway works.
- **Public DNS pointing at a private IP (this lab):** Let's Encrypt can't reach
  the edge, so use DNS-01. Routers with DNS-rebinding protection need the zone
  allowlisted.
- **Private CA:** fine for the edge, but private nodes and agents must trust it.
  Use a public CA for `loftHost` unless that trust is distributed.

### Fleet clusters

Only the cluster that hosts vCluster Platform needs the `loftHost` route.
Connected control plane clusters (agents) dial out to it. The fleet
`baseline/charts/envoy-gateway-config` chart still renders a `vcp-tls`
passthrough listener when `fleet.lab.kurtmadel.com/platform-hostname` is set.
Once Option A is proven here, change that chart to render the HTTPRoute,
BackendTrafficPolicy, and optional CORS instead. A new hub built from the fleet
templates then starts in the Option A shape.
