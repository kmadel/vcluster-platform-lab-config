# hub/edge

The shared Envoy Gateway edge on the hub: `loft-cluster`, the control plane
cluster that runs vCluster Platform and Argo CD. The
[`hub-edge`](../../bootstrap/hub-edge.yaml) Application owns this directory and
[`hub-envoy-gateway`](../../bootstrap/hub-envoy-gateway.yaml) owns the controller
chart (Envoy Gateway v1.9.2, Envoy proxy 1.39). Argo CD only reads `*.yaml` here, so this README is
never applied.

vCluster Platform is served directly by Envoy: an HTTPRoute on the terminated
`https` listener, plus a BackendTrafficPolicy that allows the HTTP upgrades
vCluster Platform needs, plus a SecurityPolicy for CORS. TLS terminates once,
at Envoy, with the `*.lab` wildcard certificate.

## Files

| File | What it is |
| --- | --- |
| `gatewayclass.yaml` | `eg` GatewayClass |
| `envoyproxy.yaml` | Envoy data plane: 2 replicas on control-plane nodes, MetalLB VIP `192.168.50.10` |
| `gateway.yaml` | `lab` Gateway: HTTP and HTTPS listeners for `*.lab`, `*.apps.lab`, `*.tenant.lab` |
| `clienttrafficpolicy.yaml` | Keeps `%2F` in paths on `https` and `apps-https` (Argo CD) |
| `certificates.yaml` | Let's Encrypt wildcards via DNS-01 (the VIP is a private IP) |
| `platform-routes.yaml` | `vcp.lab`: port 80 redirect, and `vcluster-platform-https` to `loft:80` |
| `platform-upgrades.yaml` | Upgrade types for the Platform route |
| `platform-cors.yaml` | CORS for the `*.tenant.lab` snooze page |
| `fleet-observability-otlp-route.yaml` | `otel.lab` `POST /v1/metrics` to the observability connector |

```text
vcp.lab   :443 --SNI--> https (Terminate)  --> vcluster-platform-https --> loft:80
                                               + BackendTrafficPolicy (upgrade types)
                                               + SecurityPolicy (CORS)
*.lab     :443 --SNI--> https              --> otel.lab
*.apps    :443 --SNI--> apps-https         --> Argo CD, Forgejo
*.tenant  :443 --SNI--> tenant-https       --> tenant cluster routes
any       :80           http listeners     --> redirects
```

Argo CD pulls this repo from Forgejo through `apps-https`. A broken Gateway
therefore also cuts Argo CD off from its own Git source. To protect against
that:

- The GatewayClass, EnvoyProxy, and Gateway carry `Prune=false,Delete=false`.
- The Certificates carry `Delete=false`. Recreating one reissues it and spends
  Let's Encrypt rate limit.
- The Applications have no resources-finalizer, so deleting one leaves the edge
  running.

If routing breaks anyway, apply the fix with `kubectl apply -f hub/edge/` from a
checkout, then push the same change.

## How vCluster Platform is exposed

vCluster Platform serves everything on one hostname (`config.loftHost`): the
UI, the API, the Kubernetes proxy to tenant clusters, the Tailscale coordinator
for private nodes, and DERP. It needs three things from the edge.

### 1. The route: `platform-routes.yaml`

`vcluster-platform-https` attaches to the `https` listener (the `*.lab`
wildcard cert covers `vcp.lab`) and sends everything to the `loft` Service on
port 80. TLS terminates once, at Envoy.

`timeouts.request: 0s` turns off Envoy's default 15s route timeout. Watches,
log follows, and the Tailscale map poll are long-lived streams. A
`kubectl get -w` through vCluster Platform stayed connected for 12 minutes in
testing.

### 2. Upgrade types: `platform-upgrades.yaml`

Envoy accepts an HTTP/1.1 upgrade only if its type is on an allowlist. Envoy
Gateway's default allowlist is `websocket` alone. Any other upgrade gets a
local `403`, with `upgrade_failed` in the access log.

| Type | Used by |
| --- | --- |
| `websocket` | UI; `kubectl exec`/`attach` on newer clients; DERP from websocket-built Tailscale clients |
| `spdy/3.1` | `kubectl exec`/`port-forward` on older clients |
| `tailscale-control-protocol` | Private nodes registering: `POST /ts2021` (also served at `/coordinator/ts2021`) |
| `DERP` | Private nodes' DERP relay from standard Tailscale clients: `GET /derp` |

The list replaces the default, which is why `websocket` is listed explicitly.
Don't add `requestBuffer` to this policy: when it's set, Envoy Gateway ignores
the upgrade configuration.

### 3. CORS: `platform-cors.yaml`

The snooze/wake page on `*.tenant.lab` calls the vCluster Platform API from the
browser with an `Authorization` header. That's cross-origin, so the browser
sends a preflight first. vCluster Platform sets no CORS headers, and it strips
any it gets from backends, so the edge is the only place they can come from.

- **Preflights from allowed origins:** Envoy answers them itself, and they
  never reach vCluster Platform.
- **Allowed requests:** Envoy echoes the origin in
  `Access-Control-Allow-Origin`.
- **Other origins:** Envoy adds no allow header, so the browser blocks the
  response. Non-browser clients (kubectl, private nodes) send no `Origin` and
  aren't affected.
- **`allowOrigins` format:** a list of strings. A wildcard covers one leading
  label only, and the scheme is required.
- **No `allowCredentials`:** the page sends a bearer token in a header, not
  cookies.
- **Methods:** `GET, OPTIONS`, matching the page's read-only access key. Add
  `POST` here and on the key if the page ever wakes tenant clusters itself.
- **Scope:** attach the policy to the Platform route only. A route-level
  SecurityPolicy replaces a Gateway-level one, and at Gateway level it would
  also cover Argo CD and Forgejo.

