#!/usr/bin/env bash
# Test the nginx config, then reload it. The ONLY command the `sme` service user may run as root
# (S0.5, #80). provision.sh installs this at /usr/local/sbin/sme-nginx-reload, root:root 0755 —
# deliberately outside /opt/sme/current, which `sme` owns and every deploy rewrites. A sudoers
# rule pointing into a user-writable checkout is a root shell wearing a narrow-grant costume.
#
# `nginx -t` first is not belt-and-braces, it is the whole point: this box serves four other
# projects' sites, and `deploy_site` generates vhosts from a product's `marketing_domain` at
# runtime. A reload with a broken generated config takes all of them down. Testing first turns
# that into a failed engine deploy, which is ours to fix and nobody else's outage.
set -euo pipefail

if ! nginx -t 2>&1; then
    echo "nginx-reload: config test FAILED — refusing to reload (existing config left running)" >&2
    exit 1
fi

systemctl reload nginx
echo "nginx-reload: config ok, reloaded"
