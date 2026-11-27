"""The `NarwhalFleet` custom resource's API group, version and kind.

Every label, annotation, condition type, scheduling gate and Lease name the controller uses is
derived from `GROUP`. `example.com` is reserved (RFC 2606), so nobody can own the name; the
group changes once the project registers a domain, before `v1beta1`.
"""

GROUP = "narwhal.example.com"
VERSION = "v1alpha1"
KIND = "NarwhalFleet"