## Checking it

```sh
# Route and policies accepted
kubectl -n vcluster-platform get httproute,backendtrafficpolicy,securitypolicy

# Upgrades reach vCluster Platform (a 400 "missing Tailscale handshake header"
# is the right answer to a handshake-less probe; a 403 means Envoy blocked it)
curl -s --http1.1 -X POST -H 'Upgrade: tailscale-control-protocol' -H 'Connection: upgrade' \
  https://vcp.lab.kurtmadel.com/ts2021
curl -s --http1.1 -o /dev/null -w '%{http_code}\n' -H 'Upgrade: DERP' -H 'Connection: Upgrade' \
  --max-time 5 https://vcp.lab.kurtmadel.com/derp          # 101

# CORS: an allowed origin is echoed back, any other origin is not
curl -s -D - -o /dev/null -X OPTIONS -H 'Origin: https://demo.tenant.lab.kurtmadel.com' \
  -H 'Access-Control-Request-Method: GET' https://vcp.lab.kurtmadel.com/version | grep -i access-control
```

How many private-node sessions Envoy is holding open right now:

```sh
kubectl -n envoy-gateway-system port-forward <envoy-pod> 19000:19000 &
curl -s 'http://127.0.0.1:19000/stats?filter=upgrades_active' | grep https
```

Upgraded connections appear in the access log only when they close. A healthy
private node is therefore mostly invisible in the log, so use the stat above.

**Expected noise:** the Tailscale client races port 80 against 443. You'll see
`POST /ts2021 403 upgrade_failed` with `:authority vcp.lab.kurtmadel.com:80` on
the redirect route. That's the losing port-80 attempt and needs no fix.

**Tenant routes must pin a listener.** On port 80, `*.lab` and `*.tenant.lab`
are merged into one Envoy route config. A tenant HTTPRoute with no
`sectionName` attaches to both, and Envoy rejects the whole config with
"Duplicate entry of domain". It then keeps serving the last good config, so
nothing breaks until that Envoy pod restarts. The `demo-frontend`
ArgoCDApplicationTemplate pins `sectionName` from the
`demo.vcluster.com/vcluster-api-gateway-http-listener-name` annotation on the
Cluster (`tenant-https` here). Do the same for any other template that creates
tenant routes. To check:

```sh
kubectl -n envoy-gateway-system logs -l gateway.envoyproxy.io/owning-gateway-name=lab \
  -c envoy --since=1h | grep -c "Duplicate entry of domain"    # 0
```

## Applying this to other vCluster Platform installs

The DNS-01 certs, MetalLB, and public DNS pointing at a private IP are specific
to this lab. The rest follows from what vCluster Platform needs.

### What any edge must provide

1. **HTTP/1.1 upgrades.**
   - Always: `websocket` and `spdy/3.1`.
   - **If private nodes are used**, also `tailscale-control-protocol` and
     `DERP`. The Tailscale client forces HTTP/1.1 for these.
2. **Long-lived streams.** No short request timeout, and no response buffering
   on watches, log follows, the map poll, or DERP.
3. **Host and scheme forwarded.** vCluster Platform builds redirects and OIDC
   callbacks from `Host` and `X-Forwarded-Proto: https`.
4. **No small request-body cap.** Applies through the Kubernetes proxy can be
   large.
5. **CORS only if a browser page on another origin calls the API.** vCluster
   Platform adds no CORS headers itself.

Without private nodes, only `websocket` and `spdy/3.1` matter.

### How common edges handle it

| Edge | Upgrades for private nodes | Notes |
| --- | --- | --- |
| Envoy Gateway (this setup) | `BackendTrafficPolicy.httpUpgrade`. On versions without the field, an `EnvoyPatchPolicy` on the HCM's `upgrade_configs` | Check with `kubectl explain backendtrafficpolicy.spec.httpUpgrade`. Set `timeouts.request: 0s` on the route. |
| ingress-nginx (the chart's `ingress.enabled: true` default) | Works as is: nginx forwards any `Upgrade` | Raise `proxy-read-timeout`, `proxy-send-timeout`, and `proxy-body-size` with annotations. |
| Other Envoy-based Gateway API implementations (kgateway, Istio, Cilium's Envoy) | Assume a websocket-only default until tested | Find the implementation's upgrade-type setting. |
| L4 load balancer or TLS passthrough straight to vCluster Platform | Works: vCluster Platform terminates and handles every upgrade | vCluster Platform serves a custom cert from `PROXY_TLS_CERT`/`PROXY_TLS_KEY` (chart `envValueFrom` with a `secretKeyRef`). It reads them only at startup, so restart after renewal. There's nowhere to add CORS. |

Before switching the real `loftHost` on any edge, put a test hostname on the
candidate edge and run the two upgrade probes from [Checking it](#checking-it):

- A `403` from the edge means the upgrade type is blocked.
- A reply from vCluster Platform means it passed.

### Certificates

- **Edge on a public IP:** HTTP-01 through the Gateway works.
- **Public DNS pointing at a private IP (this lab):** Let's Encrypt can't reach
  the edge, so use DNS-01. Routers with DNS-rebinding protection need the zone
  allowlisted.
- **Private CA:** works, but private nodes and agents must trust it. Use a
  public CA for `loftHost` unless you distribute that trust.

### Fleet clusters

Only the cluster that hosts vCluster Platform needs the `loftHost` route.
Connected control plane clusters (agents) dial out to it. The fleet
`baseline/charts/envoy-gateway-config` chart should render this route, the
upgrade policy, and optional CORS when `fleet.lab.kurtmadel.com/platform-hostname`
is set, so a hub built from the fleet templates starts in this shape. Today it
renders a TLS passthrough listener for that hostname instead; update it before
building a hub from it.
