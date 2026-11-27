# Narwhal CRD chart

The chart installs the `NarwhalFleet` CustomResourceDefinition, `narwhalfleets.narwhal.example.com`, version `v1alpha1`. Install it before the `narwhal` chart:

```bash
helm install narwhal-crds deploy/helm/narwhal-crds
```

The CRD sits in `templates/`, so `helm upgrade` updates it. It carries `helm.sh/resource-policy: keep`, so `helm uninstall` leaves the CRD and every fleet in place. Delete the CRD by hand to remove the fleets. The `narwhal.example.com/schema-sha256` annotation holds the digest of the CRD's spec, which the controller compares with its own.

The chart needs Kubernetes 1.30 or later and has no values. `tools/maintenance/crd_schema.py` writes its template from `SCHEMA` in `src/narwhal/deployment/fleet_resource.py`; edit the schema, then run the tool. The unit suite fails while the template differs from the schema.

The API group `narwhal.example.com` is a placeholder under a domain RFC 2606 reserves. Every label and annotation key derives from it.
