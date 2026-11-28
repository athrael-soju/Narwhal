# Narwhal CRD chart

The chart installs the `NarwhalFleet` CustomResourceDefinition, `narwhalfleets.narwhal.example.com`, version `v1alpha1`. Install it before the `narwhal` chart, from a checkout of the repository at the release tag, since the charts are not in the wheel:

```bash
helm install narwhal-crds deploy/helm/narwhal-crds --namespace <admin namespace> --create-namespace
```

The CRD is cluster-wide, but Helm keeps the release's record in the namespace `--namespace` names, or the kubeconfig's namespace without it. Give it a namespace of its own rather than a fleet's, so the record outlives every fleet's namespace.

The CRD sits in `templates/`, so `helm upgrade` updates it. It carries `helm.sh/resource-policy: keep`, so `helm uninstall` leaves the CRD and every fleet in place. Delete the CRD by hand to remove the fleets. The `narwhal.example.com/schema-sha256` annotation holds the digest of the CRD's spec, which the controller compares with its own.

The cluster admin decides which model volumes a fleet may use, in the `narwhal` chart's values. `modelVolumeSources` allows only `persistentVolumeClaim` by default. A hostPath also needs a directory in `modelHostPathPrefixes`, an NFS export a server in `modelNfsServers`, and a CSI volume a driver in `modelCsiDrivers`. Each of those lists is empty by default, which keeps that source off. The CRD itself refuses a hostPath that is not a normalised path with `type: Directory`, and any path that is or lies in a node's system, user, log, kubelet, etcd, CNI or container runtime directory.

The chart needs Kubernetes 1.30 or later and has no values. `tools/maintenance/crd_schema.py` writes its template from `SCHEMA` in `src/narwhal/deployment/fleet_resource.py`; edit the schema, then run that tool and `tools/maintenance/fleet_reference_doc.py`. The unit suite fails while the template or the reference page differs from the schema.

The API group `narwhal.example.com` is a placeholder under a domain RFC 2606 reserves. The group and the version `v1alpha1` will change before the resource reaches `v1beta1`, once the project owns a domain. Every label, annotation, condition type, scheduling gate and Lease name derives from the group.

[NarwhalFleet reference](https://athrael-soju.github.io/Narwhal/deploy/NarwhalFleet/) lists every field, and `tools/maintenance/fleet_reference_doc.py` writes its tables from the same schema. [Deploying on Kubernetes](https://athrael-soju.github.io/Narwhal/deploy/Kubernetes/) covers the fleet controller that acts on it.
