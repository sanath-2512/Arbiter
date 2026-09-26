FreeBSD jails on a bhyve host are reported as the bhyve hypervisor

Since bhyve host detection was added to the FreeBSD virtualization facts, gathering facts inside a FreeBSD
jail whose host runs bhyve (vmm.ko loaded on the host) reports

    ansible_virtualization_type: bhyve
    ansible_virtualization_role: host

The same jail used to be reported as `jails` / `guest`, and our roles rely on that to recognise jails. Note
that a jail shares the host's kernel, so it sees the host's loaded kernel modules. A bare-metal bhyve host
must still be reported as `bhyve` / `host`.
